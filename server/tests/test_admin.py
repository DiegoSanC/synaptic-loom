"""Tests for the administration CLI (server/admin.py)."""
import json
import os
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
