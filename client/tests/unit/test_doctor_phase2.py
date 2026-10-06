"""Unit tests for the phase-2 `loom doctor` rows and the fail-closed push-quarantine (E2).

Scope (R2.4.2 / AC-2.4c, plus wave-a findings F4/F6):

- ``_trigger_check`` — the 15-min periodic trigger, probed through PATH exactly like
  install.sh installs it. The host schedulers are stubbed IN PATH (never the real
  ``crontab``/``systemctl``: ``HOME=<tmpdir>`` does not isolate them).
- ``_sweep_check`` — age of E1's ``sweep_last_run.json``; warn or ok, never fail.
- ``_scanner_check`` — E3's ``active_engine``, including the two states where it RAISES
  and doctor must print a row instead of dying (F6).
- ``cmd_push_quarantine`` — a scanner that cannot produce a verdict is a hit, and never
  an upload, with or without ``--force`` (F4, I4).
- ``_trigger_parity_check`` — P2.F5: does the UNATTENDED pass resolve the same scanner
  engine this shell does? (RUNBOOK §7 finding 1: it did not, and doctor said it did.)
- ``_unit_integrity_check`` — P2.F7: installed user units vs. the shipped templates,
  after resolving the install-time substitutions.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from loom import cli, scan_engine
from loom.config import Config

SECRET_MD = "## Decisiones\n- token: sk-live-DO-NOT-PRINT-1234567890\n"


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _stub(bindir, name: str, body: str) -> None:
    """Executable shell stub called ``name`` inside ``bindir``.

    ``#!/bin/sh`` (absolute) and shell builtins only: the tests below run with a PATH that
    contains nothing but ``bindir``, so a ``/usr/bin/env`` shebang could not resolve its
    interpreter and the stub would never run.
    """
    bindir.mkdir(parents=True, exist_ok=True)
    path = bindir / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def bindir(tmp_path, monkeypatch):
    """An EMPTY PATH under tmp: no host scheduler is reachable unless a test stubs it."""
    d = tmp_path / "bin"
    d.mkdir()
    monkeypatch.setenv("PATH", str(d))
    return d


def _systemctl(bindir, *, manager="running", enabled="enabled", active="active") -> None:
    _stub(bindir, "systemctl", (
        'case "$*" in\n'
        f'  *is-system-running*) echo {manager} ;;\n'
        f'  *is-enabled*) echo {enabled} ;;\n'
        f'  *is-active*) echo {active} ;;\n'
        "esac\nexit 0\n"
    ))


def _crontab(bindir, content: str | None) -> None:
    """``crontab -l`` printing ``content``; ``None`` = the user has no crontab (exit 1)."""
    if content is None:
        _stub(bindir, "crontab", 'echo "no crontab for test" >&2\nexit 1\n')
        return
    # The content lives in a file and is echoed line by line with builtins, so arbitrary
    # cron lines (quotes included) need no escaping and no external `cat`.
    state = bindir.parent / "crontab_state.txt"
    state.write_text(content + "\n", encoding="utf-8")
    _stub(bindir, "crontab",
          f"while IFS= read -r l; do printf '%s\\n' \"$l\"; done < '{state}'\nexit 0\n")


def _config(tmp_path, **overrides) -> Config:
    return Config(loom_home=tmp_path / "loom_home", server_url="http://127.0.0.1:9",
                  token="t", **overrides)


def _write_last_run(cfg: Config, *, age_s: float, **fields) -> None:
    cfg.loom_home.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    cfg.sweep_last_run_path.write_text(json.dumps({
        "finished_at": ts.isoformat().replace("+00:00", "Z"),
        "scanned": 3, "enqueued": 1, "skipped": 2, **fields,
    }), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Check 1 — periodic trigger                                                   #
# --------------------------------------------------------------------------- #
def test_trigger_systemd_enabled_and_active_is_ok(bindir):
    _systemctl(bindir)
    level, msg = cli._trigger_check()
    assert level == "ok"
    assert cli.TRIGGER_TIMER_UNIT in msg


def test_trigger_systemd_degraded_manager_still_uses_the_systemd_branch(bindir):
    """Parity with install.sh: a degraded manager is usable (the pilot host runs degraded)."""
    _systemctl(bindir, manager="degraded")
    level, msg = cli._trigger_check()
    assert level == "ok"
    assert cli.TRIGGER_TIMER_UNIT in msg


def test_trigger_systemd_disabled_timer_is_a_fail(bindir):
    _systemctl(bindir, enabled="disabled", active="inactive")
    level, msg = cli._trigger_check()
    assert level == "fail"
    assert cli.TRIGGER_TIMER_UNIT in msg


def test_trigger_systemd_enabled_but_inactive_is_a_fail(bindir):
    _systemctl(bindir, active="inactive")
    assert cli._trigger_check()[0] == "fail"


def test_trigger_offline_manager_falls_back_to_the_cron_branch(bindir):
    """`systemctl --user` answering 'offline' is not a usable manager → read the crontab."""
    _systemctl(bindir, manager="offline")
    _crontab(bindir, f"{cli.TRIGGER_CRON_TAG}\n"
                     "*/15 * * * * /home/dev/.loom/venv/bin/loom process --quiet")
    level, msg = cli._trigger_check()
    assert level == "ok"
    assert cli.TRIGGER_CRON_TAG in msg


def test_trigger_cron_block_installed_by_install_sh_is_ok(bindir):
    """The real block install.sh writes: tag, LOOM_HOME= assignment, then the command."""
    _crontab(bindir, f"# my-backup\n0 3 * * * backup.sh\n{cli.TRIGGER_CRON_TAG}\n"
                     "LOOM_HOME=/home/dev/.loom\n"
                     "*/15 * * * * /home/dev/.loom/venv/bin/loom process --quiet")
    assert cli._trigger_check()[0] == "ok"


def test_trigger_cron_hand_edited_cadence_is_still_ok(bindir):
    """Keyed off the command marker, not the schedule: */5 is unusual, not broken."""
    _crontab(bindir, f"{cli.TRIGGER_CRON_TAG}\n"
                     "*/5 * * * * /home/dev/.loom/venv/bin/loom process --quiet")
    assert cli._trigger_check()[0] == "ok"


def test_trigger_cron_tag_without_the_command_is_a_fail(bindir):
    """LOW-9: a tag alone fires nothing — reporting it as ✓ would be a false green."""
    _crontab(bindir, f"{cli.TRIGGER_CRON_TAG}\nLOOM_HOME=/home/dev/.loom")
    level, msg = cli._trigger_check()
    assert level == "fail"
    assert "install.sh" in msg


def test_trigger_cron_tag_with_someone_elses_command_is_a_fail(bindir):
    """The block was mangled: the tag survived but the command it labels is not ours."""
    _crontab(bindir, f"{cli.TRIGGER_CRON_TAG}\n*/15 * * * * /usr/local/bin/other-tool run")
    assert cli._trigger_check()[0] == "fail"


def test_trigger_cron_commented_out_command_does_not_count(bindir):
    """A disabled entry runs nothing, whatever it says."""
    _crontab(bindir, f"{cli.TRIGGER_CRON_TAG}\n"
                     "#*/15 * * * * /home/dev/.loom/venv/bin/loom process --quiet")
    assert cli._trigger_check()[0] == "fail"


def test_trigger_cron_command_without_the_tag_is_a_warn(bindir):
    """It does fire, but it is not the block install.sh manages: a nudge, not a ✗."""
    _crontab(bindir, "*/15 * * * * /home/dev/.loom/venv/bin/loom process --quiet")
    level, msg = cli._trigger_check()
    assert level == "warn"
    assert cli.TRIGGER_CRON_TAG in msg


def test_trigger_cron_tag_absent_is_a_fail(bindir):
    _crontab(bindir, "# my-backup\n0 3 * * * backup.sh")
    level, msg = cli._trigger_check()
    assert level == "fail"
    assert "install.sh" in msg


def test_trigger_empty_crontab_is_a_fail_not_a_probe_error(bindir):
    """`crontab -l` exits nonzero when the user has none: that is 'no trigger'."""
    _crontab(bindir, None)
    assert cli._trigger_check()[0] == "fail"


def test_trigger_no_scheduler_at_all_is_a_warn(bindir):
    """Nothing was proven: an undetectable scheduler must not fail the environment."""
    level, msg = cli._trigger_check()
    assert level == "warn"
    assert cli.TRIGGER_NAME in msg


def test_trigger_never_echoes_the_users_crontab(bindir):
    """Other people's cron lines (and any credential in them) stay off the terminal."""
    _crontab(bindir, f"0 2 * * * curl -H 'Authorization: Bearer hunter2-not-ours' x\n"
                     f"{cli.TRIGGER_CRON_TAG}\nLOOM_HOME=/home/dev/.loom\n"
                     "*/15 * * * * /home/dev/.loom/venv/bin/loom process --quiet")
    level, msg = cli._trigger_check()
    assert level == "ok"
    assert "hunter2" not in msg and "Authorization" not in msg
    # Not even OUR own matched line is echoed back: the row states what matched, not what
    # the crontab holds.
    assert "/home/dev/.loom" not in msg


