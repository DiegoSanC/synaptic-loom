"""FROZEN GOAL — E1 orphan-sweep (F2.1 / spec R2.1). Read-only to implementers.

Oracle properties: real tmp filesystem + real flock as the boundary; the summarizer
and the central server (not owned by this wave) are a stub binary and
pytest-httpserver. Authored adversarially: the cwd-trap (a directory name that
decodes to the WRONG path) breaks any implementation that derives cwd from the
encoded directory name instead of the transcript content (AC-2.1c).

Run: .venv/bin/python -m pytest test/waves/a/test_goal_e1_sweep.py -q
"""
from __future__ import annotations

import fcntl
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _goalkit as kit  # noqa: E402

from loom import processor, sweep  # noqa: E402
from loom.config import Config  # noqa: E402

SID = "e1-orphan-0001"
# The trap: this encoded directory name would decode to /Users/alice/Projects/webapp.
TRAP_DIR = "-Users-alice-Projects-webapp"


def _mk_cfg(tmp_path, *, settle_s: int = 1800, enabled: bool = True,
            claude_bin: str = "claude", server_url: str = "http://127.0.0.1:9",
            token: str = "goal-token") -> Config:
    home = kit.loom_home(tmp_path)
    root = tmp_path / "transcripts"
    root.mkdir(exist_ok=True)
    return Config(
        loom_home=home, server_url=server_url, token=token,
        claude_bin=claude_bin, prompt_path=home / "prompts" / "summarizer_v1.md",
        sweep_enabled=enabled, sweep_settle_s=settle_s, sweep_transcripts_root=root,
        scanner_engine="regex",
    )


def _orphan(cfg: Config, real_cwd: str, *, sid: str = SID, aged: float = 3600) -> Path:
    path = cfg.sweep_transcripts_root / TRAP_DIR / f"{sid}.jsonl"
    kit.write_transcript(path, kit.transcript_lines(real_cwd, sid))
    kit.age_file(path, aged)
    return path


# --------------------------------------------------------------------------- #
# read_cwd                                                                     #
# --------------------------------------------------------------------------- #
def test_read_cwd_comes_from_content_not_dirname(tmp_path):
    cfg = _mk_cfg(tmp_path)
    repo = kit.make_git_repo(tmp_path / "realrepo")
    t = _orphan(cfg, str(repo))
    assert sweep.read_cwd(t) == str(repo)
    assert sweep.read_cwd(t) != "/Users/alice/Projects/webapp"


def test_read_cwd_tolerates_metadata_and_malformed_lines(tmp_path):
    cfg = _mk_cfg(tmp_path)
    t = cfg.sweep_transcripts_root / TRAP_DIR / "e1-mixed.jsonl"
    t.parent.mkdir(parents=True)
    t.write_text(
        json.dumps({"type": "file-history-snapshot", "messageId": "m0"}) + "\n"
        + "{this is not json\n"
        + json.dumps({"type": "user", "uuid": "u1", "cwd": "/real/here",
                      "message": {"role": "user", "content": "hi"}}) + "\n",
        encoding="utf-8",
    )
    assert sweep.read_cwd(t) == "/real/here"


def test_read_cwd_none_when_no_line_carries_cwd(tmp_path):
    cfg = _mk_cfg(tmp_path)
    t = cfg.sweep_transcripts_root / "d" / "e1-nocwd.jsonl"
    kit.write_transcript(t, [{"type": "file-history-snapshot", "messageId": "m0"}])
    assert sweep.read_cwd(t) is None


# --------------------------------------------------------------------------- #
# run_sweep — enqueue semantics                                                #
# --------------------------------------------------------------------------- #
def test_settled_orphan_enqueues_synthetic_marker(tmp_path):
    cfg = _mk_cfg(tmp_path)
    repo = kit.make_git_repo(tmp_path / "realrepo")
    t = _orphan(cfg, str(repo))

    report = sweep.run_sweep(cfg, {})

    assert report.enqueued == 1 and SID in report.enqueued_sessions
    marker = json.loads((cfg.queue_dir / f"{SID}.json").read_text())
    # Golden marker shape: hook schema + origin (AC-2.1d), cwd from content (AC-2.1c).
    assert marker["session_id"] == SID
    assert marker["transcript_path"] == str(t)
    assert marker["cwd"] == str(repo)
    assert marker["origin"] == "sweep"
    assert marker["reason"] == "sweep"
    assert marker["marked_at"].endswith("Z") and "T" in marker["marked_at"]


