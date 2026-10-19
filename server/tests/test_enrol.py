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

    # D17: a VALID code from the throttled address is redeemed — the window throttles
    # failures, not credentials, and a 32-byte code is not something a flood can guess —
    # and redeeming it neither consumes nor resets the window: the next failure is still 429.
    code = _invite(capsys)
    assert _enroll(client, code, **headers).status_code == 201
    assert _enroll(client, "wrong-code-wrong-code-wrong-code-000", **headers).status_code == 429
    # …while another address is unaffected: its failures are its own, and the spent code
    # is spent for it too.
    other = {"Fly-Client-IP": "192.0.2.61"}
    assert _enroll(client, code, **other).status_code == 401
    assert _enroll(client, _invite(capsys), **other).status_code == 201


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


# --------------------------------------------------------------------------- #
# Renewal chains (D19: B1a, B1b)                                               #
# --------------------------------------------------------------------------- #
def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _renew(client, token: str) -> str:
    r = client.post("/api/device-tokens/renew", headers=_bearer(token))
    assert r.status_code == 201, r.text
    return r.json()["token"]


def _device_id(env, token: str) -> int:
    return _row(env, "SELECT id FROM device_tokens WHERE token_hash = ?", db.hash_token(token))["id"]


def _status(client, token: str) -> int:
    return client.get("/api/projects", headers=_bearer(token)).status_code


def _live_ids(env) -> list[int]:
    now = db.iso_now()
    return [r["id"] for r in _rows(
        env, "SELECT id FROM device_tokens WHERE revoked_at IS NULL AND expires_at > ? ORDER BY id", now)]


def _successors(env) -> dict[int, int | None]:
    return {r["id"]: r["replaced_by"] for r in _rows(env, "SELECT id, replaced_by FROM device_tokens")}


@pytest.mark.parametrize("start", [0, 1, 2])
def test_revoking_any_id_of_a_chain_makes_every_token_of_the_chain_401(client, env, capsys, start):
    t0 = _enroll(client, _invite(capsys)).json()["token"]
    t1 = _renew(client, t0)
    t2 = _renew(client, t1)
    other = _enroll(client, _invite(capsys, name=None), device_name="desk").json()["token"]
    chain = [t0, t1, t2]
    assert [_status(client, t) for t in chain] == [200, 200, 200]  # the overlap of D-2026-09-22

    assert admin.main(["revokedevice", "--id", str(_device_id(env, chain[start]))]) == 0

    assert [_status(client, t) for t in chain] == [401, 401, 401]
    assert _status(client, other) == 200  # another machine of the same person is untouched


def test_a_renew_retried_after_an_unpersisted_successor_revokes_that_successor(client, env, capsys):
    """The client crashed (or lost the 201) before writing S1: it renews O again."""
    old = _enroll(client, _invite(capsys)).json()["token"]
    lost = _renew(client, old)

    retried = client.post("/api/device-tokens/renew", headers=_bearer(old))

    assert retried.status_code == 201, retried.text
    fresh = retried.json()["token"]
    assert _status(client, fresh) == 200
    assert _status(client, lost) == 401
    assert _status(client, old) == 200  # the predecessor still lives until its own expiry
    o, s1, s2 = (_device_id(env, t) for t in (old, lost, fresh))
    # One line, never a fork: O → S1 → S2.
    assert _successors(env) == {o: s1, s1: s2, s2: None}
    assert _live_ids(env) == [o, s2]


def _three_renewals(client, env, capsys) -> dict[str, int]:
    """Renew O→S1, S1→S2, then O again → N. Returns the four ids by name."""
    o = _enroll(client, _invite(capsys)).json()["token"]
    s1 = _renew(client, o)
    s2 = _renew(client, s1)
    n = _renew(client, o)
    return {name: _device_id(env, tok) for name, tok in
            (("O", o), ("S1", s1), ("S2", s2), ("N", n))} | {"tokens": (o, s1, s2, n)}


