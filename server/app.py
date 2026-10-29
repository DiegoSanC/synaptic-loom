"""Synaptic Loom central HTTP API (FastAPI + SQLite stdlib).

Implements the normative contracts of DESIGN.md §6.2. Cross-cutting rules:
- Bearer auth on every route except `/api/health` (middleware) — an allow-list,
  so FastAPI's own `/docs`, `/redoc` and `/openapi.json` need a token too.
- Errors are always JSON `{"error": "<code>", "detail": "..."}`.
- `created_at` and `user_id` are ALWAYS server-side (any client value is ignored).
- The server re-normalizes every remote it receives and is the authority.
"""
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import HTMLResponse, JSONResponse, Response

from server import (auth, auth_events, client_ip, db, extracts, feedback, items, links, plugins,
                    public_url, recall, search, ui_auth, ui_pages, ui_render)
# Imported for their side effect: both register their routes on `ui_pages.router` (and
# their writes in `ui_pages.WRITE_ROUTES`), so they must be imported before
# `app.include_router(ui_pages.router)` below.
from server import ui_admin, ui_machines  # noqa: F401
from server.normalize import normalize
from server.rate_limit import RateLimiter

VERSION = "0.1.0"
HSTS_HEADER = "max-age=31536000; includeSubDomains"
# A child of Uvicorn's configured error logger so INFO audit lines reach stderr/Fly Logs.
SECURITY_LOG = logging.getLogger("uvicorn.error.loom.security")
SECURITY_LOG.setLevel(logging.INFO)
STARTUP_LOG = logging.getLogger("uvicorn.error.loom.startup")
STARTUP_LOG.setLevel(logging.INFO)
# Its own logger: the startup logger's one line names the client-IP mode (D17).
ORPHAN_LOG = logging.getLogger("uvicorn.error.loom.orphans")
ORPHAN_LOG.setLevel(logging.INFO)
# Its own logger too, for the same reason (D20).
PUBLIC_URL_LOG = logging.getLogger("uvicorn.error.loom.public_url")
# And for the same reason (D21).
PLUGINS_LOG = logging.getLogger("uvicorn.error.loom.plugins")
PLUGINS_LOG.setLevel(logging.INFO)
AUTH_FAILURE_LIMIT = 10
HEALTH_REQUEST_LIMIT = 120
RATE_LIMIT_WINDOW_SECONDS = 60
AUTH_FAILURES = RateLimiter(
    limit=AUTH_FAILURE_LIMIT,
    window_seconds=RATE_LIMIT_WINDOW_SECONDS,
)
HEALTH_REQUESTS = RateLimiter(
    limit=HEALTH_REQUEST_LIMIT,
    window_seconds=RATE_LIMIT_WINDOW_SECONDS,
)
# Replaced at startup from LOOM_CLIENT_IP_SOURCE (D17); the default until then.
CLIENT_IP_SOURCE = client_ip.parse(None)


def _resolve_client_ip_source() -> client_ip.ClientIpSource:
    """Read LOOM_CLIENT_IP_SOURCE once; an unknown value stops the process from starting."""
    try:
        source = client_ip.parse(os.environ.get(client_ip.ENV_VAR))
    except ValueError as exc:
        raise RuntimeError(str(exc)) from None
    STARTUP_LOG.info("client_ip_source mode=%s", source.mode)
    for warning in client_ip.startup_warnings(source, os.environ.get("FLY_APP_NAME")):
        STARTUP_LOG.warning("%s", warning)
    return source


def _log_orphan_count(conn) -> None:
    """Count, never revoke (D19): live device tokens no renewal chain reaches, left by
    the pre-D19 renew. Here and not in `init_db`, which every `admin.py` command runs."""
    count = len(auth.orphans(conn))
    if count:
        ORPHAN_LOG.warning("device_token_orphans count=%d: run `python -m server.admin "
                           "revoke-orphans` (RUNBOOK §2)", count)
    else:
        ORPHAN_LOG.info("device_token_orphans count=0")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global CLIENT_IP_SOURCE
    CLIENT_IP_SOURCE = _resolve_client_ip_source()
    # Ensure the schema on the configured DB at startup (idempotent).
    conn = db.init_db()
    try:
        _log_orphan_count(conn)
        auth_events.prune(conn)  # client_ip is personal data (D20, DESIGN §6.4)
    finally:
        conn.close()
    if public_url.resolve() is None:
        PUBLIC_URL_LOG.warning("public_url_unset: no LOOM_PUBLIC_URL (or FLY_APP_NAME), so "
                            "every /ui write is refused (D20)")
    PLUGINS_LOG.info("server_plugins names=%s", ",".join(PLUGINS) or "none")
    AUTH_FAILURES.clear()
    HEALTH_REQUESTS.clear()
    yield


