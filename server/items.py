"""D13 (plan of 2026-09-23, slice C): addressable items — schema, validation, reads.

An ITEM is one top-level bullet of an entry's ``## Decisions`` section (with its
continuation lines), captured under a labelled prompt version. Its public id is
``<entry_id>.<ordinal>``. The entry stays what it always was — the immutable session
record (D7) — and becomes the item's provenance.

Who produces items: the CLIENT. The processor owns the label grammar
(``t-label-grammar``: "the PROCESSOR parses") and this image ships ``server/`` only,
so the server never parses markdown. It checks each item instead, with one rule that
needs no grammar: an item's ``text_md`` must be a SUBSTRING of the entry's
``content_md``. That keeps every stored byte inside text the client already
secret-scanned, and it guards quarantine too — after a hand redaction of
``content_md``, a stale item still holding the secret no longer matches and is refused.

Additive only, like every Block-T module: ``ensure_items_schema`` is idempotent DDL
called from ``db.init_db``; ``schema.sql``, ``entries``, ``entry_item_types`` and
``entry_links`` are untouched, and every object here is a NEW table, so no CHECK is
ever altered. ``item_links`` is written by ``POST /api/item-links`` (slice E, the
curator) and revoked by a person; an item with no active incoming link is ``current``.

Search reuses the entries index's query mapping and error classification
(``search._fts_expression`` / ``search._is_infrastructure_error``): one definition of
what a user query means and of which failures are the caller's.
"""
from __future__ import annotations

import re
import sqlite3

from server import links, search

# More than this in one entry is not a session summary. The bound is what keeps a
# single POST from writing an unbounded number of rows and index documents.
MAX_ITEMS = 50
# How many entry ids one `/items` read may name: a page of `/entries` at its clamp.
MAX_ENTRY_IDS = search.MAX_LIMIT
# `<entry id>.<ordinal>`, both positive, no sign, no padding, nothing around it.
ITEM_ID_RE = re.compile(r"[1-9][0-9]{0,17}\.[1-9][0-9]{0,5}")
# Chunk for `IN (...)` lists that bind the id list TWICE (see links._ID_CHUNK).
_ID_CHUNK = 300

_ITEM_TYPES_SQL_LIST = ", ".join(f"'{t}'" for t in search.ITEM_TYPES)
_KINDS_SQL_LIST = ", ".join(f"'{k}'" for k in links.LINK_KINDS)

_ITEMS_DDL = (
    "CREATE TABLE IF NOT EXISTS entry_items ("
    "id INTEGER PRIMARY KEY, "
    "entry_id INTEGER NOT NULL REFERENCES entries(id), "
    "ordinal INTEGER NOT NULL CHECK (ordinal >= 1), "
    f"item_type TEXT NOT NULL CHECK (item_type IN ({_ITEM_TYPES_SQL_LIST})), "
    # 0 = the processor's fallback typed it (the bullet missed the label grammar).
    "labelled INTEGER NOT NULL CHECK (labelled IN (0, 1)), "
    "text_md TEXT NOT NULL, "
    "UNIQUE (entry_id, ordinal))"
)
# External content over `entry_items.text_md`, synchronised by an AFTER INSERT trigger
# on `entry_items` — never on `entries`. Items are append-only like their entries. Same
# tokenizer as the entries index (`search.TOKENIZER`): one query must mean one thing.
_FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS entry_items_fts USING fts5("
    f"text_md, content='entry_items', content_rowid='id', tokenize='{search.TOKENIZER}')"
)
_TRIGGER_DDL = (
    "CREATE TRIGGER IF NOT EXISTS entry_items_ai_fts AFTER INSERT ON entry_items BEGIN "
    "INSERT INTO entry_items_fts(rowid, text_md) VALUES (new.id, new.text_md); "
    "END"
)
# Written by `create_link`. Unlike `entry_links`, an item link names its author and can be
# revoked with a reason; a revoked link stays as history and stops counting.
_LINKS_DDL = (
    "CREATE TABLE IF NOT EXISTS item_links ("
    "id INTEGER PRIMARY KEY, "
    "from_item INTEGER NOT NULL REFERENCES entry_items(id), "
    "to_item INTEGER NOT NULL REFERENCES entry_items(id), "
    f"kind TEXT NOT NULL CHECK (kind IN ({_KINDS_SQL_LIST})), "
    "rationale TEXT, "
    "created_by INTEGER NOT NULL REFERENCES users(id), "
    "created_at TEXT NOT NULL, "
    "revoked_at TEXT, "
    "revoked_by INTEGER REFERENCES users(id), "
    "revoke_reason TEXT)"
)
_LINKS_ACTIVE_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_item_links_active "
    "ON item_links(from_item, to_item, kind) WHERE revoked_at IS NULL"
)
_LINKS_TO_DDL = "CREATE INDEX IF NOT EXISTS idx_item_links_to ON item_links(to_item)"
_BACKFILL_SQL = "INSERT INTO entry_items_fts(entry_items_fts) VALUES('rebuild')"
_OBJECT_NAMES = ("entry_items", "entry_items_fts", "entry_items_ai_fts", "item_links",
                 "idx_item_links_active", "idx_item_links_to")


