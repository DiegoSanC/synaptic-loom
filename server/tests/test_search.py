"""E13 — FTS5 search substrate + typed queryability (T.F1 / T.F2 / T.F4).

Derived from the acceptance criteria of RT.1/RT.2.3 in
``docs/IMPORTED-IMPROVEMENTS-SPEC.md``:

- a seeded corpus ranks the on-topic entry first and an empty result is a clean
  ``([], False)`` — never an error;
- FTS5-reserved characters in the query (``"``, ``*``, ``NEAR(``, a stray ``-``…)
  can never escape as a ``sqlite3.OperationalError`` (→ ``InvalidQuery`` → 422),
  and never reach SQL as anything but a bound MATCH parameter;
- the backfill is idempotent (rerun changes nothing, also on a COPY of a real
  DB file written by the running server) and the AFTER INSERT trigger covers
  every row added afterwards;
- the WAL concurrency test (`test_concurrency.py`), extended to concurrent insert + search;
- the closed ``ITEM_TYPES`` vocabulary is enforced on both the write side
  (``POST /api/entries``) and the read side (``GET ?type=``), 422 never 500;
- the un-queried listing keeps today's shape exactly (no ``snippet``/``rank``).
"""
from __future__ import annotations

import shutil
import sqlite3
import threading

import pytest

from server import db, search

# Same hostile set the frozen goal uses, plus the shapes that make FTS5 answer with
# wording that LOOKS internal for what is a plain user error — "no such column" for a
# stray `-`, and (the one that shipped a 500) the terms FTS5 echoes back into its own
# message: `*interrupted` produced "unknown special query: interrupted", which any
# classifier reading the message text mistakes for SQLITE_INTERRUPT.
HOSTILE_QUERIES = [
    '"', '*', 'NEAR(', 'a AND', '((', 'x OR "', 'entries*"', '-t', 'NEAR',
    '"unbalanced', 'nosuchcol: x', 'col:*', '', '   ', 'a)', '^', 'NOT',
    '*interrupted', '-interrupted*', '* interrupted', '*out of memory',
    '*database is locked', '*disk i/o error', '-corrupt*',
]

FROZEN_ENTRY_KEYS = {"id", "author", "session_id", "created_at",
                     "covered_from", "covered_to", "content_md"}

