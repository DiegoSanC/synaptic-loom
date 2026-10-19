"""Tests for the administration CLI (server/admin.py)."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from server import admin, db

COMPOSE_FILE = Path(__file__).resolve().parents[2] / "deploy" / "compose" / "docker-compose.yml"


def test_adduser_creates_user_and_prints_token_once(env, capsys):
    rc = admin.main(["adduser", "--name", "dev2", "--email", "dev2@example.test"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "TOKEN" in out and "ONLY ONCE" in out

    # The printed token validates against the stored hash; the hash is never printed.
    conn = db.connect(env)
    try:
        row = conn.execute("SELECT token_hash, active FROM users WHERE email = ?", ("dev2@example.test",)).fetchone()
    finally:
        conn.close()
    assert row is not None and row["active"] == 1

    printed_token = None
    for line in out.splitlines():
        line = line.strip()
        if line and " " not in line and len(line) > 20 and "TOKEN" not in line:
            printed_token = line
    assert printed_token is not None
    assert db.hash_token(printed_token) == row["token_hash"]
    assert row["token_hash"] not in out  # the hash does not leak through stdout


def test_adduser_duplicate_email_fails(env):
    assert admin.main(["adduser", "--name", "dev2", "--email", "d@x.com"]) == 0
    assert admin.main(["adduser", "--name", "otro", "--email", "d@x.com"]) == 1


def test_deactivate(env):
    admin.main(["adduser", "--name", "dev2", "--email", "d@x.com"])
    assert admin.main(["deactivate", "--email", "d@x.com"]) == 0
    conn = db.connect(env)
    try:
        assert conn.execute("SELECT active FROM users WHERE email = ?", ("d@x.com",)).fetchone()["active"] == 0
    finally:
        conn.close()


def test_deactivate_unknown_email_fails(env):
    assert admin.main(["deactivate", "--email", "ghost@x.com"]) == 1


def test_listusers(env, capsys):
    admin.main(["adduser", "--name", "dev2", "--email", "dev2@x.com"])
    admin.main(["adduser", "--name", "dev3", "--email", "dev3@x.com"])
    capsys.readouterr()  # discard the adduser output
    assert admin.main(["listusers"]) == 0
    out = capsys.readouterr().out
    assert "dev2@x.com" in out and "dev3@x.com" in out


# --------------------------------------------------------------------------- #
# invite: the invitation link (D19, plan 2026-10-19 "Invitation link")         #
# --------------------------------------------------------------------------- #
# The lines `invite` printed before D19, as literals: they stay byte-identical, because
# callers other than the waves may parse them. Only a link line may follow them.
INVITE_LINES = (
    "Invitation for dev2 (dev2@example.test): single use, valid until {expires_at}.\n"
    "\n"
    "  ENROLMENT CODE (shown ONLY ONCE — burned when redeemed, useless afterwards):\n"
    "    {code}\n"
    "\n"
    "  Send it through the team's channel. On the machine, `loom enroll` reads it at a\n"
    "  hidden prompt and mints that machine's own device token; the device token never travels.\n"
)


def _invite(env, capsys, *extra: str):
    """Run `invite` for dev2; return (stdout, stderr, code, expires_at)."""
    assert admin.main(["invite", "--email", "dev2@example.test", "--name", "dev2", *extra]) == 0
    captured = capsys.readouterr()
    code = captured.out.splitlines()[3].strip()
    conn = db.connect(env)
    try:
        row = conn.execute("SELECT expires_at FROM enrolment_codes WHERE code_hash = ?",
                           (db.hash_token(code),)).fetchone()
    finally:
        conn.close()
    assert row is not None, captured.out
    return captured.out, captured.err, code, row["expires_at"]


def test_invite_appends_one_link_line_when_the_public_url_is_set(env, capsys, monkeypatch):
    monkeypatch.setenv("LOOM_PUBLIC_URL", "https://loom.example.com")
    out, err, code, expires_at = _invite(env, capsys)
    assert out == (INVITE_LINES.format(code=code, expires_at=expires_at)
                   + f"https://loom.example.com/ui/join#code={code}\n")
    assert err == ""


def test_invite_on_fly_links_to_the_apps_own_hostname(env, capsys, monkeypatch):
    monkeypatch.delenv("LOOM_PUBLIC_URL", raising=False)
    monkeypatch.setenv("FLY_APP_NAME", "loom-test")
    out, err, code, expires_at = _invite(env, capsys)
    assert out == (INVITE_LINES.format(code=code, expires_at=expires_at)
                   + f"https://loom-test.fly.dev/ui/join#code={code}\n")
    assert err == ""


def test_invite_public_url_flag_overrides_both_settings(env, capsys, monkeypatch):
    monkeypatch.setenv("LOOM_PUBLIC_URL", "https://configured.example.com")
    monkeypatch.setenv("FLY_APP_NAME", "loom-test")
    out, _, code, expires_at = _invite(env, capsys, "--public-url", "https://other.example.org/")
    assert out == (INVITE_LINES.format(code=code, expires_at=expires_at)
                   + f"https://other.example.org/ui/join#code={code}\n")


def test_invite_without_a_public_url_keeps_stdout_and_hints_on_stderr(env, capsys, monkeypatch):
    monkeypatch.delenv("LOOM_PUBLIC_URL", raising=False)
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    out, err, code, expires_at = _invite(env, capsys)
    assert out == INVITE_LINES.format(code=code, expires_at=expires_at)
    assert "LOOM_PUBLIC_URL" in err and "--public-url" in err
    assert code not in err


def test_compose_gives_the_server_its_public_url_from_the_domain(tmp_path):
    """The Compose stack's server learns its public URL from the one domain Caddy serves."""
    if shutil.which("docker") is None:
        pytest.skip("`docker compose config` renders the file under test")
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    environ = {k: v for k, v in os.environ.items() if not k.startswith("LOOM_")}
    environ["LOOM_DOMAIN"] = "loom.example.com"
    done = subprocess.run(
        ["docker", "compose", "--env-file", str(empty), "-f", str(COMPOSE_FILE),
         "config", "--format", "json"],
        capture_output=True, text=True, env=environ, timeout=120)
    assert done.returncode == 0, done.stderr
    server_env = json.loads(done.stdout)["services"]["loom"]["environment"]
    assert server_env["LOOM_PUBLIC_URL"] == "https://loom.example.com"
