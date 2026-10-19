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
DEFAULT_SETUP_CODE_TTL_S = 600
SETUP_CODE_TTL_ENV = "LOOM_SETUP_CODE_TTL_S"

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
# Renewal chains are walked backwards (who points at me?) by `revoke` and the orphan count.
_DEVICE_TOKENS_REPLACED_BY_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_device_tokens_replaced_by ON device_tokens(replaced_by)"
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
# D20 (DESIGN §6.4): `admin` or `member`, validated in code, never by a CHECK.
ROLES = ("admin", "member")
USERS_ROLE_DDL = "TEXT NOT NULL DEFAULT 'member'"
# D20 setup codes and the issuer cascade (DESIGN §6.4).
ENROLMENT_CODE_COLUMNS = (
    ("kind", "TEXT NOT NULL DEFAULT 'invite'"),
    ("created_by_user_id", "INTEGER REFERENCES users(id)"),
    ("issued_by_device_token_id", "INTEGER REFERENCES device_tokens(id)"),
)
_ENROLMENT_CODES_ISSUER_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_enrolment_codes_issuer "
    "ON enrolment_codes(issued_by_device_token_id)"
)
_OBJECT_NAMES = ("device_tokens", "idx_device_tokens_user", "idx_device_tokens_replaced_by",
                 "enrolment_codes")

_TTL_RE = re.compile(r"^(\d+)([smhd])$")
_TTL_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def ensure_auth_schema(conn: sqlite3.Connection) -> None:
    """Idempotent: ``device_tokens`` (+ user and ``replaced_by`` indexes), ``enrolment_codes``,
    and D20's ``users.role`` and ``enrolment_codes`` columns (through ``db.ensure_column``)
    with the issuer index.

    Presence check first (a migrated database costs one read and no write lock), pure
    DDL with nothing to backfill, and — like ``links.ensure_links_schema`` — it never
    commits a transaction it did not open.
    """
    from server import db  # function-level: server.db imports this module

    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    if not set(_OBJECT_NAMES) <= present:
        outermost = not conn.in_transaction
        if outermost:
            conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(_DEVICE_TOKENS_DDL)
            conn.execute(_DEVICE_TOKENS_USER_INDEX_DDL)
            conn.execute(_DEVICE_TOKENS_REPLACED_BY_INDEX_DDL)
            conn.execute(_ENROLMENT_CODES_DDL)
        except BaseException:
            if outermost:
                conn.rollback()
            raise
        if outermost:
            conn.commit()
    # D20: `users` is schema.sql's, so its migration lives with the accounts module.
    db.ensure_column(conn, "users", "role", USERS_ROLE_DDL)
    for column, definition in ENROLMENT_CODE_COLUMNS:
        db.ensure_column(conn, "enrolment_codes", column, definition)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'idx_enrolment_codes_issuer'"
                    ).fetchone() is None:
        conn.execute(_ENROLMENT_CODES_ISSUER_INDEX_DDL)


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
    return _ttl_from_env(DEVICE_TOKEN_TTL_ENV, DEFAULT_DEVICE_TOKEN_TTL_S)


def setup_code_ttl_s() -> int:
    """Setup code life in seconds (D20, DESIGN §6.4) — 10 minutes, or
    ``LOOM_SETUP_CODE_TTL_S`` when set; read at call time, with
    :func:`device_token_ttl_s`'s fallback rule."""
    return _ttl_from_env(SETUP_CODE_TTL_ENV, DEFAULT_SETUP_CODE_TTL_S)