ON_TOPIC = "bearer token authentication for the HTTP API"
OFF_TOPIC = ["vacuum the database weekly", "cache pip downloads in CI",
             "adopt the slate palette"]


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _legacy_db(path, contents, *, project_id: int = 1) -> sqlite3.Connection:
    """A REAL pre-Block-T database: schema.sql only, entries inserted raw."""
    conn = sqlite3.connect(str(path))
    conn.executescript(db.SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute("INSERT INTO users (name, email, token_hash, created_at) "
                 "VALUES ('u', 'u@x', 'h', '2026-01-01T00:00:00Z')")
    conn.execute("INSERT INTO projects (canonical_remote, name, created_by, created_at) "
                 "VALUES ('github.com/x/y', 'y', 1, '2026-01-01T00:00:00Z')")
    for k, md in enumerate(contents):
        conn.execute(
            "INSERT INTO entries (project_id, user_id, session_id, content_md, "
            "content_hash, created_at) VALUES (?, 1, ?, ?, ?, ?)",
            (project_id, f"s-{k}", md, f"hash-{k}", f"2026-01-0{k + 1}T00:00:00Z"),
        )
    conn.commit()
    return conn


def _indexed_doc_count(conn: sqlite3.Connection) -> int:
    """Documents really present in the FTS index.

    ``SELECT count(*) FROM entries_fts`` would NOT do: on an external-content
    table a full scan reads the *content* table, so it answers "how many entries
    exist", not "how many are indexed" — it cannot see a missing backfill nor a
    double one. The ``_docsize`` shadow table is the index's own bookkeeping.
    """
    return conn.execute("SELECT count(*) FROM entries_fts_docsize").fetchone()[0]


def _project(client, auth, remote="git@github.com:org/e13.git") -> int:
    resp = client.post("/api/projects", headers=auth, json={"remote_url": remote})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _post(client, auth, content, sid, *, item_types=None, remote="github.com/org/e13"):
    body = {"project_remote": remote, "session_id": sid, "content_md": content,
            "content_hash": f"h-{sid}"}
    if item_types is not None:
        body["item_types"] = item_types
    return client.post("/api/entries", headers=auth, json=body)


@pytest.fixture
def seeded(client, user_factory):
    """A project with one on-topic and three off-topic entries, over the API."""
    u = user_factory("dev13")
    pid = _project(client, u["auth"])
    assert _post(client, u["auth"], ON_TOPIC, "s-on").status_code == 201
    for n, md in enumerate(OFF_TOPIC):
        assert _post(client, u["auth"], md, f"s-off{n}").status_code == 201
    return {"user": u, "auth": u["auth"], "pid": pid}


# --------------------------------------------------------------------------- #
# T.F1 — schema, backfill, trigger                                             #
# --------------------------------------------------------------------------- #
def test_ensure_creates_fts_item_types_and_trigger(tmp_path):
    conn = _legacy_db(tmp_path / "legacy.db", ["one decision"])
    try:
        search.ensure_search_schema(conn)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE name IN "
            "('entries_fts', 'entry_item_types') OR type = 'trigger'")}
        assert "entries_fts" in names and "entry_item_types" in names
        assert any(r[0] for r in conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'entries'"))
    finally:
        conn.close()


def test_backfill_indexes_preexisting_rows_and_rerun_is_a_no_op(tmp_path):
    conn = _legacy_db(tmp_path / "legacy.db", [f"legacy decision {k}" for k in range(5)])
    try:
        search.ensure_search_schema(conn)
        assert _indexed_doc_count(conn) == 5
        search.ensure_search_schema(conn)
        search.ensure_search_schema(conn)
        assert _indexed_doc_count(conn) == 5, "the backfill must not double-index"
        # A double-indexed corpus would return the same entry twice here.
        rows, _ = search.search_entries(conn, 1, "legacy", limit=50)
        assert len(rows) == 5
        assert len({r["id"] for r in rows}) == 5
    finally:
        conn.close()


def test_backfill_idempotent_on_a_copy_of_a_real_server_db(env, client, user_factory, tmp_path):
    """Same guarantee, against a copy of a DB the running server actually wrote."""
    u = user_factory("dev-copy")
    _project(client, u["auth"], "git@github.com:org/copy.git")
    for k in range(3):
        assert _post(client, u["auth"], f"real entry {k}", f"c{k}",
                     remote="github.com/org/copy").status_code == 201

    copy_path = tmp_path / "copy.db"
    shutil.copyfile(env, copy_path)
    conn = sqlite3.connect(str(copy_path))
    try:
        before = _indexed_doc_count(conn)
        assert before == 3
        search.ensure_search_schema(conn)
        assert _indexed_doc_count(conn) == before
        rows, _ = search.search_entries(conn, 1, "real entry", limit=50)
        assert len(rows) == 3
    finally:
        conn.close()


def test_trigger_indexes_rows_inserted_after_ensure(tmp_path):
    conn = _legacy_db(tmp_path / "legacy.db", ["one decision"])
    try:
        search.ensure_search_schema(conn)
        conn.execute("INSERT INTO entries (project_id, user_id, session_id, content_md, "
                     "content_hash, created_at) VALUES (1, 1, 's-new', "
                     "'a fresh post-ensure decision', 'h-new', '2026-02-01T00:00:00Z')")
        conn.commit()
        assert _indexed_doc_count(conn) == 2
        rows, _ = search.search_entries(conn, 1, "fresh post-ensure")
        assert len(rows) == 1 and "fresh post-ensure" in rows[0]["content_md"]
    finally:
        conn.close()


def test_ensure_raises_runtime_error_naming_the_compile_option_without_fts5():
    """A sqlite build without FTS5 must fail LOUDLY, never degrade silently."""
    class _NoFts5:
        def execute(self, sql, *args):
            if "fts5" in sql.lower():
                raise sqlite3.OperationalError("no such module: fts5")
            raise AssertionError(f"nothing may run before the FTS5 probe: {sql}")

        def commit(self):  # pragma: no cover - never reached
            raise AssertionError("should not commit")

    with pytest.raises(RuntimeError) as excinfo:
        search.ensure_search_schema(_NoFts5())
    assert "FTS5" in str(excinfo.value)
    assert "ENABLE_FTS5" in str(excinfo.value)


class _Fts5LessConnection:
    """A real connection on a build where the fts5 module is gone (B3 scenario).

    Only `CREATE VIRTUAL TABLE ... USING fts5` behaves differently — exactly what a
    Python rebuilt without ENABLE_FTS5 would do to a database that already holds the
    objects (its plain tables, including the fts5 shadow tables, still read fine).
    """

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *args):
        if "using fts5" in sql.lower():
            raise sqlite3.OperationalError("no such module: fts5")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_the_fts5_guard_fires_on_a_database_that_already_has_the_objects(tmp_path):
    """`CREATE ... IF NOT EXISTS` short-circuits, so the guard must not depend on it.

    The scenario the RuntimeError exists for is not a fresh database — it is an
    existing one opened by a sqlite build that lost FTS5. Probed through the DDL, the
    check would pass silently and leave every search answering 422 and every POST
    500ing: the silently-unsearchable logbook the module docstring refuses to run.
    """
    conn = _legacy_db(tmp_path / "existing.db", ["one decision"])
    try:
        search.ensure_search_schema(conn)  # fully migrated, objects present
        with pytest.raises(RuntimeError) as excinfo:
            search.ensure_search_schema(_Fts5LessConnection(conn))
        assert "ENABLE_FTS5" in str(excinfo.value)
    finally:
        conn.close()


