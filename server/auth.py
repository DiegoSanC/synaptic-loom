"""Enrolment codes and device tokens (issue #20; owner decisions of 2026-09-22).

Two credentials with deliberately different lifetimes:

- an **enrolment code** travels operator → engineer, is short-lived (24 h by default),
  and is burned on its single redemption;
- a **device token** is minted in response to that redemption, exists only on the
  machine that redeemed it (one per machine), lives 90 days, and is renewed silently
  by the client while it still has life left.

Both are high-entropy random tokens stored as ``sha256`` — the same shape and the same
storage rule as ``users.token_hash`` — so no password-hashing dependency is needed.

The legacy permanent tokens minted by ``server.admin adduser`` are untouched: they are
the break-glass path, five frozen goal waves parse them, and the bearer middleware tries
them first.

The schema is ADDITIVE via :func:`ensure_auth_schema`, the ``ensure_*_schema`` idiom of
``server.search`` / ``server.links``: ``schema.sql`` never changes and an existing pilot
database gains the two tables on its next start.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

DEFAULT_ENROL_CODE_TTL = "24h"
DEFAULT_DEVICE_TOKEN_TTL_S = 90 * 24 * 3600
DEVICE_TOKEN_TTL_ENV = "LOOM_DEVICE_TOKEN_TTL_S"

_DEVICE_TOKENS_DDL = (
    "CREATE TABLE IF NOT EXISTS device_tokens ("
    "id INTEGER PRIMARY KEY, "
    "user_id INTEGER NOT NULL REFERENCES users(id), "
    "token_hash TEXT UNIQUE NOT NULL, "
    "device_name TEXT NOT NULL, "
    "issued_at TEXT NOT NULL, "
    "expires_at TEXT NOT NULL, "
    "revoked_at TEXT, "
    "replaced_by INTEGER REFERENCES device_tokens(id))"
)
_DEVICE_TOKENS_USER_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_device_tokens_user ON device_tokens(user_id)"
)
_ENROLMENT_CODES_DDL = (
    "CREATE TABLE IF NOT EXISTS enrolment_codes ("
    "id INTEGER PRIMARY KEY, "
    "user_id INTEGER NOT NULL REFERENCES users(id), "
    "code_hash TEXT UNIQUE NOT NULL, "
    "created_at TEXT NOT NULL, "
    "expires_at TEXT NOT NULL, "
    "redeemed_at TEXT, "
    "device_token_id INTEGER REFERENCES device_tokens(id))"
)
_OBJECT_NAMES = ("device_tokens", "idx_device_tokens_user", "enrolment_codes")

_TTL_RE = re.compile(r"^(\d+)([smhd])$")
_TTL_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def ensure_auth_schema(conn: sqlite3.Connection) -> None:
    """Idempotent: ``device_tokens`` (+ user index) and ``enrolment_codes``.

    Presence check first (a migrated database costs one read and no write lock), pure
    DDL with nothing to backfill, and — like ``links.ensure_links_schema`` — it never
    commits a transaction it did not open.
    """
    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    if set(_OBJECT_NAMES) <= present:
        return
    outermost = not conn.in_transaction
    if outermost:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(_DEVICE_TOKENS_DDL)
        conn.execute(_DEVICE_TOKENS_USER_INDEX_DDL)
        conn.execute(_ENROLMENT_CODES_DDL)
    except BaseException:
        if outermost:
            conn.rollback()
        raise
    if outermost:
        conn.commit()


# --------------------------------------------------------------------------- #
# Lifetimes                                                                    #
# --------------------------------------------------------------------------- #
def parse_ttl(text: str) -> int:
    """``"24h"`` → seconds. Units ``s``/``m``/``h``/``d``; anything else is an argparse error."""
    match = _TTL_RE.fullmatch(text.strip())
    if match is None or int(match.group(1)) <= 0:
        raise argparse.ArgumentTypeError(
            f"invalid TTL {text!r}: expected <n>s, <n>m, <n>h or <n>d (e.g. 24h)"
        )
    return int(match.group(1)) * _TTL_UNIT_S[match.group(2)]


def device_token_ttl_s() -> int:
    """Device token life in seconds — 90 days, or ``LOOM_DEVICE_TOKEN_TTL_S`` when set.

    Read at call time, not import time, so a test (or an operator) can shorten it without
    restarting anything. The override exists so renewal across a REAL expiry can be proven
    in seconds; a non-positive or non-numeric value falls back to the default.
    """
    raw = os.environ.get(DEVICE_TOKEN_TTL_ENV)
    try:
        value = int(raw) if raw else 0
    except ValueError:
        value = 0
    return value if value > 0 else DEFAULT_DEVICE_TOKEN_TTL_S


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _window(ttl_s: int) -> tuple[str, str]:
    """``(now, now + ttl)`` as the second-precision ISO-8601 Z strings the schema uses."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return _iso(now), _iso(now + timedelta(seconds=ttl_s))


