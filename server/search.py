"""Block T (RT.1, RT.2.3 substrate): FTS5 search + typed-item queryability (E13).

Additive-only schema extension per the frozen ``phase1-db-schema`` seam:
``ensure_search_schema(conn)`` is idempotent DDL (CREATE ... IF NOT EXISTS + a
one-shot, rerunnable backfill) called from ``db.init_db`` — ``schema.sql`` stays
untouched. FTS5 must be compiled into the stdlib sqlite3 build; if it is not,
``ensure_search_schema`` raises loudly (spec §1: RT.1 blocks, never degrades).

Frozen interface (t-entries-fts / t-entry-item-types): see ``DESIGN.md`` §12.3, RT.1 and
RT.2.3.

Security note (the one rule this module exists to keep): a user's search terms
reach SQLite **exclusively** as the bound right-hand side of ``MATCH``. Nothing
here ever concatenates a value into SQL — the only parts of a statement built in
Python are fixed clause fragments chosen by whether an *optional filter is
present*, and every value they introduce is a placeholder. FTS5 answers a
malformed expression with ``sqlite3.OperationalError``; that is a client error,
not a server fault, so it surfaces as :class:`InvalidQuery` (→ 422) and its
message — which quotes the offending query back — never leaves the server.
"""
from __future__ import annotations

import sqlite3

# Closed label vocabulary (t-entry-item-types). Duplicated client-side in the
# processor by design: server and client are separate packages and the vocabulary
# is a frozen contract, not shared code.
ITEM_TYPES = ("decision", "method", "task", "fact")

# Hard ceiling for `limit`, mirroring the endpoint's own clamp: the substrate is
# also called directly (admin scripts, later epics) and a search must never be
# talked into materialising the whole corpus.
MAX_LIMIT = 500
DEFAULT_LIMIT = 30

# Longest accepted search string. The endpoint is authenticated, but a query is the
# one place a caller hands the server an expression to *parse*, and an unbounded one
# is free work; past this it is a client error, not a search.
MAX_QUERY_LEN = 512

# What makes a query an FTS5 EXPRESSION rather than a bag of words (see
# `_fts_expression`): the phrase/prefix/grouping metacharacters and the four
# uppercase operators. Everything else — including the `-` of `post-ensure`, which
# raw FTS5 reads as an inverted column filter and rejects with "no such column" —
# is treated as literal text.
_EXPRESSION_CHARS = frozenset('"*()')
_EXPRESSION_OPERATORS = frozenset(("AND", "OR", "NOT", "NEAR"))

# snippet() rendering: markdown emphasis around the matched terms (the entries are
# markdown), an ellipsis for the elided text, at most 12 tokens of context.
_SNIPPET_OPEN = "**"
_SNIPPET_CLOSE = "**"
_SNIPPET_ELLIPSIS = "…"
_SNIPPET_TOKENS = 12

# Which sqlite result codes mean "the caller's expression was bad" rather than "this
# server is broken". SQLITE_ERROR (1) is what FTS5 returns for EVERY malformed MATCH
# expression; every other code — BUSY/LOCKED (contention), FULL (no disk space, and
# the ranked query does use a temp b-tree for ORDER BY), READONLY, IOERR, CORRUPT,
# NOMEM, INTERRUPT — is an infrastructure fault that must stay a 500.
#
# This used to be a deny-list of message SUBSTRINGS, and that was a real bug: FTS5
# quotes the offending term back inside its message, so `q=*interrupted` produced
# "unknown special query: interrupted", matched the "interrupted" entry, and turned a
# plain client error into an HTTP 500. Result codes are structural; message text is
# attacker-influenced. Never classify on the text.
_CLIENT_ERROR_CODES = frozenset({sqlite3.SQLITE_ERROR})


class InvalidQuery(ValueError):
    """Malformed FTS5 query syntax (mapped to 422 by app.py, never a 500)."""


