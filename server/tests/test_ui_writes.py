"""The writable UI's foundation (D20, DESIGN §6.4; plan 2026-10-19, phase 2 slice 2).

What every write route shares, proved on the first one, `POST /ui/signout`: the exact
registry of POST routes the session gate lets through, and the refusals §6.4 lists in
order — `405` for any other write, the session gate, `403 public_url_unset`,
`403 bad_origin`, `403 bad_csrf` — before anything changes.

Expected values are §6.4's literals: the `env` fixture runs as Fly does, so the public
URL is `https://loom-test.fly.dev`; the CSRF token is the lowercase hex of
`HMAC-SHA256(key = the cookie value, msg = "csrf")`, computed here from that definition.
Each signed-in browser is its own `TestClient` (its own cookie jar), so two sessions of
one user can be told apart.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re

import pytest
from fastapi.testclient import TestClient

from server import db, ui_auth

PUBLIC_ORIGIN = "https://loom-test.fly.dev"      # https://$FLY_APP_NAME.fly.dev (D19)
SAME_ORIGIN = {"Origin": PUBLIC_ORIGIN}
PUBLIC_URL_LOGGER = "uvicorn.error.loom.public_url"
AUDIT_LOGGER = "uvicorn.error.loom.security"
EXPECTED_HEADERS = {
    "content-security-policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "cache-control": "no-store",
}


def _csrf(cookie: str) -> str:
    return hmac.new(cookie.encode(), b"csrf", hashlib.sha256).hexdigest()


def _browser(user) -> TestClient:
    """A browser signed in as ``user`` through `loom ui`'s flow, with its own cookie jar."""
    from server.app import app

    browser = TestClient(app, base_url="https://testserver")
    code = browser.post("/api/ui-login", headers=user["auth"]).json()["code"]
    assert browser.post("/ui/session", json={"code": code}).status_code == 204
    return browser


def _cookie(browser: TestClient) -> str:
    return browser.cookies["loom_ui"]


def _signout(browser: TestClient, *, headers=None, csrf: str | None = "<own>", **kwargs):
    form = {} if csrf is None else {"csrf": _csrf(_cookie(browser)) if csrf == "<own>"
                                    else csrf}
    return browser.post("/ui/signout", data=form,
                        headers=SAME_ORIGIN if headers is None else headers, **kwargs)


def _session_exists(env, cookie: str) -> bool:
    conn = db.connect(env)
    try:
        return conn.execute("SELECT 1 FROM ui_sessions WHERE session_hash = ?",
                            (db.hash_token(cookie),)).fetchone() is not None
    finally:
        conn.close()


def _assert_headers(response, what: str):
    for name, value in EXPECTED_HEADERS.items():
        assert response.headers.get(name) == value, f"{what}: {name}"


@pytest.fixture
def dev1(user_factory):
    return user_factory("dev1")


@pytest.fixture
def browser(env, dev1):
    return _browser(dev1)


# --------------------------------------------------------------------------- #
# POST /ui/signout                                                             #
# --------------------------------------------------------------------------- #
def test_signout_ends_this_session_and_clears_the_cookie(env, browser):
    cookie = _cookie(browser)
    assert browser.get("/ui/").status_code == 200

    r = _signout(browser)

    assert r.status_code == 200, r.text
    assert "text/html" in r.headers["content-type"]
    assert "Signed out" in r.text
    assert "Signed in as" not in r.text
    assert not _session_exists(env, cookie)
    # Cleared with the attributes it was set with (`/ui/session`): the same name and path,
    # Secure, HttpOnly, SameSite=Strict, and an immediate expiry.
    attrs = [part.strip().lower() for part in r.headers["set-cookie"].split(";")]
    assert attrs[0] in ('loom_ui=""', "loom_ui=")
    assert "path=/ui" in attrs and "secure" in attrs and "httponly" in attrs
    assert "samesite=strict" in attrs and "max-age=0" in attrs
    assert "loom_ui" not in browser.cookies
    # The old cookie, replayed, no longer opens anything.
    assert browser.get("/ui/", headers={"Cookie": f"loom_ui={cookie}"}).status_code == 401


def test_signout_leaves_the_users_other_sessions_alive(env, dev1, user_factory):
    laptop, desktop = _browser(dev1), _browser(dev1)
    other_user = _browser(user_factory("dev2"))
    laptop_cookie = _cookie(laptop)

    assert _signout(laptop).status_code == 200

    assert laptop.get("/ui/", headers={"Cookie": f"loom_ui={laptop_cookie}"}).status_code == 401
    assert desktop.get("/ui/").status_code == 200
    assert other_user.get("/ui/").status_code == 200


