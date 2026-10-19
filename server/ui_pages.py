"""Pages of the UI (D14; plan 2026-09-29, slices 3–5) and its writes (D20, DESIGN §6.4).

Everything here is reached through ``app._ui_session_gate`` (``GET``/``HEAD`` with a live
session, or ``POST`` on a route of ``WRITE_ROUTES``) except the public assets, which
``app.PUBLIC_PATHS`` lists one by one. The sign-in routes themselves (``/api/ui-login``,
``/ui/session``) live in ``server.app``, next to the audit and rate-limit helpers they
share with ``/api/enroll``.

Templates are Jinja2 with autoescape ON and ``StrictUndefined``: a value reaches a page
escaped unless it came out of ``ui_render.render_md``, the one producer of safe markup.
"""
from __future__ import annotations

import hmac
import re
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import jinja2
from fastapi import APIRouter, Query, Request
from markupsafe import Markup, escape
from starlette.responses import HTMLResponse, Response
from starlette.routing import compile_path

from server import auth_events, db, feedback, public_url, search, ui_auth, ui_queries, ui_render

UI_DIR = Path(__file__).with_name("ui")

# Every `/ui` response, public or not, refused or not (AC-8). No inline script or style
# is allowed, which is why every page links `ui.css` and the login page `login.js`.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    # Not `no-referrer` (D20, §6.3): under it a browser sends `Origin: null` on every form
    # POST, which §6.4's Origin check must refuse. `same-origin` still sends no `Referer`
    # to any other site.
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}
# Per-path replacements for the headers above, applied by
# `app.security_headers_middleware` (which overwrites whatever a route sets). `/ui/join`
# (D19) has no script and no form, so its policy allows no script, no connection and no
# form target, and it sends no `Referer` at all.
HEADER_OVERRIDES = {
    "/ui/join": {
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'none'; "
            "base-uri 'none'; frame-ancestors 'none'"
        ),
        "Referrer-Policy": "no-referrer",
    },
}

_ENV = jinja2.Environment(
    loader=jinja2.FileSystemLoader(UI_DIR / "templates"),
    autoescape=True,
    undefined=jinja2.StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)
_ENV.globals["render_md"] = ui_render.render_md


@jinja2.pass_context
def _csrf_field(context) -> Markup:
    """``{{ csrf_field() }}``: the one way a template puts the session's CSRF token in a
    form (D20). A form rendered for nobody fails loudly instead of posting no token."""
    token = context.get("csrf")
    if not token:
        raise RuntimeError("a form was rendered without the session's CSRF token")
    return Markup('<input type="hidden" name="csrf" value="{}">').format(escape(token))


_ENV.globals["csrf_field"] = _csrf_field

router = APIRouter()


def is_ui_path(path: str) -> bool:
    return path == "/ui" or path.startswith("/ui/")


def render(name: str, request: Request | None, *, status: int = 200,
           **context) -> HTMLResponse:
    """One template, rendered inside the shell (banner, navigation, who is signed in and
    the sign-out form). ``request`` is the signed-in request the session gate let
    through, or ``None`` for a page nobody is signed in to."""
    state = request.state if request is not None else None
    return HTMLResponse(
        _ENV.get_template(name).render(user=getattr(state, "user_name", None),
                                       role=getattr(state, "user_role", None),
                                       csrf=getattr(state, "csrf", None), **context),
        status_code=status)


def message(status: int, heading: str, text: str, *, request: Request | None = None,
            headers: dict[str, str] | None = None) -> HTMLResponse:
    response = render("message.html", request, status=status, heading=heading,
                      message=text)
    response.headers.update(headers or {})
    return response


def not_found(request: Request) -> HTMLResponse:
    return message(404, "Not found", "There is no such page, project, entry or item.",
                   request=request)


def _page(path: str):
    """A signed-in page: ``GET`` and ``HEAD`` (the gate refuses every other method)."""
    return router.api_route(path, methods=["GET", "HEAD"])


