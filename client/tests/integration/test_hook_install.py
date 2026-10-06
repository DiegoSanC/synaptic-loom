"""Integration tests for the SessionEnd hook and the installer (PLAN.md §T5).

Hard rules for these tests:
- They ALWAYS run with ``HOME=<tmpdir>`` and ``LOOM_HOME=<tmpdir>/.loom``: the real
  ``~/.claude`` and ``~/.loom`` are NEVER touched.
- The processor "kick" is verified with an executable stub at
  ``$LOOM_HOME/venv/bin/python`` that records its argv in ``$LOOM_HOME/kick_argv.txt`` and
  its pid/pgid/sid in ``$LOOM_HOME/kick_ident.json`` — the real ``claude`` CLI / the real
  processor are NEVER invoked.
- The kick's launcher is exercised on every tier by BUILDING THE PATH, never by branching
  on ``sys.platform``: darwin's only relevant difference is that ``setsid`` (util-linux) is
  not on PATH, so a PATH without it reproduces the darwin condition deterministically, in
  CI, on Linux.
- ``install.sh`` runs with ``LOOM_SKIP_VENV=1`` so no real venv is built per test.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from loom import cli

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOK_SCRIPT = REPO_ROOT / "client" / "hooks" / "sessionend.sh"
INSTALL_SCRIPT = REPO_ROOT / "client" / "install.sh"
PAYLOAD_FILE = REPO_ROOT / "contract" / "sessionend_payload.json"
PROMPT_FILE = REPO_ROOT / "prompts" / "summarizer_v1.md"

PAYLOAD = json.loads(PAYLOAD_FILE.read_text(encoding="utf-8"))

# Clean summary a happy `claude` stub emits so the recovery leg reaches its POST
# (identical to test_processor.py's — the processor is not what these tests exercise).
CANNED_SUMMARY = (
    "## Contexto\nSe trabajó en el gate de proyectos.\n"
    "## Decisiones\n- **Usar SQLite** — equipo pequeño. Evidencia: db.py. Estado: vigente.\n"
    "## Pendientes\n- ninguno\n"
)

# Stub replacing $LOOM_HOME/venv/bin/python: records HOW it was launched and exits 0.
# Living in venv/bin/ means parents[2] == $LOOM_HOME, so it writes the records without
# reading the env.
#
# Two records, each written atomically (tmp + os.replace) so the poll below can never read
# half a file:
#   kick_argv.txt    the argv the hook handed it — the frozen contract.
#   kick_ident.json  pid/pgid/sid, written LAST, so its presence implies argv is complete.
#
# The identity record is what makes "the kick is detached" a DETERMINISTIC assertion rather
# than a timing one: a process with pid == pgid == sid is a session leader, which is exactly
# what `setsid` buys — and exactly what `nohup` and `( cmd & ) &` do NOT provide. They
# reparent to init and survive the hook exiting, so they satisfy any weaker "did the record
# appear" check while leaving the child inside the hook's own process group and session.
KICK_STUB_TEMPLATE = '''#!/usr/bin/env python3
import json, os, pathlib, sys, time

LOOM_HOME = pathlib.Path(__file__).resolve().parents[2]


def record(name, text):
    tmp = LOOM_HOME / (name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, LOOM_HOME / name)


record("kick_argv.txt", " ".join(sys.argv[1:]))
record("kick_ident.json", json.dumps({
    "pid": os.getpid(), "pgid": os.getpgid(0), "sid": os.getsid(0),
}))

LINGER = __LINGER__
if LINGER:
    time.sleep(LINGER)
    record("kick_done.txt", "the launched process ran to completion")
'''

# Every binary the hook shells out to. `setsid` and `python3` are the two launchers the
# kick tries, in that order; `git` is the issue-#4 attribution capture, needed only when
# the session's cwd still exists; the rest it needs on every path.
HOOK_TOOLS = ("bash", "sh", "env", "jq", "mkdir", "date", "cat", "mv", "rm", "git",
              "setsid", "python3")


# --------------------------------------------------------------------------- #
# Fixtures / helpers                                                          #
# --------------------------------------------------------------------------- #
@pytest.fixture
def home(tmp_path) -> Path:
    """Isolated temporary HOME; ~/.claude and ~/.loom hang off here."""
    h = tmp_path / "home"
    h.mkdir()
    return h


def _loom_home(home: Path) -> Path:
    return home / ".loom"


def _hook_env(home: Path, **extra) -> dict:
    """Environment to run the hook: isolated HOME + LOOM_HOME, real PATH (jq/setsid)."""
    env = {**os.environ, "HOME": str(home), "LOOM_HOME": str(_loom_home(home))}
    env.pop("LOOM_SUMMARIZER", None)
    env.update(extra)
    return env


def _write_kick_stub(loom_home: Path, *, linger: float = 0.0) -> Path:
    """Creates <loom_home>/venv/bin/python as a stub that records how it was launched.

    ``linger`` makes the stub sleep for that many seconds AFTER recording, so a caller can
    observe that the hook returned while the launched process was still alive.
    """
    py = loom_home / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text(KICK_STUB_TEMPLATE.replace("__LINGER__", repr(float(linger))),
                  encoding="utf-8")
    py.chmod(0o755)
    return py


def _install_kick_stub(home: Path, *, linger: float = 0.0) -> Path:
    """``_write_kick_stub`` against the $LOOM_HOME that hangs off a test HOME."""
    return _write_kick_stub(_loom_home(home), linger=linger)


def _fakebin(tmp_path: Path, *, omit: tuple[str, ...] = ()) -> Path:
    """A PATH directory holding the hook's tools (``HOOK_TOOLS``), minus ``omit``.

    Omitting ``setsid`` is how darwin is reproduced here: macOS does not ship util-linux's
    ``setsid`` and nothing else about the hook's launch line is platform-dependent, so an
    otherwise-complete PATH without it IS the darwin condition for this code path.
    """
    name = "fakebin_without_" + "_".join(omit) if omit else "fakebin_full"
    b = tmp_path / name
    b.mkdir(exist_ok=True)
    for tool in HOOK_TOOLS:
        if tool in omit or (b / tool).exists():
            continue
        real = shutil.which(tool)
        if real:
            (b / tool).symlink_to(real)
    for tool in omit:
        assert shutil.which(tool, path=str(b)) is None, (
            f"{tool} must NOT be reachable through this PATH — the shim is the whole point"
        )
    return b


def _kick_ident(home: Path) -> dict:
    """Waits for the launched process's identity record and returns it."""
    rec = _loom_home(home) / "kick_ident.json"
    assert _wait_for(rec), "the kick did not invoke the venv python"
    return json.loads(rec.read_text())


