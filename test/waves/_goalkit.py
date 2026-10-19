"""Shared ACCESSORY helpers for the frozen wave goals.

Implementers MAY extend this module (add helpers/cases) but MUST NOT change existing
helper semantics in ways that weaken the goal assertions — the assertions in
``test/waves/*/test_goal_*.py`` are the frozen oracle.

Everything here builds REAL boundaries: real tmp filesystems, real git repos, real
subprocesses. Only the seams the waves do not own (the Claude CLI, the central server
in unit-scope goals, the host schedulers) are stubbed/shimmed.
"""
from __future__ import annotations

import fcntl
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_PROMPT = REPO_ROOT / "prompts" / "summarizer_v1.md"

TEST_REMOTE_URL = "git@github.com:example-group/phase2-goal.git"
TEST_REMOTE_CANONICAL = "github.com/example-group/phase2-goal"

# Validated 2026-07-30 against gitleaks 8.30.1: rule generic-api-key, entropy 5.29.
# The phase-1 regex set provably returns [] for it (guard test in the E3 goal).
HIGH_ENTROPY_LINE = 'api_key = "zX9j2kQ8vB4nM7cL1wR5tY6uI3oP0aSdFgHjKlZxCvB"'
HIGH_ENTROPY_SECRET = "zX9j2kQ8vB4nM7cL1wR5tY6uI3oP0aSdFgHjKlZxCvB"
GITLEAKS_PIN = "8.30.1"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def make_git_repo(path: Path, remote_url: str = TEST_REMOTE_URL) -> Path:
    """Real git repo with a real ``origin`` remote (the processor gate reads it)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=path, check=True)
    return path


def transcript_lines(cwd: str, session_id: str, *, user_text: str = "Refactor the auth flow",
                     assistant_text: str = "Decided to keep bearer tokens.") -> list[dict]:
    """Realistic JSONL lines per contract/SCHEMA.md: metadata first (no cwd), then
    conversational lines carrying top-level ``cwd``/``uuid``/``message``."""
    return [
        {"type": "file-history-snapshot", "messageId": "m0", "snapshot": {}},
        {
            "type": "user", "uuid": f"{session_id}-u1", "cwd": cwd,
            "sessionId": session_id,
            "message": {"role": "user", "content": user_text},
        },
        {
            "type": "assistant", "uuid": f"{session_id}-a1", "cwd": cwd,
            "sessionId": session_id,
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": assistant_text}]},
        },
    ]


def write_transcript(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return path


def age_file(path: Path, seconds_ago: float) -> None:
    """Sets mtime ``seconds_ago`` in the past (the sweep settle heuristic reads it)."""
    ts = time.time() - seconds_ago
    os.utime(path, (ts, ts))


def make_claude_stub(path: Path, *, stdout: str, exit_code: int = 0) -> str:
    """Executable ``claude`` stub: prints ``stdout`` verbatim, ignores stdin."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\n"
        f"sys.stdout.write({stdout!r})\nsys.exit({exit_code})\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path)


def loom_home(tmp: Path) -> Path:
    home = tmp / "loom_home"
    for sub in ("queue", "quarantine", "failed", "log", "prompts", "hooks"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REAL_PROMPT, home / "prompts" / "summarizer_v1.md")
    return home


def write_config(home: Path, *, server_url: str, token: str = "goal-token",
                 claude_bin: str = "claude", settle_s: int = 1800,
                 transcripts_root: Path | None = None, sweep_enabled: bool = True,
                 scanner_engine: str = "auto",
                 gitleaks_version: str | None = GITLEAKS_PIN) -> Path:
    """Writes ``config.json`` with the FROZEN phase-2 keys and returns its path."""
    cfg = {
        "server_url": server_url,
        "token": token,
        "cache_ttl_s": 3600,
        "summarizer": {"model": "sonnet", "timeout_s": 300, "prompt_version": "v1"},
        "claude_bin": claude_bin,
        "prompt_path": str(home / "prompts" / "summarizer_v1.md"),
        "sweep": {
            "enabled": sweep_enabled,
            "settle_s": settle_s,
            "transcripts_root": str(transcripts_root) if transcripts_root else None,
        },
        "scanner": {"engine": scanner_engine, "gitleaks_version": gitleaks_version},
    }
    if cfg["sweep"]["transcripts_root"] is None:
        del cfg["sweep"]["transcripts_root"]
    path = home / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------- #
