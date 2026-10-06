"""Phase-4 E2E gate (P3.F7) — the phase-4 claims, proven end to end.

The repo's OWN gate suite for the phase that added the sweep-owned read-coverage cache
(P3.F1), the enqueue eligibility precheck (P3.F2), the enriched run record (P3.F3), the
strict ``read_cwd`` (P3.F4), the tear-ratio escalation + attention record (P3.F5) and the
idempotent re-install (P3.F6). Like ``test_phase3_e2e.py`` it runs against a **real
boundary**, and nothing of loom's own is faked in-process:

- a REAL ``uvicorn server.app:app`` subprocess on a random free port with a temp ``LOOM_DB``;
- REAL ``python -m server.admin`` for the user/token, the REAL HTTP API for registration
  and for reading back what actually landed;
- REAL git repos (the eligibility precheck and the processor's remote gate both run
  ``git remote get-url origin``);
- the REAL ``gitleaks`` binary (engine ``auto``) on every entry that is posted;
- REAL ``loom`` CLI subprocesses (``process``, ``status``) and the REAL
  ``client/hooks/sessionend.sh`` for the marker a crashed session leaves behind;
- the REAL ``client/install.sh --yes``, twice, with the REAL ``uv``.

Only the seams this project does not own are faked: the Claude CLI (a stub that echoes back
which markers of the delta actually reached it — we are testing capture, not the LLM) and
the host scheduler (a PATH-shimmed ``crontab`` recorder). The project registry cache is
NEVER hand-written: it is produced by a real processor pass talking to the real server,
because hand-writing it would be an in-process fake of loom's own component (AC-F7.1).

Scenarios (a)–(f) of P3.F7, and what each one actually falsifies:

- **(a) ``test_e2e_cache_lifecycle*``** — three passes over one real tree, two of them
  instrumented with ``sys.addaudithook`` in a subprocess: the cold pass OPENS both
  transcripts, the warm pass opens ZERO (and runs with every transcript ``chmod 0o000``, so
  the kernel enforces what the hook observes), and an APPEND invalidates the coverage entry
  so the transcript is read again and finally delivered. ``sweep_last_run.json``'s
  ``stat_skips`` / ``cache_hits`` partition is asserted on both skip classes.
- **(b) ``test_e2e_starvation*``** — 12 NEWER ineligible transcripts + 1 OLDER deliverable
  one, against a registry cache built by a real processor pass. The deliverable one lands on
  the FIRST pass over that corpus; before P3.F2 the ten newest ate all ten slots forever.
- **(c) ``test_e2e_late_registration*``** — the same corpus with the project registered
  MID-TEST through the real API: the historical orphans are delivered on the next pass
  (anti-tombstone), with nothing manual in between.
- **(d) ``test_e2e_reinstall*``** — the real ``install.sh --yes`` twice with the real ``uv``:
  the venv's ``bin/python`` keeps its inode, the trigger is re-armed on the shimmed crontab,
  and the real ``loom doctor`` from the kept venv reports both.
- **(e) ``test_e2e_attention*``** — a wholly-unparseable transcript marked by the REAL hook
  surfaces in ``attention.json`` and in real ``loom status``; healing it removes the entry
  and delivers the content.
- **(f) ``test_e2e_regression*``** — the phase-2/3 goal waves and the whole client suite,
  run for real; the darwin ``setsid`` hook failure is the single sanctioned exception.

**Isolation is a hard requirement of this file**, and so is host safety. Every subprocess
runs with a temp ``HOME``, a temp ``LOOM_HOME``, a temp ``LOOM_DB``, a random port, an
explicit ``sweep.transcripts_root`` under ``tmp_path`` — and a PATH built from a private
symlink allowlist rather than from the machine's own. Neither the real ``crontab`` nor the
real ``systemctl`` is reachable from any PATH constructed here, which every builder asserts
before it is used: a wave-c goal once armed a real WSL2 user timer because it appended the
real PATH to its shims.

**Skips are scoped to the test that genuinely needs the tool, never module-wide** — a
module-level ``skipif`` turns a missing binary into a whole green-looking phase gate that
executed nothing. Each test declares its own tool through ``_require_tool``, and with
``LOOM_GATE=1`` (how CI and the phase gate run this file) a missing tool is a
``pytest.fail`` instead of a skip, so the gate can never be satisfied by an unrun suite
(AC-F7.3).

Self-contained on purpose: this file never imports ``loom`` in-process and never imports
anything under the wave-goal tree — the repo's suite does not depend on build artifacts,
and a gate that drives loom through function calls is not a gate on the product.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_PROMPT = REPO_ROOT / "prompts" / "summarizer_v1.md"
INSTALL_SH = REPO_ROOT / "client" / "install.sh"
SESSIONEND_HOOK = REPO_ROOT / "client" / "hooks" / "sessionend.sh"

# This module's own path, relative to the repo root: the regression leg must exclude
# itself from the client-suite run it drives, or the gate recurses into itself.
GATE_MODULE = "client/tests/e2e/test_phase4_e2e.py"

# The project the server knows about, and the canonical identity the frozen
# `phase1-project-identity` seam derives from it (lowercase host/org/repo, no .git).
REGISTERED_REMOTE = "git@github.com:example-group/phase4-e2e.git"
REGISTERED_SLUG = "example-group/phase4-e2e"
# ...and a second, real git repo whose remote is deliberately NOT registered: the source of
# the non-deliverable corpus that starves the deliverable transcript before P3.F2.
FOREIGN_REMOTE = "git@github.com:example-group/phase4-foreign.git"

# The goal waves this gate re-runs as its regression leg (AC-F7.2). Waves after these are
# the build oracles of the phase this file IS the gate of; running them from here would be
# circular, so they are deliberately absent.
GOAL_WAVES = ("test/waves/a", "test/waves/b", "test/waves/c",
              "test/waves/d", "test/waves/e", "test/waves/f")
CLIENT_SUITE = "client/tests"
# The ONE sanctioned regression failure (test/phase_gates/*_e2e_config.json:regression_known_failures):
# the hook's kick uses setsid, which does not exist on darwin. Pinned to its exact node id
# and to its platform, so it is an exception and not a blanket tolerance.
SANCTIONED_FAILURE = (
    "client/tests/integration/test_hook_install.py"
    "::test_hook_marks_transcript_and_kicks_processor"
)
SANCTIONED_PLATFORM = "darwin"

pytestmark = [pytest.mark.e2e]

# A missing binary must not be able to green-light this gate by skipping it away. Under
# LOOM_GATE=1 (the phase gate / CI) it is a hard failure; interactively it degrades to a
# skip on the individual test that needs the tool, never on the whole module (AC-F7.3).
LOOM_GATE = os.environ.get("LOOM_GATE") == "1"


def _require_tool(name: str, why: str) -> None:
    if shutil.which(name) is not None:
        return
    reason = f"phase-4 e2e needs the real {name} binary: {why}"
    if LOOM_GATE:
        pytest.fail(f"LOOM_GATE=1 forbids skipping — {reason}")
    pytest.skip(reason)


@pytest.fixture
def gitleaks_required():
    _require_tool("gitleaks", "the scanner engine is 'auto' and must resolve gitleaks")


@pytest.fixture
def jq_required():
    _require_tool("jq", "this test runs the real client/install.sh or hook, which use jq")


# --------------------------------------------------------------------------- #
# Host-safe PATH construction (wave-c lesson, asserted rather than assumed)     #
# --------------------------------------------------------------------------- #
# Everything a legitimate pass plausibly execs, and NOTHING that schedules: no crontab, no
# systemctl, no launchctl, no at/batch.
_ALLOWED_BINS = (
    "python3", "git", "jq", "bash", "sh", "env", "cat", "mv", "cp", "mkdir", "rm",
    "date", "diff", "grep", "sed", "chmod", "uname", "dirname", "basename", "head",
    "tail", "tr", "wc", "sort", "touch", "ln", "sleep", "mktemp", "readlink", "id",
    "awk", "cut", "find", "sqlite3", "curl", "tar", "gzip", "xargs", "ps", "kill",
    "hostname", "true", "false", "printf", "expr", "stat", "od", "base64", "which",
    "uniq", "tee", "realpath", "nohup",
)
_SCHEDULERS = ("crontab", "systemctl", "launchctl", "at", "batch")


def _symlink_allowlist(dest: Path, names) -> Path:
    """A private bin dir holding symlinks to ``names`` only — never the machine's PATH."""
    dest.mkdir(parents=True, exist_ok=True)
    for name in names:
        target = shutil.which(name)
        if target and not (dest / name).exists():
            (dest / name).symlink_to(target)
    return dest


