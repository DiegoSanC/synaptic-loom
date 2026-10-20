"""sqlite3 connection (stdlib, no ORM) and idempotent application of schema.sql.

- DB path from the `LOOM_DB` environment variable (default `./loom.db`) — the same
  variable app.py and admin.py use.
- WAL + foreign_keys ON + busy_timeout on every connection (safe concurrent writes).
- The schema is applied only if it does not exist yet (schema.sql uses CREATE TABLE
  without IF NOT EXISTS; we honour that and do not rewrite it — DESIGN.md §6.1).
- Block T extends the schema ADDITIVELY, without touching schema.sql: `init_db` calls
  the per-module idempotent DDL (`search.ensure_search_schema`,
  `links.ensure_links_schema`, `items.ensure_items_schema`, `auth.ensure_auth_schema`,
  `ui_auth.ensure_ui_schema`, `recall.ensure_recall_schema`,
  `feedback.ensure_feedback_schema`, `auth_events.ensure_auth_events_schema`)
  after applying the base schema, so an existing pilot database gains the new objects
  on the next start.
- A column on an existing table goes through `ensure_column`, DESIGN §6.4's migration rule
  (D20 adds `users.role` and `ui_sessions.kind` this way).
"""
import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from server import auth, auth_events, feedback, items, links, recall, search, ui_auth

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_BUSY_TIMEOUT_MS = 5000


def db_path() -> str:
    """Path of the SQLite file (env `LOOM_DB`, default `./loom.db`)."""
    return os.environ.get("LOOM_DB", "./loom.db")


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open a connection with WAL, foreign_keys ON and busy_timeout.

    One connection per operation/request: WAL allows concurrent readers + a single
    writer, and busy_timeout absorbs write contention without corrupting or raising.
    """
    conn = sqlite3.connect(path or db_path(), timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    """Apply schema.sql only if the tables do not exist yet (idempotent)."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if exists is None:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str,
                  backfill: str | None = None) -> bool:
    """Add ``column`` to ``table`` unless it is there — DESIGN §6.4's migration rule.

    ``definition`` must make the column nullable or give it a constant ``DEFAULT`` (SQLite
    refuses anything else in ``ADD COLUMN``). The presence check is repeated inside
    ``BEGIN IMMEDIATE``: the server and an ``admin.py`` run can both read "missing" and
    queue for the write lock, and the second one must then find the column, not add it
    twice. ``backfill`` runs in the same transaction as the ``ALTER``, so a committed
    column always has its backfill; it must still be written to be re-runnable (``WHERE
    <column> IS NULL``). Returns whether the column was added. Like the ``ensure_*``
    functions, it never commits a transaction it did not open.
    """
    if _has_column(conn, table, column):
        return False
    outermost = not conn.in_transaction
    if outermost:
        conn.execute("BEGIN IMMEDIATE")
    try:
        added = not _has_column(conn, table, column)
        if added:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            if backfill is not None:
                conn.execute(backfill)
    except BaseException:
        if outermost:
            conn.rollback()
        raise
    if outermost:
        conn.commit()
    return added


def init_db(path: str | None = None) -> sqlite3.Connection:
    """Connect and ensure the schema. Returns the connection (the caller closes it).

    The additive Block-T DDL runs on every call (it is idempotent): a database created
    before Block T gets `entries_fts` + its backfill, `entry_item_types` and
    `entry_links` here, which is why `schema.sql` never had to change.
    """
    conn = connect(path)
    apply_schema(conn)
    search.ensure_search_schema(conn)
    links.ensure_links_schema(conn)
    items.ensure_items_schema(conn)
    auth.ensure_auth_schema(conn)
    ui_auth.ensure_ui_schema(conn)          # references device_tokens: after auth
    recall.ensure_recall_schema(conn)
    feedback.ensure_feedback_schema(conn)   # references entry_items: after items
    auth_events.ensure_auth_events_schema(conn)
    return conn


# SQLite's INTEGER is a signed 64-bit value, so no row id is larger.
MAX_ROW_ID = 2**63 - 1


def is_row_id(value: int) -> bool:
    """Whether a path id can name a row at all. A path parameter is an unbounded Python
    int, and SQLite cannot even bind one past int64 (``OverflowError`` → 500); every
    route with an id answers such an id as it answers an unknown one."""
    return 1 <= value <= MAX_ROW_ID


def hash_token(token: str) -> str:
    """sha256 hex of the Bearer token — what gets stored in users.token_hash."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def iso_now() -> str:
    """UTC ISO-8601 timestamp with Z suffix, second precision (e.g. 2026-07-25T09:00:00Z)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
