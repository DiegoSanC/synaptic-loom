"""D17 — where the client address comes from, and what a valid credential is owed.

`LOOM_CLIENT_IP_SOURCE` is read once, when the app starts: `peer` (the default) keys the
limiters and the audit log on the TCP peer, `fly-client-ip` on the Fly proxy's header,
`header:<name>` on one single-valued header a trusted proxy sets. Anything else refuses
to start. Every test here builds its own `TestClient` with an explicit peer address, so
the address the server sees is a literal of the test, never something it computed.

The other half of D17: the auth-failure limiter throttles FAILED attempts only. A valid
bearer, a valid `/ui` session and a valid code exchange pass even when their address's
failure window is full.
"""
import logging

import pytest
from fastapi.testclient import TestClient

from server import admin

AUDIT_LOGGER = "uvicorn.error.loom.security"
STARTUP_LOGGER = "uvicorn.error.loom.startup"
PEER = "198.51.100.7"
OTHER_PEER = "198.51.100.8"
BAD_BEARER = {"Authorization": "Bearer always-invalid"}


@pytest.fixture
def source(env, monkeypatch):
    """Set LOOM_CLIENT_IP_SOURCE (None = unset) and the Fly marker, before startup."""
    monkeypatch.delenv("LOOM_CLIENT_IP_SOURCE", raising=False)
    monkeypatch.delenv("FLY_APP_NAME", raising=False)

    def _set(value, *, fly_app=None):
        if value is not None:
            monkeypatch.setenv("LOOM_CLIENT_IP_SOURCE", value)
        if fly_app is not None:
            monkeypatch.setenv("FLY_APP_NAME", fly_app)

    return _set


def _client(peer=PEER, **kwargs):
    from server.app import app

    return TestClient(app, client=(peer, 50000), **kwargs)


def _audit_ips(caplog) -> list[str]:
    return [part.split("=", 1)[1]
            for record in caplog.records if record.name == AUDIT_LOGGER
            for part in record.getMessage().split() if part.startswith("client_ip=")]


def _startup_lines(caplog) -> list[tuple[int, str]]:
    return [(record.levelno, record.getMessage())
            for record in caplog.records if record.name == STARTUP_LOGGER]


def _exhaust(client, headers=None, *, path="/api/projects"):
    """Ten failed bearers: the address's failure window is now full."""
    for _ in range(10):
        assert client.get(path, headers={**BAD_BEARER, **(headers or {})}).status_code == 401


# --------------------------------------------------------------------------- #
# Mode resolution — refused at startup, not at the first request              #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", [
    "", "bogus", "PEER", "Peer", "fly", "xff:1", "header:", "header:bad name",
    "header:x-forwarded-for", "header:X-Forwarded-For", "header:forwarded",
])
def test_an_unknown_source_refuses_to_start_and_names_the_setting(source, value):
    source(value)
    with pytest.raises(RuntimeError) as refused:
        with _client():
            pass
    message = str(refused.value)
    assert "LOOM_CLIENT_IP_SOURCE" in message
    assert repr(value) in message
    for accepted in ("peer", "fly-client-ip", "header:<name>"):
        assert accepted in message


@pytest.mark.parametrize(("value", "logged"), [
    (None, "peer"),
    ("peer", "peer"),
    ("fly-client-ip", "fly-client-ip"),
    ("header:cf-connecting-ip", "header:cf-connecting-ip"),
    ("header:CF-Connecting-IP", "header:cf-connecting-ip"),
])
def test_startup_logs_one_line_naming_the_resolved_mode(source, caplog, value, logged):
    source(value, fly_app="loom-test" if logged == "fly-client-ip" else None)
    with caplog.at_level(logging.INFO, logger=STARTUP_LOGGER):
        with _client():
            pass
    assert _startup_lines(caplog) == [(logging.INFO, f"client_ip_source mode={logged}")]


