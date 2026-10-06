"""Recall telemetry: who consulted which logbook, from where, and what came back.

Owner decision of 2026-09-22 (issues #14 and #15; the plan in
``docs/plans/2026-09-22-feat-per-machine-opt-in-and-pilot-metrics-plan.md``): the pilot's
verdict needs to know whether Loom is consulted at all before it can ask whether what
was found was useful. The read route keeps its frozen contract; the only new input is
one request header, ``X-Loom-Source``, whose two values name the two readers there are —
the agent's ``/loom`` skill and a human at the CLI. A client that does not send it (an
older build, ``loom curate``'s own reads) is not a consultation and leaves no row.

Decision A of the same date: ``query`` is stored in clear. The terms an engineer looks
for ARE the signal the pilot measures, and the people who can read this table are the
people whose terms they are.

Additive schema, the ``ensure_*_schema`` idiom (``schema.sql`` untouched): a rolled-back
image leaves a table nobody reads.
"""
from __future__ import annotations

import logging
import sqlite3

from server import db as _db

log = logging.getLogger(__name__)

# The request header a reader sets to declare itself, and its closed vocabulary. The
# CHECK below is GENERATED from the tuple so the wire vocabulary and the table's can
# never drift; the values are that frozen literal tuple, never input.
SOURCE_HEADER = "X-Loom-Source"
SOURCES = ("skill", "cli")

_SOURCES_SQL_LIST = ", ".join(f"'{source}'" for source in SOURCES)
_RECALL_EVENTS_DDL = (
    "CREATE TABLE IF NOT EXISTS recall_events ("
    "id INTEGER PRIMARY KEY, "
    "user_id INTEGER NOT NULL REFERENCES users(id), "
    "project_id INTEGER NOT NULL REFERENCES projects(id), "
    f"source TEXT NOT NULL CHECK (source IN ({_SOURCES_SQL_LIST})), "
    "query TEXT, "
    "result_count INTEGER NOT NULL, "
    "created_at TEXT NOT NULL)"
)
_OBJECT_NAMES = ("recall_events",)


def ensure_recall_schema(conn: sqlite3.Connection) -> None:
    """Idempotent: the ``recall_events`` table.

    Same discipline as ``links.ensure_links_schema``: a presence check first (a migrated
    database costs one read and takes no write lock — this runs on every server start
    and every ``server.admin`` command), pure DDL with nothing to backfill, and it never
    commits a transaction it did not open.
    """
    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    if set(_OBJECT_NAMES) <= present:
        return
    outermost = not conn.in_transaction
    if outermost:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(_RECALL_EVENTS_DDL)
    except BaseException:
        if outermost:
            conn.rollback()
        raise
    if outermost:
        conn.commit()


def record(conn: sqlite3.Connection, source: str | None, *, user_id: int,
           project_id: int, query: str | None, result_count: int) -> bool:
    """One row for an ANSWERED read, when the request declared a known source.

    Returns whether a row was written. ``None`` and an unknown value alike write nothing
    and raise nothing: the header is telemetry, and a reader is never refused their
    answer over it — which is also why the table's CHECK cannot be reached from the
    wire. For the same reason a failed write (a database rolled back to an image without
    the table, a lock that outlived ``busy_timeout``) is logged and swallowed: the
    measurement losing one event is cheap, the agent losing its context is not.
    """
    if source not in SOURCES:
        return False
    try:
        conn.execute(
            "INSERT INTO recall_events (user_id, project_id, source, query, result_count, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, project_id, source, query, result_count, _db.iso_now()),
        )
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        log.warning("recall_events write failed (read still served): %s", exc)
        return False
    return True
