"""Phase-2 end-to-end regression suite (PLAN §T-phase2, epic E4).

The repo's OWN e2e, run by ``make test`` / ``pytest`` alongside the unit and integration
suites. It exercises the phase-2 capture chain across a **real boundary**:

- a REAL ``uvicorn server.app:app`` subprocess on a random free port with a temp ``LOOM_DB``;
- REAL ``python -m server.admin`` for the user/token, the REAL HTTP API for everything else;
- REAL git repos (the processor's remote gate runs ``git remote get-url origin``);
- the REAL ``gitleaks`` binary (engine ``auto``);
- REAL ``loom`` CLI subprocesses (``process``, ``push-quarantine``, ``init``, ``doctor``);
- the REAL ``client/hooks/sessionend.sh`` and the REAL ``client/install.sh``.

Only the seams this project does not own are faked: the Claude CLI (a stub binary that
prints a deterministic summary — we are testing capture, not the LLM) and the host
schedulers (PATH shims).

**Isolation is a hard requirement of this file.** Every test runs with a temp ``HOME``,
a temp ``LOOM_HOME``, a temp ``LOOM_DB``, a random port and — critically — an explicit
``sweep.transcripts_root`` under ``tmp_path``: :class:`loom.config.Config` defaults that
root to the developer's real ``~/.claude/projects``, so a config without it would walk
(and enqueue markers for) the machine's actual sessions. Anything that reaches a host
scheduler runs with ``crontab``/``systemctl`` resolved to shims through a PATH that does
not contain the real ones, so the user's crontab is never read and never written.

Relationship to ``test/waves/c/test_goal_e4_e2e.py``: that file is the frozen phase gate
and stays untouched. This one is the durable regression suite — same real boundary, wider
coverage (hook-vs-sweep equivalence, entry shape, pointer chaining across passes, the
secret never reaching the server's storage, a pre-existing crontab entry surviving the
install) and no dependency on the wave goal kit.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_PROMPT = REPO_ROOT / "prompts" / "summarizer_v1.md"
INSTALL_SH = REPO_ROOT / "client" / "install.sh"
SESSIONEND_SH = REPO_ROOT / "client" / "hooks" / "sessionend.sh"

REMOTE_URL = "git@github.com:example-group/phase2-e2e.git"
CANONICAL_REMOTE = "github.com/example-group/phase2-e2e"

# A high-entropy token that the real gitleaks catches (rule generic-api-key) and that the
# phase-1 regex fallback provably does not — it is the whole point of AC-2.3a. Planted in a
# summary, it must reach quarantine and never the server.
PLANTED_SECRET = "zX9j2kQ8vB4nM7cL1wR5tY6uI3oP0aSdFgHjKlZxCvB"
PLANTED_SECRET_LINE = f'api_key = "{PLANTED_SECRET}"'

CLEAN_SUMMARY = (
    "## Decision\n"
    "- **Keep the orphan sweep inside the processor lock** — it cannot race the kick.\n"
)
LEAKY_SUMMARY = (
    "## Decision\n"
    "- **Rotate the pilot credential** — it was pasted into the session.\n"
    f"{PLANTED_SECRET_LINE}\n"
)

# The e2e boundary needs the real scanner and the hook's JSON tool; without them the
# scenarios would silently degrade into a weaker test instead of failing loudly.
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        shutil.which("gitleaks") is None,
        reason="phase-2 e2e needs the real gitleaks binary (scanner engine 'auto')",
    ),
    pytest.mark.skipif(
        shutil.which("jq") is None,
        reason="phase-2 e2e runs the real SessionEnd hook and install.sh, both need jq",
    ),
]


# --------------------------------------------------------------------------- #
# Real server subprocess                                                       #
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(db_path: Path, port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        env={**os.environ, "LOOM_DB": str(db_path)}, cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _wait_health(url: str, timeout_s: float = 30.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if requests.get(f"{url}/api/health", timeout=2).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.2)
    raise TimeoutError(f"server at {url} never became healthy")


def _wait_down(url: str, timeout_s: float = 15.0) -> None:
    """Blocks until the port stops answering (a half-dead server would fake AC-2.2a)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            requests.get(f"{url}/api/health", timeout=1)
        except requests.RequestException:
            return
        time.sleep(0.1)
    raise TimeoutError(f"server at {url} is still answering")


