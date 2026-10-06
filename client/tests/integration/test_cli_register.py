"""Tests for `loom register` (PLAN.md §T6).

Central server = pytest-httpserver (mock). Real git repo with a real remote via the
``env`` fixture. The real ``~/.loom`` is NEVER touched (LOOM_HOME points at ``env``'s tmp).
"""
from __future__ import annotations

import json
import subprocess

from werkzeug.wrappers import Response

from loom import cli


def _json(body, status=200):
    return Response(json.dumps(body), status=status, content_type="application/json")


def _setup(env, monkeypatch, cwd):
    """Config with server_url = mock, LOOM_HOME = env.home, cwd pinned."""
    env.config("claude")
    monkeypatch.setenv("LOOM_HOME", str(env.home))
    monkeypatch.chdir(cwd)


def _posts(env, path="/api/projects"):
    return [req for req, _ in env.httpserver.log if req.method == "POST" and req.path == path]


# --- New registration ------------------------------------------------------------
def test_register_new_project_posts(env, monkeypatch, capsys):
    _setup(env, monkeypatch, env.repo)
    env.httpserver.expect_request("/api/projects", method="GET").respond_with_handler(
        lambda r: _json({"error": "project_not_found"}, 404) if r.args.get("remote") else _json({"projects": []})
    )
    env.httpserver.expect_request("/api/projects", method="POST").respond_with_handler(
        lambda r: _json({"id": 3, "canonical_remote": "github.com/org/b", "name": "b"}, 201)
    )

    rc = cli.main(["register"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "Registered" in out
    assert len(_posts(env)) == 1


# --- Already registered ----------------------------------------------------------
def test_register_already_registered_reports_who_and_when(env, monkeypatch, capsys):
    _setup(env, monkeypatch, env.repo)
    project = {
        "id": 3, "canonical_remote": "github.com/org/b", "name": "b",
        "created_by": "dev2", "created_at": "2026-07-01T00:00:00Z",
    }
    env.httpserver.expect_request("/api/projects", method="GET").respond_with_handler(
        lambda r: _json({"project": project}) if r.args.get("remote") else _json({"projects": [project]})
    )

    rc = cli.main(["register"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "Already registered by dev2" in out
    assert _posts(env) == []  # no POST is attempted if it already exists


# --- Twice in a row: the second one says "already registered" (AC) ---------------
def test_register_twice_second_says_already(env, monkeypatch, capsys):
    _setup(env, monkeypatch, env.repo)
    state = {"registered": False}
    project = {
        "id": 3, "canonical_remote": "github.com/org/b", "name": "b",
        "created_by": "dev2", "created_at": "2026-07-01T00:00:00Z",
    }

    def get_handler(r):
        if r.args.get("remote"):
            return _json({"project": project}) if state["registered"] else _json({"error": "project_not_found"}, 404)
        return _json({"projects": [project] if state["registered"] else []})

    def post_handler(r):
        state["registered"] = True
        return _json({"id": 3, "canonical_remote": "github.com/org/b", "name": "b"}, 201)

    env.httpserver.expect_request("/api/projects", method="GET").respond_with_handler(get_handler)
    env.httpserver.expect_request("/api/projects", method="POST").respond_with_handler(post_handler)

    assert cli.main(["register"]) == 0
    assert "Registered ✓" in capsys.readouterr().out
    assert cli.main(["register"]) == 0
    assert "Already registered" in capsys.readouterr().out


# --- No git repo / no remote → phase-3 fallback, exit 1 --------------------------
def test_register_no_git_repo_exits_one(env, monkeypatch, capsys, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    _setup(env, monkeypatch, plain)

    rc = cli.main(["register"])

    out = capsys.readouterr().out
    assert rc == 1
    assert "phase 3" in out


def test_register_git_without_remote_exits_one(env, monkeypatch, capsys, tmp_path):
    repo = tmp_path / "norem"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    _setup(env, monkeypatch, repo)

    rc = cli.main(["register"])

    assert rc == 1
    assert "phase 3" in capsys.readouterr().out


# --- Unreachable server → clear error, exit 1 ------------------------------------
def test_register_server_down_exits_one(env, monkeypatch, capsys):
    env.config("claude", server_url="http://127.0.0.1:1")
    monkeypatch.setenv("LOOM_HOME", str(env.home))
    monkeypatch.chdir(env.repo)

    rc = cli.main(["register"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "Could not reach the server" in err


# --- D9 (owner decision B, 2026-09-22): registering enables locally in opt-in mode ------
def _registered_now(env):
    project = {
        "id": 3, "canonical_remote": "github.com/org/b", "name": "b",
        "created_by": "dev2", "created_at": "2026-07-01T00:00:00Z",
    }
    env.httpserver.expect_request("/api/projects", method="GET").respond_with_handler(
        lambda r: _json({"error": "project_not_found"}, 404) if r.args.get("remote") else _json({"projects": [project]})
    )
    env.httpserver.expect_request("/api/projects", method="POST").respond_with_handler(
        lambda r: _json(project, 201)
    )


def test_register_in_opt_in_mode_enables_the_project_locally(env, monkeypatch, capsys):
    env.config("claude", capture_mode="opt_in")
    monkeypatch.setenv("LOOM_HOME", str(env.home))
    monkeypatch.chdir(env.repo)
    _registered_now(env)

    rc = cli.main(["register"])

    out = capsys.readouterr().out
    assert rc == 0
    assert env.local_state()["github.com/org/b"]["state"] == "enabled"
    assert "enabled on this machine" in out
    assert "from now on" in out


def test_register_already_registered_in_opt_in_mode_enables_locally_too(env, monkeypatch, capsys):
    env.config("claude", capture_mode="opt_in")
    monkeypatch.setenv("LOOM_HOME", str(env.home))
    monkeypatch.chdir(env.repo)
    project = {
        "id": 3, "canonical_remote": "github.com/org/b", "name": "b",
        "created_by": "dev2", "created_at": "2026-07-01T00:00:00Z",
    }
    env.httpserver.expect_request("/api/projects", method="GET").respond_with_handler(
        lambda r: _json({"project": project}) if r.args.get("remote") else _json({"projects": [project]})
    )

    rc = cli.main(["register"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "Already registered by dev2" in out
    assert env.local_state()["github.com/org/b"]["state"] == "enabled"


def test_register_in_legacy_mode_writes_no_local_state(env, monkeypatch, capsys):
    """Absent capture_mode = every registered project is captured already; there is
    nothing to enable and the output says nothing new (the frozen waves' expectation)."""
    env.config("claude")
    monkeypatch.setenv("LOOM_HOME", str(env.home))
    monkeypatch.chdir(env.repo)
    _registered_now(env)

    rc = cli.main(["register"])

    out = capsys.readouterr().out
    assert rc == 0
    assert env.local_state() == {}
    assert "enabled on this machine" not in out
    assert "capture_mode" not in env.config_json()