def _assert_scheduler_free(path: str, *, shims: Path | None = None) -> None:
    """The boundary assertion the epic mandates, on every PATH this file builds.

    ``systemctl`` must be unreachable, always: install.sh takes its systemd branch on the
    strength of ``systemctl --user is-system-running`` and would arm a REAL user timer.
    ``crontab`` must resolve to the recorder shim or to nothing — never to the host's.
    """
    assert shutil.which("systemctl", path=path) is None, (
        "the host systemctl must be unreachable: install.sh would arm a real user timer "
        "(the wave-c deviation this assertion exists to prevent)"
    )
    resolved_crontab = shutil.which("crontab", path=path)
    expected = str(shims / "crontab") if shims is not None else None
    assert resolved_crontab == expected, (
        f"crontab on this PATH must resolve to {expected!r}, not {resolved_crontab!r} — "
        "the developer's real crontab is never read and never written here"
    )
    for name in _SCHEDULERS:
        if name == "crontab" and shims is not None:
            continue
        assert shutil.which(name, path=path) is None, f"no real {name} may be reachable"


def _toolbox(tmp_path: Path, name: str) -> Path:
    """A *copy* of a real binary in a controlled temp dir.

    A copy rather than a symlink so that any ``/opt/homebrew`` or ``/usr/local`` string
    later found in what the installer wrote is proof of a baked-in host guess instead of
    genuine install-time resolution.
    """
    dest = tmp_path / f"toolbox_{name}"
    dest.mkdir(parents=True, exist_ok=True)
    binary = dest / name
    if not binary.exists():
        real = shutil.which(name)
        assert real is not None, f"the caller must gate on _require_tool({name!r}) first"
        shutil.copy2(real, binary)
    return dest


def _base_env(*, home: Path, path: str, **extra: str) -> dict:
    """Subprocess environment: temp HOME, a controlled PATH, this checkout importable."""
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": path,
        "PYTHONPATH": f"{REPO_ROOT / 'client'}{os.pathsep}{REPO_ROOT}",
    }
    env.pop("LOOM_HOME", None)
    env.update(extra)
    return env


@pytest.fixture
def safe_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return home


@pytest.fixture
def safe_path(tmp_path, gitleaks_required):
    """Scheduler-free PATH *with* gitleaks: what an interactive pass sees."""
    path = os.pathsep.join([
        str(_symlink_allowlist(tmp_path / "safe_bin", _ALLOWED_BINS)),
        str(_toolbox(tmp_path, "gitleaks")),
    ])
    _assert_scheduler_free(path)
    assert shutil.which("gitleaks", path=path) is not None
    return path


# --------------------------------------------------------------------------- #
# Real server subprocess (the phase-2/3 gate boot pattern)                      #
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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


class Server:
    """Handle on the live uvicorn subprocess."""

    def __init__(self, db: Path, port: int) -> None:
        self.db = db
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "server.app:app",
             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            env={**os.environ, "LOOM_DB": str(db)}, cwd=str(REPO_ROOT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # An alive-but-unhealthy server (port stolen in the _free_port race, DB lock, a
        # slow import under the load this gate itself generates) makes _wait_health raise
        # — and then `srv` is never bound, so the fixture's finally never runs and the
        # uvicorn outlives the pytest session holding a port and a temp DB. The handle
        # only protects what it manages to return; the constructor must clean up after
        # itself. Same failure CLASS as the wave-d leak, on the path that fixture cannot
        # reach.
        try:
            _wait_health(self.url)
        except BaseException:
            self._kill()
            raise

    def _kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=15)

    def terminate(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                # A uvicorn that ignores SIGTERM would otherwise propagate out of the
                # finalizer AND stay alive — the teardown leaking the thing it exists to
                # reap.
                self._kill()


@pytest.fixture
def server(tmp_path):
    """One real server per test, torn down by terminating THIS handle.

    The wave-d review fixed a fixture that rebound the name and then terminated the
    replacement server, leaking the one the test had actually used. There is exactly one
    handle here and it is the one that is terminated.
    """
    db = tmp_path / "server" / "loom.db"
    db.parent.mkdir(parents=True)
    srv = Server(db, _free_port())
    try:
        yield srv
    finally:
        srv.terminate()


def _adduser(db: Path, name: str) -> str:
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


def _register_project(url: str, token: str, remote: str = REGISTERED_REMOTE) -> int:
    resp = requests.post(f"{url}/api/projects", json={"remote_url": remote},
                         headers={"Authorization": f"Bearer {token}"}, timeout=10)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _entries(url: str, token: str, project_id: int) -> list[dict]:
    """The real API surface: GET /api/projects/<id>/entries."""
    resp = requests.get(f"{url}/api/projects/{project_id}/entries",
                        headers={"Authorization": f"Bearer {token}"}, timeout=10)
    assert resp.status_code == 200, resp.text
    return resp.json()["entries"]


# --------------------------------------------------------------------------- #
# Client-side plumbing (temp LOOM_HOME, real git repos, real transcripts)       #
# --------------------------------------------------------------------------- #
def _git_repo(path: Path, env: dict, remote: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, env=env, check=True, timeout=60)
    subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, env=env,
                   check=True, timeout=60)
    return path


def _loom_home(root: Path) -> Path:
    home = root / "loom_home"
    for sub in ("queue", "quarantine", "failed", "log", "prompts", "hooks"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REAL_PROMPT, home / "prompts" / "summarizer_v1.md")
    return home