def test_trigger_unexpected_scheduler_output_is_not_quoted_back(bindir):
    """A chatty/hostile `systemctl` cannot inject arbitrary text into doctor's output."""
    _stub(bindir, "systemctl", "echo 'running; rm -rf ~ <script>'\nexit 0\n")
    _crontab(bindir, None)
    level, msg = cli._trigger_check()
    assert level == "fail"  # unparseable state word → not a live manager → cron branch
    assert "script" not in msg and "rm -rf" not in msg


# --------------------------------------------------------------------------- #
# Check 2 — sweep last-run age                                                 #
# --------------------------------------------------------------------------- #
def test_sweep_fresh_record_is_ok_and_reports_the_age(tmp_path):
    cfg = _config(tmp_path)
    _write_last_run(cfg, age_s=120)
    level, msg = cli._sweep_check(cfg)
    assert level == "ok"
    assert "sweep" in msg.lower()
    assert "2m" in msg


def test_sweep_just_under_two_cadences_is_still_fresh(tmp_path):
    cfg = _config(tmp_path)
    _write_last_run(cfg, age_s=cli.SWEEP_STALE_AFTER_S - 60)
    assert cli._sweep_check(cfg)[0] == "ok"


def test_sweep_older_than_two_cadences_is_a_warn(tmp_path):
    cfg = _config(tmp_path)
    _write_last_run(cfg, age_s=cli.SWEEP_STALE_AFTER_S + 60)
    level, msg = cli._sweep_check(cfg)
    assert level == "warn"
    assert "sweep" in msg.lower()


