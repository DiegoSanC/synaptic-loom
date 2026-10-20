"""API tests (normative contracts, DESIGN.md §6.2)."""
import asyncio
import re

import httpx
import pytest

from server import db


# --------------------------------------------------------------------------- #
# Auth                                                                         #
# --------------------------------------------------------------------------- #
def test_health_needs_no_auth(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": "0.1.0"}


def test_health_allows_proxy_checks_but_bounds_a_one_ip_burst(client):
    headers = {"Fly-Client-IP": "192.0.2.40"}

    for _ in range(120):
        response = client.get("/api/health", headers=headers)
        assert response.status_code == 200

    limited = client.get("/api/health", headers=headers)
    assert limited.status_code == 429
    assert limited.json()["error"] == "rate_limited"
    assert limited.headers["Retry-After"] == "60"

    # The bound is per client IP; one noisy caller cannot suppress Fly's own probe.
    assert client.get(
        "/api/health", headers={"Fly-Client-IP": "192.0.2.41"}
    ).status_code == 200


@pytest.mark.parametrize(
    ("path", "status"),
    [("/api/health", 200), ("/api/projects", 401)],
)
def test_responses_carry_hsts(client, path, status):
    response = client.get(path)

    assert response.status_code == status
    assert response.headers["Strict-Transport-Security"] == (
        "max-age=31536000; includeSubDomains"
    )


def test_missing_auth_is_401(client):
    r = client.get("/api/projects")
    assert r.status_code == 401
    body = r.json()
    assert body["error"] == "unauthorized"
    assert "detail" in body


def test_malformed_auth_header_is_401(client):
    r = client.get("/api/projects", headers={"Authorization": "Token abc"})
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"


def test_invalid_token_is_401(client, user_factory):
    user_factory("dev1")
    r = client.get("/api/projects", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"


def test_invalid_token_is_audited_without_exposing_the_credential(
        client, user_factory, caplog):
    user_factory("dev1")
    secret = "audit-secret-never-log-this"

    r = client.get(
        "/api/projects",
        headers={
            "Authorization": f"Bearer {secret}",
            "Fly-Client-IP": "203.0.113.17",
        },
    )

    assert r.status_code == 401
    audit_lines = [record.getMessage() for record in caplog.records
                   if record.name == "uvicorn.error.loom.security"]
    assert len(audit_lines) == 1
    line = audit_lines[0]
    assert "outcome=invalid_token" in line
    assert "client_ip=203.0.113.17" in line
    assert re.search(r"timestamp=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", line)
    assert secret not in caplog.text
    assert secret[:8] not in caplog.text
    assert secret[-8:] not in caplog.text


def test_unvalidated_fly_client_ip_is_not_copied_into_the_audit_log(client, caplog):
    secret = "token-shaped-header-value"

    response = client.get(
        "/api/projects",
        headers={
            "Authorization": f"Bearer {secret}",
            "Fly-Client-IP": secret,
        },
    )

    assert response.status_code == 401
    assert "client_ip=unavailable" in caplog.text
    assert secret not in caplog.text


@pytest.mark.parametrize(
    ("authorization", "outcome"),
    [(None, "missing_or_malformed_header"), ("Bearer ", "empty_token")],
)
def test_each_bearer_auth_failure_shape_is_audited(
        client, caplog, authorization, outcome):
    headers = {"Fly-Client-IP": "203.0.113.20"}
    if authorization is not None:
        headers["Authorization"] = authorization

    response = client.get("/api/projects", headers=headers)

    assert response.status_code == 401
    assert f"outcome={outcome}" in caplog.text


def test_one_ip_is_rate_limited_after_ten_failed_auth_attempts(client, caplog):
    headers = {
        "Authorization": "Bearer always-invalid",
        "Fly-Client-IP": "203.0.113.18",
    }

    for _ in range(10):
        assert client.get("/api/projects", headers=headers).status_code == 401

    limited = client.get("/api/projects", headers=headers)
    assert limited.status_code == 429
    assert limited.json()["error"] == "rate_limited"
    assert limited.headers["Retry-After"] == "60"
    assert limited.headers["Strict-Transport-Security"] == (
        "max-age=31536000; includeSubDomains"
    )
    assert "outcome=rate_limited" in caplog.text

    other_ip = dict(headers, **{"Fly-Client-IP": "203.0.113.19"})
    assert client.get("/api/projects", headers=other_ip).status_code == 401


def test_failed_auth_allowance_recovers_after_one_minute(client, monkeypatch):
    from server import rate_limit

    now = [1_000.0]
    monkeypatch.setattr(rate_limit, "monotonic", lambda: now[0])
    headers = {
        "Authorization": "Bearer always-invalid",
        "Fly-Client-IP": "203.0.113.23",
    }

    for _ in range(10):
        assert client.get("/api/projects", headers=headers).status_code == 401
    assert client.get("/api/projects", headers=headers).status_code == 429

    now[0] += 61
    assert client.get("/api/projects", headers=headers).status_code == 401


def test_successful_authentication_does_not_consume_the_failure_allowance(
        client, user_factory):
    user = user_factory("dev1")
    headers = dict(user["auth"], **{"Fly-Client-IP": "203.0.113.21"})

    for _ in range(20):
        assert client.get("/api/projects", headers=headers).status_code == 200


def test_concurrent_successful_authentication_never_consumes_failure_slots(
        client, user_factory):
    from server.app import app

    user = user_factory("concurrent-dev")
    headers = dict(user["auth"], **{"Fly-Client-IP": "203.0.113.24"})

    async def request_together():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as async_client:
            return await asyncio.gather(*(
                async_client.get("/api/projects", headers=headers)
                for _ in range(11)
            ))

    responses = asyncio.run(request_together())

    assert [response.status_code for response in responses] == [200] * 11


def test_inactive_user_is_403_and_audited(client, user_factory, caplog):
    u = user_factory("dev1", active=0)
    headers = dict(u["auth"], **{"Fly-Client-IP": "203.0.113.22"})
    r = client.get("/api/projects", headers=headers)
    assert r.status_code == 403
    assert r.json()["error"] == "forbidden"
    assert "outcome=deactivated_user" in caplog.text


@pytest.mark.parametrize("route", ["/docs", "/redoc", "/openapi.json"])
def test_the_fastapi_doc_routes_need_auth(client, user_factory, route):
    """The three routes FastAPI serves by default are behind the same Bearer wall.

    They are not shaped like `/api/*`, so the middleware's old "exempt everything that is
    not /api/" clause served them to anyone. That was unreachable on loopback and is a
    free recon surface — the whole schema plus a "Try it out" console — the moment this is
    hosted. Asserted on all three routes rather than on `/docs` alone because each one is
    a separate FastAPI default with its own URL, and `openapi.json` is the one that
    actually carries the schema.
    """
    assert client.get(route).status_code == 401

    u = user_factory("dev1")
    authorized = client.get(route, headers=u["auth"])
    assert authorized.status_code == 200, (
        "the fix is an auth boundary, not a deletion of the docs: an authenticated "
        "caller must still get them"
    )


def test_an_unknown_path_is_401_rather_than_404_without_a_credential(client):
    """A deliberate consequence of the allow-list, recorded so it is not read as a bug.

    An anonymous caller now learns nothing about which routes exist. With a valid token
    the 404s are unchanged — every `*_404` test in this file proves that.
    """
    assert client.get("/no-such-route").status_code == 401


# --------------------------------------------------------------------------- #
# Project registration                                                         #
# --------------------------------------------------------------------------- #
def test_register_project_201(client, user_factory):
    u = user_factory("dev2")
    r = client.post("/api/projects", headers=u["auth"], json={"remote_url": "git@github.com:Org/B.git"})
    assert r.status_code == 201
    assert r.json() == {"id": r.json()["id"], "canonical_remote": "github.com/org/b", "name": "b"}


def test_register_project_explicit_name(client, user_factory):
    u = user_factory("dev2")
    r = client.post(
        "/api/projects",
        headers=u["auth"],
        json={"remote_url": "https://github.com/Org/Repo.git", "name": "project-b"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["canonical_remote"] == "github.com/org/repo"
    assert body["name"] == "project-b"


def test_register_duplicate_409_with_who_and_when(client, user_factory):
    u = user_factory("dev2")
    first = client.post("/api/projects", headers=u["auth"], json={"remote_url": "git@github.com:org/b.git"})
    assert first.status_code == 201
    created = first.json()

    # Another dev tries to register the same remote (different raw URL, same identity).
    other = user_factory("dev3")
    dup = client.post(
        "/api/projects", headers=other["auth"], json={"remote_url": "https://github.com/ORG/B"}
    )
    assert dup.status_code == 409
    body = dup.json()
    assert body["error"] == "already_registered"
    assert body["project"] == {
        "id": created["id"],
        "canonical_remote": "github.com/org/b",
        "name": created["name"],
    }
    assert body["registered_by"] == "dev2"           # who
    assert body["registered_at"].endswith("Z")       # when (ISO UTC)


def test_register_requires_remote_or_path_422(client, user_factory):
    u = user_factory("dev2")
    r = client.post("/api/projects", headers=u["auth"], json={"name": "orphan"})
    assert r.status_code == 422
    assert r.json()["error"] == "validation_error"


# --------------------------------------------------------------------------- #
# GET /api/projects                                                            #
# --------------------------------------------------------------------------- #
def test_get_project_by_remote_200(client, user_factory):
    u = user_factory("dev2")
    client.post("/api/projects", headers=u["auth"], json={"remote_url": "git@github.com:org/b.git"})

    # The incoming ?remote= is re-normalized: a raw URL resolves the same.
    r = client.get("/api/projects", headers=u["auth"], params={"remote": "git@github.com:Org/B.git"})
    assert r.status_code == 200
    proj = r.json()["project"]
    assert proj["canonical_remote"] == "github.com/org/b"
    assert proj["created_by"] == "dev2"


def test_get_project_by_remote_404(client, user_factory):
    u = user_factory("dev2")
    r = client.get("/api/projects", headers=u["auth"], params={"remote": "github.com/none/none"})
    assert r.status_code == 404
    assert r.json()["error"] == "project_not_found"


def test_list_projects(client, user_factory):
    u = user_factory("dev2")
    client.post("/api/projects", headers=u["auth"], json={"remote_url": "git@github.com:org/a.git"})
    client.post("/api/projects", headers=u["auth"], json={"remote_url": "git@github.com:org/b.git"})
    r = client.get("/api/projects", headers=u["auth"])
    assert r.status_code == 200
    remotes = {p["canonical_remote"] for p in r.json()["projects"]}
    assert remotes == {"github.com/org/a", "github.com/org/b"}


# --------------------------------------------------------------------------- #
# Registration by path (fallback without a remote)                             #
# --------------------------------------------------------------------------- #
def test_register_by_path_201(client, user_factory):
    u = user_factory("dev2")
    r = client.post(
        "/api/projects", headers=u["auth"], json={"path": "/home/dev/x", "hostname": "wsl-dev2"}
    )
    assert r.status_code == 201
    body = r.json()
    assert body["canonical_remote"] is None
    assert body["name"] == "x"

    # The associated project_paths row was created.
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT project_id FROM project_paths WHERE user_id = ? AND hostname = ? AND path = ?",
            (u["id"], "wsl-dev2", "/home/dev/x"),
        ).fetchone()
        assert row is not None and row["project_id"] == body["id"]
    finally:
        conn.close()


def test_register_by_path_duplicate_409(client, user_factory):
    u = user_factory("dev2")
    first = client.post(
        "/api/projects", headers=u["auth"], json={"path": "/home/dev/x", "hostname": "wsl-dev2"}
    )
    assert first.status_code == 201
    dup = client.post(
        "/api/projects", headers=u["auth"], json={"path": "/home/dev/x", "hostname": "wsl-dev2"}
    )
    assert dup.status_code == 409
    body = dup.json()
    assert body["error"] == "already_registered"
    assert body["project"]["id"] == first.json()["id"]
    assert body["registered_by"] == "dev2"


# --------------------------------------------------------------------------- #
# POST /api/entries                                                            #
# --------------------------------------------------------------------------- #
def _entry_payload(**over):
    base = {
        "project_remote": "github.com/org/b",
        "session_id": "sess-1",
        "covered_from": "uuid-a",
        "covered_to": "uuid-b",
        "content_md": "## Contexto\nse decidió X",
        "content_hash": "hash-1",
        "model": "sonnet",
        "prompt_version": "v1",
    }
    base.update(over)
    return base


def _register_b(client, auth):
    client.post("/api/projects", headers=auth, json={"remote_url": "git@github.com:org/b.git"})


def test_create_entry_201(client, user_factory):
    u = user_factory("dev2")
    _register_b(client, u["auth"])
    r = client.post("/api/entries", headers=u["auth"], json=_entry_payload())
    assert r.status_code == 201
    assert isinstance(r.json()["id"], int)


def test_create_entry_duplicate_hash_409(client, user_factory):
    u = user_factory("dev2")
    _register_b(client, u["auth"])
    assert client.post("/api/entries", headers=u["auth"], json=_entry_payload()).status_code == 201
    dup = client.post("/api/entries", headers=u["auth"], json=_entry_payload(content_md="otro texto"))
    assert dup.status_code == 409
    assert dup.json()["error"] == "duplicate_entry"


def test_create_entry_unknown_project_404(client, user_factory):
    u = user_factory("dev2")
    r = client.post("/api/entries", headers=u["auth"], json=_entry_payload(project_remote="github.com/org/ghost"))
    assert r.status_code == 404
    assert r.json()["error"] == "project_not_found"


def test_create_entry_missing_fields_422(client, user_factory):
    u = user_factory("dev2")
    _register_b(client, u["auth"])
    r = client.post(
        "/api/entries",
        headers=u["auth"],
        json={"project_remote": "github.com/org/b", "session_id": "s"},  # content_md/content_hash missing
    )
    assert r.status_code == 422
    assert r.json()["error"] == "validation_error"


def test_entry_user_id_and_created_at_are_server_side(client, user_factory):
    u = user_factory("dev2")
    _register_b(client, u["auth"])
    # The client tries to forge user_id and created_at; the server ignores both.
    payload = _entry_payload(user_id=99999, created_at="1999-01-01T00:00:00Z")
    r = client.post("/api/entries", headers=u["auth"], json=payload)
    assert r.status_code == 201
    conn = db.connect()
    try:
        row = conn.execute("SELECT user_id, created_at FROM entries WHERE id = ?", (r.json()["id"],)).fetchone()
        assert row["user_id"] == u["id"]
        assert row["created_at"] != "1999-01-01T00:00:00Z"
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# GET /api/projects/{id}/entries                                               #
# --------------------------------------------------------------------------- #
def _seed_entries(env, project_id, user_id, timestamps):
    """Insert entries with a controlled created_at (deterministic order in the tests)."""
    conn = db.connect(env)
    try:
        for i, ts in enumerate(timestamps):
            conn.execute(
                "INSERT INTO entries (project_id, user_id, session_id, covered_from, covered_to, "
                "content_md, content_hash, model, prompt_version, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (project_id, user_id, f"sess-{i}", f"from-{i}", f"to-{i}",
                 f"content-{i}", f"hash-{i}", "sonnet", "v1", ts),
            )
        conn.commit()
    finally:
        conn.close()


def _make_project(env, user_id, remote="github.com/org/b", name="b"):
    conn = db.connect(env)
    try:
        cur = conn.execute(
            "INSERT INTO projects (canonical_remote, name, created_by, created_at) VALUES (?, ?, ?, ?)",
            (remote, name, user_id, db.iso_now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_entries_desc_order_and_author(client, user_factory, env):
    u = user_factory("dev2")
    pid = _make_project(env, u["id"])
    _seed_entries(env, pid, u["id"], [
        "2026-07-20T10:00:00Z",
        "2026-07-21T10:00:00Z",
        "2026-07-22T10:00:00Z",
    ])
    r = client.get(f"/api/projects/{pid}/entries", headers=u["auth"])
    assert r.status_code == 200
    entries = r.json()["entries"]
    created = [e["created_at"] for e in entries]
    assert created == sorted(created, reverse=True)  # DESC
    assert all(e["author"] == "dev2" for e in entries)
    assert entries[0]["covered_from"] and entries[0]["content_md"]


def test_entries_limit_and_has_more(client, user_factory, env):
    u = user_factory("dev2")
    pid = _make_project(env, u["id"])
    _seed_entries(env, pid, u["id"], [f"2026-07-{d:02d}T10:00:00Z" for d in range(1, 6)])  # 5 entries

    r = client.get(f"/api/projects/{pid}/entries", headers=u["auth"], params={"limit": 2})
    assert r.status_code == 200
    body = r.json()
    assert len(body["entries"]) == 2
    assert body["has_more"] is True

    r_all = client.get(f"/api/projects/{pid}/entries", headers=u["auth"], params={"limit": 30})
    assert len(r_all.json()["entries"]) == 5
    assert r_all.json()["has_more"] is False


def test_entries_since_filter(client, user_factory, env):
    u = user_factory("dev2")
    pid = _make_project(env, u["id"])
    _seed_entries(env, pid, u["id"], [
        "2026-07-20T10:00:00Z",
        "2026-07-21T10:00:00Z",
        "2026-07-22T10:00:00Z",
    ])
    r = client.get(
        f"/api/projects/{pid}/entries", headers=u["auth"], params={"since": "2026-07-21T10:00:00Z"}
    )
    assert r.status_code == 200
    created = [e["created_at"] for e in r.json()["entries"]]
    assert created == ["2026-07-22T10:00:00Z"]  # only strictly later entries


def test_entries_default_limit_is_30(client, user_factory, env):
    u = user_factory("dev2")
    pid = _make_project(env, u["id"])
    _seed_entries(env, pid, u["id"], [f"2026-07-{d:02d}T10:00:00Z" for d in range(1, 32)])  # 31 entries
    r = client.get(f"/api/projects/{pid}/entries", headers=u["auth"])
    body = r.json()
    assert len(body["entries"]) == 30
    assert body["has_more"] is True


def test_entries_unknown_project_404(client, user_factory):
    u = user_factory("dev2")
    r = client.get("/api/projects/9999/entries", headers=u["auth"])
    assert r.status_code == 404
    assert r.json()["error"] == "project_not_found"


# --------------------------------------------------------------------------- #
# Follow-up F5 (D13, C5): only the entry's own content_hash answers 409        #
# --------------------------------------------------------------------------- #
def _abort_on_insert(db_path: str, table: str) -> None:
    """A trigger that fails every insert into ``table`` with a CONSTRAINT error that is
    NOT a UNIQUE violation — the class a CHECK, FK or trigger failure belongs to."""
    conn = db.connect(db_path)
    try:
        conn.execute(f"CREATE TRIGGER f5_abort BEFORE INSERT ON {table} "
                     "BEGIN SELECT RAISE(ABORT, 'f5 test abort'); END")
        conn.commit()
    finally:
        conn.close()


def test_a_non_unique_integrity_error_is_a_500_never_duplicate_entry(env, user_factory):
    """Answering it `409 duplicate_entry` tells the processor "already stored": it
    advances its pointer and drops the marker, and the entry is lost for good."""
    from fastapi.testclient import TestClient
    from server.app import app

    u = user_factory("dev2")
    with TestClient(app, raise_server_exceptions=False) as raw:
        _register_b(raw, u["auth"])
        _abort_on_insert(env, "entries")
        r = raw.post("/api/entries", headers=u["auth"], json=_entry_payload())
    assert r.status_code == 500, r.text
    assert r.json()["error"] == "internal_error"


def test_a_failed_item_insert_stores_neither_the_entry_nor_its_items(env, user_factory):
    from fastapi.testclient import TestClient
    from server.app import app

    u = user_factory("dev2")
    content = "## Decisions\n- [fact] **X** — y.\n"
    with TestClient(app, raise_server_exceptions=False) as raw:
        _register_b(raw, u["auth"])
        _abort_on_insert(env, "entry_items")
        r = raw.post("/api/entries", headers=u["auth"], json=_entry_payload(
            content_md=content, prompt_version="v3",
            items=[{"item_type": "fact", "labelled": True, "text_md": "- [fact] **X** — y."}]))
    assert r.status_code == 500, r.text
    conn = db.connect(env)
    try:
        assert conn.execute("SELECT count(*) FROM entries").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM entry_item_types").fetchone()[0] == 0
    finally:
        conn.close()


def test_labelled_must_be_a_real_boolean(client, user_factory):
    u = user_factory("dev2")
    _register_b(client, u["auth"])
    content = "## Decisions\n- [fact] **X** — y.\n"
    r = client.post("/api/entries", headers=u["auth"], json=_entry_payload(
        content_md=content,
        items=[{"item_type": "fact", "labelled": "yes", "text_md": "- [fact] **X** — y."}]))
    assert r.status_code == 422, r.text
