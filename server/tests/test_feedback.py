"""Feedback per item and the pilot's team totals (slice F of the 2026-09-23 plan, which
amends branch D of the 2026-09-22 plan; issues #14/#15).

Feedback is born item-grained (D13): it names ``<entry>.<ordinal>`` — the id `loom log`
prints — never a whole entry, because a per-entry row could never be re-assigned to the
item it was about. ``POST /api/feedback`` and ``GET /api/stats``:

- the same feedback twice → 201 then 409, one row;
- an item of another project → 422; an unknown item, or any item of an un-itemized entry
  (the maintainer's pre-v2 captures) → 404 ``item_not_found``;
- the three verdicts are accepted, a fourth is refused without echoing it;
- ``/api/stats`` counts recalls by source, feedback by verdict and the ten most useful
  items — team totals only: no field anywhere names or identifies a user.

Every expected value is a literal.
"""
from __future__ import annotations

import sqlite3

import pytest

from server import db, feedback

ITEMIZED = ("## Decisions\n"
            "- [decision] **Use SQLite with WAL for the queue** — concurrent readers.\n"
            "- [method] **Never vacuum during a flush** — it blocks writers.\n")
ITEMS = [
    {"item_type": "decision", "labelled": True,
     "text_md": "- [decision] **Use SQLite with WAL for the queue** — concurrent readers."},
    {"item_type": "method", "labelled": True,
     "text_md": "- [method] **Never vacuum during a flush** — it blocks writers."},
]
OTHER = "## Decisions\n- [fact] **CI runs on 3.12** — the runner image ships nothing else.\n"
OTHER_ITEMS = [{"item_type": "fact", "labelled": True,
                "text_md": "- [fact] **CI runs on 3.12** — the runner image ships nothing else."}]


