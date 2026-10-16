"""Fixtures shared by the server tests.

Every test runs against a temporary SQLite DB (env `LOOM_DB` pointed at tmp_path);
`./loom.db` is never created in the repo.
"""
import secrets

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point LOOM_DB at a temporary file and apply the schema.

    The suite runs as the Fly deployment does (`fly.toml [env]`, D17): the client address
    is the `Fly-Client-IP` header, which is how these tests give each scenario its own
    failure window. The `peer` default and `header:<name>` are `test_client_ip.py`'s.
    """
    path = tmp_path / "loom.db"
    monkeypatch.setenv("LOOM_DB", str(path))
    monkeypatch.setenv("LOOM_CLIENT_IP_SOURCE", "fly-client-ip")
    monkeypatch.setenv("FLY_APP_NAME", "loom-test")
    from server import db

    db.init_db(str(path)).close()
    return str(path)


@pytest.fixture
def client(env):
    """TestClient for the app; its lifespan re-applies the schema (idempotent)."""
    from server.app import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def user_factory(env):
    """Create users directly in the DB and return the token + auth header."""
    from server import db

    def _make(name="dev1", email=None, active=1):
        email = email or f"{name}@example.com"
        conn = db.connect(env)
        try:
            token = secrets.token_urlsafe(32)
            cur = conn.execute(
                "INSERT INTO users (name, email, token_hash, active, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, email, db.hash_token(token), active, db.iso_now()),
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "id": cur.lastrowid,
            "name": name,
            "email": email,
            "token": token,
            "auth": {"Authorization": f"Bearer {token}"},
        }

    return _make

@pytest.fixture
def ui(env):
    """An HTTPS TestClient for the UI (D14): the session cookie is `Secure`, so plain
    http would drop it from the jar."""
    from server.app import app

    with TestClient(app, base_url="https://testserver") as c:
        yield c

@pytest.fixture
def ui_user(ui, user_factory):
    """A user signed in to the UI on `ui` (the session cookie is in its jar)."""
    user = user_factory("dev1")
    code = ui.post("/api/ui-login", headers=user["auth"]).json()["code"]
    assert ui.post("/ui/session", json={"code": code}).status_code == 204
    return user
