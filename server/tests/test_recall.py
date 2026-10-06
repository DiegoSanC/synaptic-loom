"""Recall telemetry (issue #14/#15 measurement; owner decision of 2026-09-22, slice C).

``GET /api/projects/{id}/entries`` keeps its frozen contract. When the request carries
``X-Loom-Source: skill|cli`` the server records ONE ``recall_events`` row after the
query is answered — who consulted, which project, from where (the agent's skill or a
human at the CLI), the terms (decision A: stored in clear) and how many entries came
back. Everything else about the read is byte-for-byte what it was:

- no header (an older client, ``loom curate``'s own reads) → no row;
- a 401 never creates a row (the middleware answers before the handler runs);
- a 404 or a 422 is not an answered query → no row;
- an unknown header value is IGNORED — the reader keeps their answer, and the CHECK
  constraint can never be reached from the wire;
- the telemetry write can never cost the reader their answer;
- no row carries a credential (there is no column for one, and no value contains it).
"""
from __future__ import annotations

import sqlite3

import pytest

from server import db, recall

FROZEN_ENTRY_KEYS = {"id", "author", "session_id", "created_at",
                     "covered_from", "covered_to", "content_md"}

CONTENTS = ["bearer token authentication for the HTTP API",
            "vacuum the database weekly",
            "cache pip downloads in CI"]


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _rows(path: str) -> list[dict]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM recall_events ORDER BY id")]
    finally:
        conn.close()


def _legacy_db(path) -> sqlite3.Connection:
    """A REAL pre-Block-T database: schema.sql only."""
    conn = sqlite3.connect(str(path))
    conn.executescript(db.SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute("INSERT INTO users (name, email, token_hash, created_at) "
                 "VALUES ('u', 'u@x', 'h', '2026-01-01T00:00:00Z')")
    conn.execute("INSERT INTO projects (canonical_remote, name, created_by, created_at) "
                 "VALUES ('github.com/x/y', 'y', 1, '2026-01-01T00:00:00Z')")
    conn.commit()
    return conn


@pytest.fixture
def seeded(client, user_factory):
    """A project with three entries, over the API, and the user who wrote them."""
    user = user_factory("reader")
    resp = client.post("/api/projects", headers=user["auth"],
                       json={"remote_url": "git@github.com:org/recall.git"})
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]
    for n, md in enumerate(CONTENTS):
        resp = client.post("/api/entries", headers=user["auth"], json={
            "project_remote": "github.com/org/recall", "session_id": f"s-{n}",
            "content_md": md, "content_hash": f"h-{n}"})
        assert resp.status_code == 201, resp.text
    return {"user": user, "auth": user["auth"], "pid": pid}


def _read(client, seeded, *, source=None, headers=None, **params):
    hdrs = dict(seeded["auth"])
    if source is not None:
        hdrs["X-Loom-Source"] = source
    if headers:
        hdrs.update(headers)
    return client.get(f"/api/projects/{seeded['pid']}/entries", headers=hdrs,
                      params=params)


# --------------------------------------------------------------------------- #
# Schema                                                                       #
# --------------------------------------------------------------------------- #
def test_ensure_recall_schema_is_idempotent_and_applies_to_a_legacy_db(tmp_path):
    conn = _legacy_db(tmp_path / "legacy.db")
    try:
        recall.ensure_recall_schema(conn)
        recall.ensure_recall_schema(conn)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert "recall_events" in names
        cols = {r[1] for r in conn.execute("PRAGMA table_info(recall_events)")}
        assert cols == {"id", "user_id", "project_id", "source", "query",
                        "result_count", "created_at"}
    finally:
        conn.close()


def test_init_db_creates_recall_events_on_an_existing_database(tmp_path):
    path = tmp_path / "pre.db"
    _legacy_db(path).close()
    conn = db.init_db(str(path))
    try:
        assert conn.execute("SELECT count(*) FROM recall_events").fetchone()[0] == 0
    finally:
        conn.close()


def test_recall_events_constraints_are_in_the_table(tmp_path):
    conn = _legacy_db(tmp_path / "c.db")
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        recall.ensure_recall_schema(conn)
        ok = ("INSERT INTO recall_events (user_id, project_id, source, query, result_count, "
              "created_at) VALUES (?, ?, ?, ?, ?, ?)")
        conn.execute(ok, (1, 1, "skill", None, 0, "2026-09-23T00:00:00Z"))
        conn.execute(ok, (1, 1, "cli", "bearer", 2, "2026-09-23T00:00:00Z"))
        with pytest.raises(sqlite3.IntegrityError):          # CHECK (source)
            conn.execute(ok, (1, 1, "web", None, 0, "2026-09-23T00:00:00Z"))
        with pytest.raises(sqlite3.IntegrityError):          # NOT NULL result_count
            conn.execute(ok, (1, 1, "cli", None, None, "2026-09-23T00:00:00Z"))
        with pytest.raises(sqlite3.IntegrityError):          # FK user
            conn.execute(ok, (99, 1, "cli", None, 0, "2026-09-23T00:00:00Z"))
        with pytest.raises(sqlite3.IntegrityError):          # FK project
            conn.execute(ok, (1, 99, "cli", None, 0, "2026-09-23T00:00:00Z"))
    finally:
        conn.close()


