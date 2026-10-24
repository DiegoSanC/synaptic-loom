"""RT.1 as amended on 2026-10-24: both search indexes stem English words.

`entries_fts` and `entry_items_fts` use `porter unicode61 remove_diacritics 2`, so a
search for `migration` finds "migrations". Entries are written in English since D15,
which is what the Porter stemmer stems; accents keep folding as unicode61 did.

A database whose index was built with the old tokenizer is migrated the way §6.4's rule
asks of any schema change: the check is repeated inside `BEGIN IMMEDIATE` (the server and
an `admin.py` run can start together), the index is rebuilt from the stored text in the
same transaction, a rerun changes nothing, and a current database costs one read and no
write lock. The old database is `golden/pre_d20.sql`, whose indexes predate the change.
"""
import sqlite3
import threading
from pathlib import Path

import pytest

from server import db, items, search

GOLDEN = Path(__file__).with_name("golden") / "pre_d20.sql"
TOKENIZE = "tokenize='porter unicode61 remove_diacritics 2'"
FTS_TABLES = ("entries_fts", "entry_items_fts")
REMOTE = "git@github.com:org/stems.git"


def _old_database(tmp_path, name="loom.db") -> str:
    path = tmp_path / name
    conn = sqlite3.connect(path)
    try:
        conn.executescript(GOLDEN.read_text(encoding="utf-8"))
        conn.execute("PRAGMA journal_mode=WAL")
    finally:
        conn.close()
    return str(path)