def _apply_ddl_without_backfill(conn: sqlite3.Connection) -> None:
    """Exactly what an interrupted migration leaves behind: objects, no backfill."""
    conn.execute(search._FTS_DDL)
    conn.execute(search._TRIGGER_DDL)
    conn.execute(search._ITEM_TYPES_DDL)
    conn.execute(search._ITEM_TYPES_INDEX_DDL)
    conn.commit()


def test_ensure_heals_a_migration_whose_backfill_never_ran(tmp_path):
    """The trapped state: trigger live, index holding 1 of 6 documents.

    Reachable whenever the backfill dies after the DDL — a lock timeout against a
    concurrent writer, SIGKILL, a full disk, systemd's `Restart=always`. The next
    POST makes the index non-empty, so an "is the index empty?" test declares this
    database healthy forever and five legacy entries are silently unsearchable.
    """
    conn = _legacy_db(tmp_path / "trapped.db", [f"legacy decision {k}" for k in range(5)])
    try:
        _apply_ddl_without_backfill(conn)
        conn.execute("INSERT INTO entries (project_id, user_id, session_id, content_md, "
                     "content_hash, created_at) VALUES (1, 1, 's-new', "
                     "'a fresh post-crash decision', 'h-new', '2026-02-01T00:00:00Z')")
        conn.commit()
        assert _indexed_doc_count(conn) == 1, "precondition: the trapped state"
        assert search.search_entries(conn, 1, "legacy decision")[0] == []

        search.ensure_search_schema(conn)

        assert _indexed_doc_count(conn) == 6, "the next ensure must heal the index"
        rows, _ = search.search_entries(conn, 1, "legacy decision", limit=50)
        assert len(rows) == 5
        rows, _ = search.search_entries(conn, 1, "post-crash", limit=50)
        assert len(rows) == 1, "and must not duplicate the row the trigger indexed"
    finally:
        conn.close()


def test_an_interrupted_migration_leaves_no_trigger_behind(tmp_path, monkeypatch):
    """DDL + backfill are ONE transaction, so a failure rolls back the trigger too.

    Python's sqlite3 opens an implicit transaction for DML only: without an explicit
    BEGIN, each CREATE auto-commits and the trigger outlives the failed backfill —
    the very state the healing test above has to repair.
    """
    conn = _legacy_db(tmp_path / "interrupted.db", [f"legacy {k}" for k in range(3)])
    try:
        monkeypatch.setattr(
            search, "_BACKFILL_SQL",
            "INSERT INTO entries_fts(entries_fts) VALUES('no_such_command')")
        with pytest.raises(sqlite3.OperationalError):
            search.ensure_search_schema(conn)
        objects = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert not (objects & set(search._OBJECT_NAMES)), (
            "a half-applied migration must leave nothing behind: "
            f"{sorted(objects & set(search._OBJECT_NAMES))}"
        )
        assert conn.in_transaction is False, "the failed migration must not hold a tx"

        monkeypatch.undo()  # the retry, with a working backfill, completes cleanly
        search.ensure_search_schema(conn)
        assert _indexed_doc_count(conn) == 3
    finally:
        conn.close()


def test_verifying_a_migrated_database_writes_nothing(tmp_path):
    """The steady state is a pure read — so pure it works on a read-only connection.

    Which is also what keeps the FTS5 guard cheap enough to run FIRST and always: it
    probes the module in the `temp` schema, never in the database file.
    """
    path = tmp_path / "readonly.db"
    conn = _legacy_db(path, ["one decision"])
    search.ensure_search_schema(conn)
    conn.close()

    readonly = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        search.ensure_search_schema(readonly)
        rows, _ = search.search_entries(readonly, 1, "one decision")
        assert len(rows) == 1
    finally:
        readonly.close()