def test_signout_needs_a_session(env, browser):
    browser.cookies.clear()
    r = browser.post("/ui/signout", data={"csrf": "0" * 64}, headers=SAME_ORIGIN)
    assert r.status_code == 401
    assert "Run `loom ui`" in r.text


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_signout_accepts_no_other_write_method(env, browser, method):
    cookie = _cookie(browser)
    r = browser.request(method, "/ui/signout", headers=SAME_ORIGIN)
    assert r.status_code == 405
    assert r.json()["error"] == "method_not_allowed"
    assert _session_exists(env, cookie)


def test_signout_is_not_a_page(env, browser):
    """No state-changing GET: the route answers POST only."""
    assert browser.get("/ui/signout").status_code == 405
    assert _session_exists(env, _cookie(browser))


# --------------------------------------------------------------------------- #
# The registry: POST on a listed route only                                    #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/ui/", "/ui/usage", "/ui/signout/", "/ui/signouts",
                                  "/ui/no-such-page", "/ui"])
def test_an_unregistered_post_is_405_without_reading_the_cookie(env, ui, path, caplog):
    forged = "forged-session-value-never-read"
    with caplog.at_level(logging.DEBUG):
        r = ui.post(path, data={"csrf": _csrf(forged)},
                    headers={**SAME_ORIGIN, "Cookie": f"loom_ui={forged}"})
    assert r.status_code == 405
    assert r.json()["error"] == "method_not_allowed"
    # A cookie that was read and refused would be a 401 and an audit line.
    assert not [rec for rec in caplog.records if rec.name == AUDIT_LOGGER]


def test_the_405_names_the_exception(env, ui):
    detail = ui.put("/ui/").json()["detail"]
    assert detail == "The UI is read-only except the listed POST routes"


def test_the_registry_lists_exactly_the_ui_post_routes():
    """Every registered write is a POST-only route, and every POST route under `/ui`
    other than the anonymous `/ui/session` is registered: the gate and the router
    cannot disagree about what is writable."""
    from server import ui_pages
    from server.app import app

    assert "/ui/signout" in ui_pages.WRITE_ROUTES
    posts = set()
    for route in [*app.routes, *ui_pages.router.routes]:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None) or set()
        if (path == "/ui" or path.startswith("/ui/")) and "POST" in methods:
            posts.add(path)
            if path != "/ui/session":
                assert methods == {"POST"}, path
    assert posts - {"/ui/session"} == set(ui_pages.WRITE_ROUTES)


# --------------------------------------------------------------------------- #
# Origin / Sec-Fetch-Site                                                      #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("headers, accepted", [
    ({"Origin": PUBLIC_ORIGIN}, True),
    ({"Origin": PUBLIC_ORIGIN, "Sec-Fetch-Site": "cross-site"}, True),   # Origin decides
    ({"Origin": "http://loom-test.fly.dev"}, False),                      # scheme
    ({"Origin": "https://evil.example"}, False),                          # host
    ({"Origin": "https://loom-test.fly.dev.evil.example"}, False),        # host suffix
    ({"Origin": "https://loom-test.fly.dev:8443"}, False),                # port
    ({"Origin": "null"}, False),
    ({"Origin": "https://evil.example", "Sec-Fetch-Site": "same-origin"}, False),
    ({"Sec-Fetch-Site": "same-origin"}, True),
    ({"Sec-Fetch-Site": "same-site"}, False),
    ({"Sec-Fetch-Site": "cross-site"}, False),
    ({"Sec-Fetch-Site": "none"}, False),
    ({}, False),                                                          # neither header
])
def test_a_write_must_come_from_the_public_origin(env, browser, headers, accepted):
    cookie = _cookie(browser)

    r = _signout(browser, headers=headers)

    if accepted:
        assert r.status_code == 200, r.text
        assert not _session_exists(env, cookie)
    else:
        assert r.status_code == 403
        assert r.json()["error"] == "bad_origin"
        assert _session_exists(env, cookie)
        assert browser.get("/ui/").status_code == 200


def test_the_origin_is_the_configured_public_url(env, browser, monkeypatch):
    monkeypatch.setenv("LOOM_PUBLIC_URL", "https://loom.example.org/")
    assert _signout(browser, headers=SAME_ORIGIN).status_code == 403
    assert _signout(browser, headers={"Origin": "https://loom.example.org"}).status_code == 200


