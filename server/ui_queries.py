"""Read models of the UI pages (D14; plan 2026-09-29, slices 4–5).

Composes the substrate the API already serves — ``items.items_for_entries`` (items with
their served status and links, revoked ones included), ``links.links_for_entries`` (entry
status), ``feedback.stats`` — plus the few aggregates the pages need. Everything is
computed per read; nothing is stored. A pilot project holds tens of entries, so each page
loads its project whole and derives in Python, which keeps one definition of "current",
"replaced" and "digest" for every view.

Nothing here writes, and nothing here calls ``recall.record``: browsing is not a recall
(owner decision 3 of 2026-09-29).
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone

from server import feedback, items, links, ui_render

TYPE_ORDER = ("decision", "task", "method", "fact")   # the `current` tab's groups
TYPE_HEADINGS = {"decision": "Decisions", "task": "Tasks", "method": "Methods",
                 "fact": "Facts"}
SIGNAL_VERDICTS = ("stale", "not_applicable")
TIMELINE_PAGE = 30
_ANCHOR_ID_RE = re.compile(r":(\d+)$")


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _day(stamp: str | None) -> str:
    return (stamp or "")[:10]


# --------------------------------------------------------------------------- #
# Loading one project                                                          #
# --------------------------------------------------------------------------- #
def project(conn: sqlite3.Connection, project_id: int) -> dict | None:
    row = conn.execute(
        "SELECT p.id, p.name, p.canonical_remote, p.created_at, u.name AS created_by "
        "FROM projects p LEFT JOIN users u ON u.id = p.created_by WHERE p.id = ?",
        (project_id,)).fetchone()
    return dict(row) if row is not None else None


def _entries(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    """Every entry of the project, newest first, with its served status and title."""
    rows = conn.execute(
        "SELECT e.id, e.session_id, e.created_at, e.covered_from, e.covered_to, "
        "e.content_md, e.model, e.prompt_version, u.name AS author "
        "FROM entries e LEFT JOIN users u ON u.id = e.user_id "
        "WHERE e.project_id = ? ORDER BY e.id DESC", (project_id,)).fetchall()
    entries = [dict(row) for row in rows]
    by_entry = links.links_for_entries(conn, [entry["id"] for entry in entries])
    for entry in entries:
        entry.update(by_entry.get(entry["id"]) or {
            "links_in": [], "links_out": [], "status": links.STATUS_CURRENT})
        fields = dict(ui_render.split_frontmatter(entry["content_md"])[0])
        entry["digest"] = ui_render.is_digest(entry["content_md"])
        entry["title"] = fields.get("title") or "(untitled)"
        entry["status_label"] = ui_render.served_status_label(entry)
        entry["day"] = _day(entry["created_at"])
    return entries


def _items(conn: sqlite3.Connection, project_id: int, entry_ids: list[int], *,
           item_type: str | None = None, q: str | None = None) -> list[dict]:
    """``items.items_for_entries`` in chunks of the ids it accepts; adds ``id``."""
    found: list[dict] = []
    for start in range(0, len(entry_ids), items.MAX_ENTRY_IDS):
        found.extend(items.items_for_entries(
            conn, project_id, entry_ids[start:start + items.MAX_ENTRY_IDS],
            item_type=item_type, q=q))
    for item in found:
        item["id"] = items.public_id(item["entry_id"], item["ordinal"])
    return found


def _feedback(conn: sqlite3.Connection, project_id: int) -> dict[str, list[dict]]:
    """``{item public id: [feedback rows, oldest first]}``, each row NAMED (decision 4)."""
    rows = conn.execute(
        "SELECT i.entry_id, i.ordinal, f.verdict, f.note, f.created_at, u.name AS who "
        "FROM recall_feedback f JOIN entry_items i ON i.id = f.item_id "
        "JOIN users u ON u.id = f.user_id WHERE f.project_id = ? ORDER BY f.id",
        (project_id,)).fetchall()
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(items.public_id(row["entry_id"], row["ordinal"]), []).append({
            "verdict": row["verdict"], "note": row["note"], "who": row["who"],
            "day": _day(row["created_at"])})
    return out


def _replacement(item: dict) -> dict | None:
    """The ACTIVE incoming link that decided a replaced item's status, newest first."""
    kind = {links.STATUS_SUPERSEDED: "supersedes",
            links.STATUS_DUPLICATE: "duplicates"}.get(item["status"])
    active = [link for link in item["links_in"]
              if link["kind"] == kind and link["revoked_at"] is None]
    if not active:
        return None
    link = max(active, key=lambda link: (link["created_at"] or "", link["from"]))
    return {"kind": link["kind"], "by": link["from"], "who": link["created_by"],
            "day": _day(link["created_at"]), "rationale": link["rationale"]}