# --------------------------------------------------------------------------- #
# Invitations                                                                  #
# --------------------------------------------------------------------------- #
def create_invitation(conn: sqlite3.Connection, user_id: int, ttl_s: int) -> tuple[str, str]:
    """Insert one enrolment code for ``user_id``; returns ``(code, expires_at)``.

    The plaintext code is returned to be printed ONCE by the admin CLI; only its hash is
    stored. The caller commits.
    """
    from server import db  # function-level: server.db imports this module

    code = secrets.token_urlsafe(32)
    created_at, expires_at = _window(ttl_s)
    conn.execute(
        "INSERT INTO enrolment_codes (user_id, code_hash, created_at, expires_at) "
        "VALUES (?, ?, ?, ?)",
        (user_id, db.hash_token(code), created_at, expires_at),
    )
    return code, expires_at


def _mint(conn: sqlite3.Connection, user_id: int, device_name: str) -> tuple[int, dict]:
    """Insert a fresh device token row; returns ``(id, response body without user)``."""
    from server import db

    token = secrets.token_urlsafe(32)
    issued_at, expires_at = _window(device_token_ttl_s())
    cur = conn.execute(
        "INSERT INTO device_tokens (user_id, token_hash, device_name, issued_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, db.hash_token(token), device_name, issued_at, expires_at),
    )
    return cur.lastrowid, {"token": token, "issued_at": issued_at, "expires_at": expires_at}


