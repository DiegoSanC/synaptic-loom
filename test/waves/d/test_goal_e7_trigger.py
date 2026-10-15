"""FROZEN GOAL — E7 trigger-hardening (P2.F5..P2.F8). Read-only to implementers.

Independent real-boundary oracle: the REAL ``client/install.sh`` runs as a subprocess
against a temp HOME (``LOOM_SKIP_VENV=1``); the host schedulers are PATH-shimmed
record-replay stubs (a file-backed ``crontab`` that faithfully distinguishes ``-l`` /
``-`` / ``-r``, and a ``systemctl`` that mirrors the pilot host: *degraded* state word
with a NONZERO exit). The REAL ``gitleaks`` binary is COPIED into a controlled temp
"toolbox" dir at setup — never assumed at /opt/homebrew/bin — so each test decides
whether the interactive and/or trigger contexts can see it. AC-F5.1 executes the exact
command line the installer wrote into the (shimmed) crontab, under a minimal
cron-like environment plus ONLY the env-assignment lines the recorded block itself
carries (never re-derived), against a REAL uvicorn server: an implementation that
fixes doctor's *reporting* but not the actual trigger environment publishes the
seeded secret and FAILS here.

HOST SAFETY (wave-c "Frozen-artifact deviation" discipline): every PATH handed to
``install.sh``/doctor is shims + an allowlist tail that contains NEITHER ``crontab``
NOR ``systemctl``, and the trigger-context baseline is a controlled allowlist dir
(scheduler-free, gitleaks-free) rather than the literal ``/usr/bin:/bin`` — same
minimal-PATH property, zero reachability of the host schedulers on any platform.
``loom process`` itself never invokes a scheduler binary.

Whitelist split (amended P2.F5 interface): command-field values keep the wave-b
class ``^[A-Za-z0-9_./@+-]+$`` verbatim; env-assignment-line values get their own
class that additionally allows ``:`` (newline/CR/``%`` still rejected). This goal
never forces the colon-free class onto env lines.

Expected TODAY (pre-E7): collects cleanly; fails ONLY on the new contract —
trigger-context parity (F5.1/F5.2), doctor divergence visibility (F5.5), crontab
removal semantics (F6.1), the doctor unit-diff row (F7.1–F7.3), and the e2e marker
registration (F8.1). Wave-b-inherited disciplines (idempotency, foreign-entry
byte-exact survival, backups, no-crontab no-op) pass today and must keep passing.

Run: .venv/bin/python -m pytest test/waves/d/test_goal_e7_trigger.py -q
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

INSTALL = kit.REPO_ROOT / "client" / "install.sh"
CRON_TAG = "# loom-process"
CMD_MARKER = "/venv/bin/loom process"

# Amended P2.F5 whitelists, split by destination (see module docstring).
CMD_VALUE_RX = re.compile(r"[A-Za-z0-9_./@+-]+")          # command field (via /bin/sh)
ENV_VALUE_RX = re.compile(r"[A-Za-z0-9_./@+:-]*")         # env-assignment lines (+ ':')
ASSIGNMENT_RX = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

HAVE_GITLEAKS = shutil.which("gitleaks") is not None
requires_gitleaks = pytest.mark.skipif(
    not HAVE_GITLEAKS, reason="this scenario needs the real gitleaks binary")

LEAKY_SUMMARY = f"## Decision\nRotate the credential.\n{kit.HIGH_ENTROPY_LINE}\n"

# Foreign crontab content the goals must keep byte-exact (wave-b HIGH-1/MED-6
# discipline). The bare foreign `PATH=` assignment is deliberate: it kills any
# implementation that removes its own parity line by PREFIX instead of by line
# identity — PATH is a shared namespace, unlike `LOOM_HOME=` (accepted deviation).
FOREIGN_LINES = [
    "# nightly backup",
    "@daily keepme-backup.sh",
    "PATH=/foreign-keep/bin:/usr/bin",
    "0 3 * * * /usr/local/bin/backup.sh",
]


# --------------------------------------------------------------------------- #
# Controlled PATH construction (host-safe on every platform)                   #
# --------------------------------------------------------------------------- #
def _safe_bin(tmp: Path) -> Path:
    """Allowlist tail: the essentials, NO crontab, NO systemctl, NO gitleaks.

    Local twin of the wave-c fixed goal's tail (see _goalkit.SAFE_BIN_NAMES and
    scheduler_free_path_with_gitleaks): built locally so this frozen oracle does not
    depend on private kit helpers, and so gitleaks lives in its OWN dir (below).
    """
    d = tmp / "goal_safe_bin"
    d.mkdir(exist_ok=True)
    for name in kit.SAFE_BIN_NAMES:
        target = shutil.which(name)
        if target and not (d / name).exists():
            (d / name).symlink_to(target)
    return d


def _toolbox(tmp: Path) -> Path:
    """A COPY (not a symlink) of the real gitleaks in a controlled temp dir.

    Discovered via shutil.which at setup time — never assumed at /opt/homebrew/bin.
    A copy, so that even a realpath()-ing implementation derives a tmp-rooted pin:
    any '/opt/homebrew' (or '/usr/local') string later found in what the installer
    wrote is proof of a baked-in host guess, not of install-time resolution.
    """
    d = tmp / "goal_toolbox"
    d.mkdir(exist_ok=True)
    dst = d / "gitleaks"
    if not dst.exists():
        real = shutil.which("gitleaks")
        if real is None:
            pytest.skip("no gitleaks binary on this host")
        shutil.copy2(real, dst)
    return d


def _interactive_path(tmp: Path, shims: Path, *, with_gitleaks: bool) -> str:
    parts = [str(shims), str(_safe_bin(tmp))]
    if with_gitleaks:
        parts.append(str(_toolbox(tmp)))
    return os.pathsep.join(parts)


def _assert_host_safe(path: str, shims: Path, *, systemd_shimmed: bool) -> None:
    """The wave-c lesson, asserted on every PATH that reaches install.sh/doctor."""
    sysd = shutil.which("systemctl", path=path)
    if systemd_shimmed:
        assert sysd == str(shims / "systemctl"), (
            "the only reachable systemctl must be the recorder shim, never the host's")
    else:
        assert sysd is None, (
            "the host systemctl must be unreachable: install.sh would arm a real "
            "user timer (wave-c frozen-artifact deviation)")
    assert shutil.which("crontab", path=path) == str(shims / "crontab"), (
        "the only reachable crontab must be the recorder shim, never the host's")


# --------------------------------------------------------------------------- #
# Scheduler shims (record-replay; state in files so assertions read reality)   #
# --------------------------------------------------------------------------- #
def _crontab_shim(shims: Path, state_file: Path) -> Path:
    """File-backed crontab that faithfully distinguishes -l / - / -r / <file>.

    Unlike the kit's shim, "no crontab" is the ABSENCE of the state file, `-r`
    deletes it, and installing writes it — so AC-F6.1 can tell "crontab removed"
    apart from "empty crontab installed" (the exact bug in RUNBOOK §7 finding 2).
    """
    return kit.write_shim(shims, "crontab", f"""