class InvalidItems(ValueError):
    """A POSTed ``items`` list the server refuses (→ 422 ``invalid_items``).

    The message names the rule, never the offending value: it is echoed to the caller,
    and an item that failed the substring check may be exactly the text a redaction
    removed.
    """


# --------------------------------------------------------------------------- #
# Schema (idempotent, additive; the search.ensure_search_schema discipline)    #
# --------------------------------------------------------------------------- #
def ensure_items_schema(conn: sqlite3.Connection) -> None:
    """Create what is missing and re-index if the index disagrees — atomically.

    Called after ``search.ensure_search_schema``, which has already proved the fts5
    module is loadable. A migrated database costs one read and takes no write lock.
    """
    if _schema_is_complete(conn):
        return
    outermost = not conn.in_transaction
    if outermost:
        conn.execute("BEGIN IMMEDIATE")
    try:
        search.drop_outdated_fts(conn, "entry_items_fts")
        for ddl in (_ITEMS_DDL, _FTS_DDL, _TRIGGER_DDL, _LINKS_DDL, _LINKS_ACTIVE_DDL,
                    _LINKS_TO_DDL):
            conn.execute(ddl)
        if _needs_backfill(conn):
            conn.execute(_BACKFILL_SQL)
    except BaseException:
        if outermost:
            conn.rollback()
        raise
    if outermost:
        conn.commit()


def _schema_is_complete(conn: sqlite3.Connection) -> bool:
    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    return (set(_OBJECT_NAMES) <= present and search.fts_is_current(conn, "entry_items_fts")
            and not _needs_backfill(conn))


def _needs_backfill(conn: sqlite3.Connection) -> bool:
    """Does the index hold one document per item? (see search._needs_backfill)"""
    indexed = conn.execute("SELECT count(*) FROM entry_items_fts_docsize").fetchone()[0]
    stored = conn.execute("SELECT count(*) FROM entry_items").fetchone()[0]
    return indexed != stored


# --------------------------------------------------------------------------- #
# Write side                                                                   #
# --------------------------------------------------------------------------- #
def validate(content_md: str, items: list[dict],
             item_types: list[str] | None) -> list[str]:
    """Check a POSTed ``items`` list; return the ``item_types`` to store.

    ``items`` are ``{"item_type", "labelled", "text_md"}`` dicts in document order.
    When ``item_types`` is sent too it must equal the sorted set of the items' types
    (two statements about one entry may not disagree); when absent it is derived.
    """
    if len(items) > MAX_ITEMS:
        raise InvalidItems(f"an entry carries at most {MAX_ITEMS} items")
    for item in items:
        if item["item_type"] not in search.ITEM_TYPES:
            raise InvalidItems("unknown item_type; allowed: " + ", ".join(search.ITEM_TYPES))
        if not item["text_md"].strip():
            raise InvalidItems("every item needs a non-empty text_md")
        if item["text_md"] not in content_md:
            raise InvalidItems("every item's text_md must be part of the entry's content_md")
    derived = sorted({item["item_type"] for item in items})
    if item_types is not None and sorted(set(item_types)) != derived:
        raise InvalidItems("item_types must equal the set of the items' types")
    return derived


def insert(conn: sqlite3.Connection, entry_id: int, items: list[dict]) -> None:
    """Store ``items`` for a just-inserted entry, ordinals 1..n in payload order.

    Runs inside the caller's transaction: an entry is stored with all of its items or
    not at all.
    """
    conn.executemany(
        "INSERT INTO entry_items (entry_id, ordinal, item_type, labelled, text_md) "
        "VALUES (?, ?, ?, ?, ?)",
        [(entry_id, ordinal, item["item_type"], int(bool(item["labelled"])),
          item["text_md"]) for ordinal, item in enumerate(items, 1)],
    )