def test_renewing_an_old_token_revokes_every_token_after_it_and_keeps_one_line(client, env, capsys):
    ids = _three_renewals(client, env, capsys)
    o, s1, s2, n = ids["tokens"]

    assert [_status(client, t) for t in (o, s1, s2, n)] == [200, 401, 401, 200]
    # Strict line O → S1 → S2 → N: the caller keeps pointing at its first successor and the
    # LAST revoked token points at the new one.
    assert _successors(env) == {ids["O"]: ids["S1"], ids["S1"]: ids["S2"], ids["S2"]: ids["N"],
                                ids["N"]: None}


@pytest.mark.parametrize("start", ["O", "S1", "S2", "N"])
def test_after_a_re_issue_revoking_from_any_id_leaves_no_live_token(client, env, capsys, start):
    ids = _three_renewals(client, env, capsys)

    assert admin.main(["revokedevice", "--id", str(ids[start])]) == 0

    assert _live_ids(env) == []
    assert [_status(client, t) for t in ids["tokens"]] == [401, 401, 401, 401]


def test_a_revoke_that_lands_before_the_renew_transaction_refuses_the_renew(client, env, capsys):
    """Interleaving: the bearer middleware has already accepted O, then the operator's revoke
    commits, then `renew` takes the write lock. The renew must re-check and mint nothing."""
    from server import auth

    old = _enroll(client, _invite(capsys)).json()["token"]
    device_id = _device_id(env, old)
    assert admin.main(["revokedevice", "--id", str(device_id)]) == 0

    conn = db.connect(env)
    try:
        with pytest.raises(auth.RenewRefused) as refused:
            auth.renew(conn, device_id)
    finally:
        conn.close()

    assert refused.value.outcome == "revoked_token"
    assert _live_ids(env) == []
    assert len(_rows(env, "SELECT id FROM device_tokens")) == 1


def test_a_renew_that_commits_before_the_revoke_is_revoked_with_its_chain(client, env, capsys):
    old = _enroll(client, _invite(capsys)).json()["token"]
    fresh = _renew(client, old)

    assert admin.main(["revokedevice", "--id", str(_device_id(env, old))]) == 0

    assert _live_ids(env) == []
    assert _status(client, fresh) == 401


