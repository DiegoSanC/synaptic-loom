"""Enrolment codes and device tokens (issue #20).

Every expected value here comes from the issue's design and the owner decisions of
2026-09-22, not from the implementation: a code is single-use and expires; a device token
is minted only in response to a redemption, carries a finite life and is renewed by the
client; `adduser` and its permanent tokens are untouched (break-glass).

Expiry is REAL time, never a mocked clock: `--ttl 1s` and `LOOM_DEVICE_TOKEN_TTL_S=1`
plus a sleep. The server timestamps have one-second precision, so 1.1 s is enough.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone

import pytest

from server import admin, db

AUDIT_LOGGER = "uvicorn.error.loom.security"
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{32,}$")


def _parse_code(stdout: str) -> str:
    """The one bare high-entropy line — same shape the frozen waves parse for adduser."""
    for line in stdout.splitlines():
        candidate = line.strip()
        if CODE_RE.fullmatch(candidate):
            return candidate
    raise AssertionError(f"no enrolment code in invite output:\n{stdout}")


def _invite(capsys, email="dev2@example.test", name="dev2", ttl=None) -> str:
    argv = ["invite", "--email", email]
    if name is not None:
        argv += ["--name", name]
    if ttl is not None:
        argv += ["--ttl", ttl]
    assert admin.main(argv) == 0
    return _parse_code(capsys.readouterr().out)


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


def _enroll(client, code, device_name="mac-1", **headers):
    return client.post(
        "/api/enroll", json={"code": code, "device_name": device_name}, headers=headers
    )


# --------------------------------------------------------------------------- #
# admin.py invite                                                              #
# --------------------------------------------------------------------------- #
def test_invite_creates_the_user_and_prints_a_code_once_and_no_token(env, capsys):
    rc = admin.main(["invite", "--email", "dev2@example.test", "--name", "dev2"])
    assert rc == 0
    out = capsys.readouterr().out

    assert "ENROLMENT CODE" in out and "ONLY ONCE" in out
    code = _parse_code(out)

    user = _row(env, "SELECT id, name, token_hash, active FROM users WHERE email = ?",
                "dev2@example.test")
    assert user is not None and user["name"] == "dev2" and user["active"] == 1
    # The user's legacy column holds nothing anyone can use, and nothing about it is printed.
    assert user["token_hash"] not in out
    assert "TOKEN" not in out.replace("ENROLMENT CODE", "")

    invitation = _row(env, "SELECT * FROM enrolment_codes WHERE user_id = ?", user["id"])
    assert invitation is not None
    assert invitation["code_hash"] == db.hash_token(code)
    assert invitation["redeemed_at"] is None
    created = datetime.fromisoformat(invitation["created_at"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(invitation["expires_at"].replace("Z", "+00:00"))
    assert expires - created == timedelta(hours=24)  # the 24 h default (decision 2026-09-22)

    # No device token exists until a redemption mints one.
    assert _rows(env, "SELECT id FROM device_tokens") == []


def test_invite_for_an_existing_user_needs_no_name(env, capsys):
    admin.main(["adduser", "--name", "diego", "--email", "diego@x.com"])
    capsys.readouterr()
    code = _invite(capsys, email="diego@x.com", name=None)
    user = _row(env, "SELECT id FROM users WHERE email = ?", "diego@x.com")
    invitation = _row(env, "SELECT user_id FROM enrolment_codes WHERE code_hash = ?",
                      db.hash_token(code))
    assert invitation["user_id"] == user["id"]
    # The second machine of the same person gets its own invitation; the user is not duplicated.
    _invite(capsys, email="diego@x.com", name=None)
    assert len(_rows(env, "SELECT id FROM users WHERE email = ?", "diego@x.com")) == 1
    assert len(_rows(env, "SELECT id FROM enrolment_codes WHERE user_id = ?", user["id"])) == 2


def test_invite_unknown_email_without_name_fails(env, capsys):
    assert admin.main(["invite", "--email", "ghost@x.com"]) == 1
    assert "ghost@x.com" in capsys.readouterr().err
    assert _rows(env, "SELECT id FROM enrolment_codes") == []


@pytest.mark.parametrize(("ttl", "seconds"), [("30m", 1800), ("2h", 7200), ("3d", 259200), ("45s", 45)])
def test_invite_ttl_units(env, capsys, ttl, seconds):
    code = _invite(capsys, ttl=ttl)
    row = _row(env, "SELECT created_at, expires_at FROM enrolment_codes WHERE code_hash = ?",
               db.hash_token(code))
    created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
    assert (expires - created).total_seconds() == seconds


def test_invite_rejects_a_malformed_ttl(env, capsys):
    with pytest.raises(SystemExit):
        admin.main(["invite", "--email", "dev2@x.com", "--name", "dev2", "--ttl", "soon"])
    assert _rows(env, "SELECT id FROM enrolment_codes") == []


# --------------------------------------------------------------------------- #
# POST /api/enroll                                                             #
# --------------------------------------------------------------------------- #
def test_enroll_redeems_the_code_and_the_device_token_authenticates(client, env, capsys):
    code = _invite(capsys)

    r = _enroll(client, code, device_name="dev-laptop")

    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"token", "issued_at", "expires_at", "user"}
    assert body["user"] == {"id": 1, "name": "dev2"}
    issued = datetime.fromisoformat(body["issued_at"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
    assert expires - issued == timedelta(days=90)  # decision 2026-09-22
    assert abs((issued - datetime.now(timezone.utc)).total_seconds()) < 5

    # The token is a device token, stored hashed, tied to the user and the device name.
    device = _row(env, "SELECT * FROM device_tokens WHERE token_hash = ?", db.hash_token(body["token"]))
    assert device is not None
    assert device["user_id"] == 1 and device["device_name"] == "dev-laptop"
    assert device["revoked_at"] is None and device["replaced_by"] is None
    # The code is burned and points at what it minted.
    invitation = _row(env, "SELECT redeemed_at, device_token_id FROM enrolment_codes WHERE code_hash = ?",
                      db.hash_token(code))
    assert invitation["redeemed_at"] is not None
    assert invitation["device_token_id"] == device["id"]

    # And it authenticates like any bearer: the user is attributed on a real write.
    auth = {"Authorization": f"Bearer {body['token']}"}
    assert client.get("/api/projects", headers=auth).status_code == 200
    created = client.post("/api/projects", json={"remote_url": "git@github.com:org/b.git"}, headers=auth)
    assert created.status_code == 201
    listed = client.get("/api/projects", headers=auth).json()["projects"]
    assert listed[0]["created_by"] == "dev2"


def test_enroll_is_single_use(client, env, capsys):
    code = _invite(capsys)
    assert _enroll(client, code).status_code == 201

    again = _enroll(client, code, device_name="second-try")

    assert again.status_code == 401
    assert again.json()["error"] == "invalid_code"
    assert len(_rows(env, "SELECT id FROM device_tokens")) == 1


def test_enroll_rejects_an_expired_code_after_real_time_passes(client, env, capsys):
    code = _invite(capsys, ttl="1s")
    time.sleep(1.1)

    r = _enroll(client, code)

    assert r.status_code == 401
    assert r.json()["error"] == "invalid_code"
    assert _rows(env, "SELECT id FROM device_tokens") == []
    assert _row(env, "SELECT redeemed_at FROM enrolment_codes")["redeemed_at"] is None


def test_enroll_unknown_code_is_401_audited_and_never_logs_the_code(client, caplog):
    secret = "not-a-real-code-never-log-this-value-xyz"
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        r = _enroll(client, secret, **{"Fly-Client-IP": "192.0.2.50"})

    assert r.status_code == 401
    assert r.json()["error"] == "invalid_code"
    lines = [rec.getMessage() for rec in caplog.records if rec.name == AUDIT_LOGGER]
    assert any("outcome=enrol_invalid_code" in line and "client_ip=192.0.2.50" in line for line in lines), lines
    assert all(secret not in line for line in lines)


def test_enroll_success_is_audited_without_the_token(client, env, capsys, caplog):
    code = _invite(capsys)
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        r = _enroll(client, code, **{"Fly-Client-IP": "192.0.2.51"})

    assert r.status_code == 201
    token = r.json()["token"]
    lines = [rec.getMessage() for rec in caplog.records if rec.name == AUDIT_LOGGER]
    hit = [line for line in lines if "outcome=enrol_redeemed" in line]
    assert hit and "user_id=1" in hit[0] and "device_token_id=1" in hit[0] and "client_ip=192.0.2.51" in hit[0]
    assert all(token not in line and code not in line for line in lines)


def test_enroll_failed_redemptions_from_one_ip_are_rate_limited(client, env, capsys):
    headers = {"Fly-Client-IP": "192.0.2.60"}
    for _ in range(10):
        assert _enroll(client, "wrong-code-wrong-code-wrong-code-000", **headers).status_code == 401

    limited = _enroll(client, "wrong-code-wrong-code-wrong-code-000", **headers)
    assert limited.status_code == 429
    assert limited.json()["error"] == "rate_limited"
    assert limited.headers["Retry-After"]

    # A VALID code from the throttled address is refused too: the window is on the caller,
    # so a brute-forcer that finally guesses right gains nothing until it clears.
    code = _invite(capsys)
    assert _enroll(client, code, **headers).status_code == 429
    # …while another address is unaffected and the code is still unredeemed.
    assert _enroll(client, code, **{"Fly-Client-IP": "192.0.2.61"}).status_code == 201


def test_enroll_a_successful_redemption_does_not_consume_the_failure_allowance(client, env, capsys):
    headers = {"Fly-Client-IP": "192.0.2.70"}
    for _ in range(10):
        assert _enroll(client, _invite(capsys), **headers).status_code == 201
    assert _enroll(client, _invite(capsys), **headers).status_code == 201


def test_enroll_validates_its_body(client):
    assert client.post("/api/enroll", json={"device_name": "x"}).status_code == 422
    assert client.post("/api/enroll", json={"code": "abc"}).status_code == 422
    assert client.post("/api/enroll", json={"code": "", "device_name": "x"}).status_code == 422


def test_enroll_for_a_deactivated_user_is_refused(client, env, capsys):
    code = _invite(capsys)
    assert admin.main(["deactivate", "--email", "dev2@example.test"]) == 0
    r = _enroll(client, code)
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_code"
    assert _rows(env, "SELECT id FROM device_tokens") == []


# --------------------------------------------------------------------------- #
# Device tokens in the bearer middleware                                       #
# --------------------------------------------------------------------------- #
def test_a_device_token_past_its_expiry_is_rejected_after_real_time_passes(
        client, env, capsys, monkeypatch, caplog):
    monkeypatch.setenv("LOOM_DEVICE_TOKEN_TTL_S", "1")
    token = _enroll(client, _invite(capsys)).json()["token"]
    auth = {"Authorization": f"Bearer {token}", "Fly-Client-IP": "192.0.2.80"}
    assert client.get("/api/projects", headers=auth).status_code == 200

    time.sleep(1.1)
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        r = client.get("/api/projects", headers=auth)

    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"
    lines = [rec.getMessage() for rec in caplog.records if rec.name == AUDIT_LOGGER]
    assert any("outcome=expired_token" in line for line in lines), lines
    assert all(token not in line for line in lines)


def test_a_device_token_of_a_deactivated_user_is_403(client, env, capsys):
    token = _enroll(client, _invite(capsys)).json()["token"]
    admin.main(["deactivate", "--email", "dev2@example.test"])
    r = client.get("/api/projects", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_legacy_adduser_tokens_keep_working_and_never_expire(client, env, capsys, monkeypatch):
    monkeypatch.setenv("LOOM_DEVICE_TOKEN_TTL_S", "1")
    admin.main(["adduser", "--name", "legacy", "--email", "legacy@x.com"])
    out = capsys.readouterr().out
    legacy = _parse_code(out)  # same bare-line shape as a code
    time.sleep(1.1)
    assert client.get("/api/projects", headers={"Authorization": f"Bearer {legacy}"}).status_code == 200


# --------------------------------------------------------------------------- #
# POST /api/device-tokens/renew                                                #
# --------------------------------------------------------------------------- #
def test_renew_mints_a_successor_and_keeps_the_old_token_until_its_own_expiry(client, env, capsys):
    first = _enroll(client, _invite(capsys)).json()
    old_auth = {"Authorization": f"Bearer {first['token']}"}

    r = client.post("/api/device-tokens/renew", headers=old_auth)

    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"token", "issued_at", "expires_at", "user"}
    assert body["token"] != first["token"]
    assert body["expires_at"] >= first["expires_at"]
    new_auth = {"Authorization": f"Bearer {body['token']}"}
    assert client.get("/api/projects", headers=new_auth).status_code == 200
    # Bounded overlap: the client may crash before persisting the successor.
    assert client.get("/api/projects", headers=old_auth).status_code == 200

    old = _row(env, "SELECT id, replaced_by, revoked_at, expires_at FROM device_tokens WHERE token_hash = ?",
               db.hash_token(first["token"]))
    new = _row(env, "SELECT id, device_name, user_id FROM device_tokens WHERE token_hash = ?",
               db.hash_token(body["token"]))
    assert old["replaced_by"] == new["id"] and old["revoked_at"] is None
    assert old["expires_at"] == first["expires_at"]  # unchanged: it dies on its own schedule
    assert new["device_name"] == "mac-1" and new["user_id"] == 1


def test_renew_with_a_legacy_token_is_403(client, user_factory):
    user = user_factory("legacy")
    r = client.post("/api/device-tokens/renew", headers=user["auth"])
    assert r.status_code == 403
    assert r.json()["error"] == "not_a_device_token"


def test_renew_needs_a_credential(client):
    assert client.post("/api/device-tokens/renew").status_code == 401


# --------------------------------------------------------------------------- #
# admin.py listdevices / revokedevice                                          #
# --------------------------------------------------------------------------- #
def test_revokedevice_kills_one_machine_only(client, env, capsys, caplog):
    code_a = _invite(capsys)
    code_b = _invite(capsys, name=None)
    tok_a = _enroll(client, code_a, device_name="laptop-stolen").json()["token"]
    tok_b = _enroll(client, code_b, device_name="desk").json()["token"]
    stolen = _row(env, "SELECT id FROM device_tokens WHERE device_name = ?", "laptop-stolen")["id"]

    assert admin.main(["revokedevice", "--id", str(stolen)]) == 0
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        r = client.get("/api/projects", headers={"Authorization": f"Bearer {tok_a}"})

    assert r.status_code == 401
    assert any("outcome=revoked_token" in rec.getMessage() for rec in caplog.records)
    assert client.get("/api/projects", headers={"Authorization": f"Bearer {tok_b}"}).status_code == 200
    # Renewal cannot resurrect a revoked token.
    assert client.post("/api/device-tokens/renew",
                       headers={"Authorization": f"Bearer {tok_a}"}).status_code == 401


def test_revokedevice_unknown_id_fails(env):
    assert admin.main(["revokedevice", "--id", "999"]) == 1


def test_listdevices_shows_each_machine_and_its_state(client, env, capsys):
    tok = _enroll(client, _invite(capsys), device_name="mac-1").json()["token"]
    _enroll(client, _invite(capsys, name=None), device_name="wsl-2")
    admin.main(["revokedevice", "--id", "2"])
    capsys.readouterr()

    assert admin.main(["listdevices"]) == 0
    out = capsys.readouterr().out
    assert "mac-1" in out and "wsl-2" in out and "dev2@example.test" in out
    assert "revoked" in out
    # Never a token or a hash.
    assert tok not in out and db.hash_token(tok) not in out

    assert admin.main(["listdevices", "--email", "nobody@x.com"]) == 0
    assert "(no devices)" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Single use under concurrency                                                 #
# --------------------------------------------------------------------------- #
def test_concurrent_redemptions_of_one_code_mint_exactly_one_token(client, env, capsys):
    """Two machines racing on the same code: one 201, one 401, one device token.

    `redeem` takes the write lock (BEGIN IMMEDIATE) before reading the code row, so the
    second redemption cannot observe `redeemed_at IS NULL`. Threads against the real
    app, not a mocked lock.
    """
    import threading

    code = _invite(capsys)
    statuses: list[int] = []
    barrier = threading.Barrier(2)

    def race(device: str):
        barrier.wait()
        statuses.append(_enroll(client, code, device_name=device).status_code)

    threads = [threading.Thread(target=race, args=(f"racer-{i}",)) for i in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert sorted(statuses) == [201, 401]
    assert len(_rows(env, "SELECT id FROM device_tokens")) == 1