def redeem(conn: sqlite3.Connection, code: str, device_name: str) -> dict | None:
    """Burn ``code`` and mint the device token it pays for, in ONE transaction.

    Returns the enrol response body (plus ``device_token_id`` for the audit line), or
    ``None`` when the code is unknown, expired, already redeemed, or belongs to a
    deactivated user. All four are one answer on purpose: the caller is anonymous and
    must not be able to tell them apart.

    ``BEGIN IMMEDIATE`` takes the write lock before the read, so two concurrent
    redemptions of the same code serialize and exactly one sees ``redeemed_at IS NULL``.
    """
    from server import db

    now = _iso(datetime.now(timezone.utc))
    code_hash = db.hash_token(code)
    # A read first, outside any transaction: a code that cannot redeem never queues for
    # the write lock. Redemption is not throttled until the address has failed ten times
    # (D17), so a flood of bogus codes must cost a read, not a turn as SQLite's writer.
    if conn.execute(
        "SELECT 1 FROM enrolment_codes WHERE code_hash = ? AND redeemed_at IS NULL "
        "AND expires_at > ?", (code_hash, now),
    ).fetchone() is None:
        return None
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT c.id, c.user_id, u.name, u.active "
            "FROM enrolment_codes c JOIN users u ON u.id = c.user_id "
            "WHERE c.code_hash = ? AND c.redeemed_at IS NULL AND c.expires_at > ?",
            (code_hash, now),
        ).fetchone()
        if row is None or not row["active"]:
            conn.rollback()
            return None
        device_id, body = _mint(conn, row["user_id"], device_name)
        conn.execute(
            "UPDATE enrolment_codes SET redeemed_at = ?, device_token_id = ? WHERE id = ?",
            (now, device_id, row["id"]),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    body["user"] = {"id": row["user_id"], "name": row["name"]}
    body["device_token_id"] = device_id
    return body


# --------------------------------------------------------------------------- #
# Bearer lookup, renewal, revocation                                           #
# --------------------------------------------------------------------------- #
def lookup_bearer(conn: sqlite3.Connection, token: str) -> dict | None:
    """The credential behind a bearer, legacy ``users.token_hash`` first.

    Returns ``{"user_id", "name", "active", "device_id", "expires_at", "revoked_at"}``;
    ``device_id`` is ``None`` for a legacy token. Expiry and revocation are reported,
    not decided, here: the middleware owns the outcome names the audit log carries.
    """
    from server import db

    token_hash = db.hash_token(token)
    user = conn.execute(
        "SELECT id, name, active FROM users WHERE token_hash = ?", (token_hash,)
    ).fetchone()
    if user is not None:
        return {"user_id": user["id"], "name": user["name"], "active": user["active"],
                "device_id": None, "expires_at": None, "revoked_at": None}
    device = conn.execute(
        "SELECT d.id, d.user_id, d.expires_at, d.revoked_at, u.name, u.active "
        "FROM device_tokens d JOIN users u ON u.id = d.user_id WHERE d.token_hash = ?",
        (token_hash,),
    ).fetchone()
    if device is None:
        return None
    return {"user_id": device["user_id"], "name": device["name"], "active": device["active"],
            "device_id": device["id"], "expires_at": device["expires_at"],
            "revoked_at": device["revoked_at"]}


def renew(conn: sqlite3.Connection, device_id: int) -> dict:
    """Mint the successor of ``device_id`` and link it; the old token keeps its expiry.

    Leaving the predecessor valid until its own ``expires_at`` is deliberate: the client
    persists the successor AFTER receiving it, and a crash in between must cost a retry
    on the next pass, never a machine that can no longer capture. The overlap is bounded
    by the old expiry, which renewal never extends.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        old = conn.execute(
            "SELECT d.user_id, d.device_name, u.name FROM device_tokens d "
            "JOIN users u ON u.id = d.user_id WHERE d.id = ?",
            (device_id,),
        ).fetchone()
        new_id, body = _mint(conn, old["user_id"], old["device_name"])
        conn.execute("UPDATE device_tokens SET replaced_by = ? WHERE id = ?", (new_id, device_id))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    body["user"] = {"id": old["user_id"], "name": old["name"]}
    body["device_token_id"] = new_id
    return body


def revoke(conn: sqlite3.Connection, device_id: int) -> bool:
    """Mark one device token revoked; ``False`` when no live token has that id."""
    now = _iso(datetime.now(timezone.utc))
    cur = conn.execute(
        "UPDATE device_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (now, device_id),
    )
    conn.commit()
    return cur.rowcount == 1


def list_devices(conn: sqlite3.Connection, email: str | None = None) -> list[sqlite3.Row]:
    sql = (
        "SELECT d.id, u.email, d.device_name, d.issued_at, d.expires_at, d.revoked_at, "
        "d.replaced_by FROM device_tokens d JOIN users u ON u.id = d.user_id"
    )
    params: tuple = ()
    if email is not None:
        sql += " WHERE u.email = ?"
        params = (email,)
    return conn.execute(sql + " ORDER BY d.id", params).fetchall()


def device_state(row: sqlite3.Row, now: str | None = None) -> str:
    """``revoked`` / ``replaced`` / ``expired`` / ``active`` — for the admin listing."""
    now = now or _iso(datetime.now(timezone.utc))
    if row["revoked_at"] is not None:
        return "revoked"
    if row["replaced_by"] is not None:
        return "replaced"
    if row["expires_at"] <= now:
        return "expired"
    return "active"
