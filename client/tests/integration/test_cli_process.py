"""Tests for `loom process` (PLAN.md §T6) — wrapper around processor.run_once.

Exit semantics: exit 0 on a normal pass, 2 if the config is missing; ``--quiet`` prints
nothing on a clean pass. The pipeline logic is covered in test_processor.py.
"""
from __future__ import annotations

from loom import cli


def test_process_missing_config_exits_two(monkeypatch, tmp_path, capsys):
    empty = tmp_path / "empty_home"
    empty.mkdir()
    monkeypatch.setenv("LOOM_HOME", str(empty))

    rc = cli.main(["process"])

    assert rc == 2
    assert "config" in capsys.readouterr().err.lower()


def test_process_clean_run_prints_summary(env, monkeypatch, capsys):
    env.config("claude")
    monkeypatch.setenv("LOOM_HOME", str(env.home))
    env.register_projects()  # empty queue → nothing to process

    rc = cli.main(["process"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "processed=0" in out and "errors=0" in out


def test_process_quiet_clean_run_is_silent(env, monkeypatch, capsys):
    env.config("claude")
    monkeypatch.setenv("LOOM_HOME", str(env.home))
    env.register_projects()

    rc = cli.main(["process", "--quiet"])

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == "" and captured.err == ""