# --------------------------------------------------------------------------- #
# Item links (slice E): written by the curator, revoked by a person            #
# --------------------------------------------------------------------------- #
# One line, like a commit subject: why a person took a link back.
MAX_REVOKE_REASON_LEN = 500


class LinkRefused(Exception):
    """An item link or revocation the server refuses; the route answers ``status``.

    ``detail`` names ids the caller sent (they are validated ``<entry>.<n>`` shapes),
    never free text from the body.
    """

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def create_link(conn: sqlite3.Connection, *, from_id: str, to_id: str, kind: str,
                rationale: str | None, user_id: int, created_at: str) -> int:
    """Store ``from_id <kind> to_id``; the new link's id, or :class:`LinkRefused`.

    Both items are resolved here: 404 when either is unknown (an un-itemized entry has
    no items). 422 unless both are in the same project, and unless ``from`` belongs to
    a strictly NEWER entry than ``to`` — ids are monotonic and entries immutable (D7), so
    "newer" is a higher entry id, and requiring it rules out a link inside one entry and
    every cycle (the argument of ``POST /api/links``). 409 when an ACTIVE link already
    joins the pair, with this kind (``duplicate_link``) or the other one
    (``contradictory_link``); a revoked link is history and blocks nothing.
    """
    found_from, found_to = find_item(conn, from_id), find_item(conn, to_id)
    for public, found in ((from_id, found_from), (to_id, found_to)):
        if found is None:
            raise LinkRefused(404, "item_not_found",
                              f"No item {public}: the entry does not exist or has no items")
    if found_from[1] != found_to[1]:
        raise LinkRefused(422, "validation_error",
                          "A link must join two items of the SAME project")
    entry_from, entry_to = int(from_id.split(".")[0]), int(to_id.split(".")[0])
    if entry_from == entry_to:
        raise LinkRefused(422, "validation_error",
                          "A link must join items of two DIFFERENT entries")
    if entry_from < entry_to:
        raise LinkRefused(422, "validation_error",
                          "'from' must be an item of the NEWER entry (a higher entry id "
                          "than 'to')")
    # IMMEDIATE, so the check and the insert are one step: two machines posting the two
    # kinds at once would otherwise both read "no active link" and both insert.
    conn.execute("BEGIN IMMEDIATE")
    try:
        active = conn.execute(
            "SELECT kind FROM item_links WHERE from_item = ? AND to_item = ? "
            "AND revoked_at IS NULL", (found_from[0], found_to[0])).fetchone()
        if active is not None:
            conn.rollback()
            if active[0] == kind:
                raise LinkRefused(409, "duplicate_link", "This link already exists")
            raise LinkRefused(409, "contradictory_link",
                              "These two items are already linked with a different kind; "
                              "one pair of items states one relation")
        cur = conn.execute(
            "INSERT INTO item_links (from_item, to_item, kind, rationale, created_by, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (found_from[0], found_to[0], kind, rationale, user_id, created_at))
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        if getattr(exc, "sqlite_errorname", "") != "SQLITE_CONSTRAINT_UNIQUE":
            # A CHECK, FK or trigger failure is a fault, never "already linked": the
            # curator counts a 409 as posted and moves its watermark on (rule F5).
            raise
        raise LinkRefused(409, "duplicate_link", "This link already exists") from exc
    return cur.lastrowid


def revoke_link(conn: sqlite3.Connection, link_id: int, *, user_id: int, reason: str,
                revoked_at: str) -> dict:
    """Mark a link revoked by ``user_id`` with ``reason``; the revocation as served.

    The row stays: a revoked link is the record of a correction (#12), and it stops
    counting towards any status. 404 ``link_not_found``; 409 ``already_revoked``.
    """
    from server import db  # function-level: server.db imports this module

    if not db.is_row_id(link_id):
        raise LinkRefused(404, "link_not_found", f"No item link {link_id}")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT revoked_at FROM item_links WHERE id = ?",
                           (link_id,)).fetchone()
        if row is None:
            raise LinkRefused(404, "link_not_found", f"No item link {link_id}")
        if row[0] is not None:
            raise LinkRefused(409, "already_revoked",
                              f"Item link {link_id} is already revoked")
        conn.execute(
            "UPDATE item_links SET revoked_at = ?, revoked_by = ?, revoke_reason = ? "
            "WHERE id = ?", (revoked_at, user_id, reason, link_id))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    name = conn.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()
    return {"id": link_id, "revoked_at": revoked_at,
            "revoked_by": name[0] if name else None, "revoke_reason": reason}