# --------------------------------------------------------------------------- #
# Writes (D20, DESIGN §6.4)                                                    #
# --------------------------------------------------------------------------- #
# The exact registry of write routes: the POST path templates the session gate lets
# through. `_write` is the only way in, so a route and its registration cannot drift.
WRITE_ROUTES: list[str] = []
_WRITE_PATTERNS: list[re.Pattern] = []
FORM_TYPE = "application/x-www-form-urlencoded"
ADMIN_PREFIX = "/ui/admin/"
# §6.4's refusal codes, each shown on its page next to its explanation.
WRITE_REFUSALS = {
    "method_not_allowed": "The UI is read-only except the listed POST routes",
    "public_url_unset": "This server has no public URL (LOOM_PUBLIC_URL), so the UI "
                        "accepts no write",
    "bad_origin": "The request did not come from this server's own pages",
    "bad_csrf": "The form's CSRF token is missing or wrong; reload the page",
    "forbidden": "Only an admin can see the users page or change an account, and only a "
                 "session opened by `loom ui` can make a change",
}
_REFUSAL_HEADINGS = {403: "Forbidden", 405: "Method not allowed"}


def refusal(status: int, code: str, *, request: Request | None = None) -> HTMLResponse:
    """The page of a §6.4 refusal: ``status``, and ``code`` named on it (never JSON)."""
    return message(status, _REFUSAL_HEADINGS[status], f"{WRITE_REFUSALS[code]} ({code}).",
                   request=request)


def _write(path: str):
    """A write: ``POST`` only, gated by ``write_refusal`` before the handler runs. The
    handler reads its fields from ``request.state.form`` (the parsed form body)."""
    WRITE_ROUTES.append(path)
    _WRITE_PATTERNS.append(compile_path(path)[0])
    return router.post(path)


def is_write_route(path: str) -> bool:
    return any(pattern.match(path) for pattern in _WRITE_PATTERNS)


def parse_form(content_type: str, body: bytes) -> dict[str, str]:
    """A urlencoded body's fields (the first value of each); any other body has none."""
    if content_type.split(";")[0].strip().lower() != FORM_TYPE:
        return {}
    fields = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    return {name: values[0] for name, values in fields.items()}


def _origin(url: str) -> str:
    """``scheme://host[:port]`` as a browser serialises an ``Origin`` (no default port)."""
    parts = urlsplit(url)
    scheme, host, port = parts.scheme.lower(), parts.hostname or "", parts.port
    if ":" in host:
        host = f"[{host}]"
    default = {"https": 443, "http": 80}.get(scheme)
    return f"{scheme}://{host}" + (f":{port}" if port and port != default else "")


def write_refusal(headers, form: dict[str, str], session: str, kind: str) -> str | None:
    """Why a signed-in write must be refused (a ``WRITE_REFUSALS`` code), or ``None``.

    In §6.4's order: no resolved public URL; then ``Origin``, which when present must be
    the public URL's origin (so ``Origin: null`` is refused, whatever ``Sec-Fetch-Site``
    says), and when absent leaves the call to ``Sec-Fetch-Site: same-origin`` (neither
    header is a refusal); then the ``csrf`` field; then the session's ``kind``.
    """
    base = public_url.resolve()
    if base is None:
        return "public_url_unset"
    origin = headers.get("origin")
    if origin is not None:
        if origin != _origin(base):
            return "bad_origin"
    elif headers.get("sec-fetch-site") != "same-origin":
        return "bad_origin"
    if not hmac.compare_digest(form.get("csrf", "").encode(),
                               ui_auth.csrf_token(session).encode()):
        return "bad_csrf"
    if kind not in ui_auth.WRITABLE_KINDS:
        return "forbidden"
    return None


class WriteRefused(Exception):
    """:func:`write_transaction` found the session changed since the gate let the write
    in; ``app`` answers it as the gate would (``status`` 401 or 403, ``outcome`` the
    gate's audit outcome or refusal code)."""

    def __init__(self, status: int, outcome: str):
        super().__init__(outcome)
        self.status, self.outcome = status, outcome


