""""Retire" vs "Revoke (lost or stolen)" (D20, DESIGN §6.4 as amended on 2026-10-19).

§6.4: `POST /ui/devices/{id}/retire` — "the chain's owner, or an admin" — "Retires the
chain: revokes it and expires every unredeemed code its ids issued, **without** the
cascade, so a machine added from it keeps working (replacing a laptop: add the new one
from the old one, then retire the old one); records `device_retired`; same `404` and
`303` rules as revoke. `admin.py revokedevice --no-cascade` is the same action from the
shell". Events: `device_retired` (`device`, the id the action named); "`admin.py`'s other
commands keep phase 1's rule and write none". And every write re-reads the session
"inside each write's `BEGIN IMMEDIATE`".

Machines are enrolled through the shipped path (`admin.py invite` or a setup code from
the web → `/api/enroll`), browsers sign in through `loom ui`'s (`/api/ui-login` →
`/ui/session`), and every outcome is read back through the public surface: `/api/projects`
with a token, `/api/enroll` with a code. The `env` fixture runs as Fly does, so the public
URL is `https://loom-test.fly.dev` and the client address is `Fly-Client-IP`.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from itertools import count
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from server import admin, auth, db

PUBLIC_HOST = "loom-test.fly.dev"                 # https://$FLY_APP_NAME.fly.dev (D19)
PUBLIC_URL = f"https://{PUBLIC_HOST}"
LINK_RE = re.compile(re.escape(PUBLIC_URL) + r"/ui/join#code=([A-Za-z0-9_-]{32,})")
CODE_LINE = re.compile(r"^\s+([A-Za-z0-9_-]{32,})$")
TABLES = ("users", "device_tokens", "enrolment_codes", "ui_sessions", "auth_events")
_IPS = count(1)


def retire_path(device_id: int) -> str:
    return f"/ui/devices/{device_id}/retire"


def revoke_path(device_id: int) -> str:
    return f"/ui/devices/{device_id}/revoke"


def _ip() -> str:
    return f"203.0.113.{next(_IPS) % 250 + 1}"


def _query(env, sql, *params):
    conn = db.connect(env)
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _snapshot(env) -> dict[str, list[dict]]:
    return {table: _query(env, f"SELECT * FROM {table} ORDER BY rowid") for table in TABLES}


def _events(env) -> list[dict]:
    return _query(env, "SELECT actor_user_id, event, target_type, target_id, client_ip "
                       "FROM auth_events WHERE event IN ('device_retired', 'device_revoked') "
                       "ORDER BY id")


def _invite_code(capsys, email: str, name: str | None = None) -> str:
    argv = ["invite", "--email", email] + (["--name", name] if name else [])
    assert admin.main(argv) == 0
    return next(m.group(1) for line in capsys.readouterr().out.splitlines()
                if (m := CODE_LINE.match(line)))


def _enrol(client, code: str, device_name: str):
    return client.post("/api/enroll", json={"code": code, "device_name": device_name},
                       headers={"Fly-Client-IP": _ip()})


def _machine(client, code: str, device_name: str) -> tuple[str, int]:
    r = _enrol(client, code, device_name)
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    return token, _query(client.env, "SELECT id FROM device_tokens WHERE token_hash = ?",
                         db.hash_token(token))[0]["id"]


def _renew(client, token: str):
    return client.post("/api/device-tokens/renew",
                       headers={"Authorization": f"Bearer {token}", "Fly-Client-IP": _ip()})


def _status(client, token: str) -> int:
    return client.get("/api/projects", headers={"Authorization": f"Bearer {token}",
                                                "Fly-Client-IP": _ip()}).status_code


class Browser:
    """One signed-in `loom_ui` session posting as a same-origin page does."""

    def __init__(self, bearer: str):
        from server.app import app

        self.client = TestClient(app, base_url="https://testserver")
        code = self.client.post("/api/ui-login",
                                headers={"Authorization": f"Bearer {bearer}"}).json()["code"]
        assert self.client.post("/ui/session", json={"code": code}).status_code == 204
        self.cookie = self.client.cookies["loom_ui"]
        self.csrf = hmac.new(self.cookie.encode(), b"csrf", hashlib.sha256).hexdigest()
        self.last_ip: str | None = None

    def get(self, path: str):
        return self.client.get(path, headers={"Fly-Client-IP": _ip()})

    def post(self, path: str, fields: dict | None = None):
        self.last_ip = _ip()
        return self.client.post(path, data={"csrf": self.csrf, **(fields or {})},
                                headers={"Origin": PUBLIC_URL, "Fly-Client-IP": self.last_ip},
                                follow_redirects=False)

    def setup_code(self) -> str:
        r = self.post("/ui/machines/setup-code")
        assert r.status_code == 200, r.text
        return LINK_RE.search(r.text).group(1)

    def invite_code(self, email: str, name: str) -> str:
        r = self.post("/ui/admin/users/invite", {"email": email, "name": name, "role": "member"})
        assert r.status_code == 200, r.text
        return LINK_RE.search(r.text).group(1)


@pytest.fixture
def client(env):
    from server.app import app

    with TestClient(app) as c:
        c.env = env
        yield c


@pytest.fixture
def people(client, capsys):
    """Two members with one machine each, and an admin with one."""
    out = {}
    for name in ("dev1", "dev2", "boss"):
        email = f"{name}@example.test"
        token, device_id = _machine(client, _invite_code(capsys, email, name), f"{name}-laptop")
        user_id = _query(client.env, "SELECT id FROM users WHERE email = ?", email)[0]["id"]
        out[name] = {"email": email, "token": token, "device_id": device_id,
                     "user_id": user_id}
    assert admin.main(["promote", "--email", "boss@example.test"]) == 0
    capsys.readouterr()
    return out


@pytest.fixture
def replacement(client, people):
    """dev1's laptop replacement under way: from the old laptop's `/ui` session "Add a
    machine" issued a setup code and the new laptop enrolled with it."""
    old = people["dev1"]
    browser = Browser(old["token"])
    new_token, new_id = _machine(client, browser.setup_code(), "dev1-new-laptop")
    assert _status(client, new_token) == 200
    return {"old": old, "browser": browser, "new_token": new_token, "new_id": new_id}


