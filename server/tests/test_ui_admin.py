"""*Admin → Users* (D20, DESIGN §6.4; plan 2026-10-19, phase 2 slice 4).

`GET /ui/admin/users` and its three writes — invite, deactivate, change role — for
admins only; a member gets `403 forbidden` on every one of them, GET or POST, with the
role read from `users` on every request. The refusals the session gate owns (`405`,
public URL, origin, CSRF, session kind) are `test_ui_writes.py`'s.

Expected values are §6.4's literals: 24 h invitations of kind `invite`, the public URL
`https://loom-test.fly.dev` (the `env` fixture runs as Fly does), the event vocabulary,
`409 last_admin`. Machines are minted through the real `auth.create_invitation` →
`auth.redeem`, so the admin's session can be tied to a device token.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from server import admin, auth, db

PUBLIC_ORIGIN = "https://loom-test.fly.dev"
SAME_ORIGIN = {"Origin": PUBLIC_ORIGIN}
ADMIN_USERS = "/ui/admin/users"
INVITE = "/ui/admin/users/invite"
INVITE_TTL_S = 24 * 3600
HOSTILE = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'


def _deactivate(user_id: int) -> str:
    return f"/ui/admin/users/{user_id}/deactivate"


def _role(user_id: int) -> str:
    return f"/ui/admin/users/{user_id}/role"


def _revoke(device_id: int) -> str:
    return f"/ui/devices/{device_id}/revoke"


def _sql(env, query: str, params: tuple = ()) -> list[dict]:
    conn = db.connect(env)
    try:
        return [dict(row) for row in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def _set_role(env, user_id: int, role: str) -> None:
    conn = db.connect(env)
    try:
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        conn.commit()
    finally:
        conn.close()


def _machine(env, user, device_name: str) -> dict:
    """A device token of ``user``, minted the way `loom enroll` mints one."""
    conn = db.connect(env)
    try:
        code, _ = auth.create_invitation(conn, user["id"], 3600)
        conn.commit()
        body = auth.redeem(conn, code, device_name)
    finally:
        conn.close()
    return {"id": body["device_token_id"], "auth": {"Authorization": f"Bearer {body['token']}"}}


class Browser:
    """One signed-in `loom_ui` session, with its own cookie jar; POSTs as its pages do."""

    def __init__(self, bearer: dict):
        from server.app import app

        self.client = TestClient(app, base_url="https://testserver")
        code = self.client.post("/api/ui-login", headers=bearer).json()["code"]
        assert self.client.post("/ui/session", json={"code": code}).status_code == 204
        cookie = self.client.cookies["loom_ui"]
        self.csrf = hmac.new(cookie.encode(), b"csrf", hashlib.sha256).hexdigest()

    def get(self, path: str):
        return self.client.get(path)

    def post(self, path: str, fields: dict | None = None):
        return self.client.post(path, data={**(fields or {}), "csrf": self.csrf},
                                headers=SAME_ORIGIN, follow_redirects=False)


@pytest.fixture
def boss(env, user_factory):
    """An admin signed in from an enrolled machine (so the session has a device token)."""
    user = user_factory("boss")
    _set_role(env, user["id"], "admin")
    user["device"] = _machine(env, user, "boss-laptop")
    user["browser"] = Browser(user["device"]["auth"])
    return user


@pytest.fixture
def member(env, user_factory):
    user = user_factory("staff")
    user["device"] = _machine(env, user, "staff-laptop")
    user["browser"] = Browser(user["device"]["auth"])
    return user


def _user(env, email: str) -> dict | None:
    rows = _sql(env, "SELECT * FROM users WHERE email = ?", (email,))
    return rows[0] if rows else None


def _events(env, event: str, target_id: int) -> list[dict]:
    return _sql(env, "SELECT * FROM auth_events WHERE event = ? AND target_type = 'user' "
                     "AND target_id = ? ORDER BY id", (event, target_id))


def _link_code(page: str) -> str:
    match = re.search(re.escape(PUBLIC_ORIGIN) + r"/ui/join#code=([A-Za-z0-9_-]{20,})", page)
    assert match, page
    return match.group(1)


def _redirected_to_admin_users(r) -> None:
    assert r.status_code == 303, r.text
    assert r.headers["location"] == ADMIN_USERS


def _row(page: str, email: str) -> str:
    """The `<tr>` … `</tr>` of the user whose email is ``email``."""
    match = re.search(r"<tr[^>]*>(?:(?!</tr>).)*?" + re.escape(email) + r".*?</tr>", page,
                      re.S)
    assert match, email
    return match.group(0)


# --------------------------------------------------------------------------- #
# Admins only, read on every request                                           #
# --------------------------------------------------------------------------- #
def test_a_member_gets_403_forbidden_on_every_admin_route(env, member, user_factory):
    victim = user_factory("victim")
    browser = member["browser"]
    responses = [
        browser.get(ADMIN_USERS),
        browser.post(INVITE, {"email": "smuggled@example.com", "name": "x", "role": "admin"}),
        browser.post(_deactivate(victim["id"])),
        browser.post(_role(member["id"]), {"role": "admin"}),
    ]
    for r in responses:
        assert r.status_code == 403, r.text
        assert "<h1>Forbidden</h1>" in r.text
    assert _user(env, "smuggled@example.com") is None
    assert _user(env, victim["email"])["active"] == 1
    assert _user(env, member["email"])["role"] == "member"
    assert _sql(env, "SELECT * FROM auth_events WHERE event != 'ui_signin'") == []


def test_a_member_is_refused_before_the_id_is_looked_up(env, member):
    """The role check comes first: an unknown id tells a member nothing."""
    r = member["browser"].post(_deactivate(987_654_321))
    assert r.status_code == 403 and "<h1>Forbidden</h1>" in r.text


def test_the_role_is_read_on_every_request_of_an_open_session(env, member):
    browser = member["browser"]
    assert browser.get(ADMIN_USERS).status_code == 403
    _set_role(env, member["id"], "admin")
    assert browser.get(ADMIN_USERS).status_code == 200
    _set_role(env, member["id"], "member")
    assert browser.get(ADMIN_USERS).status_code == 403


# --------------------------------------------------------------------------- #
# GET /ui/admin/users                                                          #
# --------------------------------------------------------------------------- #
def test_the_page_lists_every_user_with_role_state_and_pending_invitation(
        env, boss, member, user_factory):
    gone = user_factory("gone", active=0)
    conn = db.connect(env)
    try:
        assert admin.invite(conn, "pending@example.com", "pending", INVITE_TTL_S) is not None
        conn.commit()
    finally:
        conn.close()

    r = boss["browser"].get(ADMIN_USERS)

    assert r.status_code == 200, r.text
    page = r.text
    boss_row, member_row = _row(page, boss["email"]), _row(page, member["email"])
    gone_row, pending_row = _row(page, gone["email"]), _row(page, "pending@example.com")
    assert ">boss<" in boss_row and ">admin<" in boss_row and ">active<" in boss_row
    assert ">staff<" in member_row and ">member<" in member_row
    assert ">deactivated<" in gone_row
    assert "pending invitation" in pending_row
    assert "pending invitation" not in member_row and "pending invitation" not in boss_row


def test_an_expired_or_redeemed_invitation_is_not_pending(env, boss, user_factory):
    late = user_factory("late")
    conn = db.connect(env)
    try:
        auth.create_invitation(conn, late["id"], -1)
        conn.commit()
    finally:
        conn.close()
    page = boss["browser"].get(ADMIN_USERS).text
    assert "pending invitation" not in _row(page, late["email"])
    # The boss's own invitation was redeemed by `_machine`.
    assert "pending invitation" not in _row(page, boss["email"])


def test_each_machine_is_listed_with_a_revoke_form_while_it_is_live(env, boss, member):
    spare = _machine(env, member, "staff-spare")
    conn = db.connect(env)
    try:
        assert auth.revoke(conn, spare["id"]) == 1
    finally:
        conn.close()

    page = boss["browser"].get(ADMIN_USERS).text

    assert "staff-laptop" in page and "staff-spare" in page
    assert f'action="{_revoke(member["device"]["id"])}"' in page
    assert f'action="{_revoke(spare["id"])}"' not in page
    assert re.search(r"<li>(?:(?!</li>).)*staff-spare(?:(?!</li>).)*revoked", page, re.S)
    assert not re.search(r"<li>(?:(?!</li>).)*staff-laptop(?:(?!</li>).)*revoked", page, re.S)


def test_a_renewed_machine_is_one_row_named_by_its_chain(env, boss, member):
    conn = db.connect(env)
    try:
        successor = auth.renew(conn, member["device"]["id"])["device_token_id"]
    finally:
        conn.close()
    page = boss["browser"].get(ADMIN_USERS).text
    assert page.count(">staff-laptop<") == 1
    assert f'action="{_revoke(member["device"]["id"])}"' in page
    assert f'action="{_revoke(successor)}"' not in page


def test_the_page_has_the_invite_deactivate_and_role_forms_with_the_token(env, boss, member):
    browser = boss["browser"]
    page = browser.get(ADMIN_USERS).text
    invite_form = re.search(rf'<form method="post" action="{INVITE}".*?</form>', page, re.S)
    assert invite_form, page
    for field in ("email", "name", "role"):
        assert f'name="{field}"' in invite_form.group(0), field
    assert '<option value="member"' in invite_form.group(0)
    assert '<option value="admin"' in invite_form.group(0)
    for path in (_deactivate(member["id"]), _role(member["id"])):
        form = re.search(rf'<form method="post" action="{path}".*?</form>', page, re.S)
        assert form, path
        assert f'name="csrf" value="{browser.csrf}"' in form.group(0)
    assert f'action="{_deactivate(boss["id"])}"' in page


def test_no_deactivate_form_for_a_deactivated_user(env, boss, user_factory):
    gone = user_factory("gone", active=0)
    page = boss["browser"].get(ADMIN_USERS).text
    assert f'action="{_deactivate(gone["id"])}"' not in page


def test_hostile_names_are_escaped(env, boss, user_factory):
    hostile = user_factory(HOSTILE, email="hostile@example.com")
    _machine(env, hostile, HOSTILE)
    page = boss["browser"].get(ADMIN_USERS).text
    assert "<script>" not in page and "<img" not in page
    assert page.count("&lt;script&gt;alert(1)&lt;/script&gt;") >= 2   # name and machine


def test_the_page_is_a_read(env, boss, member):
    tables = ("users", "device_tokens", "enrolment_codes", "auth_events", "ui_sessions")
    before = {t: _sql(env, f"SELECT * FROM {t}") for t in tables}
    browser = boss["browser"]
    assert browser.get(ADMIN_USERS).status_code == 200
    assert browser.client.head(ADMIN_USERS).status_code == 200
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert browser.client.request(method, ADMIN_USERS,
                                      headers=SAME_ORIGIN).status_code == 405, method
    assert {t: _sql(env, f"SELECT * FROM {t}") for t in tables} == before


# --------------------------------------------------------------------------- #
# POST /ui/admin/users/invite                                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", ["admin", "member"])
def test_inviting_a_new_email_creates_the_user_and_shows_the_link_once(env, boss, role):
    browser = boss["browser"]

    r = browser.post(INVITE, {"email": "newbie@example.com", "name": "newbie", "role": role})

    assert r.status_code == 200, r.text
    code = _link_code(r.text)
    user = _user(env, "newbie@example.com")
    assert (user["name"], user["role"], user["active"]) == ("newbie", role, 1)
    [row] = _sql(env, "SELECT * FROM enrolment_codes WHERE code_hash = ?",
                 (db.hash_token(code),))
    assert row["kind"] == "invite" and row["user_id"] == user["id"]
    assert row["created_by_user_id"] == boss["id"]
    assert row["issued_by_device_token_id"] == boss["device"]["id"]
    ttl = (datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
           - datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")))
    assert ttl.total_seconds() == INVITE_TTL_S
    [event] = _events(env, "user_invited", user["id"])
    assert event["actor_user_id"] == boss["id"]
    assert code not in browser.get(ADMIN_USERS).text
    conn = db.connect(env)
    try:
        assert auth.redeem(conn, code, "newbie-laptop")["user"]["name"] == "newbie"
    finally:
        conn.close()


def test_inviting_a_known_email_keeps_its_name_and_role(env, boss, member):
    r = boss["browser"].post(INVITE, {"email": member["email"], "name": "renamed",
                                      "role": "admin"})
    assert r.status_code == 200, r.text
    code = _link_code(r.text)
    user = _user(env, member["email"])
    assert (user["name"], user["role"]) == ("staff", "member")
    [row] = _sql(env, "SELECT * FROM enrolment_codes WHERE code_hash = ?",
                 (db.hash_token(code),))
    assert row["user_id"] == member["id"]
    assert [e["actor_user_id"] for e in _events(env, "user_invited", member["id"])] == [
        boss["id"]]


def test_a_known_email_needs_no_name(env, boss, member):
    r = boss["browser"].post(INVITE, {"email": member["email"], "name": "", "role": "member"})
    assert r.status_code == 200, r.text
    _link_code(r.text)


def test_the_invitation_page_names_who_it_is_for_escaped(env, boss, user_factory):
    hostile = user_factory(HOSTILE, email="hostile@example.com")
    r = boss["browser"].post(INVITE, {"email": hostile["email"], "role": "member"})
    assert r.status_code == 200
    assert "<script>" not in r.text and "&lt;script&gt;" in r.text


@pytest.mark.parametrize("fields, words", [
    ({"email": "nobody@example.com", "name": "", "role": "member"}, "name"),
    ({"email": "nobody@example.com", "name": "   ", "role": "member"}, "name"),
    ({"email": "nobody@example.com", "role": "member"}, "name"),
    ({"email": "", "name": "nobody", "role": "member"}, "email"),
    ({"name": "nobody", "role": "member"}, "email"),
    ({"email": "nobody@example.com", "name": "nobody", "role": "root"}, "role"),
    ({"email": "nobody@example.com", "name": "nobody"}, "role"),
])
def test_an_incomplete_invitation_is_a_400_that_changes_nothing(env, boss, fields, words):
    codes_before = _sql(env, "SELECT id FROM enrolment_codes")
    r = boss["browser"].post(INVITE, fields)
    assert r.status_code == 400, r.text
    assert words in r.text.lower()
    assert _user(env, "nobody@example.com") is None
    assert _sql(env, "SELECT id FROM enrolment_codes") == codes_before
    assert _sql(env, "SELECT * FROM auth_events WHERE event = 'user_invited'") == []


def test_a_session_opened_with_a_permanent_token_issues_without_a_device(env, user_factory):
    """§6.4: `issued_by_device_token_id` is `NULL` from a session opened with a
    permanent token (`adduser`'s)."""
    legacy = user_factory("legacy")
    _set_role(env, legacy["id"], "admin")
    r = Browser(legacy["auth"]).post(INVITE, {"email": "n@example.com", "name": "n",
                                              "role": "member"})
    assert r.status_code == 200, r.text
    [row] = _sql(env, "SELECT * FROM enrolment_codes WHERE code_hash = ?",
                 (db.hash_token(_link_code(r.text)),))
    assert row["created_by_user_id"] == legacy["id"]
    assert row["issued_by_device_token_id"] is None


# --------------------------------------------------------------------------- #
# POST /ui/admin/users/{id}/deactivate                                         #
# --------------------------------------------------------------------------- #
def test_deactivating_revokes_machines_expires_codes_and_ends_the_session(env, boss, member):
    conn = db.connect(env)
    try:
        pending, _ = auth.create_invitation(conn, member["id"], INVITE_TTL_S)
        conn.commit()
    finally:
        conn.close()

    _redirected_to_admin_users(boss["browser"].post(_deactivate(member["id"])))

    assert _user(env, member["email"])["active"] == 0
    assert _sql(env, "SELECT id FROM device_tokens WHERE user_id = ? AND revoked_at IS NULL",
                (member["id"],)) == []
    conn = db.connect(env)
    try:
        assert auth.redeem(conn, pending, "after") is None
    finally:
        conn.close()
    assert member["browser"].get("/ui/").status_code == 401
    [event] = _events(env, "user_deactivated", member["id"])
    assert event["actor_user_id"] == boss["id"]
    assert event["client_ip"]


def test_deactivating_an_unknown_id_is_a_404(env, boss):
    r = boss["browser"].post(_deactivate(987_654_321))
    assert r.status_code == 404
    assert _sql(env, "SELECT * FROM auth_events WHERE event = 'user_deactivated'") == []


def test_deactivating_a_deactivated_user_records_nothing_new(env, boss, user_factory):
    gone = user_factory("gone", active=0)
    _redirected_to_admin_users(boss["browser"].post(_deactivate(gone["id"])))
    assert _events(env, "user_deactivated", gone["id"]) == []


def test_the_last_active_admin_cannot_be_deactivated(env, boss, user_factory):
    r = boss["browser"].post(_deactivate(boss["id"]))
    assert r.status_code == 409, r.text
    assert "last_admin" in r.text
    assert _user(env, boss["email"])["active"] == 1
    assert _events(env, "user_deactivated", boss["id"]) == []
    # An inactive admin does not count; a second active one does.
    dormant = user_factory("dormant", active=0)
    _set_role(env, dormant["id"], "admin")
    assert boss["browser"].post(_deactivate(boss["id"])).status_code == 409
    second = user_factory("second")
    _set_role(env, second["id"], "admin")
    _redirected_to_admin_users(boss["browser"].post(_deactivate(boss["id"])))
    assert _user(env, boss["email"])["active"] == 0


# --------------------------------------------------------------------------- #
# POST /ui/admin/users/{id}/role                                               #
# --------------------------------------------------------------------------- #
def test_changing_a_role_promotes_and_demotes_with_events(env, boss, member):
    browser = boss["browser"]
    _redirected_to_admin_users(browser.post(_role(member["id"]), {"role": "admin"}))
    assert _user(env, member["email"])["role"] == "admin"
    assert member["browser"].get(ADMIN_USERS).status_code == 200
    _redirected_to_admin_users(browser.post(_role(member["id"]), {"role": "member"}))
    assert _user(env, member["email"])["role"] == "member"
    assert member["browser"].get(ADMIN_USERS).status_code == 403
    [promoted] = _events(env, "promoted", member["id"])
    [demoted] = _events(env, "demoted", member["id"])
    assert promoted["actor_user_id"] == demoted["actor_user_id"] == boss["id"]


def test_setting_the_role_a_user_already_has_records_nothing(env, boss, member):
    _redirected_to_admin_users(boss["browser"].post(_role(member["id"]), {"role": "member"}))
    assert _events(env, "demoted", member["id"]) == []


@pytest.mark.parametrize("fields", [{"role": "root"}, {"role": ""}, {}, {"role": "Admin"}])
def test_a_role_outside_the_two_is_a_400_that_changes_nothing(env, boss, member, fields):
    r = boss["browser"].post(_role(member["id"]), fields)
    assert r.status_code == 400, r.text
    assert "role" in r.text.lower()
    assert _user(env, member["email"])["role"] == "member"


def test_changing_the_role_of_an_unknown_id_is_a_404(env, boss):
    assert boss["browser"].post(_role(987_654_321), {"role": "admin"}).status_code == 404


def test_the_last_active_admin_cannot_be_demoted(env, boss, user_factory):
    r = boss["browser"].post(_role(boss["id"]), {"role": "member"})
    assert r.status_code == 409, r.text
    assert "last_admin" in r.text
    assert _user(env, boss["email"])["role"] == "admin"
    assert _events(env, "demoted", boss["id"]) == []
    second = user_factory("second")
    _set_role(env, second["id"], "admin")
    _redirected_to_admin_users(boss["browser"].post(_role(boss["id"]), {"role": "member"}))
    assert boss["browser"].get(ADMIN_USERS).status_code == 403
