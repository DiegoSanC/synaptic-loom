"""`loom ui` across the REAL boundary (D14, plan 2026-09-29 slice 6: AC-16).

A live ``uvicorn server.app:app`` (access log ON, written to a file), an entry captured by
a REAL ``loom process`` pass (``test_phase2_e2e.Client``: real git repo, real transcript,
real gitleaks; only the Claude CLI is a stub), the REAL ``loom ui`` as a subprocess with
``BROWSER`` pointing at a recorder script, and then what ``login.js`` does: read the
fragment, ``POST /ui/session``, follow ``next``.

The browser is the seam this project does not own; everything else is real. Every
expected value is a literal.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs

import pytest
import requests

from test_phase2_e2e import (  # the same real boundary and helpers, not a copy of them
    CLEAN_SUMMARY,
    REPO_ROOT,
    Client,
    _adduser,
    _free_port,
    _register_project,
    _wait_health,
)

pytestmark = pytest.mark.e2e

PROJECT = "phase2-e2e"          # the name the server derives from test_phase2_e2e's remote


class LoggedServer:
    """A live uvicorn whose access log (query strings included) lands in a file."""

    def __init__(self, tmp: Path) -> None:
        self.db = tmp / "server" / "loom.db"
        self.db.parent.mkdir(parents=True)
        self.log = tmp / "server" / "uvicorn.log"
        port = _free_port()
        self.url = f"http://127.0.0.1:{port}"
        self._log_file = self.log.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "server.app:app", "--host", "127.0.0.1",
             "--port", str(port), "--log-level", "info"],
            env={**os.environ, "LOOM_DB": str(self.db)}, cwd=str(REPO_ROOT),
            stdout=self._log_file, stderr=subprocess.STDOUT)
        _wait_health(self.url)

    def terminate(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=15)
        self._log_file.close()


@pytest.fixture
def server(tmp_path):
    srv = LoggedServer(tmp_path)
    try:
        yield srv
    finally:
        srv.terminate()


def _recorder(tmp: Path) -> tuple[Path, Path]:
    """A `BROWSER` that writes the URL it was asked to open, and opens nothing."""
    out = tmp / "opened.txt"
    script = tmp / "browser.py"
    script.write_text(f"#!{sys.executable}\nimport sys\n"
                      f"open({str(out)!r}, 'a').write(sys.argv[1] + '\\n')\n", encoding="utf-8")
    script.chmod(0o755)
    return script, out


def test_loom_ui_signs_a_browser_in_on_a_real_capture(tmp_path, server):
    token = _adduser(server.db, "e2e-ui")
    project_id = _register_project(server.url, token)
    client = Client(tmp_path, server, token, summary=CLEAN_SUMMARY)
    client.orphan("sess-ui")
    client.process()
    browser, opened = _recorder(tmp_path)

    cp = subprocess.run([sys.executable, "-m", "loom.cli", "ui", PROJECT],
                        env={**os.environ, "LOOM_HOME": str(client.home),
                             "BROWSER": str(browser)},
                        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60)

    assert cp.returncode == 0, cp.stderr
    url = opened.read_text(encoding="utf-8").strip()
    assert f"\n  {url}\n" in cp.stdout
    base, fragment = url.split("#", 1)
    assert base == f"{server.url}/ui/login"
    params = parse_qs(fragment)
    assert params["next"] == [f"/ui/projects/{project_id}"]
    code = params["code"][0]

    # What login.js does with the fragment.
    login_page = requests.get(base, timeout=10)
    assert login_page.status_code == 200 and "/ui/static/login.js" in login_page.text
    opened_session = requests.post(f"{server.url}/ui/session", json={"code": code}, timeout=10)
    assert opened_session.status_code == 204, opened_session.text
    cookie = opened_session.cookies["loom_ui"]
    # `Secure` is right for Fly (HTTPS); on this loopback http the jar would drop it, so
    # present it by hand, as a browser on https would.
    page = requests.get(f"{server.url}{params['next'][0]}",
                        headers={"Cookie": f"loom_ui={cookie}"}, timeout=10)
    assert page.status_code == 200, page.text
    assert "Signed in as e2e-ui" in page.text
    assert "Keep the orphan sweep inside the processor lock" in page.text

    # The code works once, and it never reached the server's access log.
    again = requests.post(f"{server.url}/ui/session", json={"code": code}, timeout=10)
    assert again.status_code == 401
    server.terminate()
    log = server.log.read_text(encoding="utf-8")
    assert "POST /ui/session" in log and "GET /ui/login" in log, "the access log is on"
    assert code not in log and cookie not in log
