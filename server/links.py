"""Block T (RT.3.1): entry_links schema + link operations (E15).

Additive-only per the frozen ``phase1-db-schema`` seam: ``ensure_links_schema``
is idempotent DDL called from ``db.init_db``. ``entries`` rows are NEVER mutated
(D7): supersession/duplication is expressed exclusively through new link rows, and
an entry's ``status`` is DERIVED at read time (t-entry-links, the Q-3.5
resolution) — never a stored column.

Frozen interface (t-entry-links): see ``docs/IMPORTED-IMPROVEMENTS-SPEC.md`` RT.3.1.

Reader contract (IMPORTANT, and the one thing a consumer can get wrong):
:func:`links_for_entries` is a SPARSE map — it returns a row only for an entry that
participates in at least one link, because that is what the one query behind it
found. It is NOT the response shape. ``app._with_links`` fills the gap: every entry
of an entry GET response carries ``status``, ``links_in`` and ``links_out``
unconditionally (``"current"`` + two empty lists when there are no links), exactly
as the frozen ``t-entry-links`` contract says ("entry GET responses gain …").

That is deliberately NOT the ``snippet``/``rank`` rule. Those two are *query*
metadata — they exist only for a ranked hit and mean nothing on a plain listing —
whereas ``status`` is the entity's own state, and every entry has one. Serving it
conditionally makes the natural consumer line ``entry.get("status") == "current"``
evaluate to ``False`` for every un-curated entry, with no exception and no log line
to notice it by; that failure is silent and it is the wrong way round.
"""
from __future__ import annotations

import sqlite3

LINK_KINDS = ("supersedes", "duplicates")

# Derived statuses (never stored). Precedence is fixed by the contract: an incoming
# `supersedes` wins over an incoming `duplicates`, because "this was replaced" is the
# stronger statement and the one `loom log` collapses on.
STATUS_SUPERSEDED = "superseded"
STATUS_DUPLICATE = "duplicate"
STATUS_CURRENT = "current"

# How many entry ids go into one `IN (...)` list. SQLITE_MAX_VARIABLE_NUMBER is 999 on
# older builds and the read below binds the id list TWICE (once per direction), so a
# single page of `MAX_LIMIT` (500) entries would be 1000 parameters — over the limit on
# exactly the builds least likely to be noticed in testing. Chunking is cheaper than
# depending on the build.
_ID_CHUNK = 300

# The IN list is GENERATED from `LINK_KINDS` (the same discipline `entry_item_types`
# uses for its vocabulary) so the CHECK constraint and the Python tuple cannot drift.
# The values are that frozen literal tuple, never input.
_KINDS_SQL_LIST = ", ".join(f"'{kind}'" for kind in LINK_KINDS)
_LINKS_DDL = (
    "CREATE TABLE IF NOT EXISTS entry_links ("
    "id INTEGER PRIMARY KEY, "
    "from_entry INTEGER NOT NULL REFERENCES entries(id), "
    "to_entry INTEGER NOT NULL REFERENCES entries(id), "
    f"kind TEXT NOT NULL CHECK(kind IN ({_KINDS_SQL_LIST})), "
    "rationale TEXT, "
    "created_at TEXT NOT NULL, "
    "UNIQUE(from_entry, to_entry, kind))"
)
# The UNIQUE constraint already indexes (from_entry, to_entry, kind), which serves every
# `links_out` lookup; the reverse direction — "who supersedes THIS entry?", the query
# behind the derived status on every listing — has no index without this one.
_LINKS_TO_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_entry_links_to ON entry_links(to_entry)"
)

# No `ON DELETE CASCADE`, deliberately: it matches the convention of every other table
# in this schema (`entry_item_types`, `project_paths`) and nothing deletes an entry
# (D7 — entries are append-only). Wave i recorded the whole-schema gap as follow-up F3;
# inventing a different rule for one table would only make that harder to close.
_OBJECT_NAMES = ("entry_links", "idx_entry_links_to")


