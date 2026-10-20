"""Browser login for the read-only UI (D14, plan 2026-09-29 slice 1: AC-1…AC-5).

Expected values come from D14 and the plan, not from the implementation: a login code is
minted only for a valid bearer, works once, lives 120 s; a UI session lives 7 days, renewed
as it is used (at most once an hour), never outlives the device token that asked for it, and dies on `deactivate` / `revokedevice`;
the `loom_ui` cookie opens `/ui` only, and a bearer never does.

Expiry is REAL time, never a mocked clock (the `test_enrol.py` rule): the TTL overrides
plus a sleep. The client talks HTTPS to the app so the `Secure` cookie round-trips in the
real cookie jar instead of being hand-injected.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta

from server import admin, db

AUDIT_LOGGER = "uvicorn.error.loom.security"
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{32,}$")
SEVEN_DAYS_S = 7 * 86400


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _row(env, sql, *params):
    conn = db.connect(env)
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def _rows(env, sql, *params):
    conn = db.connect(env)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _device_token(ui, capsys, email="dev2@example.test", name="dev2", device="mac-1") -> str:
    """A real device token: operator invite, then the anonymous redemption."""
    argv = ["invite", "--email", email] + (["--name", name] if name else [])
    assert admin.main(argv) == 0
    out = capsys.readouterr().out
    code = next(line.strip() for line in out.splitlines() if CODE_RE.fullmatch(line.strip()))
    r = ui.post("/api/enroll", json={"code": code, "device_name": device})
    assert r.status_code == 201, r.text
    return r.json()["token"]


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _login_code(ui, token: str) -> str:
    r = ui.post("/api/ui-login", headers=_bearer(token))
    assert r.status_code == 201, r.text
    return r.json()["code"]


def _open_session(ui, code: str, **headers):
    return ui.post("/ui/session", json={"code": code}, headers=headers)


def _signed_in(ui, capsys, **kwargs) -> tuple[str, str]:
    """(device token, session cookie value) for a freshly opened UI session."""
    token = _device_token(ui, capsys, **kwargs)
    r = _open_session(ui, _login_code(ui, token))
    assert r.status_code == 204, r.text
    return token, r.cookies["loom_ui"]


def _audit_lines(caplog) -> list[str]:
    return [rec.getMessage() for rec in caplog.records if rec.name == AUDIT_LOGGER]


# --------------------------------------------------------------------------- #
# AC-1 — POST /api/ui-login and the single-use, short-lived code               #
# --------------------------------------------------------------------------- #
def test_ui_login_needs_a_valid_bearer(ui):
    assert ui.post("/api/ui-login").status_code == 401
    assert ui.post("/api/ui-login", headers=_bearer("not-a-token")).status_code == 401


def test_ui_login_issues_a_120_second_code_stored_only_as_its_hash(ui, env, capsys):
    token = _device_token(ui, capsys)

    r = ui.post("/api/ui-login", headers=_bearer(token))

    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"code", "expires_at"}
    assert CODE_RE.fullmatch(body["code"])
    row = _row(env, "SELECT * FROM ui_login_codes WHERE code_hash = ?", db.hash_token(body["code"]))
    assert row is not None and row["redeemed_at"] is None
    assert _ts(row["expires_at"]) - _ts(row["created_at"]) == timedelta(seconds=120)
    assert row["expires_at"] == body["expires_at"]
    device = _row(env, "SELECT id, user_id FROM device_tokens WHERE token_hash = ?",
                  db.hash_token(token))
    assert (row["user_id"], row["device_token_id"]) == (device["user_id"], device["id"])


def test_a_code_opens_exactly_one_session(ui, env, capsys):
    code = _login_code(ui, _device_token(ui, capsys))

    first = _open_session(ui, code)
    again = _open_session(ui, code)

    assert first.status_code == 204, first.text
    assert again.status_code == 401
    assert again.json()["error"] == "invalid_code"
    assert len(_rows(env, "SELECT id FROM ui_sessions")) == 1


def test_a_code_past_its_life_is_refused_after_real_time_passes(ui, env, capsys, monkeypatch):
    monkeypatch.setenv("LOOM_UI_LOGIN_CODE_TTL_S", "1")
    code = _login_code(ui, _device_token(ui, capsys))
    time.sleep(1.1)

    r = _open_session(ui, code)

    assert r.status_code == 401
    assert r.json()["error"] == "invalid_code"
    assert _rows(env, "SELECT id FROM ui_sessions") == []


def test_a_code_of_a_deactivated_user_or_a_revoked_device_is_the_same_401(ui, env, capsys):
    token = _device_token(ui, capsys)
    code_a, code_b = _login_code(ui, token), _login_code(ui, token)
    other = _device_token(ui, capsys, email="dev3@example.test", name="dev3", device="wsl-3")
    code_c = _login_code(ui, other)

    device_id = _row(env, "SELECT id FROM device_tokens WHERE token_hash = ?", db.hash_token(token))["id"]
    assert admin.main(["revokedevice", "--id", str(device_id)]) == 0
    revoked = _open_session(ui, code_a)
    assert admin.main(["deactivate", "--email", "dev3@example.test"]) == 0
    deactivated = _open_session(ui, code_c)

    for r in (revoked, deactivated):
        assert r.status_code == 401
        assert r.json()["error"] == "invalid_code"
    assert _rows(env, "SELECT id FROM ui_sessions") == []
    # The refused code was not burned by the refusal (nothing was redeemed).
    assert _row(env, "SELECT redeemed_at FROM ui_login_codes WHERE code_hash = ?",
                db.hash_token(code_b))["redeemed_at"] is None


def test_open_session_validates_its_body(ui):
    assert ui.post("/ui/session", json={}).status_code == 422
    assert ui.post("/ui/session", json={"code": ""}).status_code == 422


# --------------------------------------------------------------------------- #
# AC-2 — rate limit and audit, never a secret in the log                       #
# --------------------------------------------------------------------------- #
def test_failed_session_openings_from_one_ip_are_rate_limited(ui, capsys):
    headers = {"Fly-Client-IP": "192.0.2.90"}
    for _ in range(10):
        assert _open_session(ui, "wrong-code-wrong-code-wrong-code-000", **headers).status_code == 401

    limited = _open_session(ui, "wrong-code-wrong-code-wrong-code-000", **headers)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"]

    # D17: a VALID code from the throttled address opens its session, and the window stays
    # full for the next failure; another address has its own window.
    token = _device_token(ui, capsys)
    code = _login_code(ui, token)
    assert _open_session(ui, code, **headers).status_code == 204
    assert _open_session(ui, "wrong-code-wrong-code-wrong-code-000", **headers).status_code == 429
    other = {"Fly-Client-IP": "192.0.2.91"}
    assert _open_session(ui, code, **other).status_code == 401   # spent, for everyone
    assert _open_session(ui, _login_code(ui, token), **other).status_code == 204


def test_every_outcome_is_audited_without_code_cookie_or_session_id(ui, env, capsys, caplog):
    token = _device_token(ui, capsys)
    bad = "not-a-real-code-never-log-this-value-xyz"
    ip = {"Fly-Client-IP": "192.0.2.92"}
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        code = ui.post("/api/ui-login", headers={**_bearer(token), **ip}).json()["code"]
        opened = _open_session(ui, code, **ip)
        _open_session(ui, bad, **ip)
        cookie = opened.cookies["loom_ui"]
        ui.cookies.clear()
        ui.get("/ui/", headers={"Cookie": "loom_ui=forged-session-value-never-logged", **ip})

    lines = _audit_lines(caplog)
    device_id = _row(env, "SELECT id FROM device_tokens")["id"]
    session_id = _row(env, "SELECT id FROM ui_sessions")["id"]
    issued = [line for line in lines if "outcome=ui_login_code_issued" in line]
    assert issued and "user_id=1" in issued[0] and f"device_token_id={device_id}" in issued[0]
    assert "client_ip=192.0.2.92" in issued[0]
    opened_lines = [line for line in lines if "outcome=ui_session_opened" in line]
    assert opened_lines and f"ui_session_id={session_id}" in opened_lines[0]
    assert any("outcome=ui_login_invalid_code" in line for line in lines)
    assert any("outcome=ui_invalid_session" in line for line in lines)
    for secret in (token, code, bad, cookie, "forged-session-value-never-logged",
                   db.hash_token(cookie), db.hash_token(code)):
        assert all(secret not in line for line in lines)


# --------------------------------------------------------------------------- #
# AC-3 — the cookie                                                            #
# --------------------------------------------------------------------------- #
def _set_cookie(response) -> str:
    return response.headers["set-cookie"]


def test_the_session_cookie_is_httponly_secure_strict_and_scoped_to_ui(ui, env, capsys):
    code = _login_code(ui, _device_token(ui, capsys))

    r = _open_session(ui, code)

    header = _set_cookie(r)
    attrs = [part.strip().lower() for part in header.split(";")]
    assert attrs[0].startswith("loom_ui=")
    assert "httponly" in attrs and "secure" in attrs
    assert "samesite=strict" in attrs and "path=/ui" in attrs
    max_age = int(next(a for a in attrs if a.startswith("max-age=")).split("=")[1])
    assert SEVEN_DAYS_S - 5 <= max_age <= SEVEN_DAYS_S
    session = _row(env, "SELECT * FROM ui_sessions")
    assert _ts(session["expires_at"]) - _ts(session["created_at"]) == timedelta(days=7)
    # Only the hash is stored.
    assert session["session_hash"] == db.hash_token(r.cookies["loom_ui"])


def test_a_session_never_outlives_the_device_token_that_asked_for_it(ui, env, capsys, monkeypatch):
    monkeypatch.setenv("LOOM_DEVICE_TOKEN_TTL_S", "600")
    token = _device_token(ui, capsys)

    r = _open_session(ui, _login_code(ui, token))

    attrs = [part.strip().lower() for part in _set_cookie(r).split(";")]
    max_age = int(next(a for a in attrs if a.startswith("max-age=")).split("=")[1])
    assert 590 <= max_age <= 600
    device = _row(env, "SELECT expires_at FROM device_tokens")
    assert _row(env, "SELECT expires_at FROM ui_sessions")["expires_at"] == device["expires_at"]


# --------------------------------------------------------------------------- #
# AC-4 — a session ends                                                        #
# --------------------------------------------------------------------------- #
def test_a_session_opens_ui_and_names_who_is_signed_in(ui, capsys):
    _signed_in(ui, capsys)
    r = ui.get("/ui/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "dev2" in r.text


def test_deactivating_the_user_ends_the_session_on_the_next_request(ui, capsys, caplog):
    _signed_in(ui, capsys)
    assert ui.get("/ui/").status_code == 200

    assert admin.main(["deactivate", "--email", "dev2@example.test"]) == 0
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        r = ui.get("/ui/")

    assert r.status_code == 401
    assert any("outcome=ui_deactivated_user" in line for line in _audit_lines(caplog))


def test_revoking_the_originating_device_ends_its_sessions_only(ui, env, capsys, caplog):
    stolen_token, stolen_cookie = _signed_in(ui, capsys, device="laptop-stolen")
    _, desk_cookie = _signed_in(ui, capsys, name=None, device="desk")
    ui.cookies.clear()
    stolen = _row(env, "SELECT id FROM device_tokens WHERE device_name = ?", "laptop-stolen")["id"]

    assert admin.main(["revokedevice", "--id", str(stolen)]) == 0
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        r = ui.get("/ui/", headers={"Cookie": f"loom_ui={stolen_cookie}"})

    assert r.status_code == 401
    assert any("outcome=ui_revoked_device" in line for line in _audit_lines(caplog))
    assert ui.get("/ui/", headers={"Cookie": f"loom_ui={desk_cookie}"}).status_code == 200


def test_a_session_past_its_life_is_refused_after_real_time_passes(ui, capsys, monkeypatch, caplog):
    monkeypatch.setenv("LOOM_UI_SESSION_TTL_S", "1")
    _, cookie = _signed_in(ui, capsys)
    time.sleep(1.1)
    # The jar already dropped it (Max-Age=1); a client that keeps it must still be refused
    # by the SERVER, so present it by hand.
    ui.cookies.clear()

    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        r = ui.get("/ui/", headers={"Cookie": f"loom_ui={cookie}"})

    assert r.status_code == 401
    assert any("outcome=ui_expired_session" in line for line in _audit_lines(caplog))


def test_a_legacy_adduser_token_can_sign_in_and_deactivation_still_ends_it(ui, capsys):
    admin.main(["adduser", "--name", "legacy", "--email", "legacy@x.com"])
    out = capsys.readouterr().out
    legacy = next(line.strip() for line in out.splitlines() if CODE_RE.fullmatch(line.strip()))

    assert _open_session(ui, _login_code(ui, legacy)).status_code == 204
    assert ui.get("/ui/").status_code == 200
    admin.main(["deactivate", "--email", "legacy@x.com"])
    assert ui.get("/ui/").status_code == 401


# --------------------------------------------------------------------------- #
# AC-5 — two credentials, two surfaces                                         #
# --------------------------------------------------------------------------- #
def test_ui_without_a_session_is_401_html_whether_or_not_the_page_exists(ui):
    for path in ("/ui/", "/ui/projects/1", "/ui/no-such-page"):
        r = ui.get(path)
        assert r.status_code == 401, path
        assert "text/html" in r.headers["content-type"]
        assert "loom ui" in r.text


def test_a_bearer_does_not_open_ui(ui, capsys):
    token = _device_token(ui, capsys)
    assert ui.get("/ui/", headers=_bearer(token)).status_code == 401


def test_a_session_cookie_does_not_open_the_api(ui, capsys):
    _, cookie = _signed_in(ui, capsys)
    ui.cookies.clear()
    r = ui.get("/api/projects", headers={"Cookie": f"loom_ui={cookie}"})
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"


def test_a_session_does_not_turn_unknown_ui_pages_into_anything_but_404(ui, capsys):
    _signed_in(ui, capsys)
    assert ui.get("/ui/no-such-page").status_code == 404


def test_only_get_and_head_reach_ui_pages_even_with_a_session(ui, capsys):
    _signed_in(ui, capsys)
    assert ui.head("/ui/").status_code == 200
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert ui.request(method, "/ui/").status_code == 405, method


# --------------------------------------------------------------------------- #
# The login page (public)                                                      #
# --------------------------------------------------------------------------- #
def test_the_login_page_and_its_script_are_public_and_never_read_a_code(ui):
    page = ui.get("/ui/login")
    assert page.status_code == 200
    assert '<script src="/ui/static/login.js"></script>' in page.text
    script = ui.get("/ui/static/login.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    # The code travels in the fragment and is posted, never put in a query string.
    assert "location.hash" in script.text and "/ui/session" in script.text
    assert "replaceState" in script.text
    icon = ui.get("/ui/static/favicon.svg")
    assert icon.status_code == 200 and "image/svg+xml" in icon.headers["content-type"]


# --------------------------------------------------------------------------- #
# The session slides (owner decision of 2026-10-20, DESIGN §6.3)                #
# --------------------------------------------------------------------------- #
def _set_expiry(env, moment: datetime) -> None:
    conn = db.connect(env)
    try:
        conn.execute("UPDATE ui_sessions SET expires_at = ?",
                     (moment.strftime("%Y-%m-%dT%H:%M:%SZ"),))
        conn.commit()
    finally:
        conn.close()


def _cookie_max_age(r) -> int | None:
    header = r.headers.get("set-cookie")
    if not header or not header.lower().startswith("loom_ui="):
        return None
    attrs = [part.strip().lower() for part in header.split(";")]
    return int(next(a for a in attrs if a.startswith("max-age=")).split("=")[1])


def test_a_used_session_is_renewed_to_seven_days_with_its_cookie(ui, env, capsys):
    _signed_in(ui, capsys)
    _set_expiry(env, datetime.utcnow() + timedelta(days=7, hours=-2))

    r = ui.get("/ui/")

    assert r.status_code == 200
    assert SEVEN_DAYS_S - 60 <= _cookie_max_age(r) <= SEVEN_DAYS_S
    attrs = [part.strip().lower() for part in r.headers["set-cookie"].split(";")]
    assert "httponly" in attrs and "secure" in attrs
    assert "samesite=strict" in attrs and "path=/ui" in attrs
    left = _ts(_row(env, "SELECT expires_at FROM ui_sessions")["expires_at"]) - _ts(
        datetime.utcnow().isoformat() + "Z")
    assert timedelta(days=7) - timedelta(minutes=1) <= left <= timedelta(days=7)


def test_a_session_renewed_within_the_hour_is_not_written_again(ui, env, capsys):
    _signed_in(ui, capsys)
    before = _row(env, "SELECT expires_at FROM ui_sessions")["expires_at"]

    r = ui.get("/ui/")

    assert r.status_code == 200
    assert _cookie_max_age(r) is None
    assert _row(env, "SELECT expires_at FROM ui_sessions")["expires_at"] == before


def test_a_renewal_never_outlives_the_device_token(ui, env, capsys, monkeypatch):
    monkeypatch.setenv("LOOM_DEVICE_TOKEN_TTL_S", str(3 * 86400))
    _signed_in(ui, capsys)
    _set_expiry(env, datetime.utcnow() + timedelta(days=1))

    r = ui.get("/ui/")

    device = _row(env, "SELECT expires_at FROM device_tokens")["expires_at"]
    assert _row(env, "SELECT expires_at FROM ui_sessions")["expires_at"] == device
    assert 3 * 86400 - 60 <= _cookie_max_age(r) <= 3 * 86400


def test_an_expired_session_is_not_brought_back(ui, env, capsys):
    _signed_in(ui, capsys)
    past = datetime.utcnow() - timedelta(seconds=5)
    _set_expiry(env, past)
    before = _row(env, "SELECT expires_at FROM ui_sessions")["expires_at"]

    assert ui.get("/ui/").status_code == 401
    assert _row(env, "SELECT expires_at FROM ui_sessions")["expires_at"] == before