def test_concurrent_revoke_and_renew_leave_no_live_token(client, env, capsys):
    """Real threads, real app, real SQLite: whichever order the write lock picks, the
    machine ends with no live token."""
    import threading

    for i in range(10):
        token = _enroll(client, _invite(capsys, name=None if i else "dev2"),
                        device_name=f"race-{i}").json()["token"]
        device_id = _device_id(env, token)
        barrier = threading.Barrier(2)

        def renew():
            barrier.wait()
            client.post("/api/device-tokens/renew", headers=_bearer(token))

        def revoke():
            barrier.wait()
            admin.main(["revokedevice", "--id", str(device_id)])

        threads = [threading.Thread(target=renew), threading.Thread(target=revoke)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        capsys.readouterr()

        assert _live_ids(env) == [], f"iteration {i}"


def test_renew_of_a_deactivated_user_is_refused_inside_the_transaction(client, env, capsys):
    from server import auth

    token = _enroll(client, _invite(capsys)).json()["token"]
    device_id = _device_id(env, token)
    conn = db.connect(env)
    try:
        conn.execute("UPDATE users SET active = 0")
        conn.commit()
        with pytest.raises(auth.RenewRefused) as refused:
            auth.renew(conn, device_id)
    finally:
        conn.close()
    assert refused.value.outcome == "deactivated_user"
    assert len(_rows(env, "SELECT id FROM device_tokens")) == 1


def test_renew_of_an_expired_token_is_refused_inside_the_transaction(client, env, capsys, monkeypatch):
    from server import auth

    monkeypatch.setenv("LOOM_DEVICE_TOKEN_TTL_S", "1")
    token = _enroll(client, _invite(capsys)).json()["token"]
    device_id = _device_id(env, token)
    time.sleep(1.1)
    conn = db.connect(env)
    try:
        with pytest.raises(auth.RenewRefused) as refused:
            auth.renew(conn, device_id)
    finally:
        conn.close()
    assert refused.value.outcome == "expired_token"
    assert len(_rows(env, "SELECT id FROM device_tokens")) == 1


def test_an_existing_database_gains_the_replaced_by_index_on_its_next_start(env):
    conn = db.connect(env)
    try:
        conn.execute("DROP INDEX idx_device_tokens_replaced_by")
        conn.commit()
    finally:
        conn.close()

    db.init_db(env).close()

    index = _row(env, "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'device_tokens' "
                      "AND sql LIKE '%(replaced_by)%'")
    assert index is not None


# --------------------------------------------------------------------------- #
# Orphans left by the pre-D19 renew (B1 "Orphans already in a database")       #
# --------------------------------------------------------------------------- #
ORPHAN_LOGGER = "uvicorn.error.loom.orphans"


def _forked_database(env) -> dict[str, str]:
    """A database as the pre-D19 server left it, built with that server's own writes.

    Pre-D19 ``renew`` inserted the successor and OVERWROTE the caller's ``replaced_by``,
    so renewing one token twice left the first successor pointing nowhere. Here:

    - O enrolled; O renewed twice → S1, then S2 (S1 is orphaned);
    - S1 renewed once → S1b (the orphan's own branch: S1b is referenced, by S1);
    - S2 renewed twice → D, then S3; D was later revoked (a dead orphan);
    - S3 renewed twice → E, then S4; E has expired since (a dead orphan).

    Live tokens no chain from an enrolment reaches: S1 and S1b, and only those.
    """
    import secrets

    from server import auth

    conn = db.connect(env)
    tokens: dict[str, str] = {}
    ids: dict[str, int] = {}
    try:
        user_id = conn.execute(
            "INSERT INTO users (name, email, token_hash, active, created_at) VALUES (?, ?, ?, 1, ?)",
            ("dev2", "dev2@example.test", db.hash_token(secrets.token_urlsafe(32)), db.iso_now()),
        ).lastrowid
        code, _ = auth.create_invitation(conn, user_id, 3600)
        conn.commit()
        enrolled = auth.redeem(conn, code, "mac-1")
        tokens["O"], ids["O"] = enrolled["token"], enrolled["device_token_id"]

        def old_renew(caller: str, name: str, expires_at: str = "2099-01-01T00:00:00Z"):
            tokens[name] = secrets.token_urlsafe(32)
            ids[name] = conn.execute(
                "INSERT INTO device_tokens (user_id, token_hash, device_name, issued_at, expires_at) "
                "VALUES (?, ?, 'mac-1', ?, ?)",
                (user_id, db.hash_token(tokens[name]), db.iso_now(), expires_at),
            ).lastrowid
            conn.execute("UPDATE device_tokens SET replaced_by = ? WHERE id = ?", (ids[name], ids[caller]))

        old_renew("O", "S1")
        old_renew("O", "S2")
        old_renew("S1", "S1b")
        old_renew("S2", "D")
        old_renew("S2", "S3")
        conn.execute("UPDATE device_tokens SET revoked_at = ? WHERE id = ?", (db.iso_now(), ids["D"]))
        old_renew("S3", "E", expires_at="2001-01-01T00:00:00Z")
        old_renew("S3", "S4")
        conn.commit()
    finally:
        conn.close()
    return {"ids": ids, "tokens": tokens}


def _orphan_lines(caplog) -> list[str]:
    return [rec.getMessage() for rec in caplog.records if rec.name == ORPHAN_LOGGER]


def test_startup_logs_the_orphan_count_and_revokes_nothing(env, caplog):
    from fastapi.testclient import TestClient

    from server.app import app

    fork = _forked_database(env)
    revoked_before = _rows(env, "SELECT id, revoked_at FROM device_tokens ORDER BY id")

    with caplog.at_level(logging.INFO, logger=ORPHAN_LOGGER), TestClient(app) as client:
        lines = [line for line in _orphan_lines(caplog) if "device_token_orphans" in line]
        assert len(lines) == 1, _orphan_lines(caplog)
        assert "device_token_orphans count=2" in lines[0]
        assert "revoke-orphans" in lines[0]
        # Never revoked at boot: the orphans still authenticate.
        for name in ("S1", "S1b", "O", "S4"):
            assert _status(client, fork["tokens"][name]) == 200, name

    assert _rows(env, "SELECT id, revoked_at FROM device_tokens ORDER BY id") == revoked_before


def test_revoke_orphans_revokes_exactly_the_orphaned_branches_and_prints_their_ids(env, capsys, caplog):
    from fastapi.testclient import TestClient

    from server.app import app

    fork = _forked_database(env)
    ids, tokens = fork["ids"], fork["tokens"]

    assert admin.main(["revoke-orphans"]) == 0
    out = capsys.readouterr().out

    assert f"chain {ids['S1']}: {ids['S1']} {ids['S1b']}" in out
    printed = {int(n) for line in out.splitlines() if line.strip().startswith("chain ")
               for n in line.split(":", 1)[1].split()}
    assert printed == {ids["S1"], ids["S1b"]}
    assert {r["id"] for r in _rows(env, "SELECT id FROM device_tokens WHERE revoked_at IS NOT NULL")} == {
        ids["S1"], ids["S1b"], ids["D"]}

    with caplog.at_level(logging.INFO, logger=ORPHAN_LOGGER), TestClient(app) as client:
        assert any("device_token_orphans count=0" in line for line in _orphan_lines(caplog))
        assert [_status(client, tokens[n]) for n in ("S1", "S1b")] == [401, 401]
        assert [_status(client, tokens[n]) for n in ("O", "S2", "S3", "S4")] == [200, 200, 200, 200]

    # Idempotent: a second run finds nothing and says so.
    assert admin.main(["revoke-orphans"]) == 0
    assert "No orphaned device tokens" in capsys.readouterr().out


def test_a_database_without_orphans_counts_zero_at_startup(client, env, capsys, caplog):
    from fastapi.testclient import TestClient

    from server.app import app

    old = _enroll(client, _invite(capsys)).json()["token"]
    _renew(client, old)
    _renew(client, old)  # the D19 re-issue: no fork, so no orphan

    with caplog.at_level(logging.INFO, logger=ORPHAN_LOGGER), TestClient(app):
        assert any("device_token_orphans count=0" in line for line in _orphan_lines(caplog))


# --------------------------------------------------------------------------- #
# Re-enrolment revokes the machine's old chain (D19 B2)                        #
# --------------------------------------------------------------------------- #
# The client proves possession of the token it is replacing by sending it as the bearer
# of `POST /api/enroll`; the body never changes (`EnrolRequest` forbids extra fields).
# Only a live DEVICE token of the code's own user counts. Anything else is ignored in
# silence: it is not a failed authentication, and the response is the plain enrolment.
ENROL_KEYS = {"token", "issued_at", "expires_at", "user"}


@pytest.mark.parametrize("sent", ["head", "predecessor"])
def test_re_enrolling_with_the_old_device_token_revokes_its_whole_chain(client, env, capsys, sent):
    t0 = _enroll(client, _invite(capsys)).json()["token"]
    t1 = _renew(client, t0)
    desk = _enroll(client, _invite(capsys, name=None), device_name="desk").json()["token"]
    old = {"head": t1, "predecessor": t0}[sent]  # t0 still authenticates until its expiry

    r = _enroll(client, _invite(capsys, name=None), **_bearer(old))

    assert r.status_code == 201, r.text
    assert set(r.json()) == ENROL_KEYS  # the response is unchanged
    new = r.json()["token"]
    assert [_status(client, t) for t in (t0, t1)] == [401, 401]
    assert _status(client, new) == 200
    assert _status(client, desk) == 200  # another machine of the same person is untouched
    assert _successors(env)[_device_id(env, new)] is None  # a fresh chain, not appended


def test_re_enrolling_with_another_users_device_token_leaves_it_alone(client, env, capsys):
    theirs = _enroll(client, _invite(capsys, email="other@example.test", name="other")).json()["token"]

    r = _enroll(client, _invite(capsys), **_bearer(theirs))

    assert r.status_code == 201, r.text
    assert set(r.json()) == ENROL_KEYS
    assert _status(client, theirs) == 200
    assert _live_ids(env) == [_device_id(env, theirs), _device_id(env, r.json()["token"])]


def test_re_enrolling_with_a_legacy_token_of_the_same_user_leaves_it_alone(client, env, capsys):
    admin.main(["adduser", "--name", "legacy", "--email", "legacy@x.com"])
    legacy = _parse_code(capsys.readouterr().out)

    r = _enroll(client, _invite(capsys, email="legacy@x.com", name=None), **_bearer(legacy))

    assert r.status_code == 201, r.text
    assert _status(client, legacy) == 200  # break-glass: only `deactivate` ends it


def test_a_revoked_token_sent_on_re_enrolment_revokes_nothing(client, env, capsys):
    old = _enroll(client, _invite(capsys)).json()["token"]
    lost = _renew(client, old)
    live = _renew(client, old)  # the re-issue revoked `lost`; `old` and `live` still work

    r = _enroll(client, _invite(capsys, name=None), **_bearer(lost))

    assert r.status_code == 201, r.text
    assert [_status(client, t) for t in (old, live)] == [200, 200]


def test_an_expired_token_sent_on_re_enrolment_revokes_nothing(client, env, capsys, monkeypatch):
    monkeypatch.setenv("LOOM_DEVICE_TOKEN_TTL_S", "1")
    old = _enroll(client, _invite(capsys)).json()["token"]
    monkeypatch.delenv("LOOM_DEVICE_TOKEN_TTL_S")
    live = _renew(client, old)  # a 90-day successor
    time.sleep(1.1)
    assert _status(client, old) == 401

    r = _enroll(client, _invite(capsys, name=None), **_bearer(old))

    assert r.status_code == 201, r.text
    assert _status(client, live) == 200


def test_a_refused_code_never_revokes_the_token_sent_with_it(client, env, capsys):
    old = _enroll(client, _invite(capsys)).json()["token"]

    r = _enroll(client, "wrong-code-wrong-code-wrong-code-000", **_bearer(old))

    assert r.status_code == 401 and r.json()["error"] == "invalid_code"
    assert _status(client, old) == 200


@pytest.mark.parametrize("authorization", [
    "Bearer unknown-token-unknown-token-unknown-000",
    "Bearer ",
    "Bearer",
    "Basic Zm9vOmJhcg==",
    "garbage",
])
def test_a_header_that_proves_nothing_never_fails_the_enrolment_nor_counts(client, env, capsys,
                                                                          authorization):
    """An older server ignores the header outright (the middleware lets the redemption
    path through without reading it); this one ignores everything but a live device token
    of the code's user. Either way the header never turns into a 401, a 422 or a failure
    against the address's window."""
    ip = {"Fly-Client-IP": "192.0.2.90"}
    for _ in range(9):
        assert _enroll(client, "wrong-code-wrong-code-wrong-code-000", **ip).status_code == 401
    bystander = _enroll(client, _invite(capsys, email="by@example.test", name="by")).json()["token"]

    r = _enroll(client, _invite(capsys), **ip, Authorization=authorization)

    assert r.status_code == 201, r.text
    assert set(r.json()) == ENROL_KEYS
    assert _status(client, bystander) == 200
    # Nine failures before, none for the header: the tenth is still a 401, the eleventh 429.
    assert _enroll(client, "wrong-code-wrong-code-wrong-code-000", **ip).status_code == 401
    assert _enroll(client, "wrong-code-wrong-code-wrong-code-000", **ip).status_code == 429