def _assert_session_leader(ident: dict, launcher: str) -> None:
    """The launched process must be its own session leader — the real AC.

    ``pid == pgid == sid`` is kernel-visible and race-free: a session leader is by
    definition outside the hook's process group AND its session, so it is immune both to a
    signal aimed at that group and to the hook's controlling terminal going away. This is
    the assertion that rejects `nohup` and `( cmd & ) &`, which satisfy "it outlived the
    hook" while providing neither property.
    """
    assert ident["pid"] == ident["pgid"] == ident["sid"], (
        f"the kick launched via {launcher} must be a session leader "
        f"(pid == pgid == sid), not merely a surviving child: {ident}"
    )


def _hook_log(home: Path) -> str:
    return (_loom_home(home) / "log" / "hook.log").read_text(encoding="utf-8")


def _assert_tier2_kick(home: Path, tmp_path: Path, *, why: str,
                       job_control: bool = False) -> None:
    """Runs the hook with `setsid` absent and asserts the python3 tier did its job.

    Shared by the two tests that differ only in the shell they run the hook in: the same
    tier, the same contract, two host conditions that break different naive fixes.
    """
    _install_kick_stub(home)
    env = _hook_env(home)
    env["PATH"] = str(_fakebin(tmp_path, omit=("setsid",)))

    if job_control:
        # `set -m` before the hook body: the same interpreter, job control on.
        argv = ["bash", "-c", 'set -m; exec bash "$1"', "bash", str(HOOK_SCRIPT)]
        proc = subprocess.run(argv, input=json.dumps(PAYLOAD), text=True,
                              capture_output=True, env=env)
    else:
        proc = _run_hook(home, json.dumps(PAYLOAD), env=env)

    assert proc.returncode == 0, proc.stderr
    assert _marker_path(home, PAYLOAD["session_id"]).exists()

    kick_rec = _loom_home(home) / "kick_argv.txt"
    assert _wait_for(kick_rec), f"{why}:\n{_hook_log(home)}\nstderr={proc.stderr}"
    # The argv is a frozen contract: the fallback launcher must hand the venv python the
    # SAME arguments, which is why the detacher may not be the venv python itself.
    assert kick_rec.read_text().strip() == "-m loom.processor --quiet"
    _assert_session_leader(_kick_ident(home), "python3")
    assert "launcher=python3" in _hook_log(home), _hook_log(home)