def test_sweep_absent_record_is_a_warn_never_a_fail(tmp_path):
    """The timer may simply not have fired yet on a fresh install."""
    cfg = _config(tmp_path)
    level, msg = cli._sweep_check(cfg)
    assert level == "warn"
    assert "sweep" in msg.lower()


def test_sweep_unreadable_or_incomplete_record_is_a_warn(tmp_path):
    cfg = _config(tmp_path)
    cfg.loom_home.mkdir(parents=True, exist_ok=True)
    cfg.sweep_last_run_path.write_text("{not json", encoding="utf-8")
    assert cli._sweep_check(cfg)[0] == "warn"
    cfg.sweep_last_run_path.write_text(json.dumps({"scanned": 1}), encoding="utf-8")
    assert cli._sweep_check(cfg)[0] == "warn"
    cfg.sweep_last_run_path.write_text(json.dumps({"finished_at": "not-a-date"}),
                                       encoding="utf-8")
    assert cli._sweep_check(cfg)[0] == "warn"


def test_sweep_without_config_is_a_warn():
    assert cli._sweep_check(None)[0] == "warn"


# --------------------------------------------------------------------------- #
# Check 3 — scanner engine (wave-a F5/F6)                                      #
# --------------------------------------------------------------------------- #
def _engine(monkeypatch, info=None, exc=None):
    def fake(cfg=None):
        if exc is not None:
            raise exc
        return info
    monkeypatch.setattr(scan_engine, "active_engine", fake)


def test_scanner_pin_matched_is_ok_with_the_version(tmp_path, monkeypatch):
    _engine(monkeypatch, scan_engine.EngineInfo("gitleaks", "8.30.1", "8.30.1"))
    level, msg = cli._scanner_check(_config(tmp_path, scanner_engine="auto"))
    assert level == "ok"
    assert "gitleaks 8.30.1" in msg


def test_scanner_unpinned_is_a_warn(tmp_path, monkeypatch):
    _engine(monkeypatch, scan_engine.EngineInfo("gitleaks", "8.30.1", None))
    level, msg = cli._scanner_check(_config(tmp_path))
    assert level == "warn"
    assert "gitleaks_version" in msg


def test_scanner_pin_mismatch_is_a_warn(tmp_path, monkeypatch):
    _engine(monkeypatch, scan_engine.EngineInfo("gitleaks", "8.29.0", "8.30.1"))
    level, msg = cli._scanner_check(_config(tmp_path))
    assert level == "warn"
    assert "8.29.0" in msg and "8.30.1" in msg


def test_scanner_unqueryable_version_is_a_warn(tmp_path, monkeypatch):
    _engine(monkeypatch, scan_engine.EngineInfo("gitleaks", None, "8.30.1"))
    assert cli._scanner_check(_config(tmp_path))[0] == "warn"


def test_scanner_regex_fallback_under_auto_is_a_warn(tmp_path, monkeypatch):
    _engine(monkeypatch, scan_engine.EngineInfo("regex", None, None))
    level, msg = cli._scanner_check(_config(tmp_path, scanner_engine="auto"))
    assert level == "warn"
    assert "regex" in msg.lower()


def test_scanner_explicit_regex_is_ok(tmp_path, monkeypatch):
    """A deliberate choice is not a defect; only the silent 'auto' fallback warns."""
    _engine(monkeypatch, scan_engine.EngineInfo("regex", None, None))
    level, msg = cli._scanner_check(_config(tmp_path, scanner_engine="regex"))
    assert level == "ok"
    assert "regex" in msg.lower()


def test_scanner_explicit_gitleaks_without_binary_is_a_fail_not_a_crash(tmp_path, monkeypatch):
    """F6: active_engine RAISES here — doctor owes the user a ✗ row, not a traceback."""
    _engine(monkeypatch, exc=FileNotFoundError("no gitleaks"))
    level, msg = cli._scanner_check(_config(tmp_path, scanner_engine="gitleaks"))
    assert level == "fail"
    assert "gitleaks" in msg


