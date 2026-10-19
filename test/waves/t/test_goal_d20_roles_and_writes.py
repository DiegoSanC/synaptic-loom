"""FROZEN GOAL — D20 phase 2a: roles and a writable ``/ui`` reached through ``loom ui``.
Read-only to implementers.

Oracle properties. The specification is DESIGN.md's D20 row and §6.4 ("Accounts v2 —
roles and writes"), the "ACs common to 2a and 2b" and "2a" sections of
``docs/plans/2026-10-19-feat-invite-links-and-accounts-v2-plan.md``, and the behaviour that
had already shipped when this wave was written (``admin.py``, ``/api/enroll``,
``/api/ui-login``, ``/ui/session``, the ``loom`` CLI). Every expected value below is derived
from those texts or from a literal, never from the code under test; each test's docstring
names the AC or the §6.4 row it asserts.

The boundary is the REAL server in Docker (AGENTS.md §10): the repo's own ``Dockerfile``
built the way wave ``m`` builds it, ``docker run`` against a named volume of its own with
the port published on ``127.0.0.1``, driven over real HTTP; ``docker exec … python -m
server.admin`` is the operator; the SQLite file on the volume is read (and, for two
fixtures that the public surface cannot produce, written) with ``python -m sqlite3``'s
library inside the container. No Compose: waves ``r``/``s`` own fixed subnets.

Three deployment settings shape the harness, and none of them is a product shortcut:

* ``LOOM_PUBLIC_URL`` is the exact origin the client uses (``http://127.0.0.1:<port>``),
  so the Origin check of §6.4 is graded against the real thing.
* ``LOOM_CLIENT_IP_SOURCE=header:x-goal-client-ip`` (a D17 mode, the one a trusted proxy
  uses) and a fresh address on every request. The auth-failure window is ten per address
  per minute (§6.2), and this wave deliberately provokes dozens of refusals; one shared
  address would turn its later assertions into 429s. It also lets ``auth_events.client_ip``
  be checked against the address that made the request.
* The ``loom_ui`` cookie is ``Secure``. ``requests`` will not send a Secure cookie over plain
  http, so every browser request below carries it explicitly in a ``Cookie`` header.

Skips are scoped to the test that needs the tool, and under ``LOOM_GATE=1`` a missing tool
is a FAILURE (wave ``m``'s rule). A missing Docker daemon fails the image build: a failure,
never a skip.

Run: .venv/bin/python -m pytest test/waves/t -q
"""
from __future__ import annotations

import atexit
import hashlib
import hmac
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _goalkit as kit  # noqa: E402

REPO_ROOT = kit.REPO_ROOT
DOCKERFILE = REPO_ROOT / "Dockerfile"
PRE_D20_SCHEMA = Path(__file__).with_name("fixtures") / "pre_d20_schema.sql"
LOOM_GATE = os.environ.get("LOOM_GATE") == "1"

# --------------------------------------------------------------------------- #
# Contract literals (duplicated, never imported)                               #
# --------------------------------------------------------------------------- #
INTERNAL_PORT = 8080                  # the Dockerfile CMD's --port
DB_PATH = "/data/loom.db"             # the image's ENV LOOM_DB
CLIENT_IP_HEADER = "x-goal-client-ip"
SESSION_COOKIE = "loom_ui"            # §6.3
CSRF_MESSAGE = b"csrf"                # §6.4: HMAC-SHA256(key = the cookie value, msg = "csrf")
SETUP_TTL_S = 600                     # §6.4: setup code, 10 min
INVITE_TTL_S = 24 * 3600              # §6.4: invite, 24 h
SHORT_SETUP_TTL_S = 3                 # LOOM_SETUP_CODE_TTL_S in the TTL container
EVENTS_LIMIT = 20                     # §6.4 GET /api/me: the 20 most recent rows
RETENTION_DAYS = 180                  # §6.4: LOOM_AUTH_EVENTS_RETENTION_DAYS default
ME_KEYS = {"name", "email", "role", "device_id", "events"}
EVENT_KEYS = {"ts", "event", "actor", "target_type", "target_id"}
HOSTILE = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'

SETUP_CODE = "/ui/machines/setup-code"
SIGNOUT = "/ui/signout"
INVITE = "/ui/admin/users/invite"
MACHINES = "/ui/machines"
ADMIN_USERS = "/ui/admin/users"


def revoke_path(device_id: int) -> str:
    return f"/ui/devices/{device_id}/revoke"


def deactivate_path(user_id: int) -> str:
    return f"/ui/admin/users/{user_id}/deactivate"


def role_path(user_id: int) -> str:
    return f"/ui/admin/users/{user_id}/role"


UNKNOWN_ID = 987_654_321

# Every token, code and cookie the wave has seen: none may appear in a log line or in an
# `auth_events` row (plan, "no secret in any log line"; §6.4, "No event row ever holds a
# secret"). CSRF values are not here: they are derived, never stored, and grant nothing
# without the cookie.
SECRETS: set[str] = set()
RETIRED_LOGS: list[str] = []


def _secret(value: str) -> str:
    SECRETS.add(value)
    return value


# --------------------------------------------------------------------------- #
# Tools                                                                         #
# --------------------------------------------------------------------------- #
def _require_tool(name: str, why: str) -> None:
    if shutil.which(name) is not None:
        return
    reason = f"the D20 goal needs the real {name} binary: {why}"
    if LOOM_GATE:
        pytest.fail(f"LOOM_GATE=1 forbids skipping — {reason}")
    pytest.skip(reason)