def _write_config(home: Path, *, server_url: str, token: str, claude_bin: str,
                  transcripts_root: Path, cache_ttl_s: int = 3600,
                  engine: str = "auto") -> Path:
    cfg = {
        "server_url": server_url,
        "token": token,
        "cache_ttl_s": cache_ttl_s,
        "summarizer": {"model": "sonnet", "timeout_s": 300, "prompt_version": "v1"},
        "claude_bin": claude_bin,
        "prompt_path": str(home / "prompts" / "summarizer_v1.md"),
        "sweep": {"enabled": True, "settle_s": 1800,
                  "transcripts_root": str(transcripts_root)},
        "scanner": {"engine": engine, "gitleaks_version": None},
    }
    path = home / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    path.chmod(0o600)
    return path


# Every transcript this file writes carries a `p4-<something>` marker in its message text,
# and the summarizer stub echoes back exactly the markers it found on stdin. A fixed-string
# stub would make "this transcript is the one that landed" unfalsifiable: every entry body
# would look the same whichever delta produced it.
_CLAUDE_STUB = '''\
import re, sys
delta = sys.stdin.read()
found = sorted(set(re.findall(r"p4-[a-z0-9-]+", delta)))
sys.stdout.write("## Decision\\n"
                 "- **Keep the capture** — this pass reached the summarizer.\\n"
                 "- markers-in-delta: " + (", ".join(found) or "(none)") + "\\n")
'''


def _echoing_claude_stub(path: Path) -> str:
    """Summarizer stub that reports WHICH transcript content actually reached it.

    The shebang is an absolute interpreter because this also has to run under the minimal
    controlled PATH, where ``/usr/bin/env python3`` is not guaranteed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n# -*- coding: utf-8 -*-\n{_CLAUDE_STUB}",
                    encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _settle(path: Path, seconds_ago: float = 7200) -> None:
    """Backdates mtime past ``sweep.settle_s`` — the sweep's liveness heuristic (Q-2.2)."""
    ts = time.time() - seconds_ago
    os.utime(path, (ts, ts))


def _loom(args: list[str], *, loom_home: Path, home: Path, path: str,
          timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "loom.cli", *args],
        env=_base_env(home=home, path=path, LOOM_HOME=str(loom_home)),
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=timeout,
    )


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _markers(loom_home: Path) -> list[Path]:
    return sorted((loom_home / "queue").glob("*.json"))


def _state(loom_home: Path) -> dict:
    path = loom_home / "state.json"
    return _read_json(path) if path.exists() else {}


def _processor_log(loom_home: Path) -> str:
    path = loom_home / "log" / "processor.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _last_run(loom_home: Path) -> dict:
    """The enriched sweep run record (seam ``p4-sweep-last-run-additive``)."""
    record = _read_json(loom_home / "sweep_last_run.json")
    # The frozen four keep their exact types and semantics, and the NAMED skip classes can
    # never exceed `skipped` — asserted on every record this file reads, so a counter that
    # drifts is caught wherever it drifts (AC-F3.1/AC-F3.2).
    # `<=` and not `==` on purpose: the classes partition `skipped` only once the UNNAMED
    # residue is counted too (`sweep.py:89` — "exactly one of … or the unnamed"), and this
    # shared helper runs against records whose residue is non-zero. The exact partition is
    # pinned where the residue is known: per-scenario call sites assert the individual
    # counters (stat_skips == 1, cache_hits == 1, ineligible == 12), and the frozen E9 goal
    # asserts the full partition arithmetic directly.
    for key in ("scanned", "enqueued", "skipped", "stat_skips", "cache_hits",
                "ineligible", "not_scanned"):
        assert isinstance(record.get(key), int) and not isinstance(record[key], bool), (
            f"sweep_last_run.json must carry an int {key!r}: {record}"
        )
    assert record["enqueued"] + record["skipped"] == record["scanned"], record
    classes = record["stat_skips"] + record["cache_hits"] + record["ineligible"]
    assert classes <= record["skipped"], (
        f"the skip classes must partition `skipped`, never exceed it: {record}"
    )
    return record


def _coverage(loom_home: Path) -> dict:
    """The sweep-owned read-coverage cache (seam ``p4-coverage-cache``)."""
    path = loom_home / "coverage_cache.json"
    return _read_json(path) if path.exists() else {}


