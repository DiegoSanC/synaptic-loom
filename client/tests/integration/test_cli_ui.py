"""`loom ui` (D14, plan 2026-09-29 slice 6: AC-15).

`loom ui [project]` trades this machine's bearer for a single-use login code
(`POST /api/ui-login`), builds `<server_url>/ui/login#code=…` — the code in the FRAGMENT,
never in a query string, because the server's access log records query strings — opens
the browser on it and always prints it, since a stock WSL2 has no browser Python can
find. With a project name, the fragment also carries `next=/ui/projects/<id>`.

The browser is the one seam this command does not own: `webbrowser.open` is replaced by a
recorder. Every expected value is a literal.
"""
from __future__ import annotations

import webbrowser

import pytest

from loom import auth, cli
from test_cli_log import _json, _project, _setup

CODE = "Uo1-login-code-for-this-browser-only-xyz"


@pytest.fixture
def opened(monkeypatch):
    """What the command asked the browser to open (the recorder answers 'opened')."""
    calls: list[str] = []

    def record(url, *args, **kwargs):
        calls.append(url)
        return True

    monkeypatch.setattr(webbrowser, "open", record)
    return calls


def _ui_login_server(env, response, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(dict(request.headers))
        return response
    env.httpserver.expect_request("/api/ui-login", method="POST").respond_with_handler(handler)


def _base(env) -> str:
    return env.httpserver.url_for("").rstrip("/")


def test_ui_opens_the_overview_with_the_code_in_the_fragment(env, monkeypatch, capsys, opened):
    _setup(env, monkeypatch)
    seen = []
    _ui_login_server(env, _json({"code": CODE, "expires_at": "2026-09-29T12:02:00Z"},
                                status=201), seen)

    rc = cli.main(["ui"])

    out = capsys.readouterr().out
    url = f"{_base(env)}/ui/login#code={CODE}"
    assert rc == 0
    assert opened == [url]
    assert seen[0]["Authorization"] == "Bearer test-token"
    assert out == ("Opening Loom in your browser. If nothing opened, open this link within "
                   f"2 minutes (it works once):\n  {url}\n")
    assert "?" not in url.split("#")[0]


def test_ui_with_a_project_lands_on_it(env, monkeypatch, capsys, opened):
    _setup(env, monkeypatch)
    env.httpserver.expect_request("/api/projects", method="GET").respond_with_json(
        {"projects": [_project(2, "beta", "github.com/org/beta")]})
    _ui_login_server(env, _json({"code": CODE, "expires_at": "2026-09-29T12:02:00Z"},
                                status=201))

    rc = cli.main(["ui", "beta"])

    assert rc == 0
    assert opened == [f"{_base(env)}/ui/login#code={CODE}&next=%2Fui%2Fprojects%2F2"]


def test_an_unknown_project_asks_for_no_code(env, monkeypatch, capsys, opened):
    _setup(env, monkeypatch)
    env.httpserver.expect_request("/api/projects", method="GET").respond_with_json(
        {"projects": [_project(2, "beta", "github.com/org/beta")]})

    rc = cli.main(["ui", "gamma"])

    assert rc == 1
    assert "No project matches 'gamma'." in capsys.readouterr().err
    assert opened == []
    assert not any(req.path == "/api/ui-login" for req, _ in env.httpserver.log)


def test_a_rejected_token_is_loud_and_opens_nothing(env, monkeypatch, capsys, opened):
    _setup(env, monkeypatch)
    _ui_login_server(env, _json({"error": "unauthorized", "detail": "Device token expired"},
                                status=401))

    rc = cli.main(["ui"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "Server error: HTTP 401 unauthorized: Device token expired" in err
    assert f"Hint: {auth.REJECTED_HINT}" in err
    assert opened == []


def test_a_server_without_the_ui_says_so(env, monkeypatch, capsys, opened):
    _setup(env, monkeypatch)
    # A pre-D14 server accepts the bearer (allow-list middleware), then has no such route.
    _ui_login_server(env, _json({"error": "not_found", "detail": "Not Found"}, status=404))

    rc = cli.main(["ui"])

    assert rc == 1
    assert "This server does not serve the web UI yet (no /api/ui-login)." in capsys.readouterr().err
    assert opened == []


def test_an_unreachable_server_exits_1(env, monkeypatch, capsys, opened):
    env.config("claude", server_url="http://127.0.0.1:9")
    monkeypatch.setenv("LOOM_HOME", str(env.home))

    rc = cli.main(["ui"])

    assert rc == 1
    assert "Could not reach the server" in capsys.readouterr().err
    assert opened == []


@pytest.mark.parametrize("browser", ["returns_false", "raises"])
def test_without_a_browser_the_printed_link_is_the_way_in(env, monkeypatch, capsys, browser):
    _setup(env, monkeypatch)
    _ui_login_server(env, _json({"code": CODE, "expires_at": "2026-09-29T12:02:00Z"},
                                status=201))

    def no_browser(url, *args, **kwargs):
        if browser == "raises":
            raise webbrowser.Error("could not locate runnable browser")
        return False

    monkeypatch.setattr(webbrowser, "open", no_browser)

    rc = cli.main(["ui"])

    assert rc == 0
    assert f"  {_base(env)}/ui/login#code={CODE}\n" in capsys.readouterr().out


def test_the_code_is_never_written_to_loom_home(env, monkeypatch, capsys, opened):
    _setup(env, monkeypatch)
    _ui_login_server(env, _json({"code": CODE, "expires_at": "2026-09-29T12:02:00Z"},
                                status=201))

    assert cli.main(["ui"]) == 0

    for path in env.home.rglob("*"):
        if path.is_file():
            assert CODE not in path.read_text(encoding="utf-8", errors="replace"), path