def _docker(*args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


_ADDRESSES = itertools.count(1)


def _next_ip() -> str:
    """A fresh client address per request (see the module docstring)."""
    n = next(_ADDRESSES) + (os.getpid() % 200) * 65536
    return f"10.{(n >> 16) % 250 + 1}.{(n >> 8) & 255}.{n & 255}"


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _uid() -> str:
    return uuid.uuid4().hex[:8]


# --------------------------------------------------------------------------- #
# The server in Docker                                                          #
# --------------------------------------------------------------------------- #
_SQL = (
    "import json, os, sqlite3, sys\n"
    "c = sqlite3.connect(os.environ['LOOM_DB'], timeout=30, isolation_level=None)\n"
    "c.row_factory = sqlite3.Row\n"
    "rows = c.execute(sys.argv[1], json.loads(sys.argv[2])).fetchall()\n"
    "print(json.dumps([dict(r) for r in rows]))\n"
)

_LIVE: list["Server"] = []


class Server:
    """One container of the repo image on a volume of its own."""

    def __init__(self, image: str, *, public_url: bool = True,
                 env: dict[str, str] | None = None, seed=None):
        self.image = image
        self.port = kit.find_free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.name = f"loom-goal-t-{os.getpid()}-{_uid()}"
        self.volume = f"{self.name}-data"
        _LIVE.append(self)
        made = _docker("volume", "create", self.volume)
        assert made.returncode == 0, f"`docker volume create` must succeed: {made.stderr}"
        if seed is not None:
            seed(self)
        args = ["run", "-d", "--name", self.name,
                "-p", f"127.0.0.1:{self.port}:{INTERNAL_PORT}",
                "-v", f"{self.volume}:/data",
                "-e", f"LOOM_CLIENT_IP_SOURCE=header:{CLIENT_IP_HEADER}"]
        if public_url:
            args += ["-e", f"LOOM_PUBLIC_URL={self.url}"]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        started = _docker(*args, image)
        assert started.returncode == 0, f"`docker run` must succeed: {started.stderr[-2000:]}"
        self.wait_health()

    def wait_health(self, timeout_s: float = 90.0) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                r = requests.get(f"{self.url}/api/health", timeout=2,
                                 headers={CLIENT_IP_HEADER: _next_ip()})
                if r.status_code == 200:
                    return
            except requests.RequestException:
                pass
            time.sleep(0.3)
        raise AssertionError(f"the container never served /api/health on {self.url}.\n"
                             f"--- container logs ---\n{self.logs()[-4000:]}")

    def logs(self) -> str:
        done = _docker("logs", self.name)
        return done.stdout + done.stderr

    def restart(self) -> None:
        done = _docker("restart", self.name)
        assert done.returncode == 0, done.stderr
        self.wait_health()

    def remove(self) -> None:
        if self not in _LIVE:
            return
        _LIVE.remove(self)
        try:
            RETIRED_LOGS.append(self.logs())
        finally:
            _docker("rm", "-f", self.name)
            _docker("volume", "rm", "-f", self.volume)

    # -- the operator ----------------------------------------------------------
    def admin(self, *args: str) -> subprocess.CompletedProcess:
        """`python -m server.admin` IN the container, as `app` (wave m: a root-written WAL
        sidecar is a file the server cannot write)."""
        return _docker("exec", "-u", "app", self.name, "python", "-m", "server.admin", *args)

    def sql(self, query: str, params: tuple | list = ()) -> list[dict]:
        done = _docker("exec", "-u", "app", self.name, "python", "-c", _SQL, query,
                       json.dumps(list(params)))
        assert done.returncode == 0, f"SQL failed: {query}\n{done.stderr[-2000:]}"
        return json.loads(done.stdout)

    # -- HTTP ------------------------------------------------------------------
    def api(self, method: str, path: str, token: str | None = None,
            **kwargs) -> requests.Response:
        headers = {CLIENT_IP_HEADER: _next_ip(), **kwargs.pop("headers", {})}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return requests.request(method, self.url + path, headers=headers, timeout=30,
                                allow_redirects=False, **kwargs)


def _cleanup_all() -> None:
    for server in list(_LIVE):
        server.remove()


atexit.register(_cleanup_all)


@pytest.fixture(scope="session")
def image():
    """The REAL image, from the repo's own Dockerfile and the repo as context (wave m)."""
    _require_tool("docker", "the server in Docker IS the boundary (AGENTS.md §10)")
    tag = f"loom-goal-t-img:{os.getpid()}-{_uid()}"
    built = _docker("build", "-t", tag, "-f", str(DOCKERFILE), str(REPO_ROOT), timeout=2400)
    assert built.returncode == 0, (
        f"`docker build` of the repo Dockerfile must succeed.\n"
        f"{built.stdout[-3000:]}\n{built.stderr[-3000:]}")
    yield tag
    _cleanup_all()
    _docker("image", "rm", "-f", tag)


@pytest.fixture(scope="session")
def srv(image):
    """The shared server: public URL set, default TTLs."""
    server = Server(image)
    yield server
    server.remove()


@pytest.fixture(scope="session")
def no_url_srv(image):
    """No LOOM_PUBLIC_URL and no FLY_APP_NAME: the public URL does not resolve."""
    server = Server(image, public_url=False)
    yield server
    server.remove()


@pytest.fixture
def fresh(image):
    """Containers of this test's own, for assertions about GLOBAL state (the last admin)."""
    started: list[Server] = []

    def start(**kwargs) -> Server:
        server = Server(image, **kwargs)
        started.append(server)
        return server

    yield start
    for server in started:
        server.remove()


# --------------------------------------------------------------------------- #
# Accounts, machines and browsers                                               #
# --------------------------------------------------------------------------- #
@dataclass
class Account:
    name: str
    email: str
    user_id: int
    token: str            # the device token of the first machine
    device_id: int
    device_name: str


def _invite_code(server: Server, email: str, name: str | None = None) -> str:
    args = ["invite", "--email", email] + (["--name", name] if name else [])
    done = server.admin(*args)
    assert done.returncode == 0, f"admin.py invite must succeed: {done.stderr}"
    return _secret(kit.parse_admin_token(done.stdout))


def _enrol(server: Server, code: str, device_name: str,
           bearer: str | None = None) -> requests.Response:
    return server.api("POST", "/api/enroll", bearer,
                      json={"code": code, "device_name": device_name})


def _enrolled_token(server: Server, code: str, device_name: str) -> tuple[str, int]:
    r = _enrol(server, code, device_name)
    assert r.status_code == 201, f"/api/enroll must redeem the code: {r.status_code} {r.text}"
    return _secret(r.json()["token"]), _device_id(server, device_name)


def _device_id(server: Server, device_name: str) -> int:
    rows = server.sql("SELECT id FROM device_tokens WHERE device_name = ? ORDER BY id",
                      [device_name])
    assert len(rows) == 1, f"exactly one device named {device_name!r}: {rows}"
    return rows[0]["id"]


def _user(server: Server, email: str) -> dict:
    rows = server.sql("SELECT * FROM users WHERE email = ?", [email])
    assert len(rows) == 1, f"exactly one user {email!r}: {rows}"
    return rows[0]


def _promote(server: Server, email: str) -> None:
    done = server.admin("promote", "--email", email)
    assert done.returncode == 0, (
        f"`admin.py promote --email` must exist and succeed (D20): {done.stdout}{done.stderr}")


def _demote(server: Server, email: str) -> subprocess.CompletedProcess:
    return server.admin("demote", "--email", email)


def _account(server: Server, prefix: str, *, role: str = "member") -> Account:
    tag = _uid()
    name, email = f"{prefix}-{tag}", f"{prefix}-{tag}@example.invalid"
    device_name = f"{prefix}-{tag}-laptop"
    token, device_id = _enrolled_token(server, _invite_code(server, email, name), device_name)
    if role == "admin":
        _promote(server, email)
    return Account(name, email, _user(server, email)["id"], token, device_id, device_name)


def _machine(server: Server, acct: Account, label: str) -> tuple[str, int, str]:
    """Another machine of `acct`, through a fresh admin.py invitation."""
    device_name = f"{acct.device_name}-{label}"
    token, device_id = _enrolled_token(server, _invite_code(server, acct.email), device_name)
    return token, device_id, device_name


def _permanent_user(server: Server, prefix: str) -> tuple[str, str, int, str]:
    tag = _uid()
    name, email = f"{prefix}-{tag}", f"{prefix}-{tag}@example.invalid"
    done = server.admin("adduser", "--name", name, "--email", email)
    assert done.returncode == 0, done.stderr
    token = _secret(kit.parse_admin_token(done.stdout))
    return name, email, _user(server, email)["id"], token


_SAME = object()


class Browser:
    """One `loom_ui` session, sent explicitly (the cookie is `Secure`, the origin http)."""

    def __init__(self, server: Server, cookie: str, signin_ip: str | None = None):
        self.server = server
        self.cookie = cookie
        self.csrf = hmac.new(cookie.encode("utf-8"), CSRF_MESSAGE, hashlib.sha256).hexdigest()
        self.signin_ip = signin_ip
        self.last_ip: str | None = None

    def request(self, method: str, path: str, *, data=None, origin=None,
                sec_fetch_site: str | None = None) -> requests.Response:
        self.last_ip = _next_ip()
        headers = {CLIENT_IP_HEADER: self.last_ip,
                   "Cookie": f"{SESSION_COOKIE}={self.cookie}"}
        if origin is not None:
            headers["Origin"] = origin
        if sec_fetch_site is not None:
            headers["Sec-Fetch-Site"] = sec_fetch_site
        return requests.request(method, self.server.url + path, headers=headers, data=data,
                                timeout=30, allow_redirects=False)

    def get(self, path: str) -> requests.Response:
        return self.request("GET", path)

    def post(self, path: str, fields: dict | None = None, *, csrf=_SAME, origin=_SAME,
             sec_fetch_site: str | None = None) -> requests.Response:
        """A form POST as a same-origin page would send it, unless told otherwise."""
        data = dict(fields or {})
        csrf = self.csrf if csrf is _SAME else csrf
        if csrf is not None:
            data["csrf"] = csrf
        origin = self.server.url if origin is _SAME else origin
        return self.request("POST", path, data=data or None, origin=origin,
                            sec_fetch_site=sec_fetch_site)


def _sign_in(server: Server, bearer: str) -> Browser:
    """The shipped D14 sign-in: `/api/ui-login` with the bearer, then `/ui/session`."""
    issued = server.api("POST", "/api/ui-login", bearer)
    assert issued.status_code == 201, f"/api/ui-login: {issued.status_code} {issued.text}"
    code = _secret(issued.json()["code"])
    ip = _next_ip()
    opened = requests.post(f"{server.url}/ui/session", json={"code": code},
                           headers={CLIENT_IP_HEADER: ip}, timeout=30, allow_redirects=False)
    assert opened.status_code == 204, f"/ui/session: {opened.status_code} {opened.text}"
    match = re.search(rf"{SESSION_COOKIE}=([^;]+)", opened.headers.get("set-cookie", ""))
    assert match, f"no {SESSION_COOKIE} cookie: {opened.headers}"
    return Browser(server, _secret(match.group(1)), signin_ip=ip)


def _refused(r: requests.Response, status: int, code: str) -> None:
    assert r.status_code == status and code in r.text.lower(), (
        f"expected {status} {code}, got {r.status_code}: {r.text[:600]}")


def _redirected_to(r: requests.Response, path: str) -> None:
    assert r.status_code == 303, f"expected 303 to {path}, got {r.status_code}: {r.text[:600]}"
    assert urlparse(r.headers["location"]).path == path, r.headers["location"]


def _link_code(server: Server, page: str) -> str:
    """The `/ui/join#code=…` link on a one-time page, with the public URL in front."""
    match = re.search(re.escape(server.url) + r"/ui/join#code=([A-Za-z0-9_-]{20,})", page)
    assert match, f"the page must show the {server.url}/ui/join#code=… link:\n{page[:2000]}"
    return _secret(match.group(1))


def _issue_setup_code(browser: Browser) -> str:
    r = browser.post(SETUP_CODE)
    assert r.status_code == 200, f"POST {SETUP_CODE}: {r.status_code} {r.text[:600]}"
    return _link_code(browser.server, r.text)


def _codes_of(server: Server, user_id: int) -> int:
    return len(server.sql("SELECT id FROM enrolment_codes WHERE user_id = ?", [user_id]))


def _events(server: Server, target_type: str, target_id: int) -> list[dict]:
    return server.sql("SELECT * FROM auth_events WHERE target_type = ? AND target_id = ? "
                      "ORDER BY id", [target_type, target_id])


def _has_event(server: Server, event: str, target_type: str, target_id: int,
               actor_user_id: int | None) -> dict:
    rows = [e for e in _events(server, target_type, target_id)
            if e["event"] == event and e["actor_user_id"] == actor_user_id]
    assert rows, (f"auth_events must hold {event} on {target_type} {target_id} by "
                  f"{actor_user_id}: {_events(server, target_type, target_id)}")
    return rows[-1]


def _api_status(server: Server, token: str) -> int:
    return server.api("GET", "/api/projects", token).status_code


def _segment(page: str, start: str, others: list[str]) -> str:
    """The text from `start` up to the next of `others` (or the end): one user's row."""
    i = page.index(start)
    ends = [page.find(o, i + len(start)) for o in others]
    ends = [e for e in ends if e != -1]
    return page[i:min(ends) if ends else len(page)]


def _form_action(page: str, path: str) -> bool:
    return re.search(r"action=[\"']?(?:https?://[^/\"' >]+)?" + re.escape(path) + r"[\"' >]",
                     page) is not None


def _loom(home: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    """The real `loom` CLI in a NEW SESSION (getpass falls back to stdin; the enrolment
    e2e's idiom), with a browser that opens nothing."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOOM_")}
    env.update({"LOOM_HOME": str(home), "BROWSER": shutil.which("true") or "/usr/bin/true"})
    return subprocess.run([sys.executable, "-m", "loom.cli", *args], input=stdin,
                          capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
                          timeout=180, start_new_session=True)


def _loom_config(home: Path) -> dict:
    return json.loads((home / "config.json").read_text(encoding="utf-8"))


# =========================================================================== #
# Sanity: the harness and the shipped behaviour it stands on (green today)      #
# =========================================================================== #
def test_sanity_real_loom_enroll_and_loom_ui_open_a_session(srv, tmp_path):
    """Harness sanity over SHIPPED behaviour (D14, D19): `admin.py invite` → the real
    `loom enroll` → the real `loom ui` → `/ui/session` → a page. Green before D20."""
    tag = _uid()
    email = f"sanity-{tag}@example.invalid"
    code = _invite_code(srv, email, f"sanity-{tag}")
    home = kit.loom_home(tmp_path)
    enrolled = _loom(home, "enroll", "--server-url", srv.url, "--device-name",
                     f"sanity-{tag}", stdin=code + "\n")
    assert enrolled.returncode == 0, enrolled.stdout + enrolled.stderr
    token = _secret(_loom_config(home)["token"])
    assert _api_status(srv, token) == 200

    opened = _loom(home, "ui")
    assert opened.returncode == 0, opened.stdout + opened.stderr
    match = re.search(re.escape(srv.url) + r"/ui/login#code=([A-Za-z0-9_-]+)", opened.stdout)
    assert match, opened.stdout
    session = requests.post(f"{srv.url}/ui/session", json={"code": _secret(match.group(1))},
                            headers={CLIENT_IP_HEADER: _next_ip()}, timeout=30)
    assert session.status_code == 204, session.text
    cookie = _secret(re.search(rf"{SESSION_COOKIE}=([^;]+)",
                               session.headers["set-cookie"]).group(1))
    page = Browser(srv, cookie).get("/ui/")
    assert page.status_code == 200, page.text[:500]
    assert page.headers["cache-control"] == "no-store"


def test_sanity_d14_crawl_the_shipped_pages_refuse_every_write_method(srv):
    """§6.4 "the D14 crawl holds": every shipped GET page answers 405 to POST, PUT, PATCH
    and DELETE, with a live session and a same-origin form that carries the csrf."""
    acct = _account(srv, "crawl")
    browser = _sign_in(srv, acct.token)
    for path in ("/ui/", "/ui/usage"):
        assert browser.get(path).status_code == 200, path
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            r = browser.request(method, path, data={"csrf": browser.csrf}, origin=srv.url)
            assert r.status_code == 405, f"{method} {path}: {r.status_code}"


def test_unlisted_posts_are_405_before_any_credential_is_read(srv):
    """§6.4 refusals, first: "a POST to a path not listed above → 405". With no cookie at
    all, and with a valid session sending a valid Origin and csrf."""
    acct = _account(srv, "unlisted")
    browser = _sign_in(srv, acct.token)
    unlisted = ["/ui/", "/ui/usage", MACHINES, ADMIN_USERS, "/ui/machines/setup",
                f"/ui/devices/{acct.device_id}", f"/ui/devices/{acct.device_id}/delete",
                f"/ui/admin/users/{acct.user_id}", f"/ui/admin/users/{acct.user_id}/delete",
                f"/ui/admin/users/{acct.user_id}/promote", "/ui/admin", "/ui/no-such-page"]
    for path in unlisted:
        anonymous = requests.post(srv.url + path, headers={CLIENT_IP_HEADER: _next_ip()},
                                  timeout=30, allow_redirects=False)
        assert anonymous.status_code == 405, f"anonymous POST {path}: {anonymous.status_code}"
        signed = browser.post(path)
        assert signed.status_code == 405, f"signed-in POST {path}: {signed.status_code}"
    assert _codes_of(srv, acct.user_id) == 1  # only its admin.py invitation


# =========================================================================== #
# Schema (§6.4 table) and the migration rule                                    #
# =========================================================================== #
def _columns(server: Server, table: str) -> dict[str, dict]:
    return {c["name"]: c for c in server.sql(f"PRAGMA table_info({table})")}


def _assert_d20_schema(server: Server) -> None:
    users = _columns(server, "users")
    assert "role" in users, f"users.role (§6.4): {sorted(users)}"
    assert users["role"]["type"].upper() == "TEXT" and users["role"]["notnull"] == 1
    assert users["role"]["dflt_value"] == "'member'", users["role"]

    sessions = _columns(server, "ui_sessions")
    assert "kind" in sessions, f"ui_sessions.kind (§6.4): {sorted(sessions)}"
    assert sessions["kind"]["notnull"] == 1 and sessions["kind"]["dflt_value"] == "'cli'"

    codes = _columns(server, "enrolment_codes")
    for column in ("kind", "created_by_user_id", "issued_by_device_token_id"):
        assert column in codes, f"enrolment_codes.{column} (§6.4): {sorted(codes)}"
    assert codes["kind"]["notnull"] == 1 and codes["kind"]["dflt_value"] == "'invite'"
    assert codes["created_by_user_id"]["notnull"] == 0
    assert codes["issued_by_device_token_id"]["notnull"] == 0

    events = _columns(server, "auth_events")
    assert set(events) == {"id", "ts", "actor_user_id", "event", "target_type", "target_id",
                           "client_ip"}, sorted(events)
    for column in ("ts", "event", "target_type", "target_id"):
        assert events[column]["notnull"] == 1, column
    assert events["actor_user_id"]["notnull"] == 0

    indexes = {r["name"]: r for r in server.sql(
        "SELECT name, tbl_name FROM sqlite_master WHERE type = 'index'")}
    assert indexes.get("idx_enrolment_codes_issuer", {}).get("tbl_name") == "enrolment_codes"
    assert indexes.get("idx_auth_events_target", {}).get("tbl_name") == "auth_events"
    issuer = [r["name"] for r in server.sql("PRAGMA index_info(idx_enrolment_codes_issuer)")]
    assert issuer == ["issued_by_device_token_id"], issuer
    target = [r["name"] for r in server.sql("PRAGMA index_info(idx_auth_events_target)")]
    assert target == ["target_type", "target_id", "ts"], target


def test_schema_objects_of_the_6_4_table_exist_on_a_new_database(srv):
    """§6.4 Schema table: `users.role`, `ui_sessions.kind`, the three `enrolment_codes`
    columns with `idx_enrolment_codes_issuer`, and `auth_events` with
    `idx_auth_events_target(target_type, target_id, ts)`."""
    _assert_d20_schema(srv)


def _seed_pre_d20(secrets_: dict[str, str]):
    """A volume holding a database as it stood BEFORE D20 (the frozen fixture), with one
    legacy user, their machine, an unredeemed invitation and an open UI session."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    script = (
        "import hashlib, os, sqlite3, sys\n"
        "h = lambda s: hashlib.sha256(s.encode()).hexdigest()\n"
        "schema, legacy, device, code, cookie, now, later = sys.argv[1:8]\n"
        "c = sqlite3.connect(os.environ['LOOM_DB'])\n"
        "c.executescript(schema)\n"
        "c.execute(\"INSERT INTO users (id, name, email, token_hash, active, created_at) "
        "VALUES (1, 'legacy-user', 'legacy@example.invalid', ?, 1, ?)\", (h(legacy), now))\n"
        "c.execute(\"INSERT INTO device_tokens (id, user_id, token_hash, device_name, "
        "issued_at, expires_at) VALUES (7, 1, ?, 'legacy-laptop', ?, ?)\", "
        "(h(device), now, later))\n"
        "c.execute(\"INSERT INTO enrolment_codes (user_id, code_hash, created_at, expires_at) "
        "VALUES (1, ?, ?, ?)\", (h(code), now, later))\n"
        "c.execute(\"INSERT INTO ui_sessions (user_id, device_token_id, session_hash, "
        "created_at, expires_at) VALUES (1, 7, ?, ?, ?)\", (h(cookie), now, later))\n"
        "c.commit()\n"
    )

    def seed(server: Server) -> None:
        done = _docker("run", "--rm", "-v", f"{server.volume}:/data", server.image,
                       "python", "-c", script, PRE_D20_SCHEMA.read_text(encoding="utf-8"),
                       secrets_["legacy"], secrets_["device"], secrets_["code"],
                       secrets_["cookie"], _iso(now), _iso(now + timedelta(days=30)))
        assert done.returncode == 0, f"seeding the pre-D20 database failed: {done.stderr}"

    return seed


@pytest.fixture(scope="module")
def legacy_srv(image):
    """A server started on a PRE-D20 database, with LOOM_SETUP_CODE_TTL_S shortened."""
    secrets_ = {k: _secret(f"goal-t-{k}-{uuid.uuid4().hex}{uuid.uuid4().hex}")
                for k in ("legacy", "device", "code", "cookie")}
    server = Server(image, env={"LOOM_SETUP_CODE_TTL_S": str(SHORT_SETUP_TTL_S)},
                    seed=_seed_pre_d20(secrets_))
    server.seeded = secrets_
    yield server
    server.remove()


def test_a_pre_migration_database_gains_the_columns_and_its_rows_their_defaults(legacy_srv):
    """§6.4 migration rule on "a pre-migration copy of the database": every object is added
    on start; "Every existing user starts as `member`"; an existing code is an `invite`, an
    existing session a `cli` one — so it can write — and both keep working."""
    seeded = legacy_srv.seeded
    _assert_d20_schema(legacy_srv)
    assert _user(legacy_srv, "legacy@example.invalid")["role"] == "member"
    assert [r["kind"] for r in legacy_srv.sql("SELECT kind FROM enrolment_codes")] == ["invite"]
    assert [r["kind"] for r in legacy_srv.sql("SELECT kind FROM ui_sessions")] == ["cli"]

    me = legacy_srv.api("GET", "/api/me", seeded["legacy"])
    assert me.status_code == 200, me.text
    assert me.json()["role"] == "member" and me.json()["device_id"] is None
    assert legacy_srv.api("GET", "/api/me", seeded["device"]).json()["device_id"] == 7

    browser = Browser(legacy_srv, seeded["cookie"])
    assert browser.get(MACHINES).status_code == 200
    _issue_setup_code(browser)
    redeemed = _enrol(legacy_srv, seeded["code"], "legacy-desktop")
    assert redeemed.status_code == 201 and redeemed.json()["user"]["name"] == "legacy-user"


# =========================================================================== #
# admin.py promote / demote and the last-admin guard                            #
# =========================================================================== #
def test_admin_py_promote_and_demote_with_member_as_the_default(srv):
    """D20 / plan "Common": `users.role` defaults to `member`; `admin.py promote/demote
    --email` change it and write `promoted`/`demoted` rows with a NULL actor (§6.4
    Events); an unknown email is an error."""
    _account(srv, "keeper", role="admin")       # so demoting X is never the last admin
    x = _account(srv, "promoted")
    assert _user(srv, x.email)["role"] == "member"
    _promote(srv, x.email)
    assert _user(srv, x.email)["role"] == "admin"
    _has_event(srv, "promoted", "user", x.user_id, None)
    done = _demote(srv, x.email)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _user(srv, x.email)["role"] == "member"
    _has_event(srv, "demoted", "user", x.user_id, None)
    assert srv.admin("promote", "--email", f"nobody-{_uid()}@example.invalid").returncode != 0


def test_admin_py_other_commands_write_no_event(srv):
    """§6.4 Events: "`admin.py`'s other commands keep phase 1's rule and write none"."""
    victim = _account(srv, "quiet")
    other = _account(srv, "quiet-other")
    before = srv.sql("SELECT COUNT(*) AS n FROM auth_events")[0]["n"]
    tag = _uid()
    assert srv.admin("adduser", "--name", f"q-{tag}", "--email",
                     f"q-{tag}@example.invalid").returncode == 0
    _invite_code(srv, f"qi-{tag}@example.invalid", f"qi-{tag}")
    assert srv.admin("listusers").returncode == 0
    assert srv.admin("listdevices").returncode == 0
    assert srv.admin("revokedevice", "--id", str(victim.device_id)).returncode == 0
    assert srv.admin("deactivate", "--email", other.email).returncode == 0
    assert srv.sql("SELECT COUNT(*) AS n FROM auth_events")[0]["n"] == before


def test_admin_py_refuses_to_demote_or_deactivate_the_last_active_admin(fresh):
    """D20: "the last active admin can be neither demoted nor deactivated", from
    `admin.py` (§6.4: `409 last_admin` on the web, here a failing exit with nothing
    changed). An inactive admin does not count as one."""
    server = fresh()
    a = _account(server, "only", role="admin")
    assert _demote(server, a.email).returncode != 0
    assert _user(server, a.email)["role"] == "admin"
    assert server.admin("deactivate", "--email", a.email).returncode != 0
    assert _user(server, a.email)["active"] == 1
    assert _api_status(server, a.token) == 200

    b = _account(server, "dormant", role="admin")
    assert server.admin("deactivate", "--email", b.email).returncode == 0
    assert _demote(server, a.email).returncode != 0, "B is inactive: A is still the last"
    assert _user(server, a.email)["role"] == "admin"

    c = _account(server, "second", role="admin")
    assert _demote(server, a.email).returncode == 0
    assert _user(server, a.email)["role"] == "member"
    assert _user(server, c.email)["role"] == "admin"


def test_the_last_admin_guard_from_the_web_is_409(fresh):
    """§6.4 refusals: "a demotion or deactivation of the last active admin, from the web …
    → `409 last_admin`", nothing changed; an inactive admin does not count."""
    server = fresh()
    a = _account(server, "webonly", role="admin")
    sa = _sign_in(server, a.token)
    _refused(sa.post(role_path(a.user_id), {"role": "member"}), 409, "last_admin")
    _refused(sa.post(deactivate_path(a.user_id)), 409, "last_admin")
    assert _user(server, a.email)["role"] == "admin" and _user(server, a.email)["active"] == 1
    assert _api_status(server, a.token) == 200

    b = _account(server, "webdormant", role="admin")
    _redirected_to(sa.post(deactivate_path(b.user_id)), ADMIN_USERS)
    _refused(sa.post(role_path(a.user_id), {"role": "member"}), 409, "last_admin")
    _refused(sa.post(deactivate_path(a.user_id)), 409, "last_admin")

    _account(server, "websecond", role="admin")
    _redirected_to(sa.post(role_path(a.user_id), {"role": "member"}), ADMIN_USERS)
    assert _user(server, a.email)["role"] == "member"
    _refused(sa.get(ADMIN_USERS), 403, "forbidden")


# =========================================================================== #
# My machines, setup codes, revoke                                              #
# =========================================================================== #
def test_my_machines_lists_only_the_callers_chains_with_their_forms(srv):
    """§6.4 GET /ui/machines: one row per chain of the caller with its state, an "Add a
    machine" form and a "Revoke" form per LIVE chain; the page embeds the csrf; §6.3's
    headers hold on the new page."""
    m = _account(srv, "mine")
    _, m2_id, m2_name = _machine(srv, m, "old")
    other = _account(srv, "notmine")
    assert srv.admin("revokedevice", "--id", str(m2_id)).returncode == 0
    browser = _sign_in(srv, m.token)
    r = browser.get(MACHINES)
    assert r.status_code == 200, r.text[:600]
    page = r.text
    assert m2_name in page and page.count(m.device_name) > page.count(m2_name)
    assert other.device_name not in page
    assert _form_action(page, SETUP_CODE)
    assert _form_action(page, revoke_path(m.device_id))
    assert not _form_action(page, revoke_path(m2_id)), "a revoked chain has no Revoke form"
    assert "revoked" in _segment(page, m2_name, [m.device_name]).lower()
    assert re.search(r"name=[\"']?csrf[\"' ]", page) and browser.csrf in page
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in r.headers["content-security-policy"]


def test_add_a_machine_shows_a_single_use_setup_code_that_real_loom_enroll_redeems(
        srv, tmp_path):
    """§6.4 POST /ui/machines/setup-code: `200`, the code and its `/ui/join#code=…` link
    shown once; single use; 10 min; redeemed by the existing `loom enroll` into a working
    device token of the SAME user. The row records kind, issuer and issuing device; a
    `setup_code_issued` event names the caller and the request's address."""
    m = _account(srv, "adder")
    browser = _sign_in(srv, m.token)
    r = browser.post(SETUP_CODE)
    assert r.status_code == 200, r.text[:600]
    assert r.headers["cache-control"] == "no-store"
    code = _link_code(srv, r.text)
    issued_from = browser.last_ip

    row = srv.sql("SELECT * FROM enrolment_codes WHERE code_hash = ?", [_sha256(code)])
    assert len(row) == 1, row
    row = row[0]
    assert row["kind"] == "setup" and row["user_id"] == m.user_id
    assert row["created_by_user_id"] == m.user_id
    assert row["issued_by_device_token_id"] == m.device_id
    ttl = (_parse_ts(row["expires_at"]) - _parse_ts(row["created_at"])).total_seconds()
    assert ttl == SETUP_TTL_S, ttl
    event = _has_event(srv, "setup_code_issued", "user", m.user_id, m.user_id)
    assert event["client_ip"] == issued_from

    assert code not in browser.get(MACHINES).text, "shown once"

    home = kit.loom_home(tmp_path)
    device_name = f"{m.device_name}-second"
    enrolled = _loom(home, "enroll", "--server-url", srv.url, "--device-name", device_name,
                     stdin=f"{srv.url}/ui/join#code={code}\n")
    assert enrolled.returncode == 0, enrolled.stdout + enrolled.stderr
    token = _secret(_loom_config(home)["token"])
    assert _api_status(srv, token) == 200
    owner = srv.sql("SELECT user_id FROM device_tokens WHERE device_name = ?", [device_name])
    assert owner == [{"user_id": m.user_id}], owner
    assert srv.api("GET", "/api/me", token).json()["email"] == m.email
    assert _enrol(srv, code, "replay").status_code == 401, "single use"


def test_a_setup_code_dies_at_its_ttl(legacy_srv):
    """§6.4: the setup code lives `LOOM_SETUP_CODE_TTL_S` (10 min by default); past it the
    real `/api/enroll` refuses it, while a fresh one still redeems."""
    acct = _account(legacy_srv, "ttl")
    browser = _sign_in(legacy_srv, acct.token)
    stale = _issue_setup_code(browser)
    row = legacy_srv.sql("SELECT * FROM enrolment_codes WHERE code_hash = ?", [_sha256(stale)])
    ttl = (_parse_ts(row[0]["expires_at"]) - _parse_ts(row[0]["created_at"])).total_seconds()
    assert ttl == SHORT_SETUP_TTL_S, ttl
    time.sleep(SHORT_SETUP_TTL_S + 2)
    assert _enrol(legacy_srv, stale, f"{acct.device_name}-late").status_code == 401
    fresh_code = _issue_setup_code(browser)
    assert _enrol(legacy_srv, fresh_code, f"{acct.device_name}-quick").status_code == 201


def test_revoking_any_id_of_an_own_chain_redirects_to_my_machines(srv):
    """§6.4 POST /ui/devices/{id}/revoke by the chain's owner: the chain `id` belongs to
    (any id of it) is revoked; `303` to `/ui/machines`; `device_revoked` names the id."""
    m = _account(srv, "selfrevoke")
    token2, id2, _ = _machine(srv, m, "spare")
    renewed = srv.api("POST", "/api/device-tokens/renew", token2)
    assert renewed.status_code == 201, renewed.text
    successor = _secret(renewed.json()["token"])
    browser = _sign_in(srv, m.token)
    _redirected_to(browser.post(revoke_path(id2)), MACHINES)
    assert _api_status(srv, token2) == 401 and _api_status(srv, successor) == 401
    assert _api_status(srv, m.token) == 200
    _has_event(srv, "device_revoked", "device", id2, m.user_id)


def test_an_admin_revokes_any_machine_and_lands_on_admin_users(srv):
    """§6.4 POST /ui/devices/{id}/revoke by an admin: another user's chain, `303` to
    `/ui/admin/users`; the admin's own chain still goes back to `/ui/machines`."""
    a = _account(srv, "revoker", role="admin")
    m = _account(srv, "revokee")
    sa = _sign_in(srv, a.token)
    _redirected_to(sa.post(revoke_path(m.device_id)), ADMIN_USERS)
    assert _api_status(srv, m.token) == 401
    _has_event(srv, "device_revoked", "device", m.device_id, a.user_id)
    _, own_spare, _ = _machine(srv, a, "spare")
    _redirected_to(sa.post(revoke_path(own_spare)), MACHINES)


def test_idor_a_member_revoking_anothers_device_or_an_unknown_id_gets_404(srv):
    """Plan AC "IDOR → 404" / §6.4: "an id that does not exist or belongs to another user
    (when the caller is not an admin) → `404`", and nothing changes."""
    m1 = _account(srv, "idor-a")
    m2 = _account(srv, "idor-b")
    browser = _sign_in(srv, m1.token)
    assert browser.post(revoke_path(m2.device_id)).status_code == 404
    assert browser.post(revoke_path(UNKNOWN_ID)).status_code == 404
    assert _api_status(srv, m2.token) == 200
    assert not [e for e in _events(srv, "device", m2.device_id) if e["event"] == "device_revoked"]


# =========================================================================== #
# Admin → Users                                                                 #
# =========================================================================== #
def test_a_member_gets_403_on_every_admin_route_get_and_post(srv):
    """Plan ACs "members get 403 on admin pages" and "a CLI session has no admin write
    unless it belongs to an admin" / §6.4: "a member on any `/ui/admin/*` route, GET or
    POST → `403 forbidden`", with nothing changed."""
    m = _account(srv, "nosy")
    victim = _account(srv, "nosy-victim")
    browser = _sign_in(srv, m.token)
    _refused(browser.get(ADMIN_USERS), 403, "forbidden")
    email = f"smuggled-{_uid()}@example.invalid"
    _refused(browser.post(INVITE, {"email": email, "name": "smuggled", "role": "admin"}),
             403, "forbidden")
    _refused(browser.post(deactivate_path(victim.user_id)), 403, "forbidden")
    _refused(browser.post(role_path(m.user_id), {"role": "admin"}), 403, "forbidden")
    assert not srv.sql("SELECT id FROM users WHERE email = ?", [email])
    assert _user(srv, victim.email)["active"] == 1 and _api_status(srv, victim.token) == 200
    assert _user(srv, m.email)["role"] == "member"


def test_admin_users_lists_every_user_their_machines_and_the_forms(fresh):
    """§6.4 GET /ui/admin/users: every user (name, email, role, active, pending
    invitation), their machines with a "Revoke" form, an "Invite" form (email, name,
    role), "Deactivate" and "Change role" forms."""
    server = fresh()
    a = _account(server, "boss", role="admin")
    m = _account(server, "staff")
    d = _account(server, "gone")
    assert server.admin("deactivate", "--email", d.email).returncode == 0
    tag = _uid()
    p_email, p_name = f"pending-{tag}@example.invalid", f"pending-{tag}"
    _invite_code(server, p_email, p_name)
    sa = _sign_in(server, a.token)
    r = sa.get(ADMIN_USERS)
    assert r.status_code == 200, r.text[:600]
    page = r.text
    for text in (a.name, a.email, m.name, m.email, d.name, d.email, p_name, p_email,
                 m.device_name):
        assert text in page, text
    emails = [a.email, m.email, d.email, p_email]
    assert "pending" in _segment(page, p_email, [e for e in emails if e != p_email]).lower()
    assert re.search(r"\b(deactivated|inactive|no|false)\b",
                     _segment(page, d.email, [e for e in emails if e != d.email]).lower())
    assert _form_action(page, INVITE)
    for field in ("email", "name", "role"):
        assert re.search(rf"name=[\"']?{field}[\"' ]", page), field
    assert _form_action(page, revoke_path(m.device_id))
    assert _form_action(page, deactivate_path(m.user_id))
    assert _form_action(page, role_path(m.user_id))
    assert re.search(r"name=[\"']?csrf[\"' ]", page) and sa.csrf in page
    assert _sign_in(server, a.token).csrf not in page, "the csrf is per session"
    assert r.headers["cache-control"] == "no-store"


def test_web_invite_of_a_new_email_creates_the_user_and_shows_the_link_once(srv):
    """§6.4 POST /ui/admin/users/invite: `200`, the phase-1 invitation link shown once; an
    unknown email creates the user with the given name and role. The code is an `invite`
    (24 h) recorded with its issuer, it redeems for that user, and `user_invited` names
    the admin."""
    a = _account(srv, "inviter", role="admin")
    sa = _sign_in(srv, a.token)
    for role in ("admin", "member"):
        tag = _uid()
        email, name = f"newbie-{tag}@example.invalid", f"newbie-{tag}"
        r = sa.post(INVITE, {"email": email, "name": name, "role": role})
        assert r.status_code == 200, r.text[:600]
        code = _link_code(srv, r.text)
        user = _user(srv, email)
        assert (user["name"], user["role"], user["active"]) == (name, role, 1)
        row = srv.sql("SELECT * FROM enrolment_codes WHERE code_hash = ?", [_sha256(code)])[0]
        assert row["kind"] == "invite" and row["user_id"] == user["id"]
        assert row["created_by_user_id"] == a.user_id
        assert row["issued_by_device_token_id"] == a.device_id
        ttl = (_parse_ts(row["expires_at"]) - _parse_ts(row["created_at"])).total_seconds()
        assert ttl == INVITE_TTL_S, ttl
        _has_event(srv, "user_invited", "user", user["id"], a.user_id)
        assert code not in sa.get(ADMIN_USERS).text, "shown once"
        redeemed = _enrol(srv, code, f"newbie-{tag}-laptop")
        assert redeemed.status_code == 201 and redeemed.json()["user"]["name"] == name
        _secret(redeemed.json()["token"])


def test_web_invite_of_a_known_email_keeps_its_name_and_role(srv):
    """§6.4 POST /ui/admin/users/invite: "a known one gets a new code and keeps its name
    and role" (`admin.py invite`'s semantics)."""
    a = _account(srv, "reinviter", role="admin")
    k = _account(srv, "known")
    r = _sign_in(srv, a.token).post(INVITE, {"email": k.email, "name": "renamed",
                                             "role": "admin"})
    assert r.status_code == 200, r.text[:600]
    code = _link_code(srv, r.text)
    user = _user(srv, k.email)
    assert (user["name"], user["role"]) == (k.name, "member")
    redeemed = _enrol(srv, code, f"{k.device_name}-2")
    assert redeemed.status_code == 201 and redeemed.json()["user"]["name"] == k.name
    _secret(redeemed.json()["token"])
    assert _device_id(srv, f"{k.device_name}-2") in [
        r["id"] for r in srv.sql("SELECT id FROM device_tokens WHERE user_id = ?", [k.user_id])]


def test_web_deactivate_revokes_chains_expires_every_code_and_ends_sessions(srv):
    """§6.4 POST /ui/admin/users/{id}/deactivate: `admin.py deactivate`'s effects (chains
    revoked, pending codes of every kind expired); `303` to `/ui/admin/users`; the user's
    session refused on its next request; `user_deactivated` names the admin."""
    a = _account(srv, "firer", role="admin")
    m = _account(srv, "fired")
    token2, _, _ = _machine(srv, m, "desk")
    sm = _sign_in(srv, m.token)
    pending_invite = _invite_code(srv, m.email)
    pending_setup = _issue_setup_code(sm)
    _redirected_to(_sign_in(srv, a.token).post(deactivate_path(m.user_id)), ADMIN_USERS)
    assert _user(srv, m.email)["active"] == 0
    assert _api_status(srv, m.token) == 403 and _api_status(srv, token2) == 403
    live = srv.sql("SELECT id FROM device_tokens WHERE user_id = ? AND revoked_at IS NULL",
                   [m.user_id])
    assert live == [], live
    assert sm.get(MACHINES).status_code == 401
    assert _enrol(srv, pending_invite, "after-invite").status_code == 401
    assert _enrol(srv, pending_setup, "after-setup").status_code == 401
    _has_event(srv, "user_deactivated", "user", m.user_id, a.user_id)


def test_web_role_change_is_read_on_the_next_request_of_an_open_session(srv):
    """§6.4 POST /ui/admin/users/{id}/role (`admin` | `member`, `303` to
    `/ui/admin/users`), and the plan AC "role read on every request": the target's OPEN
    session gains and loses the admin pages on its next click. `promoted`/`demoted` name
    the admin. A role outside the two is refused and changes nothing."""
    a = _account(srv, "roler", role="admin")
    m = _account(srv, "roled")
    sa, sm = _sign_in(srv, a.token), _sign_in(srv, m.token)
    _refused(sm.get(ADMIN_USERS), 403, "forbidden")
    _redirected_to(sa.post(role_path(m.user_id), {"role": "admin"}), ADMIN_USERS)
    assert _user(srv, m.email)["role"] == "admin"
    assert sm.get(ADMIN_USERS).status_code == 200
    _has_event(srv, "promoted", "user", m.user_id, a.user_id)
    _redirected_to(sa.post(role_path(m.user_id), {"role": "member"}), ADMIN_USERS)
    _refused(sm.get(ADMIN_USERS), 403, "forbidden")
    _refused(sm.post(INVITE, {"email": f"x-{_uid()}@example.invalid", "name": "x",
                              "role": "member"}), 403, "forbidden")
    _has_event(srv, "demoted", "user", m.user_id, a.user_id)
    bogus = sa.post(role_path(m.user_id), {"role": "root"})
    assert bogus.status_code >= 400, bogus.status_code
    assert _user(srv, m.email)["role"] == "member"


def test_an_admin_py_demotion_ends_admin_rights_on_the_next_request(srv):
    """D20 / §6.4: "Role and `active` are read from `users` on every request, never cached
    in the session": demote an admin while their session is open → the next admin GET and
    POST are 403."""
    _account(srv, "standing", role="admin")
    a2 = _account(srv, "demotee", role="admin")
    s2 = _sign_in(srv, a2.token)
    assert s2.get(ADMIN_USERS).status_code == 200
    assert _demote(srv, a2.email).returncode == 0
    _refused(s2.get(ADMIN_USERS), 403, "forbidden")
    email = f"late-{_uid()}@example.invalid"
    _refused(s2.post(INVITE, {"email": email, "name": "late", "role": "member"}),
             403, "forbidden")
    assert not srv.sql("SELECT id FROM users WHERE email = ?", [email])


def test_an_admin_py_deactivation_refuses_the_next_request_of_an_open_session(srv):
    """D20: "a deactivation takes effect on the next click": the open session's next GET
    and next write are refused by the session gate (401), and no code is issued."""
    m = _account(srv, "deactivee")
    browser = _sign_in(srv, m.token)
    assert browser.get(MACHINES).status_code == 200
    assert srv.admin("deactivate", "--email", m.email).returncode == 0
    before = _codes_of(srv, m.user_id)
    assert browser.get(MACHINES).status_code == 401
    assert browser.post(SETUP_CODE).status_code == 401
    assert _codes_of(srv, m.user_id) == before


# =========================================================================== #
# Sign-in, sign-out, session kinds                                              #
# =========================================================================== #
def test_signout_ends_this_session_only_and_clears_the_cookie(srv):
    """§6.4 POST /ui/signout: "Ends this session (row deleted, cookie cleared); `200`
    'Signed out'"; another session of the same user lives on; `ui_signout` is recorded."""
    m = _account(srv, "leaver")
    s1, s2 = _sign_in(srv, m.token), _sign_in(srv, m.token)
    count = lambda: srv.sql("SELECT COUNT(*) AS n FROM ui_sessions WHERE user_id = ?",  # noqa: E731
                            [m.user_id])[0]["n"]
    before = count()
    r = s1.post(SIGNOUT)
    assert r.status_code == 200 and "signed out" in r.text.lower(), r.text[:600]
    cleared = r.headers.get("set-cookie", "")
    assert f"{SESSION_COOKIE}=" in cleared and re.search(
        r"max-age=0|expires=[^;]*1970", cleared, re.IGNORECASE), cleared
    assert count() == before - 1
    assert not srv.sql("SELECT id FROM ui_sessions WHERE session_hash = ?", [_sha256(s1.cookie)])
    assert s1.get(MACHINES).status_code == 401
    assert s2.get(MACHINES).status_code == 200
    _has_event(srv, "ui_signout", "user", m.user_id, m.user_id)


def test_a_successful_sign_in_is_recorded_without_its_secret(srv):
    """§6.4 `auth_events`: "One row per state change and per successful sign-in":
    `ui_signin` on the user, by the user, from the address that redeemed the code; a
    session opened by `loom ui` is of kind `cli`."""
    m = _account(srv, "signer")
    browser = _sign_in(srv, m.token)
    event = _has_event(srv, "ui_signin", "user", m.user_id, m.user_id)
    assert event["client_ip"] == browser.signin_ip
    kinds = srv.sql("SELECT kind FROM ui_sessions WHERE session_hash = ?",
                    [_sha256(browser.cookie)])
    assert kinds == [{"kind": "cli"}], kinds


def test_a_session_whose_kind_cannot_write_is_refused(srv):
    """§6.4: "a session whose `kind` cannot write → `403 forbidden`" ("Only `cli` sessions
    write in 2a"). 2a has no way to mint another kind, so the row is set by SQL."""
    m = _account(srv, "kinded")
    browser = _sign_in(srv, m.token)
    srv.sql("UPDATE ui_sessions SET kind = 'password' WHERE session_hash = ?",
            [_sha256(browser.cookie)])
    before = _codes_of(srv, m.user_id)
    _refused(browser.post(SETUP_CODE), 403, "forbidden")
    assert _codes_of(srv, m.user_id) == before


# =========================================================================== #
# Refusals, in §6.4's order                                                     #
# =========================================================================== #
def test_without_a_public_url_every_write_is_refused_and_startup_warns(no_url_srv):
    """§6.4: "no resolved public URL → `403 public_url_unset` (startup logs a WARN, and
    every write is refused)" — after the session gate, before Origin/csrf/role; reads
    still work."""
    server = no_url_srv
    m = _account(server, "nourl")
    a = _account(server, "nourl-admin", role="admin")
    sm, sa = _sign_in(server, m.token), _sign_in(server, a.token)
    assert sm.get(MACHINES).status_code == 200
    _refused(sm.post(SETUP_CODE), 403, "public_url_unset")
    _refused(sm.post(SETUP_CODE, origin=None, sec_fetch_site="same-origin"),
             403, "public_url_unset")
    _refused(sm.post(SETUP_CODE, csrf=None, origin="http://evil.example"),
             403, "public_url_unset")
    _refused(sm.post(INVITE, {"email": f"n-{_uid()}@example.invalid", "name": "n",
                              "role": "member"}), 403, "public_url_unset")
    _refused(sa.post(INVITE, {"email": f"n-{_uid()}@example.invalid", "name": "n",
                              "role": "member"}), 403, "public_url_unset")
    _refused(sm.post(revoke_path(m.device_id)), 403, "public_url_unset")
    _refused(sm.post(SIGNOUT), 403, "public_url_unset")
    assert sm.get(MACHINES).status_code == 200, "a refused sign-out leaves the session"
    assert _codes_of(server, m.user_id) == 1 and _api_status(server, m.token) == 200
    anonymous = requests.post(server.url + SETUP_CODE, headers={CLIENT_IP_HEADER: _next_ip()},
                              timeout=30, allow_redirects=False)
    assert anonymous.status_code == 401, "the session gate comes first"
    warned = [line for line in server.logs().splitlines()
              if "WARN" in line and re.search(r"public[ _]url", line, re.IGNORECASE)]
    assert warned, f"startup must WARN about the public URL:\n{server.logs()[-3000:]}"


def test_bad_origins_are_refused_and_same_origin_is_the_fallback(srv):
    """§6.4: "`Origin` present and not the public URL's origin, or absent with
    `Sec-Fetch-Site` other than `same-origin`, or both absent → `403 bad_origin`" (plan
    AC "a cross-origin or header-less POST is rejected"), and nothing is issued; an
    absent Origin with `Sec-Fetch-Site: same-origin` is accepted, and so is the exact
    origin, whatever `Sec-Fetch-Site` says."""
    m = _account(srv, "origins")
    browser = _sign_in(srv, m.token)
    wrong = ["http://evil.example", "null", f"http://127.0.0.1:{srv.port + 1}",
             f"https://127.0.0.1:{srv.port}", f"http://localhost:{srv.port}",
             f"{srv.url}.evil.example", f"http://evil.example/{srv.url}"]
    for origin in wrong:
        _refused(browser.post(SETUP_CODE, origin=origin), 403, "bad_origin")
        _refused(browser.post(SETUP_CODE, origin=origin, sec_fetch_site="same-origin"),
                 403, "bad_origin")
    for site in ("cross-site", "same-site", "none"):
        _refused(browser.post(SETUP_CODE, origin=None, sec_fetch_site=site), 403, "bad_origin")
    _refused(browser.post(SETUP_CODE, origin=None), 403, "bad_origin")
    _refused(browser.post(SIGNOUT, origin=None), 403, "bad_origin")
    assert _codes_of(srv, m.user_id) == 1, "a refused write issued a code"
    assert browser.get(MACHINES).status_code == 200, "a refused sign-out ended the session"

    assert browser.post(SETUP_CODE, origin=None, sec_fetch_site="same-origin").status_code == 200
    assert browser.post(SETUP_CODE).status_code == 200
    assert browser.post(SETUP_CODE, sec_fetch_site="cross-site").status_code == 200
    assert _codes_of(srv, m.user_id) == 4


def test_a_missing_wrong_or_foreign_csrf_is_refused(srv):
    """§6.4: the csrf is the lowercase hex of `HMAC-SHA256(key = the cookie value, msg =
    "csrf")` in the form field `csrf`; "a missing or wrong `csrf` → `403 bad_csrf`" —
    including another session's value, a wrong derivation, and the right value sent
    anywhere but the form field."""
    m = _account(srv, "csrfs")
    other = _account(srv, "csrfs-other")
    browser, twin = _sign_in(srv, m.token), _sign_in(srv, m.token)
    stranger = _sign_in(srv, other.token)
    swapped = hmac.new(CSRF_MESSAGE, browser.cookie.encode(), hashlib.sha256).hexdigest()
    for csrf in (None, "", "0" * 64, twin.csrf, stranger.csrf, swapped,
                 _sha256(browser.cookie + "csrf"), browser.csrf[:-1]):
        _refused(browser.post(SETUP_CODE, csrf=csrf), 403, "bad_csrf")
    in_query = browser.request("POST", f"{SETUP_CODE}?csrf={browser.csrf}", origin=srv.url)
    _refused(in_query, 403, "bad_csrf")
    _refused(browser.post(SIGNOUT, csrf=twin.csrf), 403, "bad_csrf")
    assert _codes_of(srv, m.user_id) == 1
    assert browser.get(MACHINES).status_code == 200
    assert browser.post(SETUP_CODE).status_code == 200


def test_the_refusals_come_in_the_order_of_6_4(srv):
    """§6.4 "Refusals, in this order": 405 → session gate → (public URL) → bad_origin →
    bad_csrf → forbidden → 404 → 409. Each pair below satisfies two refusals at once and
    must get the earlier one."""
    m = _account(srv, "order")
    a = _account(srv, "order-admin", role="admin")
    sm, sa = _sign_in(srv, m.token), _sign_in(srv, a.token)
    anonymous = requests.post(srv.url + SETUP_CODE, timeout=30, allow_redirects=False,
                              headers={CLIENT_IP_HEADER: _next_ip(),
                                       "Origin": "http://evil.example"})
    assert anonymous.status_code == 401, "session gate before Origin"
    _refused(sm.post(SETUP_CODE, csrf="bad", origin="http://evil.example"), 403, "bad_origin")
    _refused(sm.post(deactivate_path(a.user_id), csrf="bad"), 403, "bad_csrf")
    _refused(sm.post(deactivate_path(UNKNOWN_ID)), 403, "forbidden")
    _refused(sm.post(role_path(UNKNOWN_ID), {"role": "admin"}), 403, "forbidden")
    assert sa.post(deactivate_path(UNKNOWN_ID)).status_code == 404
    assert sa.post(role_path(UNKNOWN_ID), {"role": "member"}).status_code == 404
    assert sa.post(revoke_path(UNKNOWN_ID)).status_code == 404
    assert _user(srv, a.email)["active"] == 1


def test_post_only_routes_refuse_other_methods_and_a_get_changes_nothing(srv):
    """Plan "Writes": "Every write is its own POST-only route … no state-changing GET":
    PUT/PATCH/DELETE on every listed POST route are 405, and a GET to one changes nothing
    (no code issued, nobody deactivated or promoted, nothing revoked, still signed in)."""
    a = _account(srv, "methods", role="admin")
    m = _account(srv, "methods-target")
    sa = _sign_in(srv, a.token)
    routes = [SETUP_CODE, revoke_path(m.device_id), INVITE, deactivate_path(m.user_id),
              role_path(m.user_id), SIGNOUT]
    codes_before = _codes_of(srv, a.user_id)
    for path in routes:
        for method in ("PUT", "PATCH", "DELETE"):
            r = sa.request(method, path, data={"csrf": sa.csrf, "role": "admin"},
                           origin=srv.url)
            assert r.status_code == 405, f"{method} {path}: {r.status_code}"
        assert sa.get(path).status_code in (404, 405), path
    assert _codes_of(srv, a.user_id) == codes_before
    assert _user(srv, m.email)["active"] == 1 and _user(srv, m.email)["role"] == "member"
    assert _api_status(srv, m.token) == 200
    assert sa.get(MACHINES).status_code == 200


def test_the_new_get_pages_refuse_write_methods_too(srv):
    """§6.4 "the D14 crawl holds" over the two new pages: POST/PUT/PATCH/DELETE on
    `/ui/machines` and `/ui/admin/users` are 405 (neither is a listed POST route)."""
    a = _account(srv, "crawl-admin", role="admin")
    sa = _sign_in(srv, a.token)
    for path in (MACHINES, ADMIN_USERS):
        assert sa.get(path).status_code == 200, path
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            r = sa.request(method, path, data={"csrf": sa.csrf}, origin=srv.url)
            assert r.status_code == 405, f"{method} {path}: {r.status_code}"


# =========================================================================== #
# The issuer cascade                                                            #
# =========================================================================== #
@dataclass
class Tree:
    user: Account
    a_session: Browser
    b_token: str
    b_renewed: str
    b_session: Browser
    c_token: str
    c_session: Browser
    unredeemed: list[str]
    bystander_token: str
    bystander_id: int
    bystander_session: Browser


def _cascade_tree(server: Server) -> Tree:
    """A's session issues a setup code → machine B; B's issues one → machine C; B renews;
    B's and A's sessions each leave a code unredeemed; Z is a machine of the same user
    that came from an `admin.py` invitation, outside the cascade."""
    u = _account(server, "tree")
    sa = _sign_in(server, u.token)
    b_token, b_id = _enrolled_token(server, _issue_setup_code(sa), f"{u.device_name}-b")
    row = server.sql("SELECT kind, issued_by_device_token_id FROM enrolment_codes "
                     "WHERE device_token_id = ?", [b_id])
    assert row == [{"kind": "setup", "issued_by_device_token_id": u.device_id}], row
    sb = _sign_in(server, b_token)
    c_token, _ = _enrolled_token(server, _issue_setup_code(sb), f"{u.device_name}-c")
    sc = _sign_in(server, c_token)
    renewed = server.api("POST", "/api/device-tokens/renew", b_token)
    assert renewed.status_code == 201, renewed.text
    unredeemed = [_issue_setup_code(sb), _issue_setup_code(sa)]
    z_token, z_id, _ = _machine(server, u, "z")
    tree = Tree(u, sa, b_token, _secret(renewed.json()["token"]), sb, c_token, sc,
                unredeemed, z_token, z_id, _sign_in(server, z_token))
    for token in (u.token, tree.b_token, tree.b_renewed, tree.c_token, z_token):
        assert _api_status(server, token) == 200
    return tree


def _assert_tree_dead(server: Server, tree: Tree) -> None:
    for label, token in (("A", tree.user.token), ("B", tree.b_token),
                         ("B renewed", tree.b_renewed), ("C", tree.c_token)):
        assert _api_status(server, token) == 401, f"machine {label} must be revoked"
    for label, browser in (("A", tree.a_session), ("B", tree.b_session),
                           ("C", tree.c_session)):
        assert browser.get(MACHINES).status_code == 401, f"{label}'s /ui session must end"
    for i, code in enumerate(tree.unredeemed):
        assert _enrol(server, code, f"{tree.user.device_name}-late{i}").status_code == 401
    assert _api_status(server, tree.bystander_token) == 200, "Z is outside the cascade"
    assert tree.bystander_session.get(MACHINES).status_code == 200


def test_the_issuer_cascade_through_a_web_revoke(srv):
    """§6.4 Issuer cascade / D20, through the web's revoke: revoking A's chain revokes B
    and C (minted from setup codes A's and then B's sessions issued) and B's renewal, ends
    their `/ui` sessions, and expires the codes they issued; another chain survives."""
    tree = _cascade_tree(srv)
    _redirected_to(tree.bystander_session.post(revoke_path(tree.user.device_id)), MACHINES)
    _assert_tree_dead(srv, tree)
    _has_event(srv, "device_revoked", "device", tree.user.device_id, tree.user.user_id)


def test_the_issuer_cascade_through_admin_py_revokedevice(srv):
    """§6.4 Issuer cascade: "Inside the `BEGIN IMMEDIATE` of every chain revocation
    (`revokedevice`, …)"."""
    tree = _cascade_tree(srv)
    assert srv.admin("revokedevice", "--id", str(tree.user.device_id)).returncode == 0
    _assert_tree_dead(srv, tree)


def test_the_issuer_cascade_through_a_b2_reenrolment(srv):
    """§6.4 Issuer cascade: "… B2's re-enrolment": `/api/enroll` with A's token as the
    bearer and a new code for the same user revokes A's chain AND what it issued."""
    tree = _cascade_tree(srv)
    code = _invite_code(srv, tree.user.email)
    again = _enrol(srv, code, f"{tree.user.device_name}-again", bearer=tree.user.token)
    assert again.status_code == 201, again.text
    assert _api_status(srv, _secret(again.json()["token"])) == 200
    _assert_tree_dead(srv, tree)


def test_the_cascade_follows_setup_codes_and_expires_any_unredeemed_code(srv):
    """§6.4 Issuer cascade, read literally: it revokes chains minted from a redeemed
    SETUP code, and "expire[s] every unredeemed code those ids issued". A web invitation
    an admin's machine issued and someone redeemed is not a setup code: that user keeps
    working; one still unredeemed dies with the admin's chain."""
    a = _account(srv, "cascade-admin", role="admin")
    sa = _sign_in(srv, a.token)
    tag = _uid()
    redeemed_page = sa.post(INVITE, {"email": f"invited-{tag}@example.invalid",
                                     "name": f"invited-{tag}", "role": "member"})
    assert redeemed_page.status_code == 200, redeemed_page.text[:600]
    invited_token, _ = _enrolled_token(srv, _link_code(srv, redeemed_page.text),
                                       f"invited-{tag}-laptop")
    pending_page = sa.post(INVITE, {"email": f"pending-{tag}@example.invalid",
                                    "name": f"pending-{tag}", "role": "member"})
    assert pending_page.status_code == 200, pending_page.text[:600]
    pending = _link_code(srv, pending_page.text)
    assert srv.admin("revokedevice", "--id", str(a.device_id)).returncode == 0
    assert _api_status(srv, invited_token) == 200
    assert _enrol(srv, pending, f"pending-{tag}-laptop").status_code == 401


def test_a_session_opened_with_a_permanent_token_issues_codes_with_no_issuing_device(srv):
    """§6.4 `enrolment_codes.issued_by_device_token_id`: "`NULL` … from a session opened
    with a permanent token"; `created_by_user_id` still names the user."""
    _, _, user_id, token = _permanent_user(srv, "permanent")
    code = _issue_setup_code(_sign_in(srv, token))
    row = srv.sql("SELECT * FROM enrolment_codes WHERE code_hash = ?", [_sha256(code)])[0]
    assert row["created_by_user_id"] == user_id and row["user_id"] == user_id
    assert row["issued_by_device_token_id"] is None and row["kind"] == "setup"


# =========================================================================== #
# Untrusted text                                                                #
# =========================================================================== #
def test_a_hostile_device_name_and_user_name_are_escaped(srv):
    """Plan AC "a hostile `device_name` is escaped", on `/ui/machines` and
    `/ui/admin/users` (and a hostile name an admin typed into the Invite form)."""
    m = _account(srv, "hostile")
    hostile_device = f"{HOSTILE}-{_uid()}"
    hostile_token, _ = _enrolled_token(srv, _invite_code(srv, m.email), hostile_device)
    assert _api_status(srv, hostile_token) == 200
    a = _account(srv, "hostile-admin", role="admin")
    sa = _sign_in(srv, a.token)
    invited = sa.post(INVITE, {"email": f"x-{_uid()}@example.invalid",
                               "name": f"<b onmouseover=alert(3)>{_uid()}</b>",
                               "role": "member"})
    assert invited.status_code == 200, invited.text[:600]
    _link_code(srv, invited.text)
    escaped = "&lt;script&gt;alert(1)&lt;/script&gt;"
    for page in (_sign_in(srv, m.token).get(MACHINES).text, sa.get(ADMIN_USERS).text):
        assert "<script>alert(1)</script>" not in page
        assert "<img src=x onerror=alert(2)>" not in page
        assert "<b onmouseover" not in page
        assert escaped in page


# =========================================================================== #
# GET /api/me, loom status, loom doctor                                         #
# =========================================================================== #
def test_api_me_has_its_shape_names_the_device_and_is_bearer_only(srv):
    """§6.4 GET /api/me: `{"name", "email", "role", "device_id", "events"}`, `device_id`
    the calling device token (a renewed one names itself) or `null` for a permanent token;
    `/api/*` stays bearer-only (a `/ui` cookie opens nothing there)."""
    m = _account(srv, "me")
    me = srv.api("GET", "/api/me", m.token)
    assert me.status_code == 200, me.text
    body = me.json()
    assert set(body) == ME_KEYS, body
    assert (body["name"], body["email"], body["role"], body["device_id"]) == (
        m.name, m.email, "member", m.device_id)
    assert isinstance(body["events"], list)
    renewed = srv.api("POST", "/api/device-tokens/renew", m.token)
    assert renewed.status_code == 201
    successor = _secret(renewed.json()["token"])
    successor_id = srv.sql("SELECT id FROM device_tokens WHERE token_hash = ?",
                           [_sha256(successor)])[0]["id"]
    assert srv.api("GET", "/api/me", successor).json()["device_id"] == successor_id

    name, email, _, permanent = _permanent_user(srv, "me-permanent")
    body = srv.api("GET", "/api/me", permanent).json()
    assert (body["name"], body["email"], body["role"], body["device_id"]) == (
        name, email, "member", None)

    assert srv.api("GET", "/api/me").status_code == 401
    browser = _sign_in(srv, m.token)
    cookie_only = srv.api("GET", "/api/me",
                          headers={"Cookie": f"{SESSION_COOKIE}={browser.cookie}"})
    assert cookie_only.status_code == 401


def test_api_me_returns_the_twenty_newest_events_about_the_caller(srv):
    """§6.4 GET /api/me `events`: "the 20 most recent rows about the caller (target the
    user, or one of their device tokens), newest first, each `{"ts", "event", "actor",
    "target_type", "target_id"}`, `actor` a name or `null`"."""
    a = _account(srv, "me-admin", role="admin")
    x = _account(srv, "me-busy")
    _, spare_id, _ = _machine(srv, x, "spare")
    for _ in range(12):
        _promote(srv, x.email)
        assert _demote(srv, x.email).returncode == 0
    _redirected_to(_sign_in(srv, a.token).post(revoke_path(spare_id)), ADMIN_USERS)
    events = srv.api("GET", "/api/me", x.token).json()["events"]
    assert len(events) == EVENTS_LIMIT, len(events)
    for event in events:
        assert set(event) == EVENT_KEYS, event
        assert (event["target_type"], event["target_id"]) in {
            ("user", x.user_id), ("device", x.device_id), ("device", spare_id)}, event
    assert {k: events[0][k] for k in ("event", "actor", "target_type", "target_id")} == {
        "event": "device_revoked", "actor": a.name, "target_type": "device",
        "target_id": spare_id}
    expected = ["demoted", "promoted"] * 10
    assert [e["event"] for e in events[1:]] == expected[:EVENTS_LIMIT - 1]
    assert all(e["actor"] is None and e["target_id"] == x.user_id for e in events[1:])
    stamps = [_parse_ts(e["ts"]) for e in events]
    assert stamps == sorted(stamps, reverse=True), "newest first"


def test_loom_status_and_doctor_show_the_account(srv, tmp_path):
    """§6.4 Client: `loom status` prints `account: <name> (<role>)` and the recent events;
    `loom doctor` adds an `account` row, ✓ with the role, ⚠ once an event of the last 7
    days was made by someone else (here `admin.py`)."""
    tag = _uid()
    name, email = f"cli-{tag}", f"cli-{tag}@example.invalid"
    home = kit.loom_home(tmp_path)
    enrolled = _loom(home, "enroll", "--server-url", srv.url, "--device-name",
                     f"cli-{tag}-laptop", stdin=_invite_code(srv, email, name) + "\n")
    assert enrolled.returncode == 0, enrolled.stdout + enrolled.stderr
    _secret(_loom_config(home)["token"])

    status = _loom(home, "status")
    assert f"account: {name} (member)" in status.stdout, status.stdout + status.stderr
    doctor = _loom(home, "doctor")
    rows = [line for line in doctor.stdout.splitlines() if "account" in line.lower()]
    assert rows and rows[0].startswith("✓") and "member" in rows[0], doctor.stdout

    _promote(srv, email)
    status = _loom(home, "status")
    assert f"account: {name} (admin)" in status.stdout, status.stdout + status.stderr
    assert re.search("promot", status.stdout, re.IGNORECASE), status.stdout
    doctor = _loom(home, "doctor")
    rows = [line for line in doctor.stdout.splitlines() if "account" in line.lower()]
    assert rows and rows[0].startswith("⚠"), doctor.stdout


# =========================================================================== #
# auth_events retention, and no secret anywhere                                 #
# =========================================================================== #
def test_auth_events_older_than_the_retention_are_deleted_at_startup(fresh):
    """§6.4 `auth_events`: "rows older than `LOOM_AUTH_EVENTS_RETENTION_DAYS` (default
    180) are deleted at startup" — and younger ones are kept."""
    server = fresh()
    m = _account(server, "retained")
    _sign_in(server, m.token)
    now = datetime.now(timezone.utc)
    for days, event in ((RETENTION_DAYS + 20, "promoted"), (RETENTION_DAYS - 80, "demoted")):
        server.sql("INSERT INTO auth_events (ts, actor_user_id, event, target_type, "
                   "target_id, client_ip) VALUES (?, NULL, ?, 'user', ?, NULL)",
                   [_iso(now - timedelta(days=days)), event, m.user_id])
    server.restart()
    kept = {e["event"] for e in _events(server, "user", m.user_id)}
    assert "promoted" not in kept, "a row older than 180 days must be gone"
    assert {"demoted", "ui_signin"} <= kept, kept


def test_no_secret_in_any_log_line_or_event_row(srv):
    """Plan AC "no secret in any log line", and §6.4 "No event row ever holds a secret":
    every token, code and cookie this wave saw — this test's own full flow included — is
    absent from every container's `docker logs` and from every `auth_events` row."""
    a = _account(srv, "logs-admin", role="admin")
    sa = _sign_in(srv, a.token)
    m = _account(srv, "logs")
    sm = _sign_in(srv, m.token)
    _enrolled_token(srv, _issue_setup_code(sm), f"{m.device_name}-2")
    invited = sa.post(INVITE, {"email": f"logs-{_uid()}@example.invalid", "name": "logs-inv",
                               "role": "member"})
    _link_code(srv, invited.text)
    _redirected_to(sa.post(role_path(m.user_id), {"role": "admin"}), ADMIN_USERS)
    _redirected_to(sa.post(role_path(m.user_id), {"role": "member"}), ADMIN_USERS)
    assert sm.post(SIGNOUT).status_code == 200
    _redirected_to(sa.post(deactivate_path(m.user_id)), ADMIN_USERS)

    assert len(SECRETS) >= 8
    logs = "\n".join([srv.logs(), *(s.logs() for s in list(_LIVE) if s is not srv),
                      *RETIRED_LOGS])
    leaked = sorted(s[:6] + "…" for s in SECRETS if s in logs)
    assert not leaked, f"secrets in docker logs (prefixes): {leaked}"
    rows = json.dumps(srv.sql("SELECT * FROM auth_events"))
    leaked = sorted(s[:6] + "…" for s in SECRETS if s in rows)
    assert not leaked, f"secrets in auth_events (prefixes): {leaked}"
