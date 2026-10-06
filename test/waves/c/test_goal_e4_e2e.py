"""FROZEN GOAL — E4 phase-e2e-gate (phase-2 E2E). Read-only to implementers.

The phase gate: REAL uvicorn server subprocess (temp LOOM_DB, random port), REAL
admin CLI, REAL gitleaks, REAL install.sh — only the Claude CLI (not owned) is a
stub binary, and the host schedulers are PATH-shims. Everything runs against temp
homes/DBs; the pilot deployment on this machine is never touched.

Covers: AC-2.1a/b (orphan chain, no duplicates), AC-2.2a (server down → marker kept
→ next timer-style pass lands it), AC-2.3a/b end-to-end (gitleaks quarantine +
push-quarantine), AC-2.4a/c (fresh install → doctor green with the phase-2 checks).

Run: .venv/bin/python -m pytest test/waves/c/test_goal_e4_e2e.py -q
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _goalkit as kit  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("gitleaks") is None,
    reason="phase-2 E2E requires the real gitleaks binary",
)

CLEAN_SUMMARY = "## Decision\nSwept-orphan capture works end to end.\n"
LEAKY_SUMMARY = f"## Decision\nRotate the credential.\n{kit.HIGH_ENTROPY_LINE}\n"


@pytest.fixture
def server(tmp_path):
    port = kit.find_free_port()
    db = tmp_path / "server" / "loom.db"
    db.parent.mkdir(parents=True)
    proc = kit.start_server(db, port, sys.executable)
    url = f"http://127.0.0.1:{port}"
    state = {"url": url, "db": db, "proc": proc, "port": port}
    try:
        kit.wait_health(url)
        yield state
    finally:
        # The CURRENT process, not the one started above: scenario 1 restarts the server
        # to simulate an outage and rebinds state["proc"], so terminating the local
        # `proc` would leave the replacement uvicorn holding its port forever — one
        # leaked server per clean run of this goal.
        state["proc"].terminate()
        state["proc"].wait(timeout=10)


def _adduser(db: Path, name: str = "e2e-dev") -> str:
    out = subprocess.run(
        [sys.executable, "-m", "server.admin", "adduser",
         "--name", name, "--email", f"{name}@goal.test"],
        env={**os.environ, "LOOM_DB": str(db)}, cwd=str(kit.REPO_ROOT),
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout
    for line in out.splitlines():
        candidate = line.strip()
        if len(candidate) >= 32 and re.fullmatch(r"[A-Za-z0-9_-]+", candidate):
            return candidate
    raise AssertionError(f"could not parse token from adduser output:\n{out}")


def _register_project(url: str, token: str) -> int:
    resp = requests.post(f"{url}/api/projects",
                         json={"remote_url": kit.TEST_REMOTE_URL},
                         headers={"Authorization": f"Bearer {token}"}, timeout=10)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _entries(url: str, token: str, project_id: int) -> list[dict]:
    resp = requests.get(f"{url}/api/projects/{project_id}/entries",
                        headers={"Authorization": f"Bearer {token}"}, timeout=10)
    assert resp.status_code == 200, resp.text
    return resp.json()["entries"]


def _client_env(home: Path) -> dict:
    return {**os.environ, "LOOM_HOME": str(home)}


def _loom(args: list[str], home: Path, timeout: int = 300):
    return subprocess.run([sys.executable, "-m", "loom.cli", *args],
                          env=_client_env(home), cwd=str(kit.REPO_ROOT),
                          capture_output=True, text=True, timeout=timeout)


def _setup_client(tmp_path, server, *, summary: str, sid: str) -> tuple[Path, Path]:
    """Temp LOOM_HOME wired to the real server + one settled orphan transcript."""
    home = kit.loom_home(tmp_path)
    repo = kit.make_git_repo(tmp_path / "repo")
    root = tmp_path / "transcripts"
    t = root / "-Users-nobody-x" / f"{sid}.jsonl"
    kit.write_transcript(t, kit.transcript_lines(str(repo), sid))
    kit.age_file(t, 7200)
    stub = kit.make_claude_stub(tmp_path / "claude_stub.py", stdout=summary)
    kit.write_config(home, server_url=server["url"], token=server["token"],
                     claude_bin=stub, settle_s=1800, transcripts_root=root,
                     scanner_engine="auto")
    return home, t


# --------------------------------------------------------------------------- #
# Scenario 1 — orphan chain with server outage (AC-2.1a, AC-2.1b, AC-2.2a)     #
# --------------------------------------------------------------------------- #
def test_orphan_survives_outage_lands_once(tmp_path, server):
    token = _adduser(server["db"])
    server["token"] = token
    project_id = _register_project(server["url"], token)
    home, _ = _setup_client(tmp_path, server, summary=CLEAN_SUMMARY, sid="e4-orphan")

    # Outage: stop the real server, run the pass the timer would run.
    server["proc"].terminate()
    server["proc"].wait(timeout=10)
    p1 = _loom(["process"], home)
    assert p1.returncode == 0, p1.stderr
    markers = list((home / "queue").glob("*.json"))
    assert len(markers) == 1, "sweep must enqueue even with the server down"
    assert json.loads(markers[0].read_text())["origin"] == "sweep"

    # Server back (same DB, same port): next timer-style pass lands the entry.
    server["proc"] = kit.start_server(server["db"], server["port"], sys.executable)
    kit.wait_health(server["url"])
    p2 = _loom(["process"], home)
    assert p2.returncode == 0, p2.stderr
    entries = _entries(server["url"], token, project_id)
    assert len(entries) == 1, f"exactly one entry; process output:\n{p2.stdout}"
    assert entries[0]["session_id"] == "e4-orphan"
    assert "Decision" in entries[0]["content_md"]
    assert not list((home / "queue").glob("*.json"))

    # Third pass: nothing new (pointer + content_hash + no re-sweep) — AC-2.1b.
    p3 = _loom(["process"], home)
    assert p3.returncode == 0, p3.stderr
    assert len(_entries(server["url"], token, project_id)) == 1


# --------------------------------------------------------------------------- #
# Scenario 2 — secret chain (AC-2.3a/b end-to-end)                             #
# --------------------------------------------------------------------------- #
def test_leaky_summary_quarantined_then_pushed_after_redaction(tmp_path, server):
    token = _adduser(server["db"], name="e2e-sec")
    server["token"] = token
    project_id = _register_project(server["url"], token)
    home, _ = _setup_client(tmp_path, server, summary=LEAKY_SUMMARY, sid="e4-leak")

    p1 = _loom(["process"], home)
    assert p1.returncode == 0, p1.stderr
    assert _entries(server["url"], token, project_id) == [], "leak must NOT be posted"
    qfiles = list((home / "quarantine").glob("*.json"))
    assert len(qfiles) == 1, "gitleaks-detected secret must land in quarantine"

    record = json.loads(qfiles[0].read_text())
    entry = record["entry"]
    entry["content_md"] = entry["content_md"].replace(kit.HIGH_ENTROPY_SECRET,
                                                      "[REDACTED]")
    qfiles[0].write_text(json.dumps(record), encoding="utf-8")

    p2 = _loom(["push-quarantine", qfiles[0].name], home)
    assert p2.returncode == 0, p2.stdout + p2.stderr
    entries = _entries(server["url"], token, project_id)
    assert len(entries) == 1 and "[REDACTED]" in entries[0]["content_md"]
    assert not list((home / "quarantine").glob("*.json"))


# --------------------------------------------------------------------------- #
# Scenario 3 — fresh install → doctor green (AC-2.4a, AC-2.4c)                 #
# --------------------------------------------------------------------------- #
def test_fresh_install_leaves_doctor_green(tmp_path, server):
    token = _adduser(server["db"], name="e2e-doc")
    home = tmp_path / "home"
    home.mkdir()
    loomhome = home / ".loom"
    shims = kit.shim_dir(tmp_path)
    cron_state = tmp_path / "cron_state.txt"
    kit.crontab_shim(shims, cron_state)
    contract_version = (kit.REPO_ROOT / "contract" / "VERSION").read_text().strip()
    kit.write_shim(shims, "claude", f"""