import pathlib, sys
state = pathlib.Path({str(state_file)!r})
calls = pathlib.Path({str(state_file)!r} + ".calls")
calls.parent.mkdir(parents=True, exist_ok=True)
with calls.open("a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if "-l" in args:
    if state.exists():
        sys.stdout.write(state.read_text())
        sys.exit(0)
    sys.stderr.write("crontab: no crontab for user\\n")
    sys.exit(1)
if "-r" in args:
    if state.exists():
        state.unlink()
        sys.exit(0)
    sys.stderr.write("crontab: no crontab for user\\n")
    sys.exit(1)
if "-" in args:
    state.write_text(sys.stdin.read())
    sys.exit(0)
plain = [a for a in args if not a.startswith("-")]
if len(plain) == 1:  # `crontab <file>` install form
    state.write_text(pathlib.Path(plain[0]).read_text())
    sys.exit(0)
sys.stderr.write("crontab shim: unsupported argv: %r\\n" % (args,))
sys.exit(64)
""")


def _degraded_systemctl_shim(shims: Path, log_file: Path) -> Path:
    """systemctl recorder mirroring the WSL2 pilot host: `is-system-running` prints
    the state word `degraded` and exits NONZERO (real systemd does). An installer or
    doctor that regressed to exit-code checking takes the wrong branch and fails the
    systemd-side assertions (the wave-b state-word discipline, exercised for real).
    """
    return kit.write_shim(shims, "systemctl", f"""
import pathlib, sys
log = pathlib.Path({str(log_file)!r})
log.parent.mkdir(parents=True, exist_ok=True)
with log.open("a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if "is-system-running" in args:
    sys.stdout.write("degraded\\n")
    sys.exit(1)
if "is-enabled" in args:
    sys.stdout.write("enabled\\n")
    sys.exit(0)
if "is-active" in args:
    sys.stdout.write("active\\n")
    sys.exit(0)
sys.exit(0)
""")


def _claude_version_shim(shims: Path) -> None:
    version = (kit.REPO_ROOT / "contract" / "VERSION").read_text().strip()
    kit.write_shim(shims, "claude", f"""
import sys
if "--version" in sys.argv:
    sys.stdout.write({version!r} + "\\n")
sys.exit(0)
""")


# --------------------------------------------------------------------------- #
# Temp-home plumbing                                                           #
# --------------------------------------------------------------------------- #
def _fresh_home(tmp: Path) -> tuple[Path, Path]:
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    loomhome = home / ".loom"
    for sub in ("queue", "quarantine", "failed", "log", "prompts", "hooks"):
        (loomhome / sub).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(kit.REAL_PROMPT, loomhome / "prompts" / "summarizer_v1.md")
    return home, loomhome


def _install_loom_wrapper(loomhome: Path) -> None:
    """Stand-in for the venv console script (LOOM_SKIP_VENV=1 builds none): a /bin/sh
    wrapper at the exact path the crontab points to, exec-ing THIS checkout's
    interpreter with THIS checkout first on PYTHONPATH (worktree-safe)."""
    bin_dir = loomhome / "venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "loom"
    wrapper.write_text(
        "#!/bin/sh\n"
        + f'PYTHONPATH="{kit.REPO_ROOT}/client:{kit.REPO_ROOT}'
        + '${PYTHONPATH:+:$PYTHONPATH}"\n'
        + "export PYTHONPATH\n"
        + f'exec "{sys.executable}" -m loom.cli "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)


def _claude_stub(path: Path, stdout_text: str) -> str:
    """Summarizer stub with an ABSOLUTE-interpreter shebang: must run under the
    trigger's minimal PATH, where `/usr/bin/env python3` is not guaranteed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdin.read()\n"
        f"sys.stdout.write({stdout_text!r})\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path)


def _pythonpath() -> str:
    base = f"{kit.REPO_ROOT}/client{os.pathsep}{kit.REPO_ROOT}"
    prev = os.environ.get("PYTHONPATH")
    return base + (os.pathsep + prev if prev else "")


def _run_install(args: list[str], *, home: Path, loomhome: Path, path: str,
                 timeout: int = 180) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home), "LOOM_HOME": str(loomhome),
           "LOOM_SKIP_VENV": "1", "LOOM_SKIP_GITLEAKS": "1", "PATH": path, "PYTHONPATH": _pythonpath()}
    return subprocess.run(["bash", str(INSTALL), *args], env=env,
                          capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=timeout)


def _run_doctor(*, home: Path, loomhome: Path, path: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home), "LOOM_HOME": str(loomhome),
           "PATH": path, "PYTHONPATH": _pythonpath()}
    return subprocess.run([sys.executable, "-m", "loom.cli", "doctor"], env=env,
                          cwd=str(kit.REPO_ROOT), capture_output=True, text=True,
                          timeout=120)


def _seed_fresh_sweep_record(loomhome: Path, age_s: int = 60) -> None:
    import time as _t
    from datetime import datetime, timezone
    ts = datetime.fromtimestamp(_t.time() - age_s, tz=timezone.utc)
    (loomhome / "sweep_last_run.json").write_text(json.dumps({
        "finished_at": ts.isoformat().replace("+00:00", "Z"),
        "scanned": 3, "enqueued": 0, "skipped": 3}))


# --------------------------------------------------------------------------- #
# Crontab-content parsing                                                      #
# --------------------------------------------------------------------------- #
def _cron_lines(state: Path) -> list[str]:
    return state.read_text(encoding="utf-8").splitlines()