# --------------------------------------------------------------------------- #
# Schema (idempotent, additive)                                                #
# --------------------------------------------------------------------------- #
# External content: the index stores no copy of the text, it points at
# entries.content_md by rowid. Entries are append-only (D7 — they are never
# updated nor deleted), so an AFTER INSERT trigger is the whole synchronisation
# story; there is no UPDATE/DELETE drift to keep up with in v1.
_FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5("
    "content_md, content='entries', content_rowid='id')"
)
_TRIGGER_DDL = (
    "CREATE TRIGGER IF NOT EXISTS entries_ai_fts AFTER INSERT ON entries BEGIN "
    "INSERT INTO entries_fts(rowid, content_md) VALUES (new.id, new.content_md); "
    "END"
)
# The closed vocabulary is enforced by the database too, not only by the endpoint's
# validator: this table is new in Block T, so the CHECK costs nothing today and stops
# a future writer (a migration script, a later epic, an admin one-liner) from storing
# a label the readers cannot filter on. The IN list is GENERATED from `ITEM_TYPES` so
# the two can never drift; the values are that frozen literal tuple, never input.
_ITEM_TYPES_SQL_LIST = ", ".join(f"'{item_type}'" for item_type in ITEM_TYPES)
_ITEM_TYPES_DDL = (
    "CREATE TABLE IF NOT EXISTS entry_item_types ("
    "entry_id INTEGER NOT NULL REFERENCES entries(id), "
    f"item_type TEXT NOT NULL CHECK (item_type IN ({_ITEM_TYPES_SQL_LIST})), "
    "UNIQUE(entry_id, item_type))"
)
# Covering index for the read side (`?type=` filters by label, then joins by entry).
_ITEM_TYPES_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_entry_item_types_type "
    "ON entry_item_types(item_type, entry_id)"
)
# The documented way to (re)populate an external-content table from its content
# table. It REPLACES the index rather than appending to it, which is exactly what
# makes a rerun harmless.
_BACKFILL_SQL = "INSERT INTO entries_fts(entries_fts) VALUES('rebuild')"

# Every object this module owns. `ensure_search_schema` treats "all present AND the
# index agrees with `entries`" as the definition of a migrated database.
_OBJECT_NAMES = ("entries_fts", "entries_ai_fts", "entry_item_types",
                 "idx_entry_item_types_type")

# A throwaway temp-schema table used only to ask "can this build create an fts5
# table?". `temp.` keeps it out of the database file and off the main-database lock.
_FTS5_PROBE = "temp.__loom_fts5_probe"


def ensure_search_schema(conn: sqlite3.Connection) -> None:
    """Idempotent: entries_fts (external content) + insert trigger + backfill +
    entry_item_types. Raises RuntimeError naming ENABLE_FTS5 if FTS5 is absent.

    Three properties, in order, each of which had to be earned:

    1. The FTS5 guard is checked FIRST and independently of what the database already
       contains. `CREATE VIRTUAL TABLE IF NOT EXISTS` short-circuits before resolving
       the module, so probing with it would have skipped the check on exactly the
       database this guard exists for — an existing one, opened by a Python rebuilt
       without ENABLE_FTS5.
    2. A migrated database costs one read (`_schema_is_complete`) and takes no write
       lock; the migration path is the exception, not the every-startup default.
    3. The migration is ONE transaction. Python's sqlite3 opens an implicit
       transaction for DML only, so a bare DDL block auto-commits statement by
       statement: the insert trigger would become permanent while the backfill was
       still revocable, and an interrupted migration (lock timeout, SIGKILL, full
       disk, `Restart=always`) would leave a database that indexes new entries and
       has silently lost every old one. BEGIN IMMEDIATE makes it all-or-nothing.
    """
    _require_fts5(conn)
    if _schema_is_complete(conn):
        return
    _apply_schema(conn)


def _require_fts5(conn: sqlite3.Connection) -> None:
    """Prove the fts5 module is loadable RIGHT NOW, whatever the database holds."""
    try:
        conn.execute(f"CREATE VIRTUAL TABLE {_FTS5_PROBE} USING fts5(x)")
    except sqlite3.OperationalError as exc:
        if "no such module" in str(exc).lower():
            raise RuntimeError(
                "this SQLite build has no FTS5 module: the logbook search index "
                "(entries_fts) cannot be used. Rebuild/reinstall Python's sqlite3 "
                "with the ENABLE_FTS5 compile option — the server refuses to run with "
                "a silently unsearchable logbook."
            ) from exc
        raise
    # Only reached when the CREATE succeeded, so there is always something to drop
    # and a failure here is a real fault worth surfacing rather than swallowing.
    conn.execute(f"DROP TABLE {_FTS5_PROBE}")