import sys
if "--version" in sys.argv:
    sys.stdout.write({contract_version!r} + "\\n")
sys.exit(0)
""")
    # WHY THE HOST SCHEDULERS MUST BE UNREACHABLE HERE — do not widen this PATH.
    # This scenario runs the REAL client/install.sh, whose step 5 arms a periodic trigger:
    # a systemd *user* timer when `systemctl --user` answers, a crontab entry otherwise.
    # Both are invoked through PATH, so PATH is the only thing standing between this test
    # and the developer's own machine. A PATH that keeps the whole real environment
    # (`base_path_with(..., include_gitleaks=True)`) leaves the real `systemctl` reachable
    # on Linux/WSL2, where the installer would then run `systemctl --user enable --now
    # loom-process.timer` against the live user manager, arming a timer that points at this
    # test's temp LOOM_HOME — which pytest deletes, leaving a broken unit firing every 15
    # minutes forever. Doctor's trigger check probes the same schedulers, so the host's own
    # timer would also make the assertions below pass for the wrong reason.
    # Hence: a tail that is an allowlist containing NEITHER `crontab` NOR `systemctl`, plus
    # the real `gitleaks` (the scanner row needs the real binary). With no `systemctl`
    # anywhere, install.sh and doctor take the cron branch on EVERY platform, and the only
    # `crontab` they can find is the file-backed recorder shim above.
    env = {**os.environ, "HOME": str(home), "LOOM_HOME": str(loomhome),
           "LOOM_SKIP_VENV": "1",
           "PATH": kit.scheduler_free_path_with_gitleaks(shims)}
    assert shutil.which("systemctl", path=env["PATH"]) is None, (
        "the host systemctl must be unreachable: install.sh would arm a real user timer"
    )
    assert shutil.which("crontab", path=env["PATH"]) == str(shims / "crontab"), (
        "the only reachable crontab must be the recorder shim, never the host's"
    )
    assert shutil.which("gitleaks", path=env["PATH"]) is not None, (
        "the real gitleaks must stay reachable for the scanner row"
    )

    ins = subprocess.run(["bash", str(kit.REPO_ROOT / "client" / "install.sh"),
                          "--yes"],
                         env=env, capture_output=True, text=True,
                         stdin=subprocess.DEVNULL, timeout=180)
    assert ins.returncode == 0, ins.stderr

    # The trigger branch is now a property of the PATH, not of the build host: prove the
    # installer really went through the cron recorder and never near a user manager.
    assert "cron entry installed" in ins.stdout, (
        f"install.sh must take the cron branch on every platform:\n{ins.stdout}"
    )
    assert not (home / ".config" / "systemd").exists(), (
        "the systemd branch must not be reachable, so no user units may be written"
    )
    cron_calls = Path(str(cron_state) + ".calls").read_text().splitlines()
    assert cron_calls == ["-l", "-"], (
        f"the cron branch must read then rewrite the crontab, once, through the shim; "
        f"recorded: {cron_calls}"
    )
    installed_cron = cron_state.read_text()
    assert "# loom-process" in installed_cron
    assert str(loomhome) + "/venv/bin/loom process" in installed_cron, (
        "the shimmed crontab must carry THIS test's temp LOOM_HOME, not a host path"
    )

    # ACCESSORY REPAIR (2026-09-14, found via CI on main, not by this wave's own run):
    # `--token=<value>`, never `"--token", value`. `server.admin` mints tokens with
    # `secrets.token_urlsafe`, whose alphabet includes `-`, so ~1 token in 64 STARTS with
    # one — and argparse reads a leading `-` as the next OPTION, failing with
    # "argument --token: expected one argument" before `loom init` runs at all. That made
    # this goal fail at random, on ~1.6% of runs, for a reason that has nothing to do with
    # what it asserts; it is why `main` was red at ec2ee05. The `=` form binds the value to
    # the flag and is immune to any token shape.
    #
    # This weakens no assertion — it lets the frozen assertions below be REACHED, the same
    # repair `_goalkit.parse_admin_token` records for the same class of accessory bug.
    init = subprocess.run([sys.executable, "-m", "loom.cli", "init",
                           f"--server-url={server['url']}", f"--token={token}"],
                          env=env, cwd=str(kit.REPO_ROOT),
                          capture_output=True, text=True, timeout=60)
    assert init.returncode == 0, init.stdout + init.stderr

    doc = subprocess.run([sys.executable, "-m", "loom.cli", "doctor"],
                         env=env, cwd=str(kit.REPO_ROOT),
                         capture_output=True, text=True, timeout=120)
    assert doc.returncode == 0, f"doctor must be green:\n{doc.stdout}\n{doc.stderr}"
    low = doc.stdout.lower()
    assert "loom-process" in low, "trigger check missing from doctor"
    assert "sweep" in low, "sweep last-run check missing from doctor"
    assert "gitleaks" in low, "scanner engine check missing from doctor"

    # Doctor probes the same schedulers install.sh does, so pin WHICH one answered: the
    # trigger row must be green because it found the entry the installer wrote in the
    # shimmed crontab — never because the host happens to have a loom-process timer.
    trigger_line = next(l for l in doc.stdout.splitlines() if "loom-process" in l)
    assert "✓" in trigger_line, f"trigger must be green: {trigger_line}"
    assert "crontab" in trigger_line.lower(), (
        f"doctor must have verified the trigger through the crontab shim: {trigger_line}"
    )
    # The scanner row quotes a version, which only the real gitleaks binary can supply.
    scanner_line = next(l for l in doc.stdout.splitlines() if "gitleaks" in l.lower())
    assert re.search(r"\d+\.\d+\.\d+", scanner_line), (
        f"scanner row must come from the real gitleaks binary: {scanner_line}"
    )
