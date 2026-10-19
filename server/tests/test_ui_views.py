"""The read-only UI's pages (D14, plan 2026-09-29).

Slice 3 — the shell every page shares: AC-6 (read-only by construction and by effect),
AC-8 (security headers on every `/ui` response), AC-9 (browsing is not a recall), and the
I5/D6 banner. Expected values are the plan's literals.

The crawl is DISCOVERED from the app's routes, not listed by hand: a page added by a
later slice is covered the moment it exists, and a page whose path needs a parameter the
crawl does not know fails loudly instead of being skipped.
"""
from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from server import db

EXPECTED_HEADERS = {
    "content-security-policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "referrer-policy": "same-origin",   # /ui/join keeps no-referrer: test_join.py
    "x-content-type-options": "nosniff",
    "cache-control": "no-store",
}
BANNER = ("Automatically generated from developers' Claude Code sessions. "
          "Unverified reference — check it against the code and the PR.")
PUBLIC_UI_PATHS = {"/ui/login", "/ui/session", "/ui/join", "/ui/static/login.js",
                   "/ui/static/favicon.svg", "/ui/static/ui.css"}
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


# --------------------------------------------------------------------------- #
# The crawl                                                                    #
# --------------------------------------------------------------------------- #
@pytest.fixture
def crawl_params(seeded) -> dict[str, str]:
    """Values for the path parameters of `/ui` pages, taken from the seeded corpus."""
    return {"project_id": str(seeded["alpha"]), "entry_id": "2", "ordinal": "1"}


def _ui_routes() -> list[tuple[str, set[str]]]:
    """`/ui` routes declared on the app itself AND on the pages router.

    Both, because FastAPI (0.141) wraps an included router in one opaque entry of
    `app.routes`: walking the app alone found no page at all, and every crawl-based
    assertion below passed on an empty list.
    """
    from server import ui_pages
    from server.app import app

    found = []
    for route in [*app.routes, *ui_pages.router.routes]:
        # `path_format` drops a convertor (`{project_id:int}` → `{project_id}`).
        path = getattr(route, "path_format", None) or getattr(route, "path", "")
        if path == "/ui" or path.startswith("/ui/"):
            found.append((path, set(getattr(route, "methods", set()) or set())))
    return found


def _page_paths(params: dict[str, str]) -> list[str]:
    """Every signed-in `/ui` page, with its parameters filled in."""
    paths = []
    for template, methods in _ui_routes():
        if template in PUBLIC_UI_PATHS or "GET" not in methods:
            continue
        try:
            paths.append(template.format(**params))
        except KeyError as missing:
            pytest.fail(f"the crawl has no value for {missing} in {template}: "
                        "extend `crawl_params`")
    assert "/ui/" in paths, "the crawl found no pages: every assertion over it is vacuous"
    return paths


def _row_counts(env) -> dict[str, int]:
    conn = sqlite3.connect(env)
    try:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {table: conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                for table in tables}
    finally:
        conn.close()


def _promote(env, user) -> None:
    conn = db.connect(env)
    try:
        conn.execute("UPDATE users SET role = 'admin' WHERE id = ?", (user["id"],))
        conn.commit()
    finally:
        conn.close()


def test_the_crawl_finds_the_pages(crawl_params):
    assert "/ui/" in _page_paths(crawl_params)


# --------------------------------------------------------------------------- #
# AC-8 — security headers on every `/ui` response                              #
# --------------------------------------------------------------------------- #
def _assert_headers(response, what: str):
    for name, value in EXPECTED_HEADERS.items():
        assert response.headers.get(name) == value, f"{what}: {name}"


def test_every_signed_in_page_carries_the_security_headers(ui, ui_user, env, crawl_params):
    _promote(env, ui_user)  # every page, admin ones too; members' 403s: test_ui_admin.py
    for path in _page_paths(crawl_params):
        for method in ("GET", "HEAD"):
            r = ui.request(method, path)
            assert r.status_code == 200, f"{method} {path}"
            _assert_headers(r, f"{method} {path}")


def test_public_refused_and_missing_ui_responses_carry_them_too(ui, ui_user):
    for path in ("/ui/login", "/ui/static/login.js", "/ui/static/favicon.svg",
                 "/ui/static/ui.css"):
        r = ui.get(path)
        assert r.status_code == 200, path
        _assert_headers(r, path)
    _assert_headers(ui.get("/ui/no-such-page"), "404")
    _assert_headers(ui.post("/ui/"), "405")
    _assert_headers(ui.post("/ui/session", json={"code": "wrong-code-wrong-code-0000"}),
                    "failed session")
    ui.cookies.clear()
    _assert_headers(ui.get("/ui/"), "401")