def _assignments(lines: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = ASSIGNMENT_RX.match(line)
        if m:
            out.append((m.group(1), m.group(2)))
    return out

def _command_lines(lines: list[str]) -> list[str]:
    return [l for l in lines
            if not l.lstrip().startswith("#") and CMD_MARKER in l]

def _backups(loomhome: Path) -> list[Path]:
    d = loomhome / "backup"
    return sorted(d.glob("crontab.*")) if d.is_dir() else []

def _some_backup_equals(loomhome: Path, content: str) -> bool:
    want = content.rstrip("\n")
    return any(p.read_text(encoding="utf-8").rstrip("\n") == want
               for p in _backups(loomhome))

def _assert_ordered_byte_exact(haystack: list[str], needles: list[str], msg: str) -> None:
    idx = -1
    for needle in needles:
        try:
            idx = haystack.index(needle, idx + 1)
        except ValueError:
            raise AssertionError(f"{msg}: {needle!r} missing or out of order in "
                                 f"{haystack!r}") from None


# --------------------------------------------------------------------------- #
# Real server (AC-F5.1 needs the registry + the POST that a degraded engine    #
# would let through)                                                           #
# --------------------------------------------------------------------------- #
@pytest.fixture
def server(tmp_path):
    port = kit.find_free_port()
    db = tmp_path / "server" / "loom.db"
    db.parent.mkdir(parents=True)
    proc = kit.start_server(db, port, sys.executable)
    url = f"http://127.0.0.1:{port}"
    try:
        kit.wait_health(url)
        yield {"url": url, "db": db}
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _adduser(db: Path, name: str = "e7-dev") -> str:
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


# =========================================================================== #
# P2.F5 — Trigger-context scanner parity                                       #
# =========================================================================== #
@requires_gitleaks
def test_f5_1_cron_context_run_uses_gitleaks_for_auto_and_explicit(tmp_path, server):
    """AC-F5.1: the command line the installer wrote, executed under the trigger's
    baseline environment plus ONLY the assignments the recorded block itself carries,
    must resolve the gitleaks engine — for engine=auto AND explicit gitleaks."""
    token = _adduser(server["db"])
    project_id = _register_project(server["url"], token)

    home, loomhome = _fresh_home(tmp_path)
    repo = kit.make_git_repo(tmp_path / "repo")
    root = tmp_path / "transcripts"
    t_a = root / "-Users-nobody-x" / "e7-auto.jsonl"
    kit.write_transcript(t_a, kit.transcript_lines(str(repo), "e7-auto"))
    kit.age_file(t_a, 7200)
    stub = _claude_stub(tmp_path / "claude_stub.py", LEAKY_SUMMARY)
    # Config exists BEFORE the install so an additive-config-key parity mechanism
    # (frozen interface p3-trigger-env-block) has a config to extend.
    kit.write_config(loomhome, server_url=server["url"], token=token,
                     claude_bin=stub, settle_s=1800, transcripts_root=root,
                     scanner_engine="auto")
    _install_loom_wrapper(loomhome)

    shims = kit.shim_dir(tmp_path)
    cron_state = tmp_path / "cron_state.txt"
    _crontab_shim(shims, cron_state)
    ipath = _interactive_path(tmp_path, shims, with_gitleaks=True)
    _assert_host_safe(ipath, shims, systemd_shimmed=False)
    toolbox = _toolbox(tmp_path)
    assert shutil.which("gitleaks", path=ipath) == str(toolbox / "gitleaks"), (
        "the interactive install context must resolve ONLY the controlled gitleaks")

    ins = _run_install(["--yes"], home=home, loomhome=loomhome, path=ipath)
    assert ins.returncode == 0, ins.stderr
    assert "cron entry installed" in ins.stdout, ins.stdout

    lines = _cron_lines(cron_state)
    assert any(l.strip() == CRON_TAG for l in lines)
    cmds = _command_lines(lines)
    assert len(cmds) == 1
    fields = cmds[0].split(None, 5)
    assert len(fields) == 6, f"not a five-field cron entry: {cmds[0]!r}"
    cron_cmd = fields[5]
    # HIGH-2 discipline, amended split: command-field value keeps the wave-b class...
    assert CMD_VALUE_RX.fullmatch(cron_cmd.split()[0]), cron_cmd
    # ...and env-assignment values use the extended class (':' allowed, %/CR never).
    assignments = _assignments(lines)
    for key, value in assignments:
        assert ENV_VALUE_RX.fullmatch(value), (
            f"env-assignment value outside the amended whitelist: {key}={value!r}")
    assert any(k == "LOOM_HOME" and v == str(loomhome) for k, v in assignments), (
        "the block must keep carrying LOOM_HOME= (wave-b MED-5)")
    # Kill the baked-in-host-guess implementation: the install-time environment was
    # fully controlled (tmp-rooted toolbox copy), so NO legitimate derivation can
    # produce a homebrew/usr-local path in anything the installer wrote.
    for label, text in (("crontab", "\n".join(lines)),
                        ("config.json", (loomhome / "config.json").read_text())):
        assert "/opt/homebrew" not in text and "/usr/local" not in text, (
            f"host-specific guess in {label}: parity must derive from install-time "
            f"resolution, not a hardcoded location:\n{text}")

    # The trigger's baseline: minimal, controlled, gitleaks-free, scheduler-free on
    # every platform (host-safety: same property as cron's /usr/bin:/bin for this
    # test, with zero reachability of the developer's crontab/systemctl).
    baseline = _safe_bin(tmp_path)
    for absent in ("gitleaks", "crontab", "systemctl"):
        assert shutil.which(absent, path=str(baseline)) is None
    trig_env = {"HOME": str(home), "SHELL": "/bin/sh", "LOGNAME": "goal",
                "PATH": str(baseline)}
    for key, value in assignments:      # the block's own lines, verbatim, in order
        trig_env[key] = value

    run1 = subprocess.run(["/bin/sh", "-c", cron_cmd], env=trig_env, cwd=str(home),
                          capture_output=True, text=True, timeout=600)
    assert run1.returncode == 0, (
        f"the trigger-context pass must run:\n{run1.stdout}\n{run1.stderr}")

    # AC-F5.1 kill: under a degraded (regex) engine the seeded high-entropy secret is
    # invisible (E3-goal guard) and gets PUBLISHED; gitleaks quarantines it instead.
    posted = _entries(server["url"], token, project_id)
    assert posted == [], (
        "AC-F5.1: engine=auto under the trigger environment silently degraded to "
        f"regex — the secret-bearing entry was published:\n{posted}")
    qfiles = sorted((loomhome / "quarantine").glob("*.json"))
    assert len(qfiles) == 1, (
        f"AC-F5.1: exactly one quarantined entry expected; process said:\n"
        f"{run1.stdout}\n{run1.stderr}")
    rec_a = json.loads(qfiles[0].read_text())
    assert rec_a["findings"], "quarantine record must carry findings"
    assert all(f["kind"] != "scan_error" for f in rec_a["findings"]), (
        f"quarantine must come from real findings, not a scanner failure: "
        f"{rec_a['findings']}")
    assert kit.HIGH_ENTROPY_SECRET in rec_a["entry"]["content_md"]

    # Explicit engine=gitleaks under the same trigger environment (frozen observable
    # contract p3-trigger-env-block: parity holds for auto AND explicit gitleaks).
    cfg_path = loomhome / "config.json"
    cfg_data = json.loads(cfg_path.read_text())      # preserve any additive keys
    cfg_data["scanner"]["engine"] = "gitleaks"
    cfg_path.write_text(json.dumps(cfg_data), encoding="utf-8")
    t_b = root / "-Users-nobody-x" / "e7-expl.jsonl"
    kit.write_transcript(t_b, kit.transcript_lines(str(repo), "e7-expl"))
    kit.age_file(t_b, 7200)

    run2 = subprocess.run(["/bin/sh", "-c", cron_cmd], env=trig_env, cwd=str(home),
                          capture_output=True, text=True, timeout=600)
    assert run2.returncode == 0, run2.stdout + run2.stderr
    assert _entries(server["url"], token, project_id) == []
    qfiles = sorted((loomhome / "quarantine").glob("*.json"))
    assert len(qfiles) == 2, f"second session must be quarantined too: {qfiles}"
    rec_b = json.loads(next(p for p in qfiles if p.name.startswith("e7-expl")).read_text())
    assert rec_b["findings"] and all(f["kind"] != "scan_error" for f in rec_b["findings"]), (
        "AC-F5.1 (explicit gitleaks): the trigger context must RESOLVE the binary — "
        "a scan_error quarantine means it fell back to fail-closed instead of "
        f"achieving parity: {rec_b['findings']}")


@requires_gitleaks
def test_f5_2_systemd_branch_written_units_guarantee_parity(tmp_path):
    """AC-F5.2: the WRITTEN unit content (plus, alternatively, an additive config
    pin) must guarantee trigger-context gitleaks resolution — host systemd never
    invoked; the shim answers `degraded` with a NONZERO exit (state-word discipline).
    Also the systemd leg of AC-F5.3: a second install is a fixpoint."""
    home, loomhome = _fresh_home(tmp_path)
    kit.write_config(loomhome, server_url="http://127.0.0.1:1", scanner_engine="auto")
    shims = kit.shim_dir(tmp_path)
    cron_state = tmp_path / "cron_state.txt"
    _crontab_shim(shims, cron_state)
    _degraded_systemctl_shim(shims, tmp_path / "systemctl.log")
    ipath = _interactive_path(tmp_path, shims, with_gitleaks=True)
    _assert_host_safe(ipath, shims, systemd_shimmed=True)
    toolbox = _toolbox(tmp_path)

    ins = _run_install(["--yes"], home=home, loomhome=loomhome, path=ipath)
    assert ins.returncode == 0, ins.stderr
    unit_dir = home / ".config" / "systemd" / "user"
    svc = unit_dir / "loom-process.service"
    timer = unit_dir / "loom-process.timer"
    assert svc.is_file() and timer.is_file(), (
        "a `degraded` (nonzero-exit) user manager is USABLE: the installer must "
        "take the systemd branch by state word, not by exit code")
    assert not cron_state.exists() or CRON_TAG not in cron_state.read_text(), (
        "systemd hosts must not ALSO get a cron block")

    svc_text = svc.read_text(encoding="utf-8")
    timer_text = timer.read_text(encoding="utf-8")
    # Inherited resolution (wave-b MED-5) — still exact.
    assert f"ExecStart={loomhome}/venv/bin/loom process --quiet" in svc_text, svc_text
    unit_env: dict[str, str] = {}
    for line in svc_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("Environment="):
            continue
        payload = stripped[len("Environment="):].strip()
        if payload[:1] in {'"', "'"} and payload[-1:] == payload[:1]:
            payload = payload[1:-1]
        if "=" in payload:
            k, v = payload.split("=", 1)
            unit_env[k] = v
    assert unit_env.get("LOOM_HOME") == str(loomhome), svc_text
    assert "OnUnitActiveSec=15min" in timer_text

    # No baked-in host guess anywhere the installer wrote (see F5.1 rationale).
    cfg_text = (loomhome / "config.json").read_text(encoding="utf-8")
    for label, text in (("service", svc_text), ("timer", timer_text),
                        ("config.json", cfg_text)):
        assert "/opt/homebrew" not in text and "/usr/local" not in text, (
            f"host-specific guess in {label}:\n{text}")

    # AC-F5.2 kill (fails today): the trigger context the unit defines must reach the
    # CONTROLLED gitleaks. Accepted mechanisms (p3-trigger-env-block):
    #   (a) an Environment=PATH= (or equivalent) making which('gitleaks') land in the
    #       controlled tmp tree over a minimal baseline;
    #   (b) any tmp-rooted gitleaks reference in the written unit text;
    #   (c) an additive config key pinning a tmp-rooted gitleaks path.
    def _controlled_component(component: str) -> bool:
        if not component.startswith(str(tmp_path)):
            return False
        p = Path(component)
        if p.name == "gitleaks" and p.is_file() and os.access(p, os.X_OK):
            return True
        return p.is_dir() and os.access(p / "gitleaks", os.X_OK)

    evidence = []
    if "PATH" in unit_env:
        hit = shutil.which("gitleaks", path=unit_env["PATH"])
        if hit and hit.startswith(str(tmp_path)):
            evidence.append(f"Environment PATH resolves {hit}")
    for token in re.split(r"""[\s"'=:]+""", svc_text + "\n" + timer_text):
        if _controlled_component(token):
            evidence.append(f"unit references {token}")
            break
    try:
        cfg_strings: list[str] = []
        def _walk(x):
            if isinstance(x, dict):
                for v in x.values():
                    _walk(v)
            elif isinstance(x, list):
                for v in x:
                    _walk(v)
            elif isinstance(x, str):
                cfg_strings.append(x)
        _walk(json.loads(cfg_text))
        for s in cfg_strings:
            if any(_controlled_component(c) for c in s.split(os.pathsep)):
                evidence.append(f"config pins {s}")
                break
    except json.JSONDecodeError:
        pass
    assert evidence, (
        "AC-F5.2: neither the written units nor an additive config key carry the "
        "environment needed for trigger-context gitleaks resolution — the systemd "
        f"timer would silently degrade to regex.\nservice:\n{svc_text}\n"
        f"config:\n{cfg_text}\ntoolbox: {toolbox}")

    # AC-F5.3 (systemd leg): re-install is a fixpoint — one unit, one set of lines.
    ins2 = _run_install(["--yes"], home=home, loomhome=loomhome, path=ipath)
    assert ins2.returncode == 0, ins2.stderr
    assert svc.read_text(encoding="utf-8") == svc_text, (
        "re-install must not duplicate or reorder unit lines")
    assert timer.read_text(encoding="utf-8") == timer_text


def test_f5_3_cron_reinstall_fixpoint_and_foreign_lines_survive(tmp_path):
    """AC-F5.3: two installs → one block, one set of assignments (fixpoint), and the
    wave-b regressions: seeded foreign entries — including a foreign `PATH=` line —
    survive byte-exact and in order."""
    home, loomhome = _fresh_home(tmp_path)
    kit.write_config(loomhome, server_url="http://127.0.0.1:1")
    shims = kit.shim_dir(tmp_path)
    cron_state = tmp_path / "cron_state.txt"
    _crontab_shim(shims, cron_state)
    cron_state.write_text("\n".join(FOREIGN_LINES) + "\n", encoding="utf-8")
    ipath = _interactive_path(tmp_path, shims, with_gitleaks=HAVE_GITLEAKS)
    _assert_host_safe(ipath, shims, systemd_shimmed=False)

    ins1 = _run_install(["--yes"], home=home, loomhome=loomhome, path=ipath)
    assert ins1.returncode == 0, ins1.stderr
    content1 = cron_state.read_text(encoding="utf-8")
    ins2 = _run_install(["--yes"], home=home, loomhome=loomhome, path=ipath)
    assert ins2.returncode == 0, ins2.stderr
    content2 = cron_state.read_text(encoding="utf-8")

    # AC-F5.3: the second install must be byte-identical — this is what catches a
    # duplicated PATH=/parity assignment, not just a duplicated tag.
    assert content2 == content1, (
        f"re-install is not a fixpoint:\n--- first ---\n{content1}\n"
        f"--- second ---\n{content2}")
    lines = content2.splitlines()
    assert sum(1 for l in lines if l.strip() == CRON_TAG) == 1
    assert len(_command_lines(lines)) == 1
    # Wave-b HIGH-1/MED-6: every foreign line byte-exact, original order.
    _assert_ordered_byte_exact(lines, FOREIGN_LINES,
                               "foreign crontab lines must survive install")


def test_f5_4_f6_2_uninstall_removes_every_block_line_keeps_foreign(tmp_path):
    """AC-F5.4: after --uninstall-trigger EVERY line the block wrote (parity lines
    included) is gone. AC-F6.2: foreign entries survive byte-exact, no crontab
    removal. AC-F6.3: a backup precedes the rewrite."""
    home, loomhome = _fresh_home(tmp_path)
    kit.write_config(loomhome, server_url="http://127.0.0.1:1")
    shims = kit.shim_dir(tmp_path)
    cron_state = tmp_path / "cron_state.txt"
    _crontab_shim(shims, cron_state)
    cron_state.write_text("\n".join(FOREIGN_LINES) + "\n", encoding="utf-8")
    ipath = _interactive_path(tmp_path, shims, with_gitleaks=HAVE_GITLEAKS)
    _assert_host_safe(ipath, shims, systemd_shimmed=False)

    ins = _run_install(["--yes"], home=home, loomhome=loomhome, path=ipath)
    assert ins.returncode == 0, ins.stderr
    pre_uninstall = cron_state.read_text(encoding="utf-8")
    block_lines = [l for l in pre_uninstall.splitlines()
                   if l.strip() and l not in FOREIGN_LINES]
    assert block_lines, "the install must have written a block"
    calls_file = Path(str(cron_state) + ".calls")
    calls_before = len(calls_file.read_text().splitlines())

    uni = _run_install(["--uninstall-trigger"], home=home, loomhome=loomhome,
                       path=ipath)
    assert uni.returncode == 0, uni.stderr
    assert cron_state.exists(), (
        "AC-F6.2: foreign entries remain → the crontab must be REWRITTEN, not removed")
    new_calls = calls_file.read_text().splitlines()[calls_before:]
    assert "-r" not in new_calls, f"no crontab removal with foreign entries: {new_calls}"
    final_lines = cron_state.read_text(encoding="utf-8").splitlines()
    # AC-F6.2: byte-exact, order preserved, nothing else left.
    assert final_lines == FOREIGN_LINES, (
        f"foreign crontab must be restored byte-exact:\nexpected {FOREIGN_LINES}\n"
        f"got      {final_lines}")
    # AC-F5.4: no block residue of any kind (belt over the equality above).
    for line in block_lines:
        assert line not in final_lines, f"loom-owned line left behind: {line!r}"
    assert "loom" not in "\n".join(final_lines), "loom-owned residue in the crontab"
    # AC-F6.3: backup of the pre-write content under $LOOM_HOME/backup/.
    assert _some_backup_equals(loomhome, pre_uninstall), (
        f"no backup matching the pre-uninstall crontab in {_backups(loomhome)}")


# =========================================================================== #
# P2.F6 — Clean trigger uninstall                                              #
# =========================================================================== #
def test_f6_1_uninstall_on_loom_only_crontab_removes_it_entirely(tmp_path):
    """AC-F6.1 (fails today, RUNBOOK §7 finding 2): when the filtered result is
    empty, --uninstall-trigger must REMOVE the crontab (`crontab -r` semantics),
    never install an empty one. AC-F6.3: backup taken before the removal."""
    home, loomhome = _fresh_home(tmp_path)
    kit.write_config(loomhome, server_url="http://127.0.0.1:1")
    shims = kit.shim_dir(tmp_path)
    cron_state = tmp_path / "cron_state.txt"
    _crontab_shim(shims, cron_state)
    ipath = _interactive_path(tmp_path, shims, with_gitleaks=HAVE_GITLEAKS)
    _assert_host_safe(ipath, shims, systemd_shimmed=False)

    ins = _run_install(["--yes"], home=home, loomhome=loomhome, path=ipath)
    assert ins.returncode == 0, ins.stderr
    assert cron_state.exists() and CRON_TAG in cron_state.read_text()
    pre_uninstall = cron_state.read_text(encoding="utf-8")

    uni = _run_install(["--uninstall-trigger"], home=home, loomhome=loomhome,
                       path=ipath)
    assert uni.returncode == 0, uni.stderr
    # The shim keeps "no crontab" as state-file ABSENCE: an empty-but-installed
    # crontab (today's residue) leaves the file existing with empty content.
    assert not cron_state.exists(), (
        "AC-F6.1: a loom-only crontab must be REMOVED entirely; an installed empty "
        f"crontab is the exact residue this AC forbids (content now: "
        f"{cron_state.read_text()!r})")
    calls = Path(str(cron_state) + ".calls").read_text().splitlines()
    assert "-r" in calls, f"removal must go through `crontab -r`: {calls}"
    # AC-F6.3: the removal was preceded by a backup of what was removed.
    assert _some_backup_equals(loomhome, pre_uninstall), (
        f"no backup matching the removed crontab in {_backups(loomhome)}")


def test_f6_4_uninstall_with_no_crontab_is_a_clean_noop(tmp_path):
    """AC-F6.4: no crontab at all → exit 0, nothing installed, nothing removed."""
    home, loomhome = _fresh_home(tmp_path)
    shims = kit.shim_dir(tmp_path)
    cron_state = tmp_path / "cron_state.txt"      # never created: "no crontab"
    _crontab_shim(shims, cron_state)
    ipath = _interactive_path(tmp_path, shims, with_gitleaks=False)
    _assert_host_safe(ipath, shims, systemd_shimmed=False)

    uni = _run_install(["--uninstall-trigger"], home=home, loomhome=loomhome,
                       path=ipath)
    assert uni.returncode == 0, uni.stderr + uni.stdout
    assert not cron_state.exists(), "a no-op must not conjure a crontab into being"
    calls = Path(str(cron_state) + ".calls").read_text().splitlines()
    assert "-" not in calls and "-r" not in calls, (
        f"no write and no removal may happen on a host with no crontab: {calls}")
    assert not _backups(loomhome), "nothing was touched, so nothing to back up"


# =========================================================================== #
# P2.F5 — AC-F5.5 doctor must not paint a false-green scanner+trigger picture  #
# =========================================================================== #
@requires_gitleaks
def test_f5_5_doctor_no_false_green_on_divergence(tmp_path):
    """AC-F5.5 (fails today, RUNBOOK §7 finding 1): gitleaks resolvable
    interactively, absent from the trigger baseline, mechanism sabotaged (seeded
    OLD-format block with no parity lines, no config pin): doctor must make the
    divergence VISIBLE instead of '✓ gitleaks' + '✓ trigger' from the interactive
    PATH."""
    home, loomhome = _fresh_home(tmp_path)
    shims = kit.shim_dir(tmp_path)
    _claude_version_shim(shims)
    cron_state = tmp_path / "cron_state.txt"
    _crontab_shim(shims, cron_state)
    # OLD-format (pre-E7) block: tag + LOOM_HOME= + command, nothing else. This is
    # exactly what a pre-parity install left behind.
    cron_state.write_text(
        f"{CRON_TAG}\nLOOM_HOME={loomhome}\n"
        f"*/15 * * * * {loomhome}/venv/bin/loom process --quiet\n",
        encoding="utf-8",
    )
    toolbox = _toolbox(tmp_path)
    # Pin the LIVE version of the controlled binary so the scanner row can only be
    # green for the interactive-PATH reason this test exists to indict.
    ver = subprocess.run([str(toolbox / "gitleaks"), "version"],
                         capture_output=True, text=True, timeout=30)
    m = re.search(r"(\d+\.\d+\.\d+)", ver.stdout or "")
    if not m:
        pytest.skip("cannot determine the live gitleaks version for the pin")
    kit.write_config(loomhome, server_url="http://127.0.0.1:1",
                     scanner_engine="auto", gitleaks_version=m.group(1))
    _seed_fresh_sweep_record(loomhome)   # keep the sweep row out of the ⚠ pool
    ipath = _interactive_path(tmp_path, shims, with_gitleaks=True)
    _assert_host_safe(ipath, shims, systemd_shimmed=False)

    doc = _run_doctor(home=home, loomhome=loomhome, path=ipath)
    out = doc.stdout
    assert "Traceback" not in out + doc.stderr, out + doc.stderr
    lines = out.splitlines()

    gitleaks_ok = any("✓" in l and "gitleaks" in l.lower() for l in lines)
    # The trigger row(s), excluding any unit-integrity row naming a unit file.
    trigger_rows = [l for l in lines if "loom-process" in l
                    and ".service" not in l and ".timer" not in l]
    trigger_all_ok = bool(trigger_rows) and all("✓" in l for l in trigger_rows)
    # A visible divergence: a ⚠/✗ row about the scanner/trigger surface (unit-diff
    # informational rows do not count — they are about files, not parity).
    keywords = ("gitleaks", "parity", "regex", "scan", "trigger", "cron")
    divergence_rows = [
        l for l in lines
        if ("⚠" in l or "✗" in l)
        and any(k in l.lower() for k in keywords)
        and "unit" not in l.lower() and "template" not in l.lower()
        and ".service" not in l and ".timer" not in l
    ]
    assert divergence_rows or not (gitleaks_ok and trigger_all_ok), (
        "AC-F5.5: doctor paints an all-ok scanner+trigger picture while the trigger "
        "context cannot resolve gitleaks (no parity line in the block, no config "
        f"pin): that is the false green of RUNBOOK §7 finding 1.\n{out}")


# =========================================================================== #
# P2.F7 — Doctor unit-integrity check                                          #
# =========================================================================== #
def _unit_integrity_rows(out: str) -> list[str]:
    """Rows about unit integrity: matched on distinctive keywords, robust to the
    `_line_with` quirk (phase-2 rows print first; tmp paths can carry stray
    substrings — the test names in this section avoid every matched keyword)."""
    rows = []
    for line in out.splitlines():
        low = line.lower()
        if "unit" in low or "template" in low or "loom-process.service" in line:
            rows.append(line)
    return rows


def _green_systemd_doctor_env(tmp_path, httpserver):
    """A doctor-green temp environment on the systemd branch: REAL install.sh run
    (units written, hook registered), degraded-manager shim, live http server."""
    home, loomhome = _fresh_home(tmp_path)
    shims = kit.shim_dir(tmp_path)
    _claude_version_shim(shims)
    cron_state = tmp_path / "cron_state.txt"
    _crontab_shim(shims, cron_state)
    _degraded_systemctl_shim(shims, tmp_path / "systemctl.log")
    httpserver.expect_request("/api/health", method="GET").respond_with_json(
        {"status": "ok", "version": "0.1.0"})
    httpserver.expect_request("/api/projects", method="GET").respond_with_json(
        {"projects": []})
    kit.write_config(loomhome, server_url=httpserver.url_for(""),
                     scanner_engine="auto")
    (loomhome / "config.json").chmod(0o600)
    _seed_fresh_sweep_record(loomhome)
    # AMENDED by D16 (2026-10-15; was `with_gitleaks=False`): a "doctor-green" host has
    # gitleaks now — without one capture is paused and the scanner row is a ✗.
    ipath = _interactive_path(tmp_path, shims, with_gitleaks=True)
    _assert_host_safe(ipath, shims, systemd_shimmed=True)
    ins = _run_install(["--yes"], home=home, loomhome=loomhome, path=ipath)
    assert ins.returncode == 0, ins.stderr
    svc = home / ".config" / "systemd" / "user" / "loom-process.service"
    assert svc.is_file(), ins.stdout
    return home, loomhome, ipath, svc


def test_f7_12_doctor_flags_execstart_drift_as_warn_not_fail(tmp_path, httpserver):
    """AC-F7.2: freshly-installed units → a ✓ unit-integrity row. AC-F7.1: a mutated
    ExecStart → a ⚠ row naming loom-process.service (filename ONLY, never a path),
    with the exit code still governed exclusively by ✗ rows. AC-F7.4: the phase-2
    rows still lead the checklist."""
    home, loomhome, ipath, svc = _green_systemd_doctor_env(tmp_path, httpserver)

    doc_ok = _run_doctor(home=home, loomhome=loomhome, path=ipath)
    assert doc_ok.returncode == 0, (
        f"the pre-drift environment must be doctor-green:\n{doc_ok.stdout}\n"
        f"{doc_ok.stderr}")
    ok_rows = _unit_integrity_rows(doc_ok.stdout)
    # AC-F7.2: unmutated units → a ✓ row.
    assert any("✓" in r for r in ok_rows), (
        f"AC-F7.2: no ✓ unit-integrity row in doctor output:\n{doc_ok.stdout}")
    assert not any("⚠" in r or "✗" in r for r in ok_rows), ok_rows

    # AC-F7.4: phase-2 rows first (the `_line_with` frozen-oracle constraint).
    lines = doc_ok.stdout.splitlines()
    def first_idx(pred, what):
        for i, l in enumerate(lines):
            if pred(l):
                return i
        raise AssertionError(f"no doctor row for {what}:\n{doc_ok.stdout}")
    idx_scanner = first_idx(lambda l: "scanner engine" in l.lower(), "scanner")
    idx_trigger = first_idx(lambda l: "loom-process" in l, "trigger")
    idx_sweep = first_idx(lambda l: "sweep" in l.lower(), "sweep")
    idx_jq = first_idx(lambda l: "jq" in l.lower(), "jq")
    assert max(idx_scanner, idx_trigger, idx_sweep) < idx_jq, (
        f"phase-2 rows must lead the checklist:\n{doc_ok.stdout}")

    # AC-F7.1: hostile-upstream-style drift in the installed copy.
    original = svc.read_text(encoding="utf-8")
    mutated = original.replace("process --quiet", "process --quiet --exfiltrate")
    assert mutated != original
    svc.write_text(mutated, encoding="utf-8")

    doc_drift = _run_doctor(home=home, loomhome=loomhome, path=ipath)
    drift_rows = [l for l in doc_drift.stdout.splitlines()
                  if "loom-process.service" in l and ("⚠" in l or "✗" in l)]
    assert drift_rows, (
        f"AC-F7.1: doctor must flag the drifted unit, naming loom-process.service:\n"
        f"{doc_drift.stdout}")
    row = drift_rows[0]
    assert "⚠" in row and "✗" not in row, (
        f"drift is a ⚠ (doctor is a configuration check, not an attestation — "
        f"LOW-7), never a ✗: {row}")
    # Amended P2.F7: filename ONLY — a full path would collide with the frozen
    # wave-b `_line_with` oracle ("scan" appears in pytest tmp paths).
    assert str(home) not in row and str(tmp_path) not in row, (
        f"the drift row must name the unit filename only, never a path: {row}")
    # AC-F7.1/AC-F7.4: exit semantics unchanged — a ⚠ row cannot turn doctor red.
    assert doc_drift.returncode == doc_ok.returncode == 0, (
        f"exit must be governed by ✗ rows only:\n{doc_drift.stdout}")


def test_f7_3_cron_branch_host_row_is_informational(tmp_path):
    """AC-F7.3 (first half): on a host with no systemctl at all the unit-integrity
    row is present and informational — never a ✗, never a crash."""
    home, loomhome = _fresh_home(tmp_path)
    shims = kit.shim_dir(tmp_path)
    _claude_version_shim(shims)
    cron_state = tmp_path / "cron_state.txt"
    _crontab_shim(shims, cron_state)
    kit.write_config(loomhome, server_url="http://127.0.0.1:1")
    ipath = _interactive_path(tmp_path, shims, with_gitleaks=False)
    _assert_host_safe(ipath, shims, systemd_shimmed=False)
    ins = _run_install(["--yes"], home=home, loomhome=loomhome, path=ipath)
    assert ins.returncode == 0, ins.stderr
    assert "cron entry installed" in ins.stdout

    doc = _run_doctor(home=home, loomhome=loomhome, path=ipath)
    assert doc.returncode in (0, 1), doc.stderr
    assert "Traceback" not in doc.stdout + doc.stderr
    rows = _unit_integrity_rows(doc.stdout)
    assert rows, (
        f"AC-F7.3: the unit-integrity row must still appear (as informational) on a "
        f"cron-branch host:\n{doc.stdout}")
    assert not any("✗" in r for r in rows), (
        f"a cron-branch host is not an error state: {rows}")


def test_f7_3_probe_exception_warns_naming_type_only(tmp_path, httpserver):
    """AC-F7.3 (second half): an unreadable installed unit makes the probe raise —
    doctor must warn naming ONLY the exception type, never print a traceback."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("chmod 000 does not block reads for root")
    home, loomhome, ipath, svc = _green_systemd_doctor_env(tmp_path, httpserver)
    svc.chmod(0o000)
    try:
        doc = _run_doctor(home=home, loomhome=loomhome, path=ipath)
    finally:
        svc.chmod(0o644)
    assert doc.returncode in (0, 1), doc.stderr
    assert "Traceback" not in doc.stdout + doc.stderr, (
        f"the F6/LOW-8 lesson: a doctor probe must never become a traceback:\n"
        f"{doc.stdout}\n{doc.stderr}")
    warn_rows = [l for l in doc.stdout.splitlines()
                 if "⚠" in l and "PermissionError" in l]
    assert warn_rows, (
        f"AC-F7.3: an unreadable unit must yield a ⚠ row naming the exception type "
        f"(PermissionError):\n{doc.stdout}")


# =========================================================================== #
# P2.F8 — Register pytest.mark.e2e                                             #
# =========================================================================== #
def _nested_pytest(args: list[str]) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": _pythonpath()}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *args,
         "client/tests/e2e", "client/tests/unit/test_sanity.py"],
        env=env, cwd=str(kit.REPO_ROOT), capture_output=True, text=True,
        timeout=180,
    )


def test_f8_1_no_unknown_mark_warning_for_e2e():
    """AC-F8.1 (fails today): collecting the e2e-marked tests with
    PytestUnknownMarkWarning promoted to an error must stay clean — i.e. the marker
    is REGISTERED in [tool.pytest.ini_options], not merely tolerated."""
    proc = _nested_pytest(["-W", "error::pytest.PytestUnknownMarkWarning"])
    assert proc.returncode == 0, (
        f"AC-F8.1: PytestUnknownMarkWarning raised for the e2e marker:\n"
        f"{proc.stdout}\n{proc.stderr}")
    assert "PytestUnknownMarkWarning" not in proc.stdout + proc.stderr


def test_f8_2_e2e_marker_selects_and_deselects():
    """AC-F8.2: `-m e2e` selects exactly the e2e-marked tests; `-m "not e2e"`
    excludes them (and keeps the rest)."""
    sel = _nested_pytest(["-m", "e2e"])
    assert sel.returncode == 0, sel.stdout + sel.stderr
    ids = [l for l in sel.stdout.splitlines() if "::" in l]
    assert any(l.startswith("client/tests/e2e/") for l in ids), (
        f"-m e2e must select the e2e tests:\n{sel.stdout}")
    assert not any("test_sanity" in l for l in ids), (
        f"-m e2e must not select unmarked tests:\n{sel.stdout}")

    des = _nested_pytest(["-m", "not e2e"])
    assert des.returncode == 0, des.stdout + des.stderr
    ids = [l for l in des.stdout.splitlines() if "::" in l]
    assert not any(l.startswith("client/tests/e2e/") for l in ids), (
        f"-m 'not e2e' must deselect every e2e test:\n{des.stdout}")
    assert any("test_sanity" in l for l in ids), (
        f"-m 'not e2e' must keep non-e2e tests:\n{des.stdout}")


# =========================================================================== #
# Amended P2.F5 whitelist: %/CR never reach a scheduler write                  #
# =========================================================================== #
@requires_gitleaks
def test_f5_env_assignment_values_never_carry_percent_or_cr(tmp_path):
    """Amended P2.F5 interface: env-assignment values allow ':' but still reject
    newline/CR/'%' — '%' is cron's "rest is stdin" separator. A poisoned component
    in the install-time PATH must never surface in anything written to the
    scheduler. (Passes today: the pressure is on future PATH-carrying mechanisms.)"""
    home, loomhome = _fresh_home(tmp_path)
    kit.write_config(loomhome, server_url="http://127.0.0.1:1")
    shims = kit.shim_dir(tmp_path)
    cron_state = tmp_path / "cron_state.txt"
    _crontab_shim(shims, cron_state)
    poison = tmp_path / "pois%oned-bin"
    poison.mkdir()
    ipath = _interactive_path(tmp_path, shims, with_gitleaks=True) \
        + os.pathsep + str(poison)
    _assert_host_safe(ipath, shims, systemd_shimmed=False)

    ins = _run_install(["--yes"], home=home, loomhome=loomhome, path=ipath)
    assert ins.returncode == 0, ins.stderr
    if cron_state.exists():
        content = cron_state.read_text(encoding="utf-8")
        assert "%" not in content and "\r" not in content, (
            f"'%'/CR must be rejected before any scheduler write:\n{content}")
        for key, value in _assignments(content.splitlines()):
            assert ENV_VALUE_RX.fullmatch(value), (
                f"env-assignment value outside the amended whitelist: "
                f"{key}={value!r}")
        cmds = _command_lines(content.splitlines())
        assert cmds and CMD_VALUE_RX.fullmatch(cmds[0].split(None, 5)[5].split()[0]), (
            f"command-field value must keep the wave-b whitelist verbatim: {cmds}")
