"""D16 part 2 — capture PAUSES when gitleaks is unavailable (owner decision, 2026-10-15).

At the start of a ``loom process`` pass, a gitleaks that cannot be resolved or cannot
run ``gitleaks version`` — or a config that names an engine capture no longer accepts
(``regex``, an unknown value) — stops the pass before it touches a single marker:
markers stay byte-identical in ``queue/``, no attempt is counted, the summarizer never
runs, nothing is posted (not even the registry GET), one ``paused_no_scanner`` line is
logged and the command exits non-zero. ``loom status`` says so, and why.

Boundaries: real processor, real CLI entry point, pytest-httpserver as the server, a
``claude`` stub that leaves a sentinel when it is executed. gitleaks absence is real
(a PATH built from an allowlist that provably holds no gitleaks); a broken gitleaks is
a shim at ``$LOOM_HOME/bin`` — the binary is a boundary this client does not own.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from loom import cli, processor

CANNED_SUMMARY = "## Context\nWorked on the gate.\n## Decisions\n- Keep SQLite.\n"
ALLOWLIST = ("git", "python3", "jq", "env", "sh", "bash")
# Resolved at import, before any test narrows PATH. The suite requires the real
# gitleaks (AGENTS.md "Environment"), so its absence is a failure, never a skip.
REAL_GITLEAKS = shutil.which("gitleaks")


@pytest.fixture
def no_gitleaks_path(tmp_path, monkeypatch) -> Path:
    """A PATH holding git/python3/jq and provably NO gitleaks."""
    safe = tmp_path / "safe_bin"
    safe.mkdir()
    for name in ALLOWLIST:
        target = shutil.which(name)
        if target:
            (safe / name).symlink_to(target)
    monkeypatch.setenv("PATH", str(safe))
    assert shutil.which("gitleaks") is None
    return safe


def _sentinel_stub(env, sentinel: Path) -> str:
    """A claude stub that proves whether it ran."""
    path = env.home / "claude_sentinel.py"
    path.write_text(
        f"#!{sys.executable}\nimport pathlib, sys\nsys.stdin.read()\n"
        f"pathlib.Path({str(sentinel)!r}).write_text('ran')\n"
        f"sys.stdout.write({CANNED_SUMMARY!r})\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path)


def _set_engine(env, engine: str | None, pin: str | None = "8.30.1"):
    data = env.config_json()
    if engine is None:
        data.pop("scanner", None)
    else:
        data["scanner"] = {"engine": engine, "gitleaks_version": pin}
    (env.home / "config.json").write_text(json.dumps(data), encoding="utf-8")
    return processor.Config.load(env.home)


def _home_gitleaks(env, body: str) -> Path:
    path = env.home / "bin" / "gitleaks"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\nimport sys\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _log_lines(env) -> list[str]:
    log = env.home / "log" / "processor.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _assert_paused_untouched(env, report, marker: Path, before: bytes, sentinel: Path):
    assert report.paused, "the pass must report that capture is paused"
    assert [o.action for o in report.outcomes] == ["paused_no_scanner"]
    assert report.errors == 1
    assert marker.read_bytes() == before, "the marker must stay byte-identical"
    assert "attempts" not in json.loads(before)
    assert not sentinel.exists(), "the summarizer must not run while capture is paused"
    assert list(env.httpserver.log) == [], "nothing may reach the server, not even a GET"
    assert not (env.home / "state.json").exists()
    assert not list(env.home.glob("failed/*"))
    paused = [line for line in _log_lines(env) if "\tpaused_no_scanner" in line]
    assert len(paused) == 1, _log_lines(env)


# --------------------------------------------------------------------------- #
# The pause                                                                    #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("engine", ["gitleaks", "auto", None])
def test_missing_gitleaks_pauses_the_pass(env, no_gitleaks_path, tmp_path, engine):
    """gitleaks (default), the legacy ``auto`` and an absent scanner section all pause."""
    sentinel = tmp_path / "summarizer_ran"
    env.config(_sentinel_stub(env, sentinel))
    cfg = _set_engine(env, engine)
    marker = env.drop_marker("sess-paused")
    before = marker.read_bytes()

    report = processor.run_once(cfg)

    _assert_paused_untouched(env, report, marker, before, sentinel)
    assert "gitleaks" in report.paused


def test_regex_engine_pauses_the_pass_without_running_gitleaks(env, tmp_path):
    """``regex`` is refused for capture even with gitleaks installed — and never executed."""
    sentinel = tmp_path / "summarizer_ran"
    called = tmp_path / "gitleaks_ran"
    _home_gitleaks(env, f"open({str(called)!r}, 'w').write('x')\nsys.exit(0)\n")
    env.config(_sentinel_stub(env, sentinel))
    cfg = _set_engine(env, "regex")
    marker = env.drop_marker("sess-regex")
    before = marker.read_bytes()

    report = processor.run_once(cfg)

    _assert_paused_untouched(env, report, marker, before, sentinel)
    assert "regex" in report.paused
    assert not called.exists()


def test_unrunnable_gitleaks_version_pauses_the_pass(env, tmp_path):
    sentinel = tmp_path / "summarizer_ran"
    _home_gitleaks(env, "sys.exit(1)\n")
    env.config(_sentinel_stub(env, sentinel))
    cfg = _set_engine(env, "gitleaks")
    marker = env.drop_marker("sess-broken")
    before = marker.read_bytes()

    report = processor.run_once(cfg)

    _assert_paused_untouched(env, report, marker, before, sentinel)


def test_repeated_paused_passes_never_consume_attempts(env, no_gitleaks_path, tmp_path):
    """Five paused passes later the marker is still byte-identical and never parked."""
    sentinel = tmp_path / "summarizer_ran"
    env.config(_sentinel_stub(env, sentinel))
    cfg = _set_engine(env, "gitleaks")
    marker = env.drop_marker("sess-patient")
    before = marker.read_bytes()
    for _ in range(processor.MAX_ATTEMPTS + 2):
        report = processor.run_once(cfg)
        assert [o.action for o in report.outcomes] == ["paused_no_scanner"]
    assert marker.read_bytes() == before
    assert not (env.home / "failed" / "sess-patient.json").exists()
    assert not sentinel.exists()


def test_a_version_other_than_the_pin_does_not_pause(env, tmp_path):
    """A different version is a doctor WARNING, not a pause: the pass captures."""
    _home_gitleaks(env, (
        "if sys.argv[1:2] == ['version']:\n    print('8.18.0'); sys.exit(0)\n"
        "sys.stdin.read(); sys.stdout.write('[]'); sys.exit(0)\n"
    ))
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201)
    env.config(env.make_stub(stdout=CANNED_SUMMARY))
    cfg = _set_engine(env, "gitleaks", pin="8.30.1")
    env.drop_marker("sess-other-version")

    report = processor.run_once(cfg)

    assert not report.paused
    assert [o.action for o in report.outcomes] == ["posted"]


def test_legacy_auto_with_gitleaks_captures_like_gitleaks(env):
    """An existing config that still says ``auto`` is treated as ``gitleaks`` (D16)."""
    assert REAL_GITLEAKS, "the suite requires the real gitleaks (AGENTS.md)"
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201)
    env.config(env.make_stub(stdout=CANNED_SUMMARY))
    cfg = _set_engine(env, "auto", pin=None)
    env.drop_marker("sess-auto")

    report = processor.run_once(cfg)

    assert not report.paused
    assert [o.action for o in report.outcomes] == ["posted"]


# --------------------------------------------------------------------------- #
# The CLI surface: exit code, status, recovery                                #
# --------------------------------------------------------------------------- #
@pytest.fixture
def cli_home(env, monkeypatch):
    monkeypatch.setenv("LOOM_HOME", str(env.home))
    return env


@pytest.mark.parametrize("argv", [["process"], ["process", "--quiet"]])
def test_loom_process_exits_non_zero_and_says_why(cli_home, no_gitleaks_path, tmp_path,
                                                 capsys, argv):
    env = cli_home
    env.config(_sentinel_stub(env, tmp_path / "summarizer_ran"))
    _set_engine(env, "gitleaks")
    env.drop_marker("sess-cli")

    rc = cli.main(argv)

    out, err = capsys.readouterr()
    assert rc != 0
    assert "paused" in (out + err).lower()
    assert "gitleaks" in (out + err)


def test_processor_module_entry_point_exits_non_zero(cli_home, no_gitleaks_path, tmp_path):
    env = cli_home
    env.config(_sentinel_stub(env, tmp_path / "summarizer_ran"))
    _set_engine(env, "gitleaks")
    assert processor.main(["--quiet"]) != 0


def test_status_shows_capture_paused_and_why(cli_home, no_gitleaks_path, tmp_path, capsys):
    env = cli_home
    env.config(_sentinel_stub(env, tmp_path / "summarizer_ran"))
    _set_engine(env, "gitleaks")
    env.drop_marker("sess-status")
    cli.main(["process", "--quiet"])
    capsys.readouterr()

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "CAPTURE PAUSED" in out
    assert "gitleaks" in out
    assert "sess-status" in out  # the marker is still listed as pending


def test_status_shows_the_pause_before_any_pass_ran(cli_home, no_gitleaks_path, tmp_path,
                                                    capsys):
    """The live check: the NEXT pass would pause, so status says so already."""
    env = cli_home
    env.config(_sentinel_stub(env, tmp_path / "summarizer_ran"))
    _set_engine(env, "regex")
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "CAPTURE PAUSED" in out and "regex" in out


def test_status_is_quiet_about_the_scanner_when_capture_runs(cli_home, tmp_path, capsys):
    env = cli_home
    env.config(env.make_stub(stdout=CANNED_SUMMARY))
    _set_engine(env, "gitleaks")
    assert cli.main(["status"]) == 0
    assert "CAPTURE PAUSED" not in capsys.readouterr().out


def test_a_healthy_pass_lifts_the_recorded_pause(cli_home, no_gitleaks_path, capsys):
    env = cli_home
    env.config(env.make_stub(stdout=CANNED_SUMMARY))
    _set_engine(env, "gitleaks")
    env.drop_marker("sess-recover")
    assert cli.main(["process", "--quiet"]) != 0

    # The operator installs gitleaks where install.sh puts it; the next pass captures.
    assert REAL_GITLEAKS, "the suite requires the real gitleaks (AGENTS.md)"
    (env.home / "bin").mkdir(exist_ok=True)
    (env.home / "bin" / "gitleaks").symlink_to(REAL_GITLEAKS)
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 7}, status=201)
    capsys.readouterr()

    assert cli.main(["process", "--quiet"]) == 0
    cli.main(["status"])
    assert "CAPTURE PAUSED" not in capsys.readouterr().out
    assert len(env.posted_entries()) == 1
