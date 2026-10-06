"""Item links and their revocation (slice E of the 2026-09-23 plan, D13).

The contract itself — 201/404/422/409, the author, revocation history, entry status left
alone — is frozen by wave q against a real server. These cases pin what that wave does
not: the bounds on the free-text fields, the bodies of the answers, and the F5 rule
(only the UNIQUE index is a duplicate; any other integrity failure is a 500, because a
409-tolerant curator reads 409 as "already linked" and moves on).

Every expected value is a literal.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from server import db

OLD = ("## Decisions\n"
       "- [decision] **Use REST for the sync API** — simplicity.\n"
       "- [method] **Retry three times** — flaky links.\n")
OLD_ITEMS = [
    {"item_type": "decision", "labelled": True,
     "text_md": "- [decision] **Use REST for the sync API** — simplicity."},
    {"item_type": "method", "labelled": True,
     "text_md": "- [method] **Retry three times** — flaky links."},
]
NEW = "## Decisions\n- [decision] **Switch to gRPC** — streaming.\n"
NEW_ITEMS = [{"item_type": "decision", "labelled": True,
              "text_md": "- [decision] **Switch to gRPC** — streaming."}]


def _entry(client, auth, sid, content, items):
    resp = client.post("/api/entries", headers=auth, json={
        "project_remote": "github.com/org/sync", "session_id": sid, "content_md": content,
        "content_hash": f"h-{sid}", "items": items})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture
def pair(client, user_factory):
    """Entry 1 (two items) and entry 2 (one item) in project `sync`; users ana and ben."""
    ana, ben = user_factory("ana"), user_factory("ben")
    resp = client.post("/api/projects", headers=ana["auth"],
                       json={"remote_url": "git@github.com:org/sync.git"})
    assert resp.status_code == 201, resp.text
    assert _entry(client, ana["auth"], "s1", OLD, OLD_ITEMS) == 1
    assert _entry(client, ana["auth"], "s2", NEW, NEW_ITEMS) == 2
    return {"ana": ana["auth"], "ben": ben["auth"]}


def _link(client, auth, **body):
    return client.post("/api/item-links", headers=auth,
                       json={"from": "2.1", "to": "1.1", "kind": "supersedes", **body})


def _rows(env) -> list[dict]:
    conn = sqlite3.connect(env)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM item_links ORDER BY id")]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# POST /api/item-links                                                         #
# --------------------------------------------------------------------------- #
def test_a_link_is_one_row_naming_both_items_and_its_author(client, pair, env):
    resp = _link(client, pair["ben"], rationale="gRPC replaces REST")
    assert resp.status_code == 201 and resp.json() == {"id": 1}
    [row] = _rows(env)
    assert (row["from_item"], row["to_item"], row["kind"], row["rationale"],
            row["created_by"], row["revoked_at"], row["revoked_by"],
            row["revoke_reason"]) == (3, 1, "supersedes", "gRPC replaces REST", 2,
                                      None, None, None)
    assert row["created_at"].endswith("Z")


def test_a_link_without_a_rationale_stores_none(client, pair, env):
    assert _link(client, pair["ana"]).status_code == 201
    assert _rows(env)[0]["rationale"] is None


def test_an_overlong_rationale_is_422_and_not_echoed(client, pair, env):
    marker = "R" * 2001
    resp = _link(client, pair["ana"], rationale=marker)
    assert resp.status_code == 422 and marker not in resp.text
    assert _rows(env) == []


@pytest.mark.parametrize("body", [
    {"from": None}, {"to": 11}, {"from": "2.1.1"}, {"from": " 2.1"}, {"to": "01.1"},
    {"from": "2.٣"}, {"kind": "SUPERSEDES"},
])
def test_malformed_bodies_are_422(client, pair, env, body):
    resp = _link(client, pair["ana"], **body)
    assert resp.status_code == 422, (body, resp.text)
    assert _rows(env) == []


def test_the_refused_kind_is_not_echoed(client, pair):
    resp = _link(client, pair["ana"], kind="<script>renames</script>")
    assert resp.status_code == 422 and "<script>" not in resp.text


def test_404_names_the_missing_item(client, pair):
    resp = _link(client, pair["ana"], to="1.7")
    assert resp.status_code == 404 and resp.json()["error"] == "item_not_found"
    assert "1.7" in resp.json()["detail"]


def test_a_non_unique_integrity_error_is_a_500_never_409(env, user_factory):
    from server.app import app

    ana = user_factory("ana")
    with TestClient(app, raise_server_exceptions=False) as raw:
        raw.post("/api/projects", headers=ana["auth"],
                 json={"remote_url": "git@github.com:org/sync.git"})
        _entry(raw, ana["auth"], "s1", OLD, OLD_ITEMS)
        _entry(raw, ana["auth"], "s2", NEW, NEW_ITEMS)
        conn = db.connect(env)
        try:
            conn.execute("CREATE TRIGGER e_abort BEFORE INSERT ON item_links "
                         "BEGIN SELECT RAISE(ABORT, 'e test abort'); END")
            conn.commit()
        finally:
            conn.close()
        resp = _link(raw, ana["auth"])
    assert resp.status_code == 500 and resp.json()["error"] == "internal_error", resp.text


# --------------------------------------------------------------------------- #
# POST /api/item-links/{id}/revoke                                             #
# --------------------------------------------------------------------------- #
def _revoke(client, auth, link_id=1, reason="REST stays for batch clients"):
    return client.post(f"/api/item-links/{link_id}/revoke", headers=auth,
                       json={"reason": reason})


def test_revoke_answers_the_link_and_records_the_revoker(client, pair, env):
    assert _link(client, pair["ana"]).status_code == 201
    resp = _revoke(client, pair["ben"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["id"], body["revoked_by"], body["revoke_reason"]) == (
        1, "ben", "REST stays for batch clients")
    assert body["revoked_at"].endswith("Z")
    [row] = _rows(env)
    assert (row["revoked_by"], row["revoke_reason"], row["revoked_at"]) == (
        2, "REST stays for batch clients", body["revoked_at"])


def test_an_overlong_reason_is_422_and_changes_nothing(client, pair, env):
    assert _link(client, pair["ana"]).status_code == 201
    assert _revoke(client, pair["ana"], reason="x" * 501).status_code == 422
    assert _rows(env)[0]["revoked_at"] is None


def test_revoking_an_unknown_link_is_404(client, pair):
    resp = _revoke(client, pair["ana"], link_id=42)
    assert resp.status_code == 404 and resp.json()["error"] == "link_not_found"


def test_a_link_revoked_as_contradictory_no_longer_blocks_the_other_kind(client, pair):
    assert _link(client, pair["ana"]).status_code == 201
    assert _link(client, pair["ana"], kind="duplicates").status_code == 409
    assert _revoke(client, pair["ana"]).status_code == 200
    assert _link(client, pair["ana"], kind="duplicates").status_code == 201


@pytest.mark.parametrize("link_id", [0, 2**63, 10**20])
def test_a_link_id_out_of_range_is_404_not_500(client, pair, link_id):
    resp = _revoke(client, pair["ana"], link_id=link_id)
    assert resp.status_code == 404 and resp.json()["error"] == "link_not_found", resp.text