def test_ensure_recall_schema_never_commits_a_transaction_it_did_not_open(tmp_path):
    conn = _legacy_db(tmp_path / "tx.db")
    try:
        conn.execute("INSERT INTO users (name, email, token_hash, created_at) "
                     "VALUES ('pending', 'p@x', 'h2', '2026-01-01T00:00:00Z')")
        assert conn.in_transaction
        recall.ensure_recall_schema(conn)
        conn.rollback()
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The read with the source header                                              #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("source", ["skill", "cli"])
def test_read_with_source_header_records_one_row_with_the_right_count(seeded, client, env, source):
    plain = _read(client, seeded, limit=2)
    assert plain.status_code == 200, plain.text
    resp = _read(client, seeded, source=source, limit=2)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["entries"]) == 2 and body["has_more"] is True
    assert body == plain.json()                        # the header changes NOTHING in the answer
    assert all(FROZEN_ENTRY_KEYS <= set(e) for e in body["entries"])

    rows = _rows(env)
    assert len(rows) == 1
    row = rows[0]
    assert row["user_id"] == seeded["user"]["id"]
    assert row["project_id"] == seeded["pid"]
    assert row["source"] == source
    assert row["query"] is None
    assert row["result_count"] == 2          # what came back, not the limit+1 fetched
    assert row["created_at"].endswith("Z") and len(row["created_at"]) == 20


def test_ranked_read_stores_the_query_in_clear_and_the_hit_count(seeded, client, env):
    resp = _read(client, seeded, source="skill", q="bearer token authentication")
    assert resp.status_code == 200, resp.text
    hits = resp.json()["entries"]
    assert len(hits) == 1
    (row,) = _rows(env)
    assert row["query"] == "bearer token authentication"      # decision A
    assert row["result_count"] == 1
    assert row["source"] == "skill"


def test_a_read_with_no_hits_still_records_a_zero_count(seeded, client, env):
    resp = _read(client, seeded, source="cli", q="zzqqxwyy")
    assert resp.status_code == 200
    assert resp.json()["entries"] == []
    (row,) = _rows(env)
    assert row["result_count"] == 0 and row["query"] == "zzqqxwyy"


def test_each_request_is_one_row(seeded, client, env):
    assert _read(client, seeded, source="skill").status_code == 200
    assert _read(client, seeded, source="cli", q="vacuum").status_code == 200
    assert _read(client, seeded, source="skill", limit=1).status_code == 200
    rows = _rows(env)
    assert [(r["source"], r["query"], r["result_count"]) for r in rows] == [
        ("skill", None, 3), ("cli", "vacuum", 1), ("skill", None, 1)]


def test_no_header_means_no_row(seeded, client, env):
    resp = _read(client, seeded)
    assert resp.status_code == 200
    assert len(resp.json()["entries"]) == 3
    assert _rows(env) == []


def test_a_401_never_creates_a_row(seeded, client, env):
    for headers in ({}, {"Authorization": "Bearer not-a-token"}):
        resp = client.get(f"/api/projects/{seeded['pid']}/entries",
                          headers={**headers, "X-Loom-Source": "skill"})
        assert resp.status_code == 401, resp.text
    assert _rows(env) == []


def test_404_and_422_are_not_answered_queries(seeded, client, env):
    resp = client.get("/api/projects/9999/entries",
                      headers={**seeded["auth"], "X-Loom-Source": "skill"})
    assert resp.status_code == 404
    resp = _read(client, seeded, source="skill", q='"unbalanced')
    assert resp.status_code == 422, resp.text
    resp = _read(client, seeded, source="skill", type="nonsense")
    assert resp.status_code == 422
    assert _rows(env) == []


def test_an_unknown_source_value_is_ignored_not_refused(seeded, client, env):
    resp = _read(client, seeded, source="web")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["entries"]) == 3
    assert _rows(env) == []


def test_the_header_name_is_case_insensitive(seeded, client, env):
    resp = _read(client, seeded, headers={"x-loom-source": "cli"})
    assert resp.status_code == 200
    assert [r["source"] for r in _rows(env)] == ["cli"]


def test_the_row_is_attributed_to_the_caller_not_the_author(seeded, client, env, user_factory):
    other = user_factory("other")
    resp = client.get(f"/api/projects/{seeded['pid']}/entries",
                      headers={**other["auth"], "X-Loom-Source": "cli"})
    assert resp.status_code == 200
    (row,) = _rows(env)
    assert row["user_id"] == other["id"] != seeded["user"]["id"]


def test_no_row_carries_a_credential(seeded, client, env, user_factory):
    other = user_factory("other")
    assert _read(client, seeded, source="skill", q="bearer").status_code == 200
    assert client.get(f"/api/projects/{seeded['pid']}/entries",
                      headers={**other["auth"], "X-Loom-Source": "cli"}).status_code == 200
    dump = " ".join(str(v) for row in _rows(env) for v in row.values())
    for token in (seeded["user"]["token"], other["token"]):
        assert token not in dump
    assert "token" not in {r[1] for r in sqlite3.connect(env).execute(
        "PRAGMA table_info(recall_events)")}


def test_telemetry_failure_never_costs_the_reader_the_answer(seeded, client, env):
    conn = sqlite3.connect(env)
    conn.execute("DROP TABLE recall_events")
    conn.commit()
    conn.close()
    resp = _read(client, seeded, source="skill", q="bearer")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["entries"]) == 1


def test_entries_rows_are_untouched_by_a_recall(seeded, client, env):
    before = sqlite3.connect(env).execute("SELECT * FROM entries ORDER BY id").fetchall()
    assert _read(client, seeded, source="skill").status_code == 200
    after = sqlite3.connect(env).execute("SELECT * FROM entries ORDER BY id").fetchall()
    assert before == after