def _feedback_counts(rows: list[dict]) -> list[tuple[str, int]]:
    counts = Counter(row["verdict"] for row in rows)
    return [(verdict, counts[verdict]) for verdict in feedback.VERDICTS if counts[verdict]]


# --------------------------------------------------------------------------- #
# A — the projects overview                                                    #
# --------------------------------------------------------------------------- #
def _summary(conn: sqlite3.Connection, proj: dict, month_ago: str) -> dict:
    entries = [entry for entry in _entries(conn, proj["id"]) if not entry["digest"]]
    found = _items(conn, proj["id"], [entry["id"] for entry in entries])
    fb = _feedback(conn, proj["id"])
    current = [item for item in found if item["status"] == links.STATUS_CURRENT]
    last = entries[0] if entries else None
    return {
        **proj,
        "entries": len(entries),
        "current": len(current),
        "replaced": len(found) - len(current),
        "tasks": sum(item["item_type"] == "task" for item in current),
        "last_capture": last,
        "authors_30d": len({entry["author"] for entry in entries
                            if entry["author"] and entry["created_at"] >= month_ago}),
        "signals": sum(any(row["verdict"] in SIGNAL_VERDICTS for row in fb.get(item["id"], ()))
                       for item in current),
    }


def overview(conn: sqlite3.Connection) -> list[dict]:
    """One summary per project, the most recently captured first."""
    month_ago = _iso(datetime.now(timezone.utc) - timedelta(days=30))
    rows = conn.execute("SELECT id FROM projects ORDER BY id").fetchall()
    summaries = [_summary(conn, project(conn, row["id"]), month_ago) for row in rows]
    return sorted(summaries, key=lambda s: (
        s["last_capture"]["created_at"] if s["last_capture"] else "",
        s["last_capture"]["id"] if s["last_capture"] else 0), reverse=True)


def pulse(conn: sqlite3.Connection) -> dict:
    """The team totals of the last seven days (``feedback.stats``: names nobody)."""
    return feedback.stats(conn, _iso(datetime.now(timezone.utc) - timedelta(days=7)))


# --------------------------------------------------------------------------- #
# B — the project tabs                                                         #
# --------------------------------------------------------------------------- #
def _digest_panel(entries: list[dict]) -> dict | None:
    digest = next((entry for entry in entries if entry["digest"]), None)
    if digest is None:
        return None
    low = _ANCHOR_ID_RE.search(digest["covered_from"] or "")
    high = _ANCHOR_ID_RE.search(digest["covered_to"] or "")
    return {
        "id": digest["id"], "day": digest["day"],
        "range": (f"entries #{low.group(1)}–#{high.group(1)}" if low and high
                  else f"{digest['covered_from']} – {digest['covered_to']}"),
        "since": sum(1 for entry in entries
                     if not entry["digest"] and entry["id"] > digest["id"]),
    }


