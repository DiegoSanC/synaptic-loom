"""FROZEN GOAL — E13 logbook-query-surface (wave i). Authored at plan time
(2026-08-12, strong model, adversarial mandate). READ-ONLY to implementers:
extend accessories/cases, never weaken an assertion.

Real boundary: REAL uvicorn subprocess on a temp LOOM_DB (real sqlite file, WAL),
token minted by the real ``server.admin adduser``, corpus seeded over real HTTP.
The CLI legs run the real ``loom`` CLI in-process against that server.

Kill-targets (plausible-but-wrong implementations this goal must break):
- interpolating the user query into SQL (hostile-syntax leg → 500),
- a backfill that double-inserts on rerun (idempotency leg),
- snippet/rank leaking into the un-queried listing (byte-compat leg),
- a filter that accepts arbitrary labels (vocab legs),
- a permanent 'label fallbacks: 0' status row (AC-F5.4 precedent leg).
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _goalkit as kit  # noqa: E402

VENV_PY = str(kit.REPO_ROOT / ".venv" / "bin" / "python")

ON_TOPIC = (
    "## Context\nAuth work.\n## Decisions\n"
    "- **Use bearer token authentication for the API** — rejected session "
    "cookies for statelessness. Evidence: server/app.py. Status: current.\n"
)
OFF_TOPIC = [
    "## Context\nDB work.\n## Decisions\n- **Vacuum the database weekly** — "
    "(rationale not stated). Evidence: Makefile. Status: current.\n",
    "## Context\nCI work.\n## Decisions\n- **Cache pip downloads in CI** — "
    "speed. Evidence: ci.yml. Status: current.\n",
    "## Context\nUI work.\n## Decisions\n- **Adopt the slate palette** — "
    "(rationale not stated). Evidence: styles.css. Status: current.\n",
]
HOSTILE_QUERIES = ['"', '*', 'NEAR(', 'a AND', '((', 'x OR "', 'entries*"',
                   '-t', 'NEAR', '"unbalanced']

FROZEN_ENTRY_KEYS = {"id", "author", "session_id", "created_at",
                     "covered_from", "covered_to", "content_md"}


# ---------------------------------------------------------------------------- #
# Accessory                                                                     #
# ---------------------------------------------------------------------------- #
def _adduser(db: Path, name: str = "goal13") -> str:
    cp = subprocess.run(
        [VENV_PY, "-m", "server.admin", "adduser", "--name", name,
         "--email", f"{name}@goal.test"],
        capture_output=True, text=True, cwd=str(kit.REPO_ROOT),
        env={"LOOM_DB": str(db), "PATH": "/usr/bin:/bin"}, check=True,
    )
    return kit.parse_admin_token(cp.stdout)


def _post_entry(base: str, tok: str, *, content: str, sid: str,
                item_types: list[str] | None = None,
                remote: str = kit.TEST_REMOTE_CANONICAL) -> requests.Response:
    import hashlib

    payload = {
        "project_remote": remote, "session_id": sid, "content_md": content,
        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        "model": "sonnet", "prompt_version": "v2",
    }
    if item_types is not None:
        payload["item_types"] = item_types
    return requests.post(f"{base}/api/entries", json=payload,
                         headers={"Authorization": f"Bearer {tok}"}, timeout=10)


def _get_entries(base: str, tok: str, pid: int, **params) -> requests.Response:
    return requests.get(f"{base}/api/projects/{pid}/entries", params=params,
                        headers={"Authorization": f"Bearer {tok}"}, timeout=10)


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e13")
    db = tmp / "loom.db"
    port = kit.find_free_port()
    proc = kit.start_server(db, port, VENV_PY)
    base = f"http://127.0.0.1:{port}"
    try:
        kit.wait_health(base)
        tok = _adduser(db)
        r = requests.post(f"{base}/api/projects",
                          json={"remote_url": kit.TEST_REMOTE_URL},
                          headers={"Authorization": f"Bearer {tok}"}, timeout=10)
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        assert _post_entry(base, tok, content=ON_TOPIC, sid="s-auth").status_code == 201
        for n, md in enumerate(OFF_TOPIC):
            assert _post_entry(base, tok, content=md, sid=f"s-off{n}").status_code == 201
        yield {"base": base, "tok": tok, "pid": pid, "db": db, "tmp": tmp}
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# ---------------------------------------------------------------------------- #
# T.F1 — ranked search at the real HTTP boundary                                #
# ---------------------------------------------------------------------------- #
def test_ac_rt1_query_ranks_on_topic_first(srv):
    r = _get_entries(srv["base"], srv["tok"], srv["pid"],
                     q="bearer token authentication")
    assert r.status_code == 200, r.text
    hits = r.json()["entries"]
    assert hits, "on-topic corpus must produce hits"
    assert "bearer token authentication" in hits[0]["content_md"].lower()
    for h in hits:
        assert isinstance(h.get("snippet"), str) and h["snippet"].strip()
        assert isinstance(h.get("rank"), (int, float))
    ranks = [h["rank"] for h in hits]
    assert ranks == sorted(ranks), "bm25 rank must order best-first (ascending)"


def test_ac_rt1_empty_result_clean_200(srv):
    r = _get_entries(srv["base"], srv["tok"], srv["pid"], q="zzqqxwyy")
    assert r.status_code == 200
    body = r.json()
    assert body["entries"] == [] and body["has_more"] is False


def test_ac_rt1_hostile_queries_never_500(srv):
    for q in HOSTILE_QUERIES:
        r = _get_entries(srv["base"], srv["tok"], srv["pid"], q=q)
        assert r.status_code < 500, f"q={q!r} → {r.status_code}: {r.text[:200]}"
        assert r.status_code in (200, 422), f"q={q!r} → {r.status_code}"
        if r.status_code == 422:
            assert r.json().get("error") == "invalid_query", r.text


def test_no_q_listing_superset_compatible_and_unranked(srv):
    r = _get_entries(srv["base"], srv["tok"], srv["pid"])
    assert r.status_code == 200
    for e in r.json()["entries"]:
        assert set(e) >= FROZEN_ENTRY_KEYS
        assert "snippet" not in e and "rank" not in e, (
            "ranking fields must not leak into the un-queried listing"
        )


# ---------------------------------------------------------------------------- #
# T.F1 — backfill idempotency + trigger on a real legacy DB file                #
# ---------------------------------------------------------------------------- #
def _legacy_db_with_entries(path: Path, n: int = 5) -> sqlite3.Connection:
    """A REAL pre-Block-T DB: schema.sql only (no FTS), entries inserted raw."""
    conn = sqlite3.connect(path)
    conn.executescript((kit.REPO_ROOT / "server" / "schema.sql").read_text())
    conn.execute("INSERT INTO users (name, email, token_hash, created_at) "
                 "VALUES ('u', 'u@x', 'h', '2026-01-01T00:00:00Z')")
    conn.execute("INSERT INTO projects (canonical_remote, name, created_by, "
                 "created_at) VALUES ('github.com/x/y', 'y', 1, '2026-01-01T00:00:00Z')")
    for k in range(n):
        conn.execute(
            "INSERT INTO entries (project_id, user_id, session_id, content_md, "
            "content_hash, created_at) VALUES (1, 1, ?, ?, ?, '2026-01-01T00:00:00Z')",
            (f"legacy-{k}", f"legacy decision number {k} about topic{k}", f"hash-{k}"),
        )
    conn.commit()
    return conn


def test_ac_rt1_backfill_idempotent_and_trigger_covers_new_rows(tmp_path):
    from server import search

    conn = _legacy_db_with_entries(tmp_path / "legacy.db", n=5)
    try:
        search.ensure_search_schema(conn)
        first = conn.execute("SELECT count(*) FROM entries_fts").fetchone()[0]
        assert first == 5, "backfill must index every pre-existing row"
        search.ensure_search_schema(conn)  # rerun: MUST be a no-op
        assert conn.execute("SELECT count(*) FROM entries_fts").fetchone()[0] == 5
        conn.execute(
            "INSERT INTO entries (project_id, user_id, session_id, content_md, "
            "content_hash, created_at) VALUES (1, 1, 's-new', "
            "'a fresh post-ensure decision', 'hash-new', '2026-01-02T00:00:00Z')")
        conn.commit()
        assert conn.execute("SELECT count(*) FROM entries_fts").fetchone()[0] == 6, (
            "the AFTER INSERT trigger must index rows added after ensure"
        )
        rows, _ = search.search_entries(conn, 1, "fresh post-ensure")
        assert rows and "fresh post-ensure" in rows[0]["content_md"]
    finally:
        conn.close()


def test_search_entries_raises_invalid_query_not_operational_error(tmp_path):
    from server import search

    conn = _legacy_db_with_entries(tmp_path / "q.db", n=1)
    try:
        search.ensure_search_schema(conn)
        with pytest.raises(search.InvalidQuery):
            search.search_entries(conn, 1, '"unbalanced')
    finally:
        conn.close()


# ---------------------------------------------------------------------------- #
# T.F1 — WAL concurrency: concurrent insert + search, zero 5xx                  #
# ---------------------------------------------------------------------------- #
def test_ac_rt1_wal_concurrent_insert_and_search(srv):
    errors: list[str] = []
    stop = threading.Event()

    def writer(w: int):
        for k in range(8):
            r = _post_entry(srv["base"], srv["tok"],
                            content=f"## Decisions\n- **concurrent decision "
                                    f"w{w}k{k} zeta** — x. Status: current.\n",
                            sid=f"s-w{w}-{k}")
            if r.status_code != 201:
                errors.append(f"POST w{w}k{k} → {r.status_code}")

    def reader():
        while not stop.is_set():
            r = _get_entries(srv["base"], srv["tok"], srv["pid"], q="zeta")
            if r.status_code >= 500:
                errors.append(f"search → {r.status_code}")

    readers = [threading.Thread(target=reader) for _ in range(3)]
    writers = [threading.Thread(target=writer, args=(w,)) for w in range(3)]
    for t in readers + writers:
        t.start()
    for t in writers:
        t.join(timeout=60)
    stop.set()
    for t in readers:
        t.join(timeout=10)
    assert not errors, errors
    r = _get_entries(srv["base"], srv["tok"], srv["pid"], q="zeta", limit=50)
    assert r.status_code == 200 and len(r.json()["entries"]) == 24, (
        "every concurrently inserted entry must be searchable afterwards"
    )


# ---------------------------------------------------------------------------- #
# T.F4 — typed queryability at the real HTTP boundary                           #
# ---------------------------------------------------------------------------- #
def test_ac_rt23_type_filter_server_side(srv):
    md_m = ("## Decisions\n- [method] **Always run make contract after CLI "
            "updates** — drift guard. Status: current.\n")
    md_d = "## Decisions\n- [decision] **Use argparse** — stdlib. Status: current.\n"
    assert _post_entry(srv["base"], srv["tok"], content=md_m, sid="s-m",
                       item_types=["method"]).status_code == 201
    assert _post_entry(srv["base"], srv["tok"], content=md_d, sid="s-d",
                       item_types=["decision"]).status_code == 201
    r = _get_entries(srv["base"], srv["tok"], srv["pid"], type="method")
    assert r.status_code == 200
    hits = r.json()["entries"]
    assert len(hits) == 1 and "make contract" in hits[0]["content_md"], (
        "?type=method must return exactly the method-labeled entry (untyped and "
        "pre-feature entries excluded)"
    )
    r = _get_entries(srv["base"], srv["tok"], srv["pid"], type="method",
                     q="make contract")
    assert r.status_code == 200 and len(r.json()["entries"]) == 1, (
        "type= must compose with q="
    )


def test_ac_rt23_vocabulary_enforced_422_never_500(srv):
    r = _post_entry(srv["base"], srv["tok"],
                    content="## Decisions\n- **X** — y.\n", sid="s-bad",
                    item_types=["banana"])
    assert r.status_code == 422, r.text
    r = _get_entries(srv["base"], srv["tok"], srv["pid"], type="banana")
    assert r.status_code == 422 and r.json().get("error") == "invalid_type"
    r = _get_entries(srv["base"], srv["tok"], srv["pid"], type="method", q='"')
    assert r.status_code in (200, 422) and r.status_code < 500


# ---------------------------------------------------------------------------- #
# T.F2 / T.F4 — CLI legs (real loom CLI, in-process, against the real server)   #
# ---------------------------------------------------------------------------- #
@pytest.fixture()
def cli_home(srv, tmp_path, monkeypatch):
    home = kit.loom_home(tmp_path)
    kit.write_config(home, server_url=srv["base"], token=srv["tok"])
    monkeypatch.setenv("LOOM_HOME", str(home))
    return home


def test_ac_rt13_grep_framing_first_with_snippets(srv, cli_home, capsys):
    from loom import cli

    rc = cli.main(["log", "phase2-goal", "--grep", "bearer token authentication"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith(cli.REFERENCE_FRAMING), "I5 framing must open the output"
    assert "bearer token authentication" in out.lower()


def test_ac_rt13_malformed_grep_clean_exit_1(srv, cli_home, capsys):
    from loom import cli

    rc = cli.main(["log", "phase2-goal", "--grep", '"unbalanced'])
    captured = capsys.readouterr()
    assert rc == 1
    assert "invalid_query" in captured.err
    assert "Traceback" not in captured.err + captured.out


def test_type_flag_passes_through(srv, cli_home, capsys):
    from loom import cli

    rc = cli.main(["log", "phase2-goal", "--type", "method"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "make contract" in out
    assert "argparse" not in out, "--type method must exclude decision-only entries"


def test_status_fallback_row_only_when_present(srv, cli_home, capsys):
    from loom import cli

    rc = cli.main(["status"])
    clean = capsys.readouterr().out
    assert rc == 0
    assert "label fallback" not in clean.lower(), (
        "no fallback lines → no row (AC-F5.4 no-permanent-zero-row precedent)"
    )
    log = cli_home / "log" / "processor.log"
    log.write_text(
        "2026-08-12T00:00:00Z\ts1\tlabel_fallback\tcount=2\n"
        "2026-08-12T00:00:01Z\ts1\tposted\t\n"
        "2026-08-12T00:00:02Z\ts2\tlabel_fallback\tcount=1\n",
        encoding="utf-8",
    )
    rc = cli.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    row = [ln for ln in out.splitlines() if "label fallback" in ln.lower()]
    assert row and re.search(r"\b3\b", row[0]), (
        "the row must sum the count= details (2+1=3)"
    )


def test_skill_forwards_optional_query():
    text = (kit.REPO_ROOT / "client" / "skills" / "loom" / "SKILL.md").read_text()
    assert "--grep" in text, "/loom must document forwarding an optional query"