app = FastAPI(title="Synaptic Loom API", version=VERSION, lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Response helpers                                                             #
# --------------------------------------------------------------------------- #
def _err(
    status: int,
    code: str,
    detail: str | None = None,
    *,
    headers: dict[str, str] | None = None,
    **extra,
) -> JSONResponse:
    body: dict = {"error": code}
    if detail is not None:
        body["detail"] = detail
    body.update(extra)
    response_headers = {"Strict-Transport-Security": HSTS_HEADER}
    response_headers.update(headers or {})
    return JSONResponse(status_code=status, content=body, headers=response_headers)


def _project_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "canonical_remote": row["canonical_remote"],
        "name": row["name"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        # D22: whether this project's entries keep their redacted extract. A client
        # sends one only when this is true, so a server without the key gets none.
        "store_extracts": bool(row["store_extracts"]),
    }


_PROJECT_SELECT = (
    "SELECT p.id, p.canonical_remote, p.name, p.created_at, p.store_extracts, "
    "u.name AS created_by "
    "FROM projects p LEFT JOIN users u ON u.id = p.created_by"
)


def _conflict(conn: sqlite3.Connection, project_id: int) -> JSONResponse:
    """409 `already_registered` body with the project and who registered it, and when."""
    row = conn.execute(_PROJECT_SELECT + " WHERE p.id = ?", (project_id,)).fetchone()
    return _err(
        409,
        "already_registered",
        project={"id": row["id"], "canonical_remote": row["canonical_remote"], "name": row["name"]},
        registered_by=row["created_by"],
        registered_at=row["created_at"],
    )


# --------------------------------------------------------------------------- #
# Auth (Bearer middleware)                                                      #
# --------------------------------------------------------------------------- #
def _lookup_credential(token: str) -> dict | None:
    conn = db.connect()
    try:
        return auth.lookup_bearer(conn, token)
    finally:
        conn.close()


# The ONLY paths this server answers without a credential. An allow-list, rather than a
# rule about path shapes, because the rule is what leaked. `/api/enroll` is the second
# entry (issue #20): the engineer redeeming a code has no credential yet. It is not free
# to probe — it shares the auth-failure window below, and every attempt is audited.
#
# The `/ui/` entries (D14) are the browser's way in: the login page, the script it loads,
# the stylesheet it and the signed-out page use, the icon (so no browser ever asks for
# `/favicon.ico`, which would count as a failed authentication) and `/ui/session`, where
# a login code is redeemed. Exact paths, like the rest: no static mount, no prefix.
# `/ui/join` (D19) is the static page an invitation link opens; its code stays in the
# fragment, which never reaches the server.
PUBLIC_PATHS = frozenset({
    "/api/health", "/api/enroll",
    "/ui/login", "/ui/session", "/ui/join", "/ui/static/login.js",
    "/ui/static/favicon.svg", "/ui/static/ui.css",
})
# Redeeming a secret anonymously: gated by the auth-failure window, not the health one.
_REDEMPTION_PATHS = frozenset({"/api/enroll", "/ui/session"})
_UI_METHODS = frozenset({"GET", "HEAD"})


def _client_ip(request: Request) -> str:
    """The validated client address per LOOM_CLIENT_IP_SOURCE (D17), else `unavailable`."""
    return CLIENT_IP_SOURCE.client_ip(request)


def _audit_auth(request: Request, outcome: str, **fields: object) -> None:
    """One structured security-audit line: outcome, UTC timestamp, validated client IP.

    ``fields`` are the few non-secret identifiers an outcome carries (a user id, a device
    token id). Credentials, codes and unvalidated header values never enter the log.
    """
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    extra = "".join(f" {key}={value}" for key, value in fields.items())
    SECURITY_LOG.info(
        "auth outcome=%s timestamp=%s client_ip=%s%s",
        outcome,
        timestamp,
        _client_ip(request),
        extra,
    )


def _reject_auth(
    request: Request,
    outcome: str,
    status: int,
    detail: str,
    *,
    code: str | None = None,
) -> JSONResponse:
    """Count one FAILED attempt against its address, or answer 429 if the window is full.

    The only door into the failure window (D17): every caller has already validated the
    credential and found it wanting, so a valid one never reaches here and never meets a
    full window. A full window records nothing more — the 429 is the whole answer.
    """
    retry_after = AUTH_FAILURES.record(_client_ip(request))
    if retry_after is not None:
        _audit_auth(request, "rate_limited")
        return _err(
            429,
            "rate_limited",
            "Too many failed authentication attempts",
            headers={"Retry-After": str(retry_after)},
        )
    _audit_auth(request, outcome)
    if code is None:
        code = "forbidden" if status == 403 else "unauthorized"
    return _err(status, code, detail)


def _reject_ui(request: Request, outcome: str) -> HTMLResponse:
    """The `/ui` twin of `_reject_auth`: same window, same audit line, an HTML body.

    One 401 for every reason, and for existing and non-existing pages alike, so an
    anonymous caller learns nothing about which pages exist.
    """
    retry_after = AUTH_FAILURES.record(_client_ip(request))
    if retry_after is not None:
        _audit_auth(request, "rate_limited")
        return ui_pages.message(429, "Too many attempts",
                                "Too many failed sign-in attempts from this address. "
                                "Wait a minute.",
                                headers={"Retry-After": str(retry_after)})
    _audit_auth(request, outcome)
    return ui_pages.message(401, "Signed out",
                            "Your Loom session has ended or was never opened. "
                            "Run `loom ui` in a terminal to sign in.")


def _lookup_ui_session(session: str) -> dict | None:
    conn = db.connect()
    try:
        return ui_auth.lookup_session(conn, session)
    finally:
        conn.close()


def _renew_ui_session(session: dict) -> int | None:
    conn = db.connect()
    try:
        return ui_auth.renew_session(conn, session)
    finally:
        conn.close()


async def _ui_session_gate(request: Request, call_next):
    """`/ui` pages: `GET`/`HEAD`, or `POST` on a listed write route; the `loom_ui` cookie
    only (D14, D20).

    Any other method, and a `POST` anywhere else, is refused before any credential is
    read, which keeps the UI read-only by construction outside `ui_pages.WRITE_ROUTES`.
    A `Bearer` header is never consulted here, and the cookie is never consulted on
    `/api/*`: the browser credential and the machine credential open different surfaces.
    A valid session passes even when its address's failure window is full (D17); only a
    refused one meets the 429. A write then passes `ui_pages.write_refusal` (public URL,
    origin, CSRF token, session kind, DESIGN §6.4) before its handler runs, and the handler
    re-reads the session under its own write lock (`ui_pages.write_transaction`). Every
    refusal is an HTML page.
    """
    write = request.method == "POST" and ui_pages.is_write_route(request.url.path)
    if request.method not in _UI_METHODS and not write:
        return ui_pages.refusal(405, "method_not_allowed")
    cookie = request.cookies.get(ui_auth.SESSION_COOKIE, "")
    if not cookie:
        return _reject_ui(request, "ui_no_session")
    session = await run_in_threadpool(_lookup_ui_session, cookie)
    outcome = ui_auth.session_refusal(session, db.iso_now())
    if outcome is not None:
        return _reject_ui(request, outcome)

    request.state.user_id = session["user_id"]
    request.state.user_name = session["name"]
    request.state.ui_session_id = session["ui_session_id"]
    request.state.user_role = session["role"]
    request.state.ui_device_token_id = session["device_token_id"]
    request.state.csrf = ui_auth.csrf_token(cookie)
    request.state.client_ip = _client_ip(request)
    if write:
        request.state.form = ui_pages.parse_form(request.headers.get("content-type", ""),
                                                 await request.body())
        refusal = ui_pages.write_refusal(request.headers, request.state.form, cookie,
                                         session["kind"])
        if refusal is not None:
            _audit_auth(request, f"ui_write_{refusal}", user_id=session["user_id"],
                        ui_session_id=session["ui_session_id"])
            return ui_pages.refusal(403, refusal, request=request)
        return await call_next(request)
    # Reading slides the session (§6.3); a write never does, so sign-out's cleared cookie
    # is the only Set-Cookie it answers with.
    max_age = await run_in_threadpool(_renew_ui_session, session)
    response = await call_next(request)
    if max_age is not None:
        response.set_cookie(ui_auth.SESSION_COOKIE, cookie, max_age=max_age, path="/ui",
                            secure=True, httponly=True, samesite="strict")
    return response


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Bearer on everything except `PUBLIC_PATHS` — an ALLOW-list, not a deny-list.

    This check used to read `if not path.startswith("/api/") or path == "/api/health"`,
    which exempted `/api/health` *and everything not shaped like `/api/*`*. `app` is
    constructed with no `docs_url`/`redoc_url`/`openapi_url` override, so FastAPI's
    defaults are live and `/docs`, `/redoc` and `/openapi.json` fell through that second
    clause unauthenticated. On loopback nobody but the developer could reach them; on the
    public internet it publishes the whole API schema — every route, field, validator and
    vocabulary — plus a working "Try it out" console, to any anonymous visitor.

    Nulling the three doc URLs would fix today's symptom. Inverting the condition fixes the
    class: the next non-`/api/` route anyone adds (a favicon, a static mount, a websocket)
    is now protected by default instead of exposed by default. An unknown path answers 401
    rather than 404 for the same reason — an anonymous caller learns nothing about which
    routes exist.
    """
    path = request.url.path
    if path in _REDEMPTION_PATHS:
        # Anonymous by necessity, and a failed redemption is a failed authentication: the
        # handler counts it through `_reject_auth`, which answers 429 once the window is
        # full. A valid code is redeemed whatever the window says (D17).
        return await call_next(request)
    if path in PUBLIC_PATHS:
        retry_after = HEALTH_REQUESTS.record(_client_ip(request))
        if retry_after is not None:
            return _err(
                429,
                "rate_limited",
                "Too many health requests",
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)
    if path == "/ui" or path.startswith("/ui/"):
        # Not an exemption by path shape: every other `/ui` path needs a DIFFERENT
        # credential (the session cookie), and never the bearer (D14).
        return await _ui_session_gate(request, call_next)

    # The credential is validated BEFORE the failure window is consulted (D17): a full
    # window used to answer 429 here first, so ten bad guesses from one address — or from
    # every client at once, when they all resolve to `unavailable` — locked out every
    # valid token behind it. Each request in a flood now costs one token-hash lookup.
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        return _reject_auth(
            request,
            "missing_or_malformed_header",
            401,
            "Missing or malformed Authorization header",
        )
    token = header[len("Bearer "):].strip()
    if not token:
        return _reject_auth(request, "empty_token", 401, "Empty bearer token")

    cred = await run_in_threadpool(_lookup_credential, token)
    if cred is None:
        return _reject_auth(request, "invalid_token", 401, "Invalid token")
    # `active` first (D19): `deactivate` also revokes every chain of the user, and the
    # outcome must still say deactivated_user (403), not revoked_token.
    if not cred["active"]:
        return _reject_auth(request, "deactivated_user", 403, "User is deactivated")
    if cred["device_id"] is not None:
        # Distinct outcomes so an operator reading Fly Logs can tell an expiry (the
        # client should have renewed) from a revocation (deliberate) from a typo.
        if cred["revoked_at"] is not None:
            return _reject_auth(request, "revoked_token", 401, "Device token revoked")
        if cred["expires_at"] <= db.iso_now():
            return _reject_auth(request, "expired_token", 401, "Device token expired")

    request.state.user_id = cred["user_id"]
    request.state.user_name = cred["name"]
    request.state.device_token_id = cred["device_id"]
    return await call_next(request)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["Strict-Transport-Security"] = HSTS_HEADER
    if ui_pages.is_ui_path(request.url.path):
        # Outermost middleware, so it also dresses the gate's own 401/405/429 (AC-8).
        response.headers.update(ui_pages.SECURITY_HEADERS)
        response.headers.update(ui_pages.HEADER_OVERRIDES.get(request.url.path, {}))
    return response


# --------------------------------------------------------------------------- #
# Error handlers (uniform {"error","detail"} shape)                             #
# --------------------------------------------------------------------------- #
@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    detail = "; ".join(
        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
    )
    return _err(422, "validation_error", detail or "Invalid request body")


@app.exception_handler(ui_pages.WriteRefused)
async def _write_refused_handler(request: Request, exc: ui_pages.WriteRefused):
    """A write whose session changed after the gate let it in (§6.4) is refused as the
    gate would refuse it: the same 401 (counted, audited), or `403 forbidden`."""
    if exc.status == 401:
        return _reject_ui(request, exc.outcome)
    return ui_pages.refusal(exc.status, exc.outcome, request=request)


@app.exception_handler(StarletteHTTPException)
async def _http_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404 and ui_pages.is_ui_path(request.url.path):
        return ui_pages.not_found(request)
    if exc.status_code == 405 and ui_pages.is_ui_path(request.url.path):
        return ui_pages.refusal(405, "method_not_allowed", request=request)
    codes = {404: "not_found", 405: "method_not_allowed", 401: "unauthorized", 403: "forbidden"}
    return _err(exc.status_code, codes.get(exc.status_code, "error"), str(exc.detail))


@app.exception_handler(Exception)
async def _unexpected_handler(request: Request, exc: Exception):
    """Last resort: an unforeseen fault is still `{"error","detail"}`, never bare text.

    Without this, Starlette answers `Internal Server Error` as plain text, and every
    client in this system reads the error CODE out of a JSON body (`_print_http_error`
    prints "HTTP 500" and nothing else, so the operator learns nothing). The body says
    only that the request failed: the exception message can carry a query, a path or a
    row of content, and this is the one response nobody audited in advance. Starlette
    re-raises after this handler, so uvicorn still logs the traceback server-side.
    """
    return _err(500, "internal_error", "The server could not complete the request.")


# --------------------------------------------------------------------------- #
# Request models                                                               #
# --------------------------------------------------------------------------- #
class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    remote_url: str | None = None
    path: str | None = None
    hostname: str | None = None
    name: str | None = None


class ItemIn(BaseModel):
    """One element of ``POST /api/entries`` ``items`` (D13). The RULES (vocabulary,
    non-empty, substring of ``content_md``, at most 50) are checked by the handler so
    that every violation is the same ``422 invalid_items``; the model checks shapes."""

    model_config = ConfigDict(extra="ignore")
    item_type: str
    labelled: StrictBool
    text_md: str


class EntryCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    project_remote: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    content_md: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)
    covered_from: str | None = None
    covered_to: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    # Optional and additive (t-entry-item-types): a client that does not send it
    # produces exactly the request — and the rows — it produced before Block T.
    item_types: list[str] | None = None
    # Optional and additive (D13, t-entry-items), the same way: absent → no item rows,
    # and every reader treats the entry as a whole, as before.
    items: list[ItemIn] | None = None
    # Optional and additive (D22): the redacted text the summary was written from.
    # Stored next to the entry when the project keeps extracts; never indexed.
    extract: str | None = Field(default=None, min_length=1, max_length=extracts.MAX_CHARS)

    @field_validator("item_types")
    @classmethod
    def _closed_vocabulary(cls, value: list[str] | None) -> list[str] | None:
        """Reject anything outside ITEM_TYPES → 422 (never a stored free-text label).

        The message names the vocabulary, never the offending value: the error body
        is echoed back to the caller and there is nothing to gain from reflecting a
        payload into it.
        """
        if value is not None and any(item not in search.ITEM_TYPES for item in value):
            raise ValueError(
                "unknown item type; allowed: " + ", ".join(search.ITEM_TYPES)
            )
        return value


# Longest rationale a link may carry. The field is free text written by whoever posts
# the link (in practice: a one-line justification the curator's deterministic script
# copied out of an LLM verdict), it is stored forever and it is echoed back on every
# listing, so it gets a bound like every other unbounded input on this surface.
MAX_RATIONALE_LEN = 2000


class FeedbackCreate(BaseModel):
    """``POST /api/feedback`` body (slice F, D13): one verdict on one item, ``"17.2"``."""

    model_config = ConfigDict(extra="ignore")
    project_id: int
    item: str
    verdict: str
    note: str = Field(max_length=feedback.MAX_NOTE_LEN)

    @field_validator("item")
    @classmethod
    def _item_id_shape(cls, value: str) -> str:
        if not items.ITEM_ID_RE.fullmatch(value):
            raise ValueError("'item' must be an item id, '<entry>.<n>' (e.g. 17.2)")
        return value

    @field_validator("verdict")
    @classmethod
    def _known_verdict(cls, value: str) -> str:
        """Closed vocabulary → 422, naming the vocabulary and never the value (the
        `kind` rule of `LinkCreate`)."""
        if value not in feedback.VERDICTS:
            raise ValueError("unknown verdict; allowed: " + ", ".join(feedback.VERDICTS))
        return value

    @field_validator("note")
    @classmethod
    def _one_line(cls, value: str) -> str:
        if not value.strip() or "\n" in value or "\r" in value:
            raise ValueError("'note' must be one non-empty line")
        return value


class LinkCreate(BaseModel):
    """``POST /api/links`` body (t-entry-links). ``from_entry`` is the NEWER entry."""

    model_config = ConfigDict(extra="ignore")
    from_entry: int
    to_entry: int
    kind: str
    rationale: str | None = Field(default=None, max_length=MAX_RATIONALE_LEN)

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        """Closed vocabulary → 422. The message names the vocabulary, never the value.

        Same rule (and the same reason) as `item_types`: the error body is echoed back
        to the caller and there is nothing to gain from reflecting a payload into it.
        """
        if value not in links.LINK_KINDS:
            raise ValueError("unknown kind; allowed: " + ", ".join(links.LINK_KINDS))
        return value

    @model_validator(mode="after")
    def _not_a_self_link(self) -> "LinkCreate":
        """An entry can neither supersede nor duplicate itself (→ 422).

        Rejected in the model rather than in the handler so it cannot be reached by a
        future caller that skips the endpoint: a self-link would make the entry's own
        derived status permanently `superseded`, i.e. invisible in `loom log`, with no
        other entry to point the reader at.
        """
        if self.from_entry == self.to_entry:
            raise ValueError("a link must join two DIFFERENT entries")
        return self


class ItemLinkCreate(BaseModel):
    """``POST /api/item-links`` body (slice E, D13): ``from`` is an item of the NEWER
    entry. Both are public item ids, ``"<entry>.<n>"``."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    from_item: str = Field(alias="from")
    to_item: str = Field(alias="to")
    kind: str
    rationale: str | None = Field(default=None, max_length=MAX_RATIONALE_LEN)

    @field_validator("from_item", "to_item")
    @classmethod
    def _item_id_shape(cls, value: str) -> str:
        if not items.ITEM_ID_RE.fullmatch(value):
            raise ValueError("an item id is '<entry>.<n>' (e.g. 17.2)")
        return value

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        """Closed vocabulary → 422, naming the vocabulary and never the value (the rule
        of `LinkCreate`)."""
        if value not in links.LINK_KINDS:
            raise ValueError("unknown kind; allowed: " + ", ".join(links.LINK_KINDS))
        return value


class ItemLinkRevoke(BaseModel):
    """``POST /api/item-links/{id}/revoke`` body: why the link is taken back."""

    model_config = ConfigDict(extra="ignore")
    reason: str = Field(max_length=items.MAX_REVOKE_REASON_LEN)

    @field_validator("reason")
    @classmethod
    def _one_line(cls, value: str) -> str:
        if not value.strip() or "\n" in value or "\r" in value:
            raise ValueError("'reason' must be one non-empty line")
        return value


# --------------------------------------------------------------------------- #
# Routes                                                                       #
# --------------------------------------------------------------------------- #
class EnrolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=256)
    device_name: str = Field(min_length=1, max_length=128)


