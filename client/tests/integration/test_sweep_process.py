"""Integration tests for the sweep → process chain (PHASE-2-3-SPEC R2.1, epic E1).

The sweep is never a standalone actor: ``processor.run_once`` calls it inside the
``flock`` it already holds (AC-2.1d) and the synthetic marker it leaves behind is
consumed by the NORMAL phase-1 pipeline in the same pass (AC-2.1a), without
duplicating anything on a second pass (AC-2.1b).

Boundaries are real (temporary LOOM_HOME, real transcripts tree, real git repo, real
flock); only the seams the client does not own are stubbed — the summarizer (an
executable stub) and the central server (pytest-httpserver), exactly as in
``test_processor.py``.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

from loom import processor, sweep
from loom.config import Config

# Canonical form of the test repo's remote (== conftest.TEST_REMOTE_CANONICAL).
TEST_REMOTE_CANONICAL = "github.com/org/b"

ORPHAN_SID = "sess-orphan-crash"

CANNED_SUMMARY = (
    "## Contexto\nLa sesión murió sin SessionEnd.\n"
    "## Decisiones\n- **Barrer huérfanos** — se recupera la sesión. Evidencia: sweep.py. "
    "Estado: vigente.\n## Pendientes\n- ninguno\n"
)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _transcript(path: Path, cwd: str, session_id: str, *, aged: float = 3600) -> Path:
    """Realistic transcript (metadata line + two conversational lines), aged so the
    settle heuristic considers the session dead."""
    lines = [
        {"type": "file-history-snapshot", "messageId": "m0", "snapshot": {}},
        {"type": "user", "uuid": f"{session_id}-u1", "cwd": cwd, "sessionId": session_id,
         "message": {"role": "user", "content": "¿Bearer o JWT?"}},
        {"type": "assistant", "uuid": f"{session_id}-a1", "cwd": cwd, "sessionId": session_id,
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "Bearer, por la revocación."}]}},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    ts = time.time() - aged
    os.utime(path, (ts, ts))
    return path


def _sweep_cfg(env, *, claude_bin: str, settle_s: int = 1800, enabled: bool = True) -> Config:
    """Config of the phase-1 fixture + a real, populated transcripts tree for the sweep."""
    root = env.home.parent / "claude_projects"
    root.mkdir(parents=True, exist_ok=True)
    return Config(
        loom_home=env.home,
        server_url=env.httpserver.url_for(""),
        token="test-token",
        claude_bin=claude_bin,
        prompt_path=env.prompt,
        sweep_enabled=enabled,
        sweep_settle_s=settle_s,
        sweep_transcripts_root=root,
    )


def _orphan(cfg: Config, env, *, sid: str = ORPHAN_SID, aged: float = 3600) -> Path:
    # The encoded directory name is lossy on purpose: only the content is authoritative.
    encoded = "-" + str(env.repo).lstrip("/").replace("/", "-").replace(".", "-")
    return _transcript(cfg.sweep_transcripts_root / encoded / f"{sid}.jsonl",
                       str(env.repo), sid, aged=aged)


def _actions(report) -> list[str]:
    return [o.action for o in report.outcomes]


# --------------------------------------------------------------------------- #
# The chain                                                                    #
# --------------------------------------------------------------------------- #
def test_orphan_is_swept_processed_and_not_duplicated(env):
    """AC-2.1a + AC-2.1b: one run_once sweeps, summarizes and posts; the next one
    finds nothing left to do."""
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 7}, status=201
    )
    cfg = _sweep_cfg(env, claude_bin=env.make_stub(stdout=CANNED_SUMMARY))
    transcript = _orphan(cfg, env)

    report = processor.run_once(cfg)

    assert _actions(report) == ["posted"]
    assert report.sweep is not None
    assert (report.sweep.scanned, report.sweep.enqueued) == (1, 1)
    assert report.sweep.enqueued_sessions == [ORPHAN_SID]

    bodies = env.posted_entries()
    assert len(bodies) == 1
    assert bodies[0]["session_id"] == ORPHAN_SID
    assert bodies[0]["project_remote"] == TEST_REMOTE_CANONICAL
    # The marker was consumed and the pointer advanced through the NORMAL pipeline.
    assert not list((env.home / "queue").glob("*.json"))
    pointer = env.state()[str(transcript)]
    assert pointer["last_line"] == 3 and pointer["last_uuid"] == f"{ORPHAN_SID}-a1"

    report2 = processor.run_once(cfg)

    assert _actions(report2) == []
    assert report2.sweep.enqueued == 0
    assert len(env.posted_entries()) == 1


def test_sweep_and_hook_markers_coexist_in_one_pass(env):
    """A hook marker already in the queue is processed in the same pass as the swept
    orphan, and its own transcript is not re-swept behind its back."""
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201
    )
    cfg = _sweep_cfg(env, claude_bin=env.make_stub(stdout=CANNED_SUMMARY))
    _orphan(cfg, env)
    # The hook's session lives in the same tree, but the hook already marked it.
    hooked = _transcript(cfg.sweep_transcripts_root / "-hooked" / "sess-hook.jsonl",
                         str(env.repo), "sess-hook")
    env.drop_marker("sess-hook", transcript=hooked, marked_at="2026-07-24T00:00:00Z")

    report = processor.run_once(cfg)

    assert sorted(o.session_id for o in report.outcomes) == ["sess-hook", ORPHAN_SID]
    assert _actions(report) == ["posted", "posted"]
    assert report.sweep.enqueued == 1  # only the orphan; the hooked one was queued
    assert sorted(b["session_id"] for b in env.posted_entries()) == ["sess-hook", ORPHAN_SID]


def test_hook_marker_is_never_replaced_by_a_synthetic_one(env):
    """The hook's marker carries the authoritative cwd/reason: the sweep must not
    touch it, even though the transcript looks like an orphan."""
    env.register_projects()
    cfg = _sweep_cfg(env, claude_bin=env.make_stub(stdout="NO_ENTRY"))
    transcript = _orphan(cfg, env)
    marker_path = env.drop_marker(ORPHAN_SID, transcript=transcript, cwd=env.repo)
    raw = marker_path.read_text(encoding="utf-8")

    # The sweep alone (same call the processor makes) leaves the marker untouched.
    report = sweep.run_sweep(cfg, {})

    assert report.enqueued == 0
    assert marker_path.read_text(encoding="utf-8") == raw


# --------------------------------------------------------------------------- #
# The stat pair the pointer carries (P2.F4 / p3-state-stat-fields)             #
# --------------------------------------------------------------------------- #
def _stat_pair(path: Path) -> tuple[int, int, int, int]:
    """The full record the pointer carries: ``(st_dev, st_ino, st_mtime_ns, st_size)``
    since the P2.F4 amendment (2026-08-10) added the inode half."""
    st = path.stat()
    return st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size


def _recorded_stat(entry: dict) -> tuple[int, int, int, int]:
    """The same four fields as read back off a state entry, in the same order."""
    return (entry["stat_dev"], entry["stat_ino"],
            entry["stat_mtime_ns"], entry["stat_size"])


def test_pointer_records_the_stat_pair_and_the_next_pass_reads_nothing(env, monkeypatch):
    """AC-F4.1: the pointer written by the real pipeline carries the transcript's
    ``(st_mtime_ns, st_size)``, and the following steady-state pass skips the file
    without opening it — the 21 GiB/day re-read is gone."""
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201
    )
    cfg = _sweep_cfg(env, claude_bin=env.make_stub(stdout=CANNED_SUMMARY))
    transcript = _orphan(cfg, env)
    before = _stat_pair(transcript)

    assert _actions(processor.run_once(cfg)) == ["posted"]

    entry = env.state()[str(transcript)]
    assert all(type(entry[f]) is int
               for f in ("stat_dev", "stat_ino", "stat_mtime_ns", "stat_size"))
    assert _recorded_stat(entry) == before, (
        "the recorded pair must describe the transcript as it was before the read")

    def boom(path):
        raise AssertionError(f"steady-state pass must not read {path}")

    monkeypatch.setattr(sweep, "_line_count", boom)
    report2 = processor.run_once(cfg)

    assert (report2.sweep.scanned, report2.sweep.enqueued, report2.sweep.skipped) == (1, 0, 1)
    assert len(env.posted_entries()) == 1


def test_append_racing_the_delta_read_is_recorded_as_the_pre_read_stat(env, monkeypatch):
    """AC-F4.2: an append that lands AFTER ``read_delta`` returns and BEFORE the
    pointer write must leave a STALE recorded pair — the mismatch is what makes the
    next pass re-read the raced line instead of skipping it forever."""
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201
    )
    cfg = _sweep_cfg(env, claude_bin=env.make_stub(stdout=CANNED_SUMMARY), settle_s=0)
    transcript = _orphan(cfg, env, aged=10)
    before = _stat_pair(transcript)
    real_read_delta = processor.read_delta

    def racing_read_delta(path, last_line):
        delta = real_read_delta(path, last_line)
        with Path(path).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(
                {"type": "assistant", "uuid": f"{ORPHAN_SID}-a2", "cwd": str(env.repo),
                 "sessionId": ORPHAN_SID,
                 "message": {"role": "assistant",
                             "content": [{"type": "text", "text": "raced"}]}}) + "\n")
        return delta

    monkeypatch.setattr(processor, "read_delta", racing_read_delta)
    assert _actions(processor.run_once(cfg)) == ["posted"]
    monkeypatch.undo()

    entry = env.state()[str(transcript)]
    assert entry["last_line"] == 3
    assert _recorded_stat(entry) == before, (
        "a POST-read capture would record the appended file and lose the raced line")
    assert _stat_pair(transcript) != before

    report2 = processor.run_once(cfg)

    assert report2.sweep.enqueued == 1
    assert len(env.posted_entries()) == 2
    assert env.state()[str(transcript)]["last_line"] == 4


def test_empty_delta_path_also_records_the_stat_pair(env):
    """The pointer advances on a delta with no conversation too (matrix
    ``dropped_empty_delta``); if that path skipped the capture, the entry would be
    left without the fields and re-read forever."""
    env.register_projects()
    cfg = _sweep_cfg(env, claude_bin=env.make_stub(stdout=CANNED_SUMMARY))
    transcript = _transcript(cfg.sweep_transcripts_root / "-meta" / "sess-meta.jsonl",
                             str(env.repo), "sess-meta")
    # Metadata-only content: a real cwd (so the sweep enqueues it) and no messages.
    transcript.write_text(json.dumps(
        {"type": "file-history-snapshot", "messageId": "m0", "cwd": str(env.repo)}) + "\n",
        encoding="utf-8")
    ts = time.time() - 3600
    os.utime(transcript, (ts, ts))
    before = _stat_pair(transcript)

    report = processor.run_once(cfg)

    assert _actions(report) == ["dropped_empty_delta"]
    entry = env.state()[str(transcript)]
    assert _recorded_stat(entry) == before


def test_torn_tail_pointer_written_by_the_real_pipeline_then_skips_and_resumes(env, monkeypatch):
    """P2.F4 × p3-adapter-tolerance, with NOTHING hand-crafted: E5's trailing-torn-hold
    parks the pointer before the half-written last line, so ``last_line`` legitimately
    sits below EOF. The pair recorded here covers the whole file (tear included), which
    is what makes the two compose: the unchanged torn file is skipped instead of being
    re-read every pass forever, and the moment the writer resumes it is re-read."""
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201
    )
    cfg = _sweep_cfg(env, claude_bin=env.make_stub(stdout=CANNED_SUMMARY))
    transcript = _orphan(cfg, env, sid="sess-torn-tail")
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write('{"type":"assistant","uuid":"sess-torn-tail-a2","message"')  # SIGKILL
    ts = time.time() - 3600
    os.utime(transcript, (ts, ts))
    torn_pair = _stat_pair(transcript)

    assert _actions(processor.run_once(cfg)) == ["posted"]

    entry = env.state()[str(transcript)]
    assert entry["last_line"] == 3, "E5 holds the torn tail: the pointer stays behind EOF"
    assert _recorded_stat(entry) == torn_pair, (
        "the pair must cover the file the read saw — tear included, not just the "
        "consumed prefix")

    # Steady state: pointer < EOF but the file has not moved → skip, and no read at all.
    def boom(path):
        raise AssertionError(f"the held file must not be re-read: {path}")

    with monkeypatch.context() as m:
        m.setattr(sweep, "_line_count", boom)
        held = processor.run_once(cfg)
    assert (held.sweep.enqueued, held.sweep.skipped) == (0, 1)
    assert len(env.posted_entries()) == 1

    # The session resumes: the torn line is completed and a new one follows.
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(':{"role":"assistant","content":[{"type":"text","text":"resumed"}]},'
                 '"cwd":' + json.dumps(str(env.repo)) + '}\n')
    os.utime(transcript, (ts, ts))  # older than updated_at: the naive-design trap

    resumed = processor.run_once(cfg)

    assert resumed.sweep.enqueued == 1, "changed bytes must defeat the skip"
    assert len(env.posted_entries()) == 2
    assert env.state()[str(transcript)]["last_line"] == 4


def test_pre_upgrade_pointer_is_re_read_and_then_carries_the_pair(env):
    """AC-F4.4: an entry written before the upgrade has no pair, so it is read (not
    skipped); once its transcript is processed again the pair is there."""
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201
    )
    cfg = _sweep_cfg(env, claude_bin=env.make_stub(stdout=CANNED_SUMMARY))
    transcript = _orphan(cfg, env)
    # A faithful phase-1 state.json: pointer behind the content, no stat fields.
    cfg.ensure_dirs()
    cfg.state_path.write_text(json.dumps(
        {str(transcript): {"last_line": 1, "last_uuid": None,
                           "updated_at": "2026-07-01T00:00:00Z"}}), encoding="utf-8")

    report = processor.run_once(cfg)

    assert report.sweep.enqueued == 1
    entry = env.state()[str(transcript)]
    assert entry["last_line"] == 3
    assert _recorded_stat(entry) == _stat_pair(transcript)


# --------------------------------------------------------------------------- #
# Concurrency (AC-2.1d) and isolation                                          #
# --------------------------------------------------------------------------- #
def test_sweep_does_not_run_when_another_processor_holds_the_lock(env):
    """The sweep has no lock of its own: if run_once cannot take the flock, nothing
    is swept at all."""
    cfg = _sweep_cfg(env, claude_bin=env.make_stub(stdout=CANNED_SUMMARY))
    _orphan(cfg, env)
    cfg.ensure_dirs()

    holder = open(cfg.lock_path, "w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        report = processor.run_once(cfg)
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    assert report.lock_held is True
    assert report.sweep is None
    assert not list((env.home / "queue").glob("*.json"))
    assert not cfg.sweep_last_run_path.exists()

    # Once the lock is free the very same orphan is swept: nothing was lost.
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201
    )
    assert processor.run_once(cfg).sweep.enqueued == 1


def test_sweep_is_a_noop_when_disabled(env):
    env.register_projects()
    cfg = _sweep_cfg(env, claude_bin=env.make_stub(stdout=CANNED_SUMMARY), enabled=False)
    _orphan(cfg, env)

    report = processor.run_once(cfg)

    assert report.sweep is None
    assert not list((env.home / "queue").glob("*.json"))
    assert not cfg.sweep_last_run_path.exists()
    assert env.posted_entries() == []


def test_sweep_crash_is_isolated_and_reported(env, monkeypatch):
    """A sweep failure behaves like a marker failure: logged, surfaced, and the hook
    markers are still processed."""
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201
    )
    cfg = _sweep_cfg(env, claude_bin=env.make_stub(stdout=CANNED_SUMMARY))
    env.drop_marker("sess-hook")

    def boom(*_a, **_k):
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(sweep, "run_sweep", boom)
    report = processor.run_once(cfg)

    assert report.sweep is None
    assert _actions(report) == ["error", "posted"]
    assert report.errors == 1
    assert [b["session_id"] for b in env.posted_entries()] == ["sess-hook"]
    assert "RuntimeError: sweep exploded" in (env.home / "log" / "processor.log").read_text()


def test_marker_rewrites_never_touch_the_hooks_in_flight_temp(env):
    """The processor rewrites markers in place (attempts, adapter_error,
    registry_error) while ``sessionend.sh`` writes ``<sid>.json.tmp`` in the same
    queue WITHOUT the lock: the rewrite must not use that name either."""
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"error": "project_not_found"}, status=404
    )
    cfg = _sweep_cfg(env, claude_bin=env.make_stub(stdout=CANNED_SUMMARY))
    marker = env.drop_marker("sess-404")
    # The hook is mid-write for the very session whose marker is about to be rewritten.
    hook_tmp = env.home / "queue" / "sess-404.json.tmp"
    hook_bytes = b'{"session_id": "sess-404", "cwd": "/hook'
    hook_tmp.write_bytes(hook_bytes)

    report = processor.run_once(cfg)

    assert _actions(report) == ["kept_registry_error"]
    assert json.loads(marker.read_text())["registry_error"] == "project_not_found"
    assert hook_tmp.read_bytes() == hook_bytes


def test_fresh_transcript_survives_until_it_settles(env):
    """The settle heuristic (Q-2.2) protects a session that is still alive: it is
    swept only once its transcript stops changing."""
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201
    )
    cfg = _sweep_cfg(env, claude_bin=env.make_stub(stdout=CANNED_SUMMARY), settle_s=1800)
    transcript = _orphan(cfg, env, aged=0)

    report = processor.run_once(cfg)
    assert (report.sweep.enqueued, report.sweep.skipped) == (0, 1)
    assert env.posted_entries() == []

    ts = time.time() - 3600
    os.utime(transcript, (ts, ts))
    report2 = processor.run_once(cfg)
    assert report2.sweep.enqueued == 1
    assert [b["session_id"] for b in env.posted_entries()] == [ORPHAN_SID]
