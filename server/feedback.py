"""Feedback per item, and the pilot's team totals (issues #14/#15).

Slice F of ``docs/plans/2026-09-23-fix-label-grammar-fidelity-plan.md``, which amends
branch D of the 2026-09-22 plan: feedback is born ITEM-grained (D13). A row names one
``<entry>.<ordinal>`` — the id ``loom log`` prints — because the pilot's question is
"was THIS decision useful", and a row about a whole entry could never be re-assigned to
the item it meant. Un-itemized entries (the maintainer's pre-v2 captures) cannot
receive feedback.

``/api/stats`` answers with team totals only — recalls by source, feedback by verdict,
the ten most useful items — and never a per-person field: measuring people is a
non-goal of #14 and #15.

Additive schema, the ``ensure_*_schema`` idiom (``schema.sql`` untouched; a rolled-back
image leaves a table nobody reads). It references ``entry_items``, so it is ensured
after ``items.ensure_items_schema``.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from server import recall

# The closed vocabulary. The CHECK below is GENERATED from it, so the wire's vocabulary
# and the table's cannot drift; the values are this literal tuple, never input.
VERDICTS = ("useful", "not_applicable", "stale")
# One line, like a commit subject: enough to say why, short enough to read in a table.
MAX_NOTE_LEN = 500
TOP_USEFUL = 10

_VERDICTS_SQL_LIST = ", ".join(f"'{verdict}'" for verdict in VERDICTS)
_FEEDBACK_DDL = (
    "CREATE TABLE IF NOT EXISTS recall_feedback ("
    "id INTEGER PRIMARY KEY, "
    "user_id INTEGER NOT NULL REFERENCES users(id), "
    "project_id INTEGER NOT NULL REFERENCES projects(id), "
    "item_id INTEGER NOT NULL REFERENCES entry_items(id), "
    f"verdict TEXT NOT NULL CHECK (verdict IN ({_VERDICTS_SQL_LIST})), "
    "note TEXT NOT NULL, "
    "feedback_hash TEXT UNIQUE NOT NULL, "
    "created_at TEXT NOT NULL)"
)
_OBJECT_NAMES = ("recall_feedback",)


class Duplicate(Exception):
    """This person already said exactly this about this item."""


def ensure_feedback_schema(conn: sqlite3.Connection) -> None:
    """Idempotent: the ``recall_feedback`` table (same discipline as
    ``recall.ensure_recall_schema``: a presence check first, pure DDL, and it never
    commits a transaction it did not open)."""
    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    if set(_OBJECT_NAMES) <= present:
        return
    outermost = not conn.in_transaction
    if outermost:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(_FEEDBACK_DDL)
    except BaseException:
        if outermost:
            conn.rollback()
        raise
    if outermost:
        conn.commit()


def feedback_hash(user_id: int, item_id: int, verdict: str, note: str) -> str:
    """sha256 over the four fields that make a feedback the same feedback."""
    return hashlib.sha256(
        json.dumps([user_id, item_id, verdict, note], ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def insert(conn: sqlite3.Connection, *, user_id: int, project_id: int, item_id: int,
           verdict: str, note: str, created_at: str) -> int:
    """The new row's id; :class:`Duplicate` when the same feedback exists.

    Only the UNIQUE ``feedback_hash`` is a duplicate. Any other integrity error (a CHECK,
    a foreign key) is re-raised: answering 409 for it would tell the client "already
    recorded" about a row that does not exist (the F5 rule of ``POST /api/entries``).
    """
    try:
        cur = conn.execute(
            "INSERT INTO recall_feedback (user_id, project_id, item_id, verdict, note, "
            "feedback_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, project_id, item_id, verdict, note,
             feedback_hash(user_id, item_id, verdict, note), created_at))
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        if (getattr(exc, "sqlite_errorname", "") == "SQLITE_CONSTRAINT_UNIQUE"
                and "feedback_hash" in str(exc)):
            raise Duplicate() from exc
        raise
    return cur.lastrowid


def parse_since(raw: str) -> str | None:
    """An ISO-8601 instant as the stored form (``YYYY-MM-DDTHH:MM:SSZ``, UTC), or ``None``.

    Normalized because every ``created_at`` is stored in that exact form, so the
    comparison below is a plain string comparison. A naive time is read as UTC.
    """
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        # OverflowError: an instant at the edge of datetime's range whose offset pushes it
        # past that range once shifted to UTC (0001-01-01T00:00:00+05:00).
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, AttributeError, OverflowError):
        return None


def stats(conn: sqlite3.Connection, since: str | None) -> dict:
    """Team totals since ``since`` (normalized by :func:`parse_since`), or ever.

    Nothing here is grouped by, filtered by or returned with a user: that is the rule.
    ``feedback`` counts every row; ``top_useful`` counts the PEOPLE who found an item
    useful (owner decision of 2026-09-24): the pilot measures reuse across developers,
    and one person filing the same item five times is still one person.
    """
    floor = since or ""
    recalls = dict.fromkeys(recall.SOURCES, 0)
    recalls.update(conn.execute(
        "SELECT source, count(*) FROM recall_events WHERE created_at >= ? GROUP BY source",
        (floor,)).fetchall())
    verdicts = dict.fromkeys(VERDICTS, 0)
    verdicts.update(conn.execute(
        "SELECT verdict, count(*) FROM recall_feedback WHERE created_at >= ? GROUP BY verdict",
        (floor,)).fetchall())
    top = conn.execute(
        "SELECT i.entry_id, i.ordinal, i.item_type, p.name, count(DISTINCT f.user_id) AS useful "
        "FROM recall_feedback f "
        "JOIN entry_items i ON i.id = f.item_id "
        "JOIN projects p ON p.id = f.project_id "
        "WHERE f.verdict = 'useful' AND f.created_at >= ? "
        "GROUP BY f.item_id ORDER BY useful DESC, i.entry_id, i.ordinal LIMIT ?",
        (floor, TOP_USEFUL)).fetchall()
    return {
        "since": since,
        "recalls": recalls,
        "feedback": verdicts,
        "top_useful": [{"item": f"{row[0]}.{row[1]}", "project": row[3],
                        "item_type": row[2], "useful": row[4]} for row in top],
    }