def test_init_db_wires_the_search_schema(tmp_path):
    conn = db.init_db(str(tmp_path / "fresh.db"))
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert {"entries_fts", "entry_item_types"} <= names
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# T.F1 — search_entries                                                        #
# --------------------------------------------------------------------------- #
@pytest.fixture
def corpus(tmp_path):
    conn = _legacy_db(tmp_path / "corpus.db", [ON_TOPIC, *OFF_TOPIC])
    search.ensure_search_schema(conn)
    yield conn
    conn.close()


def test_ranks_the_on_topic_entry_first_with_snippet_and_rank(corpus):
    rows, has_more = search.search_entries(corpus, 1, "bearer token authentication")
    assert rows and has_more is False
    assert ON_TOPIC in rows[0]["content_md"]
    assert set(rows[0].keys()) == FROZEN_ENTRY_KEYS | {"snippet", "rank"}
    for row in rows:
        assert isinstance(row["snippet"], str) and row["snippet"].strip()
        assert isinstance(row["rank"], (int, float))
    ranks = [r["rank"] for r in rows]
    assert ranks == sorted(ranks), "bm25 ranks ascending = best first"


def test_no_match_is_an_empty_result_not_an_error(corpus):
    assert search.search_entries(corpus, 1, "zzqqxwyy") == ([], False)


def test_search_is_scoped_to_the_project(tmp_path):
    conn = _legacy_db(tmp_path / "scoped.db", [ON_TOPIC])
    try:
        conn.execute("INSERT INTO projects (id, canonical_remote, name, created_by, "
                     "created_at) VALUES (2, 'github.com/x/z', 'z', 1, '2026-01-01T00:00:00Z')")
        conn.execute("INSERT INTO entries (project_id, user_id, session_id, content_md, "
                     "content_hash, created_at) VALUES (2, 1, 's-other', ?, 'h-other', "
                     "'2026-01-05T00:00:00Z')", (ON_TOPIC,))
        conn.commit()
        search.ensure_search_schema(conn)
        rows, _ = search.search_entries(conn, 1, "bearer token", limit=50)
        assert [r["id"] for r in rows] == [1]
    finally:
        conn.close()


@pytest.mark.parametrize("hostile", HOSTILE_QUERIES)
def test_hostile_queries_raise_invalid_query_never_operational_error(corpus, hostile):
    try:
        rows, has_more = search.search_entries(corpus, 1, hostile)
    except search.InvalidQuery:
        return
    assert isinstance(rows, list) and isinstance(has_more, bool)


def test_a_query_echoed_into_the_error_message_is_still_a_client_error(corpus):
    """The 500 that shipped: FTS5 quotes the term back, the classifier read the text.

    `*interrupted` → "unknown special query: interrupted". Classified on the message,
    that reads as SQLITE_INTERRUPT and escapes as a bare-text HTTP 500; classified on
    the result code it is what it is — SQLITE_ERROR, a malformed expression.
    """
    for query in ("*interrupted", "* interrupted", "-interrupted*", "*out of memory",
                  "*database is locked", "*disk i/o error", "-corrupt*"):
        with pytest.raises(search.InvalidQuery):
            search.search_entries(corpus, 1, query)


def test_infrastructure_faults_are_never_disguised_as_invalid_query(tmp_path):
    """A REAL lock timeout (SQLITE_BUSY) must stay an OperationalError → 500."""
    path = tmp_path / "busy.db"
    conn = _legacy_db(path, [ON_TOPIC])
    search.ensure_search_schema(conn)
    conn.close()

    writer = sqlite3.connect(str(path))
    reader = sqlite3.connect(str(path), timeout=0)  # no busy waiting
    try:
        writer.execute("BEGIN EXCLUSIVE")
        writer.execute("INSERT INTO entries (project_id, user_id, session_id, "
                       "content_md, content_hash, created_at) VALUES (1, 1, 's', 'x', "
                       "'h-x', '2026-03-01T00:00:00Z')")
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            search.search_entries(reader, 1, "bearer token")
        assert excinfo.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert not isinstance(excinfo.value, search.InvalidQuery)
    finally:
        writer.rollback()
        writer.close()
        reader.close()


def test_invalid_query_is_a_value_error_and_hides_the_sqlite_message(corpus):
    with pytest.raises(search.InvalidQuery) as excinfo:
        search.search_entries(corpus, 1, '"unbalanced')
    assert isinstance(excinfo.value, ValueError)
    assert "unbalanced" not in str(excinfo.value), "must not echo the user's query"