def _schema_is_complete(conn: sqlite3.Connection) -> bool:
    """Are all the objects present AND the index in step with `entries`?"""
    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    if not set(_OBJECT_NAMES) <= present:
        return False
    return not _needs_backfill(conn)


def _apply_schema(conn: sqlite3.Connection) -> None:
    """Create what is missing and rebuild the index — atomically (see property 3)."""
    # IMMEDIATE, not DEFERRED: the write lock is taken up front, so a competing writer
    # is settled by busy_timeout at the start instead of by a rollback at the end.
    outermost = not conn.in_transaction
    if outermost:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(_FTS_DDL)
        conn.execute(_TRIGGER_DDL)
        conn.execute(_ITEM_TYPES_DDL)
        conn.execute(_ITEM_TYPES_INDEX_DDL)
        if _needs_backfill(conn):
            conn.execute(_BACKFILL_SQL)
    except BaseException:
        if outermost:
            conn.rollback()
        raise
    if outermost:
        conn.commit()


def _needs_backfill(conn: sqlite3.Connection) -> bool:
    """Does the index hold a document for every entry? (a total check, not a probe)

    ``SELECT count(*) FROM entries_fts`` cannot answer this: on an external-content
    table a full scan reads the *content* table, so it would report the number of
    entries whether or not a single one is indexed. ``entries_fts_docsize`` is the
    index's own per-document bookkeeping (an fts5 shadow table, created because this
    table keeps the default ``columnsize=1``) and is the one place that knows.

    The comparison is EQUALITY, and the difference from "is the index empty?" is the
    difference between healing and hiding. Consider a migration whose backfill died
    after the trigger existed: one later POST is enough to make the index non-empty,
    and an is-it-empty test would then declare a database holding 1 of 5001 documents
    healthy — forever, across every restart, with searches quietly missing five
    thousand entries and no error anywhere. Entries are append-only (D7), so any
    inequality in either direction is drift and wants the same idempotent rebuild.
    """
    indexed = conn.execute("SELECT count(*) FROM entries_fts_docsize").fetchone()[0]
    entries = conn.execute("SELECT count(*) FROM entries").fetchone()[0]
    return indexed != entries


# --------------------------------------------------------------------------- #
# Search                                                                       #
# --------------------------------------------------------------------------- #
# The seven frozen response fields (phase1-api-contracts) + the two additive
# ranking fields. `rank` is bm25: negative, ascending = best first.
_SELECT = (
    "SELECT e.id, u.name AS author, e.session_id, e.created_at, e.covered_from, "
    "e.covered_to, e.content_md, "
    "snippet(entries_fts, 0, ?, ?, ?, ?) AS snippet, "
    "bm25(entries_fts) AS rank "
    "FROM entries_fts "
    "JOIN entries e ON e.id = entries_fts.rowid "
    "LEFT JOIN users u ON u.id = e.user_id "
    "WHERE entries_fts MATCH ? AND e.project_id = ?"
)
# The label filter, in one place: the ranked branch here and the unranked listing in
# app.py must agree on what `?type=` MEANS, so they share the fragment instead of
# each carrying a copy. It appends to a WHERE whose entries table is aliased `e`, and
# binds one parameter (the label).
ITEM_TYPE_FILTER_SQL = (
    " AND EXISTS (SELECT 1 FROM entry_item_types t "
    "WHERE t.entry_id = e.id AND t.item_type = ?)"
)
_SINCE_CLAUSE = " AND e.created_at > ?"
_ORDER = " ORDER BY rank ASC, e.created_at DESC, e.id DESC LIMIT ?"


def _is_expression(q: str) -> bool:
    """Did the caller write an FTS5 expression (operators/phrases), or just words?"""
    if any(ch in _EXPRESSION_CHARS for ch in q):
        return True
    return any(token in _EXPRESSION_OPERATORS for token in q.split())