def test_the_api_keeps_its_own_headers(ui, ui_user):
    r = ui.get("/api/health")
    assert "content-security-policy" not in r.headers
    assert r.headers["strict-transport-security"]


# --------------------------------------------------------------------------- #
# AC-6 — read-only by construction and by effect                               #
# --------------------------------------------------------------------------- #
def test_no_page_accepts_a_write_even_with_a_session(ui, ui_user, crawl_params):
    for path in _page_paths(crawl_params):
        for method in WRITE_METHODS:
            assert ui.request(method, path).status_code == 405, f"{method} {path}"


def test_a_crawl_of_every_page_changes_no_table(ui, ui_user, env, crawl_params):
    _promote(env, ui_user)  # every page, admin ones too; members' 403s: test_ui_admin.py
    before = _row_counts(env)
    for path in _page_paths(crawl_params):
        assert ui.get(path).status_code == 200, path
        ui.head(path)
    assert _row_counts(env) == before


# --------------------------------------------------------------------------- #
# AC-9 — browsing is not a recall (owner decision 3)                           #
# --------------------------------------------------------------------------- #
def test_browsing_writes_no_recall_event(ui, ui_user, env, crawl_params):
    for path in _page_paths(crawl_params):
        ui.get(path, headers={"X-Loom-Source": "skill"})   # even when a header claims one
    conn = db.connect(env)
    try:
        assert conn.execute("SELECT count(*) FROM recall_events").fetchone()[0] == 0
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The shell                                                                    #
# --------------------------------------------------------------------------- #
def test_every_page_carries_the_banner_the_user_and_no_inline_code(ui, ui_user, crawl_params):
    for path in _page_paths(crawl_params):
        page = ui.get(path).text
        assert BANNER in page, path
        assert "dev1" in page, path
        assert '<link rel="stylesheet" href="/ui/static/ui.css">' in page, path
        assert '<link rel="icon" href="/ui/static/favicon.svg" type="image/svg+xml">' in page
        # The CSP allows neither: an inline block would be silently dead in the browser.
        assert not re.search(r"<script(?![^>]*\ssrc=)", page), path
        assert not re.search(r"\sstyle=", page), path
        assert "<style" not in page, path


def test_the_stylesheet_is_public_css(ui):
    r = ui.get("/ui/static/ui.css")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")


def test_the_login_and_signed_out_pages_use_the_stylesheet(ui):
    link = '<link rel="stylesheet" href="/ui/static/ui.css">'
    assert link in ui.get("/ui/login").text
    assert link in ui.get("/ui/").text     # the 401 page


def test_an_unknown_page_is_an_html_404_inside_the_shell(ui, ui_user):
    r = ui.get("/ui/no-such-page")
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]
    assert "Not found" in r.text and BANNER in r.text


# =========================================================================== #
# Slice 4 — the projects overview and the project tabs (AC-11…AC-14)           #
# =========================================================================== #
ALPHA = "github.com/org/alpha"
BETA = "github.com/org/beta"


def _okf(title: str, body: str, kind: str = "decision-log-entry") -> str:
    """An entry shaped like `okf.wrap_entry` / `wrap_digest` output (the fields read)."""
    return (f"---\ntype: {kind}\ntitle: {json.dumps(title)}\n"
            "status: current\n---\n" + body)


