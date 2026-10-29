"""D22 — a developer is told, where they already look, that the server keeps extracts.

The notice names the projects this machine captures whose listing says
``store_extracts: true``: `loom enable` and `loom register` say it when they enable one,
`loom status` and `loom doctor` (which `install.sh --update` runs, so an existing install
sees it on its first update) list them. A project switched off, or a server from before
D22 (no key), produces no notice.
"""
from __future__ import annotations

import json

import pytest
from werkzeug.wrappers import Response

from loom import cli, projects_local
from loom.config import Config

REMOTE = "github.com/org/b"
OTHER = "github.com/org/c"
NOTICE = "keeps the redacted conversation"


def _project(remote, pid=3, **flags):
    return {"id": pid, "canonical_remote": remote, "name": remote.rsplit("/", 1)[-1],
            "created_by": "dev2", "created_at": "2026-07-01T00:00:00Z", **flags}


def _json(body, status=200):
    return Response(json.dumps(body), status=status, content_type="application/json")


def _serve(env, monkeypatch, projects, *, capture_mode="opt_in"):
    env.config("claude", capture_mode=capture_mode)
    monkeypatch.setenv("LOOM_HOME", str(env.home))
    monkeypatch.chdir(env.repo)
    env.httpserver.expect_request("/api/projects", method="GET").respond_with_handler(
        lambda r: _json({"projects": projects}))
    env.httpserver.expect_request("/api/health", method="GET").respond_with_json(
        {"status": "ok", "version": "0.1.0"})


@pytest.mark.parametrize("flags, told", [({"store_extracts": True}, True),
                                         ({"store_extracts": False}, False),
                                         ({}, False)],
                         ids=["on", "off", "server-before-D22"])
def test_enable_says_so_when_the_project_keeps_extracts(env, monkeypatch, capsys, flags, told):
    _serve(env, monkeypatch, [_project(REMOTE, **flags)])

    assert cli.main(["enable"]) == 0

    assert (NOTICE in capsys.readouterr().out) is told


def test_register_of_a_new_project_says_so(env, monkeypatch, capsys):
    _serve(env, monkeypatch, [])
    env.httpserver.expect_request("/api/projects", method="POST").respond_with_json(
        {"id": 3, "canonical_remote": REMOTE, "name": "b", "store_extracts": True},
        status=201)

    assert cli.main(["register"]) == 0

    assert NOTICE in capsys.readouterr().out


def test_status_and_doctor_name_the_captured_projects_that_keep_extracts(
        env, monkeypatch, capsys):
    _serve(env, monkeypatch, [_project(REMOTE, store_extracts=True),
                              _project(OTHER, pid=4, store_extracts=True),
                              _project("github.com/org/d", pid=5, store_extracts=False)])
    cfg = Config.load()
    for remote in (REMOTE, "github.com/org/d"):          # org/c is not enabled here
        projects_local.set_state(cfg, remote, projects_local.ENABLED)
    assert cli.main(["enable"]) == 0                       # fills the registry cache
    capsys.readouterr()

    cli.main(["status"])
    status = capsys.readouterr().out
    cli.main(["doctor"])
    doctor = capsys.readouterr().out

    for out in (status, doctor):
        [line] = [ln for ln in out.splitlines() if NOTICE in ln]
        assert REMOTE in line
        assert OTHER not in line and "org/d" not in line


def test_nothing_is_said_when_no_captured_project_keeps_extracts(env, monkeypatch, capsys):
    _serve(env, monkeypatch, [_project(REMOTE, store_extracts=False)])
    assert cli.main(["enable"]) == 0
    capsys.readouterr()

    cli.main(["status"])
    cli.main(["doctor"])

    assert NOTICE not in capsys.readouterr().out