def current_view(conn: sqlite3.Connection, project_id: int, *, item_type: str | None,
                 author: str | None, q: str | None, show_replaced: bool) -> dict:
    """The logbook as of now: current items by type; replaced ones only on request.

    Raises ``search.InvalidQuery`` for a malformed ``q`` (the page says so).
    """
    entries = _entries(conn, project_id)
    by_id = {entry["id"]: entry for entry in entries}
    sessions = [entry for entry in entries if not entry["digest"]]
    fb = _feedback(conn, project_id)
    found = _items(conn, project_id, [entry["id"] for entry in sessions],
                   item_type=item_type, q=q)
    rows = []
    for item in found:
        entry = by_id[item["entry_id"]]
        replaced = item["status"] != links.STATUS_CURRENT
        if not item["match"] or (author and entry["author"] != author):
            continue
        if replaced and not show_replaced:
            continue
        rows.append({**item, "author": entry["author"], "day": entry["day"],
                     "replaced": replaced, "replacement": _replacement(item),
                     "feedback": _feedback_counts(fb.get(item["id"], []))})
    groups = []
    for kind in TYPE_ORDER:
        members = sorted((row for row in rows if row["item_type"] == kind),
                         key=lambda row: (-row["entry_id"], row["ordinal"]))
        if members:
            groups.append({"heading": TYPE_HEADINGS[kind], "items": members})
    itemized = {item["entry_id"] for item in found}
    return {
        "groups": groups,
        "digest": _digest_panel(entries),
        "unitemized": [entry for entry in sessions if entry["id"] not in itemized],
        "authors": sorted({entry["author"] for entry in sessions if entry["author"]}),
    }


def timeline(conn: sqlite3.Connection, project_id: int, *, author: str | None,
             since: str | None, before: int | None) -> dict:
    """Every entry, digests included, newest first, ``TIMELINE_PAGE`` at a time."""
    entries = _entries(conn, project_id)
    counts: dict[int, list[int]] = {}
    for item in _items(conn, project_id, [entry["id"] for entry in entries]):
        tally = counts.setdefault(item["entry_id"], [0, 0])
        tally[0] += item["status"] == links.STATUS_CURRENT
        tally[1] += 1
    chosen = [entry for entry in entries
              if (not author or entry["author"] == author)
              and (not since or entry["created_at"] >= since)
              and (before is None or entry["id"] < before)]
    page = chosen[:TIMELINE_PAGE]
    for entry in page:
        current, total = counts.get(entry["id"], (0, 0))
        entry["items_label"] = f"{current} of {total} current" if total else "no items"
        entry["prompt"] = " · ".join(part for part in (entry["model"], entry["prompt_version"])
                                     if part)
    return {"entries": page,
            "older": page[-1]["id"] if len(chosen) > TIMELINE_PAGE else None,
            "authors": sorted({entry["author"] for entry in entries if entry["author"]})}


def curation(conn: sqlite3.Connection, project_id: int) -> dict:
    """The curator's inbox: signals (named), uncurated whole entries, link history."""
    entries = _entries(conn, project_id)
    sessions = [entry for entry in entries if not entry["digest"]]
    by_id = {entry["id"]: entry for entry in entries}
    fb = _feedback(conn, project_id)
    found = _items(conn, project_id, [entry["id"] for entry in entries])
    signals = [
        {**item, "author": by_id[item["entry_id"]]["author"], "feedback": fb[item["id"]]}
        for item in sorted(found, key=lambda item: (-item["entry_id"], item["ordinal"]))
        if item["status"] == links.STATUS_CURRENT
        and not by_id[item["entry_id"]]["digest"]
        and any(row["verdict"] in SIGNAL_VERDICTS for row in fb.get(item["id"], ()))
    ]
    itemized = {item["entry_id"] for item in found}
    history: dict[int, dict] = {}
    for item in found:
        for link in item["links_in"]:
            history[link["id"]] = {**link, "to": item["id"], "day": _day(link["created_at"]),
                                   "revoked_day": _day(link["revoked_at"])}
    return {
        "signals": signals,
        "uncurated": [entry for entry in sessions if entry["id"] not in itemized
                      and entry["status"] == links.STATUS_CURRENT],
        "history": [history[key] for key in sorted(history, reverse=True)],
    }


# --------------------------------------------------------------------------- #
# C — one item, D — one entry, E — usage                                       #
# --------------------------------------------------------------------------- #
LINEAGE_MAX_LINKS = 50
USAGE_WINDOWS = {"7d": 7, "30d": 30, "all": None}


def _project_of(conn: sqlite3.Connection, entry_id: int) -> int | None:
    row = conn.execute("SELECT project_id FROM entries WHERE id = ?", (entry_id,)).fetchone()
    return row[0] if row is not None else None


