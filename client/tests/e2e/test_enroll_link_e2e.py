"""`loom enroll` takes the invitation link (D19), against the REAL server.

Same real boundary as ``test_enrolment_e2e.py``: a live ``uvicorn server.app:app`` on a
temp ``LOOM_DB`` and the real ``server.admin invite``. The full flow is the operator's and
the invitee's, in order: ``invite`` with ``LOOM_PUBLIC_URL`` set prints the LINK line; a
plain HTTP GET of ``/ui/join`` (what the browser does with that link: the fragment, and so
the code, is never sent); the real ``loom enroll`` on a PSEUDO-TERMINAL, so ``getpass``
reads from its ``/dev/tty`` with echo off exactly as on a person's machine, the link is
pasted at the hidden prompt and the host question answered y; then the real
``loom register`` in a repo, and an authenticated call that answers 200. A second link
re-enrols the same machine, and the token it replaces answers 401 from then on (B2).

The refusal test runs ``loom enroll`` in a NEW SESSION without a terminal, so ``getpass``
falls back to stdin: it only needs the pasted value, never a prompt answered in order.
"""
from __future__ import annotations

import fcntl
import os
import pty
import select
import subprocess
import sys
import termios
import time

import pytest
import requests

from test_enrolment_e2e import _admin, _config, _parse_code
from test_phase2_e2e import REPO_ROOT, Server, _free_port, _git_repo, _loom_home

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


def _controlling_terminal() -> None:
    """In the child, after ``start_new_session``: make its stdin (the pty) its terminal."""
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class _Terminal:
    """The real ``loom`` CLI on a pseudo-terminal, typed into the way a person types:
    each answer only once its prompt is on the screen (``getpass`` flushes what was typed
    before it turned echo off). ``screen`` is everything the terminal displayed."""

    def __init__(self, home, *args: str, cwd=REPO_ROOT) -> None:
        master, slave = pty.openpty()
        self.fd, self.screen = master, ""
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "loom.cli", *args],
            stdin=slave, stdout=slave, stderr=slave,
            env={**os.environ, "LOOM_HOME": str(home)}, cwd=str(cwd),
            start_new_session=True, preexec_fn=_controlling_terminal,
        )
        os.close(slave)

    def _read(self, timeout_s: float) -> bool:
        ready, _, _ = select.select([self.fd], [], [], timeout_s)
        if not ready:
            return True
        try:
            chunk = os.read(self.fd, 4096)
        except OSError:  # EIO: the child closed its side
            chunk = b""
        self.screen += chunk.decode("utf-8", "replace")
        return bool(chunk)

    def answer(self, prompt: str, typed: str, timeout_s: float = 60) -> None:
        deadline = time.time() + timeout_s
        while prompt not in self.screen:
            assert time.time() < deadline and self.proc.poll() is None, self.screen
            self._read(0.2)
        os.write(self.fd, typed.encode() + b"\n")

    def finish(self, timeout_s: float = 120) -> int:
        deadline = time.time() + timeout_s
        while self._read(0.2) or self.proc.poll() is None:
            assert time.time() < deadline, self.screen
        os.close(self.fd)
        return self.proc.wait(timeout=10)


def test_the_invite_link_opens_the_join_page_enrols_on_a_terminal_and_registers(
        tmp_path, live_server, monkeypatch):
    # --- Operator: `invite` on a server whose public URL is known prints the link. -----
    monkeypatch.setenv("LOOM_PUBLIC_URL", live_server.url)
    cp = _admin(live_server.db, "invite", "--email", "link@example.test", "--name", "linked",
                "--ttl", "1h")
    assert cp.returncode == 0, cp.stderr
    code = _parse_code(cp.stdout)
    links = [line for line in cp.stdout.splitlines() if "/ui/join" in line]
    assert links == [f"{live_server.url}/ui/join#code={code}"], cp.stdout  # D19's format
    link = links[0]

    # --- Invitee's browser: the page opens anonymously; the code never reaches it. ----
    page = requests.get(link, timeout=10)  # a client drops the fragment, as a browser does
    assert page.status_code == 200, page.text
    assert page.headers["content-type"].startswith("text/html")
    assert "loom enroll" in page.text and "<script" not in page.text.lower()
    assert code not in page.text

    # --- Invitee's terminal: paste the whole link, confirm the host. -----------------
    home = _loom_home(tmp_path)
    term = _Terminal(home, "enroll", "--device-name", "link-mac")  # no --server-url
    term.answer("invitation link", link)
    term.answer("[y/N]: ", "y")
    rc = term.finish()

    assert rc == 0, term.screen
    assert f"    127.0.0.1:{live_server.port}\r\n" in term.screen  # the host, on its own line
    assert "Enrolled" in term.screen and "linked" in term.screen
    assert code not in term.screen  # echo was off at the hidden prompt: nothing displayed it
    cfg = _config(home)
    assert cfg["server_url"] == live_server.url
    assert cfg["token"] not in term.screen

    # --- And the machine works: it registers a project and reads it back. -------------
    repo = _git_repo(tmp_path / "repo")
    registered = subprocess.run([sys.executable, "-m", "loom.cli", "register"],
                                env={**os.environ, "LOOM_HOME": str(home)}, cwd=str(repo),
                                capture_output=True, text=True, timeout=120)
    assert registered.returncode == 0, registered.stdout + registered.stderr
    assert "Registered" in registered.stdout
    listed = requests.get(f"{live_server.url}/api/projects", timeout=10,
                          headers={"Authorization": f"Bearer {cfg['token']}"})
    assert listed.status_code == 200
    projects = listed.json()["projects"]
    assert [p["created_by"] for p in projects] == ["linked"]
    assert projects[0]["canonical_remote"] == "github.com/example-group/phase2-e2e"

    # --- Re-enrolling the same machine with the same server (B2): no host question, and
    # the token it replaces stops working at once instead of living out its 90 days. --
    again = _admin(live_server.db, "invite", "--email", "link@example.test", "--ttl", "1h")
    relink = next(line for line in again.stdout.splitlines() if "/ui/join" in line)
    term = _Terminal(home, "enroll", "--device-name", "link-mac")
    term.answer("invitation link", relink)
    assert term.finish() == 0, term.screen
    assert "[y/N]" not in term.screen
    renewed = _config(home)["token"]
    assert renewed != cfg["token"]
    statuses = [requests.get(f"{live_server.url}/api/projects", timeout=10,
                             headers={"Authorization": f"Bearer {t}"}).status_code
                for t in (cfg["token"], renewed)]
    assert statuses == [401, 200]


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
