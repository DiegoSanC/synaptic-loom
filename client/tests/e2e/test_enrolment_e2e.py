"""Enrolment and device-token renewal across a REAL expiry (issue #20).

Same real boundary as ``test_phase2_e2e.py`` — a live ``uvicorn server.app:app``
subprocess on a temp ``LOOM_DB``, the real ``server.admin`` CLI, the real ``loom`` CLI
as subprocesses, the real ``gitleaks`` — and the same isolation rules (temp HOME /
LOOM_HOME / DB / port, an explicit ``sweep.transcripts_root``).

What is proven here and nowhere else:

- the operator's ``invite`` prints a code and no token; ``loom enroll`` reads that code
  at a hidden prompt (the subprocess runs in a NEW SESSION so ``getpass`` cannot reach
  the developer's terminal) and the device token lands only in ``config.json``;
- with ``LOOM_DEVICE_TOKEN_TTL_S=10`` the token really expires in ten seconds, and
  **both** unattended entry points — ``loom process --quiet`` (the 15-min trigger) and
  ``python -m loom.processor --quiet`` (the SessionEnd kick) — renew it silently while
  it still lives and keep capturing after the original token is dead;
- once the operator revokes the machine, the next unattended pass records the
  rejection and ``loom status`` / ``loom doctor`` shout it.

No clock is mocked: every wait below is against the real ``token_expires_at``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

from test_phase2_e2e import (  # the same real boundary and helpers, not a copy of them
    REPO_ROOT,
    Server,
    _free_port,
    _git_repo,
    _loom,
    _loom_home,
    _processor_log,
    _write_config,
    _write_transcript,
    _transcript_lines,
    _claude_stub,
    CLEAN_SUMMARY,
    REMOTE_URL,
)

TOKEN_TTL_S = 20          # a real twenty-second device token life (10 s left the CI runner ~2 s to renew, summarize, scan and post)
RENEW_FRACTION = 0.25     # the client renews below this share of the life (5 s here)
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{32,}$")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(shutil.which("gitleaks") is None,
                       reason="e2e needs the real gitleaks binary (scanner engine 'auto')"),
]


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _admin(db: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "server.admin", *args],
                          env={**os.environ, "LOOM_DB": str(db)}, cwd=str(REPO_ROOT),
                          capture_output=True, text=True, timeout=60)


def _parse_code(stdout: str) -> str:
    codes = [line.strip() for line in stdout.splitlines() if CODE_RE.fullmatch(line.strip())]
    assert len(codes) == 1, stdout
    return codes[0]


def _enroll(home: Path, server_url: str, code: str, device_name: str) -> subprocess.CompletedProcess:
    """The real ``loom enroll`` with the code on stdin, in a NEW SESSION.

    ``getpass`` opens ``/dev/tty`` when it can; a child started with ``start_new_session``
    has no controlling terminal, so it falls back to stdin and the test never blocks on
    (or types into) the developer's terminal.
    """
    return subprocess.run(
        [sys.executable, "-m", "loom.cli", "enroll", "--server-url", server_url,
         "--device-name", device_name],
        input=code + "\n", env={**os.environ, "LOOM_HOME": str(home)}, cwd=str(REPO_ROOT),
        capture_output=True, text=True, timeout=120, start_new_session=True,
    )


def _config(home: Path) -> dict:
    return json.loads((home / "config.json").read_text(encoding="utf-8"))


def _remaining_s(home: Path) -> float:
    expires = datetime.fromisoformat(_config(home)["token_expires_at"].replace("Z", "+00:00"))
    return (expires - datetime.now(timezone.utc)).total_seconds()


def _wait_until_remaining_below(home: Path, seconds: float) -> None:
    """Sleep until the CURRENT token has less than ``seconds`` of real life left."""
    while _remaining_s(home) >= seconds:
        time.sleep(0.1)


def _drop_marker(home: Path, repo: Path, transcripts_root: Path, session_id: str) -> None:
    transcript = _write_transcript(transcripts_root / "-e2e" / f"{session_id}.jsonl",
                                   _transcript_lines(str(repo), session_id))
    (home / "queue" / f"{session_id}.json").write_text(json.dumps({
        "session_id": session_id, "transcript_path": str(transcript), "cwd": str(repo),
        "reason": "prompt_input_exit",
        "marked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }), encoding="utf-8")


def _timer_pass(home: Path) -> None:
    """What the periodic trigger runs (cron block / systemd unit)."""
    cp = _loom(["process", "--quiet"], home)
    assert cp.returncode == 0, f"{cp.stdout}\n{cp.stderr}"


def _kick_pass(home: Path) -> None:
    """What the SessionEnd hook launches, argv for argv (`sessionend.sh` §6)."""
    cp = subprocess.run([sys.executable, "-m", "loom.processor", "--quiet"],
                        env={**os.environ, "LOOM_HOME": str(home)}, cwd=str(REPO_ROOT),
                        capture_output=True, text=True, timeout=300)
    assert cp.returncode == 0, f"{cp.stdout}\n{cp.stderr}"


def _get(url: str, token: str, path: str) -> requests.Response:
    return requests.get(f"{url}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=10)


@pytest.fixture
def short_lived_server(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_DEVICE_TOKEN_TTL_S", str(TOKEN_TTL_S))  # inherited by uvicorn
    db = tmp_path / "server" / "loom.db"
    db.parent.mkdir(parents=True)
    srv = Server(db, _free_port())
    try:
        yield srv
    finally:
        srv.terminate()


# --------------------------------------------------------------------------- #
# The scenario                                                                 #
# --------------------------------------------------------------------------- #
def test_enrol_then_both_unattended_paths_renew_across_real_expiries(tmp_path, short_lived_server):
    server = short_lived_server
    home = _loom_home(tmp_path)
    repo = _git_repo(tmp_path / "repo")
    transcripts_root = tmp_path / "transcripts"
    transcripts_root.mkdir()
    stub = _claude_stub(tmp_path / "claude_stub.py", stdout=CLEAN_SUMMARY)
    # A config that predates enrolment (say, a repointed phase-1 client): enroll must
    # merge into it, replacing only the credential and the URL.
    _write_config(home, server_url="http://127.0.0.1:1", token="placeholder-never-valid",
                  claude_bin=stub, transcripts_root=transcripts_root)

    # --- Operator: an invitation. The code travels; no token is ever printed. -------
    cp = _admin(server.db, "invite", "--email", "e2e@example.test", "--name", "e2e", "--ttl", "1h")
    assert cp.returncode == 0, cp.stderr
    assert "TOKEN" not in cp.stdout.replace("ENROLMENT CODE", "")
    code = _parse_code(cp.stdout)

    # --- Engineer: enrol at a hidden prompt; the token lands in config.json only. -----
    enrolled = _enroll(home, server.url, code, "e2e-mac")
    assert enrolled.returncode == 0, f"{enrolled.stdout}\n{enrolled.stderr}"
    assert "Enrolled" in enrolled.stdout and "e2e" in enrolled.stdout
    cfg = _config(home)
    token1 = cfg["token"]
    assert token1 != "placeholder-never-valid" and token1 not in enrolled.stdout
    assert cfg["server_url"] == server.url
    assert cfg["claude_bin"] == stub and cfg["sweep"]["transcripts_root"] == str(transcripts_root)
    assert stat.S_IMODE((home / "config.json").stat().st_mode) == 0o600
    assert 0 < _remaining_s(home) <= TOKEN_TTL_S
    # The code is burned: a second machine cannot reuse it.
    assert _enroll(_loom_home(tmp_path / "other"), server.url, code, "thief").returncode == 1

    project_id = requests.post(f"{server.url}/api/projects", json={"remote_url": REMOTE_URL},
                               headers={"Authorization": f"Bearer {token1}"}, timeout=10).json()["id"]

    # --- D9: enrolment lands the machine in opt-in mode. Registered by "the team" (the
    # POST above) is not enough: until THIS machine enables the repo, a pass skips it —
    # no summarizer, no upload — and says so in status. Then `loom enable` in the repo.
    assert _config(home)["capture_mode"] == "opt_in"
    _drop_marker(home, repo, transcripts_root, "sess-0")
    _kick_pass(home)
    assert "\tdropped_not_enabled\t" in _processor_log(home)
    assert not (home / "queue" / "sess-0.json").exists()
    assert _get(server.url, token1, f"/api/projects/{project_id}/entries").json()["entries"] == []
    status = _loom(["status"], home)
    assert "1 session(s) in " in status.stdout and "not enabled on this machine" in status.stdout
    enabled = subprocess.run([sys.executable, "-m", "loom.cli", "enable"],
                             env={**os.environ, "LOOM_HOME": str(home)}, cwd=str(repo),
                             capture_output=True, text=True, timeout=60)
    assert enabled.returncode == 0, f"{enabled.stdout}\n{enabled.stderr}"
    assert "enabled on this machine" in enabled.stdout

    # --- Pass 1 (kick path), fresh token: captures, does not renew. ------------------
    _drop_marker(home, repo, transcripts_root, "sess-1")
    _kick_pass(home)
    assert _config(home)["token"] == token1
    assert "token_renewed" not in _processor_log(home)

    # --- Pass 2 (timer path) with < 25 % of life left: renews silently and captures. --
    _wait_until_remaining_below(home, TOKEN_TTL_S * RENEW_FRACTION - 0.5)
    _drop_marker(home, repo, transcripts_root, "sess-2")
    _timer_pass(home)
    token2 = _config(home)["token"]
    assert token2 != token1
    assert _processor_log(home).count("token_renewed") == 1
    assert stat.S_IMODE((home / "config.json").stat().st_mode) == 0o600

    # --- The ORIGINAL token really expires; the kick path keeps capturing regardless. --
    _wait_expired = time.time()
    while _get(server.url, token1, "/api/projects").status_code == 200:
        assert time.time() - _wait_expired < TOKEN_TTL_S + 5, "token1 never expired"
        time.sleep(0.2)
    assert _get(server.url, token1, "/api/projects").status_code == 401
    _drop_marker(home, repo, transcripts_root, "sess-3")
    _kick_pass(home)

    # --- Pass 4 (kick path) renews the SECOND token; pass 5 (timer path) outlives it. --
    _wait_until_remaining_below(home, TOKEN_TTL_S * RENEW_FRACTION - 0.5)
    _drop_marker(home, repo, transcripts_root, "sess-4")
    _kick_pass(home)
    token3 = _config(home)["token"]
    assert token3 not in (token1, token2)
    assert _processor_log(home).count("token_renewed") == 2
    _wait_expired = time.time()
    while _get(server.url, token2, "/api/projects").status_code == 200:
        assert time.time() - _wait_expired < TOKEN_TTL_S + 5, "token2 never expired"
        time.sleep(0.2)
    _drop_marker(home, repo, transcripts_root, "sess-5")
    _timer_pass(home)

    # --- Every pass landed its entry, under the enrolled user, with nothing rejected. --
    entries = _get(server.url, token3, f"/api/projects/{project_id}/entries").json()["entries"]
    assert sorted(e["session_id"] for e in entries) == ["sess-1", "sess-2", "sess-3", "sess-4", "sess-5"]
    assert {e["author"] for e in entries} == {"e2e"}
    assert list((home / "queue").glob("*.json")) == []
    assert not (home / "auth_rejected.json").exists()
    assert "auth_rejected" not in _processor_log(home)

    listed = _admin(server.db, "listdevices", "--email", "e2e@example.test").stdout
    rows = [l for l in listed.splitlines() if "e2e-mac" in l]
    assert len(rows) == 3, listed
    assert [l.split()[1] for l in rows] == ["replaced", "replaced", "active"]
    assert token3 not in listed

    # --- The operator revokes the machine: the next unattended pass is LOUD. ----------
    revoked_id = rows[-1].split()[0]
    assert _admin(server.db, "revokedevice", "--id", revoked_id).returncode == 0
    _drop_marker(home, repo, transcripts_root, "sess-6")
    _timer_pass(home)  # exit 0 — an unattended pass never crashes over a credential

    rejected = json.loads((home / "auth_rejected.json").read_text(encoding="utf-8"))
    assert rejected["status"] == 401
    assert (home / "queue" / "sess-6.json").exists()  # kept, not lost
    status = _loom(["status"], home)
    assert status.returncode == 0 and "AUTH REJECTED" in status.stdout and "loom enroll" in status.stdout
    doctor = _loom(["doctor"], home)
    assert doctor.returncode == 1
    assert any(l.startswith("✗") and "rejected the token" in l and "loom enroll" in l
               for l in doctor.stdout.splitlines()), doctor.stdout