# --------------------------------------------------------------------------- #
# Replacing a laptop: Retire vs Revoke                                         #
# --------------------------------------------------------------------------- #
def test_retiring_the_old_laptop_leaves_the_machine_it_added_working(client, replacement):
    old, browser = replacement["old"], replacement["browser"]
    r = browser.post(retire_path(old["device_id"]))

    assert r.status_code == 303 and r.headers["location"] == "/ui/machines"
    assert _status(client, old["token"]) == 401
    assert _renew(client, old["token"]).status_code == 401
    assert _status(client, replacement["new_token"]) == 200
    renewed = _renew(client, replacement["new_token"])
    assert renewed.status_code == 201
    assert _status(client, renewed.json()["token"]) == 200
    assert _events(client.env) == [
        {"actor_user_id": old["user_id"], "event": "device_retired", "target_type": "device",
         "target_id": old["device_id"], "client_ip": browser.last_ip}]


def test_revoking_the_old_laptop_also_revokes_the_machine_it_added(client, replacement):
    old, browser = replacement["old"], replacement["browser"]
    r = browser.post(revoke_path(old["device_id"]))

    assert r.status_code == 303 and r.headers["location"] == "/ui/machines"
    assert _status(client, old["token"]) == 401
    assert _status(client, replacement["new_token"]) == 401
    assert [e["event"] for e in _events(client.env)] == ["device_revoked"]


def test_retiring_the_old_laptop_ends_its_session_and_the_new_one_signs_in(client,
                                                                          replacement):
    old, browser = replacement["old"], replacement["browser"]
    assert browser.post(retire_path(old["device_id"])).status_code == 303

    assert browser.get("/ui/machines").status_code == 401
    page = Browser(replacement["new_token"]).get("/ui/machines")
    assert page.status_code == 200 and "dev1-new-laptop" in page.text


