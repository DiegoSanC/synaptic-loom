"""sqlite3 connection (stdlib, no ORM) and idempotent application of schema.sql.

- DB path from the `LOOM_DB` environment variable (default `./loom.db`) — the same
  variable app.py and admin.py use.
- WAL + foreign_keys ON + busy_timeout on every connection (safe concurrent writes).
- The schema is applied only if it does not exist yet (schema.sql uses CREATE TABLE
  without IF NOT EXISTS; we honour that and do not rewrite it — PLAN.md §T2).
- Block T extends the schema ADDITIVELY, without touching schema.sql: `init_db` calls
  the per-module idempotent DDL (`search.ensure_search_schema`,
  `links.ensure_links_schema`, `items.ensure_items_schema`, `auth.ensure_auth_schema`,
  `ui_auth.ensure_ui_schema`, `recall.ensure_recall_schema`)
  after applying the base schema, so an existing pilot database gains the new objects
  on the next start.
"""
import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from server import auth, feedback, items, links, recall, search, ui_auth

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
    return conn


def hash_token(token: str) -> str:
    """sha256 hex of the Bearer token — what gets stored in users.token_hash."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def iso_now() -> str:
    """UTC ISO-8601 timestamp with Z suffix, second precision (e.g. 2026-07-25T09:00:00Z)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