def test_fresh_transcript_is_not_swept(tmp_path):
    cfg = _mk_cfg(tmp_path, settle_s=1800)
    repo = kit.make_git_repo(tmp_path / "realrepo")
    _orphan(cfg, str(repo), aged=0)  # mtime = now, well under the threshold
    report = sweep.run_sweep(cfg, {})
    assert report.enqueued == 0
    assert not list(cfg.queue_dir.glob("*.json"))


def test_existing_marker_is_never_overwritten(tmp_path):
    cfg = _mk_cfg(tmp_path)
    repo = kit.make_git_repo(tmp_path / "realrepo")
    _orphan(cfg, str(repo))
    hook_marker = {"session_id": SID, "transcript_path": "/from/hook.jsonl",
                   "cwd": "/hook/cwd", "reason": "prompt_input_exit",
                   "marked_at": "2026-07-30T00:00:00Z"}
    marker_path = cfg.queue_dir / f"{SID}.json"
    marker_path.write_text(json.dumps(hook_marker), encoding="utf-8")

    report = sweep.run_sweep(cfg, {})

    assert report.enqueued == 0
    assert json.loads(marker_path.read_text()) == hook_marker  # byte-level trust


def test_fully_processed_transcript_is_not_reswept(tmp_path):
    cfg = _mk_cfg(tmp_path)
    repo = kit.make_git_repo(tmp_path / "realrepo")
    t = _orphan(cfg, str(repo))
    n_lines = len(t.read_text().splitlines())
    state = {str(t): {"last_line": n_lines, "last_uuid": f"{SID}-a1"}}
    report = sweep.run_sweep(cfg, state)
    assert report.enqueued == 0
    assert not list(cfg.queue_dir.glob("*.json"))


def test_no_cwd_transcript_is_skipped_not_enqueued(tmp_path):
    cfg = _mk_cfg(tmp_path)
    t = cfg.sweep_transcripts_root / "d" / "e1-nocwd2.jsonl"
    kit.write_transcript(t, [{"type": "file-history-snapshot", "messageId": "m0"}])
    kit.age_file(t, 3600)
    report = sweep.run_sweep(cfg, {})
    assert report.enqueued == 0 and report.skipped >= 1
    assert not list(cfg.queue_dir.glob("*.json"))


def test_sweep_is_idempotent_within_a_pass_cycle(tmp_path):
    cfg = _mk_cfg(tmp_path)
    repo = kit.make_git_repo(tmp_path / "realrepo")
    _orphan(cfg, str(repo))
    assert sweep.run_sweep(cfg, {}).enqueued == 1
    assert sweep.run_sweep(cfg, {}).enqueued == 0  # marker already queued


def test_last_run_record_schema(tmp_path):
    cfg = _mk_cfg(tmp_path)
    repo = kit.make_git_repo(tmp_path / "realrepo")
    _orphan(cfg, str(repo))
    sweep.run_sweep(cfg, {})
    rec = json.loads(cfg.sweep_last_run_path.read_text())
    assert set(rec) >= {"finished_at", "scanned", "enqueued", "skipped"}
    assert rec["enqueued"] == 1 and rec["scanned"] >= 1
    assert rec["finished_at"].endswith("Z")


def test_sweep_never_touches_pointer_state(tmp_path):
    cfg = _mk_cfg(tmp_path)
    repo = kit.make_git_repo(tmp_path / "realrepo")
    _orphan(cfg, str(repo))
    state: dict = {}
    sweep.run_sweep(cfg, state)
    assert state == {}  # pointers advance ONLY via the normal pipeline
    assert not cfg.state_path.exists()


