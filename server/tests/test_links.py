"""E15 — entry_links schema + POST /api/links + derived status (T.F5 / t-entry-links).

Derived from the acceptance criteria of RT.3.1 in
``docs/IMPORTED-IMPROVEMENTS-SPEC.md``, and pinning the things the
frozen goal deliberately does not reach:

- the DDL is idempotent, applies to a pre-Block-T database, and the CHECK/UNIQUE/FK
  constraints are really in the table (not only in the endpoint's validators);
- ``status`` is DERIVED on every read — the precedence rule, the fact that removing a
  link restores the previous status, and that no ``entries`` column exists (Q-3.5);
- the ADDITIVE rule, in both directions: an entry nobody linked keeps today's exact
  key set, a linked one gains exactly ``links_in``/``links_out``/``status`` — including
  on the ranked (``q=``) branch;
- the 201/409/404/422 matrix, plus the shapes only a hostile client sends
  (over-long rationale, non-integer ids, unauthenticated);
- ``entries`` rows are byte-identical after a link is posted (D7).
"""
from __future__ import annotations

import sqlite3

import pytest

from server import db, links

# The seven frozen entry fields (phase1-api-contracts). Kept as its own literal rather
# than imported from the E13 test module: two suites asserting "the shape did not
# change" must not be able to change it for each other.
FROZEN_ENTRY_KEYS = {"id", "author", "session_id", "created_at",
                     "covered_from", "covered_to", "content_md"}
LINK_KEYS = {"links_in", "links_out", "status"}

OLD = "## Decisions\n- [decision] **Use REST for the sync API** — simplicity.\n"
NEW = "## Decisions\n- [decision] **Switch the sync API to gRPC** — reverses REST.\n"


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
@pytest.fixture
def seeded(client, user_factory):
    """A project with two entries, and the auth header that created them."""
    user = user_factory("linker")
    resp = client.post("/api/projects", json={"remote_url": "git@github.com:o/links.git"},
                       headers=user["auth"])
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]
    ids = [_post_entry(client, user["auth"], md, f"s-{n}")
           for n, md in enumerate((OLD, NEW))]
    return {"auth": user["auth"], "pid": pid, "old": ids[0], "new": ids[1]}


def _post_entry(client, auth, content_md: str, session_id: str) -> int:
    resp = client.post("/api/entries", json={
        "project_remote": "github.com/o/links", "session_id": session_id,
        "content_md": content_md, "content_hash": f"h-{session_id}",
        "model": "sonnet", "prompt_version": "v2"}, headers=auth)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _entries(client, auth, pid: int, **params) -> list[dict]:
    resp = client.get(f"/api/projects/{pid}/entries", headers=auth,
                      params={"limit": 100, **params})
    assert resp.status_code == 200, resp.text
    return resp.json()["entries"]


def _link(client, auth, **body):
    return client.post("/api/links", json=body, headers=auth)