# --------------------------------------------------------------------------- #
# Read side                                                                    #
# --------------------------------------------------------------------------- #
def public_id(entry_id: int, ordinal: int) -> str:
    """``<entry_id>.<ordinal>`` — the id a reader cites (``17.2``)."""
    return f"{entry_id}.{ordinal}"


def find_item(conn: sqlite3.Connection, public_id: str) -> tuple[int, int] | None:
    """``(item row id, its project id)`` for ``"<entry>.<ordinal>"``, or ``None``."""
    entry_id, ordinal = (int(part) for part in public_id.split("."))
    row = conn.execute(
        "SELECT i.id, e.project_id FROM entry_items i JOIN entries e ON e.id = i.entry_id "
        "WHERE i.entry_id = ? AND i.ordinal = ?", (entry_id, ordinal)).fetchone()
    return (row[0], row[1]) if row is not None else None


def items_for_entries(
    conn: sqlite3.Connection,
    project_id: int,
    entry_ids: list[int],
    *,
    item_type: str | None = None,
    q: str | None = None,
    any_term: bool = False,
) -> list[dict]:
    """Every item of ``entry_ids`` that belongs to ``project_id``, with ``match``.

    ALL items come back, not only the matching ones: ``match`` is true when the item
    satisfies every filter given (``item_type`` = its label, ``q`` = an FTS match, which
    also yields a ``snippet``). That is what lets a reader tell an itemized entry with
    nothing matching apart from an entry that has no items at all.

    ``any_term`` (RT.1.4, the route's ``fallback=any_term``): an entry none of whose items
    passes every filter has its items of the requested type matched against ANY of the
    words of ``q``, and those carry ``match_mode``. Off by default — the web UI calls
    this over a whole project, where a loose match would be shown as an exact one.

    Raises :class:`search.InvalidQuery` for a malformed ``q``.
    """
    rows = _item_rows(conn, project_id, entry_ids)
    if not rows:
        return []
    snippets = loose = None
    if q is not None:
        # Both readings are mapped before any DB work, so a malformed q is a 422 either way.
        expression = search._fts_expression(q)
        loose_expression = search.any_term_expression(q) if any_term else None
        snippets = _search(conn, [row["id"] for row in rows], expression)
        if loose_expression is not None:
            unanswered = _entries_without_match(rows, snippets, item_type)
            # Only the items that could match: one of another type would gain a snippet
            # (and the reader a reason to wonder) without ever matching.
            loose = _search(conn, [row["id"] for row in rows
                                   if row["entry_id"] in unanswered
                                   and (item_type is None or row["item_type"] == item_type)],
                            loose_expression)
            snippets = {**snippets, **loose}
    by_item = _links_for_items(conn, [row["id"] for row in rows])
    out = []
    for row in rows:
        match = item_type is None or row["item_type"] == item_type
        if snippets is not None:
            match = match and row["id"] in snippets
        item = {
            "entry_id": row["entry_id"],
            "ordinal": row["ordinal"],
            "item_type": row["item_type"],
            "labelled": bool(row["labelled"]),
            "text_md": row["text_md"],
            "match": match,
        }
        if snippets is not None and row["id"] in snippets:
            item["snippet"] = snippets[row["id"]]
        if loose and row["id"] in loose:
            item["match_mode"] = search.MATCH_ANY_TERM
        item.update(by_item.get(row["id"]) or {
            "status": links.STATUS_CURRENT, "links_in": [], "links_out": []})
        out.append(item)
    return out


def _item_rows(conn: sqlite3.Connection, project_id: int,
               entry_ids: list[int]) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    try:
        for start in range(0, len(entry_ids), _ID_CHUNK):
            chunk = entry_ids[start:start + _ID_CHUNK]
            marks = ", ".join("?" for _ in chunk)
            rows.extend(cur.execute(
                "SELECT i.id, i.entry_id, i.ordinal, i.item_type, i.labelled, i.text_md "
                "FROM entry_items i JOIN entries e ON e.id = i.entry_id "
                f"WHERE e.project_id = ? AND i.entry_id IN ({marks})",
                (project_id, *chunk),
            ).fetchall())
    finally:
        cur.close()
    return sorted(rows, key=lambda row: (row["entry_id"], row["ordinal"]))