# --------------------------------------------------------------------------- #
# What a retire reaches                                                        #
# --------------------------------------------------------------------------- #
def test_retire_expires_every_unredeemed_code_any_id_of_the_chain_issued(client, people):
    """Codes issued from the chain's first token and from its renewed one, of both kinds
    (an admin's laptop issues invitations too), are dead, not merely refused for their
    revoked issuer."""
    boss = people["boss"]
    first = Browser(boss["token"])
    renewed = _renew(client, boss["token"]).json()["token"]
    second = Browser(renewed)
    pending = [first.setup_code(), second.setup_code(),
               first.invite_code("new1@example.test", "new1"),
               second.invite_code("new2@example.test", "new2")]

    r = second.post(retire_path(boss["device_id"]))

    assert r.status_code == 303 and r.headers["location"] == "/ui/machines"
    now = db.iso_now()
    rows = _query(client.env, "SELECT code_hash, expires_at FROM enrolment_codes "
                              "WHERE redeemed_at IS NULL")
    expiry = {row["code_hash"]: row["expires_at"] for row in rows}
    for code in pending:
        assert expiry[db.hash_token(code)] <= now
        assert _enrol(client, code, "probe").status_code == 401
    assert [e["target_id"] for e in _events(client.env)] == [boss["device_id"]]


def test_retire_touches_no_other_chain(client, capsys, people):
    """Neither another machine of the same user — nor one it added — nor another user's,
    nor the codes they issued."""
    dev1 = people["dev1"]
    desk_token, _ = _machine(client, _invite_code(capsys, dev1["email"]), "dev1-desk")
    desk = Browser(desk_token)
    desk_code = desk.setup_code()
    laptop = Browser(dev1["token"])

    assert laptop.post(retire_path(dev1["device_id"])).status_code == 303

    assert _status(client, dev1["token"]) == 401
    assert _status(client, desk_token) == 200
    assert _status(client, people["dev2"]["token"]) == 200
    assert _status(client, people["boss"]["token"]) == 200
    assert _enrol(client, desk_code, "dev1-third").status_code == 201


def test_any_id_of_the_chain_names_it(client, people):
    dev1 = people["dev1"]
    renewed = _renew(client, dev1["token"]).json()["token"]
    renewed_id = _query(client.env, "SELECT id FROM device_tokens WHERE token_hash = ?",
                        db.hash_token(renewed))[0]["id"]

    r = Browser(renewed).post(retire_path(renewed_id))

    assert r.status_code == 303
    assert _status(client, dev1["token"]) == 401 and _status(client, renewed) == 401
    assert [e["target_id"] for e in _events(client.env)] == [renewed_id]


def test_retiring_a_dead_chain_changes_nothing_and_records_nothing(client, capsys, people):
    dev1 = people["dev1"]
    _, old_id = _machine(client, _invite_code(capsys, dev1["email"]), "dev1-old")
    assert admin.main(["revokedevice", "--id", str(old_id), "--no-cascade"]) == 0
    capsys.readouterr()
    before = _snapshot(client.env)

    r = Browser(dev1["token"]).post(retire_path(old_id))

    assert r.status_code == 303 and r.headers["location"] == "/ui/machines"
    after = _snapshot(client.env)
    assert after["device_tokens"] == before["device_tokens"]
    assert _events(client.env) == []


# --------------------------------------------------------------------------- #
# Who may retire what (§6.4: the same 404 and 303 rules as revoke)             #
# --------------------------------------------------------------------------- #
def test_an_admin_retires_anyone_and_is_sent_back_to_users(client, people):
    boss, dev2 = people["boss"], people["dev2"]
    browser = Browser(boss["token"])

    r = browser.post(retire_path(dev2["device_id"]))

    assert r.status_code == 303 and r.headers["location"] == "/ui/admin/users"
    assert _status(client, dev2["token"]) == 401
    assert _status(client, boss["token"]) == 200
    assert _events(client.env) == [
        {"actor_user_id": boss["user_id"], "event": "device_retired", "target_type": "device",
         "target_id": dev2["device_id"], "client_ip": browser.last_ip}]