def _ttl_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw else 0
    except ValueError:
        value = 0
    return value if value > 0 else default


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _window(ttl_s: int) -> tuple[str, str]:
    """``(now, now + ttl)`` as the second-precision ISO-8601 Z strings the schema uses."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return _iso(now), _iso(now + timedelta(seconds=ttl_s))


# --------------------------------------------------------------------------- #
# Invitations                                                                  #
# --------------------------------------------------------------------------- #
ENROLMENT_CODE_KINDS = ("invite", "setup")


def create_invitation(conn: sqlite3.Connection, user_id: int, ttl_s: int, *,
                      kind: str = "invite", created_by_user_id: int | None = None,
                      issued_by_device_token_id: int | None = None) -> tuple[str, str]:
    """Insert one enrolment code for ``user_id``; returns ``(code, expires_at)``.

    ``kind`` is ``invite`` or ``setup`` (D20); a code issued from the web names who issued it
    and the device token of the issuing session, which the issuer cascade follows.

    The plaintext code is returned to be printed ONCE by the admin CLI; only its hash is
    stored. The caller commits.
    """
    from server import db  # function-level: server.db imports this module

    if kind not in ENROLMENT_CODE_KINDS:
        raise ValueError(f"unknown enrolment code kind: {kind!r}")
    code = secrets.token_urlsafe(32)
    created_at, expires_at = _window(ttl_s)
    conn.execute(
        "INSERT INTO enrolment_codes (user_id, code_hash, created_at, expires_at, kind, "
        "created_by_user_id, issued_by_device_token_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, db.hash_token(code), created_at, expires_at, kind, created_by_user_id,
         issued_by_device_token_id),
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


def redeem(conn: sqlite3.Connection, code: str, device_name: str,
           old_token: str | None = None) -> dict | None:
    """Burn ``code`` and mint the device token it pays for, in ONE transaction.

    Returns the enrol response body (plus ``device_token_id`` for the audit line), or
    ``None`` when the code is unknown, expired, already redeemed, or belongs to a
    deactivated user. All four are one answer on purpose: the caller is anonymous and
    must not be able to tell them apart.

    ``old_token`` is the bearer a re-enrolling machine sent (D19 B2): when it is a live
    device token of the code's own user, its whole chain is revoked in the same
    transaction. Anything else — unknown, revoked, expired, another user's, a legacy
    ``users.token_hash`` — changes nothing and is not reported.

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
        if old_token:
            old = conn.execute(
                "SELECT id FROM device_tokens WHERE token_hash = ? AND user_id = ? "
                "AND revoked_at IS NULL AND expires_at > ?",
                (db.hash_token(old_token), row["user_id"], now),
            ).fetchone()
            if old is not None:
                revoke_chain(conn, old["id"], now)
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


class RenewRefused(Exception):
    """``renew`` found the caller dead inside its transaction (a revoke or a deactivation
    committed after the bearer middleware accepted it). ``outcome`` is the middleware's
    own name for that state, so the audit log reads the same either way."""

    def __init__(self, outcome: str):
        super().__init__(outcome)
        self.outcome = outcome


# The connected component of ``?`` over ``replaced_by``, followed BOTH ways from every row
# found, not two one-way walks from the start: once renew never forks, that component is
# the whole history of one machine. A subquery, so the UPDATE around it reports rowcount.
_CHAIN_IDS = (
    "WITH RECURSIVE chain(id) AS ("
    " SELECT ?"
    " UNION SELECT d.id FROM device_tokens d JOIN chain c ON d.replaced_by = c.id"
    " UNION SELECT d.replaced_by FROM device_tokens d JOIN chain c ON d.id = c.id"
    " WHERE d.replaced_by IS NOT NULL) "
    "SELECT id FROM chain"
)


def renew(conn: sqlite3.Connection, device_id: int) -> dict:
    """Mint the successor of ``device_id`` and link it; the old token keeps its expiry.

    Leaving the predecessor valid until its own ``expires_at`` is deliberate: the client
    persists the successor AFTER receiving it, and a crash in between must cost a retry
    on the next pass, never a machine that can no longer capture. The overlap is bounded
    by the old expiry, which renewal never extends.

    Renewal re-issues and never forks (D19). A caller that already has a successor (the
    retry after a lost 201, or a copied ``config.json``) gets every token AFTER it revoked;
    the last of those — the one with no successor — points at the new token, and every
    other pointer stays put, so the chain is one line and a walk from any id reaches the
    live token. The caller is re-checked under the write lock and :class:`RenewRefused`
    is raised when a revoke, an expiry or a deactivation got there first.
    """
    now = _iso(datetime.now(timezone.utc))
    conn.execute("BEGIN IMMEDIATE")
    try:
        old = conn.execute(
            "SELECT d.user_id, d.device_name, d.expires_at, d.revoked_at, d.replaced_by, "
            "u.name, u.active FROM device_tokens d "
            "JOIN users u ON u.id = d.user_id WHERE d.id = ?",
            (device_id,),
        ).fetchone()
        # The middleware's order (D19 B3): a deactivated user is named as such first.
        if not old["active"]:
            raise RenewRefused("deactivated_user")
        if old["revoked_at"] is not None:
            raise RenewRefused("revoked_token")
        if old["expires_at"] <= now:
            raise RenewRefused("expired_token")
        tail = device_id
        if old["replaced_by"] is not None:
            after = conn.execute(
                "WITH RECURSIVE after(id) AS ("
                " SELECT ? UNION SELECT d.replaced_by FROM device_tokens d"
                " JOIN after a ON d.id = a.id WHERE d.replaced_by IS NOT NULL) "
                "SELECT d.id, d.replaced_by FROM device_tokens d JOIN after a ON d.id = a.id",
                (old["replaced_by"],),
            ).fetchall()
            conn.executemany(
                "UPDATE device_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                [(now, row["id"]) for row in after],
            )
            tail = next(row["id"] for row in after if row["replaced_by"] is None)
        new_id, body = _mint(conn, old["user_id"], old["device_name"])
        conn.execute("UPDATE device_tokens SET replaced_by = ? WHERE id = ?", (new_id, tail))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    body["user"] = {"id": old["user_id"], "name": old["name"]}
    body["device_token_id"] = new_id
    return body