def _fts_expression(q: str) -> str:
    """The MATCH value for a user query — still a bound parameter, never SQL.

    FTS5's query language is not "a bag of words": several characters that occur in
    ordinary prose are grammar. ``fresh post-ensure`` is the case the goal pins down —
    FTS5 reads ``-ensure`` as an inverted column filter and answers "no such column:
    ensure" — and ``foo, bar`` or ``note: fix`` are the same class of trap. Rejecting
    those with a 422 would be answering a well-formed question with an error message.

    So a query that shows no sign of being an expression is mapped to one: every
    whitespace-separated word becomes a quoted string, and FTS5's implicit AND joins
    them. Quoting is what makes the mapping total — inside a string, the grammar
    characters are just text.

    A query that DOES look like an expression (a quote, ``*``, parentheses, or one of
    AND/OR/NOT/NEAR) is passed through verbatim, so the operators keep working and a
    malformed one is still a real syntax error (→ :class:`InvalidQuery` → 422). That
    is the frozen behavioural split: ``"unbalanced`` must fail, ``post-ensure`` must
    not.

    Note the quoting cannot be escaped from: the term branch is only taken when the
    query contains no ``"`` at all, so a word cannot close its own string and append
    an operator. The doubling below is therefore already unreachable; it stays so that
    the invariant holds by itself, and not only because `_is_expression` says so.
    """
    q = q.strip()
    if len(q) > MAX_QUERY_LEN:
        raise InvalidQuery("the search query is too long")
    if _is_expression(q):
        return q
    words = q.split()
    if not words:
        raise InvalidQuery("the search query has no terms")
    return " ".join('"' + word.replace('"', '""') + '"' for word in words)


def search_entries(
    conn: sqlite3.Connection,
    project_id: int,
    q: str,
    *,
    item_type: str | None = None,
    since: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[sqlite3.Row], bool]:
    """BM25-ranked hits with snippet/rank. Raises InvalidQuery on FTS5 syntax errors.

    ``q`` is a user query, mapped to a MATCH expression by `_fts_expression`; it is
    always bound, never concatenated. Rows carry the seven frozen entry fields plus
    ``snippet`` and ``rank`` (bm25: negative, ascending = best first). ``item_type``
    must belong to `ITEM_TYPES`; ``since`` and ``limit`` behave as in the unranked
    listing, and ``has_more`` is decided by reading one row past ``limit``.
    """
    if item_type is not None and item_type not in ITEM_TYPES:
        # Defence in depth: the endpoint validates first (422 invalid_type), but the
        # substrate is a frozen interface with other callers, and a label that is not
        # in the closed vocabulary can only be a bug or an injection attempt.
        raise ValueError("item_type must be one of ITEM_TYPES")
    limit = max(1, min(int(limit), MAX_LIMIT))
    match_expression = _fts_expression(q)  # raises InvalidQuery before any DB work

    sql = _SELECT
    params: list = [_SNIPPET_OPEN, _SNIPPET_CLOSE, _SNIPPET_ELLIPSIS, _SNIPPET_TOKENS,
                    match_expression, project_id]
    if item_type is not None:
        sql += ITEM_TYPE_FILTER_SQL
        params.append(item_type)
    if since:
        sql += _SINCE_CLAUSE
        params.append(since)
    sql += _ORDER
    # One row over the limit: the cheapest honest `has_more` (same trick as the
    # unranked listing).
    params.append(limit + 1)

    # A cursor-level row_factory keeps the caller's connection untouched: callers
    # that opened their own plain connection still get mapping-style rows.
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    try:
        rows = cur.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        if _is_infrastructure_error(exc):
            raise
        # Client error: the message quotes the query back, so it stays server-side.
        raise InvalidQuery("the search query is not a valid FTS5 expression") from exc
    finally:
        cur.close()

    has_more = len(rows) > limit
    return rows[:limit], has_more


def _is_infrastructure_error(exc: sqlite3.OperationalError) -> bool:
    """Is this fault the server's rather than the query's? (by result code, never text)

    SQLITE_ERROR also covers "no such table: entries_fts", i.e. a database that never
    ran the migration, which this therefore reports as a client error. That state is
    unreachable in the server: `db.init_db` runs `ensure_search_schema` at startup,
    which proves the fts5 module is loadable and that every object exists before the
    first request is served.
    """
    return exc.sqlite_errorcode not in _CLIENT_ERROR_CODES