@pytest.mark.parametrize("device_id", ["dev2", 999, 0, 2 ** 63, 10 ** 30])
def test_a_member_cannot_retire_another_users_machine_or_a_missing_one(client, people,
                                                                      device_id):
    """Another user's chain is answered exactly as an unknown id, or one no row can have."""
    if device_id == "dev2":
        device_id = people["dev2"]["device_id"]
    before = _snapshot(client.env)

    r = Browser(people["dev1"]["token"]).post(retire_path(device_id))

    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]
    assert _status(client, people["dev2"]["token"]) == 200
    after = _snapshot(client.env)
    assert after["device_tokens"] == before["device_tokens"]
    assert after["enrolment_codes"] == before["enrolment_codes"]
    assert _events(client.env) == []


@pytest.mark.parametrize("device_id", [999, 0, 10 ** 30])
def test_an_admin_retiring_a_missing_machine_is_a_404(client, people, device_id):
    r = Browser(people["boss"]["token"]).post(retire_path(device_id))

    assert r.status_code == 404
    assert _events(client.env) == []


def test_retire_is_a_gated_write(client, people):
    """No csrf → refused before the handler; a GET is not a route that writes."""
    dev1 = people["dev1"]
    browser = Browser(dev1["token"])
    r = browser.client.post(retire_path(dev1["device_id"]),
                            headers={"Origin": PUBLIC_URL, "Fly-Client-IP": _ip()},
                            follow_redirects=False)
    assert r.status_code == 403 and "bad_csrf" in r.text
    assert browser.get(retire_path(dev1["device_id"])).status_code in (404, 405)
    assert _status(client, dev1["token"]) == 200
    assert _events(client.env) == []


