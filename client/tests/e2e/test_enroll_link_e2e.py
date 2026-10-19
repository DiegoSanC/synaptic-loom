"""`loom enroll` takes the invitation link (D19), against the REAL server.

Same real boundary as ``test_enrolment_e2e.py``: a live ``uvicorn server.app:app`` on a
temp ``LOOM_DB``, the real ``server.admin invite``, and the real ``loom enroll`` as a
subprocess in a NEW SESSION, so ``getpass`` falls back to stdin and the y/N answer is the
next stdin line, exactly in the order a person types them.

The link is built here from the server's URL and the code ``invite`` prints, in the
format the plan fixes (``<public-url>/ui/join#code=<code>``): ``invite``'s own link line
and the ``/ui/join`` page belong to the server slice, and the full invite → page → enrol
flow is asserted once both have landed.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest
import requests

from test_enrolment_e2e import _admin, _config, _parse_code
from test_phase2_e2e import REPO_ROOT, Server, _free_port, _loom_home

pytestmark = [pytest.mark.e2e]


@pytest.fixture
def live_server(tmp_path):
    db = tmp_path / "server" / "loom.db"
    db.parent.mkdir(parents=True)
    srv = Server(db, _free_port())
    try:
        yield srv
    finally:
        srv.terminate()


def _invite(server: Server) -> str:
    cp = _admin(server.db, "invite", "--email", "link@example.test", "--name", "linked", "--ttl", "1h")
    assert cp.returncode == 0, cp.stderr
    return _parse_code(cp.stdout)


def _enroll(home, stdin: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "loom.cli", "enroll", "--device-name", "link-mac", *extra],
        input=stdin, env={**os.environ, "LOOM_HOME": str(home)}, cwd=str(REPO_ROOT),
        capture_output=True, text=True, timeout=120, start_new_session=True,
    )


def test_a_pasted_link_enrols_after_the_host_is_confirmed(tmp_path, live_server):
    code = _invite(live_server)
    link = f"{live_server.url}/ui/join#code={code}"
    home = _loom_home(tmp_path)

    cp = _enroll(home, f"{link}\ny\n")  # no --server-url: the link carries it

    shown = cp.stdout + cp.stderr
    assert cp.returncode == 0, shown
    assert f"    127.0.0.1:{live_server.port}\n" in cp.stdout and "[y/N]" in cp.stdout
    assert code not in shown
    cfg = _config(home)
    assert cfg["server_url"] == live_server.url
    assert requests.get(f"{live_server.url}/api/projects", timeout=10,
                        headers={"Authorization": f"Bearer {cfg['token']}"}).status_code == 200


def test_a_link_that_contradicts_server_url_never_reaches_the_server(tmp_path, live_server):
    code = _invite(live_server)
    link = f"http://localhost:{live_server.port}/ui/join#code={code}"  # same machine, other origin
    home = _loom_home(tmp_path)

    refused = _enroll(home, f"{link}\n", "--server-url", live_server.url)

    assert refused.returncode == 1 and "--server-url" in refused.stderr
    assert code not in refused.stdout + refused.stderr
    assert not (home / "config.json").exists()
    # Nothing was sent: the single-use code is still unredeemed and enrols as a bare code.
    enrolled = _enroll(home, f"{code}\n", "--server-url", live_server.url)
    assert enrolled.returncode == 0, enrolled.stdout + enrolled.stderr