# PATH shims (record-replay stubs of host tools the waves do not own)           #
# ---------------------------------------------------------------------------- #
def shim_dir(tmp: Path) -> Path:
    d = tmp / "shims"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_shim(shims: Path, name: str, body: str) -> Path:
    """Executable shim named ``name``; ``body`` is a python3 script source."""
    path = shims / name
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def crontab_shim(shims: Path, state_file: Path) -> Path:
    """File-backed fake ``crontab``: ``-l`` prints the state, ``-`` replaces it from
    stdin; every invocation is appended to ``<state>.calls``."""
    return write_shim(shims, "crontab", f"""
import sys, pathlib
state = pathlib.Path({str(state_file)!r})
calls = pathlib.Path({str(state_file)!r} + ".calls")
calls.parent.mkdir(parents=True, exist_ok=True)
with calls.open("a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
if "-l" in sys.argv[1:]:
    if state.exists():
        sys.stdout.write(state.read_text())
        sys.exit(0)
    sys.stderr.write("no crontab\\n")
    sys.exit(1)
state.write_text(sys.stdin.read())
sys.exit(0)
""")


def systemctl_shim(shims: Path, log_file: Path) -> Path:
    """Fake ``systemctl`` that always succeeds and records every invocation."""
    return write_shim(shims, "systemctl", f"""
import sys, pathlib
log = pathlib.Path({str(log_file)!r})
log.parent.mkdir(parents=True, exist_ok=True)
with log.open("a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
if "is-system-running" in sys.argv[1:]:
    sys.stdout.write("running\\n")
if "is-enabled" in sys.argv[1:] or "is-active" in sys.argv[1:]:
    sys.stdout.write("active\\n")
sys.exit(0)
""")


# The allowlist the PATH-tail builders symlink. It deliberately contains NEITHER
# `crontab` nor `systemctl`: a goal that runs the real install.sh must never be able to
# reach the host schedulers (see `scheduler_free_path_with_gitleaks`).
SAFE_BIN_NAMES = ("python3", "git", "jq", "bash", "sh", "env", "cat", "mv", "cp",
                  "mkdir", "rm", "date", "diff", "grep", "sed", "chmod", "uname",
                  "dirname", "basename", "head", "tail", "tr", "wc", "sort", "touch",
                  "ln", "sleep", "mktemp", "readlink", "id", "awk", "cut", "find")


def _link_allowlist(dest: Path, names) -> Path:
    """Symlinks each resolvable name in ``names`` into ``dest`` (idempotent)."""
    dest.mkdir(parents=True, exist_ok=True)
    for name in names:
        target = shutil.which(name)
        if target and not (dest / name).exists():
            (dest / name).symlink_to(target)
    return dest


def base_path_with(shims: Path, *, include_gitleaks: bool) -> str:
    """A PATH whose head is ``shims`` and whose tail keeps the essentials (python,
    git, jq, ...). ``include_gitleaks=False`` guarantees no gitleaks anywhere by
    symlinking only an allowlist of binaries into a second directory."""
    if include_gitleaks:
        return f"{shims}{os.pathsep}{os.environ['PATH']}"
    safe = _link_allowlist(shims.parent / "safe_bin", SAFE_BIN_NAMES)
    return f"{shims}{os.pathsep}{safe}"


def scheduler_free_path_with_gitleaks(shims: Path) -> str:
    """Like ``base_path_with(shims, include_gitleaks=False)`` but with the REAL
    ``gitleaks`` reachable — the one host binary a phase-2 goal genuinely needs.

    ``base_path_with(shims, include_gitleaks=True)`` prepends ``shims`` to the WHOLE real
    PATH, which leaves every host tool the shims do not cover reachable — including
    ``crontab`` and ``systemctl``. That is unusable for any goal that runs the real
    ``client/install.sh``: on a host with a live user manager the installer would arm a
    systemd timer in the developer's own ``systemctl --user``, pointing at a temp
    LOOM_HOME that pytest then deletes.

    This tail is the allowlist (``SAFE_BIN_NAMES`` — no ``crontab``, no ``systemctl``)
    plus ``gitleaks``, in a directory of its own so the ``include_gitleaks=False``
    "no gitleaks anywhere" guarantee still holds for callers that rely on it.
    """
    safe = _link_allowlist(shims.parent / "safe_bin_gitleaks",
                           (*SAFE_BIN_NAMES, "gitleaks"))
    return f"{shims}{os.pathsep}{safe}"