def _jsonl(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def _user_line(session_id: str, cwd: str, n: int, text: str) -> dict:
    return {"type": "user", "uuid": f"{session_id}-u{n}", "cwd": cwd,
            "sessionId": session_id, "message": {"role": "user", "content": text}}


def _assistant_line(session_id: str, cwd: str, n: int, text: str) -> dict:
    return {"type": "assistant", "uuid": f"{session_id}-a{n}", "cwd": cwd,
            "sessionId": session_id,
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": text}]}}


def _healthy_transcript(path: Path, session_id: str, cwd: str, marker: str) -> Path:
    """A settled, complete transcript carrying ``marker`` in both conversational turns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        _jsonl({"type": "file-history-snapshot", "messageId": "m1", "snapshot": {}})
        + _jsonl(_user_line(session_id, cwd, 1, f"{marker}: audit the token store"))
        + _jsonl(_assistant_line(session_id, cwd, 1,
                                 f"{marker}: decided to rotate the pilot token."))
    )
    return path


def _cwdless_transcript(path: Path) -> Path:
    """A transcript no line of which carries a ``cwd``.

    Precisely the RUNBOOK §9 residual class: nothing will ever publish it, so before the
    read-coverage cache existed it had no record anywhere that it had been read and the
    sweep re-read it in full on every pass, forever. Metadata lines only — the adapter
    skips those types, and ``read_cwd`` streams the whole file and finds nothing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(
        _jsonl({"type": "file-history-snapshot", "messageId": f"m{n}", "snapshot": {}})
        for n in range(3)
    ))
    return path


class Client:
    """A configured temp client: LOOM_HOME + git repos + transcripts root + claude stub."""

    def __init__(self, tmp_path: Path, home: Path, path: str, server: Server, token: str,
                 *, cache_ttl_s: int = 3600) -> None:
        self.tmp_path = tmp_path
        self.os_home = home
        self.path = path
        self.loom_home = _loom_home(tmp_path)
        env = _base_env(home=home, path=path)
        self.repo = _git_repo(tmp_path / "repo", env, REGISTERED_REMOTE)
        self.foreign_repo = _git_repo(tmp_path / "repo_foreign", env, FOREIGN_REMOTE)
        self.transcripts_root = tmp_path / "transcripts"
        self.transcripts_root.mkdir(parents=True, exist_ok=True)
        self.claude_bin = _echoing_claude_stub(tmp_path / "claude_stub.py")
        _write_config(self.loom_home, server_url=server.url, token=token,
                      claude_bin=self.claude_bin,
                      transcripts_root=self.transcripts_root, cache_ttl_s=cache_ttl_s)

    def process(self, *, expect_ok: bool = True) -> subprocess.CompletedProcess:
        """One ``loom process`` pass — exactly what the 15-min trigger runs."""
        cp = _loom(["process"], loom_home=self.loom_home, home=self.os_home, path=self.path)
        if expect_ok:
            assert cp.returncode == 0, f"loom process failed:\n{cp.stdout}\n{cp.stderr}"
        return cp

    def status(self) -> str:
        cp = _loom(["status"], loom_home=self.loom_home, home=self.os_home, path=self.path,
                   timeout=120)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        return cp.stdout

    def transcript_path(self, session_id: str, sub: str = "-Users-nobody-elsewhere") -> Path:
        return self.transcripts_root / sub / f"{session_id}.jsonl"

    def hook_mark(self, session_id: str, transcript: Path, cwd: str) -> Path:
        """Marks a session through the REAL SessionEnd hook (``client/hooks/sessionend.sh``).

        The hook is the only producer of a marker for a transcript the sweep will never
        enqueue — a wholly-torn one has no countable line and no derivable cwd — so scenario
        (e) has to come in through it rather than through a hand-written queue file. Its
        kick is a no-op here: it needs ``$LOOM_HOME/venv/bin/python``, which this temp home
        deliberately does not have, so nothing is launched behind the test's back.
        """
        payload = json.dumps({"session_id": session_id,
                              "transcript_path": str(transcript),
                              "cwd": cwd, "reason": "other"})
        cp = subprocess.run(
            ["bash", str(SESSIONEND_HOOK)], input=payload,
            env=_base_env(home=self.os_home, path=self.path,
                          LOOM_HOME=str(self.loom_home)),
            capture_output=True, text=True, timeout=120,
        )
        assert cp.returncode == 0, cp.stdout + cp.stderr
        marker = self.loom_home / "queue" / f"{session_id}.json"
        assert marker.exists(), (
            f"the real SessionEnd hook must have written the marker:\n{cp.stdout}{cp.stderr}"
        )
        return marker


@pytest.fixture
def client_factory(tmp_path, safe_home, safe_path, server):
    """A configured client plus its server token and registered project id, on demand."""
    def make(user: str, *, register: bool = True, cache_ttl_s: int = 3600):
        token = _adduser(server.db, user)
        project_id = _register_project(server.url, token) if register else None
        return Client(tmp_path, safe_home, safe_path, server, token,
                      cache_ttl_s=cache_ttl_s), token, project_id
    return make


# --------------------------------------------------------------------------- #
# Open-counting instrumentation (the phase-3 gate's audit-runner technique)     #
# --------------------------------------------------------------------------- #
# Instrumented in a SUBPROCESS with sys.addaudithook: the ``open`` audit event fires for
# every builtin ``open`` and every ``os.open`` under that interpreter — including opens that
# then fail — so a pass cannot read a transcript behind this counter's back. Counting opens
# (not bytes) is the honest unit: what P3.F1 claims is that a candidate whose stat 4-tuple
# still matches its coverage entry is resolved by ``stat`` alone, and ``stat`` raises no
# ``open`` event.
_AUDIT_RUNNER = '''\
import json, sys
from pathlib import Path

TRANSCRIPTS_ROOT = {root!r}
REPORT = {report!r}
opened_files = []

def _count_opens(event, args):
    if event != "open":
        return
    target = args[0]
    if isinstance(target, bytes):
        target = target.decode("utf-8", "replace")
    if isinstance(target, str) and target.startswith(TRANSCRIPTS_ROOT):
        opened_files.append(target)

sys.addaudithook(_count_opens)

from loom.cli import main as loom_main

rc = loom_main(["process"])
Path(REPORT).write_text(json.dumps(opened_files), encoding="utf-8")
sys.exit(rc)
'''


def _instrumented_pass(tmp_path: Path, client: Client, tag: str) -> list[str]:
    """One ``loom process`` pass under an audit hook; returns the transcript files opened."""
    report = tmp_path / f"opened_{tag}.json"
    runner = tmp_path / f"audit_runner_{tag}.py"
    runner.write_text(
        _AUDIT_RUNNER.format(root=str(client.transcripts_root) + os.sep,
                             report=str(report)),
        encoding="utf-8",
    )
    cp = subprocess.run(
        [sys.executable, str(runner)],
        env=_base_env(home=client.os_home, path=client.path,
                      LOOM_HOME=str(client.loom_home)),
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=600,
    )
    assert cp.returncode == 0, f"instrumented pass failed:\n{cp.stdout}\n{cp.stderr}"
    assert report.exists(), f"the audit runner produced no report:\n{cp.stdout}{cp.stderr}"
    return json.loads(report.read_text(encoding="utf-8"))


# =========================================================================== #
# (a) AC-F7.1 / P3.F1 + P3.F3 — read-coverage cache lifecycle                  #
# =========================================================================== #
def test_e2e_cache_lifecycle_read_once_then_stat_only_and_invalidated_on_append(
        tmp_path, server, client_factory):
    """(a) Three real passes over one tree: full read once, stat-only after, invalidation
    on append — every claim read off the real artifacts, none of it hoped.

    The corpus is the honest one: a DELIVERABLE transcript, whose skip on later passes comes
    from the delivery pointer's stat 4-tuple (``stat_skips``, P2.F4), and a transcript no
    line of which carries a ``cwd``, which nothing will ever publish and whose skip can
    therefore only come from the sweep's OWN read-coverage cache (``cache_hits``, P3.F1).
    Before this phase the second one was re-read in full on every pass forever — the RUNBOOK
    §9 residual — and a gate that only watched the deliverable one could not tell.

    The warm pass runs with both transcripts ``chmod 0o000``: the audit hook observes zero
    opens and the kernel makes any open impossible, so "stat-only" is enforced as well as
    measured (a shelled-out read in a child process would escape the hook alone).
    """
    client, token, project_id = client_factory("e2e-cache")
    deliverable = client.transcript_path("sess-cache-deliverable")
    _healthy_transcript(deliverable, "sess-cache-deliverable", str(client.repo),
                        "p4-cache-deliverable")
    _settle(deliverable)
    cwdless = client.transcript_path("sess-cache-cwdless")
    _cwdless_transcript(cwdless)
    _settle(cwdless)
    transcripts = [deliverable, cwdless]

    # --- pass 1 (cold): nothing is known, so both transcripts are READ in full ----------
    opened_cold = _instrumented_pass(tmp_path, client, "cold")
    assert set(opened_cold) == {str(deliverable), str(cwdless)}, (
        "the instrumentation must SEE the reads it is meant to count; a cold pass reads "
        f"every candidate: {sorted(set(opened_cold))}"
    )
    assert len(_entries(server.url, token, project_id)) == 1, (
        "the deliverable transcript must land on the cold pass"
    )
    cold = _last_run(client.loom_home)
    assert (cold["scanned"], cold["enqueued"], cold["skipped"]) == (2, 1, 1), cold
    assert cold["cache_hits"] == 0, f"nothing can be a cache hit on a cold pass: {cold}"

    # The completed read wrote a TRUE coverage entry for the transcript nothing publishes:
    # the real line count, an explicit `cwd: null` (the read completed and found none) and
    # the stat 4-tuple of the file as it was read.
    entry = _coverage(client.loom_home)[str(cwdless)]
    st = cwdless.lstat()
    assert entry["line_count"] == 3 and entry["cwd"] is None, entry
    assert (entry["stat_dev"], entry["stat_ino"], entry["stat_mtime_ns"],
            entry["stat_size"]) == (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size), entry

    # --- pass 2 (warm): stat-only, with the reads made physically impossible ------------
    modes = {t: stat.S_IMODE(t.stat().st_mode) for t in transcripts}
    try:
        for transcript in transcripts:
            os.chmod(transcript, 0o000)
        # Proof that the mode really denies THIS process — and a loud failure rather than a
        # silently vacuous leg if the suite is ever run as root.
        with pytest.raises(PermissionError):
            transcripts[0].open("rb").close()
        opened_warm = _instrumented_pass(tmp_path, client, "warm")
    finally:
        for transcript, mode in modes.items():
            os.chmod(transcript, mode)
    assert opened_warm == [], (
        f"a warm pass over an unchanged tree must open ZERO transcripts: {opened_warm}"
    )

    # ...and the zero is the two skips firing, not the sweep having stopped looking: it
    # still scanned both, and said which class each one fell into.
    warm = _last_run(client.loom_home)
    assert (warm["scanned"], warm["enqueued"], warm["skipped"]) == (2, 0, 2), warm
    assert warm["stat_skips"] == 1, (
        f"the delivered transcript is skipped on the pointer's stat 4-tuple: {warm}"
    )
    assert warm["cache_hits"] == 1, (
        "the transcript nothing will ever publish must be skipped on the sweep's OWN "
        f"read-coverage cache — that is the whole of P3.F1: {warm}"
    )
    sweep_lines = [ln for ln in _processor_log(client.loom_home).splitlines()
                   if "sweep_pass" in ln]
    assert "stat_skips=1" in sweep_lines[-1] and "cache_hits=1" in sweep_lines[-1], (
        f"the run-log line must carry both skip classes: {sweep_lines[-1]}"
    )
    assert len(_entries(server.url, token, project_id)) == 1

    # --- pass 3: an APPEND invalidates the coverage entry and the file is read again ----
    # The append gives the previously cwd-less transcript a real cwd in a registered repo,
    # so "the entry was invalidated" is observable as an ENTRY ON THE SERVER rather than as
    # a counter: a sweep serving the stale `cwd: null` would skip this file forever.
    with cwdless.open("ab") as fh:
        fh.write(_jsonl(_user_line("sess-cache-cwdless", str(client.repo), 1,
                                   "p4-cache-appended: and after the append?")))
        fh.write(_jsonl(_assistant_line("sess-cache-cwdless", str(client.repo), 1,
                                        "p4-cache-appended: decided to publish it.")))
    _settle(cwdless)

    client.process()
    after = _last_run(client.loom_home)
    assert (after["scanned"], after["enqueued"]) == (2, 1), after
    assert after["cache_hits"] == 0, (
        "AC-F1.3: the appended transcript's stat 4-tuple moved, so its entry must be "
        f"INVALIDATED, never served as a hit: {after}"
    )
    entries = _entries(server.url, token, project_id)
    assert len(entries) == 2, entries
    appended = next(e for e in entries if e["session_id"] == "sess-cache-cwdless")
    assert "p4-cache-appended" in appended["content_md"], appended["content_md"]

    # ...and the invalidation repopulated the entry from the NEW bytes.
    fresh = _coverage(client.loom_home)[str(cwdless)]
    assert fresh["line_count"] == 5 and fresh["cwd"] == str(client.repo), fresh
    assert fresh["stat_size"] != entry["stat_size"], (fresh, entry)
    assert _markers(client.loom_home) == []
    assert list((client.loom_home / "failed").glob("*.json")) == []


# =========================================================================== #
# (b) AC-F7.1 / P3.F2 — starvation: the older deliverable transcript wins      #
# =========================================================================== #
def _registry_names(loom_home: Path) -> list[str]:
    """The canonical remotes in the project-registry cache the PROCESSOR wrote.

    Read-only, and read from the file the real pass produced: this gate never writes that
    file itself. Hand-writing it would be an in-process fake of loom's own component, and
    it is exactly the seam (``p4-sweep-eligibility``) whose input the sweep consumes.
    """
    cache = _read_json(loom_home / "projects_cache.json")
    projects = cache.get("projects")
    assert isinstance(projects, list), cache
    return [p.get("canonical_remote") for p in projects if isinstance(p, dict)]


def test_e2e_starvation_older_deliverable_transcript_beats_a_newer_ineligible_corpus(
        tmp_path, server, client_factory):
    """(b) 12 NEWER non-deliverable transcripts + 1 OLDER deliverable one: the deliverable
    one is captured on the FIRST pass over that corpus.

    ✅ VERIFIED starvation base (wave-f review, RUNBOOK §9): the sweep scans newest-first
    and stops at ``MAX_ENQUEUE_PER_PASS`` (10), so the same ten newest non-delivering
    transcripts consumed all ten slots on every pass and a registered project's older
    session starved forever. P3.F2 withholds the ineligible ones WITHOUT consuming a slot,
    which is why one pass is now enough.

    The registry the precheck reads is built by a REAL processor pass against the real
    server — a warm-up pass here writes it from the live API — never hand-written.
    """
    client, token, project_id = client_factory("e2e-starvation")

    # Warm-up pass over an empty tree: its only job is that the processor talks to the real
    # server and writes the registry cache the sweep will later read.
    client.process()
    assert REGISTERED_SLUG in " ".join(_registry_names(client.loom_home)), (
        "the warm-up pass must have written a registry naming the registered project: "
        f"{_registry_names(client.loom_home)}"
    )
    assert _last_run(client.loom_home)["scanned"] == 0, "the warm-up tree must be empty"

    # The corpus: a dozen settled sessions of an UNREGISTERED repo, all NEWER than the one
    # session of the registered repo. Twelve > MAX_ENQUEUE_PER_PASS, so pre-P3.F2 the cap
    # would be exhausted before the older transcript was ever examined.
    ineligible_paths = []
    for n in range(12):
        path = client.transcript_path(f"sess-starve-foreign-{n:02d}",
                                      sub="-Users-nobody-foreign")
        _healthy_transcript(path, f"sess-starve-foreign-{n:02d}",
                            str(client.foreign_repo), f"p4-starve-foreign-{n:02d}")
        _settle(path, seconds_ago=3600)
        ineligible_paths.append(path)
    deliverable = client.transcript_path("sess-starve-deliverable")
    _healthy_transcript(deliverable, "sess-starve-deliverable", str(client.repo),
                        "p4-starve-deliverable")
    _settle(deliverable, seconds_ago=86400)
    assert deliverable.stat().st_mtime < min(p.stat().st_mtime for p in ineligible_paths), (
        "the deliverable transcript must be the OLDEST candidate, or this scenario does "
        "not reproduce the starvation it exists to close"
    )

    first_pass = client.process()
    record = _last_run(client.loom_home)
    assert record["scanned"] == 13, (
        f"pass 1 must examine every candidate — the withheld ones consume no slot and do "
        f"not stop the scan: {record}\n{first_pass.stdout}"
    )
    assert record["ineligible"] == 12, (
        "the twelve unregistered-project transcripts must be withheld by the eligibility "
        f"precheck, not enqueued: {record}"
    )
    assert record["enqueued"] == 1 and record["not_scanned"] == 0, record

    entries = _entries(server.url, token, project_id)
    assert len(entries) == 1, (
        "AC-F2.1: the older deliverable transcript must be captured on the FIRST pass over "
        f"this corpus:\n{first_pass.stdout}\n{_processor_log(client.loom_home)}"
    )
    assert entries[0]["session_id"] == "sess-starve-deliverable"
    assert "p4-starve-deliverable" in entries[0]["content_md"], entries[0]["content_md"]

    # Withheld, not dropped, and nothing about them remembered: no marker, no parked file,
    # and no pointer for any of them in state.json (drops stay processor-owned).
    assert _markers(client.loom_home) == []
    assert list((client.loom_home / "failed").glob("*.json")) == []
    state = _state(client.loom_home)
    assert [p for p in ineligible_paths if str(p) in state] == [], (
        f"an ineligible candidate must never touch the delivery pointer state: {state}"
    )


# =========================================================================== #
# (c) AC-F7.1 / P3.F2 — late registration unblocks historical orphans         #
# =========================================================================== #
def test_e2e_late_registration_delivers_historical_orphans_without_intervention(
        tmp_path, server, client_factory):
    """(c) Orphans accumulated BEFORE the project existed on the server are delivered on the
    pass after it is registered — no tombstone, no manual step.

    The precheck is re-evaluated from the current registry on every pass, so there is
    nowhere for a "this one is ineligible" verdict to be remembered. Registration happens
    MID-TEST through the real API, and the only thing that changes between the two passes is
    what the server answers.
    """
    client, token, _ = client_factory("e2e-late", register=False, cache_ttl_s=0)

    # Warm-up: a real pass writes a registry that lists no project at all.
    client.process()
    assert _registry_names(client.loom_home) == [], (
        f"nothing is registered yet: {_registry_names(client.loom_home)}"
    )

    orphans = []
    for n in range(2):
        path = client.transcript_path(f"sess-late-orphan-{n}")
        _healthy_transcript(path, f"sess-late-orphan-{n}", str(client.repo),
                            f"p4-late-orphan-{n}")
        _settle(path)
        orphans.append(path)

    # --- before registration: withheld, and provably so ---------------------------------
    client.process()
    before = _last_run(client.loom_home)
    assert (before["scanned"], before["enqueued"], before["ineligible"]) == (2, 0, 2), before
    assert _markers(client.loom_home) == [], "a withheld candidate writes no marker"

    # --- the project is registered mid-test, through the real API -----------------------
    project_id = _register_project(server.url, token)
    assert len(_entries(server.url, token, project_id)) == 0

    after_pass = client.process()
    assert REGISTERED_SLUG in " ".join(_registry_names(client.loom_home)), (
        "the processor must have refreshed the registry cache from the real server"
    )
    after = _last_run(client.loom_home)
    assert (after["scanned"], after["enqueued"], after["ineligible"]) == (2, 2, 0), (
        f"AC-F2.2: registering the project must make its historical orphans eligible on "
        f"the very next pass: {after}\n{after_pass.stdout}"
    )
    entries = _entries(server.url, token, project_id)
    assert len(entries) == 2, (
        f"both orphans must be delivered:\n{after_pass.stdout}\n"
        f"{_processor_log(client.loom_home)}"
    )
    assert {e["session_id"] for e in entries} == {"sess-late-orphan-0", "sess-late-orphan-1"}
    for entry in entries:
        assert "p4-late-orphan" in entry["content_md"], entry["content_md"]
    assert _markers(client.loom_home) == []
    assert list((client.loom_home / "failed").glob("*.json")) == []
    assert all(str(path) in _state(client.loom_home) for path in orphans)


# =========================================================================== #
# (d) AC-F7.1 / P3.F6 — the real installer, run twice                          #
# =========================================================================== #
def _write_shim(shims: Path, name: str, body: str) -> Path:
    shims.mkdir(parents=True, exist_ok=True)
    path = shims / name
    path.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _crontab_shim(shims: Path, state_file: Path) -> Path:
    """File-backed ``crontab`` recorder: -l / - / -r, faithfully distinguished.

    The installer scenarios must NEVER invoke the developer's real crontab: on macOS a
    crontab(1) write takes 30s-to-minutes under sandbox, and a test that arms a real trigger
    pointed at a temp LOOM_HOME pytest then deletes is the wave-c incident. "No crontab" is
    the ABSENCE of the state file and ``-r`` deletes it, so "removed" and "installed empty"
    stay distinguishable.
    """
    return _write_shim(shims, "crontab", f"""
import pathlib, sys
state = pathlib.Path({str(state_file)!r})
calls = pathlib.Path({str(state_file)!r} + ".calls")
calls.parent.mkdir(parents=True, exist_ok=True)
with calls.open("a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if "-l" in args:
    if state.exists():
        sys.stdout.write(state.read_text())
        sys.exit(0)
    sys.stderr.write("crontab: no crontab for e2e\\n")
    sys.exit(1)
if "-r" in args:
    if state.exists():
        state.unlink()
        sys.exit(0)
    sys.stderr.write("crontab: no crontab for e2e\\n")
    sys.exit(1)
if "-" in args:
    state.write_text(sys.stdin.read())
    sys.exit(0)
plain = [a for a in args if not a.startswith("-")]
if len(plain) == 1:
    state.write_text(pathlib.Path(plain[0]).read_text())
    sys.exit(0)
sys.stderr.write("crontab shim: unsupported argv: %r\\n" % (args,))
sys.exit(64)
""")


def _cron_calls(state_file: Path) -> list[str]:
    calls = Path(str(state_file) + ".calls")
    return calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []


def test_e2e_reinstall_with_real_uv_keeps_the_venv_and_rearms_the_trigger(
        tmp_path, safe_home, jq_required):
    """(d) The real ``install.sh --yes`` twice, with the real ``uv``: converge, never refuse.

    ✅ VERIFIED failure base (RUNBOOK §7, 2026-08-11): ``uv venv`` declines an existing
    directory ("Use the --clear flag") and the second plain run died with exit 2 at step 2 —
    BEFORE the trigger was re-armed — so a host that re-ran the installer was left with a
    stale trigger and no warning. This leg proves the three halves of the fix at the real
    boundary: exit 0, the SAME venv (``bin/python``'s ``st_ino`` unchanged — ``lstat``,
    because uv's ``bin/python`` is a symlink whose ``stat`` would follow to the never-moving
    base interpreter), and the trigger re-armed on the shimmed crontab.

    The installer's skip-the-venv escape hatch is deliberately NOT used here: it bypasses
    exactly the step-2 path under test. This leg builds a real venv with the real toolchain,
    so the ``loom doctor`` below is the REAL console script out of that venv (AC-F6.5, at
    the E2E boundary rather than the goal-level stub).
    """
    # The availability gates come FIRST, before a single expensive step, so that under
    # LOOM_GATE=1 the conversion of this skip into a failure is observable in seconds.
    _require_tool("uv", "the re-install path under test is uv's, and it must run for real")
    _require_tool("git", "the editable install resolves this checkout")

    loom_home = safe_home / ".loom"
    shims = tmp_path / "shims"
    cron_state = tmp_path / "crontab.txt"
    _crontab_shim(shims, cron_state)
    path = os.pathsep.join([
        str(shims),
        str(_symlink_allowlist(tmp_path / "install_bin", _ALLOWED_BINS)),
        str(_toolbox(tmp_path, "uv")),
    ])
    # Wave-c lesson, asserted before anything is executed: the only reachable crontab is the
    # recorder, and systemctl is not reachable at all (or install.sh takes the systemd
    # branch and arms a REAL user timer on this developer's machine).
    _assert_scheduler_free(path, shims=shims)
    assert shutil.which("uv", path=path) == str(_toolbox(tmp_path, "uv") / "uv")

    def run_install(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(INSTALL_SH), *args],
            env=_base_env(home=safe_home, path=path, LOOM_HOME=str(loom_home)),
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=900,
        )

    first = run_install("--yes")
    assert first.returncode == 0, f"fresh install:\n{first.stdout}\n{first.stderr}"
    venv_python = loom_home / "venv" / "bin" / "python"
    assert venv_python.exists() and os.access(venv_python, os.X_OK), first.stdout
    inode_before = os.lstat(venv_python).st_ino
    writes_before = _cron_calls(cron_state).count("-")

    second = run_install("--yes")
    assert second.returncode == 0, (
        "AC-F6.1: a plain --yes over a healthy existing venv must exit 0 — the phase-3 "
        f"failure exited 2 at step 2:\n{second.stdout}\n{second.stderr}"
    )
    assert os.lstat(venv_python).st_ino == inode_before, (
        "AC-F6.1: the healthy venv must be KEPT — a changed bin/python inode is "
        "rm-and-recreate masquerading as idempotency"
    )
    assert "loom installed in" in second.stdout, (
        f"the re-install must run to completion, not stop after step 2:\n{second.stdout}"
    )
    # AC-F6.5, half one: the run reached step 5 and re-armed the trigger.
    assert _cron_calls(cron_state).count("-") > writes_before, (
        f"the re-install must reach the trigger step: {_cron_calls(cron_state)}"
    )
    block = cron_state.read_text(encoding="utf-8")
    assert "# loom-process" in block and "/venv/bin/loom process --quiet" in block, block
    assert "-r" not in " ".join(_cron_calls(cron_state)).split(), (
        f"an install must never REMOVE a crontab: {_cron_calls(cron_state)}"
    )

    # The kept venv is a working install, not merely a surviving directory.
    # PYTHONPATH is cleared for the probe on purpose: with this checkout on it, `import
    # loom` would succeed whether or not the editable install survived the re-install.
    probe = subprocess.run([str(venv_python), "-c", "import loom; print(loom.__file__)"],
                           env={**os.environ, "PYTHONPATH": ""},
                           capture_output=True, text=True, timeout=120, cwd=str(tmp_path))
    assert probe.returncode == 0, f"the kept venv must import the package:\n{probe.stderr}"
    assert str(REPO_ROOT) in probe.stdout, (
        f"the editable install must still point at this checkout: {probe.stdout!r}"
    )

    # AC-F6.5, half two: the doctor step, run for real out of the venv the re-install kept.
    doctor = subprocess.run(
        [str(loom_home / "venv" / "bin" / "loom"), "doctor"],
        env=_base_env(home=safe_home, path=path, LOOM_HOME=str(loom_home)),
        capture_output=True, text=True, timeout=300,
    )
    assert f"venv python present ({venv_python})" in doctor.stdout, (
        f"doctor must see the very venv the re-install kept:\n{doctor.stdout}{doctor.stderr}"
    )
    assert "periodic trigger installed" in doctor.stdout, (
        f"doctor must see the re-armed trigger:\n{doctor.stdout}"
    )
    assert "SessionEnd hook registered" in doctor.stdout, doctor.stdout