@contextmanager
def write_transaction(request: Request, conn):
    """Every write's ``BEGIN IMMEDIATE`` (D20, §6.4): the block runs under the write lock
    and is committed when it ends, rolled back when it raises.

    Before the block, the session is read again under that lock — row, expiry, the user's
    ``active`` and role, its device token — because the gate read it before the body
    arrived. A session that would not pass the gate now raises :class:`WriteRefused`
    (401), and so does a role that no longer opens a ``/ui/admin/`` route (403
    ``forbidden``); nothing has changed. ``request.state.user_role`` is updated to the
    role read here, so the block decides with it.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        session = ui_auth.reread_session(conn, request.state.ui_session_id)
        outcome = ui_auth.session_refusal(session, db.iso_now())
        if outcome is not None:
            raise WriteRefused(401, outcome)
        if request.url.path.startswith(ADMIN_PREFIX) and session["role"] != "admin":
            raise WriteRefused(403, "forbidden")
        request.state.user_role = session["role"]
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


# --------------------------------------------------------------------------- #
# Public assets (each one an entry of `app.PUBLIC_PATHS`)                      #
# --------------------------------------------------------------------------- #
_LOGIN_HTML = (UI_DIR / "login.html").read_text(encoding="utf-8")
_JOIN_HTML = (UI_DIR / "join.html").read_text(encoding="utf-8")
_STATIC = {
    name: (UI_DIR / "static" / name).read_text(encoding="utf-8")
    for name in ("login.js", "favicon.svg", "ui.css")
}


@router.get("/ui/login")
def login_page():
    """Static. The code is in the URL fragment; this request never carries it."""
    return HTMLResponse(_LOGIN_HTML)


@router.get("/ui/join")
def join_page():
    """Static (D19). The invitation link's code is in the fragment: this page never
    receives, redeems, looks up or renders it. It explains how to get to `loom enroll`."""
    return HTMLResponse(_JOIN_HTML)


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
    return render("home.html", request, projects=projects, pulse=pulse)


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
    if not db.is_row_id(project_id):
        return not_found(request)
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
        "project.html", request, project=proj, tab=tab, tabs=TABS,
        view=view, error=error, older_url=older_url, types=search.ITEM_TYPES,
        filtering=bool(item_type or author or q),
        filters={"type": item_type, "author": author, "q": q,
                 "show_replaced": bool(show_replaced),
                 "since_day": (since_floor or "")[:10]},
    )


@_page("/ui/items/{entry_id:int}.{ordinal:int}")
def item_page(request: Request, entry_id: int, ordinal: int):
    """C — one item: state, lineage, every link, provenance and NAMED feedback."""
    if not (db.is_row_id(entry_id) and db.is_row_id(ordinal)):
        return not_found(request)
    conn = db.connect()
    try:
        item = ui_queries.item_detail(conn, entry_id, ordinal)
    finally:
        conn.close()
    if item is None:
        return not_found(request)
    return render("item.html", request, item=item)


@_page("/ui/entries/{entry_id:int}")
def entry_page(request: Request, entry_id: int):
    """D — one entry: the served status over its frontmatter (D12), items, text."""
    if not db.is_row_id(entry_id):
        return not_found(request)
    conn = db.connect()
    try:
        entry = ui_queries.entry_detail(conn, entry_id)
    finally:
        conn.close()
    if entry is None:
        return not_found(request)
    return render("entry.html", request, entry=entry)


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
    return render("usage.html", request, stats=stats, window=since,
                  windows=USAGE_TABS)


@_write("/ui/signout")
def signout(request: Request):
    """Ends this session, and only this one: the row goes, and the cookie is cleared
    with the attributes ``/ui/session`` set it with."""
    conn = db.connect()
    try:
        with write_transaction(request, conn):
            if ui_auth.end_sessions(conn, session_id=request.state.ui_session_id):
                auth_events.record(conn, actor_user_id=request.state.user_id,
                                   event="ui_signout", target_type="user",
                                   target_id=request.state.user_id,
                                   client_ip=request.state.client_ip)
    finally:
        conn.close()
    response = message(200, "Signed out", "You are signed out of Loom. Run `loom ui` in a "
                       "terminal to sign in again.")
    response.delete_cookie(ui_auth.SESSION_COOKIE, path="/ui", secure=True, httponly=True,
                           samesite="strict")
    return response
