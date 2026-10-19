"""`GET /ui/join`, the page an invitation link opens (D19, plan 2026-10-19).

The link is `<public-url>/ui/join#code=<code>`. A browser never sends a fragment, so the
code never reaches this server: the page is static, has no script and no form, and only
tells the invitee how to install the client and to paste the whole link into
`loom enroll`. Driven by plain HTTP GETs against a live uvicorn whose access log is on,
and against the app in process for what a live log would itself record. Every expected
value is a literal.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
JOIN_HEADERS = {
    # SECURITY_HEADERS' policy without script-src and connect-src, and with no form target.
    "content-security-policy": (
        "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'none'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "cache-control": "no-store",
    "strict-transport-security": "max-age=31536000; includeSubDomains",
}


class LoggedServer:
    """A live uvicorn on loopback whose access log (query strings included) is a file."""

    def __init__(self, tmp: Path) -> None:
        self.db = tmp / "loom.db"
        self.log = tmp / "uvicorn.log"
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("LOOM_") and k != "FLY_APP_NAME"}
        self.env["LOOM_DB"] = str(self.db)
        self._log_file = self.log.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "server.app:app", "--host", "127.0.0.1",
             "--port", str(port), "--log-level", "info"],
            env=self.env, cwd=str(REPO_ROOT), stdout=self._log_file, stderr=subprocess.STDOUT)
        deadline = time.time() + 30
        while _get(f"{self.url}/api/health")[0] != 200:
            assert time.time() < deadline and self.proc.poll() is None, self.log.read_text()
            time.sleep(0.2)

    def invite(self) -> str:
        """The operator's `invite`, for this server; returns the link it printed."""
        done = subprocess.run(
            [sys.executable, "-m", "server.admin", "invite", "--email", "dev2@example.test",
             "--name", "dev2", "--public-url", self.url],
            env=self.env, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr
        return done.stdout.splitlines()[-1]

    def terminate(self) -> str:
        """Stop the server; return its whole log."""
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=15)
        self._log_file.close()
        return self.log.read_text(encoding="utf-8")


def _get(url: str) -> tuple[int, dict[str, str], str]:
    """A plain GET. Like a browser, urllib keeps a URL's fragment to itself."""
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read().decode()
    except urllib.error.URLError:
        return 0, {}, ""


def _post_json(url: str, body: bytes) -> int:
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.fixture
def server(tmp_path):
    srv = LoggedServer(tmp_path)
    try:
        yield srv
    finally:
        srv.terminate()


def test_the_invitation_link_opens_a_public_page_with_no_script_and_no_form(server):
    link = server.invite()
    base, fragment = link.split("#", 1)
    assert base == f"{server.url}/ui/join"
    code = fragment.removeprefix("code=")

    status, headers, body = _get(link)       # no cookie, no bearer: an anonymous visitor

    assert status == 200
    assert headers["content-type"].startswith("text/html")
    for name, value in JOIN_HEADERS.items():
        assert headers.get(name) == value, name
    page = body.lower()
    assert "<script" not in page and "<form" not in page and "<input" not in page
    assert "bash client/install.sh" in body
    assert "Paste this whole link into <code>loom enroll</code>" in body
    assert code not in body


def test_opening_the_page_leaves_the_code_unredeemed(server):
    link = server.invite()
    code = link.split("#code=", 1)[1]
    assert _get(link)[0] == 200
    assert _get(link)[0] == 200

    body = ('{"code": "%s", "device_name": "laptop"}' % code).encode()
    assert _post_json(f"{server.url}/api/enroll", body) == 201


def test_the_server_log_never_carries_the_code(server):
    link = server.invite()
    code = link.split("#code=", 1)[1]
    assert _get(link)[0] == 200
    body = ('{"code": "%s", "device_name": "laptop"}' % code).encode()
    assert _post_json(f"{server.url}/api/enroll", body) == 201

    log = server.terminate()
    assert "GET /ui/join HTTP/1.1" in log and "POST /api/enroll" in log, "the access log is on"
    assert code not in log


# --------------------------------------------------------------------------- #
# In process: what a live access log would record if the test sent it          #
# --------------------------------------------------------------------------- #
def test_the_page_is_the_same_whatever_the_request_carries(client):
    """It never reads a code: a query string or a stray cookie changes nothing."""
    plain = client.get("/ui/join")
    assert plain.status_code == 200
    probe = "a-code-in-the-wrong-place-0123456789abcdef"
    for response in (client.get(f"/ui/join?code={probe}"),
                     client.get("/ui/join", cookies={"loom_ui": probe}),
                     client.get("/ui/join", headers={"Authorization": f"Bearer {probe}"})):
        assert response.status_code == 200
        assert response.text == plain.text
        assert probe not in response.text
        assert response.headers["content-security-policy"] == JOIN_HEADERS["content-security-policy"]


def test_the_page_accepts_no_write(client):
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert client.request(method, "/ui/join").status_code == 405, method


def test_the_other_ui_pages_keep_the_shared_policy(client):
    """The override is for this page alone: the login page still loads its script."""
    csp = client.get("/ui/login").headers["content-security-policy"]
    assert "script-src 'self'" in csp and "form-action 'self'" in csp