# =========================================================================== #
# (e) AC-F7.1 / P3.F5 — escalation visibility                                  #
# =========================================================================== #
def _torn_line(n: int) -> bytes:
    """A physical line that cannot parse: the tail of a session killed mid-write."""
    return (b'{"type":"assistant","uuid":"torn-%d","message":{"role":"assi' % n) + b"\n"


def test_e2e_attention_unparseable_transcript_surfaces_in_record_and_status(
        tmp_path, server, client_factory, jq_required):
    """(e) A wholly-unparseable transcript becomes VISIBLE — in ``attention.json`` and in
    real ``loom status`` — and disappears again when it heals.

    Before P3.F5 this session logged one ``held_lines_at_eof`` line and vanished into
    ``dropped_empty_delta``: quiet-but-not-silent, invisible unless somebody grepped the log.
    The marker comes from the REAL SessionEnd hook, because it has to: every line of this
    transcript is torn, so there is no countable line and no derivable cwd and the sweep will
    never enqueue it — which is precisely why the condition was invisible.
    """
    client, token, project_id = client_factory("e2e-attention")
    session_id = "sess-attention-torn"
    transcript = client.transcript_path(session_id)
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_bytes(b"".join(_torn_line(n) for n in range(3)))

    client.hook_mark(session_id, transcript, str(client.repo))
    client.process()

    log = _processor_log(client.loom_home)
    assert f"{session_id}\theld_lines_at_eof\theld=3" in log, log
    assert f"{session_id}\tdropped_empty_delta" in log, (
        f"the capture path is unchanged: nothing parsed, so the marker is dropped:\n{log}"
    )
    # p4-tear-escalation-event is REPORT-ONLY and fires on the RATIO: with nothing parsed
    # there is no ratio at all, so the escalation stays silent here and speaks on the heal.
    assert "tear_ratio_high" not in log, log

    record = _read_json(client.loom_home / "attention.json")
    assert list(record) == [session_id], (
        f"AC-F5.2: exactly one attention entry, keyed by session: {record}"
    )
    entry = record[session_id]
    assert entry["held"] == 3 and entry["transcript_path"] == str(transcript), entry
    assert entry["first_seen"] and entry["last_seen"], entry
    assert "assi" not in json.dumps(record), (
        f"AC-F5.5: the record must never carry transcript content: {record}"
    )

    status = client.status()
    assert "Attention (attention.json): 1" in status, (
        f"AC-F5.4: `loom status` must surface the attention count:\n{status}"
    )
    assert f"{session_id}  held=3" in status, status
    assert len(_entries(server.url, token, project_id)) == 0, (
        "nothing parsed, so nothing may have been published"
    )
    assert _markers(client.loom_home) == [] and \
        list((client.loom_home / "failed").glob("*.json")) == []

    # --- the session comes back and writes complete lines: the condition heals ----------
    with transcript.open("ab") as fh:
        fh.write(_jsonl(_user_line(session_id, str(client.repo), 1,
                                   "p4-attention-healed: and after the crash?")))
        fh.write(_jsonl(_assistant_line(session_id, str(client.repo), 1,
                                        "p4-attention-healed: decided to retry.")))
    client.hook_mark(session_id, transcript, str(client.repo))
    client.process()

    healed_log = _processor_log(client.loom_home)
    assert f"{session_id}\ttorn_lines_skipped\ttorn=3" in healed_log, healed_log
    # Now there IS a ratio — 3 torn against 2 parsed — so the escalation fires, counts only.
    assert f"{session_id}\ttear_ratio_high\ttorn=3 parsed=2 ratio=0.60" in healed_log, (
        f"p4-tear-escalation-event must report the crossing with counts only:\n{healed_log}"
    )
    assert _read_json(client.loom_home / "attention.json") == {}, (
        "AC-F5.3: the entry must be removed on the pass where the transcript parses"
    )
    healed_status = client.status()
    assert "Attention (attention.json)" not in healed_status, (
        f"AC-F5.4: an empty record renders exactly as a pre-P3.F5 client:\n{healed_status}"
    )
    entries = _entries(server.url, token, project_id)
    assert len(entries) == 1 and "p4-attention-healed" in entries[0]["content_md"], entries