def _fts_sql(path) -> dict[str, str]:
    conn = sqlite3.connect(path)
    try:
        return {name: sql for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE name IN (?, ?)", FTS_TABLES)}
    finally:
        conn.close()


def _match(path, table, term) -> list[int]:
    conn = sqlite3.connect(path)
    try:
        return [row[0] for row in conn.execute(
            f"SELECT rowid FROM {table} WHERE {table} MATCH ? ORDER BY rowid", (term,))]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# What a reader sees                                                           #
# --------------------------------------------------------------------------- #
@pytest.fixture
def posted(client, user_factory):
    """One itemized entry posted through the API; returns (auth, project id, entry id)."""
    user = user_factory()
    project = client.post("/api/projects", json={"remote_url": REMOTE},
                          headers=user["auth"]).json()["id"]
    item = "- [fact] **Schema migrations run at startup, retrying twice**"
    content = f"## Context\nDatabase work.\n## Decisions\n{item}\n"
    resp = client.post("/api/entries", headers=user["auth"], json={
        "project_remote": REMOTE, "session_id": "s-stems", "covered_from": None,
        "covered_to": "u-1", "content_md": content, "content_hash": "a" * 64,
        "model": "sonnet", "prompt_version": "v4",
        "items": [{"item_type": "fact", "labelled": True, "text_md": item}],
    })
    assert resp.status_code == 201, resp.text
    return user["auth"], project, resp.json()["id"]


@pytest.mark.parametrize("terms", ["migration", "migrate", "retries", "migrating startups"])
def test_a_search_finds_another_form_of_the_word(client, posted, terms):
    auth, project, entry = posted
    hits = client.get(f"/api/projects/{project}/entries", params={"q": terms},
                      headers=auth).json()["entries"]
    assert [h["id"] for h in hits] == [entry]
    found = client.get(f"/api/projects/{project}/items",
                       params={"entry_ids": str(entry), "q": terms}, headers=auth).json()
    assert [i["match"] for i in found["items"]] == [True]


def test_migrant_does_not_match_migrations(client, posted):
    auth, project, _ = posted
    hits = client.get(f"/api/projects/{project}/entries", params={"q": "migrant"},
                      headers=auth).json()["entries"]
    assert hits == []


def test_a_prefix_longer_than_the_stem_finds_nothing(client, posted):
    # A prefix query is stemmed like any term, and the index holds stems: "migrations" is
    # stored as `migrat`, so `migratio*` (which matched before stemming) no longer does.
    # The trade-off DESIGN RT.1.1 records; the /loom skill tells agents to use whole words.
    auth, project, entry = posted

    def ids(q):
        return [h["id"] for h in client.get(f"/api/projects/{project}/entries",
                                            params={"q": q}, headers=auth).json()["entries"]]

    assert ids("migratio*") == []
    assert ids("migrat*") == [entry]
    assert ids("migrations") == [entry]


def test_unrelated_words_sharing_a_stem_now_match(env):
    # The other side of the trade-off, pinned so it stays a known, documented cost:
    # Porter reduces "university" and "universe" to one stem, and "general" and "generous".
    conn = db.connect(env)
    try:
        conn.execute("INSERT INTO users (name, email, token_hash, created_at) "
                     "VALUES ('u', 'u@example.com', 'h', '2026-10-24T09:00:00Z')")
        conn.execute("INSERT INTO projects (canonical_remote, name, created_at) "
                     "VALUES ('github.com/org/stems', 'stems', '2026-10-24T09:00:00Z')")
        for n, text in enumerate(("the university library", "a general rule"), 1):
            conn.execute(
                "INSERT INTO entries (project_id, user_id, session_id, content_md, "
                "content_hash, created_at) VALUES (1, 1, 's', ?, ?, '2026-10-24T09:00:00Z')",
                (text, str(n) * 64))
        conn.commit()
    finally:
        conn.close()
    assert _match(env, "entries_fts", "universe") == [1]
    assert _match(env, "entries_fts", "generous") == [2]


def test_both_indexes_are_built_with_the_stemming_tokenizer(env):
    for name, sql in _fts_sql(env).items():
        assert TOKENIZE in sql, (name, sql)
    assert set(_fts_sql(env)) == set(FTS_TABLES)


# --------------------------------------------------------------------------- #
# Migrating a database built with the old tokenizer                            #
# --------------------------------------------------------------------------- #
def test_the_golden_database_predates_the_stemming_tokenizer(tmp_path):
    for name, sql in _fts_sql(_old_database(tmp_path)).items():
        assert "tokenize" not in sql, (name, sql)


def test_an_old_index_is_rebuilt_with_the_new_tokenizer_and_keeps_every_row(tmp_path):
    path = _old_database(tmp_path)
    db.init_db(path).close()
    for name, sql in _fts_sql(path).items():
        assert TOKENIZE in sql, (name, sql)
    # The golden's one entry, re-indexed from the stored text.
    assert _match(path, "entries_fts", "sqlite") == [1]
    conn = db.connect(path)
    try:
        assert conn.execute("SELECT count(*) FROM entries_fts_docsize").fetchone()[0] == 1
        # And the trigger feeds the new index.
        conn.execute(
            "INSERT INTO entries (project_id, user_id, session_id, content_md, content_hash, "
            "created_at) VALUES (1, 1, 's-2', 'schema migrations at startup', ?, ?)",
            ("5" * 64, "2026-10-24T09:00:00Z"))
        conn.commit()
    finally:
        conn.close()
    assert _match(path, "entries_fts", "migration") == [2]


def test_the_items_index_is_rebuilt_from_its_stored_rows(tmp_path):
    path = _old_database(tmp_path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("INSERT INTO entry_items (entry_id, ordinal, item_type, labelled, "
                     "text_md) VALUES (1, 1, 'fact', 1, '- [fact] **schema migrations**')")
        conn.commit()
    finally:
        conn.close()
    # Indexed by the old trigger, with the old tokenizer: no stem match yet.
    assert _match(path, "entry_items_fts", "migration") == []
    db.init_db(path).close()
    assert _match(path, "entry_items_fts", "migration") == [1]


def test_an_interrupted_migration_leaves_the_old_index_whole(tmp_path, monkeypatch):
    path = _old_database(tmp_path)
    # The rebuild fails after the old index was dropped and the new one created.
    monkeypatch.setattr(search, "_BACKFILL_SQL",
                        "INSERT INTO entries_fts(entries_fts) VALUES('no_such_command')")
    with pytest.raises(sqlite3.Error):
        db.init_db(path)
    assert "tokenize" not in _fts_sql(path)["entries_fts"]
    assert _match(path, "entries_fts", "sqlite") == [1]
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT count(*) FROM entries_fts_docsize").fetchone()[0] == 1
    finally:
        conn.close()


def _fts_data(path, table) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT * FROM {table}_data ORDER BY id").fetchall()
    finally:
        conn.close()


def test_only_the_outdated_index_is_rebuilt(tmp_path):
    path = str(tmp_path / "loom.db")
    db.init_db(path).close()
    conn = sqlite3.connect(path)
    try:
        conn.execute("INSERT INTO users (name, email, token_hash, created_at) "
                     "VALUES ('u', 'u@example.com', 'h', '2026-10-24T09:00:00Z')")
        conn.execute("INSERT INTO projects (canonical_remote, name, created_at) "
                     "VALUES ('github.com/org/stems', 'stems', '2026-10-24T09:00:00Z')")
        conn.execute("INSERT INTO entries (project_id, user_id, session_id, content_md, "
                     "content_hash, created_at) VALUES (1, 1, 's', 'schema migrations', ?, "
                     "'2026-10-24T09:00:00Z')", ("6" * 64,))
        # The items index alone goes back to the old definition.
        conn.execute("DROP TABLE entry_items_fts")
        conn.execute("CREATE VIRTUAL TABLE entry_items_fts USING fts5(text_md, "
                     "content='entry_items', content_rowid='id')")
        conn.commit()
    finally:
        conn.close()
    entries_index = _fts_data(path, "entries_fts")
    db.init_db(path).close()
    assert TOKENIZE in _fts_sql(path)["entry_items_fts"]
    assert _fts_data(path, "entries_fts") == entries_index


def _drops(conn, apply) -> list[str]:
    """The tables ``apply(conn)`` drops, seen by SQLite's authorizer."""
    dropped: list[str] = []

    def authorizer(action, arg1, *_):
        if action in (sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_VTABLE):
            dropped.append(arg1)
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorizer)
    try:
        apply(conn)
    finally:
        conn.set_authorizer(None)
    return dropped


def test_the_tokenizer_is_re_read_under_the_lock(tmp_path):
    # A caller that found the index outdated may reach the write lock after another
    # process rebuilt it: under the lock the index is current, and nothing is dropped.
    path = str(tmp_path / "loom.db")
    db.init_db(path).close()
    conn = sqlite3.connect(path)
    try:
        assert _drops(conn, search._apply_schema) == []
    finally:
        conn.close()
    old = _old_database(tmp_path, "old.db")
    conn = sqlite3.connect(old)
    try:
        assert "entries_fts" in _drops(conn, search._apply_schema)
    finally:
        conn.close()


def test_a_migrated_database_has_the_index_definitions_of_a_fresh_one(tmp_path):
    migrated = _old_database(tmp_path, "old.db")
    db.init_db(migrated).close()
    fresh = tmp_path / "fresh.db"
    db.init_db(str(fresh)).close()
    assert _fts_sql(migrated) == _fts_sql(str(fresh))


def test_two_concurrent_init_db_calls_on_an_old_database_both_succeed(tmp_path):
    path = _old_database(tmp_path)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def run():
        try:
            barrier.wait()
            db.init_db(path).close()
        except BaseException as exc:  # noqa: BLE001 — reported by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors
    for name, sql in _fts_sql(path).items():
        assert TOKENIZE in sql, (name, sql)
    assert _match(path, "entries_fts", "sqlite") == [1]


def test_a_current_database_is_left_alone_without_taking_the_write_lock(env):
    holder = sqlite3.connect(env)
    try:
        holder.execute("BEGIN IMMEDIATE")
        # Another connection that would fail at once if it needed the write lock.
        conn = sqlite3.connect(env, timeout=0)
        try:
            search.ensure_search_schema(conn)
            items.ensure_items_schema(conn)
        finally:
            conn.close()
    finally:
        holder.rollback()
        holder.close()
