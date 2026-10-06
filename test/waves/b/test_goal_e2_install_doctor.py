"""FROZEN GOAL — E2 timer-packaging (F2.2 + F2.4 / spec R2.2 + R2.4). Read-only.

Oracle properties: the REAL ``client/install.sh`` runs as a subprocess against a temp
HOME (``LOOM_SKIP_VENV=1``, the phase-1 test seam); the host schedulers this wave does
not own (crontab, systemctl) are PATH-shimmed record-replay stubs, so the assertions
check the EXACT commands and crontab content the installer produces. ``--update``
runs against a real ``file://`` clone with a real upstream commit. Doctor assertions
pin behavior (symbol + keyword per line), not exact wording.

Run: .venv/bin/python -m pytest test/waves/b/test_goal_e2_install_doctor.py -q
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _goalkit as kit  # noqa: E402

INSTALL = kit.REPO_ROOT / "client" / "install.sh"
CRON_TAG = "# loom-process"


def _run_install(tmp_path, *args, with_systemctl: bool, extra_env=None,
                 script: Path = INSTALL):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    shims = kit.shim_dir(tmp_path)
    cron_state = tmp_path / "cron_state.txt"
    kit.crontab_shim(shims, cron_state)
    sysd_log = tmp_path / "systemctl.log"
    if with_systemctl:
        kit.systemctl_shim(shims, sysd_log)
    env = {
        **os.environ,
        "HOME": str(home),
        "LOOM_HOME": str(home / ".loom"),
        "LOOM_SKIP_VENV": "1",
        "PATH": kit.base_path_with(shims, include_gitleaks=False),
    }
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["bash", str(script), "--yes", *args],
        env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL,
        timeout=180,
    )
    return proc, home, cron_state, sysd_log


# --------------------------------------------------------------------------- #
# Trigger install — cron branch (the executable boundary on this machine)      #
# --------------------------------------------------------------------------- #
def test_cron_branch_installs_tagged_15min_line(tmp_path):
    proc, home, cron_state, _ = _run_install(tmp_path, with_systemctl=False)
    assert proc.returncode == 0, proc.stderr
    content = cron_state.read_text()
    assert CRON_TAG in content
    line = next(l for l in content.splitlines() if "loom process" in l)
    assert line.startswith("*/15 * * * *")
    assert str(home / ".loom") + "/venv/bin/loom process --quiet" in line


def test_cron_branch_is_idempotent(tmp_path):
    _run_install(tmp_path, with_systemctl=False)
    proc, _, cron_state, _ = _run_install(tmp_path, with_systemctl=False)
    assert proc.returncode == 0, proc.stderr
    content = cron_state.read_text()
    assert content.count(CRON_TAG) == 1, "re-install must not duplicate the cron entry"
    assert sum("loom process" in l for l in content.splitlines()) == 1


def test_cron_branch_preserves_existing_user_crontab(tmp_path):
    shims = kit.shim_dir(tmp_path)
    cron_state = tmp_path / "cron_state.txt"
    cron_state.write_text("# my-backup\n0 3 * * * /usr/local/bin/backup.sh\n")
    kit.crontab_shim(shims, cron_state)
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "LOOM_HOME": str(home / ".loom"),
           "LOOM_SKIP_VENV": "1",
           "PATH": kit.base_path_with(shims, include_gitleaks=False)}
    proc = subprocess.run(["bash", str(INSTALL), "--yes"], env=env,
                          capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=180)
    assert proc.returncode == 0, proc.stderr
    content = cron_state.read_text()
    assert "# my-backup" in content and "backup.sh" in content
    assert CRON_TAG in content


# --------------------------------------------------------------------------- #
# Trigger install — systemd branch (verified through the recorder)             #
# --------------------------------------------------------------------------- #
def test_systemd_branch_installs_units_and_enables_timer(tmp_path):
    proc, home, cron_state, sysd_log = _run_install(tmp_path, with_systemctl=True)
    assert proc.returncode == 0, proc.stderr
    unit_dir = home / ".config" / "systemd" / "user"
    assert (unit_dir / "loom-process.service").is_file()
    assert (unit_dir / "loom-process.timer").is_file()
    timer_text = (unit_dir / "loom-process.timer").read_text()
    assert "OnUnitActiveSec=15min" in timer_text
    calls = sysd_log.read_text()
    assert "daemon-reload" in calls
    assert re.search(r"enable --now .*loom-process\.timer", calls)
    assert not cron_state.exists() or CRON_TAG not in cron_state.read_text(), (
        "systemd hosts must not ALSO write a cron entry"
    )


def test_systemd_branch_idempotent(tmp_path):
    _run_install(tmp_path, with_systemctl=True)
    proc, _, _, _ = _run_install(tmp_path, with_systemctl=True)
    assert proc.returncode == 0, proc.stderr


# --------------------------------------------------------------------------- #
# install.sh --update (R2.4.1)                                                 #
# --------------------------------------------------------------------------- #
def test_update_pulls_recopies_and_runs_doctor(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(kit.REPO_ROOT), str(origin)],
                   check=True, timeout=300)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)],
                   check=True, timeout=300)

    proc, home, _, _ = _run_install(tmp_path, with_systemctl=False,
                                    script=work / "client" / "install.sh")
    assert proc.returncode == 0, proc.stderr

    # Upstream moves: the hook gains a marker line.
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True,
                   timeout=300)
    hook = seed / "client" / "hooks" / "sessionend.sh"
    hook.write_text(hook.read_text() + "\n# upstream-update-marker\n")
    git_env = {**os.environ, "GIT_AUTHOR_NAME": "goal", "GIT_AUTHOR_EMAIL": "g@x",
               "GIT_COMMITTER_NAME": "goal", "GIT_COMMITTER_EMAIL": "g@x"}
    subprocess.run(["git", "-C", str(seed), "commit", "-aqm", "update hook"],
                   check=True, env=git_env)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "HEAD"],
                   check=True, env=git_env)

    # Fake installed-CLI so --update's doctor step has something to run
    # (LOOM_SKIP_VENV=1 means there is no real venv in the temp home).
    doctor_log = tmp_path / "doctor_invocations.txt"
    loom_bin = home / ".loom" / "venv" / "bin" / "loom"
    loom_bin.parent.mkdir(parents=True, exist_ok=True)
    loom_bin.write_text(
        "#!/usr/bin/env python3\nimport pathlib, sys\n"
        f"pathlib.Path({str(doctor_log)!r}).write_text(' '.join(sys.argv[1:]))\n"
        "sys.exit(0)\n"
    )
    loom_bin.chmod(0o755)

    shims = kit.shim_dir(tmp_path)
    env = {**os.environ, "HOME": str(home), "LOOM_HOME": str(home / ".loom"),
           "LOOM_SKIP_VENV": "1",
           "PATH": kit.base_path_with(shims, include_gitleaks=False)}
    upd = subprocess.run(["bash", str(work / "client" / "install.sh"),
                          "--yes", "--update"],
                         env=env, capture_output=True, text=True,
                         stdin=subprocess.DEVNULL, timeout=300)
    assert upd.returncode == 0, upd.stderr

    assert "# upstream-update-marker" in (work / "client" / "hooks" /
                                          "sessionend.sh").read_text(), "git pull ran"
    assert "# upstream-update-marker" in (home / ".loom" / "hooks" /
                                          "sessionend.sh").read_text(), "hook re-copied"
    assert doctor_log.exists() and "doctor" in doctor_log.read_text(), "doctor ran"


# --------------------------------------------------------------------------- #
# RUNBOOK documentation half of AC-2.2c                                        #
# --------------------------------------------------------------------------- #
def test_runbook_documents_timer_and_wsl2_manual_step():
    text = (kit.REPO_ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    assert "loom-process" in text, "RUNBOOK must document the periodic trigger"
    assert "wsl2" in text.lower(), "RUNBOOK must keep the WSL2 caveat/manual step"


# --------------------------------------------------------------------------- #
# loom doctor — the three phase-2 checks (R2.4.2 / AC-2.4c)                    #
# --------------------------------------------------------------------------- #
def _doctor_env(tmp_path, httpserver, *, cron_line: bool, last_run_age_s: int | None,
                gitleaks: bool):
    """A doctor-green phase-1 environment, parameterized on the phase-2 surface."""
    import shutil as _shutil

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    loomhome = home / ".loom"
    for sub in ("queue", "quarantine", "failed", "log", "prompts", "hooks"):
        (loomhome / sub).mkdir(parents=True, exist_ok=True)
    _shutil.copyfile(kit.REAL_PROMPT, loomhome / "prompts" / "summarizer_v1.md")

    httpserver.expect_request("/api/health", method="GET").respond_with_json(
        {"status": "ok", "version": "0.1.0"})
    httpserver.expect_request("/api/projects", method="GET").respond_with_json(
        {"projects": []})

    stub = kit.make_claude_stub(tmp_path / "claude_stub.py", stdout="ok")
    kit.write_config(loomhome, server_url=httpserver.url_for(""), claude_bin=stub,
                     scanner_engine="auto")
    hook_dst = loomhome / "hooks" / "sessionend.sh"
    hook_dst.write_text("#!/bin/bash\nexit 0\n")
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(json.dumps(
        {"hooks": {"SessionEnd": [{"hooks": [
            {"type": "command", "command": str(hook_dst)}]}]}}))

    shims = kit.shim_dir(tmp_path)
    cron_state = tmp_path / "cron_state.txt"
    if cron_line:
        cron_state.write_text(
            f"{CRON_TAG}\n*/15 * * * * {loomhome}/venv/bin/loom process --quiet\n")
    kit.crontab_shim(shims, cron_state)

    if last_run_age_s is not None:
        import time as _t
        from datetime import datetime, timezone
        ts = datetime.fromtimestamp(_t.time() - last_run_age_s, tz=timezone.utc)
        (loomhome / "sweep_last_run.json").write_text(json.dumps({
            "finished_at": ts.isoformat().replace("+00:00", "Z"),
            "scanned": 3, "enqueued": 0, "skipped": 3}))

    env = {**os.environ, "HOME": str(home), "LOOM_HOME": str(loomhome),
           "PATH": kit.base_path_with(shims, include_gitleaks=gitleaks)}
    return env


def _doctor(env):
    return subprocess.run([sys.executable, "-m", "loom.cli", "doctor"], env=env,
                          capture_output=True, text=True, timeout=120)


def _line_with(out: str, *keywords: str) -> str:
    for line in out.splitlines():
        low = line.lower()
        if all(k.lower() in low for k in keywords):
            return line
    raise AssertionError(f"no doctor line with {keywords!r} in:\n{out}")


def test_doctor_green_with_full_phase2_surface(tmp_path, httpserver):
    env = _doctor_env(tmp_path, httpserver, cron_line=True, last_run_age_s=60,
                      gitleaks=True)
    proc = _doctor(env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "✓" in _line_with(proc.stdout, "loom-process")          # trigger present
    assert "✓" in _line_with(proc.stdout, "sweep")                 # fresh last-run
    scanner_line = _line_with(proc.stdout, "gitleaks")             # engine + version
    assert re.search(r"\d+\.\d+\.\d+", scanner_line)


def test_doctor_fails_when_trigger_missing(tmp_path, httpserver):
    env = _doctor_env(tmp_path, httpserver, cron_line=False, last_run_age_s=60,
                      gitleaks=True)
    proc = _doctor(env)
    assert proc.returncode == 1
    trig = _line_with(proc.stdout, "loom-process")
    assert "✗" in trig


def test_doctor_warns_on_stale_or_absent_sweep(tmp_path, httpserver):
    env = _doctor_env(tmp_path, httpserver, cron_line=True, last_run_age_s=None,
                      gitleaks=True)
    proc = _doctor(env)
    assert proc.returncode == 0, "absent last-run is a WARN, never a FAIL"
    assert "⚠" in _line_with(proc.stdout, "sweep")


def test_doctor_scanner_fallback_is_a_warn(tmp_path, httpserver):
    env = _doctor_env(tmp_path, httpserver, cron_line=True, last_run_age_s=60,
                      gitleaks=False)
    proc = _doctor(env)
    assert proc.returncode == 0, "auto-fallback to regex is a WARN, never a FAIL"
    scanner = _line_with(proc.stdout, "scan")
    assert "⚠" in scanner or "regex" in scanner.lower()