@app.post("/api/enroll", status_code=201)
def enroll(payload: EnrolRequest, request: Request):
    """Redeem a single-use enrolment code for THIS machine's device token (issue #20).

    Anonymous (see `PUBLIC_PATHS`). Unknown, expired, already-redeemed and
    deactivated-user codes are one `401 invalid_code`: the caller learns nothing, and
    the attempt counts against the address's failure window exactly like a bad bearer.

    A re-enrolling machine may send its current token as `Authorization: Bearer` (D19
    B2), and `redeem` revokes that token's chain when it is the code's user's. The
    middleware never reads the header on this path, and nothing here rejects on it: a
    header that proves nothing is ignored, never a 401 and never a counted failure.
    """
    header = request.headers.get("authorization", "")
    old_token = header[len("Bearer "):].strip() if header.startswith("Bearer ") else None
    conn = db.connect()
    try:
        result = auth.redeem(conn, payload.code, payload.device_name, old_token or None)
    finally:
        conn.close()
    if result is None:
        return _reject_auth(
            request, "enrol_invalid_code", 401,
            "Invalid, expired or already redeemed enrolment code", code="invalid_code",
        )
    device_token_id = result.pop("device_token_id")
    _audit_auth(request, "enrol_redeemed", user_id=result["user"]["id"],
                device_token_id=device_token_id)
    return result


