"""Integration fixtures for the processor (PLAN.md §T4).

Every test runs with:
- a temporary ``LOOM_HOME`` (never the real one);
- a temporary git repo with a real remote (``git remote add origin …``) for the gate;
- ``pytest-httpserver`` as the mock central server;
- a ``claude`` stub (executable script) per scenario — the real CLI is **never** invoked.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from loom.config import Config

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_FIXTURE = REPO_ROOT / "contract" / "fixtures" / "real_session.jsonl"
REAL_PROMPT = REPO_ROOT / "prompts" / "summarizer_v1.md"

# Test remote and its canonical form (normalize()).
TEST_REMOTE_URL = "git@github.com:org/b.git"
TEST_REMOTE_CANONICAL = "github.com/org/b"


def _make_git_repo(path: Path, remote_url: str = TEST_REMOTE_URL) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=path, check=True)
    return path


def _make_stub(
    path: Path,
    *,
    stdout: str = "",
    exit_code: int = 0,
    sleep: float = 0,
    echo_stdin: bool = False,
) -> str:
    """Writes an executable ``claude`` stub and returns its absolute path (str).

    ``echo_stdin`` re-emits the received stdin (the rendered delta) on stdout, handy to
    inspect which delta was summarized. Otherwise it prints ``stdout`` verbatim.
    """
    lines = ["#!/usr/bin/env python3", "import sys, time"]
    if sleep:
        lines.append(f"time.sleep({sleep})")
    if echo_stdin:
        lines.append("sys.stdout.write(sys.stdin.read())")
    else:
        lines.append(f"sys.stdout.write({stdout!r})")
    lines.append(f"sys.exit({exit_code})")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


@dataclass
class Env:
    home: Path
    repo: Path
    transcript: Path
    prompt: Path
    httpserver: object
    _stub_seq: list = None  # noqa: RUF012

    # --- Config -------------------------------------------------------------------
    def config(self, claude_bin: str, *, model: str = "sonnet", timeout_s: int = 300,
               server_url: str | None = None, capture_mode: str | None = None) -> Config:
        """Writes ``config.json`` in the temporary LOOM_HOME and returns the loaded Config.

        ``capture_mode`` is written only when given: the phase-1 tests keep the key
        ABSENT, which is the legacy ``all_registered`` behaviour (D9), so they assert the
        pre-D9 pipeline byte for byte.
        """
        url = server_url if server_url is not None else self.httpserver.url_for("")
        cfg = {
            "server_url": url,
            "token": "test-token",
            "cache_ttl_s": 3600,
            "summarizer": {"model": model, "timeout_s": timeout_s, "prompt_version": "v1"},
            "claude_bin": claude_bin,
            "prompt_path": str(self.prompt),
            # Phase 2 (R2.1): the orphan sweep runs inside run_once and its default
            # root is the REAL ~/.claude/projects. Pin it at an empty tree under the
            # temporary LOOM_HOME so these phase-1 tests stay hermetic (they assert
            # exact outcome lists) and never walk the developer's own transcripts.
            # The sweep's own behavior lives in test_sweep*.py.
            "sweep": {"transcripts_root": str(self.home / "sweep_transcripts")},
        }
        if capture_mode is not None:
            cfg["capture_mode"] = capture_mode
        (self.home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        return Config.load(self.home)

    def set_local_state(self, state: str, remote: str = TEST_REMOTE_CANONICAL, *,
                        since: str = "2026-07-01T00:00:00Z") -> None:
        """Writes ``projects_local.json`` with ``remote`` in ``state`` (D9 local gate).

        ``since`` is also the capture cutoff of an enabled project (D9 amendment of
        2026-09-30). The default predates every fixture transcript (``real_session.jsonl``
        is of 2026-07-24), so a test that is not about the cutoff captures everything.
        """
        path = self.home / "projects_local.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        data[remote] = {"state": state, "since": since}
        path.write_text(json.dumps(data), encoding="utf-8")

    def local_state(self) -> dict:
        try:
            return json.loads((self.home / "projects_local.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def config_json(self) -> dict:
        return json.loads((self.home / "config.json").read_text(encoding="utf-8"))

    def make_stub(self, name: str = "claude_stub.py", **kwargs) -> str:
        return _make_stub(self.home / name, **kwargs)

    # --- Markers ------------------------------------------------------------------
    def drop_marker(self, session_id: str = "sess-1", *, marked_at: str = "2026-07-24T00:00:00Z",
                    transcript: Path | None = None, cwd: Path | None = None, **extra) -> Path:
        queue = self.home / "queue"
        queue.mkdir(parents=True, exist_ok=True)
        marker = {
            "session_id": session_id,
            "transcript_path": str(transcript or self.transcript),
            "cwd": str(cwd or self.repo),
            "reason": "prompt_input_exit",
            "marked_at": marked_at,
            **extra,
        }
        path = queue / f"{session_id}.json"
        path.write_text(json.dumps(marker), encoding="utf-8")
        return path

    # --- Mock server --------------------------------------------------------------
    def register_projects(self, remotes=(TEST_REMOTE_CANONICAL,)) -> None:
        """GET /api/projects → list of projects with those ``canonical_remote``."""
        projects = [
            {"id": i + 1, "canonical_remote": r, "name": r.rsplit("/", 1)[-1],
             "created_by": "dev2", "created_at": "2026-07-01T00:00:00Z"}
            for i, r in enumerate(remotes)
        ]
        self.httpserver.expect_request("/api/projects", method="GET").respond_with_json(
            {"projects": projects}
        )

    def posted_entries(self) -> list[dict]:
        """JSON bodies of every ``POST /api/entries`` the mock received."""
        bodies = []
        for request, _response in self.httpserver.log:
            if request.method == "POST" and request.path == "/api/entries":
                bodies.append(request.get_json())
        return bodies

    # --- State --------------------------------------------------------------------
    def state(self) -> dict:
        try:
            return json.loads((self.home / "state.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def pointer(self) -> dict:
        return self.state().get(str(self.transcript), {})


@pytest.fixture
def env(tmp_path, httpserver) -> Env:
    home = tmp_path / "loom_home"
    home.mkdir()
    (home / "prompts").mkdir()
    prompt = home / "prompts" / "summarizer_v1.md"
    shutil.copyfile(REAL_PROMPT, prompt)

    repo = _make_git_repo(tmp_path / "repo")

    transcript = tmp_path / "transcript.jsonl"
    shutil.copyfile(REAL_FIXTURE, transcript)

    return Env(home=home, repo=repo, transcript=transcript, prompt=prompt, httpserver=httpserver)
