"""Tests for `loom enable` / `loom disable` (D9: the per-machine capture opt-in).

The registry is the mock server; the local state is ``<LOOM_HOME>/projects_local.json``,
never a file inside the repository. The real ``~/.loom`` is NEVER touched.
"""
from __future__ import annotations

import json

from werkzeug.wrappers import Response

from loom import cli

REMOTE = "github.com/org/b"
PROJECT = {"id": 3, "canonical_remote": REMOTE, "name": "b",
           "created_by": "dev2", "created_at": "2026-07-01T00:00:00Z"}


def _json(body, status=200):
    return Response(json.dumps(body), status=status, content_type="application/json")


def _setup(env, monkeypatch, cwd, *, registered=True, capture_mode=None):
    env.config("claude", capture_mode=capture_mode)
    monkeypatch.setenv("LOOM_HOME", str(env.home))
    monkeypatch.chdir(cwd)
    projects = [PROJECT] if registered else []
    env.httpserver.expect_request("/api/projects", method="GET").respond_with_handler(
        lambda r: _json({"projects": projects})
    )


def _registry_reads(env):
    return [req for req, _ in env.httpserver.log
            if req.method == "GET" and req.path == "/api/projects"]


# --- enable ------------------------------------------------------------------------------
def test_enable_registered_project_records_it_and_switches_to_opt_in(env, monkeypatch, capsys):
    _setup(env, monkeypatch, env.repo)

    rc = cli.main(["enable"])

    out = capsys.readouterr().out
    assert rc == 0, out
    assert env.local_state()[REMOTE]["state"] == "enabled"
    assert env.local_state()[REMOTE]["since"].endswith("Z")
    assert env.config_json()["capture_mode"] == "opt_in"
    assert "enabled" in out.lower() and REMOTE in out
    # D9 amendment (2026-09-30): the history is not backfilled, and the person is told.
    assert "from now on" in out
    # The switch is said out loud: the other registered projects on this machine are
    # no longer captured until enabled.
    assert "opt-in" in out
    # The remote was never sent to the server: enable resolves against the listing.
    assert all(not r.args.get("remote") for r in _registry_reads(env))


def test_enable_forces_a_registry_refresh(env, monkeypatch):
    """A fresh-looking cache that lacks the project must not win over the server: the
    person is at the keyboard, so enable asks."""
    _setup(env, monkeypatch, env.repo)
    (env.home / "projects_cache.json").write_text(json.dumps({
        "fetched_at": "2999-01-01T00:00:00Z", "projects": [],
    }), encoding="utf-8")

    rc = cli.main(["enable"])

    assert rc == 0
    assert len(_registry_reads(env)) == 1
    assert env.local_state()[REMOTE]["state"] == "enabled"
    cache = json.loads((env.home / "projects_cache.json").read_text(encoding="utf-8"))
    assert [p["canonical_remote"] for p in cache["projects"]] == [REMOTE]


def test_enable_unregistered_project_refuses_and_writes_nothing(env, monkeypatch, capsys):
    _setup(env, monkeypatch, env.repo, registered=False)

    rc = cli.main(["enable"])

    out = capsys.readouterr()
    assert rc == 1
    assert "not registered" in out.out + out.err
    assert "loom register" in out.out + out.err
    assert env.local_state() == {}
    assert "capture_mode" not in env.config_json()


def test_enable_twice_is_idempotent(env, monkeypatch, capsys):
    _setup(env, monkeypatch, env.repo)
    assert cli.main(["enable"]) == 0
    first = env.local_state()[REMOTE]

    rc = cli.main(["enable"])

    out = capsys.readouterr().out
    assert rc == 0
    assert env.local_state() == {REMOTE: first}   # same `since`, one entry
    assert "already" in out.lower()


def test_enable_outside_a_repo_with_a_remote_fails(env, monkeypatch, tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    _setup(env, monkeypatch, plain)

    rc = cli.main(["enable"])

    err = capsys.readouterr()
    assert rc == 1
    assert "remote" in (err.out + err.err)
    assert env.local_state() == {}


def test_enable_with_server_down_and_no_cache_fails_loudly(env, monkeypatch, capsys):
    env.config("claude", server_url="http://127.0.0.1:1")
    monkeypatch.setenv("LOOM_HOME", str(env.home))
    monkeypatch.chdir(env.repo)

    rc = cli.main(["enable"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "reach" in err.lower()
    assert env.local_state() == {}


# --- disable -----------------------------------------------------------------------------
def test_disable_records_the_no_and_switches_to_opt_in(env, monkeypatch, capsys):
    _setup(env, monkeypatch, env.repo)

    rc = cli.main(["disable"])

    out = capsys.readouterr().out
    assert rc == 0, out
    assert env.local_state()[REMOTE]["state"] == "disabled"
    assert env.config_json()["capture_mode"] == "opt_in"
    assert "disabled" in out.lower() and REMOTE in out


def test_disable_works_for_an_unregistered_project(env, monkeypatch):
    """The "no" is recorded whatever the server says: if the project is registered
    tomorrow, this machine still does not capture it."""
    _setup(env, monkeypatch, env.repo, registered=False)

    rc = cli.main(["disable"])

    assert rc == 0
    assert env.local_state()[REMOTE]["state"] == "disabled"


def test_disable_then_enable_flips_the_state(env, monkeypatch):
    _setup(env, monkeypatch, env.repo)
    assert cli.main(["disable"]) == 0
    assert cli.main(["enable"]) == 0
    assert env.local_state()[REMOTE]["state"] == "enabled"
    assert cli.main(["disable"]) == 0
    assert env.local_state()[REMOTE]["state"] == "disabled"


def test_local_state_lives_in_loom_home_not_in_the_repo(env, monkeypatch):
    _setup(env, monkeypatch, env.repo)
    assert cli.main(["enable"]) == 0
    assert (env.home / "projects_local.json").exists()
    assert not any(p.name.startswith(".loom") or "loom" in p.name for p in env.repo.iterdir()
                   if p.name != ".git")