# Machines: renewal chains in listdevices, a truthful deactivate (D19)         #
# --------------------------------------------------------------------------- #
CODE_LINE = re.compile(r"^\s+([A-Za-z0-9_-]{32,})$")


def _invite_code(capsys, email, name=None) -> str:
    argv = ["invite", "--email", email] + (["--name", name] if name else [])
    assert admin.main(argv) == 0
    return next(m.group(1) for line in capsys.readouterr().out.splitlines()
                if (m := CODE_LINE.match(line)))


def _enroll(client, code, device_name) -> str:
    r = client.post("/api/enroll", json={"code": code, "device_name": device_name})
    assert r.status_code == 201, r.text
    return r.json()["token"]


def _renew(client, token) -> str:
    r = client.post("/api/device-tokens/renew", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 201, r.text
    return r.json()["token"]


def _get(client, token):
    return client.get("/api/projects", headers={"Authorization": f"Bearer {token}"})


def _query(env, sql, *params):
    conn = db.connect(env)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_listdevices_keeps_one_row_per_token_and_names_each_chain_by_its_root(client, env, capsys):
    laptop = _enroll(client, _invite_code(capsys, "dev2@x.com", "dev2"), "laptop")   # id 1
    _renew(client, _renew(client, laptop))                                     # ids 2, 3
    _enroll(client, _invite_code(capsys, "dev2@x.com"), "desk")                     # id 4
    capsys.readouterr()

    assert admin.main(["listdevices"]) == 0
    lines = capsys.readouterr().out.splitlines()

    assert lines[0].split()[-1] == "chain"
    rows = [line.split() for line in lines[1:]]
    # id, state, email, device, issued_at, expires_at, chain
    assert [(r[0], r[1], r[3], r[-1]) for r in rows] == [
        ("1", "replaced", "laptop", "1"),
        ("2", "replaced", "laptop", "1"),
        ("3", "active", "laptop", "1"),
        ("4", "active", "desk", "4"),
    ]


def test_deactivate_revokes_every_chain_and_expires_pending_codes_and_says_so(client, env, capsys):
    laptop = _enroll(client, _invite_code(capsys, "dev2@x.com", "dev2"), "laptop")
    laptop2 = _renew(client, laptop)
    desk = _enroll(client, _invite_code(capsys, "dev2@x.com"), "desk")
    pending = [_invite_code(capsys, "dev2@x.com"), _invite_code(capsys, "dev2@x.com")]
    other = _enroll(client, _invite_code(capsys, "dev3@x.com", "dev3"), "theirs")
    other_pending = _invite_code(capsys, "dev3@x.com")

    assert admin.main(["deactivate", "--email", "dev2@x.com"]) == 0
    out = capsys.readouterr().out

    assert "dev2@x.com deactivated" in out
    assert "3 device token(s) revoked" in out
    assert "2 pending enrolment code(s) expired" in out
    assert "token is now revoked" not in out  # the pre-D19 claim, false for legacy tokens

    now = db.iso_now()
    dev2_tokens = _query(env, "SELECT revoked_at FROM device_tokens d JOIN users u ON u.id = d.user_id "
                              "WHERE u.email = 'dev2@x.com'")
    assert len(dev2_tokens) == 3 and all(r["revoked_at"] is not None for r in dev2_tokens)
    for code in pending:
        row = _query(env, "SELECT expires_at FROM enrolment_codes WHERE code_hash = ?", db.hash_token(code))[0]
        assert row["expires_at"] <= now

    # Revoked, not merely blocked: re-activating the row by SQL (there is no `reactivate`)
    # brings no machine and no pending code back.
    conn = db.connect(env)
    try:
        conn.execute("UPDATE users SET active = 1 WHERE email = 'dev2@x.com'")
        conn.commit()
    finally:
        conn.close()
    assert [_get(client, t).status_code for t in (laptop, laptop2, desk)] == [401, 401, 401]
    for code in pending:
        assert client.post("/api/enroll", json={"code": code, "device_name": "x"}).status_code == 401

    # Nobody else is touched.
    assert _get(client, other).status_code == 200
    assert client.post("/api/enroll", json={"code": other_pending, "device_name": "y"}).status_code == 201


def test_deactivate_refuses_the_legacy_token_too(client, env, capsys):
    admin.main(["adduser", "--name", "legacy", "--email", "legacy@x.com"])
    token = next(line.strip() for line in capsys.readouterr().out.splitlines()
                 if CODE_LINE.match(line))
    assert _get(client, token).status_code == 200

    assert admin.main(["deactivate", "--email", "legacy@x.com"]) == 0
    out = capsys.readouterr().out

    assert "0 device token(s) revoked" in out
    r = _get(client, token)
    assert r.status_code == 403 and r.json()["error"] == "forbidden"


def test_the_runbook_mis_sent_code_one_liner_expires_only_that_users_pending_codes(client, env, capsys):
    """RUNBOOK §4's documented remedy, run as written (python -m sqlite3: the image has no
    sqlite3 binary), with the Fly form carrying the very same SQL."""
    import shlex
    import subprocess
    import sys
    from pathlib import Path

    runbook = (Path(__file__).resolve().parents[2] / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    local = next(line for line in runbook.splitlines() if line.startswith(".venv/bin/python -m sqlite3 "))
    on_fly = next(line for line in runbook.splitlines()
                  if line.startswith("fly ssh console") and "python -m sqlite3" in line)
    sql = shlex.split(local)[-1]
    assert shlex.split(shlex.split(on_fly)[-1]) == ["python", "-m", "sqlite3", "/data/loom.db", sql]

    mis_sent = _invite_code(capsys, "dev2@example.test", "dev2")
    used = _invite_code(capsys, "dev2@example.test")
    machine = _enroll(client, used, "laptop")
    theirs = _invite_code(capsys, "dev3@x.com", "dev3")

    argv = [sys.executable if a == ".venv/bin/python" else a for a in shlex.split(local)]
    argv = [env if a == "$LOOM_DB" else a for a in argv]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr

    assert client.post("/api/enroll", json={"code": mis_sent, "device_name": "x"}).status_code == 401
    assert _get(client, machine).status_code == 200      # not a deactivate: the machine keeps working
    assert client.post("/api/enroll", json={"code": theirs, "device_name": "y"}).status_code == 201
    assert client.post("/api/enroll", json={"code": _invite_code(capsys, "dev2@example.test"),
                                            "device_name": "z"}).status_code == 201  # a fresh invite works