def test_a_public_url_with_its_default_port_is_the_same_origin(env, browser, monkeypatch):
    monkeypatch.setenv("LOOM_PUBLIC_URL", "https://loom.example.org:443")
    assert _signout(browser, headers={"Origin": "https://loom.example.org"}).status_code == 200


def test_no_public_url_refuses_every_write(env, browser, monkeypatch):
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    monkeypatch.delenv("LOOM_PUBLIC_URL", raising=False)
    cookie = _cookie(browser)

    for headers in ({"Origin": "https://testserver"}, {"Sec-Fetch-Site": "same-origin"}):
        r = _signout(browser, headers=headers)
        assert r.status_code == 403
        assert r.json()["error"] == "public_url_unset"
    assert _session_exists(env, cookie)


def test_startup_warns_when_no_public_url_is_resolved(env, monkeypatch, caplog):
    from server.app import app

    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    monkeypatch.delenv("LOOM_PUBLIC_URL", raising=False)
    with caplog.at_level(logging.INFO, logger=PUBLIC_URL_LOGGER):
        with TestClient(app):
            pass
    warnings = [rec.getMessage() for rec in caplog.records
                if rec.name == PUBLIC_URL_LOGGER and rec.levelno == logging.WARNING]
    assert any("public_url_unset" in line for line in warnings), warnings


def test_startup_does_not_warn_when_the_public_url_is_known(env, caplog):
    from server.app import app

    with caplog.at_level(logging.INFO, logger=PUBLIC_URL_LOGGER):
        with TestClient(app):
            pass
    assert not [rec for rec in caplog.records
                if rec.name == PUBLIC_URL_LOGGER and "public_url" in rec.getMessage()]


def test_the_session_gate_runs_before_the_origin_check(env, ui):
    r = ui.post("/ui/signout", data={"csrf": "0" * 64},
                headers={"Origin": "https://evil.example"})
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# The CSRF token                                                               #
# --------------------------------------------------------------------------- #
def test_a_write_without_the_csrf_field_is_refused(env, browser):
    r = _signout(browser, csrf=None)
    assert r.status_code == 403
    assert r.json()["error"] == "bad_csrf"
    assert _session_exists(env, _cookie(browser))


@pytest.mark.parametrize("token", ["", "0" * 64, "not-hex"])
def test_a_wrong_csrf_token_is_refused(env, browser, token):
    r = _signout(browser, csrf=token)
    assert r.status_code == 403
    assert r.json()["error"] == "bad_csrf"
    assert _session_exists(env, _cookie(browser))


def test_the_csrf_token_is_lowercase_hex(env, browser):
    r = _signout(browser, csrf=_csrf(_cookie(browser)).upper())
    assert r.status_code == 403
    assert r.json()["error"] == "bad_csrf"


def test_another_sessions_csrf_token_is_refused(env, dev1):
    mine, theirs = _browser(dev1), _browser(dev1)

    r = _signout(mine, csrf=_csrf(_cookie(theirs)))

    assert r.status_code == 403
    assert r.json()["error"] == "bad_csrf"
    assert _session_exists(env, _cookie(mine)) and _session_exists(env, _cookie(theirs))


def test_the_csrf_token_travels_in_a_form_body_only(env, browser):
    token = _csrf(_cookie(browser))
    as_json = browser.post("/ui/signout", json={"csrf": token}, headers=SAME_ORIGIN)
    in_query = browser.post(f"/ui/signout?csrf={token}", headers=SAME_ORIGIN)
    in_header = browser.post("/ui/signout", headers={**SAME_ORIGIN, "X-CSRF-Token": token})
    for r in (as_json, in_query, in_header):
        assert r.status_code == 403
        assert r.json()["error"] == "bad_csrf"
    assert _session_exists(env, _cookie(browser))


def test_the_token_is_never_stored(env, browser):
    token = _csrf(_cookie(browser))
    with open(env, "rb") as database:
        assert token.encode() not in database.read()


# --------------------------------------------------------------------------- #
# The shell: a sign-out form carrying the token on every signed-in page        #
# --------------------------------------------------------------------------- #
SIGNOUT_FORM = re.compile(
    r'<form method="post" action="/ui/signout"[^>]*>\s*'
    r'<input type="hidden" name="csrf" value="([0-9a-f]{64})">')


def _parameterless_pages() -> list[str]:
    from server import ui_pages

    return [route.path for route in ui_pages.router.routes
            if "GET" in (getattr(route, "methods", None) or set())
            and "{" not in route.path
            and route.path not in {"/ui/login", "/ui/join"}
            and not route.path.startswith("/ui/static/")]