class Server:
    """Handle on the live uvicorn subprocess (restartable on the same DB and port)."""

    def __init__(self, db: Path, port: int) -> None:
        self.db = db
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.proc = _start_server(db, port)
        _wait_health(self.url)

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=15)
        _wait_down(self.url)

    def start(self) -> None:
        self.proc = _start_server(self.db, self.port)
        _wait_health(self.url)

    def terminate(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=15)

    # --- storage ------------------------------------------------------------
    def storage_bytes(self) -> bytes:
        """Every byte the server has on disk (DB + WAL + SHM).

        Lets a test assert that a secret never reached the server at all, not merely
        that it is absent from the API's projection of it.
        """
        blob = b""
        for path in sorted(self.db.parent.iterdir()):
            if path.is_file():
                blob += path.read_bytes()
        return blob


@pytest.fixture
def server(tmp_path):
    db = tmp_path / "server" / "loom.db"
    db.parent.mkdir(parents=True)
    srv = Server(db, _free_port())
    try:
        yield srv
    finally:
        srv.terminate()


# --------------------------------------------------------------------------- #
# Server-side setup through the real seams                                     #
# --------------------------------------------------------------------------- #
def _adduser(db: Path, name: str) -> str:
    """Real ``python -m server.admin adduser``; returns the one-shot token."""
    out = subprocess.run(
        [sys.executable, "-m", "server.admin", "adduser",
         "--name", name, "--email", f"{name}@e2e.test"],
        env={**os.environ, "LOOM_DB": str(db)}, cwd=str(REPO_ROOT),
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout
    for line in out.splitlines():
        candidate = line.strip()
        if len(candidate) >= 32 and re.fullmatch(r"[A-Za-z0-9_-]+", candidate):
            return candidate
    raise AssertionError(f"no token in adduser output:\n{out}")


def _register_project(url: str, token: str) -> int:
    resp = requests.post(f"{url}/api/projects", json={"remote_url": REMOTE_URL},
                         headers={"Authorization": f"Bearer {token}"}, timeout=10)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _entries(url: str, token: str, project_id: int) -> list[dict]:
    resp = requests.get(f"{url}/api/projects/{project_id}/entries",
                        headers={"Authorization": f"Bearer {token}"}, timeout=10)
    assert resp.status_code == 200, resp.text
    return resp.json()["entries"]


# --------------------------------------------------------------------------- #
# Client-side setup (temp LOOM_HOME, real git repo, real transcripts)          #
# --------------------------------------------------------------------------- #
def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", REMOTE_URL], cwd=path, check=True)
    return path


def _transcript_lines(cwd: str, session_id: str, *, turn: int = 1) -> list[dict]:
    """Realistic JSONL per contract/SCHEMA.md: metadata first, then conversation.

    The metadata line deliberately carries no ``cwd``: the sweep must derive the session's
    directory from the conversational lines, never from the encoded directory name (AC-2.1c).
    """
    return [
        {"type": "file-history-snapshot", "messageId": f"m{turn}", "snapshot": {}},
        {"type": "user", "uuid": f"{session_id}-u{turn}", "cwd": cwd,
         "sessionId": session_id,
         "message": {"role": "user", "content": f"Turn {turn}: refactor the auth flow"}},
        {"type": "assistant", "uuid": f"{session_id}-a{turn}", "cwd": cwd,
         "sessionId": session_id,
         "message": {"role": "assistant",
                     "content": [{"type": "text",
                                  "text": f"Turn {turn}: decided to keep bearer tokens."}]}},
    ]


def _write_transcript(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return path


def _append_transcript(path: Path, lines: list[dict]) -> Path:
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


def _settle(path: Path, seconds_ago: float = 7200) -> None:
    """Backdates mtime past ``sweep.settle_s`` — the sweep's liveness heuristic (Q-2.2)."""
    ts = time.time() - seconds_ago
    os.utime(path, (ts, ts))


def _claude_stub(path: Path, *, stdout: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\n"
        f"sys.stdout.write({stdout!r})\nsys.exit(0)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path)


def _loom_home(tmp: Path) -> Path:
    home = tmp / "loom_home"
    for sub in ("queue", "quarantine", "failed", "log", "prompts", "hooks"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REAL_PROMPT, home / "prompts" / "summarizer_v1.md")
    return home


def _write_config(home: Path, *, server_url: str, token: str, claude_bin: str,
                  transcripts_root: Path) -> Path:
    """``config.json`` with the phase-2 keys. ``transcripts_root`` is NOT optional here."""
    cfg = {
        "server_url": server_url,
        "token": token,
        "cache_ttl_s": 3600,
        "summarizer": {"model": "sonnet", "timeout_s": 300, "prompt_version": "v1"},
        "claude_bin": claude_bin,
        "prompt_path": str(home / "prompts" / "summarizer_v1.md"),
        "sweep": {"enabled": True, "settle_s": 1800,
                  "transcripts_root": str(transcripts_root)},
        "scanner": {"engine": "auto", "gitleaks_version": None},
    }
    path = home / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def _loom(args: list[str], home: Path, *, timeout: int = 300) -> subprocess.CompletedProcess:
    """Runs the real CLI as the timer would (``loom <cmd>``) against a temp LOOM_HOME."""
    return subprocess.run([sys.executable, "-m", "loom.cli", *args],
                          env={**os.environ, "LOOM_HOME": str(home)}, cwd=str(REPO_ROOT),
                          capture_output=True, text=True, timeout=timeout)


def _markers(home: Path) -> list[Path]:
    return sorted((home / "queue").glob("*.json"))


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _state(home: Path) -> dict:
    path = home / "state.json"
    return _read_json(path) if path.exists() else {}


def _processor_log(home: Path) -> str:
    path = home / "log" / "processor.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


class Client:
    """A configured temp client: LOOM_HOME + git repo + transcripts root + claude stub."""

    def __init__(self, tmp_path: Path, server: Server, token: str, *, summary: str) -> None:
        self.home = _loom_home(tmp_path)
        self.repo = _git_repo(tmp_path / "repo")
        self.transcripts_root = tmp_path / "transcripts"
        self.transcripts_root.mkdir(parents=True, exist_ok=True)
        self.stub = _claude_stub(tmp_path / "claude_stub.py", stdout=summary)
        _write_config(self.home, server_url=server.url, token=token,
                      claude_bin=self.stub, transcripts_root=self.transcripts_root)

    def orphan(self, session_id: str, *, encoded_dir: str = "-Users-nobody-elsewhere") -> Path:
        """A crashed session's transcript: content on disk, NO marker, already settled."""
        path = self.transcripts_root / encoded_dir / f"{session_id}.jsonl"
        _write_transcript(path, _transcript_lines(str(self.repo), session_id))
        _settle(path)
        return path

    def process(self) -> subprocess.CompletedProcess:
        """One ``loom process`` pass — exactly what the 15-min trigger runs."""
        cp = _loom(["process"], self.home)
        assert cp.returncode == 0, f"loom process failed:\n{cp.stdout}\n{cp.stderr}"
        return cp


# --------------------------------------------------------------------------- #
# 1. Swept orphan + server outage (AC-2.1a, AC-2.1c, AC-2.2a)                  #
# --------------------------------------------------------------------------- #
def test_swept_orphan_survives_a_server_outage_and_lands_on_the_next_pass(tmp_path, server):
    """A killed session is captured by the sweep even while the server is unreachable.

    The work must not be lost: the pass enqueues the synthetic marker, keeps it because the
    registry cannot be decided, advances nothing — and the next pass (the one the timer
    fires 15 minutes later) delivers it.
    """
    token = _adduser(server.db, "e2e-orphan")
    project_id = _register_project(server.url, token)
    client = Client(tmp_path, server, token, summary=CLEAN_SUMMARY)
    client.orphan("sess-orphan")

    server.stop()
    pass1 = client.process()

    markers = _markers(client.home)
    assert len(markers) == 1, "the sweep must enqueue even with the server down"
    marker = _read_json(markers[0])
    assert marker["session_id"] == "sess-orphan"
    assert marker["origin"] == "sweep" and marker["reason"] == "sweep"
    # AC-2.1c: the cwd comes from the transcript's own lines. The encoded directory name
    # ("-Users-nobody-elsewhere") points somewhere else entirely, so this only passes if the
    # sweep read the content.
    assert marker["cwd"] == str(client.repo)
    assert "kept_server_unreachable" in pass1.stdout, pass1.stdout
    # Nothing was consumed: the pointer must not move for work that never left the machine.
    assert _state(client.home) == {}
    # The pass still records itself, which is what doctor's sweep-age row reads.
    assert (client.home / "sweep_last_run.json").exists()

    server.start()
    pass2 = client.process()

    entries = _entries(server.url, token, project_id)
    assert len(entries) == 1, f"the deferred work must land:\n{pass2.stdout}"
    entry = entries[0]
    assert entry["session_id"] == "sess-orphan"
    assert entry["covered_from"] is None
    assert entry["covered_to"] == "sess-orphan-a1"
    assert "Keep the orphan sweep inside the processor lock" in entry["content_md"]
    assert _markers(client.home) == []
    assert _state(client.home), "the pointer must advance once the entry is confirmed"


def test_entries_are_extractable_through_the_api_in_the_documented_shape(tmp_path, server):
    """The captured entry is retrievable as a self-contained OKF concept (D7 / §5.1)."""
    token = _adduser(server.db, "e2e-shape")
    project_id = _register_project(server.url, token)
    client = Client(tmp_path, server, token, summary=CLEAN_SUMMARY)
    client.orphan("sess-shape")
    client.process()

    entries = _entries(server.url, token, project_id)
    assert len(entries) == 1
    entry = entries[0]
    # Block T (E15) made the curation fields unconditional, so the phase-2 key set is
    # now a SUBSET, not the whole shape: every entry carries links_in/links_out/status
    # (empty + "current" when nothing links it) so a consumer never has to guess a
    # default. `snippet`/`rank` stay query-only and must NOT appear in a plain listing.
    assert set(entry) >= {"id", "author", "session_id", "created_at",
                          "covered_from", "covered_to", "content_md"}
    assert set(entry) <= {"id", "author", "session_id", "created_at", "covered_from",
                          "covered_to", "content_md", "links_in", "links_out", "status"}
    assert (entry["links_in"], entry["links_out"], entry["status"]) == ([], [], "current")
    assert entry["author"] == "e2e-shape"

    head, body = entry["content_md"].split("---\n", 2)[1:]
    assert head.startswith("type: decision-log-entry")
    assert f'project: "{CANONICAL_REMOTE}"' in head
    assert 'covered_from: null' in head
    assert '    covered_to: "sess-shape-a1"' in head
    assert '    model: "sonnet"' in head and '    prompt: "v1"' in head
    assert body.lstrip().startswith("## Decision")


# --------------------------------------------------------------------------- #
# 2. Capture-path equivalence: hook marker vs swept orphan                     #
# --------------------------------------------------------------------------- #
def _normalize_entry(content_md: str, session_id: str) -> str:
    """Erases the identifiers that legitimately differ between two distinct sessions."""
    out = content_md.replace(session_id, "SESSION")
    out = re.sub(r'timestamp: "[^"]+"', 'timestamp: "TS"', out)
    return out


def test_hook_marked_and_swept_sessions_produce_equivalent_entries(tmp_path, server):
    """The capture path must not change the product.

    Two sessions with identical content: one closed cleanly (the REAL SessionEnd hook wrote
    the marker) and one killed (no marker; the sweep has to find it). One ``loom process``
    pass handles both, and the two entries must be the same document modulo their session
    identifiers.
    """
    token = _adduser(server.db, "e2e-equiv")
    project_id = _register_project(server.url, token)
    client = Client(tmp_path, server, token, summary=CLEAN_SUMMARY)

    swept = client.orphan("sess-swept")
    hooked = client.transcripts_root / "-Users-nobody-elsewhere" / "sess-hooked.jsonl"
    _write_transcript(hooked, _transcript_lines(str(client.repo), "sess-hooked"))
    _settle(hooked)

    # The REAL hook, fed the payload Claude Code sends on SessionEnd. There is no venv
    # python under the temp LOOM_HOME, so the hook writes its marker and skips the kick —
    # which is precisely the situation the periodic trigger exists to cover.
    payload = json.dumps({"session_id": "sess-hooked", "transcript_path": str(hooked),
                          "cwd": str(client.repo), "reason": "prompt_input_exit"})
    hook = subprocess.run(["bash", str(SESSIONEND_SH)], input=payload,
                          env={**os.environ, "LOOM_HOME": str(client.home)},
                          capture_output=True, text=True, timeout=60)
    assert hook.returncode == 0, hook.stderr

    hook_marker = _read_json(client.home / "queue" / "sess-hooked.json")
    assert "origin" not in hook_marker, "a hook marker carries no sweep provenance"
    assert hook_marker["reason"] == "prompt_input_exit"

    client.process()

    # The sweep enqueued the orphan and left the already-marked session alone (it must never
    # overwrite a hook marker, which may carry a fresher cwd/reason).
    log = _processor_log(client.home)
    assert "sess-swept\tsweep_enqueued" in log
    assert "sess-hooked\tsweep_enqueued" not in log

    entries = {e["session_id"]: e for e in _entries(server.url, token, project_id)}
    assert set(entries) == {"sess-swept", "sess-hooked"}
    assert _normalize_entry(entries["sess-swept"]["content_md"], "sess-swept") == \
        _normalize_entry(entries["sess-hooked"]["content_md"], "sess-hooked")
    assert entries["sess-swept"]["covered_from"] is None
    assert entries["sess-hooked"]["covered_from"] is None
    assert _markers(client.home) == []
    assert str(swept) in _state(client.home) and str(hooked) in _state(client.home)


# --------------------------------------------------------------------------- #
# 3. Planted secret, end to end (AC-2.3a, AC-2.3b)                             #
# --------------------------------------------------------------------------- #
def test_planted_secret_is_quarantined_and_only_uploaded_after_redaction(tmp_path, server):
    """A real high-entropy secret in a summary never reaches the server (I4).

    It is quarantined locally; ``loom push-quarantine`` refuses it until it is redacted and
    then delivers it (201). The assertion is not "the API does not show it" but "the secret
    is nowhere in the server's storage".
    """
    token = _adduser(server.db, "e2e-secret")
    project_id = _register_project(server.url, token)
    client = Client(tmp_path, server, token, summary=LEAKY_SUMMARY)
    client.orphan("sess-leak")

    scan_pass = client.process()
    assert "quarantined" in scan_pass.stdout, scan_pass.stdout
    assert _entries(server.url, token, project_id) == []
    assert PLANTED_SECRET.encode() not in server.storage_bytes(), \
        "the secret must never have reached the server's storage"

    quarantined = sorted((client.home / "quarantine").glob("*.json"))
    assert len(quarantined) == 1
    record = _read_json(quarantined[0])
    assert record["reason"] == "secret_scan"
    assert record["findings"], "the quarantine record must name what was found"
    for finding in record["findings"]:
        assert PLANTED_SECRET not in finding["redacted"], \
            "quarantine metadata must not repeat the secret verbatim"
    assert PLANTED_SECRET in record["entry"]["content_md"], \
        "the quarantined payload keeps the secret on purpose, for local review"
    # The marker is resolved, not left spinning: a quarantine is a decided outcome.
    assert _markers(client.home) == []
    assert _state(client.home), "the pointer advances on quarantine (I2/I6)"

    # AC-2.3b: push-quarantine rescans with the SAME engine and refuses a dirty entry.
    refused = _loom(["push-quarantine", quarantined[0].name], client.home)
    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert quarantined[0].exists(), "a refused push must not delete the evidence"
    assert _entries(server.url, token, project_id) == []

    record["entry"]["content_md"] = record["entry"]["content_md"].replace(
        PLANTED_SECRET, "[REDACTED]")
    quarantined[0].write_text(json.dumps(record), encoding="utf-8")

    pushed = _loom(["push-quarantine", quarantined[0].name], client.home)
    assert pushed.returncode == 0, pushed.stdout + pushed.stderr

    entries = _entries(server.url, token, project_id)
    assert len(entries) == 1 and "[REDACTED]" in entries[0]["content_md"]
    assert not list((client.home / "quarantine").glob("*.json"))
    assert PLANTED_SECRET.encode() not in server.storage_bytes()


# --------------------------------------------------------------------------- #
# 4. Backpressure: repeated passes never duplicate (AC-2.1b, I6)               #
# --------------------------------------------------------------------------- #
def test_repeated_passes_do_not_duplicate_and_chain_the_pointer(tmp_path, server):
    """The timer fires every 15 minutes forever; that must be free of side effects.

    Re-running over settled, fully-consumed transcripts produces nothing new (pointer +
    ``content_hash``), while genuinely new content on the same transcript produces exactly
    one incremental entry whose ``covered_from`` is the previous entry's ``covered_to``.
    """
    token = _adduser(server.db, "e2e-idem")
    project_id = _register_project(server.url, token)
    client = Client(tmp_path, server, token, summary=CLEAN_SUMMARY)
    transcript = client.orphan("sess-idem")

    client.process()
    first = _entries(server.url, token, project_id)
    assert len(first) == 1
    pointer = _state(client.home)[str(transcript)]
    assert pointer["last_line"] == 3 and pointer["last_uuid"] == "sess-idem-a1"

    for _ in range(3):
        client.process()
        assert len(_entries(server.url, token, project_id)) == 1
        assert _markers(client.home) == [], "a settled, consumed transcript is not re-swept"
    assert _state(client.home)[str(transcript)]["last_line"] == 3

    # The session is resumed and dies again: new lines, same transcript.
    _append_transcript(transcript, _transcript_lines(str(client.repo), "sess-idem", turn=2))
    _settle(transcript)

    client.process()
    entries = _entries(server.url, token, project_id)
    assert len(entries) == 2, "new content must produce exactly one new entry"
    incremental = next(e for e in entries if e["id"] != first[0]["id"])
    assert incremental["covered_from"] == first[0]["covered_to"] == "sess-idem-a1"
    assert incremental["covered_to"] == "sess-idem-a2"
    assert _state(client.home)[str(transcript)]["last_line"] == 6

    for _ in range(2):
        client.process()
        assert len(_entries(server.url, token, project_id)) == 2


# --------------------------------------------------------------------------- #
# 5. Fresh install → doctor green (AC-2.4a, AC-2.4c)                           #
# --------------------------------------------------------------------------- #
def _write_shim(shims: Path, name: str, body: str) -> Path:
    path = shims / name
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _isolated_path(tmp_path: Path, shims: Path) -> str:
    """PATH with the shims first and NO real ``crontab``/``systemctl`` anywhere behind them.

    install.sh and doctor resolve the host schedulers through PATH by design, so this is the
    seam that keeps a test run off the developer's real crontab and user manager. The tail is
    an explicit allowlist of binaries symlinked into a private directory — ``crontab`` and
    ``systemctl`` are simply not in it — plus the real ``gitleaks``, which the scanner row
    must genuinely probe.
    """
    safe = tmp_path / "safe_bin"
    safe.mkdir(parents=True, exist_ok=True)
    allowed = ("python3", "git", "jq", "bash", "sh", "env", "cat", "mv", "cp", "mkdir",
               "rm", "date", "diff", "grep", "sed", "chmod", "uname", "dirname",
               "basename", "head", "tail", "tr", "wc", "sort", "touch", "ln", "sleep",
               "mktemp", "readlink", "id", "awk", "cut", "find", "gitleaks")
    for name in allowed:
        target = shutil.which(name)
        if target and not (safe / name).exists():
            (safe / name).symlink_to(target)
    assert (safe / "gitleaks").exists(), "the scanner row must probe the real binary"
    assert not (safe / "crontab").exists() and not (safe / "systemctl").exists()
    return f"{shims}{os.pathsep}{safe}"


def test_fresh_install_leaves_doctor_green(tmp_path, server):
    """A brand-new machine: real install.sh → loom init → a real pass → doctor exits 0.

    Doctor's three phase-2 rows (AC-2.4c) must all be present and none of them a ✗, and the
    installer must have armed the trigger without disturbing anything already in the
    (faked) crontab.
    """
    token = _adduser(server.db, "e2e-doctor")
    home = tmp_path / "home"
    home.mkdir()
    loom_home = home / ".loom"

    shims = tmp_path / "shims"
    shims.mkdir()
    cron_state = tmp_path / "crontab.txt"
    # A crontab the user already had. Losing it is the wave-b HIGH-1/MED-6 regression.
    pre_existing = "@daily /usr/local/bin/my-important-backup.sh\n"
    cron_state.write_text(pre_existing, encoding="utf-8")
    _write_shim(shims, "crontab", f"""
import sys, pathlib
state = pathlib.Path({str(cron_state)!r})
if "-l" in sys.argv[1:]:
    if state.exists():
        sys.stdout.write(state.read_text())
        sys.exit(0)
    sys.stderr.write("no crontab for nobody\\n")
    sys.exit(1)
state.write_text(sys.stdin.read())
sys.exit(0)
""")
    contract_version = (REPO_ROOT / "contract" / "VERSION").read_text(encoding="utf-8").strip()
    _write_shim(shims, "claude", f"""
import sys
if "--version" in sys.argv:
    sys.stdout.write({contract_version!r} + "\\n")
sys.exit(0)
""")

    env = {**os.environ, "HOME": str(home), "LOOM_HOME": str(loom_home),
           "LOOM_SKIP_VENV": "1", "PATH": _isolated_path(tmp_path, shims)}

    installed = subprocess.run(["bash", str(INSTALL_SH), "--yes"], env=env,
                               capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=180)
    assert installed.returncode == 0, installed.stdout + installed.stderr

    cron_now = cron_state.read_text(encoding="utf-8")
    assert "# loom-process" in cron_now and "/venv/bin/loom process" in cron_now
    assert f"LOOM_HOME={loom_home}" in cron_now
    assert pre_existing.strip() in cron_now, "install.sh must preserve existing cron entries"
    assert (home / ".claude" / "settings.json").exists(), "the hook must be registered"

    # `--token=<tok>`, not `--token <tok>`: the token is secrets.token_urlsafe(32)
    # (server/admin.py), whose alphabet includes `-`, so ~1 run in 65 produces a token
    # argparse consumes as an option ("error: argument --token: expected one argument").
    # Measured at 1.54% over 200k draws and observed firing on a real phase-4 gate run.
    init = subprocess.run([sys.executable, "-m", "loom.cli", "init",
                           "--server-url", server.url, f"--token={token}"],
                          env=env, cwd=str(REPO_ROOT),
                          capture_output=True, text=True, timeout=60)
    assert init.returncode == 0, init.stdout + init.stderr

    # `loom init` writes no sweep root, and Config then defaults it to the REAL
    # ~/.claude/projects. Pin it at an empty temp tree before running anything, exactly as an
    # operator would when relocating the tree — then let a genuine pass produce
    # sweep_last_run.json rather than seeding the file doctor is about to read.
    config_path = loom_home / "config.json"
    config = _read_json(config_path)
    empty_root = tmp_path / "no_transcripts"
    empty_root.mkdir()
    config["sweep"] = {"enabled": True, "settle_s": 1800, "transcripts_root": str(empty_root)}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    config_path.chmod(0o600)

    swept = subprocess.run([sys.executable, "-m", "loom.cli", "process"], env=env,
                           cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
    assert swept.returncode == 0, swept.stdout + swept.stderr
    assert (loom_home / "sweep_last_run.json").exists()

    doctor = subprocess.run([sys.executable, "-m", "loom.cli", "doctor"], env=env,
                            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
    assert doctor.returncode == 0, f"doctor must be green:\n{doctor.stdout}\n{doctor.stderr}"
    assert "Environment OK." in doctor.stdout
    assert "✗" not in doctor.stdout

    rows = doctor.stdout.splitlines()

    def row_with(keyword: str) -> str:
        matches = [r for r in rows if keyword in r]
        assert matches, f"no doctor row mentions {keyword!r}:\n{doctor.stdout}"
        return matches[0]

    # AC-2.4c: the three phase-2 rows, each reporting on the real thing it probes.
    assert row_with("scanner engine").startswith(("✓", "⚠"))
    assert "gitleaks" in row_with("scanner engine")
    assert row_with("loom-process").startswith("✓")
    assert "crontab block" in row_with("loom-process")
    assert row_with("sweep ran").startswith("✓")
