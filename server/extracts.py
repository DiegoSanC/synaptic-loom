"""D22 — the redacted extract an entry was summarized from.

The client sends the text the summarizer read (step 5b's output: every secret the scanner
found already replaced by ``[REDACTED:<kind>]``), cut to its first and last
``client/loom/processor.EXTRACT_HEAD_CHARS`` characters. It is stored zlib-compressed in
``entry_extracts``, one row per entry, and is **never indexed**: search reads the
summary and its items only, exactly as before.

``projects.store_extracts`` (default 1) is the per-project switch, turned by
``admin.py extracts``. When it is 0 the entry is stored and its extract is discarded; the
listing carries the flag so a client can skip sending it at all.
"""
from __future__ import annotations

import sqlite3
import zlib

# Two halves of 250 000 characters and the line the client puts between them: the client
# never sends more than this, so anything longer is not one of its extracts (→ 422).
MAX_CHARS = 2 * 250_000 + 1_000

PROJECTS_COLUMN_DDL = "INTEGER NOT NULL DEFAULT 1"

_EXTRACTS_DDL = """
CREATE TABLE IF NOT EXISTS entry_extracts (
  entry_id INTEGER PRIMARY KEY REFERENCES entries(id),
  body BLOB NOT NULL,                  -- zlib(utf-8 text)
  chars INTEGER NOT NULL,
  created_at TEXT NOT NULL
)
"""


def ensure_extracts_schema(conn: sqlite3.Connection) -> None:
    """Idempotent: ``entry_extracts`` and the ``projects.store_extracts`` column.

    Like the other ``ensure_*`` functions it never commits a transaction it did not open.
    An existing project gains the column at its default: on.
    """
    from server import db  # function-level: server.db imports this module

    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'entry_extracts'"
                    ).fetchone() is None:
        outermost = not conn.in_transaction
        if outermost:
            conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(_EXTRACTS_DDL)
        except BaseException:
            if outermost:
                conn.rollback()
            raise
        if outermost:
            conn.commit()
    db.ensure_column(conn, "projects", "store_extracts", PROJECTS_COLUMN_DDL)


def insert(conn: sqlite3.Connection, entry_id: int, text: str, created_at: str) -> None:
    """Store ``text`` for ``entry_id``, in the caller's transaction."""
    conn.execute(
        "INSERT INTO entry_extracts (entry_id, body, chars, created_at) VALUES (?, ?, ?, ?)",
        (entry_id, zlib.compress(text.encode("utf-8"), 9), len(text), created_at),
    )


def fetch(conn: sqlite3.Connection, entry_id: int) -> dict | None:
    """``{"entry_id", "extract", "chars"}``, or ``None`` when the entry has none."""
    row = conn.execute("SELECT body, chars FROM entry_extracts WHERE entry_id = ?",
                       (entry_id,)).fetchone()
    if row is None:
        return None
    return {"entry_id": entry_id, "extract": zlib.decompress(row[0]).decode("utf-8"),
            "chars": row[1]}


def purge(conn: sqlite3.Connection, project_id: int) -> int:
    """Delete every stored extract of ``project_id``, in the caller's transaction.

    The rows are gone; their bytes are not until `rewrite_database` has run.
    """
    cur = conn.execute(
        "DELETE FROM entry_extracts WHERE entry_id IN "
        "(SELECT id FROM entries WHERE project_id = ?)", (project_id,))
    return cur.rowcount


def rewrite_database(conn: sqlite3.Connection) -> bool:
    """Rebuild the database file from its live rows and empty the WAL, so that neither
    holds the bytes of a deleted row. Run it after the purge's commit, outside any
    transaction.

    A ``DELETE`` only unlinks: the bytes stay in the freed space, in the pages the table
    rewrote as it grew (an older copy of its first rows) and in the WAL's earlier frames.
    ``PRAGMA secure_delete`` on the purging connection zeroes the first of those and not
    the second, which is why this is a ``VACUUM``: it copies the live rows into a new file
    and nothing else. The ``TRUNCATE`` checkpoint then writes that copy over the database
    file and cuts the WAL to zero bytes.

    ``False`` when the checkpoint could not finish because another connection was still
    reading an earlier state: the WAL then still holds the old frames, and a second run
    completes it. ``VACUUM`` itself waits for a writer like any other write
    (``busy_timeout``) and needs room for a second copy of the database while it runs.
    """
    conn.execute("VACUUM")
    busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
    return busy == 0