def revoke_chain(conn: sqlite3.Connection, device_id: int, now: str) -> int:
    """Revoke every live token of ``device_id``'s chain, with the issuer cascade; returns
    how many tokens were revoked, the cascade's included.

    Joins the caller's transaction: it neither begins nor commits one.
    """
    return revoke_with_cascade(conn, [device_id], now)[0]


def revoke_with_cascade(conn: sqlite3.Connection, device_ids: list[int],
                        now: str) -> tuple[int, int]:
    """Revoke the chains of ``device_ids`` and, transitively, every chain minted from a
    setup code one of their tokens issued (D20, DESIGN §6.4 "Issuer cascade"); expire
    every unredeemed code, of any kind, those tokens issued. Returns ``(tokens revoked,
    codes expired)``.

    A web invitation is not followed even once redeemed: its chain is the invitee's own
    enrolment, not a machine the issuer added. The walk follows every id of a chain, live
    or not, and visits each id once, so it reaches a fixpoint on any data. Joins the
    caller's transaction: it neither begins nor commits one.
    """
    seen: set[int] = set()
    frontier = set(device_ids)
    revoked = expired = 0
    while frontier:
        chain: set[int] = set()
        for start in frontier - seen:
            chain.update(row[0] for row in conn.execute(_CHAIN_IDS, (start,)))
        chain -= seen
        if not chain:
            break
        seen |= chain
        ids = sorted(chain)
        marks = ", ".join("?" * len(ids))
        revoked += conn.execute(
            f"UPDATE device_tokens SET revoked_at = ? WHERE revoked_at IS NULL AND id IN ({marks})",
            (now, *ids),
        ).rowcount
        expired += conn.execute(
            "UPDATE enrolment_codes SET expires_at = ? WHERE redeemed_at IS NULL "
            f"AND expires_at > ? AND issued_by_device_token_id IN ({marks})",
            (now, now, *ids),
        ).rowcount
        frontier = {row[0] for row in conn.execute(
            "SELECT device_token_id FROM enrolment_codes WHERE kind = 'setup' "
            "AND redeemed_at IS NOT NULL AND device_token_id IS NOT NULL "
            f"AND issued_by_device_token_id IN ({marks})", ids)}
    return revoked, expired


def revoke(conn: sqlite3.Connection, device_id: int) -> int:
    """Revoke the whole chain ``device_id`` belongs to (any id of it), with the issuer
    cascade; returns how many tokens were still unrevoked — ``0`` when the id is unknown
    and nothing the cascade reaches is live."""
    now = _iso(datetime.now(timezone.utc))
    conn.execute("BEGIN IMMEDIATE")
    try:
        count = revoke_chain(conn, device_id, now)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return count


# Pre-D19 ``renew`` overwrote ``replaced_by``, so renewing one token twice left the first
# successor pointing nowhere. An orphan root is a token nothing points at — no
# ``replaced_by`` and no enrolment code — and its branch is the line forward from it
# (nothing points back in, so that line is its whole connected component). Every LIVE
# token of such a branch is one no chain from an enrolment reaches.
_ORPHANS = (
    "WITH RECURSIVE branch(id, root) AS ("
    " SELECT d.id, d.id FROM device_tokens d"
    " WHERE NOT EXISTS (SELECT 1 FROM device_tokens p WHERE p.replaced_by = d.id)"
    " AND NOT EXISTS (SELECT 1 FROM enrolment_codes c WHERE c.device_token_id = d.id)"
    " UNION SELECT d.replaced_by, b.root FROM device_tokens d JOIN branch b ON d.id = b.id"
    " WHERE d.replaced_by IS NOT NULL) "
    "SELECT b.root, d.id FROM branch b JOIN device_tokens d ON d.id = b.id "
    "WHERE d.revoked_at IS NULL AND d.expires_at > ? ORDER BY b.root, d.id"
)