# --------------------------------------------------------------------------- #
# run_once integration — lock, chain, no duplicates                            #
# --------------------------------------------------------------------------- #
def _wire_server(httpserver, *, canonical: str = kit.TEST_REMOTE_CANONICAL):
    httpserver.expect_request("/api/projects", method="GET").respond_with_json(
        {"projects": [{"id": 1, "canonical_remote": canonical, "name": "phase2-goal",
                       "created_by": "goal", "created_at": "2026-07-01T00:00:00Z"}]}
    )
    httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201
    )


def test_run_once_sweeps_processes_and_does_not_duplicate(tmp_path, httpserver):
    """AC-2.1a + AC-2.1b: swept orphan reaches the server via the NORMAL pipeline;
    a second pass produces nothing new."""
    cfg = _mk_cfg(tmp_path, server_url=httpserver.url_for(""))
    repo = kit.make_git_repo(tmp_path / "realrepo")
    _orphan(cfg, str(repo))
    stub = kit.make_claude_stub(
        tmp_path / "claude_stub.py",
        stdout="## Decision\nKept bearer tokens over JWT (revocation).",
    )
    cfg.claude_bin = stub
    _wire_server(httpserver)

    report1 = processor.run_once(cfg)
    posted = [r.get_json() for r, _ in httpserver.log
              if r.method == "POST" and r.path == "/api/entries"]
    assert len(posted) == 1, f"expected exactly one POST, outcomes={report1.outcomes}"
    assert posted[0]["session_id"] == SID
    assert posted[0]["project_remote"] == kit.TEST_REMOTE_CANONICAL
    assert not list(cfg.queue_dir.glob("*.json"))  # marker consumed

    report2 = processor.run_once(cfg)
    posted_after = [r for r, _ in httpserver.log
                    if r.method == "POST" and r.path == "/api/entries"]
    assert len(posted_after) == 1, "second pass must not re-post (pointer + no re-sweep)"


def test_run_once_with_lock_held_does_not_sweep(tmp_path):
    """AC-2.1d: the sweep runs under the processor lock — if another processor holds
    it, NOTHING happens (no markers, no last-run record)."""
    cfg = _mk_cfg(tmp_path)
    repo = kit.make_git_repo(tmp_path / "realrepo")
    _orphan(cfg, str(repo))
    cfg.ensure_dirs()
    holder = open(cfg.lock_path, "w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        report = processor.run_once(cfg)
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
    assert report.lock_held is True
    assert not list(cfg.queue_dir.glob("*.json"))
    assert not cfg.sweep_last_run_path.exists()


def test_sweep_disabled_is_a_noop(tmp_path, httpserver):
    cfg = _mk_cfg(tmp_path, enabled=False, server_url=httpserver.url_for(""))
    repo = kit.make_git_repo(tmp_path / "realrepo")
    _orphan(cfg, str(repo))
    _wire_server(httpserver)
    processor.run_once(cfg)
    assert not list(cfg.queue_dir.glob("*.json"))
    assert not cfg.sweep_last_run_path.exists()


def test_sweep_crash_does_not_block_hook_markers(tmp_path, httpserver, monkeypatch):
    """A sweep failure is isolated like a marker failure: hook markers still process."""
    cfg = _mk_cfg(tmp_path, server_url=httpserver.url_for(""))
    repo = kit.make_git_repo(tmp_path / "realrepo")
    hook_t = tmp_path / "hook_session.jsonl"
    kit.write_transcript(hook_t, kit.transcript_lines(str(repo), "hook-1"))
    cfg.ensure_dirs()
    (cfg.queue_dir / "hook-1.json").write_text(json.dumps({
        "session_id": "hook-1", "transcript_path": str(hook_t), "cwd": str(repo),
        "reason": "prompt_input_exit", "marked_at": kit.utc_now_iso()}), encoding="utf-8")
    stub = kit.make_claude_stub(tmp_path / "claude_stub.py", stdout="## Decision\nX.")
    cfg.claude_bin = stub
    _wire_server(httpserver)

    def boom(*a, **k):
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(sweep, "run_sweep", boom)
    report = processor.run_once(cfg)
    posted = [r for r, _ in httpserver.log
              if r.method == "POST" and r.path == "/api/entries"]
    assert len(posted) == 1, f"hook marker must still be processed; outcomes={report.outcomes}"
