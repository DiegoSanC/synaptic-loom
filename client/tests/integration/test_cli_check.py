"""Tests for `loom check` (D10: the local task-start check the agent runs).

Five stable outputs, all decided LOCALLY against the registry cache: the cwd's remote is
compared with the listing and never sent to the server. The cache is refreshed only when
stale, with a short budget, and a failed refresh falls back to the stale cache. The mock
server's request log is the oracle for "nothing about a private repo leaves the machine".
"""
from __future__ import annotations

import json
import socket
import time

from werkzeug.wrappers import Response

from loom import cli

REMOTE = "github.com/org/b"
PROJECT = {"id": 3, "canonical_remote": REMOTE, "name": "b",
           "created_by": "dev2", "created_at": "2026-07-01T00:00:00Z"}


def _json(body, status=200):
    return Response(json.dumps(body), status=status, content_type="application/json")


def _serve(env, projects):
    env.httpserver.expect_request("/api/projects", method="GET").respond_with_handler(
        lambda r: _json({"projects": projects})
    )


def _requests(env):
    return [req for req, _ in env.httpserver.log]


def _cache(env, projects, fetched_at="2999-01-01T00:00:00Z"):
    (env.home / "projects_cache.json").write_text(json.dumps({
        "fetched_at": fetched_at, "projects": projects,
    }), encoding="utf-8")


def _setup(env, monkeypatch, cwd=None, **config_kwargs):
    env.config("claude", **config_kwargs)
    monkeypatch.setenv("LOOM_HOME", str(env.home))
    monkeypatch.chdir(cwd if cwd is not None else env.repo)


# --- the five states -------------------------------------------------------------------
def test_check_not_registered(env, monkeypatch, capsys):
    _setup(env, monkeypatch, capture_mode="opt_in")
    _serve(env, [])

    rc = cli.main(["check"])

    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "not_registered"
    assert all(not r.args.get("remote") for r in _requests(env))


def test_check_registered_undecided(env, monkeypatch, capsys):
    _setup(env, monkeypatch, capture_mode="opt_in")
    _serve(env, [PROJECT])

    rc = cli.main(["check"])

    assert rc == 0
    assert capsys.readouterr().out.strip() == "registered_undecided"


def test_check_enabled(env, monkeypatch, capsys):
    _setup(env, monkeypatch, capture_mode="opt_in")
    _serve(env, [PROJECT])
    env.set_local_state("enabled")

    rc = cli.main(["check"])

    assert rc == 0
    assert capsys.readouterr().out.strip() == "enabled"


def test_check_disabled(env, monkeypatch, capsys):
    _setup(env, monkeypatch, capture_mode="opt_in")
    _serve(env, [PROJECT])
    env.set_local_state("disabled")

    rc = cli.main(["check"])

    assert rc == 0
    assert capsys.readouterr().out.strip() == "disabled"


def test_check_legacy_mode_reports_a_registered_project_as_enabled(env, monkeypatch, capsys):
    """all_registered: the project IS captured on this machine, so the agent may consult."""
    _setup(env, monkeypatch)   # no capture_mode
    _serve(env, [PROJECT])

    rc = cli.main(["check"])

    assert rc == 0
    assert capsys.readouterr().out.strip() == "enabled"


def test_check_unknown_when_no_cache_and_server_down(env, monkeypatch, capsys):
    _setup(env, monkeypatch, capture_mode="opt_in", server_url="http://127.0.0.1:1")

    rc = cli.main(["check"])

    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "unknown"


def test_check_unknown_without_a_config(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "nohome"))
    monkeypatch.chdir(tmp_path)

    rc = cli.main(["check"])

    assert rc == 0
    assert capsys.readouterr().out.strip() == "unknown"


def test_check_outside_a_repo_is_not_registered(env, monkeypatch, tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    _setup(env, monkeypatch, cwd=plain, capture_mode="opt_in")
    _serve(env, [PROJECT])

    rc = cli.main(["check"])

    assert rc == 0
    assert capsys.readouterr().out.strip() == "not_registered"


# --- the cache discipline ----------------------------------------------------------------
def test_check_uses_a_fresh_cache_without_touching_the_server(env, monkeypatch, capsys):
    _setup(env, monkeypatch, capture_mode="opt_in")
    _cache(env, [PROJECT])   # fresh (future fetched_at), lists the project

    rc = cli.main(["check"])

    assert rc == 0
    assert capsys.readouterr().out.strip() == "registered_undecided"
    assert _requests(env) == []


def test_check_refreshes_a_stale_cache(env, monkeypatch, capsys):
    _setup(env, monkeypatch, capture_mode="opt_in")
    _cache(env, [], fetched_at="2000-01-01T00:00:00Z")   # stale, and lacks the project
    _serve(env, [PROJECT])

    rc = cli.main(["check"])

    assert rc == 0
    assert capsys.readouterr().out.strip() == "registered_undecided"
    assert len(_requests(env)) == 1
    cache = json.loads((env.home / "projects_cache.json").read_text(encoding="utf-8"))
    assert [p["canonical_remote"] for p in cache["projects"]] == [REMOTE]


def test_check_falls_back_to_the_stale_cache_when_the_server_is_down(env, monkeypatch, capsys):
    _setup(env, monkeypatch, capture_mode="opt_in", server_url="http://127.0.0.1:1")
    _cache(env, [PROJECT], fetched_at="2000-01-01T00:00:00Z")
    env.set_local_state("enabled")

    started = time.monotonic()
    rc = cli.main(["check"])
    elapsed = time.monotonic() - started

    assert rc == 0
    assert capsys.readouterr().out.strip() == "enabled"
    assert elapsed < 2.5


def test_check_gives_the_refresh_two_seconds_then_answers_from_the_stale_cache(
        env, monkeypatch, capsys):
    """A server that accepts the connection and never answers (a cold Fly Machine that
    hangs, a proxy black hole) costs the task-start check two seconds, not fifteen."""
    hole = socket.socket()
    hole.bind(("127.0.0.1", 0))
    hole.listen(1)          # accepts into the backlog; nobody ever reads or replies
    try:
        port = hole.getsockname()[1]
        _setup(env, monkeypatch, capture_mode="opt_in", server_url=f"http://127.0.0.1:{port}")
        _cache(env, [PROJECT], fetched_at="2000-01-01T00:00:00Z")

        started = time.monotonic()
        rc = cli.main(["check"])
        elapsed = time.monotonic() - started
    finally:
        hole.close()

    assert rc == 0
    assert capsys.readouterr().out.strip() == "registered_undecided"
    assert 1.5 < elapsed < 4.0, elapsed


# --- --json ------------------------------------------------------------------------------
def test_check_json_carries_state_project_and_reason(env, monkeypatch, capsys):
    _setup(env, monkeypatch, capture_mode="opt_in")
    _serve(env, [PROJECT])

    rc = cli.main(["check", "--json"])

    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["state"] == "registered_undecided"
    assert data["project"] == REMOTE
    assert isinstance(data["reason"], str) and data["reason"]


def test_check_json_unknown_names_the_reason(env, monkeypatch, capsys):
    _setup(env, monkeypatch, capture_mode="opt_in", server_url="http://127.0.0.1:1")

    rc = cli.main(["check", "--json"])

    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["state"] == "unknown"
    assert data["project"] is None
    assert "reach" in data["reason"].lower() or "cache" in data["reason"].lower()
