"""Writes that race a revocation (D20, DESIGN §6.4 as amended on 2026-10-19).

§6.4: role and `active` are read "again inside each write's `BEGIN IMMEDIATE`: if the
session, its device token, the user's `active` or the role a route needs changed after the
gate let the request in, the write is refused as the gate would refuse it and nothing
changes (a body held back while the machine is revoked buys nothing)". And "Issuer
cascade": redeeming "re-checks its issuer under the write lock: a code whose
`issued_by_device_token_id` is revoked is refused like an expired one (`401
invalid_code`)".

The gate reads the session before it reads the body, so a client that holds its body back
can let a revocation commit in between. These tests drive the app as an ASGI server would,
with a `receive` that commits the change before it hands over the body, and assert that
every write route then refuses and that the database is exactly as the change left it.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import threading
from datetime import datetime, timedelta, timezone
from itertools import count
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from server import auth, db

PUBLIC_HOST = "loom-test.fly.dev"                 # https://$FLY_APP_NAME.fly.dev (D19)
PUBLIC_ORIGIN = f"https://{PUBLIC_HOST}"
TABLES = ("users", "device_tokens", "enrolment_codes", "ui_sessions", "auth_events")
_IPS = count(1)   # each request its own failure window (D17): a refusal here is counted


def _sql(env, query: str, params: tuple = ()) -> list[dict]:
    conn = db.connect(env)
    try:
        return [dict(row) for row in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def _write(env, query: str, params: tuple = ()) -> None:
    conn = db.connect(env)
    try:
        conn.execute(query, params)
        conn.commit()
    finally:
        conn.close()


def _snapshot(env) -> dict[str, list[dict]]:
    return {table: _sql(env, f"SELECT * FROM {table} ORDER BY rowid") for table in TABLES}


def _machine(env, user_id: int, device_name: str) -> dict:
    """A device token minted the way `loom enroll` mints one."""
    conn = db.connect(env)
    try:
        code, _ = auth.create_invitation(conn, user_id, 3600)
        conn.commit()
        body = auth.redeem(conn, code, device_name)
    finally:
        conn.close()
    return {"id": body["device_token_id"], "token": body["token"]}


def _sign_in(token: str) -> str:
    """`loom ui`'s flow; returns the session cookie value."""
    from server.app import app

    browser = TestClient(app, base_url="https://testserver")
    code = browser.post("/api/ui-login",
                        headers={"Authorization": f"Bearer {token}"}).json()["code"]
    assert browser.post("/ui/session", json={"code": code}).status_code == 204
    return browser.cookies["loom_ui"]