@pytest.mark.parametrize("value", [None, "peer", "header:cf-connecting-ip"])
def test_startup_warns_when_on_fly_without_the_fly_mode(source, caplog, value):
    source(value, fly_app="loom-test")
    with caplog.at_level(logging.INFO, logger=STARTUP_LOGGER):
        with _client():
            pass
    warnings = [msg for level, msg in _startup_lines(caplog) if level == logging.WARNING]
    assert len(warnings) == 1, _startup_lines(caplog)
    assert "FLY_APP_NAME" in warnings[0] and "fly-client-ip" in warnings[0]


def test_startup_warns_when_the_fly_mode_runs_off_fly(source, caplog):
    source("fly-client-ip")
    with caplog.at_level(logging.INFO, logger=STARTUP_LOGGER):
        with _client():
            pass
    warnings = [msg for level, msg in _startup_lines(caplog) if level == logging.WARNING]
    assert len(warnings) == 1, _startup_lines(caplog)
    assert "FLY_APP_NAME" in warnings[0] and "fly-client-ip" in warnings[0]


@pytest.mark.parametrize(("value", "fly_app"), [
    (None, None), ("peer", None), ("header:cf-connecting-ip", None),
    ("fly-client-ip", "loom-test"),
])
def test_a_consistent_setting_warns_about_nothing(source, caplog, value, fly_app):
    source(value, fly_app=fly_app)
    with caplog.at_level(logging.INFO, logger=STARTUP_LOGGER):
        with _client():
            pass
    assert [level for level, _ in _startup_lines(caplog)] == [logging.INFO]


# --------------------------------------------------------------------------- #
# Which address keys the failure window and the audit line                     #
# --------------------------------------------------------------------------- #
def test_peer_mode_ignores_client_sent_proxy_headers(source, caplog):
    """A client cannot buy a fresh bucket by sending a header, nor plant one in the log."""
    source("peer")
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER), _client() as client:
        for n in range(10):
            spoof = {"Fly-Client-IP": f"203.0.113.{n}", "X-Forwarded-For": f"192.0.2.{n}",
                     "CF-Connecting-IP": f"192.0.2.{100 + n}"}
            assert client.get("/api/projects", headers={**BAD_BEARER, **spoof}).status_code == 401
        limited = client.get("/api/projects", headers={
            **BAD_BEARER, "Fly-Client-IP": "203.0.113.99", "X-Forwarded-For": "192.0.2.99"})
        assert limited.status_code == 429
        assert limited.headers["Retry-After"] == "60"

    assert set(_audit_ips(caplog)) == {PEER}

    with _client(OTHER_PEER) as other:
        assert other.get("/api/projects", headers=BAD_BEARER).status_code == 401


def test_peer_mode_health_bucket_is_per_peer(source):
    source(None)
    with _client() as client:
        for _ in range(120):
            assert client.get("/api/health",
                              headers={"Fly-Client-IP": "203.0.113.5"}).status_code == 200
        assert client.get("/api/health",
                          headers={"Fly-Client-IP": "203.0.113.6"}).status_code == 429
    with _client(OTHER_PEER) as other:
        assert other.get("/api/health").status_code == 200


def test_peer_mode_a_non_ip_peer_is_unavailable(source, caplog):
    source("peer")
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER), _client("testclient") as client:
        client.get("/api/projects", headers=BAD_BEARER)
    assert _audit_ips(caplog) == ["unavailable"]


def test_fly_mode_keys_on_the_fly_header_not_the_peer(source, caplog):
    source("fly-client-ip", fly_app="loom-test")
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER), _client() as client:
        _exhaust(client, {"Fly-Client-IP": "203.0.113.30"})
        assert client.get("/api/projects", headers={
            **BAD_BEARER, "Fly-Client-IP": "203.0.113.30"}).status_code == 429
        assert client.get("/api/projects", headers={
            **BAD_BEARER, "Fly-Client-IP": "203.0.113.31"}).status_code == 401
        client.get("/api/projects", headers={**BAD_BEARER, "Fly-Client-IP": "not-an-ip"})
    ips = _audit_ips(caplog)
    assert PEER not in ips
    assert ips[0] == "203.0.113.30" and ips[-2] == "203.0.113.31" and ips[-1] == "unavailable"