def _item_status_label(item: dict) -> str:
    """``superseded (by 3.1)`` — the entry wording of D12, with an item id."""
    replacement = _replacement(item)
    if replacement is None:
        return item["status"]
    word = "by" if replacement["kind"] == "supersedes" else "of"
    return f"{item['status']} ({word} {replacement['by']})"


def _lineage(start: str, by_id: dict[str, dict]) -> list[dict]:
    """The ACTIVE links reachable from ``start`` in either direction, newest first.

    A revoked link stops counting, so it never extends a lineage (it stays in the item's
    link history). Bounded at ``LINEAGE_MAX_LINKS``; a cycle cannot exist
    (``items.create_link`` only lets a newer entry's item point at an older one).
    """
    edges: dict[int, dict] = {}
    seen, frontier = {start}, [start]
    while frontier and len(edges) < LINEAGE_MAX_LINKS:
        current = by_id[frontier.pop()]
        ends = [(link, link["from"], current["id"]) for link in current["links_in"]]
        ends += [(link, current["id"], link["to"]) for link in current["links_out"]]
        for link, source, target in ends:
            if link["revoked_at"] is not None or link["id"] in edges:
                continue
            edges[link["id"]] = {**link, "from": source, "to": target,
                                 "day": _day(link["created_at"])}
            for other in (source, target):
                if other not in seen and other in by_id:
                    seen.add(other)
                    frontier.append(other)
    return [edges[key] for key in sorted(edges, reverse=True)][:LINEAGE_MAX_LINKS]


def _all_links(item: dict) -> list[dict]:
    """Every link touching ``item``, active and revoked, newest first."""
    rows = [{**link, "to": item["id"]} for link in item["links_in"]]
    rows += [{**link, "from": item["id"]} for link in item["links_out"]]
    for row in rows:
        row["day"], row["revoked_day"] = _day(row["created_at"]), _day(row["revoked_at"])
    return sorted(rows, key=lambda row: row["id"], reverse=True)


def item_detail(conn: sqlite3.Connection, entry_id: int, ordinal: int) -> dict | None:
    project_id = _project_of(conn, entry_id)
    if project_id is None:
        return None
    entries = {entry["id"]: entry for entry in _entries(conn, project_id)}
    by_id = {item["id"]: item for item in _items(conn, project_id, list(entries))}
    item = by_id.get(items.public_id(entry_id, ordinal))
    if item is None:
        return None
    return {**item, "status_label": _item_status_label(item),
            "entry": entries[entry_id], "project": project(conn, project_id),
            "lineage": _lineage(item["id"], by_id), "links": _all_links(item),
            "feedback": _feedback(conn, project_id).get(item["id"], [])}


def entry_detail(conn: sqlite3.Connection, entry_id: int) -> dict | None:
    project_id = _project_of(conn, entry_id)
    if project_id is None:
        return None
    entry = next(entry for entry in _entries(conn, project_id) if entry["id"] == entry_id)
    fb = _feedback(conn, project_id)
    entry_items = [
        {**item, "author": entry["author"], "day": entry["day"],
         "replaced": item["status"] != links.STATUS_CURRENT,
         "replacement": _replacement(item), "feedback": _feedback_counts(fb.get(item["id"], []))}
        for item in _items(conn, project_id, [entry_id])]
    return {**entry, "project": project(conn, project_id),
            "frontmatter": ui_render.frontmatter_rows(entry),
            "body": ui_render.split_frontmatter(entry["content_md"])[1],
            "items": entry_items,
            "entry_links": ([{**link, "to_entry": entry_id, "day": _day(link["created_at"])}
                             for link in entry["links_in"]]
                            + [{**link, "from_entry": entry_id, "day": _day(link["created_at"])}
                               for link in entry["links_out"]])}


def usage(conn: sqlite3.Connection, window: str) -> dict:
    """``feedback.stats`` over one of ``USAGE_WINDOWS`` — team totals, names nobody."""
    days = USAGE_WINDOWS[window]
    floor = None if days is None else _iso(datetime.now(timezone.utc) - timedelta(days=days))
    return feedback.stats(conn, floor)
