"""Phase-3 E2E gate (P2.F9) — the phase-2-robustness claims, proven end to end.

The repo's OWN gate suite for the phase that added crash recovery (P2.F1/F2), sweep
economics (P2.F4), trigger parity (P2.F5) and clean uninstall (P2.F6). Like
``test_phase2_e2e.py`` it runs against a **real boundary**:

- a REAL ``uvicorn server.app:app`` subprocess on a random free port with a temp ``LOOM_DB``;
- REAL ``python -m server.admin`` for the user/token, the REAL HTTP API for everything else;
- REAL git repos (the processor's remote gate runs ``git remote get-url origin``);
- the REAL ``gitleaks`` binary (engine ``auto``);
- REAL ``loom`` CLI subprocesses (``process``, ``status``), run the way the trigger runs them;
- the REAL ``client/install.sh`` (install **and** ``--uninstall-trigger``).

Only the seams this project does not own are faked: the Claude CLI (a stub that echoes back
which halves of the transcript actually reached it — we are testing capture, not the LLM)
and the host schedulers (PATH-shimmed recorders).

What this file adds on top of ``test_phase2_e2e.py`` (which stays green and untouched):

- **AC-F9.1 recovery** — the gate the phase-2 record explicitly left open: "crashed-session
  recovery is proven for cleanly-truncated transcripts and NOT proven for torn ones". Two
  real tears (mid-line, and mid-multi-byte-character), each then *resumed*, each proving both
  content halves land as entries through the real API.
- **AC-F9.2 cost** — an ``sys.addaudithook``-instrumented pass counts every file the pass
  opens under the transcripts root. Over a settled, fully-processed tree the count must be
  zero (the 21 GiB/day class, closed by P2.F4).
- **AC-F9.3 parity** — the command line the installer wrote, executed under a minimal
  trigger-like PATH that cannot see gitleaks, must still quarantine a seeded high-entropy
  secret *via the gitleaks engine*, asserted against the quarantine record's findings.
- **AC-F9.4 uninstall** — real ``install.sh --uninstall-trigger`` leaves **no crontab** when
  loom's block was all there was, and leaves a seeded ``@daily`` foreign entry byte-exact.

**Isolation is a hard requirement of this file**, and so is host safety. Every subprocess
runs with a temp ``HOME``, a temp ``LOOM_HOME``, a temp ``LOOM_DB``, a random port, an
explicit ``sweep.transcripts_root`` under ``tmp_path`` — and a PATH built from a private
symlink allowlist rather than from the machine's own. Neither the real ``crontab`` nor the
real ``systemctl`` is reachable from any PATH constructed here, which every builder asserts
before it is used: a wave-c goal once armed a real WSL2 user timer because it appended the
real PATH to its shims. The real schedulers are never invoked, not even to read.

**Skips are scoped to the test that genuinely needs the tool, never module-wide.** A
module-level ``skipif`` turns a missing binary into a whole green-looking phase gate that
executed nothing, and the two AC-F9.4 crontab tests need no scanner at all. Each test declares
its own tool via the ``gitleaks_required`` / ``jq_required`` fixtures, and when ``LOOM_GATE=1``
is set — how CI and the phase gate run this file — a missing tool is a ``pytest.fail`` instead
of a skip, so the gate can never be satisfied by an unrun suite.

Self-contained on purpose: this file must NEVER import ``test/waves/_goalkit.py`` or anything
else under the wave-goal tree — the repo's suite does not depend on build artifacts.
"""
from __future__ import annotations

import hashlib
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

REMOTE_URL = "git@github.com:example-group/phase3-e2e.git"

# The same high-entropy token class as AC-2.3a: the real gitleaks catches it (rule
# ``generic-api-key``) and the phase-1 regex set provably does not. That asymmetry is what
# makes AC-F9.3 a real parity proof rather than a tautology.
PLANTED_SECRET = "qF7t2LmX9vD4pR8sK1nB6wZ3yH5jC0aEgUiOoPlKmNb"
PLANTED_SECRET_LINE = f'api_key = "{PLANTED_SECRET}"'
GITLEAKS_RULE_ID = "generic-api-key"

# Content markers planted in the transcript halves either side of the tear. The summarizer
# stub echoes back exactly the ones it was fed, so an entry's body is evidence of which half
# of the torn transcript actually reached the summarizer — not of what the test hoped.
PRE_TEAR_TOKEN = "pre-tear-half-alpha"
POST_TEAR_TOKEN = "post-tear-half-beta"
# ...and a marker that lives ONLY on the torn line itself, past the cut, so it exists nowhere
# on disk until the resume heals that line. POST_TEAR_TOKEN alone cannot prove the healed line
# was consumed: it is also carried by the ordinary line appended after the resume, and the
# stub collapses duplicate markers — so an entry body would look identical if `read_delta`
# classified the healed line as an interior tear and dropped it (a live product path, counted
# as ``torn`` and logged ``torn_lines_skipped``). This token is the falsifier for that.
HEALED_LINE_TOKEN = "healed-torn-line-gamma"
# The accented text that the multi-byte tear cuts in half. Asserting it survives verbatim is
# what distinguishes a genuinely healed line from one decoded with ``errors="replace"``.
ACCENTED_TEXT = "résumé of the café"

pytestmark = [pytest.mark.e2e]

# A missing binary must not be able to green-light this gate by skipping it away. Under
# LOOM_GATE=1 (the phase gate / CI) it is a hard failure; interactively it degrades to a skip
# on the individual test that needs the tool, never on the whole module.
LOOM_GATE = os.environ.get("LOOM_GATE") == "1"


def _require_tool(name: str, why: str) -> None:
    if shutil.which(name) is not None:
        return
    reason = f"phase-3 e2e needs the real {name} binary: {why}"
    if LOOM_GATE:
        pytest.fail(f"LOOM_GATE=1 forbids skipping — {reason}")
    pytest.skip(reason)


