"""HTTP client for the central API (DESIGN.md §6.2), a thin wrapper over ``requests``.

``Authorization: Bearer <token>`` auth on every route except ``/api/health``. Short
connect/read timeouts: a server that is down must not block the processor. Connection and
timeout errors are raised as :class:`ServerUnreachable` so the processor can tell "server
down" (keep the marker, retry) apart from a concrete HTTP status (201/409/404/5xx), which
is decided by inspecting the returned response.
"""
from __future__ import annotations

import requests

# (connect, read) in seconds. Short connect: fail fast if the server is not there.
DEFAULT_TIMEOUT: tuple[float, float] = (3.05, 15.0)


class ServerUnreachable(Exception):
    """Could not talk to the server (connection refused, timeout, DNS…).

    Different from "the server answered with an error status": that is inspected on the
    returned ``Response``.
    """


class AuthRejected(ServerUnreachable):
    """The server refused the credential (401/403) on a call that raises on non-200.

    A subclass, so every ``except ServerUnreachable`` keeps behaving as before (the
    registry cannot be refreshed either way); callers that want to be LOUD about it —
    the processor's ``auth_rejected.json``, the CLI hint — catch this one first.
    """

    def __init__(self, status: int, what: str) -> None:
        self.status = status
        self.what = what
        super().__init__(
            f"{what} returned {status} — the server rejected this machine's token "
            "(expired, revoked or deactivated); run 'loom enroll'"
        )


class ApiClient:
    """API client for a given ``server_url`` + ``token`` (empty token = anonymous)."""

    def __init__(
        self,
        server_url: str,
        token: str,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = server_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._session = session or requests.Session()

    # --- Internals ---------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _request(self, method: str, path: str, *, headers: dict[str, str] | None = None,
                 **kwargs) -> requests.Response:
        """One request with the bearer attached. ``headers`` are ADDED to it (a caller
        cannot replace or drop the credential through this seam)."""
        url = f"{self.base_url}{path}"
        try:
            return self._session.request(
                method,
                url,
                headers={**(headers or {}), **self._headers()},
                timeout=self.timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise ServerUnreachable(f"{method} {url}: {exc}") from exc

    # --- API ---------------------------------------------------------------------
    def list_projects(self) -> list[dict]:
        """``GET /api/projects`` → list of projects (for the gate's cache).

        Raises :class:`ServerUnreachable` on a connection failure **or** a 5xx (in both
        cases the registry cannot be refreshed). An unexpected 2xx or a 4xx surface as
        ``ServerUnreachable`` too, since the gate cannot trust a registry that did not
        arrive complete.
        """
        resp = self._request("GET", "/api/projects")
        if resp.status_code in (401, 403):
            raise AuthRejected(resp.status_code, "GET /api/projects")
        if resp.status_code != 200:
            raise ServerUnreachable(
                f"GET /api/projects returned {resp.status_code}"
            )
        return resp.json().get("projects", [])

    def get_project_by_remote(self, remote: str) -> dict | None:
        """``GET /api/projects?remote=<remote>`` → project or ``None`` (404).

        Raises :class:`ServerUnreachable` on a connection failure. Used by ``loom
        register``; the processor uses the cache (:meth:`list_projects`).
        """
        resp = self._request("GET", "/api/projects", params={"remote": remote})
        if resp.status_code == 404:
            return None
        if resp.status_code in (401, 403):
            raise AuthRejected(resp.status_code, "GET /api/projects?remote=")
        if resp.status_code != 200:
            raise ServerUnreachable(
                f"GET /api/projects?remote= returned {resp.status_code}"
            )
        return resp.json().get("project")

    def post_entry(self, payload: dict) -> requests.Response:
        """``POST /api/entries``. Returns the raw ``Response``.

        The processor decides by ``status_code`` (201/409 → advance; 404 → registry_error;
        5xx → keep). Raises :class:`ServerUnreachable` only on a connection failure.
        """
        return self._request("POST", "/api/entries", json=payload)

    # --- Enrolment / device tokens (issue #20) -----------------------------------
    def enroll(self, code: str, device_name: str) -> requests.Response:
        """``POST /api/enroll`` (anonymous). 201 carries this machine's device token."""
        return self._request("POST", "/api/enroll", json={"code": code, "device_name": device_name})

    def renew_device_token(self) -> requests.Response:
        """``POST /api/device-tokens/renew``. 201 carries the successor token."""
        return self._request("POST", "/api/device-tokens/renew")

    def ui_login(self) -> requests.Response:
        """``POST /api/ui-login`` (D14). 201 carries a single-use, short-lived login code."""
        return self._request("POST", "/api/ui-login")

    def me(self) -> requests.Response:
        """``GET /api/me`` (D20): who this token is and the account events about them."""
        return self._request("GET", "/api/me")