# --------------------------------------------------------------------------- #
# The in-transaction re-check (§6.4; the harness of test_write_races.py)       #
# --------------------------------------------------------------------------- #
def _post_racing(path: str, cookie: str, change) -> tuple[int, str]:
    """POST ``path`` (with the session's csrf) straight through the ASGI app from the
    server's own origin; ``change`` commits after the gate has read the session and
    before the app receives the body."""
    from server.app import app

    csrf = hmac.new(cookie.encode(), b"csrf", hashlib.sha256).hexdigest()
    body = urlencode({"csrf": csrf}).encode()
    sent = asyncio.Event()
    ip = _ip().encode()

    async def receive():
        if not sent.is_set():
            sent.set()
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
        "headers": [(b"host", PUBLIC_HOST.encode()), (b"origin", PUBLIC_URL.encode()),
                    (b"fly-client-ip", ip),
                    (b"content-type", b"application/x-www-form-urlencoded"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cookie", f"loom_ui={cookie}".encode())],
    }
    asyncio.run(app(scope, receive, send))
    return out["status"], out["body"].decode("utf-8", "replace")


def test_a_retire_whose_session_was_revoked_mid_request_writes_nothing(client, people):
    """The boss's machine is revoked after the gate let the retire of dev2's in: the write
    is refused as the gate would refuse it (401) and dev2's machine lives."""
    boss, dev2 = people["boss"], people["dev2"]
    cookie = Browser(boss["token"]).cookie
    after_change = {}

    def change():
        conn = db.connect(client.env)
        try:
            assert auth.revoke(conn, boss["device_id"]) == 1
        finally:
            conn.close()
        after_change.update(_snapshot(client.env))

    status, page = _post_racing(retire_path(dev2["device_id"]), cookie, change)

    assert status == 401, page[:400]
    assert "loom ui" in page
    assert _snapshot(client.env) == after_change
    assert _status(client, dev2["token"]) == 200


def test_an_admin_demoted_mid_request_cannot_retire_another_users_machine(client, people,
                                                                         capsys):
    boss, dev2 = people["boss"], people["dev2"]
    assert admin.main(["promote", "--email", dev2["email"]]) == 0   # boss is not the last
    capsys.readouterr()
    cookie = Browser(boss["token"]).cookie
    after_change = {}

    def change():
        conn = db.connect(client.env)
        try:
            conn.execute("UPDATE users SET role = 'member' WHERE id = ?", (boss["user_id"],))
            conn.commit()
        finally:
            conn.close()
        after_change.update(_snapshot(client.env))

    status, page = _post_racing(retire_path(dev2["device_id"]), cookie, change)

    assert status == 404, page[:400]
    assert _snapshot(client.env) == after_change


def test_the_racing_harness_lets_an_untouched_retire_through(client, people):
    """The control for the two tests above."""
    dev2 = people["dev2"]
    status, _ = _post_racing(retire_path(dev2["device_id"]),
                             Browser(people["boss"]["token"]).cookie, lambda: None)
    assert status == 303
    assert _status(client, dev2["token"]) == 401


# --------------------------------------------------------------------------- #
# admin.py revokedevice --no-cascade                                           #
# --------------------------------------------------------------------------- #
def test_revokedevice_no_cascade_retires_from_the_shell(client, capsys, replacement):
    old = replacement["old"]
    pending = replacement["browser"].setup_code()

    assert admin.main(["revokedevice", "--id", str(old["device_id"]), "--no-cascade"]) == 0

    out = capsys.readouterr().out
    assert f"Device token {old['device_id']} and its renewal chain retired (1 token(s);" in out
    assert _status(client, old["token"]) == 401
    assert _status(client, replacement["new_token"]) == 200
    assert _enrol(client, pending, "probe").status_code == 401
    assert _events(client.env) == []                    # admin.py keeps phase 1's rule


def test_revokedevice_without_the_flag_still_cascades(client, capsys, replacement):
    old = replacement["old"]

    assert admin.main(["revokedevice", "--id", str(old["device_id"])]) == 0

    out = capsys.readouterr().out
    assert f"Device token {old['device_id']} and its renewal chain revoked (2 token(s);" in out
    assert _status(client, old["token"]) == 401
    assert _status(client, replacement["new_token"]) == 401


def test_revokedevice_no_cascade_on_a_dead_or_unknown_chain_fails(client, capsys, people):
    dev1 = people["dev1"]
    assert admin.main(["revokedevice", "--id", "999", "--no-cascade"]) == 1
    assert admin.main(["revokedevice", "--id", str(dev1["device_id"]), "--no-cascade"]) == 0
    capsys.readouterr()
    assert admin.main(["revokedevice", "--id", str(dev1["device_id"]), "--no-cascade"]) == 1
    assert "error: no unrevoked device token in the chain of id" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# The forms                                                                    #
# --------------------------------------------------------------------------- #
def _form_action(page: str, path: str) -> bool:
    return re.search(r"action=\"" + re.escape(path) + "\"", page) is not None


def test_my_machines_has_retire_and_revoke_forms_per_live_chain(client, capsys, people):
    dev1 = people["dev1"]
    _, dead_id = _machine(client, _invite_code(capsys, dev1["email"]), "dev1-dead")
    assert admin.main(["revokedevice", "--id", str(dead_id)]) == 0

    text = Browser(dev1["token"]).get("/ui/machines").text

    assert _form_action(text, retire_path(dev1["device_id"]))
    assert _form_action(text, revoke_path(dev1["device_id"]))
    assert "Retire" in text and "Revoke (lost or stolen)" in text
    assert not _form_action(text, retire_path(dead_id))
    assert not _form_action(text, revoke_path(dead_id))


def test_admin_users_has_retire_and_revoke_forms_per_live_machine(client, people):
    text = Browser(people["boss"]["token"]).get("/ui/admin/users").text

    for name in ("dev1", "dev2", "boss"):
        assert _form_action(text, retire_path(people[name]["device_id"])), name
        assert _form_action(text, revoke_path(people[name]["device_id"])), name
    assert "Revoke (lost or stolen)" in text
