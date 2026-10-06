"""`loom unlink` (slice E of the 2026-09-23 plan): take back one wrong item link.

`loom unlink <link-id> --reason "<one line>"` revokes the link `loom log` printed as
`(link <id>)` next to a `→ replaced by` / `→ duplicate of` note. The link stays on the
server as history with who revoked it and why; the item counts as current again. The
reason is one line, checked here before any request; a link already revoked is not an
error; a server that predates item links says so.

Every expected value is a literal.
"""
from __future__ import annotations

import json

import pytest

from loom import cli
from test_cli_log import _json, _setup

REVOKED = {"id": 7, "revoked_at": "2026-09-24T10:00:00Z", "revoked_by": "ana",
           "revoke_reason": "REST stays for batch clients"}


def _revoke_server(env, response, seen=None, link_id=7):
    def handler(request):
        if seen is not None:
            seen.append(json.loads(request.data))
        return response
    env.httpserver.expect_request(f"/api/item-links/{link_id}/revoke",
                                  method="POST").respond_with_handler(handler)


def test_unlink_posts_the_reason_and_says_what_it_did(env, monkeypatch, capsys):
    _setup(env, monkeypatch)
    seen = []
    _revoke_server(env, _json(REVOKED), seen)

    rc = cli.main(["unlink", "7", "--reason", "REST stays for batch clients"])

    assert rc == 0
    assert seen == [{"reason": "REST stays for batch clients"}]
    assert capsys.readouterr().out == (
        "Link 7 revoked (kept as history). The item is current again unless another link "
        "still replaces it: `loom log` shows it.\n")


def test_a_reason_that_starts_with_a_dash_is_a_value(env, monkeypatch, capsys):
    _setup(env, monkeypatch)
    seen = []
    _revoke_server(env, _json(REVOKED), seen)

    assert cli.main(["unlink", "7", "--reason", "-- wrong target"]) == 0
    assert seen == [{"reason": "-- wrong target"}]


def test_a_link_already_revoked_is_not_an_error(env, monkeypatch, capsys):
    _setup(env, monkeypatch)
    _revoke_server(env, _json({"error": "already_revoked",
                               "detail": "Item link 7 is already revoked"}, status=409))

    assert cli.main(["unlink", "7", "--reason", "twice"]) == 0
    assert capsys.readouterr().out == "Link 7 was already revoked.\n"


def test_an_unknown_link_exits_1_naming_it(env, monkeypatch, capsys):
    _setup(env, monkeypatch)
    _revoke_server(env, _json({"error": "link_not_found", "detail": "No item link 7"},
                              status=404))

    assert cli.main(["unlink", "7", "--reason", "gone"]) == 1
    err = capsys.readouterr().err
    assert "link_not_found" in err and "7" in err


def test_a_server_without_item_links_says_so(env, monkeypatch, capsys):
    _setup(env, monkeypatch)
    _revoke_server(env, _json({"error": "not_found", "detail": "Not Found"}, status=404))

    assert cli.main(["unlink", "7", "--reason", "gone"]) == 1
    assert "does not support item links yet" in capsys.readouterr().err


@pytest.mark.parametrize("reason", ["", "   ", "two\nlines", "x" * 501])
def test_a_reason_that_is_not_one_short_line_is_refused_before_any_request(
        env, monkeypatch, capsys, reason):
    _setup(env, monkeypatch)

    assert cli.main(["unlink", "7", "--reason", reason]) == 2
    assert "one line" in capsys.readouterr().err
    assert env.httpserver.log == []


@pytest.mark.parametrize("link_id", ["0", "-3", "x", "7.1", "9" * 19])
def test_a_link_id_is_a_positive_integer(env, monkeypatch, capsys, link_id):
    _setup(env, monkeypatch)

    with pytest.raises(SystemExit) as exc:
        cli.main(["unlink", link_id, "--reason", "r"])
    assert exc.value.code == 2
    assert env.httpserver.log == []