# ---------------------------------------------------------------------------- #
# Real server subprocess (E4)                                                   #
# ---------------------------------------------------------------------------- #
def find_free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(db_path: Path, port: int, venv_python: str) -> subprocess.Popen:
    """REAL uvicorn subprocess with a temp LOOM_DB. Caller must terminate() it."""
    env = {**os.environ, "LOOM_DB": str(db_path)}
    return subprocess.Popen(
        [venv_python, "-m", "uvicorn", "server.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        env=env, cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def wait_health(url: str, timeout_s: float = 20.0) -> None:
    import requests

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if requests.get(f"{url}/api/health", timeout=2).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.3)
    raise TimeoutError(f"server at {url} did not become healthy")


def parse_admin_token(stdout: str) -> str:
    """Extracts the bearer token from ``server.admin adduser`` stdout.

    ACCESSORY REPAIR (wave i build, 2026-08-12): the waves i/j/k accessories
    took the LAST non-empty stdout line, but ``adduser`` prints an operator hint
    AFTER the token line, so the hint was used as the bearer token and every request
    401'd before a single frozen assertion could run. This is the same regex scan
    waves c/d have always used: it weakens no assertion — it lets the frozen
    assertions be reached at all.
    """
    for line in stdout.splitlines():
        candidate = line.strip()
        if len(candidate) >= 32 and re.fullmatch(r"[A-Za-z0-9_-]+", candidate):
            return candidate
    raise AssertionError(f"could not parse token from adduser output:\n{stdout}")


# ---------------------------------------------------------------------------- #
# Host-wide lock for the Compose waves (r, s)                                   #
# ---------------------------------------------------------------------------- #
COMPOSE_LOCK_ENV = "GOAL_COMPOSE_LOCK"
_compose_locks: dict[str, list] = {}   # realpath -> [fd, depth], this process's holds


def compose_lock_path() -> Path:
    """One path for every checkout and worktree of this user: the passwd home's cache,
    not ``$HOME`` (tests rewrite it) nor ``$TMPDIR`` (per-process on macOS).
    ``GOAL_COMPOSE_LOCK`` overrides it, for the lock's own tests."""
    override = os.environ.get(COMPOSE_LOCK_ENV)
    if override:
        return Path(override)
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    return home / ".cache" / "synaptic-loom" / "compose-waves.lock"


def _say_waiting(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


@contextmanager
def compose_host_lock(path: Path | None = None, *,
                      on_wait: Callable[[str], None] = _say_waiting) -> Iterator[None]:
    """Holds the host-wide lock the Compose waves share, blocking until it is free.

    ONE lock for every Compose wave, re-entrant within a process: r and s fix disjoint
    subnets and share a host by design, and one session holds both their stacks until it
    ends, so a lock per subnet set could deadlock two runs that reach them in opposite
    orders. ``flock`` belongs to the open file, so the kernel releases it however the
    holder exits, ``kill -9`` included. ``on_wait`` gets one line, only if it waits.
    """
    path = path or compose_lock_path()
    key = os.path.realpath(path)
    held = _compose_locks.get(key)
    if held is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                holder = path.read_text(encoding="utf-8").strip() or "an unknown process"
                on_wait(f"waiting for the Compose-wave host lock {path} (held by {holder})")
                fcntl.flock(fd, fcntl.LOCK_EX)
            os.ftruncate(fd, 0)
            os.pwrite(fd, f"pid {os.getpid()} in {REPO_ROOT}\n".encode(), 0)
        except BaseException:
            os.close(fd)
            raise
        held = _compose_locks[key] = [fd, 0]
    held[1] += 1
    try:
        yield
    finally:
        held[1] -= 1
        if held[1] == 0:
            del _compose_locks[key]
            os.ftruncate(held[0], 0)
            os.close(held[0])