def ensure_links_schema(conn: sqlite3.Connection) -> None:
    """Idempotent: entry_links table (+ UNIQUE(from_entry,to_entry,kind)).

    The presence check comes first so a migrated database costs one read and takes no
    write lock — `db.init_db` runs on every server start AND on every `server.admin`
    command, and `CREATE ... IF NOT EXISTS` still opens a write transaction.

    Unlike `search.ensure_search_schema` this needs no backfill: both objects are pure
    DDL with no data to keep in step, so a migration interrupted between them leaves a
    state the next call finishes. There is nothing here that can become "permanently
    half-applied".

    It does NOT commit a transaction it did not open (the `outermost` guard, copied
    from `search._apply_schema`). Committing unconditionally silently commits whatever
    the CALLER had in flight: with an uncommitted INSERT open, the caller's later
    `rollback()` becomes a no-op. `db.init_db` is safe today only because
    `_apply_schema` happens to run first and commit — that is an ordering accident,
    and this is the guard that stops it being load-bearing.

    Finally the UNIQUE triple is VERIFIED, not assumed. The presence check is
    name-only, so a database carrying an `entry_links` table created by anything other
    than this DDL (an older build, a hand-run migration) would satisfy it while letting
    the same triple be inserted twice — and every 409-tolerant POST in `loom curate`
    depends on that constraint to make a rerun idempotent. Serving reads over such a
    table is worse than refusing to start, which is the same call `search` makes when
    FTS5 is missing.
    """
    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    if not set(_OBJECT_NAMES) <= present:
        outermost = not conn.in_transaction
        if outermost:
            conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(_LINKS_DDL)
            conn.execute(_LINKS_TO_INDEX_DDL)
        except BaseException:
            if outermost:
                conn.rollback()
            raise
        if outermost:
            conn.commit()
    _verify_unique_triple(conn)


def _verify_unique_triple(conn: sqlite3.Connection) -> None:
    """Raise unless some UNIQUE index really covers (from_entry, to_entry, kind)."""
    for name, is_unique in conn.execute(
        'SELECT name, "unique" FROM pragma_index_list(?)', ("entry_links",)
    ).fetchall():
        if not is_unique:
            continue
        columns = [row[0] for row in conn.execute(
            "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (name,)
        ).fetchall()]
        if columns == ["from_entry", "to_entry", "kind"]:
            return
    raise RuntimeError(
        "the entry_links table in this database has no UNIQUE(from_entry, to_entry, "
        "kind) constraint: POST /api/links could not answer 409 for a link that "
        "already exists, and every `loom curate` rerun would post it again. The "
        "server refuses to run against a schema it cannot make idempotent."
    )


def _link_rows(conn: sqlite3.Connection, entry_ids: list[int]) -> list[sqlite3.Row]:
    """Every link touching one of ``entry_ids``, in a stable order, chunked."""
    rows: list[sqlite3.Row] = []
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    try:
        for start in range(0, len(entry_ids), _ID_CHUNK):
            chunk = entry_ids[start:start + _ID_CHUNK]
            marks = ", ".join("?" for _ in chunk)
            rows.extend(cur.execute(
                "SELECT from_entry, to_entry, kind, rationale, created_at "
                f"FROM entry_links WHERE from_entry IN ({marks}) OR to_entry IN ({marks}) "
                "ORDER BY id",
                (*chunk, *chunk),
            ).fetchall())
    finally:
        cur.close()
    return rows


def derive_status(links_in: list[dict]) -> str:
    """The Q-3.5 derivation, in ONE place: incoming links → the entry's status.

    Exported because `loom log`'s collapsed rendering and any later reader must agree
    with what the endpoint serves; a second copy of this three-line rule is how a
    stored-status column grows back by accident.
    """
    kinds = {link["kind"] for link in links_in}
    if "supersedes" in kinds:
        return STATUS_SUPERSEDED
    if "duplicates" in kinds:
        return STATUS_DUPLICATE
    return STATUS_CURRENT


def links_for_entries(
    conn: sqlite3.Connection, entry_ids: list[int]
) -> dict[int, dict]:
    """{entry_id: {"links_in":[...], "links_out":[...], "status": "..."}}.

    Only entries that participate in at least one link appear in the result (see the
    module docstring's reader contract). ``links_in`` carries the SOURCE of each
    incoming link (``from_entry``) and ``links_out`` the TARGET of each outgoing one
    (``to_entry``), matching the frozen response shape.
    """
    wanted = {int(entry_id) for entry_id in entry_ids}
    if not wanted:
        return {}
    result: dict[int, dict] = {}

    def _slot(entry_id: int) -> dict:
        return result.setdefault(
            entry_id, {"links_in": [], "links_out": [], "status": STATUS_CURRENT}
        )

    for row in _link_rows(conn, sorted(wanted)):
        common = {"kind": row["kind"], "rationale": row["rationale"],
                  "created_at": row["created_at"]}
        if row["to_entry"] in wanted:
            _slot(row["to_entry"])["links_in"].append(
                {"from_entry": row["from_entry"], **common}
            )
        if row["from_entry"] in wanted:
            _slot(row["from_entry"])["links_out"].append(
                {"to_entry": row["to_entry"], **common}
            )
    for entry_id, payload in result.items():
        payload["status"] = derive_status(payload["links_in"])
    return result