def test_query_terms_are_bound_never_interpolated(corpus):
    """A payload that would be catastrophic if concatenated into the SQL."""
    payload = "x\") ; DROP TABLE entries; --"
    try:
        search.search_entries(corpus, 1, payload)
    except search.InvalidQuery:
        pass
    assert corpus.execute("SELECT count(*) FROM entries").fetchone()[0] == 4


def test_plain_words_are_terms_not_fts5_grammar(corpus):
    """A hyphen (or any prose punctuation) must not turn a question into an error.

    Raw FTS5 reads ``-ensure`` as an inverted column filter ("no such column:
    ensure"); the frozen goal requires ``fresh post-ensure`` to find its entry.
    """
    conn = corpus
    conn.execute("INSERT INTO entries (project_id, user_id, session_id, content_md, "
                 "content_hash, created_at) VALUES (1, 1, 's-h', "
                 "'a fresh post-ensure decision, e.g. note: fix', 'h-h', "
                 "'2026-01-09T00:00:00Z')")
    conn.commit()
    for query in ("fresh post-ensure", "note: fix", "post-ensure, decision"):
        rows, _ = search.search_entries(conn, 1, query, limit=50)
        assert len(rows) == 1, f"{query!r} found {len(rows)} rows"
        assert rows[0]["session_id"] == "s-h"


def test_plain_words_are_and_ed(corpus):
    assert search.search_entries(corpus, 1, "bearer palette")[0] == []


def test_explicit_fts5_expressions_are_honoured(corpus):
    rows, _ = search.search_entries(corpus, 1, '"vacuum the database"', limit=50)
    assert len(rows) == 1
    rows, _ = search.search_entries(corpus, 1, "bearer OR palette", limit=50)
    assert len(rows) == 2
    rows, _ = search.search_entries(corpus, 1, "authent*", limit=50)
    assert len(rows) == 1


@pytest.mark.parametrize("query, expected", [
    ("fresh post-ensure", '"fresh" "post-ensure"'),
    ("note: fix", '"note:" "fix"'),
    ("-t", '"-t"'),
    ("  spaced   out  ", '"spaced" "out"'),
    ('"an exact phrase"', '"an exact phrase"'),   # expression: verbatim
    ("a OR b", "a OR b"),
    ("pre*", "pre*"),
])
def test_query_to_match_expression_mapping(query, expected):
    """The exact term/expression split the frozen goal pins down."""
    assert search._fts_expression(query) == expected


def test_the_term_branch_is_unreachable_for_a_query_carrying_a_quote(corpus):
    """Nothing can break out of the term quoting: a `"` forces the verbatim branch.

    So a crafted term cannot close its own quote and append an operator — FTS5
    parses it as the expression the user actually wrote (valid or a syntax error),
    and either way it stays inside the MATCH parameter.
    """
    for query in ('x" OR "a', 'x" NEAR "a', 'zzqq" OR "wyyz'):
        assert search._fts_expression(query) == query
        rows, _ = search.search_entries(corpus, 1, query, limit=50)
        assert rows == [], "an injected operator must not turn into a match-all"


def test_blank_and_oversized_queries_are_invalid(corpus):
    for query in ("", "   ", "x" * (search.MAX_QUERY_LEN + 1)):
        with pytest.raises(search.InvalidQuery):
            search.search_entries(corpus, 1, query)


def test_limit_caps_the_rows_and_reports_has_more(corpus):
    rows, has_more = search.search_entries(corpus, 1, "the OR a OR weekly OR CI OR palette",
                                           limit=2)
    assert len(rows) == 2 and has_more is True
    rows, has_more = search.search_entries(corpus, 1, "bearer", limit=2)
    assert len(rows) == 1 and has_more is False


def test_since_composes_with_the_query(corpus):
    rows, _ = search.search_entries(corpus, 1, "weekly OR palette", limit=50)
    assert len(rows) == 2
    rows, _ = search.search_entries(corpus, 1, "weekly OR palette",
                                    since="2026-01-03T00:00:00Z", limit=50)
    assert len(rows) == 1 and "palette" in rows[0]["content_md"]