# =========================================================================== #
# (f) AC-F7.2 — regression legs                                                #
# =========================================================================== #
def _run_suite(args: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    """A real pytest run of part of this repo, in this interpreter's environment.

    ``LOOM_GATE=1`` is passed on deliberately: the goal waves and the phase-2/3 gates use
    the same discipline this file does, so a driven suite that quietly skipped its
    environment-dependent legs would report green having proven nothing.
    """
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=no", "-rf",
         "-p", "no:cacheprovider", *args],
        env={**os.environ, "LOOM_GATE": "1"}, cwd=str(REPO_ROOT),
        capture_output=True, text=True, timeout=timeout,
    )


_COUNT_RE = re.compile(r"(\d+) (passed|failed|error|errors|skipped)")


def _counts(cp: subprocess.CompletedProcess) -> dict:
    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
    for line in reversed((cp.stdout or "").strip().splitlines()):
        found = _COUNT_RE.findall(line)
        if found:
            for number, kind in found:
                counts["error" if kind == "errors" else kind] += int(number)
            return counts
    raise AssertionError(f"no pytest summary line in:\n{cp.stdout}\n{cp.stderr}")


def _failed_ids(cp: subprocess.CompletedProcess) -> list[str]:
    return sorted({line.split()[1] for line in (cp.stdout or "").splitlines()
                   if line.startswith("FAILED ") and len(line.split()) > 1})


