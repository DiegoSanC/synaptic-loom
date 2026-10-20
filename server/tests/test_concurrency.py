"""Concurrency: simultaneous writes neither corrupt nor raise (DESIGN.md §6).

WAL + busy_timeout must absorb the contention of several clients writing at once.
"""
import threading

from server import db


def _make_project_and_user(env):
    conn = db.connect(env)
    try:
        cur = conn.execute(
            "INSERT INTO users (name, email, token_hash, active, created_at) VALUES (?, ?, ?, 1, ?)",
            ("dev1", "dev1@x.com", db.hash_token("t"), db.iso_now()),
        )
        uid = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO projects (canonical_remote, name, created_by, created_at) VALUES (?, ?, ?, ?)",
            ("github.com/org/c", "c", uid, db.iso_now()),
        )
        pid = cur.lastrowid
        conn.commit()
        return uid, pid
    finally:
        conn.close()


def test_concurrent_entry_writes_do_not_corrupt_or_raise(env):
    uid, pid = _make_project_and_user(env)
    n_threads, per_thread = 8, 25
    errors: list[Exception] = []

    def worker(t: int):
        try:
            conn = db.connect(env)  # one connection per thread
            try:
                for i in range(per_thread):
                    conn.execute(
                        "INSERT INTO entries (project_id, user_id, session_id, content_md, content_hash, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (pid, uid, "sess", "md", f"hash-{t}-{i}", db.iso_now()),
                    )
                    conn.commit()
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 — the point of the test is that NOTHING raises
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert errors == [], f"escrituras concurrentes lanzaron: {errors}"

    conn = db.connect(env)
    try:
        total = conn.execute("SELECT COUNT(*) AS c FROM entries").fetchone()["c"]
        # integrity_check confirms the file did not end up corrupted.
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    assert total == n_threads * per_thread
    assert integrity == "ok"


def test_concurrent_api_posts(client, user_factory, env):
    """Several threads POSTing entries through the API at once: all 201, no corruption."""
    from concurrent.futures import ThreadPoolExecutor

    u = user_factory("dev2")
    client.post("/api/projects", headers=u["auth"], json={"remote_url": "git@github.com:org/b.git"})

    def post(i: int):
        return client.post(
            "/api/entries",
            headers=u["auth"],
            json={
                "project_remote": "github.com/org/b",
                "session_id": f"sess-{i}",
                "content_md": f"contenido {i}",
                "content_hash": f"api-hash-{i}",
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(post, range(30)))

    assert all(s == 201 for s in statuses)
    conn = db.connect(env)
    try:
        total = conn.execute("SELECT COUNT(*) AS c FROM entries").fetchone()["c"]
    finally:
        conn.close()
    assert total == 30
