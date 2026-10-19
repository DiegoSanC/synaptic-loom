"""The route table as a whole (D19, plan 2026-10-19 "Structural rule").

Two properties no single route test can hold: the anonymous surface is exactly the
listed set, and no route takes a secret in its path or query string (uvicorn's access
log records both, and a browser keeps them in its history). A secret travels in a body,
a header, a cookie or a URL fragment, which never reaches the server.
"""
from fastapi.routing import APIRoute
from starlette.routing import Route

from server import app as app_module
from server import ui_pages

# DESIGN §6.2, as an exact set. Adding a path here is adding an anonymous door.
EXPECTED_PUBLIC_PATHS = {
    "/api/health", "/api/enroll",
    "/ui/login", "/ui/session", "/ui/join",
    "/ui/static/login.js", "/ui/static/favicon.svg", "/ui/static/ui.css",
}
# Every path or query parameter in the route table, under the name it travels as. None is
# a credential: a new one joins this list only once someone has checked that it is not.
EXPECTED_URL_PARAMETERS = {
    "author", "before", "entry_id", "entry_ids", "limit", "link_id", "ordinal",
    "project_id", "q", "remote", "show_replaced", "since", "tab", "type", "user_id",
    "device_id",    # D20 /ui/devices/{id}/revoke: a device_tokens row id (§6.4), not a secret
}
# FastAPI's own documentation routes: plain Starlette routes that take no parameter.
DOC_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def _routes() -> list:
    """The app's routes AND the pages router's: FastAPI wraps an included router in one
    opaque entry of `app.routes` (see `test_ui_views._ui_routes`)."""
    return [r for r in [*app_module.app.routes, *ui_pages.router.routes]
            if r.__class__.__name__ != "_IncludedRouter"]


def test_the_public_allow_list_is_exactly_the_expected_set():
    assert set(app_module.PUBLIC_PATHS) == EXPECTED_PUBLIC_PATHS


def test_every_public_path_is_a_route():
    paths = {r.path for r in _routes()}
    assert EXPECTED_PUBLIC_PATHS <= paths, EXPECTED_PUBLIC_PATHS - paths


def test_no_route_takes_a_secret_in_its_path_or_query():
    found = set()
    for route in _routes():
        if isinstance(route, APIRoute):
            params = [*route.dependant.path_params, *route.dependant.query_params]
            found |= {p.alias for p in params}
        else:
            # Anything else could read the URL past this inventory; only the docs may.
            assert isinstance(route, Route) and route.path in DOC_PATHS, route
    assert found == EXPECTED_URL_PARAMETERS


def test_the_secrets_travel_in_a_body():
    """The two anonymous redemptions take their code in the JSON body."""
    by_path = {r.path: r for r in _routes() if isinstance(r, APIRoute)}
    for path in ("/api/enroll", "/ui/session"):
        route = by_path[path]
        assert route.methods == {"POST"}, path
        assert route.dependant.body_params, path