def orphans(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    """``(orphan root, live token id)`` for every live token of an orphaned branch."""
    now = _iso(datetime.now(timezone.utc))
    return [(row["root"], row["id"]) for row in conn.execute(_ORPHANS, (now,))]


def revoke_orphans(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    """Revoke every token :func:`orphans` lists, in one transaction; returns them."""
    now = _iso(datetime.now(timezone.utc))
    conn.execute("BEGIN IMMEDIATE")
    try:
        found = [(row["root"], row["id"]) for row in conn.execute(_ORPHANS, (now,))]
        conn.executemany(
            "UPDATE device_tokens SET revoked_at = ? WHERE id = ?", [(now, i) for _, i in found]
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return found


class LastAdmin(Exception):
    """The change would leave no active admin (D20; DESIGN §6.4's ``409 last_admin``)."""


def refuse_last_admin(conn: sqlite3.Connection, user_id: int) -> None:
    """Raise :class:`LastAdmin` when ``user_id`` is the only active admin.

    For a demotion or a deactivation, called inside its ``BEGIN IMMEDIATE``: the count is
    read under the write lock, so two concurrent removals of the last two admins
    serialize and the second one sees the first.
    """
    if conn.execute(
        "SELECT 1 FROM users WHERE id = ? AND role = 'admin' AND active = 1 "
        "AND (SELECT count(*) FROM users WHERE role = 'admin' AND active = 1) = 1",
        (user_id,),
    ).fetchone() is not None:
        raise LastAdmin()


def deactivate(conn: sqlite3.Connection, email: str) -> tuple[int, int] | None:
    """Deactivate the user, revoke every one of their chains with the issuer cascade (D20)
    and expire their pending enrolment codes, in one transaction (D19 B3). Returns
    ``(tokens revoked, codes expired)``, the cascade's included, or ``None`` when no user
    has that email.

    Every token of a chain belongs to one user (renew copies ``user_id``), so the user's
    tokens ARE the user's chains. UI sessions are left alone: the ``active`` join already
    ends them, and re-activating the row by SQL would bring no machine back.

    Raises :class:`LastAdmin`, with nothing changed, for the last active admin (D20).
    """
    now = _iso(datetime.now(timezone.utc))
    conn.execute("BEGIN IMMEDIATE")
    try:
        user = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if user is None:
            conn.rollback()
            return None
        refuse_last_admin(conn, user["id"])
        conn.execute("UPDATE users SET active = 0 WHERE id = ?", (user["id"],))
        expired = conn.execute(
            "UPDATE enrolment_codes SET expires_at = ? "
            "WHERE user_id = ? AND redeemed_at IS NULL AND expires_at > ?",
            (now, user["id"], now),
        ).rowcount
        own = [row[0] for row in conn.execute(
            "SELECT id FROM device_tokens WHERE user_id = ?", (user["id"],))]
        revoked, cascaded = revoke_with_cascade(conn, own, now)
        expired += cascaded
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return revoked, expired


def list_devices(conn: sqlite3.Connection, email: str | None = None) -> list[sqlite3.Row]:
    """One row per token; ``chain`` is the id of the token its chain started from."""
    sql = (
        "WITH RECURSIVE line(id, root) AS ("
        " SELECT id, id FROM device_tokens WHERE id NOT IN"
        " (SELECT replaced_by FROM device_tokens WHERE replaced_by IS NOT NULL)"
        " UNION SELECT d.replaced_by, l.root FROM device_tokens d JOIN line l ON d.id = l.id"
        " WHERE d.replaced_by IS NOT NULL) "
        "SELECT d.id, u.email, d.device_name, d.issued_at, d.expires_at, d.revoked_at, "
        "d.replaced_by, l.root AS chain FROM device_tokens d JOIN users u ON u.id = d.user_id "
        "JOIN line l ON l.id = d.id"
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