def _run_hook(home: Path, stdin: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        input=stdin,
        text=True,
        capture_output=True,
        env=env if env is not None else _hook_env(home),
    )


def _wait_for(path: Path, timeout: float = 5.0) -> bool:
    """Polls until ``path`` shows up — the kick is a detached, asynchronous process."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return path.exists()


def _marker_path(home: Path, session_id: str) -> Path:
    return _loom_home(home) / "queue" / f"{session_id}.json"


# --------------------------------------------------------------------------- #
# Hook: happy path                                                            #
# --------------------------------------------------------------------------- #
def test_hook_marks_transcript_and_kicks_processor(home):
    _install_kick_stub(home)
    hook_env = _hook_env(home)
    expected_launcher = (
        "setsid" if shutil.which("setsid", path=hook_env["PATH"]) else "python3"
    )
    proc = _run_hook(home, json.dumps(PAYLOAD), env=hook_env)

    assert proc.returncode == 0, proc.stderr

    marker = _marker_path(home, PAYLOAD["session_id"])
    assert marker.exists()
    data = json.loads(marker.read_text())
    assert data["session_id"] == PAYLOAD["session_id"]
    assert data["transcript_path"] == PAYLOAD["transcript_path"]
    assert data["cwd"] == PAYLOAD["cwd"]
    assert data["reason"] == PAYLOAD["reason"]
    # marked_at = UTC ISO-8601 (jq `now | todate`), ends in Z.
    assert data["marked_at"].endswith("Z") and "T" in data["marked_at"]

    # The kick fired with `-m loom.processor --quiet` (equivalent to `loom process`, T6).
    kick_rec = _loom_home(home) / "kick_argv.txt"
    assert _wait_for(kick_rec), "the kick did not invoke the venv python"
    assert kick_rec.read_text().strip() == "-m loom.processor --quiet"

    # ...and it is DETACHED, not merely launched. The hook prefers util-linux `setsid`
    # on Linux/WSL and falls back to Python's os.setsid() on macOS, so the host's PATH
    # determines which launcher the integration boundary must report.
    _assert_session_leader(_kick_ident(home), expected_launcher)
    assert f"launcher={expected_launcher}" in _hook_log(home), (
        "hook.log must name the tier that launched the kick — the failure this fix exists "
        f"for was invisible precisely because nothing was logged:\n{_hook_log(home)}"
    )


def test_hook_remark_overwrites_single_marker(home):
    _install_kick_stub(home)

    r1 = _run_hook(home, json.dumps(PAYLOAD))
    assert r1.returncode == 0
    r2 = _run_hook(home, json.dumps(PAYLOAD))
    assert r2.returncode == 0

    queue = _loom_home(home) / "queue"
    markers = list(queue.glob("*.json"))
    assert len(markers) == 1  # re-mark = overwrite, does not accumulate
    assert markers[0] == _marker_path(home, PAYLOAD["session_id"])
    # and no .tmp files are left dangling
    assert list(queue.glob("*.tmp")) == []


# --------------------------------------------------------------------------- #
# Hook: project attribution capture (issue #4 — contract/ATTRIBUTION.md §1)     #
# --------------------------------------------------------------------------- #
def _hook_repo(path: Path, remote_url: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=path, check=True)
    return path


def _marker_for(home: Path, payload: dict) -> dict:
    proc = _run_hook(home, json.dumps(payload))
    assert proc.returncode == 0, proc.stderr
    return json.loads(_marker_path(home, payload["session_id"]).read_text())


def test_hook_captures_the_raw_origin_of_a_live_cwd(home, tmp_path):
    """The captured url is the RAW one, byte for byte.

    Canonicalizing here would mean a second implementation of `normalize()` in jq/shell,
    which can drift from the Python authority — and a drift does not fail loudly, it
    files sessions under a key nobody else computes. The hook captures a string and makes
    no judgement."""
    _install_kick_stub(home)
    repo = _hook_repo(tmp_path / "repo", "git@github.com:org/b.git")
    payload = {**PAYLOAD, "session_id": "attr-live", "cwd": str(repo)}

    assert _marker_for(home, payload)["cwd_remote_url"] == "git@github.com:org/b.git"


def test_hook_omits_the_field_rather_than_writing_a_null(home, tmp_path):
    """A cwd that is not a repo, and a cwd that no longer exists, both OMIT the key.

    Absent is the one representation: it makes "the hook could not resolve it" and "the
    hook predates this feature" a single state, which the processor then needs one code
    path for instead of three."""
    _install_kick_stub(home)
    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    gone = tmp_path / "deleted_worktree"  # never created

    for label, cwd in (("not a repo", plain), ("does not exist", gone)):
        payload = {**PAYLOAD, "session_id": f"attr-none-{label.replace(' ', '-')}",
                   "cwd": str(cwd)}
        marker = _marker_for(home, payload)
        assert "cwd_remote_url" not in marker, f"{label}: the key must be absent"
        # The marker itself is never optional — the five original fields are intact.
        assert marker["session_id"] == payload["session_id"]
        assert marker["cwd"] == str(cwd)
        assert marker["marked_at"].endswith("Z")


def test_hook_redacts_credentials_before_they_reach_the_queue(home, tmp_path):
    """A tokenized clone url must never be written to disk.

    Agent worktrees are routinely cloned with `https://x-access-token:<tok>@host/...`.
    Written verbatim, that credential rests in queue/ — and indefinitely in failed/ once
    the marker is parked. `normalize()` discards userinfo anyway, so it is pure liability.

    The scp-like leg is the other half: `git@host:org/repo.git` has no `://` and its
    `git@` is a USERNAME. A redaction that stripped it would mangle every SSH remote."""
    _install_kick_stub(home)
    tokenized = _hook_repo(tmp_path / "tok",
                           "https://x-access-token:redact-me-0000@github.com/org/b.git")
    marker = _marker_for(home, {**PAYLOAD, "session_id": "attr-tok",
                                "cwd": str(tokenized)})
    assert marker["cwd_remote_url"] == "https://github.com/org/b.git"
    assert "redact-me-0000" not in _marker_path(home, "attr-tok").read_text()

    scp = _hook_repo(tmp_path / "scp", "git@github.com:org/b.git")
    scp_marker = _marker_for(home, {**PAYLOAD, "session_id": "attr-scp", "cwd": str(scp)})
    assert scp_marker["cwd_remote_url"] == "git@github.com:org/b.git", (
        "the scp-like form carries no credential and must survive byte for byte")


def test_hook_pins_the_capture_to_the_first_fire_that_could_answer(home, tmp_path):
    """SessionEnd fires more than once per session (reason=clear/resume), and the hook
    rebuilds the marker from stdin every time. That is idempotent for every field EXCEPT
    this one, which only the fire that ran while the worktree existed can produce.

    Two legs, because re-deriving fails in two different directions: erasing the answer,
    and replacing it with a recycled path's unrelated repo."""
    _install_kick_stub(home)
    repo = _hook_repo(tmp_path / "repo", "git@github.com:org/b.git")
    payload = {**PAYLOAD, "session_id": "attr-pin", "cwd": str(repo), "reason": "clear"}
    assert _marker_for(home, payload)["cwd_remote_url"] == "git@github.com:org/b.git"

    # Leg 1: the directory is gone by the final fire → the capture must survive.
    shutil.rmtree(repo)
    final = {**payload, "reason": "prompt_input_exit"}
    marker = _marker_for(home, final)
    assert marker["cwd_remote_url"] == "git@github.com:org/b.git", (
        "a later fire must not erase what the first one captured")
    assert marker["reason"] == "prompt_input_exit", (
        "only the capture is pinned; every other field still tracks the latest fire")

    # Leg 2: the path is recycled for a DIFFERENT repo → the capture must not follow it.
    _hook_repo(tmp_path / "repo", "git@github.com:org/stranger.git")
    assert _marker_for(home, final)["cwd_remote_url"] == "git@github.com:org/b.git", (
        "the capture belongs to the session, not to whatever now occupies its path")