def test_e2e_regression_phase2_and_phase3_goal_waves_still_pass(gitleaks_required,
                                                                jq_required):
    """AC-F7.2, first leg: every phase-2/3 goal wave, re-run for real.

    The goal waves are the frozen oracles of the two phases this one builds on. They are
    listed one by one rather than swept up by a glob so that a wave that stops being
    collected — renamed, emptied, moved — fails here instead of quietly not running.
    """
    for wave in GOAL_WAVES:
        assert (REPO_ROOT / wave).is_dir(), f"{wave} must exist to be a regression leg"

    result = _run_suite(list(GOAL_WAVES), timeout=3600)
    counts = _counts(result)
    if result.returncode != 0 or counts["failed"] or counts["error"]:
        # `pytest.fail` rather than a bare assert, and the node ids LAST: when this leg is
        # itself driven by the phase gate, only the tail of this output survives, and the
        # one thing an operator needs from it is WHICH wave test failed.
        pytest.fail(
            "AC-F7.2: the phase-2/3 goal waves must still pass.\n"
            f"{result.stdout[-1200:]}\n"
            f"counts={counts} FAILED IN THE DRIVEN WAVE RUN: {_failed_ids(result)}"
        )
    assert counts["passed"] >= 100, (
        f"the goal waves must actually have RUN (a collection failure is not a pass): "
        f"{counts}\n{result.stdout[-1200:]}"
    )