def test_header_mode_keys_on_the_named_header_only(source, caplog):
    source("header:cf-connecting-ip")
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER), _client() as client:
        _exhaust(client, {"CF-Connecting-IP": "2001:db8::1", "Fly-Client-IP": "203.0.113.40"})
        # Same named header, any other spoofable header: still the same full bucket.
        assert client.get("/api/projects", headers={
            **BAD_BEARER, "CF-Connecting-IP": "2001:db8::1",
            "Fly-Client-IP": "203.0.113.41"}).status_code == 429
        assert client.get("/api/projects", headers={
            **BAD_BEARER, "CF-Connecting-IP": "2001:db8::2"}).status_code == 401
        client.get("/api/projects", headers=BAD_BEARER)  # header absent
    ips = _audit_ips(caplog)
    assert ips[0] == "2001:db8::1" and ips[-2] == "2001:db8::2" and ips[-1] == "unavailable"
    assert PEER not in ips and "203.0.113.40" not in ips


# --------------------------------------------------------------------------- #
# A valid credential is never rejected by the failure limiter                  #
# --------------------------------------------------------------------------- #
def test_a_valid_bearer_passes_when_its_address_window_is_full(source, user_factory, caplog):
    source("peer")
    user = user_factory("dev1")
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER), _client() as client:
        _exhaust(client)
        assert client.get("/api/projects", headers=BAD_BEARER).status_code == 429
        assert client.get("/api/projects").status_code == 429          # no header at all
        for _ in range(3):
            assert client.get("/api/projects", headers=user["auth"]).status_code == 200
        # …and the window is still full for the next failure: nothing was consumed or reset.
        limited = client.get("/api/projects", headers=BAD_BEARER)
        assert limited.status_code == 429
        assert limited.json()["error"] == "rate_limited"
    outcomes = [record.getMessage().split()[1] for record in caplog.records
                if record.name == AUDIT_LOGGER]
    assert outcomes == ["outcome=invalid_token"] * 10 + ["outcome=rate_limited"] * 3


def test_a_full_window_does_not_unlock_a_known_but_refused_credential(
        source, user_factory):
    """Validated first does not mean waved through: a deactivated user is still refused,
    and on a full window the refusal is the 429."""
    source("peer")
    inactive = user_factory("gone", active=0)
    with _client() as client:
        _exhaust(client)
        assert client.get("/api/projects", headers=inactive["auth"]).status_code == 429


def _ui_code(client, user) -> str:
    return client.post("/api/ui-login", headers=user["auth"]).json()["code"]


def test_a_valid_ui_session_passes_when_its_address_window_is_full(source, user_factory):
    source("peer")
    user = user_factory("dev1")
    with _client(base_url="https://testserver") as ui:
        assert ui.post("/ui/session", json={"code": _ui_code(ui, user)}).status_code == 204
        session = ui.cookies["loom_ui"]
        for _ in range(10):
            ui.cookies.clear()
            assert ui.get("/ui/", headers={"Cookie": "loom_ui=forged"}).status_code == 401
        ui.cookies.clear()
        assert ui.get("/ui/", headers={"Cookie": "loom_ui=forged"}).status_code == 429
        assert ui.get("/ui/").status_code == 429                          # no cookie at all
        assert ui.get("/ui/", headers={"Cookie": f"loom_ui={session}"}).status_code == 200
        assert ui.get("/ui/", headers={"Cookie": "loom_ui=forged"}).status_code == 429


def test_a_valid_ui_login_code_is_exchanged_when_the_window_is_full(source, user_factory):
    source("peer")
    user = user_factory("dev1")
    with _client(base_url="https://testserver") as ui:
        code = _ui_code(ui, user)
        for _ in range(10):
            assert ui.post("/ui/session", json={"code": "wrong-" * 6}).status_code == 401
        assert ui.post("/ui/session", json={"code": "wrong-" * 6}).status_code == 429
        assert ui.post("/ui/session", json={"code": code}).status_code == 204
        assert ui.post("/ui/session", json={"code": code}).status_code == 429   # now spent