def test_hook_without_git_still_marks_the_session(home, tmp_path):
    """`git` absent from PATH degrades exactly like a non-repo cwd: omit the field, log
    one line, still write the marker, still exit 0. A session's shutdown may never be
    blocked or failed by this feature."""
    repo = _hook_repo(tmp_path / "repo", "git@github.com:org/b.git")
    payload = {**PAYLOAD, "session_id": "attr-nogit", "cwd": str(repo)}
    env = _hook_env(home, PATH=str(_fakebin(tmp_path, omit=("git",))))

    proc = _run_hook(home, json.dumps(payload), env=env)

    assert proc.returncode == 0, proc.stderr
    marker = json.loads(_marker_path(home, "attr-nogit").read_text())
    assert "cwd_remote_url" not in marker
    assert "git not found in PATH" in _hook_log(home), _hook_log(home)


# --------------------------------------------------------------------------- #
# Hook: the kick's launcher tiers (issue #3 — portability, macOS has no setsid) #
# --------------------------------------------------------------------------- #
def test_hook_kicks_processor_without_setsid_on_path(home, tmp_path):
    """The kick fires, with the contracted argv, and still detaches, when `setsid` is gone.

    This is the darwin defect, reproduced on Linux by removing `setsid` from PATH. Before
    the tiered launcher, bash reported `setsid: command not found` INTO the background
    job's stderr — which the launch line redirects at processor.log — so the hook exited 0,
    the marker was written, and nothing ever ran it until the 15-minute periodic pass.
    """
    _assert_tier2_kick(home, tmp_path, why="with no `setsid` the kick must still fire")