def test_scanner_unknown_engine_value_is_a_fail_not_a_crash(tmp_path, monkeypatch):
    _engine(monkeypatch, exc=ValueError("unknown engine"))
    level, msg = cli._scanner_check(_config(tmp_path, scanner_engine="gitleeks"))
    assert level == "fail"
    assert "gitleeks" in msg


def test_scanner_without_config_is_a_warn():
    assert cli._scanner_check(None)[0] == "warn"


@pytest.mark.parametrize("exc", [
    # `active_engine` also probes the binary's version, which makes a temp dir and spawns a
    # process: a full or read-only /tmp raises a bare OSError past the specific handlers.
    OSError("[Errno 30] Read-only file system: '/tmp/loom-gitleaks-x'"),
    MemoryError(),
    RuntimeError("boom"),
])
def test_scanner_any_other_failure_is_a_warn_not_a_traceback(tmp_path, monkeypatch, exc):
    """LOW-8: the row is TOTAL — its siblings cannot raise, and neither may this one."""
    _engine(monkeypatch, exc=exc)
    level, msg = cli._scanner_check(_config(tmp_path))
    assert level == "warn"
    assert type(exc).__name__ in msg


def test_scanner_failure_message_discloses_the_type_only(tmp_path, monkeypatch):
    """Same disclosure policy as push-quarantine: never the engine's error text."""
    _engine(monkeypatch, exc=OSError("choked on sk-live-DO-NOT-PRINT-1234567890"))
    _, msg = cli._scanner_check(_config(tmp_path))
    assert "sk-live" not in msg