def _entries_without_match(rows: list[sqlite3.Row], snippets: dict[int, str],
                           item_type: str | None) -> set[int]:
    """The entries among ``rows`` none of whose items passes every filter."""
    answered = {row["entry_id"] for row in rows if row["id"] in snippets
                and (item_type is None or row["item_type"] == item_type)}
    return {row["entry_id"] for row in rows} - answered


def _search(conn: sqlite3.Connection, item_ids: list[int],
            expression: str) -> dict[int, str]:
    """``{item id: snippet}`` for the items among ``item_ids`` that match ``expression``.

    ``expression`` is a MATCH value the entries search's mapping produced from a user
    query, and reaches SQLite only as the bound right-hand side of MATCH; a malformed
    one is the caller's error.
    """
    found: dict[int, str] = {}
    cur = conn.cursor()
    try:
        for start in range(0, len(item_ids), _ID_CHUNK):
            chunk = item_ids[start:start + _ID_CHUNK]
            marks = ", ".join("?" for _ in chunk)
            try:
                rows = cur.execute(
                    "SELECT entry_items_fts.rowid, "
                    "snippet(entry_items_fts, 0, ?, ?, ?, ?) "
                    "FROM entry_items_fts "
                    f"WHERE entry_items_fts MATCH ? AND entry_items_fts.rowid IN ({marks})",
                    (search._SNIPPET_OPEN, search._SNIPPET_CLOSE, search._SNIPPET_ELLIPSIS,
                     search._SNIPPET_TOKENS, expression, *chunk),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if search._is_infrastructure_error(exc):
                    raise
                raise search.InvalidQuery(
                    "the search query is not a valid FTS5 expression") from exc
            found.update({row[0]: row[1] for row in rows})
    finally:
        cur.close()
    return found


def _links_for_items(conn: sqlite3.Connection, item_ids: list[int]) -> dict[int, dict]:
    """{item id: {"status", "links_in", "links_out"}} for items that have any link.

    Status is derived exactly as for entries (``links.derive_status``) from the ACTIVE
    incoming links; a revoked link is listed as history, with who revoked it and why,
    and never counts.
    """
    wanted = set(item_ids)
    rows: list[sqlite3.Row] = []
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    try:
        for start in range(0, len(item_ids), _ID_CHUNK):
            chunk = item_ids[start:start + _ID_CHUNK]
            marks = ", ".join("?" for _ in chunk)
            rows.extend(cur.execute(
                "SELECT l.id, l.from_item, l.to_item, l.kind, l.rationale, l.created_at, "
                "l.revoked_at, l.revoke_reason, cb.name AS created_by, "
                "rb.name AS revoked_by, "
                "fi.entry_id AS from_entry, fi.ordinal AS from_ordinal, "
                "ti.entry_id AS to_entry, ti.ordinal AS to_ordinal "
                "FROM item_links l "
                "JOIN entry_items fi ON fi.id = l.from_item "
                "JOIN entry_items ti ON ti.id = l.to_item "
                "LEFT JOIN users cb ON cb.id = l.created_by "
                "LEFT JOIN users rb ON rb.id = l.revoked_by "
                f"WHERE l.from_item IN ({marks}) OR l.to_item IN ({marks}) ORDER BY l.id",
                (*chunk, *chunk),
            ).fetchall())
    finally:
        cur.close()
    result: dict[int, dict] = {}
    seen: set[int] = set()
    for row in rows:
        if row["id"] in seen:   # a link between two items of the same chunk
            continue
        seen.add(row["id"])
        common = {"id": row["id"], "kind": row["kind"], "rationale": row["rationale"],
                  "created_by": row["created_by"], "created_at": row["created_at"],
                  "revoked_at": row["revoked_at"], "revoked_by": row["revoked_by"],
                  "revoke_reason": row["revoke_reason"]}
        if row["to_item"] in wanted:
            result.setdefault(row["to_item"], {"links_in": [], "links_out": []})[
                "links_in"].append(
                    {"from": public_id(row["from_entry"], row["from_ordinal"]), **common})
        if row["from_item"] in wanted:
            result.setdefault(row["from_item"], {"links_in": [], "links_out": []})[
                "links_out"].append(
                    {"to": public_id(row["to_entry"], row["to_ordinal"]), **common})
    for payload in result.values():
        payload["status"] = links.derive_status(
            [link for link in payload["links_in"] if link["revoked_at"] is None])
    return result
