"""The issuer cascade (D20, DESIGN §6.4 "Issuer cascade").

"Inside the `BEGIN IMMEDIATE` of every chain revocation (`revokedevice`, `deactivate`,
the web's revoke, B2's re-enrolment): collect the chain's ids; revoke every chain minted
from a redeemed setup code whose `issued_by_device_token_id` is one of them; expire
every unredeemed code those ids issued; repeat with the newly revoked ids until nothing
changes." The web's revoke is `test_ui_machines.py`'s; this file drives the other three.

Codes are issued with `auth.create_invitation`, the function the web's routes call,
naming the issuing device as a `/ui` session would; every outcome is read back through
the public surface (`/api/projects` with the token, `/api/enroll` with the code).
"""
from __future__ import annotations

import re
import threading
from itertools import count

import pytest

from server import admin, auth, db

CODE_LINE = re.compile(r"^\s+([A-Za-z0-9_-]{32,})$")
_IPS = count(1)


def _ip() -> str:
    return f"198.51.100.{next(_IPS) % 250 + 1}"


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


def _enrol(client, code: str, device_name: str, bearer: str | None = None):
    headers = {"Fly-Client-IP": _ip()}
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    return client.post("/api/enroll", json={"code": code, "device_name": device_name},
                       headers=headers)


def _machine(client, env, code: str, device_name: str) -> tuple[str, int]:
    r = _enrol(client, code, device_name)
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    return token, _query(env, "SELECT id FROM device_tokens WHERE token_hash = ?",
                         db.hash_token(token))[0]["id"]


def _issue(env, user_id: int, issuer: int | None, *, kind: str = "setup") -> str:
    """A code ``issuer``'s `/ui` session would issue (``auth.create_invitation``)."""
    conn = db.connect(env)
    try:
        code, _ = auth.create_invitation(conn, user_id, 600, kind=kind,
                                         created_by_user_id=user_id,
                                         issued_by_device_token_id=issuer)
        conn.commit()
    finally:
        conn.close()
    return code


def _status(client, token: str) -> int:
    return client.get("/api/projects", headers={"Authorization": f"Bearer {token}",
                                                "Fly-Client-IP": _ip()}).status_code