_RENEW_REFUSALS = {
    "deactivated_user": (403, "User is deactivated"),
    "revoked_token": (401, "Device token revoked"),
    "expired_token": (401, "Device token expired"),
}


@app.post("/api/device-tokens/renew", status_code=201)
def renew_device_token(request: Request):
    """Mint the successor of the calling device token; the old one keeps its own expiry.

    The client calls this silently, inside a pass it was already running, once less
    than a quarter of the token's life remains. A legacy `adduser` token has nothing to
    renew (it never expires) and is told so.
    """
    device_id = request.state.device_token_id
    if device_id is None:
        return _err(403, "not_a_device_token",
                    "Only a device token minted by enrolment can be renewed")
    conn = db.connect()
    try:
        result = auth.renew(conn, device_id)
    except auth.RenewRefused as refused:
        # The middleware accepted the bearer, then a revoke or a deactivation committed
        # before renew took the write lock (D19): answer as the middleware now would.
        status, detail = _RENEW_REFUSALS[refused.outcome]
        return _reject_auth(request, refused.outcome, status, detail)
    finally:
        conn.close()
    new_id = result.pop("device_token_id")
    _audit_auth(request, "device_token_renewed", user_id=result["user"]["id"],
                device_token_id=device_id, new_device_token_id=new_id)
    return result