def test_a_valid_enrolment_code_is_redeemed_when_the_window_is_full(source, capsys):
    source("peer")
    assert admin.main(["invite", "--name", "dev2", "--email", "dev2@example.test"]) == 0
    code = [line.strip() for line in capsys.readouterr().out.splitlines()
            if len(line.strip()) >= 32 and " " not in line.strip()][0]
    body = {"code": code, "device_name": "laptop"}
    with _client() as client:
        for _ in range(10):
            assert client.post("/api/enroll", json={**body, "code": "wrong-" * 6}).status_code == 401
        assert client.post("/api/enroll", json={**body, "code": "wrong-" * 6}).status_code == 429
        enrolled = client.post("/api/enroll", json=body)
        assert enrolled.status_code == 201
        token = enrolled.json()["token"]
        assert client.get("/api/projects",
                          headers={"Authorization": f"Bearer {token}"}).status_code == 200
        assert client.post("/api/enroll", json=body).status_code == 429          # now spent


# --------------------------------------------------------------------------- #
# Values that parse as addresses but must not become one                       #
# --------------------------------------------------------------------------- #
def test_an_ipv6_scope_id_is_unavailable_never_logged(source, caplog):
    """`ipaddress` accepts any text after `%` as a scope id: a spoofable header could
    then mint a fresh bucket per request and write free text into the audit line."""
    source("header:cf-connecting-ip")
    forged = "fe80::1%x outcome=forged client_ip=192.0.2.66"
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER), _client() as client:
        for n in range(10):
            client.get("/api/projects", headers={**BAD_BEARER, "CF-Connecting-IP": f"fe80::1%{n}"})
        limited = client.get("/api/projects", headers={**BAD_BEARER, "CF-Connecting-IP": forged})
    assert limited.status_code == 429, "scope ids must not buy fresh buckets"
    assert _audit_ips(caplog) == ["unavailable"] * 11
    assert "outcome=forged" not in caplog.text and "192.0.2.66" not in caplog.text


def test_an_ipv4_mapped_peer_shares_the_plain_ipv4_bucket(source, caplog):
    source("peer")
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        with _client(f"::ffff:{PEER}") as mapped:
            _exhaust(mapped)
            # Not entered: a second lifespan would clear the windows under test.
            plain = _client(PEER)
            assert plain.get("/api/projects", headers=BAD_BEARER).status_code == 429
    assert set(_audit_ips(caplog)) == {PEER}


def test_header_fly_client_ip_is_the_fly_mode(source, caplog):
    source("header:Fly-Client-IP")
    with caplog.at_level(logging.INFO, logger=STARTUP_LOGGER):
        with _client():
            pass
    lines = _startup_lines(caplog)
    assert lines[0] == (logging.INFO, "client_ip_source mode=fly-client-ip")
    # Same header, same risk off Fly: the fly-mode warning, not silence.
    assert [level for level, _ in lines[1:]] == [logging.WARNING], lines


# --------------------------------------------------------------------------- #
# A flood of bogus codes never queues behind the database's write lock         #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("path", "body"), [
    ("/api/enroll", {"code": "wrong-" * 6, "device_name": "laptop"}),
    ("/ui/session", {"code": "wrong-" * 6}),
])
def test_a_bogus_code_is_refused_without_waiting_for_the_write_lock(source, env, path, body):
    """Redemption is unthrottled for the first ten failures and answered 429 after: a
    bogus code must be refused by a read, not by queueing for SQLite's single writer
    (busy_timeout is 5 s) behind legitimate writes."""
    import sqlite3
    import time

    source("peer")
    holder = sqlite3.connect(env, isolation_level=None)
    try:
        with _client(base_url="https://testserver") as client:
            holder.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            refused = client.post(path, json=body)
            elapsed = time.monotonic() - started
            holder.execute("ROLLBACK")
    finally:
        holder.close()
    assert refused.status_code == 401, refused.text
    assert elapsed < 1.0, f"a bogus code waited {elapsed:.1f}s for the write lock"