def test_hook_kick_detaches_even_with_job_control_enabled(home, tmp_path):
    """`set -m` makes every background job its own process-group leader.

    That matters because ``os.setsid()`` raises EPERM when the caller is ALREADY a process
    group leader. A fallback launcher that calls ``setsid()`` without forking first passes
    every test written against a plain non-interactive shell and then fails silently on a
    host whose shell enables job control — the failure going, again, to processor.log.

    Same tier and same assertions as the test above; what differs is the shell it runs in,
    which is the entire point — this is the host condition that separates a forking
    launcher from a naive one.
    """
    _assert_tier2_kick(
        home, tmp_path, job_control=True,
        why="the fallback launcher must fork BEFORE setsid(), or job control makes it EPERM",
    )


def test_doctor_probe_lists_match_the_hook_they_report_on():
    """`cli.ATTRIBUTION_PROBES` / `cli.KICK_LAUNCHERS` and the hook's own `command -v`
    chain are two statements of ONE fact.

    They are deliberately not shared: the hook cannot import from the venv it is about to
    launch, and having doctor parse the shell script would be worse than the duplication.
    What makes the duplication safe is this assertion — a doctor row naming a tool the
    hook does not actually try, or silent about one it does, would be worse than no row.

    Exact in both directions, `jq` included and IN ORDER, so ANY new `command -v` probe in
    the hook has to be acknowledged here rather than drifting in unnoticed. Issue #4 added
    three (git, timeout, gtimeout) and this assertion is what forced them to be declared.
    """
    probed = re.findall(r"command -v (\S+) >/dev/null", HOOK_SCRIPT.read_text(encoding="utf-8"))
    declared = ["jq", *cli.ATTRIBUTION_PROBES, *cli.KICK_LAUNCHERS]

    assert probed == declared, (
        f"the hook probes {probed}, but `loom doctor` reports on {declared} "
        "— the rows and the behavior they describe have drifted apart"
    )


def test_hook_returns_while_the_launched_processor_is_still_running(home, tmp_path):
    """The kick is fire-and-forget: the hook must never wait on the processor.

    A SessionEnd hook that blocks delays the user's session shutdown by however long a
    processor pass takes (network, the summarizer CLI). Asserted on both launcher tiers,
    because a fallback that forgot the trailing `&` would be nonblocking on neither.
    """
    linger = 10.0
    for omit in ((), ("setsid",)):
        tier = "python3" if omit else "setsid"
        sub = home / f"tier_{tier}"
        sub.mkdir()
        _install_kick_stub(sub, linger=linger)
        env = _hook_env(sub)
        env["PATH"] = str(_fakebin(tmp_path, omit=omit))

        started = time.monotonic()
        proc = _run_hook(sub, json.dumps(PAYLOAD), env=env)
        elapsed = time.monotonic() - started

        assert proc.returncode == 0, proc.stderr
        ident = _kick_ident(sub)
        try:
            # The hook came back long before the child could finish...
            assert elapsed < linger / 2, (
                f"the {tier} kick made the hook wait {elapsed:.2f}s on a child that "
                f"sleeps {linger}s — the launch is blocking"
            )
            # ...and the child is provably still alive and still unfinished, so the
            # timing above is not a stub that silently never ran.
            assert not (_loom_home(sub) / "kick_done.txt").exists()
            os.kill(ident["pid"], 0)  # raises if it is already gone
        finally:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(ident["pid"], signal.SIGKILL)


