"""Browser login for the read-only UI (D14; plan 2026-09-29, slice 1).

Two credentials, the shape of ``server.auth`` applied to a browser:

- a **login code** is minted for a caller that already holds a bearer (``loom ui``), lives
  120 s and is burned on its single redemption — the only secret that reaches the browser;
- a **UI session** is minted in response to that redemption; the browser keeps the
  plaintext id in the ``loom_ui`` cookie, the server only its ``sha256``. It lives 12 h and
  never outlives the device token that asked for it.

Both are checked against the user's ``active`` flag and the originating device's
``revoked_at`` on every use, so ``admin.py deactivate`` and ``revokedevice`` end a browser
session without any UI-specific revocation surface. A session also ends on purpose
(D20): :func:`end_sessions` deletes rows, and sign-out is its first caller.

A session that writes (D20, DESIGN §6.4) proves it with :func:`csrf_token`, derived from
the cookie value and never stored.

The schema is ADDITIVE via :func:`ensure_ui_schema`, the ``ensure_*_schema`` idiom:
``schema.sql`` never changes and an existing database gains the tables on its next start.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

SESSION_COOKIE = "loom_ui"
DEFAULT_LOGIN_CODE_TTL_S = 120
DEFAULT_SESSION_TTL_S = 12 * 3600
LOGIN_CODE_TTL_ENV = "LOOM_UI_LOGIN_CODE_TTL_S"
SESSION_TTL_ENV = "LOOM_UI_SESSION_TTL_S"

_LOGIN_CODES_DDL = (
    "CREATE TABLE IF NOT EXISTS ui_login_codes ("
    "id INTEGER PRIMARY KEY, "
    "user_id INTEGER NOT NULL REFERENCES users(id), "
    "device_token_id INTEGER REFERENCES device_tokens(id), "
    "code_hash TEXT UNIQUE NOT NULL, "
    "created_at TEXT NOT NULL, "
    "expires_at TEXT NOT NULL, "
    "redeemed_at TEXT)"
)
_SESSIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS ui_sessions ("
    "id INTEGER PRIMARY KEY, "
    "user_id INTEGER NOT NULL REFERENCES users(id), "
    "device_token_id INTEGER REFERENCES device_tokens(id), "
    "session_hash TEXT UNIQUE NOT NULL, "
    "created_at TEXT NOT NULL, "
    "expires_at TEXT NOT NULL)"
)
_SESSIONS_DEVICE_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_ui_sessions_device ON ui_sessions(device_token_id)"
)
# D20 (DESIGN §6.4): `cli`, a session `loom ui` opened — the only kind that writes in 2a.
SESSIONS_KIND_DDL = "TEXT NOT NULL DEFAULT 'cli'"
_OBJECT_NAMES = ("ui_login_codes", "ui_sessions", "idx_ui_sessions_device")


def ensure_ui_schema(conn: sqlite3.Connection) -> None:
    """Idempotent: ``ui_login_codes``, ``ui_sessions`` (+ device index) and
    ``ui_sessions.kind`` (D20, through ``db.ensure_column``).

    Same rules as ``auth.ensure_auth_schema``: presence check first, pure DDL, and it
    never commits a transaction it did not open.
    """
    from server import db  # function-level: server.db imports this module

    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    if not set(_OBJECT_NAMES) <= present:
        outermost = not conn.in_transaction
        if outermost:
            conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(_LOGIN_CODES_DDL)
            conn.execute(_SESSIONS_DDL)
            conn.execute(_SESSIONS_DEVICE_INDEX_DDL)
        except BaseException:
            if outermost:
                conn.rollback()
            raise
        if outermost:
            conn.commit()
    db.ensure_column(conn, "ui_sessions", "kind", SESSIONS_KIND_DDL)


def _ttl_from_env(name: str, default: int) -> int:
    """Read at call time so a test can prove a REAL expiry in seconds (``auth`` idiom)."""
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw else 0
    except ValueError:
        value = 0
    return value if value > 0 else default


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def issue_login_code(conn: sqlite3.Connection, user_id: int,
                     device_token_id: int | None) -> tuple[str, str]:
    """Insert and commit one login code; returns ``(code, expires_at)``.

    The plaintext is returned once, to the bearer that asked; only its hash is stored.
    """
    from server import db  # function-level: server.db imports this module

    code = secrets.token_urlsafe(32)
    now = _now()
    expires_at = _iso(now + timedelta(seconds=_ttl_from_env(
        LOGIN_CODE_TTL_ENV, DEFAULT_LOGIN_CODE_TTL_S)))
    conn.execute(
        "INSERT INTO ui_login_codes (user_id, device_token_id, code_hash, created_at, "
        "expires_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, device_token_id, db.hash_token(code), _iso(now), expires_at),
    )
    conn.commit()
    return code, expires_at


def open_session(conn: sqlite3.Connection, code: str, *,
                 client_ip: str | None = None) -> dict | None:
    """Burn ``code`` and mint the UI session it pays for, in ONE transaction, which also
    records ``ui_signin`` (D20, §6.4) from ``client_ip``.

    Returns ``{"session", "max_age", "user_id", "device_token_id", "ui_session_id"}``, or
    ``None`` when the code is unknown, expired, already redeemed, belongs to a deactivated
    user, or was issued to a device that is now revoked or expired. One answer for all of
    them: the caller is anonymous. A refused code is NOT burned.

    ``BEGIN IMMEDIATE`` takes the write lock before the read, so two concurrent
    redemptions serialize and exactly one sees ``redeemed_at IS NULL`` (``auth.redeem``).
    """
    from server import auth_events, db

    now = _now()
    now_iso = _iso(now)
    code_hash = db.hash_token(code)
    # Read before locking, as `auth.redeem` does (D17): a bogus code never waits for the
    # writer.
    if conn.execute(
        "SELECT 1 FROM ui_login_codes WHERE code_hash = ? AND redeemed_at IS NULL "
        "AND expires_at > ?", (code_hash, now_iso),
    ).fetchone() is None:
        return None
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT c.id, c.user_id, c.device_token_id, u.active, "
            "d.expires_at AS device_expires_at, d.revoked_at AS device_revoked_at "
            "FROM ui_login_codes c JOIN users u ON u.id = c.user_id "
            "LEFT JOIN device_tokens d ON d.id = c.device_token_id "
            "WHERE c.code_hash = ? AND c.redeemed_at IS NULL AND c.expires_at > ?",
            (code_hash, now_iso),
        ).fetchone()
        device_dead = row is not None and row["device_token_id"] is not None and (
            row["device_revoked_at"] is not None or row["device_expires_at"] <= now_iso)
        if row is None or not row["active"] or device_dead:
            conn.rollback()
            return None
        expires = now + timedelta(seconds=_ttl_from_env(SESSION_TTL_ENV, DEFAULT_SESSION_TTL_S))
        if row["device_token_id"] is not None:
            expires = min(expires, _parse(row["device_expires_at"]))
        session = secrets.token_urlsafe(32)
        cur = conn.execute(
            "INSERT INTO ui_sessions (user_id, device_token_id, session_hash, created_at, "
            "expires_at) VALUES (?, ?, ?, ?, ?)",
            (row["user_id"], row["device_token_id"], db.hash_token(session), now_iso,
             _iso(expires)),
        )
        conn.execute("UPDATE ui_login_codes SET redeemed_at = ? WHERE id = ?",
                     (now_iso, row["id"]))
        auth_events.record(conn, actor_user_id=row["user_id"], event="ui_signin",
                           target_type="user", target_id=row["user_id"], client_ip=client_ip)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {"session": session, "max_age": int((expires - now).total_seconds()),
            "user_id": row["user_id"], "device_token_id": row["device_token_id"],
            "ui_session_id": cur.lastrowid}


_SESSION_SELECT = (
    "SELECT s.id, s.user_id, s.device_token_id, s.expires_at, s.kind, u.name, u.active, "
    "u.role, d.revoked_at AS device_revoked_at "
    "FROM ui_sessions s JOIN users u ON u.id = s.user_id "
    "LEFT JOIN device_tokens d ON d.id = s.device_token_id "
)


def _session_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {"ui_session_id": row["id"], "user_id": row["user_id"], "name": row["name"],
            "active": row["active"], "expires_at": row["expires_at"],
            "device_revoked_at": row["device_revoked_at"], "kind": row["kind"],
            "role": row["role"], "device_token_id": row["device_token_id"]}


def lookup_session(conn: sqlite3.Connection, session: str) -> dict | None:
    """The session behind a cookie value, or ``None`` when no row has its hash.

    Returns ``{"ui_session_id", "user_id", "name", "active", "expires_at",
    "device_revoked_at", "kind", "role", "device_token_id"}``; ``role`` is read from
    ``users`` on every call, never cached in the session (D20). Like ``auth.lookup_bearer``,
    it reports and :func:`session_refusal` decides.
    """
    from server import db

    return _session_dict(conn.execute(_SESSION_SELECT + "WHERE s.session_hash = ?",
                                      (db.hash_token(session),)).fetchone())


def reread_session(conn: sqlite3.Connection, ui_session_id: int) -> dict | None:
    """:func:`lookup_session` by row id: a write reads its session again under its own
    write lock (D20, §6.4), after the gate let it in."""
    return _session_dict(conn.execute(_SESSION_SELECT + "WHERE s.id = ?",
                                      (ui_session_id,)).fetchone())


def session_refusal(session: dict | None, now: str) -> str | None:
    """Why ``session`` (from :func:`lookup_session`) opens nothing, as the audit outcome
    the session gate logs, or ``None`` when it is live.

    ``active`` before revocation (D19): ``deactivate`` also revokes the user's machines,
    and the outcome must still name the deactivation.
    """
    if session is None:
        return "ui_invalid_session"
    if session["expires_at"] <= now:
        return "ui_expired_session"
    if not session["active"]:
        return "ui_deactivated_user"
    if session["device_revoked_at"] is not None:
        return "ui_revoked_device"
    return None


# The session kinds that may write (D20, §6.4): `cli` is the one `loom ui` opens; 2b adds
# `password`.
WRITABLE_KINDS = frozenset({"cli"})


def csrf_token(session: str) -> str:
    """The CSRF token of the session behind cookie value ``session`` (D20, §6.4).

    Lowercase hex of ``HMAC-SHA256(key = the cookie value, msg = "csrf")``: derived, never
    stored, and worth nothing without the cookie it was derived from.
    """
    return hmac.new(session.encode(), b"csrf", hashlib.sha256).hexdigest()


def end_sessions(conn: sqlite3.Connection, *, session_id: int | None = None,
                 user_id: int | None = None,
                 device_token_ids: list[int] | None = None) -> int:
    """Delete UI sessions, selected by exactly one of: one session id, every session of
    one user, or every session opened by one of ``device_token_ids``. Returns how many.

    Never commits a transaction it did not open (``ensure_ui_schema``'s rule), so a
    revocation can call it inside its own ``BEGIN IMMEDIATE``.
    """
    selectors = [s for s in (session_id, user_id, device_token_ids) if s is not None]
    if len(selectors) != 1:
        raise ValueError("end_sessions takes exactly one of session_id, user_id, "
                         "device_token_ids")
    if session_id is not None:
        where, params = "id = ?", [session_id]
    elif user_id is not None:
        where, params = "user_id = ?", [user_id]
    else:
        params = list(device_token_ids)
        if not params:
            return 0
        where = f"device_token_id IN ({', '.join('?' * len(params))})"
    outermost = not conn.in_transaction
    try:
        deleted = conn.execute(f"DELETE FROM ui_sessions WHERE {where}", params).rowcount
    except BaseException:
        if outermost:
            conn.rollback()
        raise
    if outermost:
        conn.commit()
    return deleted