def _device_field(device_token_id: int | None) -> dict:
    """The audit field for a device token id; a legacy bearer has none to name."""
    return {} if device_token_id is None else {"device_token_id": device_token_id}


@app.post("/api/ui-login", status_code=201)
def ui_login(request: Request):
    """Mint a single-use, 120 s login code for the caller's browser (D14).

    `loom ui` calls this with the machine's bearer and opens `/ui/login#code=…`. The
    code is tied to the calling device token, so revoking the device refuses the code
    and ends every session it opened.
    """
    conn = db.connect()
    try:
        code, expires_at = ui_auth.issue_login_code(
            conn, request.state.user_id, request.state.device_token_id)
    finally:
        conn.close()
    _audit_auth(request, "ui_login_code_issued", user_id=request.state.user_id,
                **_device_field(request.state.device_token_id))
    return {"code": code, "expires_at": expires_at}


@app.get("/api/me")
def me(request: Request):
    """Who the bearer is, and the account events about them (D20, §6.4).

    `device_id` is the calling device token, `None` for a permanent token. This is how a
    user learns that an admin changed their role or revoked one of their machines.
    """
    conn = db.connect()
    try:
        user = conn.execute("SELECT name, email, role FROM users WHERE id = ?",
                            (request.state.user_id,)).fetchone()
        events = auth_events.about_user(conn, request.state.user_id)
    finally:
        conn.close()
    return {"name": user["name"], "email": user["email"], "role": user["role"],
            "device_id": request.state.device_token_id, "events": events}


class UiSessionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    code: str = Field(min_length=1, max_length=256)


@app.post("/ui/session", status_code=204)
def open_ui_session(payload: UiSessionRequest, request: Request):
    """Redeem a login code for the `loom_ui` session cookie (D14). Anonymous.

    Unknown, expired, redeemed, deactivated-user and revoked-device codes are one
    `401 invalid_code`, counted against the address's failure window like a bad bearer.
    """
    conn = db.connect()
    try:
        result = ui_auth.open_session(conn, payload.code, client_ip=_client_ip(request))
    finally:
        conn.close()
    if result is None:
        return _reject_auth(
            request, "ui_login_invalid_code", 401,
            "Invalid, expired or already used login code", code="invalid_code",
        )
    _audit_auth(request, "ui_session_opened", user_id=result["user_id"],
                **_device_field(result["device_token_id"]),
                ui_session_id=result["ui_session_id"])
    response = Response(status_code=204)
    response.set_cookie(ui_auth.SESSION_COOKIE, result["session"], max_age=result["max_age"],
                        path="/ui", secure=True, httponly=True, samesite="strict")
    return response


app.include_router(ui_pages.router)  # with `ui_machines`' and `ui_admin`'s routes


@app.get("/api/health")
def health():
    return {"status": "ok", "version": VERSION}