def _entries_dump(path: str) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT * FROM entries ORDER BY id").fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Schema (t-entry-links)                                                       #
# --------------------------------------------------------------------------- #
def test_ensure_links_schema_is_idempotent_and_additive(tmp_path):
    """Rerunning the DDL changes nothing — including on a pre-Block-T database."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(db.SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    assert conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE name='entry_links'"
    ).fetchone()[0] == 0, "precondition: schema.sql knows nothing about entry_links"

    links.ensure_links_schema(conn)
    first = conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    links.ensure_links_schema(conn)
    links.ensure_links_schema(conn)
    assert conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall() \
        == first, "the DDL is not idempotent"
    conn.close()


def test_schema_enforces_the_frozen_constraints(env):
    """CHECK(kind), UNIQUE(from,to,kind) and the entries FK live in the TABLE.

    The endpoint validates all three too, but a constraint that only exists in a
    validator is one admin one-liner away from a row nothing downstream can render.
    """
    conn = db.connect(env)
    try:
        conn.execute("INSERT INTO users (name, email, token_hash, created_at) "
                     "VALUES ('u', 'u@x', 'h', '2026-01-01T00:00:00Z')")
        conn.execute("INSERT INTO projects (canonical_remote, name, created_by, created_at) "
                     "VALUES ('github.com/x/y', 'y', 1, '2026-01-01T00:00:00Z')")
        for n in (1, 2):
            conn.execute(
                "INSERT INTO entries (project_id, user_id, session_id, content_md, "
                "content_hash, created_at) VALUES (1, 1, ?, 'x', ?, '2026-01-01T00:00:00Z')",
                (f"s{n}", f"h{n}"))
        conn.commit()

        insert = ("INSERT INTO entry_links (from_entry, to_entry, kind, created_at) "
                  "VALUES (?, ?, ?, '2026-01-01T00:00:00Z')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, (2, 1, "renames"))          # CHECK(kind)
        conn.execute(insert, (2, 1, "supersedes"))
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, (2, 1, "supersedes"))       # UNIQUE triple
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, (2, 999999, "duplicates"))  # FK → entries(id)
        conn.rollback()
        # The same pair with the OTHER kind is a different fact, and allowed.
        conn.execute(insert, (2, 1, "duplicates"))
        conn.commit()
    finally:
        conn.close()


def test_status_is_never_a_stored_entries_column(env):
    """The Q-3.5 resolution, checked where it can actually be violated."""
    conn = db.connect(env)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(entries)")}
    finally:
        conn.close()
    assert "status" not in cols


# --------------------------------------------------------------------------- #
# links_for_entries / derive_status                                            #
# --------------------------------------------------------------------------- #
def test_derive_status_precedence():
    assert links.derive_status([]) == "current"
    assert links.derive_status([{"kind": "duplicates"}]) == "duplicate"
    assert links.derive_status([{"kind": "supersedes"}]) == "superseded"
    assert links.derive_status(
        [{"kind": "duplicates"}, {"kind": "supersedes"}]) == "superseded", (
        "an incoming 'supersedes' is the stronger statement and wins"
    )


def test_links_for_entries_omits_unlinked_entries(client, seeded, env):
    """The reader contract: no row for an entry with no links (see server/links.py)."""
    third = _post_entry(client, seeded["auth"], "## Decisions\n- [fact] **CI** — x.\n", "s-3")
    assert _link(client, seeded["auth"], from_entry=seeded["new"],
                 to_entry=seeded["old"], kind="supersedes").status_code == 201
    conn = db.connect(env)
    try:
        mapping = links.links_for_entries(conn, [seeded["old"], seeded["new"], third])
        assert links.links_for_entries(conn, []) == {}
    finally:
        conn.close()
    assert third not in mapping, "an entry with no links must not appear at all"
    assert mapping[seeded["old"]]["status"] == "superseded"
    assert mapping[seeded["old"]]["links_in"][0]["from_entry"] == seeded["new"]
    assert mapping[seeded["old"]]["links_out"] == []
    assert mapping[seeded["new"]]["status"] == "current"
    assert mapping[seeded["new"]]["links_out"][0]["to_entry"] == seeded["old"]


def test_links_for_entries_chunks_large_id_lists(client, seeded, env):
    """More ids than `_ID_CHUNK` must not become an sqlite variable-limit error.

    The read binds the id list TWICE (once per direction), so a page of the endpoint's
    own maximum (500) is 1000 parameters — over the 999 limit of the sqlite builds
    least likely to be the one anybody tests on.
    """
    conn = db.connect(env)
    try:
        ids = list(range(1, links._ID_CHUNK * 3 + 7))
        mapping = links.links_for_entries(conn, ids)
    finally:
        conn.close()
    assert mapping == {}, "no links exist yet, so nothing is reported for any chunk"


# --------------------------------------------------------------------------- #
# POST /api/links — the contract matrix                                        #
# --------------------------------------------------------------------------- #
def test_post_link_201_then_409_on_the_unique_triple(client, seeded):
    ok = _link(client, seeded["auth"], from_entry=seeded["new"], to_entry=seeded["old"],
               kind="supersedes", rationale="reverses the REST decision")
    assert ok.status_code == 201 and isinstance(ok.json()["id"], int)
    again = _link(client, seeded["auth"], from_entry=seeded["new"],
                  to_entry=seeded["old"], kind="supersedes")
    assert again.status_code == 409
    assert again.json()["error"] == "duplicate_link"


def test_the_same_pair_may_not_hold_two_contradictory_kinds(client, seeded):
    """`kind` is in the UNIQUE triple, so the DB alone would accept both rows.

    Two machines curating the same project is the normal pilot topology — `curate.lock`
    is host-local — so this is reachable without anyone doing anything strange, and it
    asserts two incompatible things about one pair of entries.
    """
    assert _link(client, seeded["auth"], from_entry=seeded["new"],
                 to_entry=seeded["old"], kind="supersedes").status_code == 201
    clash = _link(client, seeded["auth"], from_entry=seeded["new"],
                  to_entry=seeded["old"], kind="duplicates")
    assert clash.status_code == 409 and clash.json()["error"] == "contradictory_link"


def test_from_entry_must_be_the_newer_entry(client, seeded):
    """The contract's meaning of the field, enforced — which makes cycles unpostable.

    Without it, `old supersedes new` on top of `new supersedes old` marks BOTH entries
    superseded and the default `loom log` prints no body at all.
    """
    backwards = _link(client, seeded["auth"], from_entry=seeded["old"],
                      to_entry=seeded["new"], kind="supersedes")
    assert backwards.status_code == 422
    assert backwards.json()["error"] == "validation_error"
    # Order of checks: a missing entry is still a 404, not a 422 about ordering.
    assert _link(client, seeded["auth"], from_entry=seeded["old"], to_entry=999999,
                 kind="supersedes").status_code == 404


def test_a_non_unique_integrity_error_is_never_answered_as_409(client, seeded, env):
    """409 means "already linked" to every 409-tolerant caller — including `curate`.

    A CHECK violation arrives as the SAME `sqlite3.IntegrityError` as the UNIQUE
    triple, so a blanket catch would let `loom curate` count a link that was never
    created as posted and advance its watermark past it, silently and forever. The
    fixture is exactly the day this becomes reachable: a database whose CHECK was
    baked at CREATE-TABLE time, before a kind was added to `LINK_KINDS`.
    """
    from fastapi.testclient import TestClient

    from server.app import app

    assert "duplicates" in links.LINK_KINDS, "precondition: the endpoint accepts it"
    conn = db.connect(env)
    try:
        conn.execute("DROP TABLE entry_links")
        conn.execute(
            "CREATE TABLE entry_links ("
            "id INTEGER PRIMARY KEY, "
            "from_entry INTEGER NOT NULL REFERENCES entries(id), "
            "to_entry INTEGER NOT NULL REFERENCES entries(id), "
            "kind TEXT NOT NULL CHECK(kind IN ('supersedes')), "
            "rationale TEXT, created_at TEXT NOT NULL, "
            "UNIQUE(from_entry, to_entry, kind))"
        )
        conn.commit()
    finally:
        conn.close()

    with TestClient(app, raise_server_exceptions=False) as raw:
        resp = raw.post("/api/links", headers=seeded["auth"], json={
            "from_entry": seeded["new"], "to_entry": seeded["old"],
            "kind": "duplicates"})
    assert resp.status_code == 500, "a constraint that is not the UNIQUE triple is a fault"
    assert resp.json()["error"] == "internal_error"


def test_a_table_without_the_unique_triple_is_refused_at_startup(tmp_path):
    """The presence check is name-only, so the constraint itself is verified.

    Without `UNIQUE(from_entry, to_entry, kind)` the endpoint cannot answer 409 for a
    link that already exists, and every `loom curate` rerun would post it again.
    """
    path = tmp_path / "loose.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(db.SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.execute(
            "CREATE TABLE entry_links ("
            "id INTEGER PRIMARY KEY, from_entry INTEGER NOT NULL, "
            "to_entry INTEGER NOT NULL, kind TEXT NOT NULL, rationale TEXT, "
            "created_at TEXT NOT NULL)"
        )
        conn.execute("CREATE INDEX idx_entry_links_to ON entry_links(to_entry)")
        conn.commit()
        with pytest.raises(RuntimeError, match="UNIQUE"):
            links.ensure_links_schema(conn)
    finally:
        conn.close()


def test_ensure_links_schema_never_commits_the_callers_transaction(tmp_path):
    """`db.init_db` composes it with other DDL; it must not end their transaction.

    Verified where it bites: with an uncommitted INSERT open, an unguarded `commit()`
    inside the migration makes the caller's later `rollback()` a no-op.
    """
    path = tmp_path / "tx.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(db.SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
        conn.execute("INSERT INTO users (name, email, token_hash, created_at) "
                     "VALUES ('ghost', 'g@x', 'h', '2026-01-01T00:00:00Z')")
        assert conn.in_transaction, "precondition: the caller has work in flight"
        links.ensure_links_schema(conn)
        conn.rollback()
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0, (
            "the caller's rollback must still undo the caller's own INSERT"
        )
    finally:
        conn.close()


def test_post_link_rejects_unknown_endpoints_and_shapes(client, seeded, user_factory):
    auth = seeded["auth"]
    assert _link(client, auth, from_entry=seeded["new"], to_entry=999999,
                 kind="supersedes").json()["error"] == "entry_not_found"
    assert _link(client, auth, from_entry=999999, to_entry=seeded["old"],
                 kind="supersedes").status_code == 404
    for body, why in (
        ({"from_entry": seeded["new"], "to_entry": seeded["old"], "kind": "renames"},
         "kind outside the closed vocabulary"),
        ({"from_entry": seeded["old"], "to_entry": seeded["old"], "kind": "duplicates"},
         "self-link"),
        ({"from_entry": seeded["new"], "to_entry": seeded["old"], "kind": "supersedes",
          "rationale": "x" * 5000}, "rationale over the column bound"),
        ({"from_entry": "not-a-number", "to_entry": seeded["old"], "kind": "supersedes"},
         "non-integer id"),
        ({"to_entry": seeded["old"], "kind": "supersedes"}, "missing from_entry"),
    ):
        resp = _link(client, auth, **body)
        assert resp.status_code == 422, f"{why}: {resp.status_code} {resp.text}"
        assert resp.json()["error"] == "validation_error"
    # The rejected kind must not be echoed back into the error body.
    body = _link(client, auth, from_entry=seeded["new"], to_entry=seeded["old"],
                 kind="renames").json()
    assert "renames" not in body.get("detail", "")


def test_post_link_rejects_a_cross_project_pair(client, seeded, user_factory):
    resp = client.post("/api/projects", json={"remote_url": "git@github.com:o/other.git"},
                       headers=seeded["auth"])
    assert resp.status_code == 201
    other = client.post("/api/entries", json={
        "project_remote": "github.com/o/other", "session_id": "s-x",
        "content_md": "## Decisions\n- [fact] **x** — y.\n", "content_hash": "h-x"},
        headers=seeded["auth"])
    assert other.status_code == 201
    resp = _link(client, seeded["auth"], from_entry=seeded["new"],
                 to_entry=other.json()["id"], kind="duplicates")
    assert resp.status_code == 422 and resp.json()["error"] == "validation_error"


def test_post_link_requires_authentication(client, seeded):
    resp = client.post("/api/links", json={"from_entry": seeded["new"],
                                           "to_entry": seeded["old"],
                                           "kind": "supersedes"})
    assert resp.status_code == 401 and resp.json()["error"] == "unauthorized"


def test_posting_a_link_never_touches_the_entries_table(client, seeded, env):
    """D7: current state is new rows, never an UPDATE (the AC, at the DB level)."""
    before = _entries_dump(env)
    assert _link(client, seeded["auth"], from_entry=seeded["new"],
                 to_entry=seeded["old"], kind="supersedes").status_code == 201
    assert _entries_dump(env) == before


# --------------------------------------------------------------------------- #
# Entry GET responses — additive, both branches                                #
# --------------------------------------------------------------------------- #
def test_unlinked_entries_carry_the_link_fields_too(client, seeded):
    """`status`/`links_in`/`links_out` are UNCONDITIONAL — the contract's own words.

    A consumer writing the natural `entry.get("status") == "current"` must not silently
    get `False` for every entry nobody has curated yet. `snippet`/`rank` stay query
    metadata and remain absent, which is what keeps the two rules distinguishable.
    """
    for entry in _entries(client, seeded["auth"], seeded["pid"]):
        assert set(entry) == FROZEN_ENTRY_KEYS | LINK_KEYS
        assert entry["status"] == "current"
        assert entry["links_in"] == [] and entry["links_out"] == []
    for hit in _entries(client, seeded["auth"], seeded["pid"], q="sync API"):
        assert set(hit) == FROZEN_ENTRY_KEYS | LINK_KEYS | {"snippet", "rank"}
        assert hit["status"] == "current"


def test_linked_entries_carry_links_and_derived_status_on_both_branches(client, seeded):
    assert _link(client, seeded["auth"], from_entry=seeded["new"], to_entry=seeded["old"],
                 kind="supersedes", rationale="reversal").status_code == 201

    listed = {e["id"]: e for e in _entries(client, seeded["auth"], seeded["pid"])}
    assert set(listed[seeded["old"]]) == FROZEN_ENTRY_KEYS | LINK_KEYS
    assert listed[seeded["old"]]["status"] == "superseded"
    assert listed[seeded["old"]]["links_in"] == [{
        "from_entry": seeded["new"], "kind": "supersedes", "rationale": "reversal",
        "created_at": listed[seeded["old"]]["links_in"][0]["created_at"]}]
    assert listed[seeded["old"]]["links_out"] == []
    assert listed[seeded["new"]]["status"] == "current"
    assert listed[seeded["new"]]["links_out"][0]["to_entry"] == seeded["old"]

    ranked = {h["id"]: h for h in _entries(client, seeded["auth"], seeded["pid"],
                                           q="sync API")}
    assert set(ranked[seeded["old"]]) == FROZEN_ENTRY_KEYS | LINK_KEYS | {"snippet", "rank"}
    assert ranked[seeded["old"]]["status"] == "superseded"


def test_status_is_recomputed_on_every_read_not_stored(client, seeded, env):
    """Deleting the link restores `current` with no migration — the point of Q-3.5."""
    assert _link(client, seeded["auth"], from_entry=seeded["new"], to_entry=seeded["old"],
                 kind="duplicates").status_code == 201
    listed = {e["id"]: e for e in _entries(client, seeded["auth"], seeded["pid"])}
    assert listed[seeded["old"]]["status"] == "duplicate"

    conn = db.connect(env)
    try:
        conn.execute("DELETE FROM entry_links")
        conn.commit()
    finally:
        conn.close()
    listed = {e["id"]: e for e in _entries(client, seeded["auth"], seeded["pid"])}
    assert listed[seeded["old"]]["status"] == "current", (
        "removing the link restores `current` with no migration"
    )
    assert listed[seeded["old"]]["links_in"] == []
