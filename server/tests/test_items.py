"""D13 (C8): the entry_items schema is additive, idempotent and self-healing.

The HTTP behaviour of items is the frozen goal `test/waves/p`; what is pinned HERE is
the migration: a database created before D13 gains the new objects at boot, running
it again changes nothing, and the item index always agrees with the item table.
"""
import sqlite3

from server import db, items


def _objects(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO users (name, email, token_hash, created_at) "
                 "VALUES ('u', 'u@x', 'h', '2026-09-23T00:00:00Z')")
    conn.execute("INSERT INTO projects (canonical_remote, name, created_by, created_at) "
                 "VALUES ('github.com/x/y', 'y', 1, '2026-09-23T00:00:00Z')")
    conn.execute("INSERT INTO entries (project_id, user_id, session_id, content_md, "
                 "content_hash, created_at) VALUES (1, 1, 's', ?, 'h1', "
                 "'2026-09-23T00:00:00Z')", ("## Decisions\n- [fact] **WAL on**\n",))
    items.insert(conn, 1, [{"item_type": "fact", "labelled": True,
                            "text_md": "- [fact] **WAL on**"}])
    conn.commit()


def _fts_hits(conn: sqlite3.Connection, term: str) -> list[int]:
    return [row[0] for row in conn.execute(
        "SELECT rowid FROM entry_items_fts WHERE entry_items_fts MATCH ?", (term,))]


def test_a_pre_d13_database_gains_the_item_objects_at_boot(tmp_path):
    path = str(tmp_path / "old.db")
    conn = db.connect(path)
    db.apply_schema(conn)          # what a database created before D13 holds
    assert "entry_items" not in _objects(conn)
    conn.close()

    conn = db.init_db(path)
    try:
        assert {"entry_items", "entry_items_fts", "entry_items_ai_fts", "item_links",
                "idx_item_links_active", "idx_item_links_to"} <= _objects(conn)
    finally:
        conn.close()


def test_ensure_items_schema_twice_on_a_populated_database_is_a_no_op(tmp_path):
    path = str(tmp_path / "loom.db")
    conn = db.init_db(path)
    try:
        _seed(conn)
        before = conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
        rows = conn.execute("SELECT * FROM entry_items").fetchall()
        items.ensure_items_schema(conn)
        items.ensure_items_schema(conn)
        assert conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == before
        assert conn.execute("SELECT * FROM entry_items").fetchall() == rows
        assert _fts_hits(conn, "WAL") == [1]
    finally:
        conn.close()


def test_an_index_out_of_step_with_its_table_is_rebuilt(tmp_path):
    """The docsize-equality rule of `search.ensure_search_schema`: an index that lost
    documents is healed at the next boot instead of silently missing them forever."""
    path = str(tmp_path / "loom.db")
    conn = db.init_db(path)
    try:
        _seed(conn)
        conn.execute("INSERT INTO entry_items_fts(entry_items_fts) VALUES('delete-all')")
        conn.commit()
        assert _fts_hits(conn, "WAL") == []
        items.ensure_items_schema(conn)
        assert _fts_hits(conn, "WAL") == [1]
    finally:
        conn.close()


def test_the_item_table_refuses_what_the_endpoint_refuses(tmp_path):
    """Defence in depth: a writer that skips the endpoint (an admin one-liner, a later
    slice) still cannot store an unknown label or a zero ordinal."""
    conn = db.init_db(str(tmp_path / "loom.db"))
    try:
        _seed(conn)
        for bad in (("banana", 2, 1), ("fact", 0, 1), ("fact", 3, 2)):
            try:
                conn.execute("INSERT INTO entry_items (entry_id, item_type, ordinal, "
                             "labelled, text_md) VALUES (1, ?, ?, ?, 'x')", bad)
            except sqlite3.IntegrityError:
                continue
            raise AssertionError(f"accepted {bad}")
    finally:
        conn.close()


def test_one_active_link_per_pair_and_kind_but_history_may_repeat(tmp_path):
    conn = db.init_db(str(tmp_path / "loom.db"))
    try:
        _seed(conn)
        items.insert(conn, 1, [])   # no-op
        conn.execute("INSERT INTO entries (project_id, user_id, session_id, content_md, "
                     "content_hash, created_at) VALUES (1, 1, 's2', 'x', 'h2', "
                     "'2026-09-23T00:00:01Z')")
        conn.execute("INSERT INTO entry_items (entry_id, ordinal, item_type, labelled, "
                     "text_md) VALUES (2, 1, 'fact', 1, 'x')")
        link = ("INSERT INTO item_links (from_item, to_item, kind, created_by, created_at, "
                "revoked_at) VALUES (2, 1, 'supersedes', 1, '2026-09-23T00:00:02Z', ?)")
        conn.execute(link, ("2026-09-23T00:00:03Z",))   # revoked history
        conn.execute(link, (None,))                     # active
        try:
            conn.execute(link, (None,))                 # a second active copy
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("two active copies of one item link")
        [status] = [i["status"] for i in items.items_for_entries(conn, 1, [1])]
        assert status == "superseded"
    finally:
        conn.close()