@pytest.fixture
def gitleaks_required():
    _require_tool("gitleaks", "the scanner engine is 'auto' and must resolve gitleaks")


@pytest.fixture
def jq_required():
    _require_tool("jq", "this test runs the real client/install.sh, which parses JSON with jq")


# --------------------------------------------------------------------------- #
# Host-safe PATH construction (wave-c lesson, asserted rather than assumed)     #
# --------------------------------------------------------------------------- #
# Everything a legitimate pass plausibly execs, and NOTHING that schedules: no crontab, no
# systemctl, no launchctl, no at/batch. `gitleaks` is deliberately absent — it is added only
# where a scenario means to make it reachable.
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


def _minimal_path(tmp_path: Path) -> str:
    """The trigger-like baseline: the essentials, no gitleaks, no scheduler at all.

    This is what a cron/systemd-launched pass sees before any assignment the installed
    trigger carries. It stands in for cron's own minimal PATH, but it is built from a
    private symlink allowlist rather than written out as the bare system-bin pair cron
    actually defaults to — on Linux that literal pair resolves the developer's REAL
    crontab, and a scheduler this suite can reach for real is the wave-c incident.
    """
    path = str(_symlink_allowlist(tmp_path / "minimal_bin", _ALLOWED_BINS))
    _assert_scheduler_free(path)
    assert shutil.which("gitleaks", path=path) is None, (
        "the minimal trigger PATH must NOT resolve gitleaks — that absence is the "
        "whole premise of the AC-F9.3 parity check"
    )
    return path


def _gitleaks_toolbox(tmp_path: Path) -> Path:
    """A *copy* of the real gitleaks in a controlled temp dir.

    A copy rather than a symlink so that any ``/opt/homebrew`` or ``/usr/local`` string
    later found in what the installer wrote is proof of a baked-in host guess instead of
    genuine install-time resolution.
    """
    dest = tmp_path / "toolbox"
    dest.mkdir(parents=True, exist_ok=True)
    binary = dest / "gitleaks"
    if not binary.exists():
        real = shutil.which("gitleaks")
        assert real is not None, "the gitleaks_required fixture should have skipped/failed"
        shutil.copy2(real, binary)
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
    """Scheduler-free PATH *with* gitleaks: the interactive-shell baseline."""
    path = os.pathsep.join([
        str(_symlink_allowlist(tmp_path / "safe_bin", _ALLOWED_BINS)),
        str(_gitleaks_toolbox(tmp_path)),
    ])
    _assert_scheduler_free(path)
    assert shutil.which("gitleaks", path=path) is not None
    return path


# --------------------------------------------------------------------------- #
# Real server subprocess (the test_phase2_e2e.py boot pattern)                  #
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
        _wait_health(self.url)

    def terminate(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=15)


@pytest.fixture
def server(tmp_path):
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


def _register_project(url: str, token: str) -> int:
    resp = requests.post(f"{url}/api/projects", json={"remote_url": REMOTE_URL},
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
# Client-side plumbing (temp LOOM_HOME, real git repo, real transcripts)        #
# --------------------------------------------------------------------------- #
def _git_repo(path: Path, env: dict) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, env=env, check=True, timeout=60)
    subprocess.run(["git", "remote", "add", "origin", REMOTE_URL], cwd=path, env=env,
                   check=True, timeout=60)
    return path


def _loom_home(root: Path) -> Path:
    home = root / "loom_home"
    for sub in ("queue", "quarantine", "failed", "log", "prompts", "hooks"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REAL_PROMPT, home / "prompts" / "summarizer_v1.md")
    return home


