"""Tests for `loom doctor` (PLAN.md §T6/§T5.4).

Runs against a fully fake environment: HOME + LOOM_HOME under tmp, ``claude``/``jq`` as
stubs on the PATH, mock server. The real ``~/.claude/settings.json`` is NEVER read or
written (HOME points at tmp). All green → exit 0; break the hook → exit 1.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from loom import cli

REPO_ROOT = Path(__file__).resolve().parents[3]
PINNED_VERSION = (REPO_ROOT / "contract" / "VERSION").read_text(encoding="utf-8").strip()


def _fakebin(tmp_path):
    """Stubs for every binary doctor shells out to: ``claude``, ``jq`` and the schedulers.

    Doctor's phase-2 trigger check (R2.4.2) probes the host scheduler through PATH, and
    ``HOME=<tmpdir>`` does not isolate ``crontab``/``systemctl``: without these stubs the
    test would read the developer's real crontab (or user timers) and its verdict would
    depend on the machine it runs on.
    """
    b = tmp_path / "fakebin"
    b.mkdir(parents=True, exist_ok=True)
    # Every stub below is a `#!/usr/bin/env bash` script, so `bash` must be resolvable
    # THROUGH THIS DIRECTORY: normally it is prepended to the real PATH and bash comes from
    # there, but a test that narrows PATH to this directory alone would otherwise find the
    # stubs unrunnable and read the resulting silence as a real verdict.
    real_bash = shutil.which("bash")
    if real_bash:
        (b / "bash").symlink_to(real_bash)
    claude = b / "claude"
    claude.write_text(f"#!/usr/bin/env bash\necho {PINNED_VERSION!r}\n", encoding="utf-8")
    claude.chmod(0o755)
    jq = b / "jq"
    jq.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    jq.chmod(0o755)
    # A live user manager with the timer enabled+active (the state install.sh leaves).
    systemctl = b / "systemctl"
    systemctl.write_text(
        '#!/usr/bin/env bash\ncase "$*" in\n'
        '  *is-system-running*) echo running ;;\n'
        '  *is-enabled*) echo enabled ;;\n'
        '  *is-active*) echo active ;;\nesac\nexit 0\n',
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    # Never reached while the systemctl stub answers, but it keeps the real crontab out
    # of reach whichever branch the check takes.
    crontab = b / "crontab"
    crontab.write_text(
        "#!/usr/bin/env bash\necho '# loom-process'\n"
        "echo '*/15 * * * * $HOME/.loom/venv/bin/loom process --quiet'\nexit 0\n",
        encoding="utf-8",
    )
    crontab.chmod(0o755)
    return b


def _write_settings(home_dir: Path, hook_dst: str, *, include_hook: bool) -> None:
    claude_dir = home_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    if include_hook:
        settings = {"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": hook_dst}]}]}}
    else:
        settings = {"model": "opus"}
    (claude_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")


def _prepare(env, monkeypatch, tmp_path, *, include_hook: bool) -> None:
    home_dir = env.home.parent  # HOME; ~/.claude hangs off here
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("LOOM_HOME", str(env.home))
    fakebin = _fakebin(tmp_path)
    monkeypatch.setenv("PATH", f"{fakebin}{os.pathsep}{os.environ['PATH']}")

    env.config("claude")  # server_url = mock; claude_bin = "claude" (resolved in fakebin)
    for sub in ("queue", "quarantine", "failed", "log"):
        (env.home / sub).mkdir(parents=True, exist_ok=True)

    env.httpserver.expect_request("/api/health", method="GET").respond_with_json(
        {"status": "ok", "version": "0.1.0"}
    )
    env.httpserver.expect_request("/api/projects", method="GET").respond_with_json({"projects": []})

    hook_dst = str(env.home / "hooks" / "sessionend.sh")
    _write_settings(home_dir, hook_dst, include_hook=include_hook)


def test_doctor_all_green_exits_zero(env, monkeypatch, tmp_path, capsys):
    _prepare(env, monkeypatch, tmp_path, include_hook=True)

    rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    assert rc == 0, out
    assert "✗" not in out
    assert "SessionEnd hook registered" in out
    assert f"claude {PINNED_VERSION}" in out


def test_doctor_names_the_kick_launcher(env, monkeypatch, tmp_path, capsys):
    """AC5a: doctor reports which session-detach launcher the SessionEnd kick can use.

    The darwin bug was invisible for a phase because nothing surfaced it: the failure went
    to processor.log (the launch line's redirect target), hook.log recorded a clean session
    end, and doctor had no row for it at all. This is that row.

    The fixture's PATH is the fakebin PREPENDED to the real one, so `setsid` is reachable
    here and the row is the tier-1 `ok`.
    """
    _prepare(env, monkeypatch, tmp_path, include_hook=True)

    rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    assert rc == 0, out
    row = next(r for r in out.splitlines() if "session-detach launcher" in r)
    assert row.startswith("\u2713"), row
    assert "setsid" in row


def test_doctor_names_python3_when_setsid_is_the_one_thing_missing(env, monkeypatch,
                                                                   tmp_path, capsys):
    """The darwin row: `setsid` gone, `python3` present — doctor must name the tier-2 one.

    Reporting the FIRST available launcher rather than any available one is what makes the
    row worth reading: it has to name the tier the hook would actually take, or an operator
    reading it learns nothing about their host.
    """
    _prepare(env, monkeypatch, tmp_path, include_hook=True)
    fakebin = _fakebin(tmp_path / "darwinish")
    (fakebin / "python3").symlink_to(shutil.which("python3"))
    monkeypatch.setenv("PATH", str(fakebin))

    rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    row = next(r for r in out.splitlines() if "session-detach launcher" in r)
    assert row.startswith("\u2713"), row
    assert "python3" in row.split(":", 1)[1].split("(")[0], (
        f"the row must name python3 as the tier in use, not merely as one it tried: {row}"
    )
    assert rc == 0, out


def test_doctor_kick_launcher_row_warns_without_ever_failing(env, monkeypatch, tmp_path,
                                                             capsys):
    """With no launcher on PATH the row must WARN, never fail.

    Doctor's exit code gates real things (install.sh's --update step runs it, and two e2e
    gates assert `"\u2717" not in out`). A missing launcher degrades the kick to the periodic
    pass; it does not break the install, so it must not be able to turn doctor red.
    """
    _prepare(env, monkeypatch, tmp_path, include_hook=True)
    # fakebin ALONE this time: it holds claude/jq/systemctl/crontab and nothing else, so
    # neither `setsid` nor `python3` is reachable.
    monkeypatch.setenv("PATH", str(_fakebin(tmp_path / "launcherless")))

    rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    row = next(r for r in out.splitlines() if "session-detach launcher" in r)
    assert row.startswith("\u26a0"), f"the launcher row must be a warning, never a failure: {row}"
    assert "setsid" in row and "python3" in row, (
        f"the row must name what it looked for, so the fix is obvious: {row}"
    )
    assert rc == 0, f"a missing launcher must not turn doctor red:\n{out}"


# --- The attribution row (issue #4) ----------------------------------------------
SHIPPED_HOOK = REPO_ROOT / "client" / "hooks" / "sessionend.sh"


def _install_hook(env, *, text: str | None = None) -> Path:
    """Puts a hook copy where install.sh would, i.e. where doctor reads it."""
    dst = env.home / "hooks" / "sessionend.sh"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text if text is not None
                   else SHIPPED_HOOK.read_text(encoding="utf-8"), encoding="utf-8")
    return dst


def _attribution_row(out: str) -> str:
    return next(r for r in out.splitlines() if "attribution" in r)


def test_doctor_confirms_the_installed_hook_captures_attribution(env, monkeypatch,
                                                                 tmp_path, capsys):
    """The current hook installed, git on PATH → the capability is really there."""
    _prepare(env, monkeypatch, tmp_path, include_hook=True)
    _install_hook(env)

    rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    row = _attribution_row(out)
    assert row.startswith("✓"), row
    assert "captures project attribution" in row
    assert rc == 0, out


def test_doctor_warns_when_the_installed_hook_predates_the_feature(env, monkeypatch,
                                                                   tmp_path, capsys):
    """THE row's reason for existing: install.sh COPIES the hook, so a machine that
    installed before issue #4 keeps running the old one.

    The old hook is not broken — it writes a perfectly valid marker, it just omits the
    remote — so every worktree session is dropped as `dropped_no_remote`, which `loom
    status` counts as a legitimate skip. No error, no counter, no log line reads as loss.
    Being told is the only way an operator finds out."""
    _prepare(env, monkeypatch, tmp_path, include_hook=True)
    pre_fix = SHIPPED_HOOK.read_text(encoding="utf-8").replace("cwd_remote_url", "cwd")
    _install_hook(env, text=pre_fix)

    rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    row = _attribution_row(out)
    assert row.startswith("⚠"), f"a degraded install is a warning, never a failure: {row}"
    assert "does NOT capture" in row
    assert "install.sh" in row, "the row must name the fix, not just the symptom"
    assert rc == 0, f"a stale hook must not turn doctor red:\n{out}"


def test_doctor_warns_when_no_hook_file_is_installed_at_all(env, monkeypatch,
                                                            tmp_path, capsys):
    """The third cause, and the one a fresh or half-broken install actually shows:
    `~/.loom/hooks/sessionend.sh` is not there to read. It must be a row, not a crash,
    and it must not be worded as the stale-hook case — the fix is the same installer run,
    but telling an operator their hook is "pre-issue-#4" when there is no hook at all
    sends them looking for the wrong thing."""
    _prepare(env, monkeypatch, tmp_path, include_hook=True)  # deliberately no _install_hook

    rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    row = _attribution_row(out)
    assert row.startswith("⚠"), row
    assert "not checked" in row
    assert "does NOT capture" not in row, (
        f"an absent hook is not the same diagnosis as a stale one: {row}")
    assert rc == 0, out


def test_doctor_distinguishes_a_missing_git_from_a_stale_hook(env, monkeypatch,
                                                              tmp_path, capsys):
    """Two different causes, two different fixes — so they must not share one message.
    Re-running install.sh does nothing for a host with no git."""
    _prepare(env, monkeypatch, tmp_path, include_hook=True)
    _install_hook(env)
    # fakebin ALONE: claude/jq/schedulers and nothing else, so `git` is unreachable.
    monkeypatch.setenv("PATH", str(_fakebin(tmp_path / "gitless")))

    rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    row = _attribution_row(out)
    assert row.startswith("⚠"), row
    assert "git is not" in row and "PATH" in row
    assert "install.sh" not in row, (
        f"re-running the installer does not put git on PATH; saying so would send the "
        f"operator to the wrong fix: {row}")
    assert rc == 0, out


def test_doctor_missing_hook_exits_one(env, monkeypatch, tmp_path, capsys):
    _prepare(env, monkeypatch, tmp_path, include_hook=False)

    rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    assert rc == 1
    assert "SessionEnd hook is NOT in" in out


# --- Credential row (issue #20) ---------------------------------------------------
def _with_lifetime(env, *, issued_ago_days: float, life_days: float = 90) -> None:
    from datetime import datetime, timedelta, timezone

    path = env.home / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    issued = now - timedelta(days=issued_ago_days)
    data["token_issued_at"] = issued.strftime("%Y-%m-%dT%H:%M:%SZ")
    data["token_expires_at"] = (issued + timedelta(days=life_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps(data), encoding="utf-8")


def _credential_row(out: str) -> str:
    return next(r for r in out.splitlines() if "token" in r and ("expires" in r or "permanent" in r or "expired" in r))


def test_doctor_warns_about_a_legacy_permanent_token(env, monkeypatch, tmp_path, capsys):
    _prepare(env, monkeypatch, tmp_path, include_hook=True)  # env.config: no lifetime keys

    rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    assert rc == 0, out
    row = _credential_row(out)
    assert row.startswith("⚠"), row
    assert "permanent" in row and "loom enroll" in row


def test_doctor_reports_the_days_a_device_token_has_left(env, monkeypatch, tmp_path, capsys):
    _prepare(env, monkeypatch, tmp_path, include_hook=True)
    _with_lifetime(env, issued_ago_days=10)  # 80 days left

    rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    assert rc == 0, out
    row = _credential_row(out)
    assert row.startswith("✓"), row
    assert "80 days" in row or "79 days" in row
    assert "renews" in row


def test_doctor_fails_on_an_expired_device_token(env, monkeypatch, tmp_path, capsys):
    _prepare(env, monkeypatch, tmp_path, include_hook=True)
    _with_lifetime(env, issued_ago_days=100)
    # The mock still answers 200 here; the row is about the recorded lifetime, so a
    # machine whose clock says the token is gone is told to enrol before it 401s.

    rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    assert rc == 1, out
    row = _credential_row(out)
    assert row.startswith("✗"), row
    assert "expired" in row and "loom enroll" in row


def test_doctor_fails_while_the_server_has_rejected_the_token(env, monkeypatch, tmp_path, capsys):
    _prepare(env, monkeypatch, tmp_path, include_hook=True)
    (env.home / "auth_rejected.json").write_text(json.dumps({
        "status": 401, "at": "2026-09-22T11:00:00Z", "detail": "POST /api/entries → 401 unauthorized",
    }), encoding="utf-8")

    rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    assert rc == 1, out
    row = next(r for r in out.splitlines() if "rejected" in r)
    assert row.startswith("✗"), row
    assert "2026-09-22T11:00:00Z" in row and "loom enroll" in row


# --- D9: doctor shows the capture mode of THIS machine ---------------------------------
def test_doctor_warns_on_a_legacy_all_registered_machine(env, monkeypatch, tmp_path, capsys):
    _prepare(env, monkeypatch, tmp_path, include_hook=True)   # config without capture_mode

    rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    assert rc == 0, out
    row = next(r for r in out.splitlines() if "capture mode" in r)
    assert row.startswith("⚠"), row
    assert "all_registered" in row and "loom enable" in row


def test_doctor_reports_opt_in_with_the_enabled_count(env, monkeypatch, tmp_path, capsys):
    _prepare(env, monkeypatch, tmp_path, include_hook=True)
    env.config("claude", capture_mode="opt_in")
    env.set_local_state("enabled")
    env.set_local_state("disabled", remote="github.com/org/c")

    rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    assert rc == 0, out
    row = next(r for r in out.splitlines() if "capture mode" in r)
    assert row.startswith("✓"), row
    assert "opt-in" in row and "1 project" in row


def test_doctor_warns_on_opt_in_with_nothing_enabled(env, monkeypatch, tmp_path, capsys):
    """The symmetric failure of opt-in: a machine that captures nothing and does not know."""
    _prepare(env, monkeypatch, tmp_path, include_hook=True)
    env.config("claude", capture_mode="opt_in")

    rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    assert rc == 0, out
    row = next(r for r in out.splitlines() if "capture mode" in r)
    assert row.startswith("⚠"), row
    assert "0 projects" in row and "loom enable" in row