def test_item_type_filter_composes_with_the_query(corpus):
    corpus.execute("INSERT INTO entry_item_types (entry_id, item_type) VALUES (1, 'method')")
    corpus.commit()
    rows, _ = search.search_entries(corpus, 1, "bearer OR weekly", item_type="method", limit=50)
    assert [r["id"] for r in rows] == [1]
    rows, _ = search.search_entries(corpus, 1, "bearer OR weekly", item_type="task", limit=50)
    assert rows == []


def test_unknown_item_type_is_refused_by_the_substrate(corpus):
    with pytest.raises(ValueError):
        search.search_entries(corpus, 1, "bearer", item_type="banana")


# --------------------------------------------------------------------------- #
# T.F1 — WAL concurrency (test_concurrency.py, extended to insert + search)   #
# --------------------------------------------------------------------------- #
def test_concurrent_insert_and_search_neither_raise_nor_lose_rows(env):
    conn = db.connect(env)
    try:
        conn.execute("INSERT INTO users (name, email, token_hash, active, created_at) "
                     "VALUES ('c', 'c@x', 'h', 1, ?)", (db.iso_now(),))
        conn.execute("INSERT INTO projects (canonical_remote, name, created_by, created_at) "
                     "VALUES ('github.com/org/wal', 'wal', 1, ?)", (db.iso_now(),))
        conn.commit()
    finally:
        conn.close()

    n_writers, per_writer = 3, 8
    errors: list[str] = []
    stop = threading.Event()

    def writer(w: int):
        try:
            c = db.connect(env)
            try:
                for k in range(per_writer):
                    c.execute(
                        "INSERT INTO entries (project_id, user_id, session_id, content_md, "
                        "content_hash, created_at) VALUES (1, 1, ?, ?, ?, ?)",
                        (f"s-{w}-{k}", f"concurrent decision w{w}k{k} zeta",
                         f"hash-{w}-{k}", db.iso_now()),
                    )
                    c.commit()
            finally:
                c.close()
        except Exception as exc:  # noqa: BLE001 — nothing may raise
            errors.append(f"writer {w}: {exc!r}")

    def reader():
        try:
            c = db.connect(env)
            try:
                while not stop.is_set():
                    search.search_entries(c, 1, "zeta", limit=50)
            finally:
                c.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"reader: {exc!r}")

    readers = [threading.Thread(target=reader) for _ in range(3)]
    writers = [threading.Thread(target=writer, args=(w,)) for w in range(n_writers)]
    for t in readers + writers:
        t.start()
    for t in writers:
        t.join(timeout=60)
    stop.set()
    for t in readers:
        t.join(timeout=10)

    assert errors == []
    conn = db.connect(env)
    try:
        rows, _ = search.search_entries(conn, 1, "zeta", limit=100)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    assert len(rows) == n_writers * per_writer
    assert integrity == "ok"