def _write_config(home: Path, *, server_url: str, token: str, claude_bin: str,
                  transcripts_root: Path, engine: str = "auto") -> Path:
    cfg = {
        "server_url": server_url,
        "token": token,
        "cache_ttl_s": 3600,
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


def _echoing_claude_stub(path: Path) -> str:
    """Summarizer stub that reports WHICH content reached it.

    A fixed-string stub would make "both halves landed" unfalsifiable: the entry body would
    look the same whether the delta carried one half or both. This one echoes back, from
    stdin only:

    - the half-markers it found (``pre-tear-…``/``post-tear-…``), collapsed and sorted;
    - the healed-line marker, which lives past the tear on the torn line alone — so it is
      evidence that the healed line itself was consumed, not merely that *some* post-tear
      line was;
    - the accented phrase the multi-byte tear splits, echoed VERBATIM when present, plus the
      number of U+FFFD replacement characters in the delta. Together those two make a
      ``errors="replace"`` decode (or a dropped non-ASCII tail) visible in the entry body,
      which an ASCII-only marker cannot do.

    The shebang is an absolute interpreter because this also has to run under the minimal
    trigger PATH, where ``/usr/bin/env python3`` is not guaranteed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"#!{sys.executable}\n"
        "# -*- coding: utf-8 -*-\n"
        "import re, sys\n"
        "delta = sys.stdin.read()\n"
        "found = sorted(set(re.findall("
        "r'(?:pre|post)-tear-half-[a-z]+|healed-torn-line-[a-z]+', delta)))\n"
        f"accented = {ACCENTED_TEXT!r}\n"
        "sys.stdout.write('## Decision\\n'\n"
        "                 '- **Keep the torn-tail hold** — the pointer parks before it.\\n'\n"
        "                 '- halves-in-delta: ' + (', '.join(found) or '(none)') + '\\n'\n"
        "                 '- accented-text: ' + (accented if accented in delta\n"
        "                                        else '(absent)') + '\\n'\n"
        "                 '- replacement-chars: %d\\n' % delta.count('\\ufffd'))\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path)


def _fixed_claude_stub(path: Path, *, stdout: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdin.read()\n"
        f"sys.stdout.write({stdout!r})\n",
        encoding="utf-8",
    )
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


def _markers(loom_home: Path) -> list[Path]:
    """Everything still pending in the queue/ directory."""
    return sorted((loom_home / "queue").glob("*.json"))


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _state(loom_home: Path) -> dict:
    path = loom_home / "state.json"
    return _read_json(path) if path.exists() else {}


def _processor_log(loom_home: Path) -> str:
    path = loom_home / "log" / "processor.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _assert_status_clean(loom_home: Path, home: Path, path: str) -> str:
    """Real ``loom status`` must report an idle machine: nothing pending, nothing parked."""
    status = _loom(["status"], loom_home=loom_home, home=home, path=path, timeout=120)
    assert status.returncode == 0, status.stdout + status.stderr
    assert "Pending markers (queue/): 0" in status.stdout, status.stdout
    assert "Parked (failed/): 0" in status.stdout, status.stdout
    assert "Quarantine (quarantine/): 0" in status.stdout, status.stdout
    assert "(no recent errors)" in status.stdout, status.stdout
    assert "adapter_error" not in status.stdout, status.stdout
    return status.stdout


# --------------------------------------------------------------------------- #
# Transcript construction, down to the byte (a real tear is not a text edit)    #
# --------------------------------------------------------------------------- #
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


def _healthy_transcript(path: Path, session_id: str, cwd: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = (
        _jsonl({"type": "file-history-snapshot", "messageId": "m1", "snapshot": {}})
        + _jsonl(_user_line(session_id, cwd, 1, "Refactor the auth flow"))
        + _jsonl(_assistant_line(session_id, cwd, 1, "Decided to keep bearer tokens."))
    )
    path.write_bytes(blob)
    return path


def _torn_transcript(path: Path, session_id: str, cwd: str, *, multibyte: bool) -> bytes:
    """A transcript killed mid-write, and the bytes the resume still owes it.

    Three complete lines carrying the PRE-tear half, then a fourth line cut off partway —
    the file the kernel is left holding when a session is SIGKILLed mid-``write``. Returns
    the remainder, so the caller can *resume* the session by finishing that very line.

    ``multibyte=True`` cuts INSIDE a UTF-8 sequence, which is the harsher variant: the tail
    is not merely unparseable JSON, it is undecodable bytes. The phase-2 record called out
    exactly this case ("the same symptom ... when the tear landed inside a multi-byte
    character") as unproven.
    """
    if multibyte:
        post_text = (f"{POST_TEAR_TOKEN}: {ACCENTED_TEXT} migration — decided to retry. "
                     f"{HEALED_LINE_TOKEN}")
    else:
        post_text = (f"{POST_TEAR_TOKEN}: after the resume, decided to retry the migration. "
                     f"{HEALED_LINE_TOKEN}")

    head = (
        _jsonl({"type": "file-history-snapshot", "messageId": "m1", "snapshot": {}})
        + _jsonl(_user_line(session_id, cwd, 1, f"{PRE_TEAR_TOKEN}: audit the token store"))
        + _jsonl(_assistant_line(session_id, cwd, 1,
                                 f"{PRE_TEAR_TOKEN}: decided to rotate the pilot token."))
    )
    post_line = _jsonl(_assistant_line(session_id, cwd, 2, post_text))

    if multibyte:
        # Cut one byte into the two-byte 'é' of "résumé": the trailing lone 0xC3 makes the
        # line undecodable, which is a different failure path from bad JSON.
        marker = "é".encode("utf-8")
        idx = post_line.index(marker)
        cut = idx + 1
        assert post_line[:cut].endswith(b"\xc3")
        try:
            post_line[:cut].decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:  # pragma: no cover - guards the fixture, not the product
            raise AssertionError("the mid-multibyte tear must not decode as UTF-8")
    else:
        # A few bytes PAST the post-tear marker, not at len//2: with a realistic pytest
        # tmp_path the halfway point lands inside the long `cwd` field, before the message
        # text, and then "the marker is not in the pre-tear entry" would be true because it
        # is not on disk at all — a vacuous assertion, and vacuous by accident of the temp
        # path's length, so differently vacuous per machine. Cutting here makes the marker
        # genuinely part of the truncated line.
        cut = post_line.index(POST_TEAR_TOKEN.encode()) + len(POST_TEAR_TOKEN.encode()) + 8
        assert cut < len(post_line), "the cut must leave something for the resume to owe"
        post_line[:cut].decode("utf-8")  # valid UTF-8, invalid JSON: a plain mid-line tear
        try:
            json.loads(post_line[:cut].decode("utf-8"))
        except ValueError:
            pass
        else:  # pragma: no cover - guards the fixture, not the product
            raise AssertionError("the mid-line tear must not parse as JSON")

    # What the tear leaves on disk, asserted rather than assumed, for BOTH variants: the
    # post-tear marker IS inside the truncated bytes (so "the pre-tear entry does not carry
    # it" is a claim about the summarizer, not about an absent marker), and the healed-line
    # marker is NOT (so finding it in an entry can only mean the resumed line was consumed).
    assert POST_TEAR_TOKEN.encode() in post_line[:cut], (
        "the post-tear marker must survive the cut, or its absence from the pre-tear entry "
        "proves nothing"
    )
    assert HEALED_LINE_TOKEN.encode() not in post_line[:cut], (
        "the healed-line marker must live past the cut — it is the resume's own witness"
    )
    assert HEALED_LINE_TOKEN.encode() in post_line[cut:]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(head + post_line[:cut])
    return post_line[cut:]


def _resume_session(path: Path, owed: bytes, session_id: str, cwd: str) -> None:
    """The session comes back and finishes the line it was killed inside, then goes on."""
    with path.open("ab") as fh:
        fh.write(owed)
        fh.write(_jsonl(_user_line(session_id, cwd, 2, "and after the crash?")))
        fh.write(_jsonl(_assistant_line(session_id, cwd, 3,
                                        f"{POST_TEAR_TOKEN}: confirmed, migration retried.")))


class Client:
    """A configured temp client: LOOM_HOME + git repo + transcripts root + claude stub."""

    def __init__(self, tmp_path: Path, home: Path, path: str, server: Server, token: str,
                 *, claude_bin: str | None = None, engine: str = "auto") -> None:
        self.tmp_path = tmp_path
        self.os_home = home
        self.path = path
        self.loom_home = _loom_home(tmp_path)
        self.repo = _git_repo(tmp_path / "repo", _base_env(home=home, path=path))
        self.transcripts_root = tmp_path / "transcripts"
        self.transcripts_root.mkdir(parents=True, exist_ok=True)
        self.claude_bin = claude_bin or _echoing_claude_stub(tmp_path / "claude_stub.py")
        _write_config(self.loom_home, server_url=server.url, token=token,
                      claude_bin=self.claude_bin,
                      transcripts_root=self.transcripts_root, engine=engine)

    def process(self, *, expect_ok: bool = True) -> subprocess.CompletedProcess:
        """One ``loom process`` pass — exactly what the 15-min trigger runs."""
        cp = _loom(["process"], loom_home=self.loom_home, home=self.os_home, path=self.path)
        if expect_ok:
            assert cp.returncode == 0, f"loom process failed:\n{cp.stdout}\n{cp.stderr}"
        return cp

    def status_is_clean(self) -> str:
        return _assert_status_clean(self.loom_home, self.os_home, self.path)


# --------------------------------------------------------------------------- #
# AC-F9.1 — a torn transcript, resumed: both halves land                        #
# --------------------------------------------------------------------------- #
def _prove_torn_then_resumed_recovery(tmp_path, home, path, server, *, multibyte: bool):
    """Shared body of the two AC-F9.1 variants (plain mid-line tear / mid-multibyte tear).

    The claim under test, in order: the sweep finds a torn orphan at all; the pointer parks
    *before* the tear instead of failing forever; the PRE-tear half is delivered as a real
    entry; the resumed session's POST-tear half is delivered as a second, correctly chained
    entry; the queue drains; ``loom status`` is clean; nothing is parked in ``failed/``.
    """
    label = "mb" if multibyte else "ml"
    session_id = f"sess-torn-{label}"
    token = _adduser(server.db, f"e2e-torn-{label}")
    project_id = _register_project(server.url, token)
    client = Client(tmp_path, home, path, server, token)

    transcript = client.transcripts_root / "-Users-nobody-elsewhere" / f"{session_id}.jsonl"
    owed = _torn_transcript(transcript, session_id, str(client.repo), multibyte=multibyte)
    _settle(transcript)

    # --- pass 1: the torn file is swept and the PRE-tear half is captured ---------------
    first_pass = client.process()
    log = _processor_log(client.loom_home)
    assert f"{session_id}\tsweep_enqueued" in log, (
        f"the sweep must enqueue a torn orphan:\n{first_pass.stdout}\n{log}"
    )
    assert f"{session_id}\theld_lines_at_eof\theld=1" in log, (
        "the trailing torn run must be reported as held (session-scoped, with its count), "
        "not silently dropped and not attributed to some other session:\n" + log
    )

    entries = _entries(server.url, token, project_id)
    assert len(entries) == 1, f"the pre-tear half must land:\n{first_pass.stdout}"
    pre_entry = entries[0]
    assert pre_entry["session_id"] == session_id
    assert PRE_TEAR_TOKEN in pre_entry["content_md"], pre_entry["content_md"]
    assert POST_TEAR_TOKEN not in pre_entry["content_md"], (
        "the summarizer cannot have seen content that is still torn on disk"
    )
    assert HEALED_LINE_TOKEN not in pre_entry["content_md"], (
        "the healed-line marker does not exist on disk yet — only the resume writes it"
    )
    assert pre_entry["covered_from"] is None
    assert pre_entry["covered_to"] == f"{session_id}-a1"

    # The pointer parks BEFORE the tear (P2.F1 rule 2): three complete lines consumed, the
    # fourth held because it may still heal. That is the whole difference between this and
    # the pre-phase-2 behaviour, where the marker failed on every pass forever.
    pointer = _state(client.loom_home)[str(transcript)]
    assert pointer["last_line"] == 3, pointer
    assert pointer["last_uuid"] == f"{session_id}-a1"
    assert _markers(client.loom_home) == [], "the queue must drain, not strand the marker"
    assert list((client.loom_home / "failed").glob("*.json")) == [], (
        "a torn transcript is recoverable — it must NOT be parked as a failure"
    )
    client.status_is_clean()

    # A second pass over the still-torn file must be a no-op: no duplicate entry, no
    # re-enqueue loop on a tear that has not healed yet.
    client.process()
    assert len(_entries(server.url, token, project_id)) == 1
    assert _markers(client.loom_home) == []

    # --- the session is resumed: the torn line is finished, more turns follow -----------
    _resume_session(transcript, owed, session_id, str(client.repo))
    _settle(transcript)

    second_pass = client.process()
    entries = _entries(server.url, token, project_id)
    assert len(entries) == 2, (
        f"the post-tear half must land after the resume:\n{second_pass.stdout}\n"
        f"{_processor_log(client.loom_home)}"
    )
    post_entry = next(e for e in entries if e["id"] != pre_entry["id"])
    assert POST_TEAR_TOKEN in post_entry["content_md"], post_entry["content_md"]
    # THE falsifier for this AC. POST_TEAR_TOKEN above is also carried by the ordinary line
    # appended after the resume, and the stub collapses duplicates, so the body — and every
    # covered_from/covered_to/last_line below — is byte-identical whether the healed line
    # reached the summarizer or `read_delta` reclassified it as an interior tear and dropped
    # it. This marker exists only on the healed line, past the cut.
    assert HEALED_LINE_TOKEN in post_entry["content_md"], (
        "AC-F9.1: the healed torn line was not delivered — the entry carries the lines "
        f"around it but not the line the resume repaired:\n{post_entry['content_md']}"
    )
    # ...and it was not delivered by luck: the recovered pass must not have skipped ANY line
    # of this session as an interior tear.
    assert f"{session_id}\ttorn_lines_skipped" not in _processor_log(client.loom_home), (
        "the healed line (or a neighbour) was counted as an interior tear and dropped:\n"
        + _processor_log(client.loom_home)
    )
    if multibyte:
        # The tear split a UTF-8 sequence, so "the line came back" is not enough: it must
        # come back INTACT. The stub echoes the accented phrase verbatim when it finds it on
        # stdin, and counts U+FFFD, so a lossy `errors="replace"` heal — or a dropped
        # non-ASCII tail — fails here instead of sliding past an ASCII-only marker.
        assert ACCENTED_TEXT in post_entry["content_md"], (
            "AC-F9.1 (multi-byte): the split character did not survive the heal — the "
            f"summarizer never saw {ACCENTED_TEXT!r}:\n{post_entry['content_md']}"
        )
        assert "- replacement-chars: 0\n" in post_entry["content_md"], (
            "the healed delta reached the summarizer with U+FFFD replacement characters "
            f"in it:\n{post_entry['content_md']}"
        )
        assert "�" not in post_entry["content_md"], post_entry["content_md"]
    # Correct chaining: the second entry starts exactly where the first one stopped, so the
    # healed line was consumed once — not skipped, and not delivered twice.
    assert post_entry["covered_from"] == pre_entry["covered_to"] == f"{session_id}-a1"
    assert post_entry["covered_to"] == f"{session_id}-a3"

    assert _state(client.loom_home)[str(transcript)]["last_line"] == 6
    assert _markers(client.loom_home) == []
    assert list((client.loom_home / "failed").glob("*.json")) == []
    client.status_is_clean()

    # Idempotent afterwards: the timer keeps firing forever and must produce nothing.
    client.process()
    assert len(_entries(server.url, token, project_id)) == 2
    assert _markers(client.loom_home) == []


def test_ac_f9_1_transcript_torn_mid_line_then_resumed_lands_both_halves(
        tmp_path, safe_home, safe_path, server):
    """AC-F9.1: a real repo session torn mid-line, then resumed, loses neither half.

    This closes the gate the phase-2 record left open verbatim: "crashed-session recovery is
    proven for cleanly-truncated transcripts and NOT proven for torn ones"."""
    _prove_torn_then_resumed_recovery(tmp_path, safe_home, safe_path, server,
                                      multibyte=False)


def test_ac_f9_1_transcript_torn_mid_multibyte_then_resumed_lands_both_halves(
        tmp_path, safe_home, safe_path, server):
    """AC-F9.1 (multi-byte variant): the tear lands inside a UTF-8 sequence.

    The undecodable-bytes path is a different classifier branch from bad JSON, and it is the
    one the RUNBOOK described as producing bare ``UnicodeDecodeError`` lines forever."""
    _prove_torn_then_resumed_recovery(tmp_path, safe_home, safe_path, server,
                                      multibyte=True)


# --------------------------------------------------------------------------- #
# AC-F9.2 — a steady-state pass opens zero transcript files                     #
# --------------------------------------------------------------------------- #
# Instrumented in-process with sys.addaudithook: the ``open`` audit event fires for every
# builtin ``open`` and every ``os.open`` under the interpreter, so a pass cannot read a
# transcript behind this counter's back. Counting opens (not bytes) is the honest unit: the
# claim P2.F4 makes is that a settled, already-processed transcript is resolved by ``stat``
# alone, and ``stat`` raises no ``open`` event.
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


def _instrumented_pass(tmp_path: Path, client: Client, tag: str) -> tuple[list[str], str]:
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
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300,
    )
    assert cp.returncode == 0, f"instrumented pass failed:\n{cp.stdout}\n{cp.stderr}"
    assert report.exists(), f"the audit runner produced no report:\n{cp.stdout}{cp.stderr}"
    return json.loads(report.read_text(encoding="utf-8")), cp.stdout


def test_ac_f9_2_steady_state_pass_over_a_settled_tree_opens_zero_transcripts(
        tmp_path, safe_home, safe_path, server):
    """AC-F9.2: once the tree is settled and processed, a pass reads no transcript at all.

    The pre-P2.F4 cost was a full re-read of every settled transcript on every pass — the
    measured 21 GiB/day class. Two instrumented passes make the claim falsifiable: the FIRST
    pass (nothing captured yet) must open the transcripts, so the counter is provably not
    blind; the SECOND, over the same now-settled and fully-processed tree, must open zero —
    and runs with every transcript chmod'ed 0o000, so "zero opens" is enforced by the kernel
    as well as observed by the hook (the hook alone cannot see a child process's reads).
    """
    token = _adduser(server.db, "e2e-cost")
    project_id = _register_project(server.url, token)
    client = Client(tmp_path, safe_home, safe_path, server, token,
                    claude_bin=_fixed_claude_stub(
                        tmp_path / "claude_stub.py",
                        stdout="## Decision\n- **Keep the stat-skip above _line_count.**\n"))

    transcripts = []
    for n in range(4):
        transcript = (client.transcripts_root / f"-Users-nobody-p{n % 2}"
                      / f"sess-cost-{n}.jsonl")
        _healthy_transcript(transcript, f"sess-cost-{n}", str(client.repo))
        _settle(transcript)
        transcripts.append(transcript)

    # --- pass 1: the tree is NOT yet settled work — every transcript is read ------------
    opened_cold, _ = _instrumented_pass(tmp_path, client, "cold")
    assert len(set(opened_cold)) == len(transcripts), (
        "the instrumentation must SEE the reads it is meant to count; a cold pass reads "
        f"every transcript: {sorted(set(opened_cold))}"
    )
    assert len(_entries(server.url, token, project_id)) == len(transcripts)

    # --- pass 2: steady state over the settled, processed tree -------------------------
    # The audit hook is per-interpreter and is NOT inherited across fork+exec, so on its own
    # it would measure zero for a regression that shelled the read out (`wc -l`, `grep -c`).
    # Belt and braces: make the read physically impossible too. `stat` needs no read
    # permission, so an honest stat-skip pass is untouched by this, while ANY open — in this
    # interpreter or a child's — fails with EACCES and takes the pass's exit code with it.
    modes = {t: stat.S_IMODE(t.stat().st_mode) for t in transcripts}
    try:
        for transcript in transcripts:
            os.chmod(transcript, 0o000)
        # Proof that the mode really denies THIS process (and a loud failure rather than a
        # silently vacuous leg if the suite is ever run as root).
        with pytest.raises(PermissionError):
            transcripts[0].open("rb").close()
        opened_steady, stdout = _instrumented_pass(tmp_path, client, "steady")
    finally:
        for transcript, mode in modes.items():
            os.chmod(transcript, mode)
    assert opened_steady == [], (
        "AC-F9.2: a steady-state pass over a settled, processed tree must open ZERO "
        f"transcript files; it opened {opened_steady}\n{stdout}"
    )

    # The zero is the stat-skip firing, not the sweep having stopped looking: it still
    # scanned every transcript, and said so.
    last_run = _read_json(client.loom_home / "sweep_last_run.json")
    assert last_run["scanned"] == len(transcripts), last_run
    assert last_run["enqueued"] == 0 and last_run["skipped"] == len(transcripts), last_run
    sweep_lines = [ln for ln in _processor_log(client.loom_home).splitlines()
                   if "sweep_pass" in ln]
    assert sweep_lines, _processor_log(client.loom_home)
    assert f"stat_skips={len(transcripts)}" in sweep_lines[-1], (
        "every settled transcript must be resolved by stat alone — and this pass ran with "
        f"all of them unreadable, so it could not have cheated: {sweep_lines[-1]}"
    )
    assert len(_entries(server.url, token, project_id)) == len(transcripts)
    assert _markers(client.loom_home) == []

    # And a third pass stays at zero — the skip is a steady state, not a one-shot.
    opened_again, _ = _instrumented_pass(tmp_path, client, "steady2")
    assert opened_again == [], opened_again


# --------------------------------------------------------------------------- #
# AC-F9.3 — trigger-context scanner parity                                      #
# --------------------------------------------------------------------------- #
def _write_shim(shims: Path, name: str, body: str) -> Path:
    shims.mkdir(parents=True, exist_ok=True)
    path = shims / name
    path.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _crontab_shim(shims: Path, state_file: Path) -> Path:
    """File-backed ``crontab`` recorder: -l / - / -r, faithfully distinguished.

    "No crontab" is the ABSENCE of the state file and ``-r`` deletes it, so AC-F9.4 can tell
    "the crontab was removed" apart from "an empty crontab was installed" — the exact residue
    the macOS rehearsal found and P2.F6 fixed.
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


def _claude_version_shim(shims: Path) -> Path:
    version = (REPO_ROOT / "contract" / "VERSION").read_text(encoding="utf-8").strip()
    return _write_shim(shims, "claude", f"""
import sys
if "--version" in sys.argv:
    sys.stdout.write({version!r} + "\\n")
sys.exit(0)
""")


def _install_loom_wrapper(loom_home: Path) -> Path:
    """Stand-in for the venv console script the crontab command points at.

    ``LOOM_SKIP_VENV=1`` builds no venv, so the trigger's command line would otherwise point
    at nothing. A /bin/sh wrapper at exactly that path, exec-ing this checkout.
    """
    bin_dir = loom_home / "venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "loom"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'PYTHONPATH="{REPO_ROOT}/client{os.pathsep}{REPO_ROOT}"\n'
        "export PYTHONPATH\n"
        f'exec "{sys.executable}" -m loom.cli "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


def _run_install(args: list[str], *, home: Path, loom_home: Path, path: str,
                 timeout: int = 240) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(INSTALL_SH), *args],
        env=_base_env(home=home, path=path, LOOM_HOME=str(loom_home), LOOM_SKIP_VENV="1"),
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout,
    )


_ASSIGNMENT_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _cron_assignments(lines: list[str]) -> list[tuple[str, str]]:
    out = []
    for line in lines:
        if line.lstrip().startswith("#"):
            continue
        m = _ASSIGNMENT_RE.match(line)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def _cron_command(lines: list[str]) -> str:
    cmds = [ln for ln in lines
            if ln.strip() and not ln.lstrip().startswith("#")
            and not _ASSIGNMENT_RE.match(ln)]
    assert len(cmds) == 1, f"exactly one command line expected in the block: {cmds}"
    fields = cmds[0].split(None, 5)
    assert len(fields) == 6, f"not a five-field cron entry: {cmds[0]!r}"
    return fields[5]


def _seed_leaky_session(client: Client, session_id: str) -> Path:
    transcript = client.transcripts_root / "-Users-nobody-elsewhere" / f"{session_id}.jsonl"
    _healthy_transcript(transcript, session_id, str(client.repo))
    _settle(transcript)
    return transcript


def test_ac_f9_3_triggered_pass_under_a_minimal_path_still_quarantines_via_gitleaks(
        tmp_path, safe_home, server, gitleaks_required, jq_required):
    """AC-F9.3: trigger-context scanner parity, proven against quarantine metadata.

    The macOS rehearsal found that a scheduler's own minimal PATH cannot see a brew-installed
    gitleaks, so ``scanner.engine=auto`` silently downgraded the *triggered* scans to regex
    while an interactive ``loom doctor`` reported gitleaks — nothing surfaced it. P2.F5 makes
    the installer carry its install-time PATH into the trigger.

    Both legs run the very command line the installer wrote, under the same gitleaks-free
    minimal PATH. The control leg applies only ``LOOM_HOME=`` and shows the degradation for
    real (the secret is published). The parity leg applies the assignments the recorded block
    actually carries, and the secret must instead be quarantined by the **gitleaks** engine —
    asserted on the finding's rule id, which the regex set cannot produce.
    """
    token = _adduser(server.db, "e2e-parity")
    project_id = _register_project(server.url, token)

    minimal_path = _minimal_path(tmp_path)
    shims = tmp_path / "shims"
    cron_state = tmp_path / "crontab.txt"
    _crontab_shim(shims, cron_state)
    _claude_version_shim(shims)
    interactive_path = os.pathsep.join(
        [str(shims), minimal_path, str(_gitleaks_toolbox(tmp_path))])
    _assert_scheduler_free(interactive_path, shims=shims)
    assert shutil.which("gitleaks", path=interactive_path) == \
        str(_gitleaks_toolbox(tmp_path) / "gitleaks"), (
        "the install context must resolve ONLY the controlled gitleaks copy"
    )

    leaky = ("## Decision\n"
             "- **Rotate the pilot credential** — it was pasted into the session.\n"
             f"{PLANTED_SECRET_LINE}\n")
    client = Client(tmp_path, safe_home, interactive_path, server, token,
                    claude_bin=_fixed_claude_stub(tmp_path / "claude_stub.py",
                                                  stdout=leaky))
    _install_loom_wrapper(client.loom_home)

    installed = _run_install(["--yes"], home=safe_home, loom_home=client.loom_home,
                             path=interactive_path)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert "cron entry installed" in installed.stdout, installed.stdout
    lines = cron_state.read_text(encoding="utf-8").splitlines()
    assignments = _cron_assignments(lines)
    cron_cmd = _cron_command(lines)
    assert any(k == "LOOM_HOME" for k, _ in assignments), lines
    assert any(k == "PATH" for k, _ in assignments), (
        "P2.F5: the installed block must carry the install-time PATH, or the triggered "
        f"pass cannot resolve the same scanner engine:\n{lines}"
    )
    for label, text in (("crontab", "\n".join(lines)),
                        ("config.json", (client.loom_home / "config.json").read_text())):
        assert "/opt/homebrew" not in text and "/usr/local" not in text, (
            f"host-specific guess in {label}: parity must derive from install-time "
            f"resolution, not a hardcoded location:\n{text}"
        )

    # --- control leg: the scheduler's own minimal PATH, no carried assignments ----------
    # Documents (rather than assumes) that this PATH genuinely degrades the engine, which is
    # what makes the parity leg below a real result instead of a coincidence.
    _seed_leaky_session(client, "sess-parity-control")
    control_env = {"HOME": str(safe_home), "SHELL": "/bin/sh", "LOGNAME": "e2e",
                   "PATH": minimal_path, "LOOM_HOME": str(client.loom_home)}
    control = subprocess.run(["/bin/sh", "-c", cron_cmd], env=control_env,
                             cwd=str(safe_home), capture_output=True, text=True,
                             timeout=600)
    assert control.returncode == 0, control.stdout + control.stderr
    published = _entries(server.url, token, project_id)
    assert len(published) == 1 and PLANTED_SECRET in published[0]["content_md"], (
        "control leg: without the carried PATH the engine falls back to regex and the "
        f"secret is published — if that stopped being true, rewrite this leg:\n{published}"
    )
    assert list((client.loom_home / "quarantine").glob("*.json")) == []

    # --- parity leg: the same command, plus exactly the block's own assignments ---------
    _seed_leaky_session(client, "sess-parity-trigger")
    trigger_env = {"HOME": str(safe_home), "SHELL": "/bin/sh", "LOGNAME": "e2e",
                   "PATH": minimal_path}
    for key, value in assignments:      # the recorded block's lines, verbatim, in order
        trigger_env[key] = value
    assert shutil.which("gitleaks", path=trigger_env["PATH"]) is not None, (
        "the trigger environment must resolve gitleaks — that IS the parity property"
    )
    # The carried PATH is the install-time one, so it still contains the shim dir — the
    # recorder, never the host's crontab, and no systemctl at all.
    _assert_scheduler_free(trigger_env["PATH"], shims=shims)

    triggered = subprocess.run(["/bin/sh", "-c", cron_cmd], env=trigger_env,
                               cwd=str(safe_home), capture_output=True, text=True,
                               timeout=600)
    assert triggered.returncode == 0, triggered.stdout + triggered.stderr

    assert _entries(server.url, token, project_id) == published, (
        "AC-F9.3: the triggered pass published the secret-bearing entry instead of "
        "quarantining it — trigger-context parity is broken"
    )
    quarantined = sorted((client.loom_home / "quarantine").glob("*.json"))
    assert len(quarantined) == 1, f"expected one quarantine record: {quarantined}"
    record = _read_json(quarantined[0])
    assert record["reason"] == "secret_scan", record
    # The load-bearing assertion: quarantine METADATA, not a file count. The rule id is
    # gitleaks' own RuleID, so it is only present if the gitleaks engine produced the
    # verdict — the regex set finds nothing at all in this text, and a scanner that could
    # not run at all would show up as a `scan_error` finding (fail-closed, I4).
    kinds = [f["kind"] for f in record["findings"]]
    assert kinds, "the quarantine record must name what was found"
    assert GITLEAKS_RULE_ID in kinds, (
        f"AC-F9.3: the findings must come from the gitleaks engine, got kinds={kinds}"
    )
    assert "scan_error" not in kinds, (
        f"a fail-closed scan_error is not parity — the binary was not resolved: {kinds}"
    )
    for finding in record["findings"]:
        assert PLANTED_SECRET not in finding["redacted"], (
            "quarantine metadata must not repeat the secret verbatim (I5/I6)"
        )
    assert PLANTED_SECRET in record["entry"]["content_md"], (
        "the quarantined payload keeps the secret on purpose, for local review"
    )
    assert record["entry"]["session_id"] == "sess-parity-trigger", record["entry"]


# --------------------------------------------------------------------------- #
# AC-F9.4 — uninstall leaves no residue, and no collateral damage               #
# --------------------------------------------------------------------------- #
def _install_then_uninstall(tmp_path: Path, home: Path, seed: str | None):
    """Real install.sh install → real ``--uninstall-trigger``, on a shimmed crontab."""
    loom_home = home / ".loom"
    shims = tmp_path / "shims"
    cron_state = tmp_path / "crontab.txt"
    _crontab_shim(shims, cron_state)
    _claude_version_shim(shims)
    if seed is not None:
        cron_state.write_text(seed, encoding="utf-8")

    path = os.pathsep.join([str(shims), _minimal_path(tmp_path)])
    # Wave-c lesson, asserted before anything is executed: the only reachable crontab is the
    # recorder, and systemctl is not reachable at all (or install.sh takes the systemd branch
    # and arms a REAL user timer on this developer's machine).
    _assert_scheduler_free(path, shims=shims)

    installed = _run_install(["--yes"], home=home, loom_home=loom_home, path=path)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert cron_state.exists(), "the installer must have written the shimmed crontab"
    assert "# loom-process" in cron_state.read_text(encoding="utf-8")
    # The exact bytes the uninstall is about to destroy — what the RUNBOOK's restore step
    # has to be able to put back.
    before_uninstall = cron_state.read_bytes()

    removed = _run_install(["--uninstall-trigger"], home=home, loom_home=loom_home,
                           path=path)
    assert removed.returncode == 0, removed.stdout + removed.stderr
    return cron_state, shims, removed, before_uninstall


def test_ac_f9_4_uninstall_trigger_on_a_loom_only_crontab_leaves_no_crontab(
        tmp_path, safe_home, jq_required):
    """AC-F9.4: install → ``--uninstall-trigger`` on an otherwise-empty crontab.

    Installing the filtered (empty) content back would leave an empty-but-registered crontab
    that ``crontab -l`` still reports and that some cron implementations mail about — the
    residue the macOS rehearsal recorded as finding 2. Uninstall must remove the crontab
    itself, which this asserts through the shim's own ``-l``: "no crontab".
    """
    cron_state, shims, removed, before_uninstall = _install_then_uninstall(
        tmp_path, safe_home, seed=None)

    assert not cron_state.exists(), (
        "AC-F9.4: the crontab must be REMOVED, not rewritten empty — "
        f"it still holds {cron_state.read_text(encoding='utf-8')!r}"
    )
    calls = (cron_state.parent / (cron_state.name + ".calls")).read_text(encoding="utf-8")
    assert "-r" in calls.split(), f"uninstall must reach `crontab -r`: {calls!r}"

    listed = subprocess.run([str(shims / "crontab"), "-l"], capture_output=True, text=True,
                            timeout=60)
    assert listed.returncode != 0 and "no crontab" in listed.stderr, (
        f"`crontab -l` must report no crontab after uninstall: {listed.stderr!r}"
    )
    assert "no empty crontab left behind" in removed.stdout, removed.stdout

    # A backup is taken before `crontab -r`, which has no undo (RUNBOOK §8). Existence is
    # not the property the RUNBOOK relies on — the operator is told to restore the crontab
    # FROM this file, so a zero-byte or truncated backup is exactly as bad as none at all.
    backups = sorted((safe_home / ".loom" / "backup").glob("crontab.*"))
    assert len(backups) == 1, (
        f"exactly one backup is expected (the pre-`crontab -r` one): {backups}"
    )
    assert backups[0].read_bytes() == before_uninstall, (
        "AC-F9.4: the backup must be the crontab that was removed, byte for byte — "
        f"`crontab -r` is irreversible\nbacked up={backups[0].read_bytes()!r}\n"
        f"removed   ={before_uninstall!r}"
    )


def test_ac_f9_4_uninstall_trigger_keeps_a_seeded_daily_foreign_entry_byte_exact(
        tmp_path, safe_home, jq_required):
    """AC-F9.4: a foreign ``@daily`` entry survives install → uninstall byte for byte.

    The wave-b HIGH-1/MED-6 regression class: loom's own filter must claim only loom's own
    lines. Byte-exact is the bar — same lines, same order, same trailing newline — because a
    crontab that comes back "equivalent" is still a crontab someone else's job ran from.
    """
    foreign = (
        "# nightly backup, do not touch\n"
        "@daily /usr/local/bin/my-important-backup.sh\n"
        "0 3 * * * /usr/local/bin/rotate-logs.sh --verbose\n"
    )
    cron_state, shims, removed, _ = _install_then_uninstall(tmp_path, safe_home, seed=foreign)

    assert cron_state.exists(), (
        "AC-F9.4: a crontab holding foreign entries must never be removed outright"
    )
    after = cron_state.read_text(encoding="utf-8")
    assert after == foreign, (
        "AC-F9.4: the foreign entries must survive byte-exact\n"
        f"before={foreign!r}\nafter ={after!r}"
    )
    assert hashlib.sha256(after.encode("utf-8")).hexdigest() == \
        hashlib.sha256(foreign.encode("utf-8")).hexdigest()
    assert "@daily /usr/local/bin/my-important-backup.sh" in after
    assert "# loom-process" not in after and "/venv/bin/loom process" not in after, (
        f"every line of loom's block must be gone:\n{after}"
    )
    assert "every other entry kept" in removed.stdout, removed.stdout

    calls = (cron_state.parent / (cron_state.name + ".calls")).read_text(encoding="utf-8")
    assert "-r" not in calls.split(), (
        f"`crontab -r` must NEVER run while foreign entries exist: {calls!r}"
    )