def test_every_signed_in_page_has_a_signout_form_with_the_token(env, browser):
    pages = _parameterless_pages() + ["/ui/no-such-page"]
    assert "/ui/" in pages and "/ui/usage" in pages
    for path in pages:
        found = SIGNOUT_FORM.findall(browser.get(path).text)
        assert found == [_csrf(_cookie(browser))], path


def test_the_signed_out_pages_have_no_signout_form(env, browser):
    assert "/ui/signout" not in browser.get("/ui/login").text
    assert "/ui/signout" not in _signout(browser).text
    assert "/ui/signout" not in browser.get("/ui/").text            # the 401 page


def test_the_signout_form_posted_as_a_browser_would_signs_out(env, browser):
    """The token the page embeds is the one the gate accepts."""
    token = SIGNOUT_FORM.search(browser.get("/ui/").text).group(1)
    r = browser.post("/ui/signout", data={"csrf": token},
                     headers={"Origin": PUBLIC_ORIGIN, "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# Headers and logs                                                             #
# --------------------------------------------------------------------------- #
def test_write_responses_carry_the_ui_security_headers(env, dev1):
    browser = _browser(dev1)
    _assert_headers(_signout(browser, headers={}), "bad_origin")
    _assert_headers(_signout(browser, csrf="0" * 64), "bad_csrf")
    _assert_headers(browser.put("/ui/signout"), "405")
    _assert_headers(_signout(browser), "signed out")
    _assert_headers(_signout(_browser(dev1)), "signed out")


def test_no_secret_reaches_any_log_line(env, dev1, caplog):
    with caplog.at_level(logging.DEBUG):
        browser = _browser(dev1)
        cookie, token = _cookie(browser), _csrf(_cookie(browser))
        _signout(browser, headers={"Origin": "https://evil.example"})
        _signout(browser, csrf="0" * 64)
        _signout(browser, csrf=_csrf("someone-elses-session"))
        browser.put("/ui/signout")
        assert _signout(browser).status_code == 200
        browser.get("/ui/", headers={"Cookie": f"loom_ui={cookie}"})
    lines = [rec.getMessage() for rec in caplog.records]
    assert lines, "nothing was logged: the assertion below is vacuous"
    for secret in (cookie, token, db.hash_token(cookie), dev1["token"]):
        assert all(secret not in line for line in lines)


# --------------------------------------------------------------------------- #
# ui_auth.end_sessions: the one way a session ends                             #
# --------------------------------------------------------------------------- #
def _device_browser(ui, capsys, email, name, device):
    """A browser signed in through a REAL device token (invite → enroll → loom ui)."""
    from server import admin

    assert admin.main(["invite", "--email", email, "--name", name]) == 0
    out = capsys.readouterr().out
    code = next(line.strip() for line in out.splitlines()
                if re.fullmatch(r"[A-Za-z0-9_-]{32,}", line.strip()))
    enrolled = ui.post("/api/enroll", json={"code": code, "device_name": device}).json()
    return _browser({"auth": {"Authorization": f"Bearer {enrolled['token']}"}})


def _device_token_id(env, cookie: str) -> int:
    conn = db.connect(env)
    try:
        return conn.execute("SELECT device_token_id FROM ui_sessions WHERE session_hash = ?",
                            (db.hash_token(cookie),)).fetchone()[0]
    finally:
        conn.close()


def _user_id(env, cookie: str) -> int:
    conn = db.connect(env)
    try:
        return conn.execute("SELECT user_id FROM ui_sessions WHERE session_hash = ?",
                            (db.hash_token(cookie),)).fetchone()[0]
    finally:
        conn.close()


def test_end_sessions_by_user_ends_every_session_of_that_user_only(env, dev1, user_factory):
    a, b, other = _browser(dev1), _browser(dev1), _browser(user_factory("dev2"))
    conn = db.connect(env)
    try:
        assert ui_auth.end_sessions(conn, user_id=dev1["id"]) == 2
    finally:
        conn.close()
    assert a.get("/ui/").status_code == 401 and b.get("/ui/").status_code == 401
    assert other.get("/ui/").status_code == 200


def test_end_sessions_by_device_token_ids(env, ui, capsys):
    mac = _device_browser(ui, capsys, "mac@example.test", "mac", "mac-1")
    pc = _device_browser(ui, capsys, "mac@example.test", "mac", "pc-1")   # same user
    conn = db.connect(env)
    try:
        assert ui_auth.end_sessions(
            conn, device_token_ids=[_device_token_id(env, _cookie(mac))]) == 1
        assert ui_auth.end_sessions(conn, device_token_ids=[]) == 0
    finally:
        conn.close()
    assert mac.get("/ui/").status_code == 401
    assert pc.get("/ui/").status_code == 200


def test_end_sessions_by_session_id(env, dev1):
    a, b = _browser(dev1), _browser(dev1)
    conn = db.connect(env)
    try:
        session_id = conn.execute("SELECT id FROM ui_sessions WHERE session_hash = ?",
                                  (db.hash_token(_cookie(a)),)).fetchone()[0]
        assert ui_auth.end_sessions(conn, session_id=session_id) == 1
    finally:
        conn.close()
    assert a.get("/ui/").status_code == 401
    assert b.get("/ui/").status_code == 200


def test_end_sessions_needs_exactly_one_selector(env):
    conn = db.connect(env)
    try:
        with pytest.raises(ValueError):
            ui_auth.end_sessions(conn)
        with pytest.raises(ValueError):
            ui_auth.end_sessions(conn, session_id=1, user_id=1)
    finally:
        conn.close()


def test_end_sessions_inside_a_callers_transaction_leaves_the_commit_to_it(env, dev1):
    """A later revocation calls it inside its own `BEGIN IMMEDIATE`: a rollback there
    must take the deleted sessions back with everything else."""
    a = _browser(dev1)
    conn = db.connect(env)
    try:
        conn.execute("BEGIN IMMEDIATE")
        assert ui_auth.end_sessions(conn, user_id=_user_id(env, _cookie(a))) == 1
        assert conn.in_transaction
        conn.rollback()
    finally:
        conn.close()
    assert a.get("/ui/").status_code == 200


# --------------------------------------------------------------------------- #
# Integration of slices 1 and 2: the session kind, and the sign-in/out events   #
# --------------------------------------------------------------------------- #
def _events(env, event: str) -> list[tuple]:
    conn = db.connect(env)
    try:
        return conn.execute(
            "SELECT actor_user_id, target_type, target_id, client_ip FROM auth_events "
            "WHERE event = ? ORDER BY id", (event,)).fetchall()
    finally:
        conn.close()


def test_a_session_of_a_kind_that_cannot_write_is_refused_before_anything_changes(env, browser):
    """§6.4: only `cli` sessions write in 2a; any other kind → `403 forbidden`."""
    cookie = _cookie(browser)
    conn = db.connect(env)
    try:
        conn.execute("UPDATE ui_sessions SET kind = 'password' WHERE session_hash = ?",
                     (db.hash_token(cookie),))
        conn.commit()
    finally:
        conn.close()

    assert browser.get("/ui/").status_code == 200    # reading is not a write
    r = _signout(browser)

    assert r.status_code == 403
    assert r.json()["error"] == "forbidden"
    assert _session_exists(env, cookie)


def test_the_csrf_check_comes_before_the_kind_check(env, browser):
    """§6.4's order: a wrong `csrf` is `bad_csrf` even on a session that cannot write."""
    cookie = _cookie(browser)
    conn = db.connect(env)
    try:
        conn.execute("UPDATE ui_sessions SET kind = 'password' WHERE session_hash = ?",
                     (db.hash_token(cookie),))
        conn.commit()
    finally:
        conn.close()
    r = _signout(browser, csrf="0" * 64)
    assert r.status_code == 403
    assert r.json()["error"] == "bad_csrf"


def test_signing_in_and_out_records_one_event_each(env, dev1):
    """§6.4: `ui_signin` and `ui_signout` on the user, the actor the user, with the address."""
    browser = _browser(dev1)
    signins = _events(env, "ui_signin")
    assert [(a, t, i) for a, t, i, _ in signins] == [(dev1["id"], "user", dev1["id"])]
    assert signins[0][3]                              # the client address is recorded

    assert _signout(browser).status_code == 200
    signouts = _events(env, "ui_signout")
    assert [(a, t, i) for a, t, i, _ in signouts] == [(dev1["id"], "user", dev1["id"])]


def test_a_refused_sign_in_or_sign_out_records_no_event(env, browser):
    """Failures stay log lines (§6.4)."""
    from server.app import app

    TestClient(app, base_url="https://testserver").post("/ui/session", json={"code": "x" * 43})
    assert _signout(browser, csrf="0" * 64).status_code == 403
    assert len(_events(env, "ui_signin")) == 1        # the fixture's own sign-in only
    assert _events(env, "ui_signout") == []