def test_hook_marker_survives_when_no_launcher_is_available(home, tmp_path, env,
                                                            monkeypatch):
    """With neither `setsid` nor `python3`: log the refusal, keep the marker, exit 0.

    Deliberately NOT a `nohup` tier. `nohup` would launch something — in the hook's own
    process group and session — while logging a successful kick, converting a loud,
    diagnosable gap into a quiet conditional one. A logged refusal plus the periodic pass
    is the better failure (owner decision, 2026-09-13).

    The recovery half is asserted for real: `loom process` afterwards drains the very
    marker this hook wrote and posts the entry.
    """
    payload = {**PAYLOAD, "transcript_path": str(env.transcript), "cwd": str(env.repo)}
    # The venv python must EXIST, or the hook short-circuits on `[ -x "$PY" ]` and never
    # reaches the launcher tiers this test is about.
    _write_kick_stub(env.home)
    hook_env = {**os.environ, "HOME": str(home), "LOOM_HOME": str(env.home)}
    hook_env.pop("LOOM_SUMMARIZER", None)
    hook_env["PATH"] = str(_fakebin(tmp_path, omit=("setsid", "python3")))

    proc = _run_hook(home, json.dumps(payload), env=hook_env)

    assert proc.returncode == 0, proc.stderr
    marker = env.home / "queue" / f"{payload['session_id']}.json"
    assert marker.exists(), "a kick that cannot launch must not cost us the marker"
    data = json.loads(marker.read_text())
    assert data["session_id"] == payload["session_id"]
    assert data["transcript_path"] == str(env.transcript)
    assert data["marked_at"].endswith("Z")

    time.sleep(0.3)
    assert not (env.home / "kick_argv.txt").exists(), "nothing may be launched"
    assert "kick skipped (no session-detach launcher)" in (
        env.home / "log" / "hook.log").read_text(encoding="utf-8")

    # The periodic pass is the recovery path, and it really recovers this marker.
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 7}, status=201
    )
    env.config(env.make_stub(stdout=CANNED_SUMMARY))
    monkeypatch.setenv("LOOM_HOME", str(env.home))

    assert cli.main(["process", "--quiet"]) == 0
    assert not marker.exists(), "`loom process` must consume the marker the hook left"
    assert len(env.posted_entries()) == 1


# --------------------------------------------------------------------------- #
# Hook: paths that must NEVER break the session shutdown                       #
# --------------------------------------------------------------------------- #
def test_hook_summarizer_guard_no_marker_no_kick(home):
    _install_kick_stub(home)
    proc = _run_hook(home, json.dumps(PAYLOAD), env=_hook_env(home, LOOM_SUMMARIZER="1"))

    assert proc.returncode == 0
    # Neither marker nor kick: the summarizer's session does not self-mark (anti-recursion).
    assert not _marker_path(home, PAYLOAD["session_id"]).exists()
    time.sleep(0.3)
    assert not (_loom_home(home) / "kick_argv.txt").exists()
    # In fact the guard exits before even creating the queue.
    assert not (_loom_home(home) / "queue").exists()


def test_hook_malformed_stdin_exits_zero_without_marker(home):
    _install_kick_stub(home)
    proc = _run_hook(home, "this is not json {{{ <<< garbage")

    assert proc.returncode == 0  # does not crash the session shutdown
    queue = _loom_home(home) / "queue"
    assert not queue.exists() or list(queue.glob("*.json")) == []
    time.sleep(0.3)
    assert not (_loom_home(home) / "kick_argv.txt").exists()


def test_hook_empty_stdin_exits_zero(home):
    proc = _run_hook(home, "")
    assert proc.returncode == 0
    queue = _loom_home(home) / "queue"
    assert not queue.exists() or list(queue.glob("*.json")) == []