def test_e2e_regression_client_suite_passes_except_the_darwin_setsid_hook(
        gitleaks_required, jq_required):
    """AC-F7.2, second leg: the whole client suite, with ONE sanctioned exception.

    The exception is the pre-existing darwin failure recorded in
    ``test/phase_gates/*_e2e_config.json:regression_known_failures``: the hook's kick uses ``setsid``,
    which macOS does not have (the pilot host is WSL2/Linux). It is pinned to its exact node
    id AND to its platform, so it tolerates that one failure and nothing else — and on a
    platform that has ``setsid`` it tolerates nothing at all.

    This module excludes ITSELF from the run (``--ignore``): the client suite contains this
    gate, and a gate that re-runs itself as its own regression leg never terminates.
    """
    # The self-exclusion is checked before it is relied on: a `--ignore` that stopped
    # matching would turn this leg into an infinite regress that nobody notices until it
    # times out.
    collected = _run_suite(["--collect-only", f"--ignore={GATE_MODULE}", CLIENT_SUITE],
                           timeout=600)
    assert collected.returncode == 0, collected.stdout[-2000:]
    assert GATE_MODULE not in collected.stdout, (
        f"--ignore={GATE_MODULE} must keep this gate out of the suite it drives:\n"
        f"{collected.stdout[-2000:]}"
    )
    assert "test_phase3_e2e.py" in collected.stdout, (
        "...while the rest of the e2e suite must still be collected (a --ignore that "
        f"swallowed everything would be a silent pass):\n{collected.stdout[-2000:]}"
    )

    result = _run_suite([f"--ignore={GATE_MODULE}", CLIENT_SUITE], timeout=3600)
    counts = _counts(result)
    failed = _failed_ids(result)

    assert counts["passed"] >= 400, (
        f"the client suite must actually have RUN: {counts}\n{result.stdout[-1200:]}"
    )
    unexpected = [node for node in failed if node != SANCTIONED_FAILURE]
    if unexpected or counts["error"]:
        pytest.fail(
            f"AC-F7.2: the ONLY sanctioned client-suite failure is {SANCTIONED_FAILURE} "
            f"(darwin, setsid).\n{result.stdout[-1200:]}\n"
            f"counts={counts} UNEXPECTED IN THE DRIVEN CLIENT RUN: {unexpected}"
        )
    if failed:
        assert sys.platform == SANCTIONED_PLATFORM, (
            f"{SANCTIONED_FAILURE} is sanctioned on {SANCTIONED_PLATFORM} only, and this "
            f"host is {sys.platform!r} — a platform with setsid must run it green"
        )
        assert counts["failed"] == len(failed) == 1, (
            f"exactly one sanctioned failure is tolerated: {counts} / {failed}"
        )
