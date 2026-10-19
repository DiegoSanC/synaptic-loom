"""A renewal whose 201 never reaches the client (D19 B1b), through the REAL client path.

The server mints a successor, but the answer is lost on the way back: a test-owned proxy
between the real ``loom`` CLI and the real ``uvicorn server.app:app`` forwards every
request, and for the first ``POST /api/device-tokens/renew`` it forwards the request,
keeps the server's 201 for the test, and closes the client's connection without a reply.
Nothing of the product is mocked: the client's ``_renew_device_token`` sees exactly what a
dropped connection looks like, and the server is the real one on a temp ``LOOM_DB``.

What is proven:

- the pass that lost the 201 keeps its token and still captures;
- the next pass renews with that same (old) token, the server re-issues — revoking the
  successor nobody holds — and capture continues under the new token;
- revoking the machine by an id the client never saw (the lost successor's) makes every
  token of the machine 401.

Real time, as in ``test_enrolment_e2e.py``: the token lives ``TOKEN_TTL_S`` seconds and
renewal is due once less than a quarter of that is left.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from test_enrolment_e2e import (  # the same real boundary and helpers, not a copy of them
    RENEW_FRACTION,
    _admin,
    _config,
    _drop_marker,
    _enroll,
    _get,
    _kick_pass,
    _parse_code,
    _timer_pass,
    _wait_until_remaining_below,
)
from test_phase2_e2e import (
    CLEAN_SUMMARY,
    REMOTE_URL,
    Server,
    _claude_stub,
    _free_port,
    _git_repo,
    _loom_home,
    _processor_log,
    _write_config,
)

TOKEN_TTL_S = 30  # renewal is due below 7.5 s left; the two passes run inside that window
RENEW_PATH = "/api/device-tokens/renew"
# Not forwarded: hop-by-hop, recomputed, or (content-encoding) already decoded by requests.
_HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "content-length", "host",
               "content-encoding"}

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(shutil.which("gitleaks") is None,
                       reason="e2e needs the real gitleaks binary"),
]


class _LossyProxy:
    """Forwards everything to ``upstream``; the first renew's response never comes back.

    ``lost`` holds the bodies the server answered and the client never received.
    """

    def __init__(self, upstream: str) -> None:
        self.upstream = upstream
        self.lost: list[dict] = []
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _forward(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP_BY_HOP}
                resp = requests.request(self.command, f"{proxy.upstream}{self.path}",
                                        headers=headers, data=body, timeout=30,
                                        allow_redirects=False)
                if self.path == RENEW_PATH and not proxy.lost:
                    assert resp.status_code == 201, resp.text  # the server DID mint it
                    proxy.lost.append(resp.json())
                    self.close_connection = True  # the 201 dies here, unanswered
                    return
                self.send_response(resp.status_code)
                for key, value in resp.headers.items():
                    if key.lower() not in _HOP_BY_HOP:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(resp.content)))
                self.end_headers()
                self.wfile.write(resp.content)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _forward

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def lossy(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_DEVICE_TOKEN_TTL_S", str(TOKEN_TTL_S))  # inherited by uvicorn
    db = tmp_path / "server" / "loom.db"
    db.parent.mkdir(parents=True)
    srv = Server(db, _free_port())
    proxy = _LossyProxy(srv.url)
    try:
        yield srv, proxy
    finally:
        proxy.close()
        srv.terminate()


def _device_ids(server: Server) -> dict[str, list[str]]:
    """``state → [id, …]`` of the one machine, from the operator's real listing."""
    listed = _admin(server.db, "listdevices", "--email", "lossy@example.test").stdout
    by_state: dict[str, list[str]] = {}
    for line in listed.splitlines():
        if "lossy-mac" in line:
            token_id, state = line.split()[:2]
            by_state.setdefault(state, []).append(token_id)
    return by_state


def test_a_lost_renew_201_is_retried_with_the_old_token_and_capture_continues(tmp_path, lossy):
    server, proxy = lossy
    home = _loom_home(tmp_path)
    repo = _git_repo(tmp_path / "repo")
    transcripts_root = tmp_path / "transcripts"
    transcripts_root.mkdir()
    stub = _claude_stub(tmp_path / "claude_stub.py", stdout=CLEAN_SUMMARY)
    _write_config(home, server_url="http://127.0.0.1:1", token="placeholder-never-valid",
                  claude_bin=stub, transcripts_root=transcripts_root)

    cp = _admin(server.db, "invite", "--email", "lossy@example.test", "--name", "lossy", "--ttl", "1h")
    assert cp.returncode == 0, cp.stderr
    enrolled = _enroll(home, proxy.url, _parse_code(cp.stdout), "lossy-mac")  # all traffic via the proxy
    assert enrolled.returncode == 0, f"{enrolled.stdout}\n{enrolled.stderr}"
    old = _config(home)["token"]
    project_id = requests.post(f"{server.url}/api/projects", json={"remote_url": REMOTE_URL},
                               headers={"Authorization": f"Bearer {old}"}, timeout=10).json()["id"]
    enabled = subprocess.run([sys.executable, "-m", "loom.cli", "enable"],
                             env={**os.environ, "LOOM_HOME": str(home)}, cwd=str(repo),
                             capture_output=True, text=True, timeout=60)
    assert enabled.returncode == 0, f"{enabled.stdout}\n{enabled.stderr}"

    # --- Pass 1: renewal is due; the server mints S1 and the 201 is lost. -------------
    _wait_until_remaining_below(home, TOKEN_TTL_S * RENEW_FRACTION - 0.5)
    _drop_marker(home, repo, transcripts_root, "sess-1")
    _timer_pass(home)

    assert len(proxy.lost) == 1
    lost = proxy.lost[0]["token"]
    assert lost != old
    assert _config(home)["token"] == old  # nothing was persisted
    log = _processor_log(home)
    assert "token_renewal_failed" in log and "token_renewed" not in log

    # --- Pass 2: the same old token renews again; the server re-issues. --------------
    _drop_marker(home, repo, transcripts_root, "sess-2")
    _kick_pass(home)

    fresh = _config(home)["token"]
    assert fresh not in (old, lost)
    assert _processor_log(home).count("token_renewed") == 1
    assert _get(server.url, fresh, "/api/projects").status_code == 200
    assert _get(server.url, lost, "/api/projects").status_code == 401  # nobody holds it: revoked
    entries = _get(server.url, fresh, f"/api/projects/{project_id}/entries").json()["entries"]
    assert sorted(e["session_id"] for e in entries) == ["sess-1", "sess-2"]  # capture continued
    assert not (home / "auth_rejected.json").exists()

    # --- The operator revokes the machine by the id nobody ever held: all of it dies. --
    ids = _device_ids(server)
    assert len(ids.get("revoked", [])) == 1 and len(ids.get("active", [])) == 1, ids
    assert _admin(server.db, "revokedevice", "--id", ids["revoked"][0]).returncode == 0
    assert [_get(server.url, t, "/api/projects").status_code for t in (old, lost, fresh)] == [401] * 3
    _drop_marker(home, repo, transcripts_root, "sess-3")
    _timer_pass(home)
    assert json.loads((home / "auth_rejected.json").read_text(encoding="utf-8"))["status"] == 401