@app.post("/api/projects")
def create_project(payload: ProjectCreate, request: Request):
    uid = request.state.user_id
    conn = db.connect()
    try:
        if payload.remote_url:
            canonical = normalize(payload.remote_url)
            existing = conn.execute(
                "SELECT id FROM projects WHERE canonical_remote = ?", (canonical,)
            ).fetchone()
            if existing:
                return _conflict(conn, existing["id"])
            name = payload.name or canonical.rsplit("/", 1)[-1]
            try:
                cur = conn.execute(
                    "INSERT INTO projects (canonical_remote, name, created_by, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (canonical, name, uid, db.iso_now()),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                # Race: another registration created the same remote between check and insert.
                conn.rollback()
                existing = conn.execute(
                    "SELECT id FROM projects WHERE canonical_remote = ?", (canonical,)
                ).fetchone()
                return _conflict(conn, existing["id"])
            return JSONResponse(
                status_code=201,
                # D22: a new project keeps extracts (the column's default).
                content={"id": cur.lastrowid, "canonical_remote": canonical, "name": name,
                         "store_extracts": True},
            )

        if payload.path and payload.hostname:
            existing = conn.execute(
                "SELECT project_id FROM project_paths WHERE user_id = ? AND hostname = ? AND path = ?",
                (uid, payload.hostname, payload.path),
            ).fetchone()
            if existing:
                return _conflict(conn, existing["project_id"])
            name = payload.name or os.path.basename(payload.path.rstrip("/")) or payload.path
            try:
                cur = conn.execute(
                    "INSERT INTO projects (canonical_remote, name, created_by, created_at) "
                    "VALUES (NULL, ?, ?, ?)",
                    (name, uid, db.iso_now()),
                )
                pid = cur.lastrowid
                conn.execute(
                    "INSERT INTO project_paths (project_id, user_id, path, hostname, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (pid, uid, payload.path, payload.hostname, db.iso_now()),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                # UNIQUE(user_id, hostname, path): the path was already registered (race).
                conn.rollback()
                existing = conn.execute(
                    "SELECT project_id FROM project_paths WHERE user_id = ? AND hostname = ? AND path = ?",
                    (uid, payload.hostname, payload.path),
                ).fetchone()
                return _conflict(conn, existing["project_id"])
            return JSONResponse(
                status_code=201,
                content={"id": pid, "canonical_remote": None, "name": name},
            )

        return _err(422, "validation_error", "Provide either 'remote_url' or ('path' and 'hostname')")
    finally:
        conn.close()


@app.get("/api/projects")
def get_projects(request: Request, remote: str | None = None):
    conn = db.connect()
    try:
        if remote is not None:
            canonical = normalize(remote)
            row = conn.execute(
                _PROJECT_SELECT + " WHERE p.canonical_remote = ?", (canonical,)
            ).fetchone()
            if row is None:
                return _err(404, "project_not_found", f"No project registered for remote '{canonical}'")
            return {"project": _project_dict(row)}
        rows = conn.execute(_PROJECT_SELECT + " ORDER BY p.id").fetchall()
        return {"projects": [_project_dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/api/entries")
def create_entry(payload: EntryCreate, request: Request):
    uid = request.state.user_id
    canonical = normalize(payload.project_remote)
    entry_items = None
    item_types = payload.item_types
    if payload.items is not None:
        entry_items = [item.model_dump() for item in payload.items]
        try:
            item_types = items.validate(payload.content_md, entry_items, payload.item_types)
        except items.InvalidItems as exc:
            return _err(422, "invalid_items", str(exc))
    conn = db.connect()
    try:
        proj = conn.execute(
            "SELECT id, store_extracts FROM projects WHERE canonical_remote = ?", (canonical,)
        ).fetchone()
        if proj is None:
            return _err(404, "project_not_found", f"No project registered for remote '{canonical}'")
        # D22: a project switched off keeps the entry and drops the extract.
        store_extract = payload.extract is not None and bool(proj["store_extracts"])
        try:
            created_at = db.iso_now()
            cur = conn.execute(
                "INSERT INTO entries (project_id, user_id, session_id, covered_from, covered_to, "
                "content_md, content_hash, model, prompt_version, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    proj["id"], uid, payload.session_id, payload.covered_from, payload.covered_to,
                    payload.content_md, payload.content_hash, payload.model, payload.prompt_version,
                    created_at,
                ),
            )
            # Same transaction as the entry: a stored entry always carries exactly the
            # labels it was posted with, or none. The vocabulary was already validated
            # by the model; duplicates in the payload collapse into one row.
            for item_type in sorted(set(item_types or ())):
                conn.execute(
                    "INSERT INTO entry_item_types (entry_id, item_type) VALUES (?, ?)",
                    (cur.lastrowid, item_type),
                )
            if entry_items:
                items.insert(conn, cur.lastrowid, entry_items)
            if store_extract:
                extracts.insert(conn, cur.lastrowid, payload.extract, created_at)
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            if (getattr(exc, "sqlite_errorname", "") != "SQLITE_CONSTRAINT_UNIQUE"
                    or "content_hash" not in str(exc)):
                # Follow-up F5: a CHECK, FK or any other UNIQUE violation arrives as the
                # SAME exception class, and answering it `409 duplicate_entry` is read
                # by the processor as "already stored" — it advances its pointer and
                # drops the marker, so the entry is lost for good. Only the entry's own
                # content_hash is a duplicate; anything else is a fault (→ 500).
                raise
            return _err(409, "duplicate_entry", "An entry with this content_hash already exists")
        return JSONResponse(status_code=201,
                            content={"id": cur.lastrowid, "extract_stored": store_extract})
    finally:
        conn.close()


@app.get("/api/entries/{entry_id}/extract")
def get_entry_extract(entry_id: int):
    """``GET /api/entries/{id}/extract`` (D22) — 200 · 404 ``extract_not_found``.

    The redacted text the entry was summarized from. An unknown entry, and one stored
    without an extract, answer the same 404.
    """
    conn = db.connect()
    try:
        found = extracts.fetch(conn, entry_id) if db.is_row_id(entry_id) else None
    finally:
        conn.close()
    if found is None:
        return _err(404, "extract_not_found", f"No stored extract for entry {entry_id}")
    return found


@app.post("/api/links")
def create_link(payload: LinkCreate, request: Request):
    """``POST /api/links`` (t-entry-links) — 201 {id} · 409 · 404 · 422.

    The ONLY way an entry's state changes in this system: a new row here, never an
    UPDATE on `entries` (D7). Both endpoints are reached, so both are re-derived from
    the database rather than trusted from the body — the caller names two entry ids and
    the server decides whether they exist and whether they belong to the same project.

    Authenticated like every other `/api/` route (the bearer middleware); the caller's
    identity is deliberately NOT recorded, because the frozen `entry_links` DDL has no
    author column and a link is a statement about two entries, not about a person.

    `from_entry` MUST be the newer entry — id-wise, which on this surface IS "newer":
    every id comes from this one server's `entries` table, ids are monotonic and D7
    means they are never reused. Enforcing it here makes a cycle unrepresentable, and
    a cycle is not a theoretical worry: posting `1 supersedes 2` on top of an existing
    `2 supersedes 1` marks BOTH entries superseded, and the default `loom log` then
    prints no entry body at all. The client's candidate pool already only offers older
    entries (`curate._candidates_for`), so this rejects nothing a curation pass builds.

    The contradictory PAIR is rejected too. `kind` is part of the UNIQUE triple, so
    `5 supersedes 4` and `5 duplicates 4` are two different rows and both would be
    accepted — which is exactly what two machines curating the same project produce
    (`curate.lock` is host-local), and it asserts two incompatible things about one
    pair of entries. The second one answers 409: from the poster's point of view the
    pair IS already linked, which is what 409 means here, and a 409-tolerant curator
    treats it as a fact already recorded rather than as a fault.
    """
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id, project_id FROM entries WHERE id IN (?, ?)",
            (payload.from_entry, payload.to_entry),
        ).fetchall()
        project_of = {row["id"]: row["project_id"] for row in rows}
        if payload.from_entry not in project_of or payload.to_entry not in project_of:
            return _err(404, "entry_not_found",
                        "Both 'from_entry' and 'to_entry' must be existing entries")
        if project_of[payload.from_entry] != project_of[payload.to_entry]:
            # A cross-project link would make one team's logbook able to collapse
            # another team's entry out of `loom log`.
            return _err(422, "validation_error",
                        "A link must join two entries of the SAME project")
        if payload.from_entry < payload.to_entry:
            return _err(422, "validation_error",
                        "'from_entry' must be the NEWER entry (a higher id than "
                        "'to_entry')")
        # IMMEDIATE, so the contradiction check and the insert are ONE atomic step: two
        # machines posting the two opposite kinds concurrently would otherwise both read
        # "no row yet" and both insert.
        conn.execute("BEGIN IMMEDIATE")
        try:
            clash = conn.execute(
                "SELECT kind FROM entry_links WHERE from_entry = ? AND to_entry = ? "
                "AND kind <> ?",
                (payload.from_entry, payload.to_entry, payload.kind),
            ).fetchone()
            if clash is not None:
                conn.rollback()
                return _err(409, "contradictory_link",
                            "These two entries are already linked with a different "
                            "kind; one pair of entries states one relation")
            cur = conn.execute(
                "INSERT INTO entry_links (from_entry, to_entry, kind, rationale, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (payload.from_entry, payload.to_entry, payload.kind, payload.rationale,
                 db.iso_now()),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            if getattr(exc, "sqlite_errorname", "") != "SQLITE_CONSTRAINT_UNIQUE":
                # A CHECK or FK violation arrives as the SAME exception class, and
                # answering 409 for it would be read as "already linked" by every
                # 409-tolerant caller: `loom curate` would count the link as posted and
                # advance its watermark over a link that does not exist. Only the UNIQUE
                # triple is a duplicate; anything else is a fault and must surface as
                # a 500 (`_unexpected_handler`).
                raise
            # UNIQUE(from_entry, to_entry, kind): the link is already there. This is what
            # makes the curator's 409-tolerant posting (and therefore its reruns)
            # idempotent, so it is a normal outcome, not a fault.
            return _err(409, "duplicate_link", "This link already exists")
        return JSONResponse(status_code=201, content={"id": cur.lastrowid})
    finally:
        conn.close()


@app.post("/api/item-links")
def create_item_link(payload: ItemLinkCreate, request: Request):
    """``POST /api/item-links`` (slice E, D13) — 201 {id} · 404 · 422 · 409.

    The item-grained counterpart of ``/api/links``, and deliberately a separate route:
    an old server would silently read an item-qualified body sent to ``/api/links`` as a
    permanent whole-entry link. An item link changes the status of ONE item; the
    entries' own status stays derived from ``entry_links`` only. The author is the
    caller (``request.state``), never the body.
    """
    conn = db.connect()
    try:
        new_id = items.create_link(conn, from_id=payload.from_item, to_id=payload.to_item,
                                   kind=payload.kind, rationale=payload.rationale,
                                   user_id=request.state.user_id, created_at=db.iso_now())
    except items.LinkRefused as exc:
        return _err(exc.status, exc.code, exc.detail)
    finally:
        conn.close()
    return JSONResponse(status_code=201, content={"id": new_id})


@app.post("/api/item-links/{link_id}/revoke")
def revoke_item_link(link_id: int, payload: ItemLinkRevoke, request: Request):
    """``POST /api/item-links/{id}/revoke`` (slice E) — 200 · 404 · 409 · 422.

    A wrong link is taken back, not deleted: the row keeps who made it, who revoked it
    and why (#12), and it stops counting. The same link may then be asserted again.
    """
    conn = db.connect()
    try:
        return items.revoke_link(conn, link_id, user_id=request.state.user_id,
                                 reason=payload.reason, revoked_at=db.iso_now())
    except items.LinkRefused as exc:
        return _err(exc.status, exc.code, exc.detail)
    finally:
        conn.close()


@app.post("/api/feedback")
def create_feedback(payload: FeedbackCreate, request: Request):
    """``POST /api/feedback`` (slice F, D13) — 201 {id} · 409 · 404 · 422.

    One person's verdict on one ITEM. The item is resolved here and its project checked
    against the body, so a caller cannot file feedback on another project's logbook.
    An un-itemized entry has no items and answers ``404 item_not_found`` like any
    unknown id. The author is the caller (``request.state``), never the body.
    """
    conn = db.connect()
    try:
        found = items.find_item(conn, payload.item)
        if found is None:
            return _err(404, "item_not_found",
                        f"No item {payload.item}: the entry does not exist or has no items")
        item_id, project_id = found
        if project_id != payload.project_id:
            return _err(422, "item_not_in_project",
                        f"Item {payload.item} belongs to another project")
        try:
            new_id = feedback.insert(conn, user_id=request.state.user_id,
                                     project_id=project_id, item_id=item_id,
                                     verdict=payload.verdict, note=payload.note,
                                     created_at=db.iso_now())
        except feedback.Duplicate:
            return _err(409, "duplicate_feedback", "This feedback is already recorded")
        return JSONResponse(status_code=201, content={"id": new_id})
    finally:
        conn.close()


@app.get("/api/stats")
def get_stats(since: str | None = None):
    """``GET /api/stats[?since=<ISO>]`` (slice F) — the pilot's team totals: recalls by
    source, feedback by verdict, the ten most useful items. No field names a person."""
    floor = None
    if since is not None:
        floor = feedback.parse_since(since)
        if floor is None:
            return _err(422, "invalid_since", "'since' must be an ISO-8601 date or time")
    conn = db.connect()
    try:
        return feedback.stats(conn, floor)
    finally:
        conn.close()


def _entry_dict(row: sqlite3.Row, *, ranked: bool = False) -> dict:
    """The seven frozen entry fields, plus the two ranking fields on a `q=` hit.

    `snippet`/`rank` are ADDITIVE and only ever appear on a search response: a client
    that lists without `q=` must keep receiving byte-identical objects (a reader that
    iterates the keys — the Claude-facing skills do — would otherwise see the shape
    change under it).
    """
    entry = {
        "id": row["id"],
        "author": row["author"],
        "session_id": row["session_id"],
        "created_at": row["created_at"],
        "covered_from": row["covered_from"],
        "covered_to": row["covered_to"],
        "content_md": row["content_md"],
    }
    if ranked:
        entry["snippet"] = row["snippet"]
        entry["rank"] = row["rank"]
    return entry


def _with_links(conn: sqlite3.Connection, entries: list[dict]) -> list[dict]:
    """Adds `links_in`/`links_out`/`status` (t-entry-links) to EVERY entry.

    ONE query for the whole page, never one per entry. All three keys are present on
    every entry — `"current"` and two empty lists when nothing links it — because that
    is what the frozen contract says ("entry GET responses gain …") and because
    `status` is the entity's own state, which every entry has.

    NOT the `snippet`/`rank` rule, deliberately. Those are query metadata: they exist
    only on a ranked hit and there is nothing to say about them on a plain listing.
    Emitting `status` only for linked entries instead makes the natural consumer line
    `entry.get("status") == "current"` come out FALSE for every un-curated entry, with
    no exception and no log line — the digest builder that reads it that way concludes
    it covers nothing. A silent, asymmetric failure is not a compatible default.

    `status` is computed HERE, on every read, from the link rows — the Q-3.5 resolution.
    There is no column to migrate, nothing to keep in step, and deleting a link
    restores the previous status by construction.
    """
    if not entries:
        return entries
    by_entry = links.links_for_entries(conn, [entry["id"] for entry in entries])
    for entry in entries:
        entry.update(by_entry.get(entry["id"]) or {
            "links_in": [], "links_out": [], "status": links.STATUS_CURRENT})
    return entries


def _parse_entry_ids(raw: str | None) -> list[int] | None:
    """``"17,21,4"`` → ``[17, 21, 4]`` (deduplicated, order kept); ``None`` if malformed.

    Strict on purpose: a bare decimal id per element, 1..MAX_ENTRY_IDS of them. A lax
    parse (spaces, signs, empty elements) would make two spellings of one request.
    """
    if not raw:
        return None
    parts = raw.split(",")
    if len(parts) > items.MAX_ENTRY_IDS or not all(
            part.isascii() and part.isdigit() and len(part) <= 18 for part in parts):
        return None
    return list(dict.fromkeys(int(part) for part in parts))


def _invalid_fallback() -> JSONResponse:
    return _err(422, "invalid_fallback",
                "Unknown 'fallback'; allowed: " + ", ".join(search.FALLBACKS))


@app.get("/api/projects/{project_id}/items")
def list_items(
    project_id: int,
    entry_ids: str | None = None,
    q: str | None = None,
    item_type: str | None = Query(None, alias="type"),
    fallback: str | None = None,
):
    """``GET /api/projects/{id}/items?entry_ids=…[&type=X][&q=…][&fallback=any_term]``
    (D13, t-entry-items; ``fallback``: RT.1.4).

    Every item of the listed entries of THIS project — entry ids of another project
    answer nothing — each with ``match`` (does it answer every filter given?),
    ``snippet`` on a ``q`` hit, and its ``status``/``links_in``/``links_out``. Reading
    stays entry-first: a caller pages ``/entries`` as before and asks here for that
    page's items. It records no recall event: the ``/entries`` read already did.
    """
    if item_type is not None and item_type not in search.ITEM_TYPES:
        return _err(422, "invalid_type",
                    "Unknown 'type'; allowed: " + ", ".join(search.ITEM_TYPES))
    if fallback is not None and fallback not in search.FALLBACKS:
        return _invalid_fallback()
    ids = _parse_entry_ids(entry_ids)
    if ids is None:
        return _err(422, "invalid_entry_ids",
                    "'entry_ids' must be 1 to "
                    f"{items.MAX_ENTRY_IDS} comma-separated entry ids")
    conn = db.connect()
    try:
        if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            return _err(404, "project_not_found", f"No project with id {project_id}")
        try:
            found = items.items_for_entries(
                conn, project_id, ids, item_type=item_type, q=q,
                any_term=fallback == search.MATCH_ANY_TERM)
        except search.InvalidQuery:
            return _err(422, "invalid_query",
                        "The 'q' parameter is not a valid search expression.")
        return {"items": found}
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/entries")
def list_entries(
    project_id: int,
    request: Request,
    since: str | None = None,
    limit: int = search.DEFAULT_LIMIT,
    q: str | None = None,
    item_type: str | None = Query(None, alias="type"),
    fallback: str | None = None,
):
    if limit < 1:
        limit = search.DEFAULT_LIMIT
    limit = min(limit, search.MAX_LIMIT)
    # Validated before anything is opened or looked up: `type` is a closed vocabulary
    # (t-entry-item-types), so an unknown label is a client error whatever else the
    # request asks for — and answering it costs no database work.
    if item_type is not None and item_type not in search.ITEM_TYPES:
        return _err(422, "invalid_type",
                    "Unknown 'type'; allowed: " + ", ".join(search.ITEM_TYPES))
    # Validated whether or not there is a `q`; without one it changes nothing (RT.1.4).
    if fallback is not None and fallback not in search.FALLBACKS:
        return _invalid_fallback()
    conn = db.connect()
    try:
        if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            return _err(404, "project_not_found", f"No project with id {project_id}")

        if q is not None:
            # Ranked branch (t-entries-fts). The terms never touch the SQL text: they
            # are bound to MATCH inside `search_entries`, and a malformed expression is
            # a 422 — the client's mistake, not a server fault. The body names the code
            # only: FTS5's own message quotes the query back and would turn this into a
            # reflection channel.
            any_term = False
            try:
                rows, has_more = search.search_entries(
                    conn, project_id, q, item_type=item_type, since=since, limit=limit
                )
                if (fallback == search.MATCH_ANY_TERM and not has_more
                        and all(ui_render.is_digest(row["content_md"]) for row in rows)
                        and search.any_term_expression(q) is not None):
                    # RT.1.4, asked for: no entry holds every word — a digest that does is
                    # no answer, since `loom log` only ever shows the latest one — so ask
                    # for any of them. When that finds nothing, the answer is the query's
                    # own. `has_more` means more strict hits beyond this page; a client
                    # topping up its page reaches them, or the end, with a larger limit.
                    # A digest is what the web UI calls one (`ui_render.is_digest`); the
                    # client's own test (`curate.is_digest`) agrees on the frontmatter
                    # okf writes, `type: digest` unquoted.
                    loose, loose_more = search.search_entries(
                        conn, project_id, q, item_type=item_type, since=since,
                        limit=limit, any_term=True)
                    if loose:
                        rows, has_more, any_term = loose, loose_more, True
            except search.InvalidQuery:
                return _err(422, "invalid_query",
                            "The 'q' parameter is not a valid search expression.")
            entries = _with_links(conn, [_entry_dict(r, ranked=True) for r in rows])
            if any_term:
                for entry in entries:
                    entry["match_mode"] = search.MATCH_ANY_TERM
        else:
            sql = (
                "SELECT e.id, e.session_id, e.created_at, e.covered_from, e.covered_to, "
                "e.content_md, u.name AS author "
                "FROM entries e LEFT JOIN users u ON u.id = e.user_id "
                "WHERE e.project_id = ?"
            )
            params: list = [project_id]
            if item_type is not None:
                # Server-side filter (never a post-filter of a page): entries with no
                # label rows — everything written before Block T — simply do not match.
                # Same fragment the ranked branch uses, so the two cannot drift apart.
                sql += search.ITEM_TYPE_FILTER_SQL
                params.append(item_type)
            if since:
                sql += " AND e.created_at > ?"
                params.append(since)
            sql += " ORDER BY e.created_at DESC, e.id DESC LIMIT ?"
            params.append(limit + 1)

            rows = conn.execute(sql, params).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            entries = _with_links(conn, [_entry_dict(r) for r in rows])

        # Recall telemetry (owner decision of 2026-09-22, issues #14/#15): one row per
        # ANSWERED read that declared its source. Only reached after the 404/422 exits
        # above, and only ever with a user the middleware authenticated; a missing or
        # unknown header writes nothing, and a failed write never reaches the reader.
        recall.record(conn, request.headers.get(recall.SOURCE_HEADER),
                      user_id=request.state.user_id, project_id=project_id,
                      query=q, result_count=len(entries))
        return {"entries": entries, "has_more": has_more}
    finally:
        conn.close()


# Last, after every core route (D21): a plugin route that redeclares one stops the start.
PLUGINS = plugins.load(app)
