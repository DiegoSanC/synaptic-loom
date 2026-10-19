"""Tests for `loom status` (PLAN.md §T6, DESIGN §7 failure visibility).

status is informational: always exit 0 unless the config is absent (exit ≠ 0). Its one
server call is the account line's `GET /api/me` (D20, `test_cli_account.py`). ``claude_bin`` points at a nonexistent binary so the real CLI is not run.
"""
from __future__ import annotations

import json

from loom import cli

BOGUS_CLAUDE = "/nonexistent/claude-binary"


def test_status_reports_stuck_marker_failed_and_quarantine(env, monkeypatch, capsys):
    env.config(BOGUS_CLAUDE)
    monkeypatch.setenv("LOOM_HOME", str(env.home))
    for sub in ("queue", "failed", "quarantine"):
        (env.home / sub).mkdir(parents=True, exist_ok=True)

    # Stuck marker: old, with attempts and adapter_error.
    (env.home / "queue" / "sess-stuck.json").write_text(json.dumps({
        "session_id": "sess-stuck", "marked_at": "2026-01-01T00:00:00Z",
        "attempts": 2, "adapter_error": "schema mismatch",
        "pipeline_error": "pipeline failure: RuntimeError",
    }), encoding="utf-8")
    # Parked in failed/.
    (env.home / "failed" / "sess-old.json").write_text(json.dumps({
        "session_id": "sess-old", "attempts": 3,
    }), encoding="utf-8")
    # Quarantined entry.
    (env.home / "quarantine" / "sess-secret-20260101.json").write_text(json.dumps({
        "reason": "secret_scan", "findings": [{"kind": "openai_key", "redacted": "sk-*"}], "entry": {},
    }), encoding="utf-8")

    rc = cli.main(["status"])

    out = capsys.readouterr().out
    assert rc == 0
    # stuck marker with its age + tags
    assert "sess-stuck" in out
    assert "(age " in out
    assert "attempts=2" in out
    assert "adapter_error" in out
    assert "pipeline_error" in out
    # failed/ and quarantine listed
    assert "sess-old" in out
    assert "sess-secret-20260101.json" in out
    assert "openai_key" in out


def test_status_missing_config_exits_nonzero(monkeypatch, tmp_path, capsys):
    empty = tmp_path / "empty_home"
    empty.mkdir()
    monkeypatch.setenv("LOOM_HOME", str(empty))

    rc = cli.main(["status"])

    assert rc == 2
    assert "config" in capsys.readouterr().err.lower()


def test_status_renders_non_object_marker_files_without_crashing(env, monkeypatch, capsys):
    env.config(BOGUS_CLAUDE)
    monkeypatch.setenv("LOOM_HOME", str(env.home))
    for sub in ("queue", "failed", "quarantine"):
        directory = env.home / sub
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{sub}-bad.json").write_text("[1, 2]", encoding="utf-8")

    rc = cli.main(["status"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "queue-bad" in out
    assert "failed-bad" in out
    assert "quarantine-bad.json" in out


# --- A rejected credential is the first thing status says (issue #20) -------------
def test_status_shouts_a_rejected_token_before_anything_else(env, monkeypatch, capsys):
    env.config(BOGUS_CLAUDE)
    monkeypatch.setenv("LOOM_HOME", str(env.home))
    (env.home / "auth_rejected.json").write_text(json.dumps({
        "status": 401, "at": "2026-09-22T11:00:00Z", "detail": "POST /api/entries → 401 unauthorized",
    }), encoding="utf-8")

    rc = cli.main(["status"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "AUTH REJECTED" in out and "2026-09-22T11:00:00Z" in out and "loom enroll" in out
    assert out.index("AUTH REJECTED") < out.index("Pending markers")


def test_status_is_quiet_about_auth_when_nothing_was_rejected(env, monkeypatch, capsys):
    env.config(BOGUS_CLAUDE)
    monkeypatch.setenv("LOOM_HOME", str(env.home))

    cli.main(["status"])

    assert "AUTH REJECTED" not in capsys.readouterr().out


# --- D9: sessions skipped because this machine has not enabled the project -------------
def test_status_names_sessions_skipped_as_not_enabled(env, monkeypatch, capsys):
    env.config(BOGUS_CLAUDE, capture_mode="opt_in")
    monkeypatch.setenv("LOOM_HOME", str(env.home))
    (env.home / "log").mkdir(parents=True, exist_ok=True)
    (env.home / "log" / "processor.log").write_text(
        "2026-09-22T15:00:00Z\tsess-a\tdropped_not_enabled\tgithub.com/org/b\n"
        "2026-09-22T15:05:00Z\tsess-b\tposted\tid=7\n"
        "2026-09-22T15:10:00Z\tsess-c\tdropped_not_enabled\tgithub.com/org/b\n"
        "2026-09-22T15:11:00Z\tsess-d\tdropped_not_enabled\tgithub.com/org/c\n",
        encoding="utf-8",
    )

    rc = cli.main(["status"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "2 session(s) in github.com/org/b skipped: not enabled on this machine" in out
    assert "1 session(s) in github.com/org/c skipped: not enabled on this machine" in out
    assert "loom enable" in out


def test_status_says_nothing_about_not_enabled_when_none(env, monkeypatch, capsys):
    env.config(BOGUS_CLAUDE)
    monkeypatch.setenv("LOOM_HOME", str(env.home))

    rc = cli.main(["status"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "not enabled on this machine" not in out
