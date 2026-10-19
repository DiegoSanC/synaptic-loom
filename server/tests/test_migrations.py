"""The migration rule (DESIGN §6.4, D20): a column on an existing table.

`PRAGMA table_info` is re-checked inside `BEGIN IMMEDIATE`, because the server and an
`admin.py` run can start together; the column is nullable or has a constant `DEFAULT`; a
backfill can be re-run. The old database is `golden/pre_d20.sql`, written by `init_db` as
it was before D20 (main at dae54e1) and frozen there.
"""
import sqlite3
import threading
import time
from pathlib import Path

from server import db

GOLDEN = Path(__file__).with_name("golden") / "pre_d20.sql"


def _pre_d20(tmp_path, name="loom.db") -> str:
    path = tmp_path / name
    conn = sqlite3.connect(path)
    try:
        conn.executescript(GOLDEN.read_text(encoding="utf-8"))
        # As `init_db` left it: WAL persists in the file. (Switching INTO WAL is the one
        # step two first connections can race on, and no deployed database takes it.)
        conn.execute("PRAGMA journal_mode=WAL")
    finally:
        conn.close()
    return str(path)


def _rows(path) -> dict[str, tuple[list[str], list[tuple]]]:
    """Every ordinary table's columns and rows, in rowid order."""
    conn = sqlite3.connect(path)
    try:
        tables = [r[1] for r in conn.execute("PRAGMA table_list")
                  if r[2] == "table" and not r[1].startswith("sqlite_")]
        out = {}
        for table in tables:
            cur = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
            out[table] = ([d[0] for d in cur.description], cur.fetchall())
        return out
    finally:
        conn.close()


def _columns(path, table) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def _schema(path) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    finally:
        conn.close()


def test_the_golden_database_predates_d20(tmp_path):
    path = _pre_d20(tmp_path)
    assert "role" not in _columns(path, "users")
    assert "kind" not in _columns(path, "ui_sessions")
    assert "kind" not in _columns(path, "enrolment_codes")
    assert _rows(path)["users"][1], "the golden database must hold rows to keep"


def test_a_pre_d20_database_migrates_and_keeps_every_row(tmp_path):
    path = _pre_d20(tmp_path)
    before = _rows(path)

    db.init_db(path).close()

    conn = sqlite3.connect(path)
    try:
        for table, (columns, rows) in before.items():
            names = ", ".join(f'"{c}"' for c in columns)
            assert conn.execute(f'SELECT {names} FROM "{table}" ORDER BY rowid').fetchall() == rows, table
        assert conn.execute("SELECT id, role FROM users ORDER BY id").fetchall() == [
            (1, "member"), (2, "member"), (3, "member")]
        assert conn.execute("SELECT id, kind FROM ui_sessions").fetchall() == [(1, "cli")]
        codes = conn.execute("SELECT kind, created_by_user_id, issued_by_device_token_id "
                             "FROM enrolment_codes").fetchall()
        assert codes and set(codes) == {("invite", None, None)}
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_a_migrated_database_has_the_schema_of_a_fresh_one(tmp_path):
    migrated = _pre_d20(tmp_path)
    db.init_db(migrated).close()
    fresh = str(tmp_path / "fresh.db")
    db.init_db(fresh).close()
    assert _schema(migrated) == _schema(fresh)