# --------------------------------------------------------------------------- #
# doctor as a whole: the raising engine reaches the report, not the traceback   #
# --------------------------------------------------------------------------- #
def test_doctor_survives_a_raising_active_engine_and_exits_one(tmp_path, monkeypatch, capsys):
    """End-to-end guard for F6 through the real cmd_doctor (server unreachable is fine:
    what is asserted is that doctor RUNS and prints the scanner row)."""
    home = tmp_path / "home"
    loom_home = home / ".loom"
    loom_home.mkdir(parents=True)
    (loom_home / "config.json").write_text(json.dumps({
        "server_url": "http://127.0.0.1:9", "token": "t",
        "scanner": {"engine": "gitleaks"},
    }), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LOOM_HOME", str(loom_home))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

    rc = cli.cmd_doctor(argparse.Namespace())

    out = capsys.readouterr().out
    assert rc == 1
    scanner_row = next(line for line in out.splitlines() if "scanner.engine" in line)
    assert scanner_row.startswith("✗")
    # The phase-2 rows lead the checklist (see cmd_doctor) and the scanner row is first,
    # so a keyword search for it cannot be shadowed by an absolute path in another row.
    assert out.splitlines()[0] == scanner_row


# --------------------------------------------------------------------------- #
# Check 4 — trigger scanner parity (P2.F5 / AC-F5.5)                           #
# --------------------------------------------------------------------------- #
def _gitleaks_dir(tmp_path, name="toolbox"):
    """A directory holding an executable ``gitleaks``; parity is about RESOLUTION only."""
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    stub = d / "gitleaks"
    stub.write_text("#!/bin/sh\necho 8.30.1\n", encoding="utf-8")
    stub.chmod(0o755)
    return d


def _gitleaks_active(monkeypatch):
    """Pretend THIS shell resolves gitleaks (the interactive half of the divergence)."""
    _engine(monkeypatch, scan_engine.EngineInfo("gitleaks", "8.30.1", "8.30.1"))


def test_parity_ok_when_the_cron_block_path_resolves_gitleaks(tmp_path, bindir, monkeypatch):
    cfg = _config(tmp_path, scanner_engine="auto")
    _gitleaks_active(monkeypatch)
    toolbox = _gitleaks_dir(tmp_path)
    _crontab(bindir, f"{cli.TRIGGER_CRON_TAG}\nLOOM_HOME={cfg.loom_home}\n"
                     f"PATH={cfg.loom_home}/venv/bin:{toolbox}\n"
                     f"*/15 * * * * {cfg.loom_home}/venv/bin/loom process --quiet")
    level, msg = cli._trigger_parity_check(cfg)
    assert level == "ok"
    assert "parity" in msg.lower()


def test_parity_warns_when_the_block_carries_no_path(tmp_path, bindir, monkeypatch):
    """RUNBOOK §7 finding 1 exactly: the pre-parity block (tag + LOOM_HOME + command) on a
    host whose gitleaks is not in cron's minimal PATH. Both the scanner row and the trigger
    row are individually honest ✓; together they paint a false green."""
    cfg = _config(tmp_path, scanner_engine="auto")
    _gitleaks_active(monkeypatch)
    _crontab(bindir, f"{cli.TRIGGER_CRON_TAG}\nLOOM_HOME={cfg.loom_home}\n"
                     f"*/15 * * * * {cfg.loom_home}/venv/bin/loom process --quiet")
    level, msg = cli._trigger_parity_check(cfg)
    assert level == "warn"
    assert "gitleaks" in msg and "regex" in msg


def test_parity_warns_when_the_block_path_cannot_resolve_gitleaks(tmp_path, bindir,
                                                                  monkeypatch):
    cfg = _config(tmp_path, scanner_engine="auto")
    _gitleaks_active(monkeypatch)
    empty = tmp_path / "no_tools"
    empty.mkdir()
    _crontab(bindir, f"{cli.TRIGGER_CRON_TAG}\nLOOM_HOME={cfg.loom_home}\n"
                     f"PATH={cfg.loom_home}/venv/bin:{empty}\n"
                     f"*/15 * * * * {cfg.loom_home}/venv/bin/loom process --quiet")
    assert cli._trigger_parity_check(cfg)[0] == "warn"


def test_parity_ignores_a_foreign_path_line(tmp_path, bindir, monkeypatch):
    """`PATH=` is a shared namespace: someone else's assignment is not evidence about OUR
    trigger, so it must not be read as parity (nor, elsewhere, deleted as ours)."""
    cfg = _config(tmp_path, scanner_engine="auto")
    _gitleaks_active(monkeypatch)
    toolbox = _gitleaks_dir(tmp_path)
    _crontab(bindir, f"PATH={toolbox}\n@daily somebody-else.sh\n"
                     f"{cli.TRIGGER_CRON_TAG}\nLOOM_HOME={cfg.loom_home}\n"
                     f"*/15 * * * * {cfg.loom_home}/venv/bin/loom process --quiet")
    assert cli._trigger_parity_check(cfg)[0] == "warn"


def test_parity_ignores_a_foreign_path_line_that_merely_mentions_our_bin_dir(
        tmp_path, bindir, monkeypatch):
    """An operator who puts our venv on their OWN cron PATH does not make it ours.

    `PATH=/usr/local/bin:<loom_home>/venv/bin` is a perfectly ordinary line for someone
    who wants `loom` on the path of their other entries. Recognising OUR assignment by
    "the marker appears somewhere in the value" claims that line — so doctor reports
    parity from a PATH the trigger never carries. Our line is the one `cron_block`
    writes, whose FIRST component is the marker.
    """
    cfg = _config(tmp_path, scanner_engine="auto")
    _gitleaks_active(monkeypatch)
    toolbox = _gitleaks_dir(tmp_path)
    _crontab(bindir, f"PATH=/usr/local/bin:{cfg.loom_home}/venv/bin:{toolbox}\n"
                     "@daily somebody-else.sh\n"
                     f"{cli.TRIGGER_CRON_TAG}\nLOOM_HOME={cfg.loom_home}\n"
                     f"*/15 * * * * {cfg.loom_home}/venv/bin/loom process --quiet")

    assert cli._trigger_env_path(cfg.loom_home) is None
    # …and the row that follows says the trigger carries no PATH, not that it is fine.
    level, msg = cli._trigger_parity_check(cfg)
    assert level == "warn"
    assert "no PATH of its own" in msg


def test_parity_reads_the_block_path_of_a_loom_home_that_moved(tmp_path, bindir,
                                                               monkeypatch):
    """The mirror of the filter's home-independent command marker.

    A block installed under an older LOOM_HOME is still OUR block — the command marker
    already recognises it that way — so the PATH line beside it must be read as ours
    too, or doctor reports "no PATH" about a trigger that plainly has one.
    """
    cfg = _config(tmp_path, scanner_engine="auto")
    _gitleaks_active(monkeypatch)
    toolbox = _gitleaks_dir(tmp_path)
    old_home = tmp_path / "previous_home"
    _crontab(bindir, f"{cli.TRIGGER_CRON_TAG}\nLOOM_HOME={old_home}\n"
                     f"PATH={old_home}/venv/bin:{toolbox}\n"
                     f"*/15 * * * * {old_home}/venv/bin/loom process --quiet")

    assert cli._trigger_env_path(cfg.loom_home) == f"{old_home}/venv/bin:{toolbox}"


def test_parity_reads_the_systemd_unit_on_a_systemd_host(tmp_path, bindir, monkeypatch):
    cfg = _config(tmp_path, scanner_engine="auto")
    _gitleaks_active(monkeypatch)
    _systemctl(bindir)
    toolbox = _gitleaks_dir(tmp_path)
    home = tmp_path / "home"
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "loom-process.service").write_text(
        "[Service]\n"
        f"Environment=LOOM_HOME={cfg.loom_home}\n"
        f"Environment=PATH={cfg.loom_home}/venv/bin:{toolbox}\n"
        f"ExecStart={cfg.loom_home}/venv/bin/loom process --quiet\n",
        encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    assert cli._trigger_parity_check(cfg)[0] == "ok"

    (unit_dir / "loom-process.service").write_text(
        f"[Service]\nEnvironment=LOOM_HOME={cfg.loom_home}\n"
        f"ExecStart={cfg.loom_home}/venv/bin/loom process --quiet\n", encoding="utf-8")
    assert cli._trigger_parity_check(cfg)[0] == "warn"


def test_parity_is_ok_when_this_shell_has_no_gitleaks_either(tmp_path, bindir, monkeypatch):
    """Nothing diverges: the interactive run is regex too, so the trigger is not weaker."""
    _engine(monkeypatch, scan_engine.EngineInfo("regex", None, None))
    _crontab(bindir, None)
    assert cli._trigger_parity_check(_config(tmp_path, scanner_engine="auto"))[0] == "ok"


def test_parity_without_config_is_a_warn():
    assert cli._trigger_parity_check(None)[0] == "warn"


@pytest.mark.parametrize("exc", [FileNotFoundError("no gitleaks"), ValueError("bad"),
                                 OSError("read-only /tmp"), MemoryError()])
def test_parity_any_engine_failure_is_a_warn_naming_the_type(tmp_path, monkeypatch, exc):
    """LOW-8 again: a new probe is as total as its siblings, and discloses the type only."""
    _engine(monkeypatch, exc=exc)
    level, msg = cli._trigger_parity_check(_config(tmp_path))
    assert level == "warn"
    assert type(exc).__name__ in msg


def test_parity_never_echoes_the_users_crontab(tmp_path, bindir, monkeypatch):
    cfg = _config(tmp_path, scanner_engine="auto")
    _gitleaks_active(monkeypatch)
    _crontab(bindir, "0 2 * * * curl -H 'Authorization: Bearer hunter2-not-ours' x\n"
                     f"{cli.TRIGGER_CRON_TAG}\nLOOM_HOME={cfg.loom_home}\n"
                     f"*/15 * * * * {cfg.loom_home}/venv/bin/loom process --quiet")
    _, msg = cli._trigger_parity_check(cfg)
    assert "hunter2" not in msg and "Authorization" not in msg


# --------------------------------------------------------------------------- #
# Check 5 — installed units vs. shipped templates (P2.F7)                      #
# --------------------------------------------------------------------------- #
SHIPPED_UNIT_DIR = cli._shipped_unit_dir()


def _install_units(home, loom_home, *, path_value: str | None = None):
    """Writes the units exactly as install.sh's awk does, from the REAL templates.

    Using the shipped files (not a hand-written copy) is the point: if install.sh and
    doctor ever disagree about the substitutions, the ✓ case below stops being green.
    """
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    service = "\n".join(cli._render_service_unit(
        (SHIPPED_UNIT_DIR / "loom-process.service").read_text(encoding="utf-8"),
        loom_home, path_value)) + "\n"
    (unit_dir / "loom-process.service").write_text(service, encoding="utf-8")
    (unit_dir / "loom-process.timer").write_text(
        (SHIPPED_UNIT_DIR / "loom-process.timer").read_text(encoding="utf-8"),
        encoding="utf-8")
    return unit_dir


@pytest.fixture
def unit_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def test_unit_integrity_ok_for_freshly_installed_units(tmp_path, unit_home):
    """AC-F7.2."""
    loom_home = unit_home / ".loom"
    _install_units(unit_home, loom_home, path_value=f"{loom_home}/venv/bin:/usr/bin")
    level, msg = cli._unit_integrity_check(loom_home)
    assert level == "ok", msg
    assert "loom-process.service" in msg


def test_unit_integrity_ok_without_a_path_environment_line(tmp_path, unit_home):
    """A host where no safe PATH could be built still installs a valid unit: the absent
    line is a legitimate install-time outcome, not drift."""
    loom_home = unit_home / ".loom"
    _install_units(unit_home, loom_home, path_value=None)
    assert cli._unit_integrity_check(loom_home)[0] == "ok"


def test_unit_integrity_flags_a_mutated_execstart(tmp_path, unit_home):
    """AC-F7.1. The wave-b trigger row asks only is-enabled/is-active, so a hostile
    `ExecStart` landed by `--update` reported ✓. This row is what catches it."""
    loom_home = unit_home / ".loom"
    unit_dir = _install_units(unit_home, loom_home, path_value=f"{loom_home}/venv/bin")
    svc = unit_dir / "loom-process.service"
    svc.write_text(svc.read_text().replace("process --quiet",
                                           "process --quiet --exfiltrate"))

    level, msg = cli._unit_integrity_check(loom_home)

    assert level == "warn", "drift is a ⚠ (a configuration check, not an attestation)"
    assert "loom-process.service" in msg


def test_unit_integrity_drift_row_names_the_filename_never_a_path(tmp_path, unit_home):
    """Amended P2.F7: an absolute path in this row would carry pytest tmp substrings (and
    the operator's home) into a keyword-matched doctor line."""
    loom_home = unit_home / ".loom"
    unit_dir = _install_units(unit_home, loom_home)
    (unit_dir / "loom-process.timer").write_text("[Timer]\nOnUnitActiveSec=1min\n")

    _, msg = cli._unit_integrity_check(loom_home)

    assert "loom-process.timer" in msg
    assert str(unit_home) not in msg and str(tmp_path) not in msg


def test_unit_integrity_flags_an_added_environment_line(tmp_path, unit_home):
    """An extra `Environment=` beyond the ones install.sh injects is drift: a unit is a
    privileged, unattended thing and nothing else in the pilot would notice."""
    loom_home = unit_home / ".loom"
    unit_dir = _install_units(unit_home, loom_home, path_value=f"{loom_home}/venv/bin")
    svc = unit_dir / "loom-process.service"
    svc.write_text(svc.read_text() + "Environment=LD_PRELOAD=/tmp/evil.so\n")
    assert cli._unit_integrity_check(loom_home)[0] == "warn"


def test_unit_integrity_flags_a_path_no_install_could_have_written(tmp_path, unit_home):
    """`Environment=PATH=` is the one field the comparison cannot re-derive — the shell that
    ran the installer is gone — so it is echoed back as given. Its SHAPE is not given:
    `build_trigger_path` always writes our venv FIRST. That is the field worth tampering
    with, since a directory ahead of the venv decides which `git`, `gitleaks` and `loom` the
    unattended pass actually runs."""
    loom_home = unit_home / ".loom"
    _install_units(unit_home, loom_home,
                   path_value=f"/tmp/evil:{loom_home}/venv/bin:/usr/bin")

    level, msg = cli._unit_integrity_check(loom_home)

    assert level == "warn"
    assert "loom-process.service" in msg
    assert str(unit_home) not in msg and "/tmp/evil" not in msg


def test_unit_integrity_flags_a_relative_path_component(tmp_path, unit_home):
    """install.sh carries absolute directories only: '.' in a job that runs from $HOME every
    15 minutes turns a dropped file into an executable."""
    loom_home = unit_home / ".loom"
    _install_units(unit_home, loom_home, path_value=f"{loom_home}/venv/bin:rel/bin")
    assert cli._unit_integrity_check(loom_home)[0] == "warn"


def test_unit_integrity_flags_a_path_component_outside_the_whitelist(tmp_path, unit_home):
    loom_home = unit_home / ".loom"
    _install_units(unit_home, loom_home, path_value=f"{loom_home}/venv/bin:/opt/a b")
    assert cli._unit_integrity_check(loom_home)[0] == "warn"


def test_unit_integrity_accepts_a_redundantly_slashed_loom_home(tmp_path, unit_home):
    """`LOOM_HOME=/a/b/` and `Path('/a/b')` name the same directory: install.sh keeps the
    operator's spelling, so a redundant slash must not read as tampering."""
    loom_home = unit_home / ".loom"
    _install_units(unit_home, loom_home, path_value=f"{loom_home}//venv/bin:/usr/bin")
    assert cli._unit_integrity_check(loom_home)[0] == "ok"


def test_unit_integrity_flags_half_an_installed_pair(tmp_path, unit_home):
    loom_home = unit_home / ".loom"
    unit_dir = _install_units(unit_home, loom_home)
    (unit_dir / "loom-process.timer").unlink()
    level, msg = cli._unit_integrity_check(loom_home)
    assert level == "warn"
    assert "loom-process.timer" in msg


def test_unit_integrity_on_a_cron_branch_host_is_informational(tmp_path, unit_home):
    """AC-F7.3. macOS has no user manager and therefore no units: nothing to compare and
    nothing wrong. Never a ✗ — it must not colour doctor's exit code."""
    level, msg = cli._unit_integrity_check(unit_home / ".loom")
    assert level == "ok"
    assert "unit" in msg.lower()


def test_unit_integrity_unreadable_unit_warns_naming_the_type(tmp_path, unit_home):
    """AC-F7.3, second half (the F6/LOW-8 lesson): the probe reads files, so it can raise —
    and a doctor row must never become a traceback."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("chmod 000 does not block reads for root")
    loom_home = unit_home / ".loom"
    unit_dir = _install_units(unit_home, loom_home)
    svc = unit_dir / "loom-process.service"
    svc.chmod(0o000)
    try:
        level, msg = cli._unit_integrity_check(loom_home)
    finally:
        svc.chmod(0o644)
    assert level == "warn"
    assert "PermissionError" in msg


def test_unit_integrity_without_the_shipped_templates_is_a_warn(tmp_path, unit_home,
                                                                monkeypatch):
    """A pip-installed (non-editable) client has no checkout to compare against: say so
    instead of inventing drift."""
    loom_home = unit_home / ".loom"
    _install_units(unit_home, loom_home)
    monkeypatch.setattr(cli, "_shipped_unit_dir", lambda: None)
    level, msg = cli._unit_integrity_check(loom_home)
    assert level == "warn"
    assert "template" in msg.lower()


def test_render_service_unit_mirrors_the_installer_substitutions():
    """The comparison is only as good as this renderer: it must reproduce install.sh's awk
    — same lines, same ORDER — or every fresh install would report drift."""
    template = (SHIPPED_UNIT_DIR / "loom-process.service").read_text(encoding="utf-8")
    from pathlib import Path as _P

    rendered = cli._render_service_unit(template, _P("/opt/loom"), "/opt/loom/venv/bin:/usr/bin")

    at = rendered.index("[Service]")
    assert rendered[at + 1] == "Environment=LOOM_HOME=/opt/loom"
    assert rendered[at + 2] == "Environment=PATH=/opt/loom/venv/bin:/usr/bin"
    assert "ExecStart=/opt/loom/venv/bin/loom process --quiet" in rendered
    assert not any("%h" in line for line in rendered), "the template's %h must be resolved"


def test_unit_environment_parses_the_forms_systemd_writes():
    text = ('[Service]\nEnvironment=LOOM_HOME=/a/b\n'
            '  Environment="PATH=/x:/y"\nEnvironment=NOPE\nExecStart=/a/b/loom process\n')
    env = cli._unit_environment(text)
    assert env == {"LOOM_HOME": "/a/b", "PATH": "/x:/y"}


# --------------------------------------------------------------------------- #
# push-quarantine: fail-closed on a scanner error (wave-a F4, I4)              #
# --------------------------------------------------------------------------- #
def _quarantined(tmp_path, monkeypatch, content_md: str = SECRET_MD):
    """A LOOM_HOME with config.json and one quarantine file; returns its path."""
    loom_home = tmp_path / "loom_home"
    (loom_home / "quarantine").mkdir(parents=True)
    (loom_home / "config.json").write_text(json.dumps({
        "server_url": "http://127.0.0.1:9", "token": "t",
    }), encoding="utf-8")
    monkeypatch.setenv("LOOM_HOME", str(loom_home))
    path = loom_home / "quarantine" / "sess-q.json"
    path.write_text(json.dumps({
        "reason": "secret_scan",
        "entry": {"project_remote": "github.com/org/b", "session_id": "sess-q",
                  "content_md": content_md},
    }), encoding="utf-8")
    return path


def _no_upload(monkeypatch):
    from loom.api_client import ApiClient

    def boom(self, entry):  # pragma: no cover — reaching it is the failure
        raise AssertionError("a scanner error must never result in an upload")

    monkeypatch.setattr(ApiClient, "post_entry", boom)


@pytest.mark.parametrize("force", [False, True])
def test_push_quarantine_scanner_error_never_uploads(tmp_path, monkeypatch, capsys, force):
    """--force overrides findings the operator reviewed, never a missing verdict (I4)."""
    path = _quarantined(tmp_path, monkeypatch)
    _no_upload(monkeypatch)
    monkeypatch.setattr(scan_engine, "scan", lambda text, cfg=None: (_ for _ in ()).throw(
        scan_engine.GitleaksError("gitleaks exited 1 (3 bytes of stderr, not quoted)")))

    rc = cli.cmd_push_quarantine(argparse.Namespace(target="sess-q", force=force))

    err = capsys.readouterr().err
    assert rc == 1
    assert path.exists(), "the quarantine file must survive a failed rescan"
    assert "GitleaksError" in err


@pytest.mark.parametrize("exc", [
    FileNotFoundError("scanner.engine is 'gitleaks' but no binary was found"),
    ValueError("unknown scanner.engine 'gitleeks'"),
    RuntimeError("boom"),
])
def test_push_quarantine_is_fail_closed_for_any_scanner_exception(tmp_path, monkeypatch,
                                                                  capsys, exc):
    path = _quarantined(tmp_path, monkeypatch)
    _no_upload(monkeypatch)
    monkeypatch.setattr(scan_engine, "scan", lambda text, cfg=None: (_ for _ in ()).throw(exc))

    rc = cli.cmd_push_quarantine(argparse.Namespace(target="sess-q", force=False))

    assert rc == 1
    assert path.exists()
    assert type(exc).__name__ in capsys.readouterr().err


def test_push_quarantine_scanner_error_prints_no_scanned_content(tmp_path, monkeypatch,
                                                                 capsys):
    """The diagnostic carries the exception TYPE only — never the text being scanned."""
    _quarantined(tmp_path, monkeypatch)
    _no_upload(monkeypatch)
    leaky = scan_engine.GitleaksError(f"engine choked on {SECRET_MD}")
    monkeypatch.setattr(scan_engine, "scan",
                        lambda text, cfg=None: (_ for _ in ()).throw(leaky))

    rc = cli.cmd_push_quarantine(argparse.Namespace(target="sess-q", force=True))

    captured = capsys.readouterr()
    assert rc == 1
    assert "sk-live" not in captured.err + captured.out
    assert "Decisiones" not in captured.err + captured.out