def test_hook_missing_jq_exits_zero(home, tmp_path):
    """With no jq on PATH the hook logs the failure and exits 0 (no marker, no crash)."""
    # Minimal PATH with the tools the hook uses BEFORE jq (mkdir, date for the log), but
    # WITHOUT jq. `_fakebin` asserts the omission for us.
    fakebin = _fakebin(tmp_path, omit=("jq",))

    env = _hook_env(home)
    env["PATH"] = str(fakebin)
    proc = _run_hook(home, json.dumps(PAYLOAD), env=env)

    assert proc.returncode == 0
    assert not _marker_path(home, PAYLOAD["session_id"]).exists()
    # the reason was recorded in hook.log
    hook_log = _loom_home(home) / "log" / "hook.log"
    assert hook_log.exists()
    assert "jq" in hook_log.read_text()


# --------------------------------------------------------------------------- #
# install.sh                                                                  #
# --------------------------------------------------------------------------- #
def _run_install(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "HOME": str(home),
        "LOOM_HOME": str(_loom_home(home)),
        "LOOM_SKIP_VENV": "1",  # don't build a real venv in every test
        # These phase-1 tests run install.sh with the REAL PATH, so step 5 would reach
        # the host scheduler and write the developer's actual crontab (or systemd user
        # units) on every run — HOME being a tmpdir does not isolate `crontab`. The
        # trigger's own behavior is covered by test_install_update.py and by
        # test/waves/b, both of which PATH-shim the schedulers.
        "LOOM_SKIP_TRIGGER": "1",
    }
    return subprocess.run(
        ["bash", str(INSTALL_SCRIPT), *args],
        text=True,
        capture_output=True,
        env=env,
    )


def _settings(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def _loom_commands(settings: dict) -> list[str]:
    """Commands of every SessionEnd command hook (to count the loom entries)."""
    out = []
    for group in settings.get("hooks", {}).get("SessionEnd", []):
        for h in group.get("hooks", []):
            out.append(h.get("command", ""))
    return out


def test_install_merges_into_existing_settings_preserving_content(home):
    # pre-existing settings.json with UNRELATED keys + an existing PreToolUse hook.
    settings_path = _settings(home)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    existing = {
        "model": "opus",
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo pre"}]}
            ]
        },
    }
    settings_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    proc = _run_install(home, "--yes")
    assert proc.returncode == 0, proc.stderr

    result = json.loads(settings_path.read_text())
    # previous content UNTOUCHED
    assert result["model"] == "opus"
    assert result["hooks"]["PreToolUse"] == existing["hooks"]["PreToolUse"]
    # loom hook added, pointing at $LOOM_HOME/hooks/sessionend.sh
    hook_dst = str(_loom_home(home) / "hooks" / "sessionend.sh")
    assert _loom_commands(result) == [hook_dst]

    # backup created (settings.json.bak-<epoch>)
    backups = list(settings_path.parent.glob("settings.json.bak-*"))
    assert len(backups) == 1
    # the backup keeps the original content (without the loom hook)
    assert _loom_commands(json.loads(backups[0].read_text())) == []

    # hook and prompt copied into LOOM_HOME
    assert Path(hook_dst).exists() and os.access(hook_dst, os.X_OK)
    assert (_loom_home(home) / "prompts" / "summarizer_v1.md").read_text() == PROMPT_FILE.read_text()

    # directory tree created
    for sub in ("queue", "quarantine", "failed", "log", "prompts", "hooks"):
        assert (_loom_home(home) / sub).is_dir()


def test_install_rerun_is_idempotent_no_duplicate_hook(home):
    r1 = _run_install(home, "--yes")
    assert r1.returncode == 0, r1.stderr
    r2 = _run_install(home, "--yes")
    assert r2.returncode == 0, r2.stderr

    result = json.loads(_settings(home).read_text())
    hook_dst = str(_loom_home(home) / "hooks" / "sessionend.sh")
    assert _loom_commands(result) == [hook_dst]  # exactly one entry, not duplicated
    assert "already installed" in r2.stdout  # the 2nd run reports it

    # the 2nd run must not create a backup (it did not modify settings)
    assert list(_settings(home).parent.glob("settings.json.bak-*")) == []


def test_install_creates_settings_when_absent(home):
    assert not _settings(home).exists()

    proc = _run_install(home, "--yes")
    assert proc.returncode == 0, proc.stderr

    settings_path = _settings(home)
    assert settings_path.exists()
    result = json.loads(settings_path.read_text())  # valid JSON
    hook_dst = str(_loom_home(home) / "hooks" / "sessionend.sh")
    assert _loom_commands(result) == [hook_dst]
    # created from scratch, so there is no backup of a previous user settings
    assert list(settings_path.parent.glob("settings.json.bak-*")) == []