def _post_racing(path: str, cookie: str, fields: dict, change=None) -> tuple[int, str]:
    """POST ``fields`` (plus the session's CSRF token) to ``path`` straight through the
    ASGI app, from the server's own origin. ``change`` runs, and commits, after the gate
    has read the session and before the app receives the body."""
    from server.app import app

    csrf = hmac.new(cookie.encode(), b"csrf", hashlib.sha256).hexdigest()
    body = urlencode({**fields, "csrf": csrf}).encode()
    sent = asyncio.Event()
    ip = f"203.0.113.{next(_IPS) % 250 + 1}".encode()

    async def receive():
        if not sent.is_set():
            sent.set()
            if change is not None:
                change()
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(3600)          # the client stays connected
        return {"type": "http.disconnect"}

    out = {"status": None, "body": b""}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "https", "path": path, "raw_path": path.encode(),
        "root_path": "", "query_string": b"", "client": (ip.decode(), 4000),
        "server": (PUBLIC_HOST, 443),
        "headers": [(b"host", PUBLIC_HOST.encode()), (b"origin", PUBLIC_ORIGIN.encode()),
                    (b"fly-client-ip", ip),
                    (b"content-type", b"application/x-www-form-urlencoded"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cookie", f"loom_ui={cookie}".encode())],
    }
    asyncio.run(app(scope, receive, send))
    return out["status"], out["body"].decode("utf-8", "replace")


@pytest.fixture
def team(env, user_factory):
    """Two admins (so neither is the last one) and a member with two machines; the first
    admin is signed in from an enrolled machine."""
    boss, deputy, staff = (user_factory(name) for name in ("boss", "deputy", "staff"))
    _write(env, "UPDATE users SET role = 'admin' WHERE id IN (?, ?)", (boss["id"], deputy["id"]))
    boss["device"] = _machine(env, boss["id"], "boss-laptop")
    boss["cookie"] = _sign_in(boss["device"]["token"])
    staff["laptop"] = _machine(env, staff["id"], "staff-laptop")
    staff["desktop"] = _machine(env, staff["id"], "staff-desktop")
    return {"boss": boss, "deputy": deputy, "staff": staff}


# Every write route of §6.4, as the boss's session posts it.
def _routes(team) -> dict[str, tuple[str, dict]]:
    staff = team["staff"]
    return {
        "setup-code": ("/ui/machines/setup-code", {}),
        "revoke": (f"/ui/devices/{staff['desktop']['id']}/revoke", {}),
        "invite": ("/ui/admin/users/invite",
                   {"email": "new@example.com", "name": "newcomer", "role": "admin"}),
        "deactivate": (f"/ui/admin/users/{staff['id']}/deactivate", {}),
        "role": (f"/ui/admin/users/{staff['id']}/role", {"role": "admin"}),
        "signout": ("/ui/signout", {}),
    }


ROUTES = ("setup-code", "revoke", "invite", "deactivate", "role", "signout")
ACCEPTED = {"setup-code": 200, "revoke": 303, "invite": 200, "deactivate": 303,
            "role": 303, "signout": 200}


def _revoke_machine(env, team):
    conn = db.connect(env)
    try:
        assert auth.revoke(conn, team["boss"]["device"]["id"]) == 1
    finally:
        conn.close()


def _deactivate_user(env, team):
    conn = db.connect(env)
    try:
        assert auth.deactivate(conn, team["boss"]["email"]) is not None
    finally:
        conn.close()


def _sign_out_elsewhere(env, team):
    _write(env, "DELETE FROM ui_sessions WHERE session_hash = ?",
           (db.hash_token(team["boss"]["cookie"]),))


def _expire_session(env, team):
    _write(env, "UPDATE ui_sessions SET expires_at = '2000-01-01T00:00:00Z'")


def _demote(env, team):
    _write(env, "UPDATE users SET role = 'member' WHERE id = ?", (team["boss"]["id"],))


SESSION_ENDERS = {"machine revoked": _revoke_machine, "user deactivated": _deactivate_user,
                  "session deleted": _sign_out_elsewhere, "session expired": _expire_session}


@pytest.mark.parametrize("route", ROUTES)
def test_the_harness_lets_an_untouched_write_through(env, team, route):
    """The control: with nothing committed in between, each POST is accepted, so a
    refusal below is the race's doing and not the harness's."""
    path, fields = _routes(team)[route]
    status, _ = _post_racing(path, team["boss"]["cookie"], fields)
    assert status == ACCEPTED[route]


@pytest.mark.parametrize("ender", sorted(SESSION_ENDERS))
@pytest.mark.parametrize("route", ROUTES)
def test_a_session_ended_while_the_body_is_held_back_writes_nothing(env, team, route, ender):
    path, fields = _routes(team)[route]
    after_change = {}

    def change():
        SESSION_ENDERS[ender](env, team)
        after_change.update(_snapshot(env))

    status, page = _post_racing(path, team["boss"]["cookie"], fields, change)

    assert status == 401, page[:400]
    assert "loom ui" in page                       # the gate's own 401 page
    assert _snapshot(env) == after_change


@pytest.mark.parametrize("route", ["invite", "deactivate", "role"])
def test_an_admin_demoted_while_the_body_is_held_back_changes_no_account(env, team, route):
    path, fields = _routes(team)[route]
    after_change = {}

    def change():
        _demote(env, team)
        after_change.update(_snapshot(env))

    status, page = _post_racing(path, team["boss"]["cookie"], fields, change)

    assert status == 403 and "forbidden" in page, page[:400]
    assert _snapshot(env) == after_change


def test_an_admin_demoted_while_the_body_is_held_back_cannot_revoke_another_users_machine(
        env, team):
    """§6.4: a chain that is not the caller's is a 404 unless the caller is an admin, and
    that role is the one read under the write lock."""
    path, fields = _routes(team)["revoke"]
    after_change = {}

    def change():
        _demote(env, team)
        after_change.update(_snapshot(env))

    status, page = _post_racing(path, team["boss"]["cookie"], fields, change)

    assert status == 404, page[:400]
    assert _snapshot(env) == after_change


# --------------------------------------------------------------------------- #
# Redeeming re-checks its issuer under the write lock                         #
# --------------------------------------------------------------------------- #
def _setup_code(env, user_id: int, issuer: int) -> str:
    """A setup code as "Add a machine" stores one: kind `setup`, issued by ``issuer``."""
    conn = db.connect(env)
    try:
        code, _ = auth.create_invitation(conn, user_id, 600, kind="setup",
                                         created_by_user_id=user_id,
                                         issued_by_device_token_id=issuer)
        conn.commit()
    finally:
        conn.close()
    return code


def test_a_setup_code_issued_while_its_issuer_was_revoked_does_not_enrol(env, client, team):
    """The interleaving of race_issue: the issuer is revoked, then a code naming it is
    stored (the write that raced the revoke). Redeeming it is `401 invalid_code`."""
    staff = team["staff"]
    conn = db.connect(env)
    try:
        auth.revoke(conn, staff["laptop"]["id"])
    finally:
        conn.close()
    code = _setup_code(env, staff["id"], staff["laptop"]["id"])
    tokens_before = _sql(env, "SELECT id FROM device_tokens")

    r = client.post("/api/enroll", json={"code": code, "device_name": "rogue"},
                    headers={"Fly-Client-IP": "203.0.113.20"})

    assert r.status_code == 401 and r.json()["error"] == "invalid_code"
    assert _sql(env, "SELECT id FROM device_tokens") == tokens_before
    assert _sql(env, "SELECT redeemed_at FROM enrolment_codes WHERE kind = 'setup'") == [
        {"redeemed_at": None}]


def test_a_redemption_queued_behind_its_issuers_revocation_mints_nothing(env, team):
    """The interleaving of race_redeem: the redemption has read the code as valid and is
    waiting for the write lock while the issuer's revocation holds it and commits. It must
    then find the code dead, and no chain may appear that the cascade has already passed."""
    staff = team["staff"]
    issuer = staff["laptop"]["id"]
    code = _setup_code(env, staff["id"], issuer)
    tokens_before = _sql(env, "SELECT id FROM device_tokens")

    holder = db.connect(env)
    queued = threading.Event()
    out = {}

    def redeem():
        redeemer = db.connect(env)
        redeemer.set_trace_callback(
            lambda sql: queued.set() if sql.strip().upper() == "BEGIN IMMEDIATE" else None)
        try:
            out["r"] = auth.redeem(redeemer, code, "rogue")
        finally:
            redeemer.close()

    holder.execute("BEGIN IMMEDIATE")
    thread = threading.Thread(target=redeem)
    thread.start()
    try:
        assert queued.wait(10), "the redemption never asked for the write lock"
        # The revocation commits a little later than the redemption read its clock.
        later = (datetime.now(timezone.utc) + timedelta(seconds=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        auth.revoke_chain(holder, issuer, later)
        holder.commit()
    finally:
        thread.join(30)
        holder.close()

    assert out["r"] is None
    assert _sql(env, "SELECT id FROM device_tokens") == tokens_before
    assert _sql(env, "SELECT redeemed_at FROM enrolment_codes WHERE kind = 'setup'") == [
        {"redeemed_at": None}]
