"""`GET /api/me` (DESIGN §6.4, D20 phase 2a): who the bearer is and the account events
about them, newest first. Bearer-only, like every `/api` route."""
from __future__ import annotations

import re
import sqlite3

from server import admin, db

CODE_RE = re.compile(r"^[A-Za-z0-9_-]{32,}$")
# §6.4: `{"name", "email", "role", "device_id", "events"}`; each event
# `{"ts", "event", "actor", "target_type", "target_id"}`.
ME_KEYS = {"name", "email", "role", "device_id", "events"}
EVENT_KEYS = {"ts", "event", "actor", "target_type", "target_id"}


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _enrolled(client, capsys, email: str, name: str, device_name: str) -> tuple[str, int]:
    """`admin.py invite` + `POST /api/enroll`: a device token and its row id."""
    assert admin.main(["invite", "--email", email, "--name", name]) == 0
    code = next(line.strip() for line in capsys.readouterr().out.splitlines()
                if CODE_RE.fullmatch(line.strip()))
    r = client.post("/api/enroll", json={"code": code, "device_name": device_name})
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    return token, _device_id(email, token)


def _device_id(email: str, token: str) -> int:
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT d.id FROM device_tokens d JOIN users u ON u.id = d.user_id "
            "WHERE u.email = ? AND d.token_hash = ?", (email, db.hash_token(token))).fetchone()[0]
    finally:
        conn.close()


def _user_id(email: str) -> int:
    conn = db.connect()
    try:
        return conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()[0]
    finally:
        conn.close()


def _insert_events(env: str, rows: list[tuple]) -> None:
    """Rows `(ts, actor_user_id, event, target_type, target_id, client_ip)`, as written."""
    conn = sqlite3.connect(env)
    try:
        conn.executemany("INSERT INTO auth_events (ts, actor_user_id, event, target_type, "
                         "target_id, client_ip) VALUES (?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


def test_a_device_token_gets_its_name_email_role_and_device_id(client, capsys):
    token, device_id = _enrolled(client, capsys, "ana@example.test", "ana", "ana-laptop")

    r = client.get("/api/me", headers=_bearer(token))

    assert r.status_code == 200, r.text
    assert r.json() == {"name": "ana", "email": "ana@example.test", "role": "member",
                        "device_id": device_id, "events": []}


def test_a_renewed_token_names_itself(client, capsys):
    token, first_id = _enrolled(client, capsys, "ana@example.test", "ana", "ana-laptop")
    successor = client.post("/api/device-tokens/renew", headers=_bearer(token)).json()["token"]

    body = client.get("/api/me", headers=_bearer(successor)).json()

    assert body["device_id"] == _device_id("ana@example.test", successor) != first_id


def test_a_permanent_token_has_no_device_and_sees_its_promotion(client, user_factory):
    user = user_factory("bea", email="bea@example.test")
    assert admin.main(["promote", "--email", "bea@example.test"]) == 0

    body = client.get("/api/me", headers=user["auth"]).json()

    assert set(body) == ME_KEYS
    assert (body["name"], body["email"], body["role"], body["device_id"]) == (
        "bea", "bea@example.test", "admin", None)
    assert [{k: e[k] for k in EVENT_KEYS - {"ts"}} for e in body["events"]] == [
        {"event": "promoted", "actor": None, "target_type": "user", "target_id": user["id"]}]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", body["events"][0]["ts"])


def test_events_are_the_callers_own_newest_first_with_the_actors_name(
        client, capsys, env, user_factory):
    """The user's rows and every one of their device tokens' rows (any chain, revoked or
    not); nobody else's. `actor` is a name, or null for admin.py; no `client_ip`."""
    token, live_id = _enrolled(client, capsys, "ana@example.test", "ana", "ana-laptop")
    _, old_id = _enrolled(client, capsys, "ana@example.test", "ana", "ana-old")
    ana = _user_id("ana@example.test")
    other = user_factory("carl", email="carl@example.test")
    _, dans_device = _enrolled(client, capsys, "dan@example.test", "dan", "dan-desk")
    _insert_events(env, [
        ("2026-10-01T10:00:00Z", ana, "ui_signin", "user", ana, "203.0.113.7"),
        ("2026-10-03T10:00:00Z", other["id"], "device_revoked", "device", old_id, "203.0.113.8"),
        ("2026-10-02T10:00:00Z", None, "promoted", "user", ana, None),
        # Same second: the later row is the newer one.
        ("2026-10-04T10:00:00Z", None, "demoted", "user", ana, None),
        ("2026-10-04T10:00:00Z", ana, "setup_code_issued", "user", ana, "203.0.113.7"),
        # Not about ana: another user, and another user's device.
        ("2026-10-05T10:00:00Z", ana, "user_invited", "user", other["id"], "203.0.113.7"),
        ("2026-10-05T10:00:00Z", None, "device_revoked", "device", dans_device, None),
    ])

    events = client.get("/api/me", headers=_bearer(token)).json()["events"]

    assert events == [
        {"ts": "2026-10-04T10:00:00Z", "event": "setup_code_issued", "actor": "ana",
         "target_type": "user", "target_id": ana},
        {"ts": "2026-10-04T10:00:00Z", "event": "demoted", "actor": None,
         "target_type": "user", "target_id": ana},
        {"ts": "2026-10-03T10:00:00Z", "event": "device_revoked", "actor": "carl",
         "target_type": "device", "target_id": old_id},
        {"ts": "2026-10-02T10:00:00Z", "event": "promoted", "actor": None,
         "target_type": "user", "target_id": ana},
        {"ts": "2026-10-01T10:00:00Z", "event": "ui_signin", "actor": "ana",
         "target_type": "user", "target_id": ana},
    ]
    assert live_id != old_id


def test_events_are_capped_at_the_twenty_newest(client, env, user_factory):
    user = user_factory("ana", email="ana@example.test")
    _insert_events(env, [(f"2026-10-01T10:00:{s:02d}Z", None, "promoted", "user", user["id"], None)
                         for s in range(25)])

    events = client.get("/api/me", headers=user["auth"]).json()["events"]

    assert [e["ts"] for e in events] == [f"2026-10-01T10:00:{s:02d}Z" for s in range(24, 4, -1)]


def test_it_is_bearer_only_and_refuses_a_dead_credential(client, capsys, user_factory):
    assert client.get("/api/me").status_code == 401
    token, device_id = _enrolled(client, capsys, "ana@example.test", "ana", "ana-laptop")
    assert admin.main(["revokedevice", "--id", str(device_id)]) == 0
    assert client.get("/api/me", headers=_bearer(token)).status_code == 401

    gone = user_factory("eve", email="eve@example.test", active=0)
    assert client.get("/api/me", headers=gone["auth"]).status_code == 403


def test_a_ui_session_cookie_opens_nothing_on_api_me(ui, ui_user):
    """D14: `/api/*` never reads the cookie, and `ui_user` left one in the jar."""
    assert ui.get("/api/me").status_code == 401
    assert ui.get("/api/me", headers=ui_user["auth"]).status_code == 200
