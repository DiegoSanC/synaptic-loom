"""*My machines*, setup codes and the web's revoke (D20, DESIGN §6.4; plan 2026-10-19,
phase 2 slice 3).

Expected values are §6.4's literals: one row per chain of the caller with its name,
created, last renewed, expiry and state; a setup code lives 10 minutes
(`LOOM_SETUP_CODE_TTL_S`), is single use and is shown once with its `/ui/join#code=…`
link; a revoke reaches the chain of any of its ids, `303`s to `/ui/machines` (own chain)
or `/ui/admin/users`, and is a `404` for an unknown id or another user's (unless the
caller is an admin). The `env` fixture runs as Fly does, so the public URL is
`https://loom-test.fly.dev` and the client address is `Fly-Client-IP`.

Every machine here is enrolled through the shipped path (`admin.py invite` →
`/api/enroll`) and every browser signs in through `loom ui`'s (`/api/ui-login` →
`/ui/session`), so `ui_sessions.device_token_id` is the real one.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from datetime import datetime
from itertools import count

import pytest
from fastapi.testclient import TestClient

from server import admin, db

PUBLIC_URL = "https://loom-test.fly.dev"          # https://$FLY_APP_NAME.fly.dev (D19)
LINK_RE = re.compile(re.escape(PUBLIC_URL) + r"/ui/join#code=([A-Za-z0-9_-]{32,})")
CODE_LINE = re.compile(r"^\s+([A-Za-z0-9_-]{32,})$")
SETUP_CODE = "/ui/machines/setup-code"
SETUP_TTL_S = 600                                 # §6.4: 10 min
HOSTILE = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'
_IPS = count(1)


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


def _invite_code(capsys, email: str, name: str | None = None) -> str:
    argv = ["invite", "--email", email] + (["--name", name] if name else [])
    assert admin.main(argv) == 0
    return next(m.group(1) for line in capsys.readouterr().out.splitlines()
                if (m := CODE_LINE.match(line)))


def _enroll(client, code: str, device_name: str) -> tuple[str, int]:
    r = client.post("/api/enroll", json={"code": code, "device_name": device_name},
                    headers={"Fly-Client-IP": _ip()})
    assert r.status_code == 201, r.text
    return r.json()["token"], _query(client.env, "SELECT id FROM device_tokens "
                                                 "WHERE token_hash = ?",
                                     db.hash_token(r.json()["token"]))[0]["id"]


def _renew(client, token: str) -> str:
    r = client.post("/api/device-tokens/renew", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 201, r.text
    return r.json()["token"]


def _status(client, token: str) -> int:
    return client.get("/api/projects", headers={"Authorization": f"Bearer {token}",
                                                "Fly-Client-IP": _ip()}).status_code


class Browser:
    """One signed-in `loom_ui` session, with its own cookie jar, posting as a same-origin
    page does (Origin = the public URL, the csrf in the form)."""

    def __init__(self, bearer: str):
        from server.app import app

        self.client = TestClient(app, base_url="https://testserver")
        code = self.client.post("/api/ui-login",
                                headers={"Authorization": f"Bearer {bearer}"}).json()["code"]
        assert self.client.post("/ui/session", json={"code": code}).status_code == 204
        cookie = self.client.cookies["loom_ui"]
        self.csrf = hmac.new(cookie.encode(), b"csrf", hashlib.sha256).hexdigest()
        self.last_ip: str | None = None

    def get(self, path: str):
        return self.client.get(path, headers={"Fly-Client-IP": _ip()})

    def post(self, path: str, fields: dict | None = None):
        self.last_ip = _ip()
        return self.client.post(path, data={"csrf": self.csrf, **(fields or {})},
                                headers={"Origin": PUBLIC_URL, "Fly-Client-IP": self.last_ip},
                                follow_redirects=False)


def _setup_code(browser: Browser) -> str:
    r = browser.post(SETUP_CODE)
    assert r.status_code == 200, r.text
    match = LINK_RE.search(r.text)
    assert match, r.text
    return match.group(1)


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
        token, device_id = _enroll(client, _invite_code(capsys, email, name), f"{name}-laptop")
        user_id = _query(client.env, "SELECT id FROM users WHERE email = ?", email)[0]["id"]
        out[name] = {"email": email, "token": token, "device_id": device_id,
                     "user_id": user_id}
    assert admin.main(["promote", "--email", "boss@example.test"]) == 0
    capsys.readouterr()
    return out


def _segment(page: str, start: str) -> str:
    """One table row of the page: from the `<tr` before `start` to its `</tr>`."""
    i = page.index(start)
    return page[page.rindex("<tr", 0, i):page.index("</tr>", i)]


def _form_action(page: str, path: str) -> bool:
    return re.search(r"action=\"" + re.escape(path) + "\"", page) is not None


# --------------------------------------------------------------------------- #
# GET /ui/machines                                                             #
# --------------------------------------------------------------------------- #
def test_my_machines_has_one_row_per_chain_of_the_caller_only(client, capsys, people):
    dev1 = people["dev1"]
    laptop2 = _renew(client, dev1["token"])
    _renew(client, laptop2)                                  # three tokens, one chain
    old_token, old_id = _enroll(client, _invite_code(capsys, dev1["email"]), "dev1-old")
    assert admin.main(["revokedevice", "--id", str(old_id)]) == 0

    page = Browser(dev1["token"]).get("/ui/machines")

    assert page.status_code == 200
    text = page.text
    assert "dev2-laptop" not in text and "boss-laptop" not in text
    assert len(re.findall(r"<tr data-chain=", text)) == 2
    laptop = _segment(text, "dev1-laptop")
    tokens = _query(client.env, "SELECT issued_at, expires_at FROM device_tokens "
                                "WHERE user_id = ? AND device_name = 'dev1-laptop' "
                                "ORDER BY id", dev1["user_id"])
    assert len(tokens) == 3
    created, last_renewed = tokens[0]["issued_at"], tokens[-1]["issued_at"]
    assert re.search(r"data-col=\"created\">" + re.escape(created), laptop)
    assert re.search(r"data-col=\"renewed\">" + re.escape(last_renewed), laptop)
    assert re.search(r"data-col=\"expires\">" + re.escape(tokens[-1]["expires_at"]), laptop)
    assert "data-col=\"state\">live" in laptop
    assert "data-col=\"state\">revoked" in _segment(text, "dev1-old")
    # A Revoke form per LIVE chain only; any id of the chain names it.
    assert _form_action(laptop, revoke_path(dev1["device_id"]))
    assert "/revoke" not in _segment(text, "dev1-old")
    assert _form_action(text, SETUP_CODE)
    assert text.count('name="csrf"') >= 3                    # sign-out, add, revoke


def test_an_expired_chain_says_so_and_has_no_revoke_form(client, capsys, people, monkeypatch):
    monkeypatch.setenv("LOOM_DEVICE_TOKEN_TTL_S", "1")
    _enroll(client, _invite_code(capsys, people["dev1"]["email"]), "dev1-short")
    time.sleep(1.1)

    text = Browser(people["dev1"]["token"]).get("/ui/machines").text

    row = _segment(text, "dev1-short")
    assert "data-col=\"state\">expired" in row and "/revoke" not in row


def test_a_hostile_device_name_is_escaped(client, capsys, people):
    _enroll(client, _invite_code(capsys, people["dev1"]["email"]), HOSTILE)

    text = Browser(people["dev1"]["token"]).get("/ui/machines").text

    assert "<script>alert(1)</script>" not in text
    assert "<img src=x onerror=alert(2)>" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;&#34;&gt;&lt;img" in text


# --------------------------------------------------------------------------- #
# POST /ui/machines/setup-code                                                 #
# --------------------------------------------------------------------------- #
def test_add_a_machine_issues_a_setup_code_shown_once(client, people):
    dev1 = people["dev1"]
    browser = Browser(dev1["token"])

    r = browser.post(SETUP_CODE)

    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    code = LINK_RE.search(r.text).group(1)
    assert r.text.count(code) >= 2                           # the code, and its link
    row = _query(client.env, "SELECT * FROM enrolment_codes WHERE code_hash = ?",
                 db.hash_token(code))
    assert len(row) == 1
    row = row[0]
    assert row["kind"] == "setup" and row["user_id"] == dev1["user_id"]
    assert row["created_by_user_id"] == dev1["user_id"]
    assert row["issued_by_device_token_id"] == dev1["device_id"]
    assert row["redeemed_at"] is None
    ttl = (datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
           - datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")))
    assert ttl.total_seconds() == SETUP_TTL_S
    events = _query(client.env, "SELECT * FROM auth_events WHERE event = 'setup_code_issued'")
    assert [(e["actor_user_id"], e["target_type"], e["target_id"], e["client_ip"])
            for e in events] == [(dev1["user_id"], "user", dev1["user_id"], browser.last_ip)]
    assert code not in browser.get("/ui/machines").text


def test_the_setup_code_redeems_once_through_api_enroll_for_the_same_user(client, people):
    dev1 = people["dev1"]
    code = _setup_code(Browser(dev1["token"]))

    token, device_id = _enroll(client, code, "dev1-desk")

    assert _status(client, token) == 200
    assert _query(client.env, "SELECT user_id FROM device_tokens WHERE id = ?",
                  device_id) == [{"user_id": dev1["user_id"]}]
    replay = client.post("/api/enroll", json={"code": code, "device_name": "again"},
                         headers={"Fly-Client-IP": _ip()})
    assert replay.status_code == 401


def test_the_setup_code_ttl_is_loom_setup_code_ttl_s(client, people, monkeypatch):
    monkeypatch.setenv("LOOM_SETUP_CODE_TTL_S", "1")
    browser = Browser(people["dev1"]["token"])
    stale = _setup_code(browser)
    time.sleep(2.1)

    late = client.post("/api/enroll", json={"code": stale, "device_name": "late"},
                       headers={"Fly-Client-IP": _ip()})

    assert late.status_code == 401
    _enroll(client, _setup_code(browser), "quick")


def test_a_permanent_token_session_issues_codes_with_no_issuing_device(client, user_factory):
    user = user_factory("legacy")
    code = _setup_code(Browser(user["token"]))

    row = _query(client.env, "SELECT * FROM enrolment_codes WHERE code_hash = ?",
                 db.hash_token(code))[0]

    assert row["kind"] == "setup" and row["user_id"] == user["id"]
    assert row["created_by_user_id"] == user["id"]
    assert row["issued_by_device_token_id"] is None


# --------------------------------------------------------------------------- #
# POST /ui/devices/{id}/revoke                                                 #
# --------------------------------------------------------------------------- #
def _revoked_events(env, device_id: int) -> list[dict]:
    return _query(env, "SELECT * FROM auth_events WHERE event = 'device_revoked' "
                       "AND target_type = 'device' AND target_id = ?", device_id)


@pytest.mark.parametrize("named", ["first", "renewed"])
def test_revoking_any_id_of_an_own_chain_redirects_to_my_machines(client, capsys, people,
                                                                 named):
    dev1 = people["dev1"]
    spare, spare_id = _enroll(client, _invite_code(capsys, dev1["email"]), "dev1-spare")
    successor = _renew(client, spare)
    successor_id = _query(client.env, "SELECT id FROM device_tokens WHERE token_hash = ?",
                          db.hash_token(successor))[0]["id"]
    target = {"first": spare_id, "renewed": successor_id}[named]
    browser = Browser(dev1["token"])

    r = browser.post(revoke_path(target))

    assert r.status_code == 303 and r.headers["location"].endswith("/ui/machines")
    assert _status(client, spare) == 401 and _status(client, successor) == 401
    assert _status(client, dev1["token"]) == 200
    assert [(e["actor_user_id"], e["client_ip"]) for e in _revoked_events(client.env, target)
            ] == [(dev1["user_id"], browser.last_ip)]
    assert "data-col=\"state\">revoked" in _segment(browser.get("/ui/machines").text,
                                                    "dev1-spare")


def test_a_member_gets_404_for_anothers_device_or_an_unknown_id(client, people):
    browser = Browser(people["dev1"]["token"])

    for device_id in (people["dev2"]["device_id"], people["boss"]["device_id"], 987654321):
        assert browser.post(revoke_path(device_id)).status_code == 404

    assert _status(client, people["dev2"]["token"]) == 200
    assert _status(client, people["boss"]["token"]) == 200
    assert _query(client.env, "SELECT * FROM auth_events WHERE event = 'device_revoked'") == []


def test_an_admin_revokes_another_users_machine_and_lands_on_admin_users(client, capsys,
                                                                        people):
    boss, dev2 = people["boss"], people["dev2"]
    browser = Browser(boss["token"])

    r = browser.post(revoke_path(dev2["device_id"]))

    assert r.status_code == 303 and r.headers["location"].endswith("/ui/admin/users")
    assert _status(client, dev2["token"]) == 401
    assert [e["actor_user_id"] for e in _revoked_events(client.env, dev2["device_id"])
            ] == [boss["user_id"]]
    _, own_spare = _enroll(client, _invite_code(capsys, boss["email"]), "boss-spare")
    own = browser.post(revoke_path(own_spare))
    assert own.status_code == 303 and own.headers["location"].endswith("/ui/machines")
    assert browser.post(revoke_path(987654321)).status_code == 404


def test_revoking_the_chain_this_session_runs_on_ends_the_session(client, people):
    dev1 = people["dev1"]
    browser = Browser(dev1["token"])

    r = browser.post(revoke_path(dev1["device_id"]))

    assert r.status_code == 303 and r.headers["location"].endswith("/ui/machines")
    assert _status(client, dev1["token"]) == 401
    assert browser.get("/ui/machines").status_code == 401


def test_revoking_a_dead_chain_changes_nothing_and_records_nothing(client, people):
    dev1 = people["dev1"]
    _, spare_id = _enroll(client, _setup_code(Browser(dev1["token"])), "dev1-spare")
    assert admin.main(["revokedevice", "--id", str(spare_id)]) == 0

    r = Browser(dev1["token"]).post(revoke_path(spare_id))

    assert r.status_code == 303
    assert _revoked_events(client.env, spare_id) == []


def test_the_web_revoke_runs_the_issuer_cascade(client, capsys, people):
    """§6.4 Issuer cascade "through the web's revoke": A's session issued B's setup code,
    B's issued C's; revoking A from another machine of the same user (one that came from
    an `admin.py` invitation, so outside the cascade) ends A, B and C and B's pending
    code, and nothing else."""
    dev1 = people["dev1"]
    a = Browser(dev1["token"])
    b_token, _ = _enroll(client, _setup_code(a), "dev1-b")
    b = Browser(b_token)
    c_token, _ = _enroll(client, _setup_code(b), "dev1-c")
    pending = _setup_code(b)
    bystander_token, _ = _enroll(client, _invite_code(capsys, dev1["email"]), "dev1-z")
    bystander = Browser(bystander_token)

    r = bystander.post(revoke_path(dev1["device_id"]))

    assert r.status_code == 303 and r.headers["location"].endswith("/ui/machines")
    assert [_status(client, t) for t in (dev1["token"], b_token, c_token)] == [401] * 3
    assert a.get("/ui/machines").status_code == 401
    assert b.get("/ui/machines").status_code == 401
    assert client.post("/api/enroll", json={"code": pending, "device_name": "late"},
                       headers={"Fly-Client-IP": _ip()}).status_code == 401
    assert _status(client, bystander_token) == 200
    assert bystander.get("/ui/machines").status_code == 200
    assert len(_revoked_events(client.env, dev1["device_id"])) == 1


# --------------------------------------------------------------------------- #
# The new routes accept nothing else                                           #
# --------------------------------------------------------------------------- #
def test_a_get_to_a_write_route_changes_nothing(client, people):
    dev1 = people["dev1"]
    browser = Browser(dev1["token"])

    assert browser.get(SETUP_CODE).status_code in (404, 405)
    assert browser.get(revoke_path(dev1["device_id"])).status_code in (404, 405)

    assert _query(client.env, "SELECT id FROM enrolment_codes WHERE kind = 'setup'") == []
    assert _status(client, dev1["token"]) == 200


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_my_machines_refuses_every_write_method(client, people, method):
    browser = Browser(people["dev1"]["token"])
    r = browser.client.request(method, "/ui/machines", data={"csrf": browser.csrf},
                               headers={"Origin": PUBLIC_URL})
    assert r.status_code == 405