def _post_entry(ui, user, remote, session, title, body, items=None, *, kind="decision-log-entry",
                prompt="v3", covered=(None, None)) -> int:
    payload = {"project_remote": remote, "session_id": session,
               "content_md": _okf(title, body, kind),
               "content_hash": hashlib.sha256(session.encode()).hexdigest(),
               "covered_from": covered[0], "covered_to": covered[1],
               "model": "sonnet", "prompt_version": prompt}
    if items is not None:
        payload["items"] = [{"item_type": t, "labelled": labelled, "text_md": text}
                            for t, labelled, text in items]
    r = ui.post("/api/entries", json=payload, headers=user["auth"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _ok(response, status=201):
    assert response.status_code == status, response.text
    return response.json()


@pytest.fixture
def seeded(ui, ui_user, user_factory, env):
    """The literal corpus the slice-4 expectations are computed from, by hand.

    alpha — E1 ana (41 d ago, no items) · E2 ana (40 d ago: 2.1 decision, 2.2 task,
    2.3 fact) · E3 bea (3.1 decision SUPERSEDES 2.1, 3.2 method, 3.3 task) · E4 bea (no
    items, SUPERSEDES E1) · E5 ana DIGEST of 1–4 (carries one item that must never show)
    · E6 bea (6.1 fact UNLABELLED, 6.2 decision DUPLICATES 3.2 — revoked).
    beta — E7 bea (2 d ago: 7.1 task).
    Feedback: dev1 stale 2.3 · bea not_applicable 2.3 · ana useful 3.1 ·
    bea not_applicable 2.1 (a replaced item: never a curation signal).
    """
    dev1 = ui_user
    ana, bea = user_factory("ana"), user_factory("bea")
    alpha = _ok(ui.post("/api/projects", json={"remote_url": "git@github.com:org/alpha.git"},
                        headers=ana["auth"]))["id"]
    beta = _ok(ui.post("/api/projects", json={"remote_url": "https://github.com/org/beta"},
                       headers=bea["auth"]))["id"]

    e1 = _post_entry(ui, ana, ALPHA, "s-1", "REST for the API",
                     "## Context\nPre-OKF style capture.\n")
    i21 = "- [decision] **Use httpx** — async support."
    i22 = "- [task] Write the migration guide."
    i23 = "- [fact] The proxy strips `Authorization`."
    e2 = _post_entry(ui, ana, ALPHA, "s-2", "Use httpx",
                     f"## Decisions\n{i21}\n{i22}\n{i23}\n",
                     [("decision", True, i21), ("task", True, i22), ("fact", True, i23)])
    i31 = "- [decision] **Back to requests** — httpx breaks the proxy."
    i32 = "- [method] Pin dependencies with `uv lock`."
    i33 = "- [task] Drop the httpx shim."
    e3 = _post_entry(ui, bea, ALPHA, "s-3", "Back to requests",
                     f"## Decisions\n{i31}\n{i32}\n{i33}\n",
                     [("decision", True, i31), ("method", True, i32), ("task", True, i33)])
    e4 = _post_entry(ui, bea, ALPHA, "s-4", "gRPC for the API", "## Context\nNo items.\n")
    digest_item = "- [decision] **Digest line that is not an item**"
    e5 = _post_entry(ui, ana, ALPHA, "digest-1-4", "Current state of decisions (2026-09-28)",
                     f"## Current state\n{digest_item}\n",
                     [("decision", True, digest_item)], kind="digest", prompt="digest-v1",
                     covered=(f"digest:{ALPHA}:1", f"digest:{ALPHA}:4"))
    i61 = "- CI runs on Python 3.12."
    i62 = "- [decision] **Keep one worker** — SQLite has one writer."
    e6 = _post_entry(ui, bea, ALPHA, "s-6", "Keep one worker",
                     f"## Decisions\n{i61}\n{i62}\n",
                     [("fact", False, i61), ("decision", True, i62)])
    e7 = _post_entry(ui, bea, BETA, "s-7", "Beta CI", "## Decisions\n- [task] Set up beta CI.\n",
                     [("task", True, "- [task] Set up beta CI.")])
    assert (e1, e2, e3, e4, e5, e6, e7) == (1, 2, 3, 4, 5, 6, 7)

    link1 = _ok(ui.post("/api/item-links", json={"from": "3.1", "to": "2.1", "kind": "supersedes",
                                                 "rationale": "httpx breaks the proxy"},
                        headers=bea["auth"]))["id"]
    link2 = _ok(ui.post("/api/item-links", json={"from": "6.2", "to": "3.2", "kind": "duplicates"},
                        headers=ana["auth"]))["id"]
    _ok(ui.post(f"/api/item-links/{link2}/revoke", json={"reason": "not the same decision"},
                headers=bea["auth"]), 200)
    _ok(ui.post("/api/links", json={"from_entry": 4, "to_entry": 1, "kind": "supersedes",
                                    "rationale": "REST replaced by gRPC"}, headers=bea["auth"]))
    for who, item, verdict, note in [
            (dev1, "2.3", "stale", "proxy fixed upstream"),
            (bea, "2.3", "not_applicable", "different proxy here"),
            (ana, "3.1", "useful", "saved me a day"),
            (bea, "2.1", "not_applicable", "we never used httpx")]:
        _ok(ui.post("/api/feedback", json={"project_id": alpha, "item": item,
                                           "verdict": verdict, "note": note},
                    headers=who["auth"]))

    now = datetime.now(timezone.utc)
    conn = db.connect(env)
    try:
        for entry_id, days in ((1, 41), (2, 40), (7, 2)):
            conn.execute("UPDATE entries SET created_at = ? WHERE id = ?",
                         ((now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"), entry_id))
        conn.commit()
    finally:
        conn.close()
    return {"alpha": alpha, "beta": beta, "today": now.strftime("%Y-%m-%d"),
            "link1": link1, "link2": link2,
            "names": ("ana", "bea", "dev1"), "users": {"ana": ana, "bea": bea}}


def _section(page: str, section_id: str) -> str:
    match = re.search(rf'<section id="{section_id}"[^>]*>(.*?)</section>', page, re.DOTALL)
    assert match is not None, f"no section #{section_id}"
    return match.group(1)


def _text(fragment: str) -> str:
    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _ids(fragment: str, attr: str) -> list[str]:
    return re.findall(rf'data-{attr}="([^"]+)"', fragment)


def _block(fragment: str, tag: str, attr: str, value: str) -> str:
    match = re.search(rf'<{tag}[^>]*data-{attr}="{re.escape(value)}"[^>]*>(.*?)</{tag}>',
                      fragment, re.DOTALL)
    assert match is not None, f"no <{tag} data-{attr}={value}>"
    return match.group(0)


def _cells(row: str) -> dict[str, str]:
    return {name: _text(value) for name, value in
            re.findall(r'<td data-col="([^"]+)"[^>]*>(.*?)</td>', row, re.DOTALL)}


def _project_page(ui, seeded, **params):
    r = ui.get(f"/ui/projects/{seeded['alpha']}", params=params)
    assert r.status_code == 200, r.text
    return r.text


# --------------------------------------------------------------------------- #
# A — the projects overview (AC-13, AC-12 strip)                               #
# --------------------------------------------------------------------------- #
def test_overview_numbers_match_the_seeded_corpus(ui, seeded):
    page = ui.get("/ui/").text
    table = _section(page, "projects")

    assert _ids(table, "project") == ["alpha", "beta"]   # last capture, newest first
    assert _cells(_block(table, "tr", "project", "alpha")) == {
        "name": "alpha github.com/org/alpha", "registered": f"ana {seeded['today']}",
        "entries": "5", "current": "7", "replaced": "1", "tasks": "2",
        "last_capture": f"{seeded['today']} bea", "authors_30d": "1", "signals": "1"}
    beta_day = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
    assert _cells(_block(table, "tr", "project", "beta")) == {
        "name": "beta github.com/org/beta", "registered": f"bea {seeded['today']}",
        "entries": "1", "current": "1", "replaced": "0", "tasks": "1",
        "last_capture": f"{beta_day} bea", "authors_30d": "1", "signals": "0"}
    assert f'href="/ui/projects/{seeded["alpha"]}"' in table


def test_the_pulse_strip_counts_the_last_seven_days_and_names_nobody(ui, seeded):
    strip = _section(ui.get("/ui/").text, "pulse")

    assert dict(re.findall(r'data-stat="([^"]+)">(\d+)<', strip)) == {
        "recalls-skill": "0", "recalls-cli": "0", "feedback-useful": "1",
        "feedback-not_applicable": "2", "feedback-stale": "1"}
    for name in seeded["names"]:
        assert name not in _text(strip)


def test_the_overview_of_an_empty_logbook_says_how_to_start(ui, ui_user):
    page = ui.get("/ui/").text
    assert "No project registered yet" in page and "loom register" in page


# --------------------------------------------------------------------------- #
# B — the `current` tab (AC-11, AC-14)                                         #
# --------------------------------------------------------------------------- #
def test_current_lists_current_items_by_type_newest_first(ui, seeded):
    items = _section(_project_page(ui, seeded), "current-items")

    assert _ids(items, "item") == ["6.2", "3.1", "3.3", "2.2", "3.2", "6.1", "2.3"]
    groups = re.findall(r"<h2[^>]*>([^<]+)</h2>", items)
    assert [g.split()[0] for g in groups] == ["Decisions", "Tasks", "Methods", "Facts"]


def test_a_replaced_item_shows_only_on_request_with_its_replacement(ui, seeded):
    items = _section(_project_page(ui, seeded, show_replaced="1"), "current-items")

    assert _ids(items, "item") == ["6.2", "3.1", "2.1", "3.3", "2.2", "3.2", "6.1", "2.3"]
    row = _block(items, "article", "item", "2.1")
    assert 'data-status="superseded"' in row and "replaced" in row.split(">")[0]
    assert f"replaced by 3.1 · bea · {seeded['today']} · httpx breaks the proxy" in _text(row)


def test_a_revoked_link_leaves_its_target_current(ui, seeded):
    items = _section(_project_page(ui, seeded, show_replaced="1"), "current-items")
    row = _block(items, "article", "item", "3.2")
    assert 'data-status="current"' in row and "replaced by" not in _text(row)


def test_item_rows_carry_author_date_feedback_and_the_unlabelled_marker(ui, seeded):
    items = _section(_project_page(ui, seeded), "current-items")

    fact = _text(_block(items, "article", "item", "2.3"))
    assert "ana" in fact and "stale 1" in fact and "not applicable 1" in fact
    assert "useful 1" in _text(_block(items, "article", "item", "3.1"))
    assert "unlabelled" in _text(_block(items, "article", "item", "6.1"))
    assert "unlabelled" not in _text(_block(items, "article", "item", "6.2"))
    assert 'href="/ui/items/2.3"' in items


def test_the_digest_panel_states_its_range_and_what_came_after(ui, seeded):
    panel = _text(_section(_project_page(ui, seeded), "digest"))
    assert "Digest #5" in panel and "entries #1–#4" in panel
    assert "1 entry captured since" in panel


def test_entries_without_items_are_listed_with_their_served_status(ui, seeded):
    section = _section(_project_page(ui, seeded), "unitemized")
    assert _ids(section, "entry") == ["4", "1"]
    assert "superseded (by #4)" in _text(_block(section, "div", "entry", "1"))
    assert "current" in _text(_block(section, "div", "entry", "4"))


@pytest.mark.parametrize(("params", "expected"), [
    ({"type": "task"}, ["3.3", "2.2"]),
    ({"author": "bea"}, ["6.2", "3.1", "3.3", "3.2", "6.1"]),
    ({"q": "proxy"}, ["3.1", "2.3"]),
    ({"type": "decision", "author": "ana"}, []),
])
def test_current_filters(ui, seeded, params, expected):
    assert _ids(_section(_project_page(ui, seeded, **params), "current-items"), "item") == expected


def test_a_malformed_search_is_said_not_crashed(ui, seeded):
    page = _project_page(ui, seeded, q='"unbalanced')
    assert "not a valid search expression" in page
    assert _ids(_section(page, "current-items"), "item") == []


def test_unknown_project_tab_or_type_is_a_404(ui, seeded):
    assert ui.get("/ui/projects/999").status_code == 404
    assert ui.get("/ui/projects/abc").status_code == 404
    assert ui.get(f"/ui/projects/{seeded['alpha']}", params={"tab": "nope"}).status_code == 404
    assert ui.get(f"/ui/projects/{seeded['alpha']}", params={"type": "nope"}).status_code == 404


# --------------------------------------------------------------------------- #
# B — the `timeline` tab (AC-10 on the timeline)                               #
# --------------------------------------------------------------------------- #
def test_timeline_lists_every_entry_newest_first_with_its_state(ui, seeded):
    rows = _section(_project_page(ui, seeded, tab="timeline"), "timeline")

    assert _ids(rows, "entry") == ["6", "5", "4", "3", "2", "1"]
    first = _cells(_block(rows, "tr", "entry", "1"))
    assert first["status"] == "superseded (by #4)" and first["items"] == "no items"
    assert first["title"] == "REST for the API" and first["author"] == "ana"
    assert _cells(_block(rows, "tr", "entry", "2"))["items"] == "2 of 3 current"
    assert "digest" in _cells(_block(rows, "tr", "entry", "5"))["title"].lower()
    assert _cells(_block(rows, "tr", "entry", "3"))["prompt"] == "sonnet · v3"


@pytest.mark.parametrize(("params", "expected"), [
    ({"author": "bea"}, ["6", "4", "3"]),
    ({"before": "4"}, ["3", "2", "1"]),
    ("since-today", ["6", "5", "4", "3"]),
])
def test_timeline_filters(ui, seeded, params, expected):
    if params == "since-today":
        params = {"since": seeded["today"]}
    rows = _section(_project_page(ui, seeded, tab="timeline", **params), "timeline")
    assert _ids(rows, "entry") == expected


# --------------------------------------------------------------------------- #
# B — the `curation` tab (AC-12: feedback is NAMED here)                       #
# --------------------------------------------------------------------------- #
def test_curation_signals_name_who_said_what(ui, seeded):
    signals = _section(_project_page(ui, seeded, tab="curation"), "signals")

    assert _ids(signals, "item") == ["2.3"]
    text = _text(signals)
    assert "dev1 · stale · proxy fixed upstream" in text
    assert "bea · not applicable · different proxy here" in text


def test_curation_lists_uncurated_whole_entries_and_the_link_history(ui, seeded):
    page = _project_page(ui, seeded, tab="curation")

    assert _ids(_section(page, "uncurated"), "entry") == ["4"]
    history = _section(page, "link-history")
    assert _ids(history, "link") == [str(seeded["link2"]), str(seeded["link1"])]
    revoked = _text(_block(history, "tr", "link", str(seeded["link2"])))
    assert "6.2 duplicates 3.2" in revoked and "ana" in revoked
    assert "revoked by bea" in revoked and "not the same decision" in revoked
    active = _text(_block(history, "tr", "link", str(seeded["link1"])))
    assert "3.1 supersedes 2.1" in active and "bea" in active and "httpx breaks the proxy" in active


def test_every_tab_renders_inside_the_shell(ui, seeded):
    for tab in ("current", "timeline", "curation"):
        page = _project_page(ui, seeded, tab=tab)
        assert BANNER in page
        assert f'href="/ui/projects/{seeded["alpha"]}?tab={tab}" aria-current="page"' in page


def test_timeline_pages_through_older_entries(ui, seeded, monkeypatch):
    from server import ui_queries

    monkeypatch.setattr(ui_queries, "TIMELINE_PAGE", 2)
    first = _section(_project_page(ui, seeded, tab="timeline", author="bea"), "timeline")
    assert _ids(first, "entry") == ["6", "4"]
    older = html_lib.unescape(re.search(r'href="([^"]+)">Older entries', first).group(1))
    assert older == f"/ui/projects/{seeded['alpha']}?tab=timeline&author=bea&before=4"

    last = _section(ui.get(older).text, "timeline")
    assert _ids(last, "entry") == ["3"] and "Older entries" not in last


# =========================================================================== #
# Slice 5 — item, entry and usage pages (AC-10, AC-12)                         #
# =========================================================================== #
def _field(fragment: str, name: str) -> str:
    return _text(_block(fragment, "dd", "field", name))


# --------------------------------------------------------------------------- #
# C — the item page                                                            #
# --------------------------------------------------------------------------- #
def test_item_page_shows_text_state_provenance_and_named_feedback(ui, seeded):
    page = ui.get("/ui/items/2.3").text

    assert "The proxy strips <code>Authorization</code>." in page
    summary = _section(page, "item-summary")
    assert _field(summary, "type") == "fact" and _field(summary, "status") == "current"
    assert _field(summary, "project") == "alpha"
    provenance = _section(page, "provenance")
    assert _field(provenance, "entry") == "#2 Use httpx"
    assert _field(provenance, "author") == "ana"
    assert _field(provenance, "session") == "s-2"
    assert _field(provenance, "model") == "sonnet · v3"
    feedback_text = _text(_section(page, "feedback"))
    assert "dev1 · stale · proxy fixed upstream" in feedback_text
    assert "bea · not applicable · different proxy here" in feedback_text


def test_item_page_status_names_its_replacement(ui, seeded):
    summary = _section(ui.get("/ui/items/2.1").text, "item-summary")
    assert _field(summary, "status") == "superseded (by 3.1)"


def test_item_lineage_walks_active_links_both_ways(ui, seeded):
    ana = seeded["users"]["ana"]
    extra = _ok(ui.post("/api/item-links", json={"from": "6.2", "to": "3.1", "kind": "supersedes",
                                                 "rationale": "one worker settles it"},
                        headers=ana["auth"]))["id"]

    for item in ("2.1", "3.1", "6.2"):
        lineage = _section(ui.get(f"/ui/items/{item}").text, "lineage")
        assert _ids(lineage, "link") == [str(extra), str(seeded["link1"])], item
    text = _text(_section(ui.get("/ui/items/2.1").text, "lineage"))
    assert "6.2 supersedes 3.1 · ana" in text and "one worker settles it" in text
    assert "3.1 supersedes 2.1 · bea" in text


def test_a_revoked_link_is_history_on_the_item_page_never_lineage(ui, seeded):
    page = ui.get("/ui/items/3.2").text

    assert _ids(_section(page, "lineage"), "link") == []
    row = _text(_block(_section(page, "links"), "tr", "link", str(seeded["link2"])))
    assert "6.2 duplicates 3.2" in row and "ana" in row
    assert "revoked by bea" in row and "not the same decision" in row
    assert _field(_section(page, "item-summary"), "status") == "current"


@pytest.mark.parametrize("path", [f"/ui/projects/{2**63}", f"/ui/entries/{10**20}",
                                  f"/ui/items/{2**63}.1", f"/ui/items/2.{2**63}"])
def test_an_id_too_large_for_a_row_is_a_404_not_a_500(ui, seeded, path):
    r = ui.get(path)
    assert r.status_code == 404, path
    assert "Not found" in r.text


def test_unknown_items_are_404(ui, seeded):
    for path in ("/ui/items/9.1", "/ui/items/2.99", "/ui/items/abc", "/ui/items/2"):
        r = ui.get(path)
        assert r.status_code == 404, path
        assert "Not found" in r.text


# --------------------------------------------------------------------------- #
# D — the entry page (AC-10)                                                   #
# --------------------------------------------------------------------------- #
def test_entry_page_overlays_the_served_status_on_the_stored_frontmatter(ui, seeded):
    page = ui.get("/ui/entries/1").text

    frontmatter = _section(page, "frontmatter")
    assert _field(frontmatter, "status") == "superseded (by #4)"
    assert _field(frontmatter, "title") == "REST for the API"
    assert "status: current" not in page
    assert "<h2>Context</h2>" in _section(page, "body")
    links_text = _text(_section(page, "entry-links"))
    assert "#4 supersedes #1" in links_text and "REST replaced by gRPC" in links_text


def test_entry_page_lists_its_items_with_ids_and_state(ui, seeded):
    items_html = _section(ui.get("/ui/entries/2").text, "entry-items")

    assert _ids(items_html, "item") == ["2.1", "2.2", "2.3"]
    row = _block(items_html, "article", "item", "2.1")
    assert 'data-status="superseded"' in row
    assert "replaced by 3.1 · bea" in _text(row)


def test_a_digest_entry_page_says_it_is_a_digest(ui, seeded):
    page = ui.get("/ui/entries/5").text
    assert "digest" in _text(_section(page, "entry-summary")).lower()
    assert _field(_section(page, "frontmatter"), "type") == "digest"


def test_unknown_entries_are_404(ui, seeded):
    assert ui.get("/ui/entries/999").status_code == 404
    assert ui.get("/ui/entries/abc").status_code == 404


# --------------------------------------------------------------------------- #
# E — the usage page (AC-12: person-free)                                      #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("since", [None, "7d", "30d", "all"])
def test_usage_totals_and_top_useful_items_name_nobody(ui, seeded, since):
    r = ui.get("/ui/usage", params={"since": since} if since else None)
    assert r.status_code == 200
    usage = _section(r.text, "usage")

    assert dict(re.findall(r'data-stat="([^"]+)">(\d+)<', usage)) == {
        "recalls-skill": "0", "recalls-cli": "0", "feedback-useful": "1",
        "feedback-not_applicable": "2", "feedback-stale": "1"}
    top = _section(r.text, "top-useful")
    assert _ids(top, "item") == ["3.1"]
    assert _cells(_block(top, "tr", "item", "3.1")) == {
        "item": "3.1", "project": "alpha", "type": "decision", "people": "1"}
    for name in seeded["names"]:
        assert name not in _text(usage) and name not in _text(top)


def test_usage_window_is_a_closed_vocabulary(ui, seeded):
    assert ui.get("/ui/usage", params={"since": "yesterday"}).status_code == 404


def test_the_navigation_reaches_usage(ui, seeded):
    assert '<a href="/ui/usage">Usage</a>' in ui.get("/ui/").text