def test_the_new_columns_carry_the_section_6_4_definitions(tmp_path):
    path = str(tmp_path / "fresh.db")
    db.init_db(path).close()
    conn = sqlite3.connect(path)
    try:
        role = {r[1]: r for r in conn.execute("PRAGMA table_info(users)")}["role"]
        kind = {r[1]: r for r in conn.execute("PRAGMA table_info(ui_sessions)")}["kind"]
        codes = {r[1]: r for r in conn.execute("PRAGMA table_info(enrolment_codes)")}
        issuer_index = conn.execute("PRAGMA index_info(idx_enrolment_codes_issuer)").fetchall()
        fks = {(r[3], r[2], r[4]) for r in conn.execute("PRAGMA foreign_key_list(enrolment_codes)")}
    finally:
        conn.close()
    # (cid, name, type, notnull, dflt_value, pk)
    assert role[2:5] == ("TEXT", 1, "'member'")
    assert kind[2:5] == ("TEXT", 1, "'cli'")
    assert codes["kind"][2:5] == ("TEXT", 1, "'invite'")
    assert codes["created_by_user_id"][2:5] == ("INTEGER", 0, None)
    assert codes["issued_by_device_token_id"][2:5] == ("INTEGER", 0, None)
    assert [r[2] for r in issuer_index] == ["issued_by_device_token_id"]
    assert ("created_by_user_id", "users", "id") in fks
    assert ("issued_by_device_token_id", "device_tokens", "id") in fks


def test_init_db_on_a_migrated_database_changes_nothing(tmp_path):
    path = _pre_d20(tmp_path)
    db.init_db(path).close()
    schema, rows = _schema(path), _rows(path)
    db.init_db(path).close()
    assert _schema(path) == schema and _rows(path) == rows


def _init_in_thread(path, errors, barrier=None) -> threading.Thread:
    def run():
        try:
            if barrier is not None:
                barrier.wait(timeout=10)
            db.init_db(path).close()
        except BaseException as exc:  # reported to the test, not swallowed
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def test_two_concurrent_init_db_calls_both_succeed(tmp_path):
    for attempt in range(5):
        path = _pre_d20(tmp_path, f"race{attempt}.db")
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)
        threads = [_init_in_thread(path, errors, barrier) for _ in range(2)]
        for thread in threads:
            thread.join(timeout=30)
        assert errors == []
        assert _columns(path, "users").count("role") == 1
        assert _columns(path, "ui_sessions").count("kind") == 1


def test_the_column_is_rechecked_under_the_write_lock(tmp_path):
    """The interleaving the rule exists for, made deterministic: a second `init_db` reads
    `users` without `role`, then waits for the write lock while another connection adds
    it. It must find the column on its re-check and not add it again."""
    path = _pre_d20(tmp_path)
    holder = db.connect(path)
    try:
        holder.execute("BEGIN IMMEDIATE")
        errors: list[BaseException] = []
        thread = _init_in_thread(path, errors)
        time.sleep(0.5)
        assert thread.is_alive(), "the second init_db never waited for the write lock"
        db.ensure_column(holder, "users", "role", "TEXT NOT NULL DEFAULT 'member'")
        holder.commit()
        thread.join(timeout=30)
    finally:
        holder.close()
    assert errors == []
    assert _columns(path, "users").count("role") == 1


def test_ensure_column_backfills_in_the_same_transaction_and_reruns_harmlessly(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT)")
        conn.executemany("INSERT INTO t (a) VALUES (?)", [("x",), ("y",)])
        conn.commit()
        backfill = "UPDATE t SET b = upper(a) WHERE b IS NULL"
        assert db.ensure_column(conn, "t", "b", "TEXT", backfill=backfill) is True
        assert not conn.in_transaction
        assert [tuple(r) for r in conn.execute("SELECT a, b FROM t ORDER BY id")] == [("x", "X"), ("y", "Y")]
        assert db.ensure_column(conn, "t", "b", "TEXT", backfill=backfill) is False
        assert [r[1] for r in conn.execute("PRAGMA table_info(t)")].count("b") == 1
    finally:
        conn.close()


def test_ensure_column_joins_a_transaction_it_did_not_open(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        assert db.ensure_column(conn, "t", "c", "INTEGER") is True
        assert conn.in_transaction, "it committed a transaction it did not open"
        conn.rollback()
        assert "c" not in [r[1] for r in conn.execute("PRAGMA table_info(t)")]
    finally:
        conn.close()
