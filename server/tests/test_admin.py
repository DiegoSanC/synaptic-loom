"""Tests for the administration CLI (server/admin.py)."""
from server import admin, db


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