def _project(client, auth, name):
    resp = client.post("/api/projects", headers=auth,
                       json={"remote_url": f"git@github.com:org/{name}.git"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _entry(client, auth, name, sid, content, items=None):
    body = {"project_remote": f"github.com/org/{name}", "session_id": sid,
            "content_md": content, "content_hash": f"h-{name}-{sid}"}
    if items is not None:
        body["items"] = items
    resp = client.post("/api/entries", headers=auth, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture
def logbook(client, user_factory):
    """Project `queue` (entry 1 itemized, entry 2 not), project `ci` (entry 3 itemized)."""
    ana = user_factory("ana")
    ben = user_factory("ben")
    queue = _project(client, ana["auth"], "queue")
    ci = _project(client, ana["auth"], "ci")
    assert _entry(client, ana["auth"], "queue", "s1", ITEMIZED, ITEMS) == 1
    assert _entry(client, ana["auth"], "queue", "s2", "- **Legacy** — a v1 entry.") == 2
    assert _entry(client, ana["auth"], "ci", "s3", OTHER, OTHER_ITEMS) == 3
    return {"ana": ana["auth"], "ben": ben["auth"], "queue": queue, "ci": ci}


def _feedback(client, auth, project_id, item, verdict="useful", note="it saved a day"):
    return client.post("/api/feedback", headers=auth, json={
        "project_id": project_id, "item": item, "verdict": verdict, "note": note})


def _rows(env) -> list[dict]:
    conn = sqlite3.connect(env)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM recall_feedback ORDER BY id")]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# POST /api/feedback                                                           #
# --------------------------------------------------------------------------- #
def test_the_same_feedback_twice_is_201_then_409_and_one_row(client, logbook, env):
    first = _feedback(client, logbook["ana"], logbook["queue"], "1.2")
    again = _feedback(client, logbook["ana"], logbook["queue"], "1.2")

    assert first.status_code == 201 and first.json() == {"id": 1}
    assert again.status_code == 409 and again.json()["error"] == "duplicate_feedback"
    [row] = _rows(env)
    assert (row["user_id"], row["project_id"], row["verdict"], row["note"]) == (
        1, logbook["queue"], "useful", "it saved a day")
    item = sqlite3.connect(env).execute(
        "SELECT entry_id, ordinal FROM entry_items WHERE id = ?", (row["item_id"],)).fetchone()
    assert item == (1, 2), "the row points at the ITEM, not at the entry"


def test_another_note_or_another_person_is_another_row(client, logbook, env):
    assert _feedback(client, logbook["ana"], logbook["queue"], "1.2").status_code == 201
    assert _feedback(client, logbook["ana"], logbook["queue"], "1.2",
                     note="and again next week").status_code == 201
    assert _feedback(client, logbook["ben"], logbook["queue"], "1.2").status_code == 201
    assert len(_rows(env)) == 3


@pytest.mark.parametrize("verdict", ["useful", "not_applicable", "stale"])
def test_the_three_verdicts_are_accepted(client, logbook, verdict):
    assert _feedback(client, logbook["ana"], logbook["queue"], "1.1",
                     verdict=verdict).status_code == 201


def test_a_fourth_verdict_is_refused_without_echoing_it(client, logbook, env):
    resp = _feedback(client, logbook["ana"], logbook["queue"], "1.1", verdict="brilliant")

    assert resp.status_code == 422
    assert "useful, not_applicable, stale" in resp.text and "brilliant" not in resp.text
    assert _rows(env) == []


def test_an_item_of_another_project_is_422(client, logbook, env):
    resp = _feedback(client, logbook["ana"], logbook["queue"], "3.1")

    assert resp.status_code == 422 and resp.json()["error"] == "item_not_in_project"
    assert _rows(env) == []


@pytest.mark.parametrize("item", ["9.1", "1.3", "2.1"])
def test_an_unknown_item_or_an_unitemized_entry_is_404_naming_it(client, logbook, item):
    resp = _feedback(client, logbook["ana"], logbook["queue"], item)

    assert resp.status_code == 404
    assert resp.json()["error"] == "item_not_found" and item in resp.json()["detail"]


@pytest.mark.parametrize("field, value", [
    ("item", "17"), ("item", "17.0"), ("item", "a.b"), ("item", "17.2.1"), ("item", " 1.2"),
    ("note", ""), ("note", "   "), ("note", "two\nlines"), ("note", "x" * 501),
])
def test_a_malformed_item_id_or_note_is_422(client, logbook, env, field, value):
    body = {"project_id": logbook["queue"], "item": "1.2", "verdict": "useful",
            "note": "fine", field: value}
    resp = client.post("/api/feedback", headers=logbook["ana"], json=body)

    assert resp.status_code == 422, resp.text
    assert _rows(env) == []


def test_feedback_needs_a_credential(client, logbook, env):
    resp = client.post("/api/feedback", json={"project_id": logbook["queue"], "item": "1.2",
                                              "verdict": "useful", "note": "x"})
    assert resp.status_code == 401 and _rows(env) == []


# --------------------------------------------------------------------------- #
# GET /api/stats                                                               #
# --------------------------------------------------------------------------- #
def _read(client, auth, project_id, source):
    resp = client.get(f"/api/projects/{project_id}/entries", headers={
        **auth, "X-Loom-Source": source})
    assert resp.status_code == 200, resp.text


def _keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def test_stats_are_team_totals_with_the_most_useful_items(client, logbook):
    _read(client, logbook["ana"], logbook["queue"], "skill")
    _read(client, logbook["ben"], logbook["queue"], "skill")
    _read(client, logbook["ben"], logbook["ci"], "cli")
    for auth in (logbook["ana"], logbook["ben"]):
        assert _feedback(client, auth, logbook["queue"], "1.2").status_code == 201
    assert _feedback(client, logbook["ben"], logbook["ci"], "3.1").status_code == 201
    assert _feedback(client, logbook["ana"], logbook["queue"], "1.1",
                     verdict="stale").status_code == 201

    resp = client.get("/api/stats", headers=logbook["ana"])

    assert resp.status_code == 200
    assert resp.json() == {
        "since": None,
        "recalls": {"skill": 2, "cli": 1},
        "feedback": {"useful": 3, "not_applicable": 0, "stale": 1},
        "top_useful": [
            {"item": "1.2", "project": "queue", "item_type": "method", "useful": 2},
            {"item": "3.1", "project": "ci", "item_type": "fact", "useful": 1},
        ],
    }
    assert not _keys(resp.json()) & {"user", "user_id", "users", "author", "name",
                                     "email", "created_by"}, "never per person"


def test_stats_count_only_what_happened_since(client, logbook):
    _read(client, logbook["ana"], logbook["queue"], "skill")
    assert _feedback(client, logbook["ana"], logbook["queue"], "1.2").status_code == 201

    resp = client.get("/api/stats", headers=logbook["ana"],
                      params={"since": "2999-01-01T00:00:00+00:00"})

    assert resp.status_code == 200
    assert resp.json() == {"since": "2999-01-01T00:00:00Z",
                           "recalls": {"skill": 0, "cli": 0},
                           "feedback": {"useful": 0, "not_applicable": 0, "stale": 0},
                           "top_useful": []}


def test_stats_top_useful_stops_at_ten(client, logbook):
    content = "## Decisions\n" + "".join(f"- [fact] **Fact {n}** — r.\n" for n in range(12))
    items = [{"item_type": "fact", "labelled": True, "text_md": f"- [fact] **Fact {n}** — r."}
             for n in range(12)]
    entry = _entry(client, logbook["ana"], "queue", "s-many", content, items)
    for n in range(1, 13):
        assert _feedback(client, logbook["ana"], logbook["queue"],
                         f"{entry}.{n}").status_code == 201

    top = client.get("/api/stats", headers=logbook["ana"]).json()["top_useful"]

    assert [row["item"] for row in top] == [f"{entry}.{n}" for n in range(1, 11)]


@pytest.mark.parametrize("since", ["yesterday", "2026-13-01T00:00:00Z", "",
                                   "0001-01-01T00:00:00+05:00",
                                   "9999-12-31T23:59:59-05:00"])
def test_stats_refuse_a_since_that_is_not_a_date(client, logbook, since):
    resp = client.get("/api/stats", headers=logbook["ana"], params={"since": since})
    assert resp.status_code == 422 and resp.json()["error"] == "invalid_since"


def test_stats_need_a_credential(client, logbook):
    assert client.get("/api/stats").status_code == 401


# --------------------------------------------------------------------------- #
# Schema                                                                       #
# --------------------------------------------------------------------------- #
def test_the_schema_is_additive_and_idempotent_on_a_legacy_database(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(db.SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.close()

    for _ in range(2):
        db.init_db(str(path)).close()

    conn = sqlite3.connect(str(path))
    columns = [row[1] for row in conn.execute("PRAGMA table_info(recall_feedback)")]
    fks = {(row[3], row[2]) for row in conn.execute("PRAGMA foreign_key_list(recall_feedback)")}
    conn.close()
    assert columns == ["id", "user_id", "project_id", "item_id", "verdict", "note",
                       "feedback_hash", "created_at"]
    assert ("item_id", "entry_items") in fks, "feedback references an ITEM (D13)"
    assert feedback.VERDICTS == ("useful", "not_applicable", "stale")


def test_top_useful_counts_people_not_rows(client, logbook):
    """Owner decision of 2026-09-24: one person saying `useful` about one item several
    times (different notes) is one person; the ranking measures reuse across people."""
    for note in ("first time", "second time", "third time"):
        assert _feedback(client, logbook["ana"], logbook["queue"], "1.1",
                         note=note).status_code == 201
    for auth in (logbook["ana"], logbook["ben"]):
        assert _feedback(client, auth, logbook["queue"], "1.2").status_code == 201

    body = client.get("/api/stats", headers=logbook["ana"]).json()

    assert body["top_useful"] == [
        {"item": "1.2", "project": "queue", "item_type": "method", "useful": 2},
        {"item": "1.1", "project": "queue", "item_type": "decision", "useful": 1},
    ]
    assert body["feedback"]["useful"] == 5, "the verdict totals still count every row"
