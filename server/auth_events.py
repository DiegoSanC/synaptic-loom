"""Account security events (D20; DESIGN §6.4).

One row per state change of an account or a machine, and per successful sign-in; a
failure stays a log line. ``actor_user_id`` is ``NULL`` when ``admin.py`` made the change.
No row ever holds a secret: the columns are ids, a fixed vocabulary and the caller's
address. That address is personal data, so rows older than
``LOOM_AUTH_EVENTS_RETENTION_DAYS`` (default 180) are deleted when the server starts.

The schema is ADDITIVE via :func:`ensure_auth_events_schema`, the ``ensure_*_schema``
idiom: ``schema.sql`` never changes and an existing database gains the table on its next
start.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

RETENTION_ENV = "LOOM_AUTH_EVENTS_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 180

# event → the target_type it names (DESIGN §6.4, "Events").
EVENTS = {
    "ui_signin": "user",
    "ui_signout": "user",
    "setup_code_issued": "user",
    "user_invited": "user",
    "user_deactivated": "user",
    "promoted": "user",
    "demoted": "user",
    "device_revoked": "device",
}

_AUTH_EVENTS_DDL = (
    "CREATE TABLE IF NOT EXISTS auth_events ("
    "id INTEGER PRIMARY KEY, "
    "ts TEXT NOT NULL, "
    "actor_user_id INTEGER REFERENCES users(id), "
    "event TEXT NOT NULL, "
    "target_type TEXT NOT NULL, "
    "target_id INTEGER NOT NULL, "
    "client_ip TEXT)"
)
_AUTH_EVENTS_TARGET_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_auth_events_target "
    "ON auth_events(target_type, target_id, ts)"
)
_OBJECT_NAMES = ("auth_events", "idx_auth_events_target")


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Cursor:
    """Run one write in the caller's transaction, or in one of its own that it commits."""
    outermost = not conn.in_transaction
    if outermost:
        conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(sql, params)
    except BaseException:
        if outermost:
            conn.rollback()
        raise
    if outermost:
        conn.commit()
    return cur


def ensure_auth_events_schema(conn: sqlite3.Connection) -> None:
    """Idempotent: ``auth_events`` + ``idx_auth_events_target``.

    Same rules as ``auth.ensure_auth_schema``: presence check first, pure DDL, and it
    never commits a transaction it did not open.
    """
    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    if set(_OBJECT_NAMES) <= present:
        return
    outermost = not conn.in_transaction
    if outermost:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(_AUTH_EVENTS_DDL)
        conn.execute(_AUTH_EVENTS_TARGET_INDEX_DDL)
    except BaseException:
        if outermost:
            conn.rollback()
        raise
    if outermost:
        conn.commit()


def record(conn: sqlite3.Connection, *, actor_user_id: int | None, event: str,
           target_type: str, target_id: int, client_ip: str | None = None) -> int:
    """Insert one event row; returns its id.

    ``event`` must be one of :data:`EVENTS` and ``target_type`` the one it names, or
    ``ValueError`` is raised before anything is written. Called inside a transaction it
    joins it, so the event commits or rolls back with the change it records; it never
    commits a transaction it did not open.
    """
    if EVENTS.get(event) != target_type:
        raise ValueError(f"not an auth event: {event!r} on {target_type!r}")
    return _write(
        conn,
        "INSERT INTO auth_events (ts, actor_user_id, event, target_type, target_id, client_ip) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (_iso(datetime.now(timezone.utc)), actor_user_id, event, target_type, target_id,
         client_ip),
    ).lastrowid


def retention_days() -> int:
    """``LOOM_AUTH_EVENTS_RETENTION_DAYS``, read at call time; a non-positive or
    non-numeric value falls back to the default (``auth.device_token_ttl_s``'s rule), and
    :func:`prune` reads one too large to be a date as the default too."""
    raw = os.environ.get(RETENTION_ENV)
    try:
        value = int(raw) if raw else 0
    except ValueError:
        value = 0
    return value if value > 0 else DEFAULT_RETENTION_DAYS


def prune(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """Delete the rows older than the retention; returns how many.

    A retention too large to be a date is read as the default (§6.4). The cutoff's year
    is zero-padded by hand: ``%Y`` leaves a year before 1000 unpadded on glibc, and
    ``"5-01-01…"`` sorts after every real timestamp, so every row would go.
    """
    now = now or datetime.now(timezone.utc)
    try:
        cutoff = now - timedelta(days=retention_days())
    except OverflowError:
        cutoff = now - timedelta(days=DEFAULT_RETENTION_DAYS)
    cutoff = f"{cutoff.year:04d}{cutoff.strftime('-%m-%dT%H:%M:%SZ')}"
    return _write(conn, "DELETE FROM auth_events WHERE ts < ?", (cutoff,)).rowcount


def about_user(conn: sqlite3.Connection, user_id: int, limit: int = 20) -> list[dict]:
    """The ``limit`` newest events about ``user_id`` (``GET /api/me``, DESIGN §6.4).

    "About" means aimed at the user, or at any of their device tokens (every chain,
    revoked or not). Newest first, the later row first within one second. ``actor`` is
    the actor's name, ``None`` for ``admin.py``. ``client_ip`` is never returned.
    """
    rows = conn.execute(
        "SELECT e.ts, e.event, a.name AS actor, e.target_type, e.target_id "
        "FROM auth_events e LEFT JOIN users a ON a.id = e.actor_user_id "
        "WHERE (e.target_type = 'user' AND e.target_id = ?) "
        "OR (e.target_type = 'device' AND e.target_id IN "
        "(SELECT id FROM device_tokens WHERE user_id = ?)) "
        "ORDER BY e.ts DESC, e.id DESC LIMIT ?",
        (user_id, user_id, limit),
    ).fetchall()
    return [{"ts": r[0], "event": r[1], "actor": r[2], "target_type": r[3],
             "target_id": r[4]} for r in rows]
