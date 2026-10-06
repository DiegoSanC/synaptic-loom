"""Pages of the read-only UI (D14; plan 2026-09-29, slices 3–5).

Everything here is reached through ``app._ui_session_gate`` (``GET``/``HEAD`` with a live
session) except the public assets, which ``app.PUBLIC_PATHS`` lists one by one. The
sign-in routes themselves (``/api/ui-login``, ``/ui/session``) live in ``server.app``,
next to the audit and rate-limit helpers they share with ``/api/enroll``.

Templates are Jinja2 with autoescape ON and ``StrictUndefined``: a value reaches a page
escaped unless it came out of ``ui_render.render_md``, the one producer of safe markup.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

import jinja2
from fastapi import APIRouter, Query, Request
from starlette.responses import HTMLResponse, Response

from server import db, feedback, search, ui_queries, ui_render

UI_DIR = Path(__file__).with_name("ui")

# Every `/ui` response, public or not, refused or not (AC-8). No inline script or style
# is allowed, which is why every page links `ui.css` and the login page `login.js`.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}

_ENV = jinja2.Environment(
    loader=jinja2.FileSystemLoader(UI_DIR / "templates"),
    autoescape=True,
    undefined=jinja2.StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)
_ENV.globals["render_md"] = ui_render.render_md

router = APIRouter()


def is_ui_path(path: str) -> bool:
    return path == "/ui" or path.startswith("/ui/")


def render(name: str, *, user: str | None, status: int = 200, **context) -> HTMLResponse:
    """One template, rendered inside the shell (banner, navigation, who is signed in)."""
    return HTMLResponse(_ENV.get_template(name).render(user=user, **context),
                        status_code=status)


def message(status: int, heading: str, text: str, *, user: str | None = None,
            headers: dict[str, str] | None = None) -> HTMLResponse:
    response = render("message.html", user=user, status=status, heading=heading,
                      message=text)
    response.headers.update(headers or {})
    return response


def not_found(request: Request) -> HTMLResponse:
    return message(404, "Not found", "There is no such page, project, entry or item.",
                   user=getattr(request.state, "user_name", None))


def _page(path: str):
    """A signed-in page: ``GET`` and ``HEAD`` (the gate refuses every other method)."""
    return router.api_route(path, methods=["GET", "HEAD"])


# --------------------------------------------------------------------------- #
# Public assets (each one an entry of `app.PUBLIC_PATHS`)                      #
# --------------------------------------------------------------------------- #
_LOGIN_HTML = (UI_DIR / "login.html").read_text(encoding="utf-8")
_STATIC = {
    name: (UI_DIR / "static" / name).read_text(encoding="utf-8")
    for name in ("login.js", "favicon.svg", "ui.css")
}


@router.get("/ui/login")
def login_page():
    """Static. The code is in the URL fragment; this request never carries it."""
    return HTMLResponse(_LOGIN_HTML)


@router.get("/ui/static/login.js")
def login_script():
    return Response(_STATIC["login.js"], media_type="text/javascript; charset=utf-8")


@router.get("/ui/static/favicon.svg")
def favicon():
    return Response(_STATIC["favicon.svg"], media_type="image/svg+xml")


@router.get("/ui/static/ui.css")
def stylesheet():
    return Response(_STATIC["ui.css"], media_type="text/css; charset=utf-8")


# --------------------------------------------------------------------------- #
# Pages                                                                        #
# --------------------------------------------------------------------------- #
@_page("/ui/")
def home(request: Request):
    """A — the projects overview, and the person-free pulse of the last seven days."""
    conn = db.connect()
    try:
        projects, pulse = ui_queries.overview(conn), ui_queries.pulse(conn)
    finally:
        conn.close()
    return render("home.html", user=request.state.user_name, projects=projects, pulse=pulse)


TABS = (("current", "Current"), ("timeline", "Timeline"), ("curation", "Curation"))
_EMPTY_CURRENT = {"groups": [], "digest": None, "unitemized": [], "authors": []}


@_page("/ui/projects/{project_id:int}")
def project_page(
    request: Request,
    project_id: int,
    tab: str = "current",
    item_type: str | None = Query(None, alias="type"),
    author: str | None = None,
    q: str | None = None,
    show_replaced: str | None = None,
    since: str | None = None,
    before: str | None = None,
):
    """B — one project: ``current`` (default), ``timeline`` or ``curation``.

    Every filter is a query parameter, so a view can be linked. An empty form field is
    no filter; an unknown tab, type or page cursor is a 404 like an unknown project.
    """
    item_type, author, q = item_type or None, author or None, (q or "").strip() or None
    if tab not in dict(TABS) or (item_type is not None and item_type not in search.ITEM_TYPES):
        return not_found(request)
    if before is not None and not before.isdigit():
        return not_found(request)
    error, older_url, since_floor = None, None, None
    if since:
        since_floor = feedback.parse_since(since)
        if since_floor is None:
            error = "'Since' must be a date (YYYY-MM-DD)."
    conn = db.connect()
    try:
        proj = ui_queries.project(conn, project_id)
        if proj is None:
            return not_found(request)
        if tab == "current":
            try:
                view = ui_queries.current_view(conn, project_id, item_type=item_type,
                                               author=author, q=q,
                                               show_replaced=bool(show_replaced))
            except search.InvalidQuery:
                error, view = "The search is not a valid search expression.", _EMPTY_CURRENT
        elif tab == "timeline":
            view = ui_queries.timeline(conn, project_id, author=author, since=since_floor,
                                       before=int(before) if before else None)
            if view["older"] is not None:
                older_url = f"/ui/projects/{project_id}?" + urlencode(
                    {"tab": "timeline", **({"author": author} if author else {}),
                     **({"since": since} if since_floor else {}), "before": view["older"]})
        else:
            view = ui_queries.curation(conn, project_id)
    finally:
        conn.close()
    return render(
        "project.html", user=request.state.user_name, project=proj, tab=tab, tabs=TABS,
        view=view, error=error, older_url=older_url, types=search.ITEM_TYPES,
        filtering=bool(item_type or author or q),
        filters={"type": item_type, "author": author, "q": q,
                 "show_replaced": bool(show_replaced),
                 "since_day": (since_floor or "")[:10]},
    )


@_page("/ui/items/{entry_id:int}.{ordinal:int}")
def item_page(request: Request, entry_id: int, ordinal: int):
    """C — one item: state, lineage, every link, provenance and NAMED feedback."""
    conn = db.connect()
    try:
        item = ui_queries.item_detail(conn, entry_id, ordinal)
    finally:
        conn.close()
    if item is None:
        return not_found(request)
    return render("item.html", user=request.state.user_name, item=item)


@_page("/ui/entries/{entry_id:int}")
def entry_page(request: Request, entry_id: int):
    """D — one entry: the served status over its frontmatter (D12), items, text."""
    conn = db.connect()
    try:
        entry = ui_queries.entry_detail(conn, entry_id)
    finally:
        conn.close()
    if entry is None:
        return not_found(request)
    return render("entry.html", user=request.state.user_name, entry=entry)


USAGE_TABS = (("7d", "Last 7 days"), ("30d", "Last 30 days"), ("all", "All time"))


@_page("/ui/usage")
def usage_page(request: Request, since: str = "7d"):
    """E — the team's totals (``feedback.stats``). Person-free, like ``/api/stats``."""
    if since not in ui_queries.USAGE_WINDOWS:
        return not_found(request)
    conn = db.connect()
    try:
        stats = ui_queries.usage(conn, since)
    finally:
        conn.close()
    return render("usage.html", user=request.state.user_name, stats=stats, window=since,
                  windows=USAGE_TABS)