# --------------------------------------------------------------------------- #
# T.F1 — HTTP surface                                                          #
# --------------------------------------------------------------------------- #
def test_listing_without_q_keeps_todays_shape(seeded, client):
    resp = client.get(f"/api/projects/{seeded['pid']}/entries", headers=seeded["auth"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_more"] is False
    for entry in body["entries"]:
        # Additive, matching the frozen oracle's own `>=` check: the seven frozen keys
        # are all there and `snippet`/`rank` are not. Block T's `status`/`links_in`/
        # `links_out` (t-entry-links) are unconditional entity state, not query
        # metadata, so an `==` here would over-pin relative to the contract.
        assert FROZEN_ENTRY_KEYS <= set(entry), "the frozen fields must all be served"
        assert not {"snippet", "rank"} & set(entry), (
            "no snippet/rank in the un-queried listing"
        )


def test_q_returns_ranked_hits_with_snippet_and_rank(seeded, client):
    resp = client.get(f"/api/projects/{seeded['pid']}/entries", headers=seeded["auth"],
                      params={"q": "bearer token authentication"})
    assert resp.status_code == 200
    hits = resp.json()["entries"]
    assert hits and "bearer token authentication" in hits[0]["content_md"]
    for hit in hits:
        assert FROZEN_ENTRY_KEYS | {"snippet", "rank"} <= set(hit)
        assert hit["snippet"].strip()
    assert [h["rank"] for h in hits] == sorted(h["rank"] for h in hits)


def test_q_with_no_match_is_a_clean_200(seeded, client):
    resp = client.get(f"/api/projects/{seeded['pid']}/entries", headers=seeded["auth"],
                      params={"q": "zzqqxwyy"})
    assert resp.status_code == 200
    assert resp.json() == {"entries": [], "has_more": False}


@pytest.mark.parametrize("hostile", HOSTILE_QUERIES)
def test_hostile_q_is_422_or_200_never_500(seeded, client, hostile):
    resp = client.get(f"/api/projects/{seeded['pid']}/entries", headers=seeded["auth"],
                      params={"q": hostile})
    assert resp.status_code in (200, 422), resp.text
    if resp.status_code == 422:
        assert resp.json()["error"] == "invalid_query"


# The words sqlite puts in its own OperationalError messages. FTS5 echoes a rejected
# term back into ITS message, so any of these, sent as a query, lands inside an error
# string that a text-matching classifier reads as an infrastructure failure.
_SQLITE_ERROR_WORDS = (
    "interrupted", "locked", "corrupt", "malformed", "readonly", "full",
    "out of memory", "disk i/o error", "no such table", "database is locked",
    "unable to open database", "database disk image is malformed",
)
_QUERY_SHAPES = ("{w}", "*{w}", "* {w}", "-{w}*", '"{w}', "{w}*", "^{w}", "{w}: x",
                 "({w}", "{w} AND")


@pytest.mark.parametrize("word", _SQLITE_ERROR_WORDS)
def test_no_sqlite_error_word_in_a_query_can_become_a_500(seeded, client, word):
    """The generalisation of the `*interrupted` 500: never classify on message text.

    A user query is attacker-controlled text that FTS5 splices into its own error
    message, so every sqlite failure word can be made to appear in one. Whether the
    fault is the client's is decided by the RESULT CODE, which no query can forge.
    """
    for shape in _QUERY_SHAPES:
        query = shape.format(w=word)
        resp = client.get(f"/api/projects/{seeded['pid']}/entries",
                          headers=seeded["auth"], params={"q": query})
        assert resp.status_code in (200, 422), f"q={query!r} → {resp.status_code}"
        if resp.status_code == 422:
            assert resp.json()["error"] == "invalid_query"


def test_invalid_query_body_leaks_no_internals(seeded, client):
    resp = client.get(f"/api/projects/{seeded['pid']}/entries", headers=seeded["auth"],
                      params={"q": '"unbalanced'})
    assert resp.status_code == 422
    body = resp.text.lower()
    assert "unbalanced" not in body, "the error body must not reflect the query back"
    for internal in ("sqlite", "operationalerror", "traceback", "select ", "entries_fts"):
        assert internal not in body


def test_an_unexpected_fault_is_a_json_500_not_bare_text(seeded, monkeypatch):
    """No response may leave this API as plain text with no error code."""
    from fastapi.testclient import TestClient

    from server.app import app

    def _boom(*args, **kwargs):
        raise RuntimeError("q=secret-terms leaked into the exception message")

    monkeypatch.setattr(search, "search_entries", _boom)
    with TestClient(app, raise_server_exceptions=False) as raw:
        resp = raw.get(f"/api/projects/{seeded['pid']}/entries",
                       headers=seeded["auth"], params={"q": "bearer"})
    assert resp.status_code == 500
    assert resp.json()["error"] == "internal_error"
    assert "secret-terms" not in resp.text and "RuntimeError" not in resp.text
    assert resp.headers["Strict-Transport-Security"] == (
        "max-age=31536000; includeSubDomains"
    )


def test_q_composes_with_limit(seeded, client):
    resp = client.get(f"/api/projects/{seeded['pid']}/entries", headers=seeded["auth"],
                      params={"q": "weekly OR CI OR palette", "limit": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["entries"]) == 2 and body["has_more"] is True


def test_q_on_an_unknown_project_is_404(seeded, client):
    resp = client.get("/api/projects/9999/entries", headers=seeded["auth"],
                      params={"q": "bearer"})
    assert resp.status_code == 404
    assert resp.json()["error"] == "project_not_found"


# --------------------------------------------------------------------------- #
# T.F4 — typed queryability                                                    #
# --------------------------------------------------------------------------- #
def test_item_types_round_trip_and_filter_server_side(seeded, client, env):
    assert _post(client, seeded["auth"], "always run make contract", "s-m",
                 item_types=["method"]).status_code == 201
    assert _post(client, seeded["auth"], "use argparse", "s-d",
                 item_types=["decision", "decision"]).status_code == 201

    resp = client.get(f"/api/projects/{seeded['pid']}/entries", headers=seeded["auth"],
                      params={"type": "method"})
    assert resp.status_code == 200
    hits = resp.json()["entries"]
    assert len(hits) == 1 and "make contract" in hits[0]["content_md"]
    assert FROZEN_ENTRY_KEYS <= set(hits[0]), "the frozen fields must all be served"
    assert not {"snippet", "rank"} & set(hits[0]), (
        "a type-only listing carries no snippet/rank"
    )

    conn = db.connect(env)
    try:
        rows = conn.execute("SELECT entry_id, item_type FROM entry_item_types "
                            "ORDER BY entry_id, item_type").fetchall()
    finally:
        conn.close()
    assert [tuple(r) for r in rows] == [(5, "method"), (6, "decision")], "deduplicated"


def test_type_composes_with_q(seeded, client):
    assert _post(client, seeded["auth"], "always run make contract", "s-m",
                 item_types=["method"]).status_code == 201
    assert _post(client, seeded["auth"], "always run make contract too", "s-m2",
                 item_types=["decision"]).status_code == 201
    resp = client.get(f"/api/projects/{seeded['pid']}/entries", headers=seeded["auth"],
                      params={"type": "method", "q": "make contract"})
    assert resp.status_code == 200
    hits = resp.json()["entries"]
    assert len(hits) == 1 and hits[0]["session_id"] == "s-m"


def test_absent_or_empty_item_types_writes_no_rows(seeded, client, env):
    assert _post(client, seeded["auth"], "no labels here", "s-none").status_code == 201
    assert _post(client, seeded["auth"], "empty labels", "s-empty",
                 item_types=[]).status_code == 201
    conn = db.connect(env)
    try:
        assert conn.execute("SELECT count(*) FROM entry_item_types").fetchone()[0] == 0
    finally:
        conn.close()


def test_the_database_also_enforces_the_closed_vocabulary(env):
    """The CHECK is the backstop for writers that never see the endpoint's validator."""
    conn = db.connect(env)
    try:
        conn.execute("INSERT INTO users (name, email, token_hash, active, created_at) "
                     "VALUES ('v', 'v@x', 'h', 1, ?)", (db.iso_now(),))
        conn.execute("INSERT INTO projects (canonical_remote, name, created_by, "
                     "created_at) VALUES ('github.com/org/v', 'v', 1, ?)", (db.iso_now(),))
        conn.execute("INSERT INTO entries (project_id, user_id, session_id, content_md, "
                     "content_hash, created_at) VALUES (1, 1, 's', 'x', 'h', ?)",
                     (db.iso_now(),))
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO entry_item_types (entry_id, item_type) "
                         "VALUES (1, 'banana')")
        for item_type in search.ITEM_TYPES:  # and every frozen label is accepted
            conn.execute("INSERT INTO entry_item_types (entry_id, item_type) "
                         "VALUES (1, ?)", (item_type,))
        conn.commit()
    finally:
        conn.close()


def test_unknown_item_type_on_post_is_422(seeded, client, env):
    resp = _post(client, seeded["auth"], "bad label", "s-bad", item_types=["banana"])
    assert resp.status_code == 422
    assert resp.json()["error"] == "validation_error"
    conn = db.connect(env)
    try:
        assert conn.execute("SELECT count(*) FROM entries WHERE session_id = 's-bad'"
                            ).fetchone()[0] == 0, "the entry must not be stored either"
    finally:
        conn.close()


def test_non_list_item_types_is_422_not_500(seeded, client):
    body = {"project_remote": "github.com/org/e13", "session_id": "s-weird",
            "content_md": "x", "content_hash": "h-weird", "item_types": "method"}
    resp = client.post("/api/entries", headers=seeded["auth"], json=body)
    assert resp.status_code == 422


def test_unknown_type_query_param_is_422_invalid_type(seeded, client):
    resp = client.get(f"/api/projects/{seeded['pid']}/entries", headers=seeded["auth"],
                      params={"type": "banana"})
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_type"


def test_unknown_type_wins_over_a_hostile_q_and_never_500(seeded, client):
    resp = client.get(f"/api/projects/{seeded['pid']}/entries", headers=seeded["auth"],
                      params={"type": "banana", "q": '"'})
    assert resp.status_code == 422
    resp = client.get(f"/api/projects/{seeded['pid']}/entries", headers=seeded["auth"],
                      params={"type": "method", "q": '"'})
    assert resp.status_code in (200, 422)


def test_type_filter_excludes_entries_that_predate_the_feature(seeded, client):
    """The seeded corpus was posted without item_types: it must not leak into ?type=."""
    resp = client.get(f"/api/projects/{seeded['pid']}/entries", headers=seeded["auth"],
                      params={"type": "decision"})
    assert resp.status_code == 200
    assert resp.json()["entries"] == []