def _renew(client, token: str) -> str:
    r = client.post("/api/device-tokens/renew", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 201, r.text
    return r.json()["token"]


def _redeems(client, code: str) -> bool:
    return _enrol(client, code, "probe").status_code == 201


@pytest.fixture
def tree(client, env, capsys):
    """A (from `admin.py invite`) issues B's setup code; B issues C's; B renews; A and B
    each leave a setup code unredeemed; Z, another machine of the same user from an
    `admin.py` invitation, and Y, a machine of another user, sit outside the cascade."""
    a, a_id = _machine(client, env, _invite_code(capsys, "dev1@x.test", "dev1"), "a")
    uid = _query(env, "SELECT id FROM users WHERE email = 'dev1@x.test'")[0]["id"]
    b, b_id = _machine(client, env, _issue(env, uid, a_id), "b")
    c, c_id = _machine(client, env, _issue(env, uid, b_id), "c")
    b_renewed = _renew(client, b)
    z, z_id = _machine(client, env, _invite_code(capsys, "dev1@x.test"), "z")
    y, _ = _machine(client, env, _invite_code(capsys, "dev2@x.test", "dev2"), "y")
    t = {"uid": uid, "a": a, "a_id": a_id, "b": b, "b_id": b_id, "b_renewed": b_renewed,
         "c": c, "c_id": c_id, "z": z, "z_id": z_id, "y": y,
         "pending_a": _issue(env, uid, a_id), "pending_b": _issue(env, uid, b_id),
         "pending_z": _issue(env, uid, z_id)}
    for name in ("a", "b", "b_renewed", "c", "z", "y"):
        assert _status(client, t[name]) == 200, name
    return t


def _assert_a_b_c_dead_and_z_y_alive(client, t):
    assert [_status(client, t[n]) for n in ("a", "b", "b_renewed", "c")] == [401] * 4
    assert not _redeems(client, t["pending_a"]) and not _redeems(client, t["pending_b"])
    assert [_status(client, t[n]) for n in ("z", "y")] == [200, 200]
    assert _redeems(client, t["pending_z"])


def test_revokedevice_revokes_two_levels_down_and_expires_their_codes(client, env, capsys, tree):
    assert admin.main(["revokedevice", "--id", str(tree["a_id"])]) == 0
    _assert_a_b_c_dead_and_z_y_alive(client, tree)


def test_the_cascade_runs_downwards_only(client, env, capsys, tree):
    assert admin.main(["revokedevice", "--id", str(tree["b_id"])]) == 0

    assert [_status(client, tree[n]) for n in ("b", "b_renewed", "c")] == [401] * 3
    assert not _redeems(client, tree["pending_b"])
    assert [_status(client, tree[n]) for n in ("a", "z", "y")] == [200] * 3
    assert _redeems(client, tree["pending_a"])


def test_b2_re_enrolment_runs_the_cascade(client, env, capsys, tree):
    again = _enrol(client, _invite_code(capsys, "dev1@x.test"), "a-again", bearer=tree["a"])

    assert again.status_code == 201, again.text
    assert _status(client, again.json()["token"]) == 200
    _assert_a_b_c_dead_and_z_y_alive(client, tree)


def test_deactivate_expires_the_codes_its_machines_issued_for_other_users(client, env,
                                                                         capsys):
    """`deactivate` already revokes every chain of the user and expires their own codes
    (D19 B3); the cascade adds the codes those chains issued for someone else."""
    a, a_id = _machine(client, env, _invite_code(capsys, "dev1@x.test", "dev1"), "a")
    other = _invite_code(capsys, "dev2@x.test", "dev2")             # creates dev2
    other_uid = _query(env, "SELECT id FROM users WHERE email = 'dev2@x.test'")[0]["id"]
    for_other = _issue(env, other_uid, a_id, kind="invite")
    redeemed = _issue(env, other_uid, a_id, kind="invite")
    invited, _ = _machine(client, env, redeemed, "dev2-laptop")

    assert admin.main(["deactivate", "--email", "dev1@x.test"]) == 0
    out = capsys.readouterr().out

    assert "1 device token(s) revoked" in out
    assert "1 pending enrolment code(s) expired" in out             # the cascade's own
    assert _status(client, a) == 403                                 # deactivated first
    assert not _redeems(client, for_other)
    # A redeemed INVITATION is not a setup code: its chain is left alone (wave t's reading).
    assert _status(client, invited) == 200
    assert _redeems(client, other)                                   # admin.py's, no issuer


def test_an_unredeemed_code_of_any_kind_expires_with_its_issuer(client, env, capsys, tree):
    invite = _issue(env, tree["uid"], tree["c_id"], kind="invite")

    assert admin.main(["revokedevice", "--id", str(tree["a_id"])]) == 0

    assert not _redeems(client, invite)


def test_a_pre_d20_chain_without_an_issuer_revokes_alone(client, env, capsys):
    """Rows that predate D20 carry the migration's defaults: `kind='invite'`, no issuer.
    Such a chain revokes as before, and no other revocation reaches it."""
    old, old_id = _machine(client, env, _invite_code(capsys, "dev1@x.test", "dev1"), "old")
    other, other_id = _machine(client, env, _invite_code(capsys, "dev1@x.test"), "other")
    row = _query(env, "SELECT kind, issued_by_device_token_id FROM enrolment_codes "
                      "WHERE device_token_id = ?", old_id)
    assert row == [{"kind": "invite", "issued_by_device_token_id": None}]

    assert admin.main(["revokedevice", "--id", str(other_id)]) == 0
    assert _status(client, old) == 200
    assert admin.main(["revokedevice", "--id", str(old_id)]) == 0
    assert _status(client, old) == 401


def test_the_fixpoint_terminates_on_an_issuer_cycle(client, env, capsys):
    """No public path makes a cycle (a code is issued before the chain it mints), but the
    walk must not depend on that: A's own enrolment code is rewritten by SQL to be a setup
    code C issued, so A → B → C → A. Revoking any of them ends all three, and returns."""
    a, a_id = _machine(client, env, _invite_code(capsys, "dev1@x.test", "dev1"), "a")
    uid = _query(env, "SELECT id FROM users WHERE email = 'dev1@x.test'")[0]["id"]
    b, b_id = _machine(client, env, _issue(env, uid, a_id), "b")
    c, c_id = _machine(client, env, _issue(env, uid, b_id), "c")
    conn = db.connect(env)
    try:
        conn.execute("UPDATE enrolment_codes SET kind = 'setup', issued_by_device_token_id = ? "
                     "WHERE device_token_id = ?", (c_id, a_id))
        conn.commit()
    finally:
        conn.close()

    done: list[int] = []
    worker = threading.Thread(target=lambda: done.append(
        admin.main(["revokedevice", "--id", str(b_id)])), daemon=True)
    worker.start()
    worker.join(timeout=30)

    assert done == [0], "the cascade must reach a fixpoint"
    assert [_status(client, t) for t in (a, b, c)] == [401] * 3
    assert "3 token(s)" in capsys.readouterr().out
