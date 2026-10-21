"""Unit tests for the orphan-transcript sweep (DESIGN.md §12 R2.1, epic E1).

Scope: ``sweep.read_cwd`` and ``sweep.run_sweep`` in isolation — real temporary
filesystem, no server, no git, no lock (``run_sweep`` is *always* called by
``processor.run_once`` under the existing flock; the chain is covered in
``client/tests/integration/test_sweep_process.py``).
"""
from __future__ import annotations

import errno
import json
import os
import time
from pathlib import Path

from loom import sweep
from loom.config import Config

SID = "sess-orphan-1"
# Encoded directory names are lossy (``-`` collapses ``/`` and ``.``): this one would
# decode to /Users/alice/Projects/webapp, which is NOT the cwd in the content (AC-2.1c).
ENCODED_DIR = "-Users-alice-Projects-webapp"


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _cfg(tmp_path: Path, **kwargs) -> Config:
    home = tmp_path / "loom_home"
    root = tmp_path / "transcripts"
    root.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        loom_home=home,
        server_url="http://127.0.0.1:9",
        token="t",
        sweep_transcripts_root=root,
        **kwargs,
    )
    cfg.ensure_dirs()
    return cfg


def _lines(cwd: str, session_id: str = SID) -> list[dict]:
    """Realistic transcript lines: metadata first (no cwd), then conversational ones."""
    return [
        {"type": "file-history-snapshot", "messageId": "m0", "snapshot": {}},
        {"type": "user", "uuid": f"{session_id}-u1", "cwd": cwd, "sessionId": session_id,
         "message": {"role": "user", "content": "hola"}},
        {"type": "assistant", "uuid": f"{session_id}-a1", "cwd": cwd, "sessionId": session_id,
         "message": {"role": "assistant", "content": [{"type": "text", "text": "vale"}]}},
    ]


def _write(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return path


def _age(path: Path, seconds_ago: float) -> None:
    ts = time.time() - seconds_ago
    os.utime(path, (ts, ts))


def _orphan(cfg: Config, cwd: str = "/work/repo", *, sid: str = SID,
            aged: float = 3600, subdir: str = ENCODED_DIR) -> Path:
    path = _write(cfg.sweep_transcripts_root / subdir / f"{sid}.jsonl", _lines(cwd, sid))
    _age(path, aged)
    return path


def _markers(cfg: Config) -> list[str]:
    return sorted(p.name for p in cfg.queue_dir.iterdir())


# --------------------------------------------------------------------------- #
# read_cwd                                                                     #
# --------------------------------------------------------------------------- #
def test_read_cwd_returns_first_cwd_in_the_content(tmp_path):
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/home/dev/project")
    assert sweep.read_cwd(t) == "/home/dev/project"


def test_read_cwd_ignores_the_encoded_directory_name(tmp_path):
    """AC-2.1c: the directory name is lossy; only the content is authoritative."""
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/Users/alice/Projects.webapp/src")
    assert sweep.read_cwd(t) == "/Users/alice/Projects.webapp/src"
    assert sweep.read_cwd(t) != "/Users/alice/Projects/webapp"


def test_read_cwd_first_value_wins(tmp_path):
    cfg = _cfg(tmp_path)
    t = _write(cfg.sweep_transcripts_root / "d" / "multi.jsonl", [
        {"type": "user", "uuid": "u1", "cwd": "/first", "message": {"role": "user", "content": "a"}},
        {"type": "user", "uuid": "u2", "cwd": "/second", "message": {"role": "user", "content": "b"}},
    ])
    assert sweep.read_cwd(t) == "/first"


def test_read_cwd_skips_malformed_blank_and_non_object_lines(tmp_path):
    cfg = _cfg(tmp_path)
    t = cfg.sweep_transcripts_root / "d" / "mixed.jsonl"
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_text(
        json.dumps({"type": "file-history-snapshot"}) + "\n"
        + "{not json at all\n"
        + "\n"
        + "[1, 2, 3]\n"
        + json.dumps({"type": "user", "uuid": "u1", "cwd": "/real/here",
                      "message": {"role": "user", "content": "hi"}}) + "\n",
        encoding="utf-8",
    )
    assert sweep.read_cwd(t) == "/real/here"


def test_read_cwd_none_when_no_line_carries_one(tmp_path):
    cfg = _cfg(tmp_path)
    t = _write(cfg.sweep_transcripts_root / "d" / "nocwd.jsonl",
               [{"type": "file-history-snapshot", "messageId": "m0"}])
    assert sweep.read_cwd(t) is None


def test_read_cwd_none_for_empty_missing_or_unusable_cwd(tmp_path):
    cfg = _cfg(tmp_path)
    empty = cfg.sweep_transcripts_root / "d" / "empty.jsonl"
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_text("", encoding="utf-8")
    assert sweep.read_cwd(empty) is None
    assert sweep.read_cwd(cfg.sweep_transcripts_root / "d" / "ghost.jsonl") is None
    # A cwd that is not a usable string is ignored, not returned.
    weird = _write(cfg.sweep_transcripts_root / "d" / "weird.jsonl", [
        {"type": "user", "uuid": "u1", "cwd": None, "message": {}},
        {"type": "user", "uuid": "u2", "cwd": 42, "message": {}},
        {"type": "user", "uuid": "u3", "cwd": "   ", "message": {}},
    ])
    assert sweep.read_cwd(weird) is None


# --------------------------------------------------------------------------- #
# run_sweep — enqueue semantics                                                #
# --------------------------------------------------------------------------- #
def test_settled_orphan_produces_a_synthetic_marker(tmp_path):
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/work/repo")

    report = sweep.run_sweep(cfg, {})

    assert (report.scanned, report.enqueued, report.skipped) == (1, 1, 0)
    assert report.enqueued_sessions == [SID]
    marker = json.loads((cfg.queue_dir / f"{SID}.json").read_text(encoding="utf-8"))
    # Hook-marker schema + origin, and nothing else (AC-2.1d).
    assert set(marker) == {"session_id", "transcript_path", "cwd", "reason",
                           "marked_at", "origin"}
    assert marker["session_id"] == SID
    assert marker["transcript_path"] == str(t)
    assert marker["cwd"] == "/work/repo"
    assert marker["reason"] == "sweep"
    assert marker["origin"] == "sweep"
    assert marker["marked_at"].endswith("Z") and "T" in marker["marked_at"]


def test_transcripts_are_discovered_recursively_and_only_jsonl(tmp_path):
    cfg = _cfg(tmp_path)
    _orphan(cfg, "/work/a", sid="deep-1", subdir="a/b/c")
    noise = cfg.sweep_transcripts_root / "a" / "notes.txt"
    noise.write_text("not a transcript", encoding="utf-8")
    _age(noise, 3600)

    report = sweep.run_sweep(cfg, {})

    assert report.scanned == 1 and report.enqueued_sessions == ["deep-1"]
    assert _markers(cfg) == ["deep-1.json"]


def test_fresh_transcript_is_left_alone(tmp_path):
    cfg = _cfg(tmp_path, sweep_settle_s=1800)
    _orphan(cfg, "/work/repo", aged=0)

    report = sweep.run_sweep(cfg, {})

    assert (report.enqueued, report.skipped) == (0, 1)
    assert _markers(cfg) == []


def test_settle_threshold_is_honoured_per_config(tmp_path):
    cfg = _cfg(tmp_path, sweep_settle_s=60)
    _orphan(cfg, "/work/repo", aged=120)
    assert sweep.run_sweep(cfg, {}).enqueued == 1


def test_existing_marker_is_never_overwritten(tmp_path):
    """A hook marker may carry a fresher cwd/reason: byte-level preservation."""
    cfg = _cfg(tmp_path)
    _orphan(cfg, "/work/repo")
    hook_marker = {"session_id": SID, "transcript_path": "/from/hook.jsonl",
                   "cwd": "/hook/cwd", "reason": "prompt_input_exit",
                   "marked_at": "2026-07-30T00:00:00Z"}
    marker_path = cfg.queue_dir / f"{SID}.json"
    raw = json.dumps(hook_marker)
    marker_path.write_text(raw, encoding="utf-8")

    report = sweep.run_sweep(cfg, {})

    assert (report.enqueued, report.skipped) == (0, 1)
    assert marker_path.read_text(encoding="utf-8") == raw


def test_transcript_without_new_content_is_skipped(tmp_path):
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/work/repo")
    n_lines = len(t.read_text(encoding="utf-8").splitlines())
    state = {str(t): {"last_line": n_lines, "last_uuid": f"{SID}-a1"}}

    report = sweep.run_sweep(cfg, state)

    assert (report.enqueued, report.skipped) == (0, 1)
    assert _markers(cfg) == []


def test_partially_processed_transcript_is_swept(tmp_path):
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/work/repo")
    state = {str(t): {"last_line": 1, "last_uuid": None}}
    assert sweep.run_sweep(cfg, state).enqueued == 1


def test_missing_pointer_entry_means_pointer_zero(tmp_path):
    cfg = _cfg(tmp_path)
    _orphan(cfg, "/work/repo")
    assert sweep.run_sweep(cfg, {"/some/other/transcript.jsonl": {"last_line": 99}}).enqueued == 1


def test_empty_transcript_is_never_enqueued(tmp_path):
    cfg = _cfg(tmp_path)
    empty = cfg.sweep_transcripts_root / "d" / "empty.jsonl"
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_text("", encoding="utf-8")
    _age(empty, 3600)

    report = sweep.run_sweep(cfg, {})

    assert (report.scanned, report.enqueued, report.skipped) == (1, 0, 1)
    assert _markers(cfg) == []


def test_transcript_without_cwd_is_skipped_and_retried_next_pass(tmp_path):
    cfg = _cfg(tmp_path)
    t = _write(cfg.sweep_transcripts_root / "d" / "nocwd.jsonl",
               [{"type": "file-history-snapshot", "messageId": "m0"}])
    _age(t, 3600)

    assert sweep.run_sweep(cfg, {}).skipped == 1
    assert _markers(cfg) == []

    # The cwd shows up later (the session wrote more lines) → next pass enqueues it.
    _write(t, [{"type": "file-history-snapshot", "messageId": "m0"},
               {"type": "user", "uuid": "u1", "cwd": "/late/cwd",
                "message": {"role": "user", "content": "x"}}])
    _age(t, 3600)
    assert sweep.run_sweep(cfg, {}).enqueued == 1


def test_sweep_is_idempotent(tmp_path):
    cfg = _cfg(tmp_path)
    _orphan(cfg, "/work/repo")
    assert sweep.run_sweep(cfg, {}).enqueued == 1
    assert sweep.run_sweep(cfg, {}).enqueued == 0
    assert _markers(cfg) == [f"{SID}.json"]


def test_several_orphans_in_one_pass(tmp_path):
    cfg = _cfg(tmp_path)
    for i in range(3):
        _orphan(cfg, f"/work/repo{i}", sid=f"multi-{i}", subdir=f"dir{i}")

    report = sweep.run_sweep(cfg, {})

    assert (report.scanned, report.enqueued, report.skipped) == (3, 3, 0)
    assert sorted(report.enqueued_sessions) == ["multi-0", "multi-1", "multi-2"]


def test_no_temporary_files_survive_the_pass(tmp_path):
    """The tmp + rename convention must leave nothing behind (the processor's marker
    iterator would otherwise trip over it)."""
    cfg = _cfg(tmp_path)
    _orphan(cfg, "/work/repo")
    _orphan(cfg, "/work/other", sid="second", subdir="d2")

    sweep.run_sweep(cfg, {})

    assert not [p for p in cfg.queue_dir.iterdir() if p.name.endswith(".tmp")]


# --------------------------------------------------------------------------- #
# run_sweep — safety against the UNLOCKED hook writing the same queue          #
# --------------------------------------------------------------------------- #
def test_the_hooks_in_flight_temp_file_is_never_touched(tmp_path):
    """``sessionend.sh`` writes ``<sid>.json.tmp`` without taking the lock, so the
    sweep must not pick that name: sharing it means each writer truncates the other's
    in-flight file and deletes the other's temp."""
    cfg = _cfg(tmp_path)
    _orphan(cfg, "/work/repo")
    _orphan(cfg, "/work/other", sid="other-session", subdir="d2")
    # The hook is mid-write for a THIRD session while the sweep runs.
    hook_tmp = cfg.queue_dir / "hook-session.json.tmp"
    hook_bytes = b'{"session_id": "hook-session", "half-written'
    hook_tmp.write_bytes(hook_bytes)

    sweep.run_sweep(cfg, {})

    assert hook_tmp.exists(), "the sweep deleted the hook's in-flight temp file"
    assert hook_tmp.read_bytes() == hook_bytes, "the sweep truncated the hook's temp file"
    assert sorted(_markers(cfg)) == ["hook-session.json.tmp", "other-session.json",
                                     f"{SID}.json"]


def test_the_hooks_temp_for_the_same_session_is_never_touched(tmp_path):
    """Worst case of the same bug: the hook is writing the temp of the very session
    the sweep is about to enqueue."""
    cfg = _cfg(tmp_path)
    _orphan(cfg, "/work/repo")
    hook_tmp = cfg.queue_dir / f"{SID}.json.tmp"
    hook_bytes = b'{"session_id": "' + SID.encode() + b'", "cwd": "/hook'
    hook_tmp.write_bytes(hook_bytes)

    report = sweep.run_sweep(cfg, {})

    assert report.enqueued == 1
    assert hook_tmp.read_bytes() == hook_bytes


def test_published_marker_is_not_left_linked_to_a_temp_file(tmp_path):
    """The marker is published with ``os.link``; if our temp survived, a hook opening
    ``<sid>.json.tmp`` with ``>`` would truncate the PUBLISHED marker in place."""
    cfg = _cfg(tmp_path)
    _orphan(cfg, "/work/repo")

    sweep.run_sweep(cfg, {})

    assert (cfg.queue_dir / f"{SID}.json").stat().st_nlink == 1


def test_link_failure_skips_without_clobbering_or_raising(tmp_path, monkeypatch):
    """No ``os.replace`` fallback: a link that fails must degrade to "retry next pass",
    never to a check-then-overwrite of a marker the hook may have just written."""
    cfg = _cfg(tmp_path)
    _orphan(cfg, "/work/repo")
    _orphan(cfg, "/work/other", sid="other-session", subdir="d2")

    def no_link(*_a, **_k):
        raise OSError(errno.EPERM, "operation not permitted")

    monkeypatch.setattr(os, "link", no_link)
    report = sweep.run_sweep(cfg, {})

    assert (report.scanned, report.enqueued, report.skipped) == (2, 0, 2)
    assert _markers(cfg) == []                       # nothing published…
    assert cfg.sweep_last_run_path.exists()          # …but the pass still completed
    assert "sweep_enqueue_failed" in cfg.log_file.read_text(encoding="utf-8")


def test_temp_vanishing_before_the_link_does_not_abort_the_pass(tmp_path, monkeypatch):
    """``FileNotFoundError`` is an ``OSError``: the hook's ``rm -f`` on a shared temp
    used to raise straight out of the pass, skipping the rest and the last-run record."""
    cfg = _cfg(tmp_path)
    _orphan(cfg, "/work/repo")

    def vanished(*_a, **_k):
        raise FileNotFoundError(errno.ENOENT, "no such file")

    monkeypatch.setattr(os, "link", vanished)
    report = sweep.run_sweep(cfg, {})

    assert (report.enqueued, report.skipped) == (0, 1)
    assert cfg.sweep_last_run_path.exists()


# --------------------------------------------------------------------------- #
# run_sweep — bounded pass (first sweep after an upgrade is a full backfill)    #
# --------------------------------------------------------------------------- #
def _backlog(cfg: Config, count: int) -> list[str]:
    """``count`` settled orphans, oldest last. Returns the ids newest-first."""
    for i in range(count):
        _orphan(cfg, f"/work/repo{i}", sid=f"cap-{i:02d}", subdir=f"d{i:02d}",
                aged=3600 + i * 60)
    return [f"cap-{i:02d}" for i in range(count)]


def test_pass_stops_scanning_once_the_enqueue_cap_is_reached(tmp_path):
    """A machine upgrading into phase 2 has no pointer for ANY historical transcript,
    so without a cap one pass would enqueue every transcript on disk and summarize
    them all inside the held lock."""
    cap = sweep.MAX_ENQUEUE_PER_PASS
    cfg = _cfg(tmp_path)
    newest_first = _backlog(cfg, cap + 5)

    report = sweep.run_sweep(cfg, {})

    assert report.enqueued == cap
    assert report.scanned == cap, "the pass must stop scanning, not just stop enqueueing"
    assert report.skipped == 0
    # Newest first: the sessions a human might still act on come back first.
    assert report.enqueued_sessions == newest_first[:cap]
    log = cfg.log_file.read_text(encoding="utf-8")
    assert f"capped={cap}" in log and "not_scanned=5" in log


def test_successive_passes_drain_the_backlog(tmp_path):
    cap = sweep.MAX_ENQUEUE_PER_PASS
    cfg = _cfg(tmp_path)
    newest_first = _backlog(cfg, cap * 2 + 3)

    drained: list[str] = []
    for _ in range(3):
        drained += sweep.run_sweep(cfg, {}).enqueued_sessions

    assert drained == newest_first          # in order, no repeats, none lost
    assert len(_markers(cfg)) == cap * 2 + 3
    assert sweep.run_sweep(cfg, {}).enqueued == 0


def test_an_uncapped_pass_does_not_advertise_a_backlog(tmp_path):
    cfg = _cfg(tmp_path)
    _backlog(cfg, sweep.MAX_ENQUEUE_PER_PASS - 1)

    sweep.run_sweep(cfg, {})

    assert "capped=" not in cfg.log_file.read_text(encoding="utf-8")


def test_file_names_that_are_not_safe_session_ids_are_skipped(tmp_path):
    """The stem becomes a file name in queue/: it is validated, never trusted."""
    cfg = _cfg(tmp_path)
    directory = cfg.sweep_transcripts_root / "d"
    directory.mkdir(parents=True, exist_ok=True)
    # "..": traversal-shaped stem. ".hidden": dot-file. The others carry separators a
    # naive implementation would splice into a log line or a path.
    for name in ("...jsonl", ".hidden.jsonl", "with space.jsonl", "tab\tsep.jsonl",
                 "nl\nsep.jsonl"):
        path = directory / name
        try:
            _write(path, _lines("/work/repo"))
        except OSError:  # names the host filesystem refuses outright
            continue
        _age(path, 3600)

    report = sweep.run_sweep(cfg, {})

    assert report.scanned == report.skipped and report.enqueued == 0
    assert _markers(cfg) == []


# --------------------------------------------------------------------------- #
# run_sweep — the stat skip (P2.F4 / p3-state-stat-fields)                     #
#                                                                              #
# The skip is a silent-corruption risk: whenever the recorded pair does not     #
# describe EXACTLY the file on disk, the pass must fall back to today's full    #
# check. ``_line_count`` is booby-trapped in most of these so "did it read?" is #
# an assertion, not an inference.                                              #
# --------------------------------------------------------------------------- #
def _forbid_line_count(monkeypatch) -> None:
    """Makes any transcript read a hard failure (the skip must precede it)."""
    def boom(path):
        raise AssertionError(f"_line_count must not be called for {path}")

    monkeypatch.setattr(sweep, "_line_count", boom)


def _stat_entry(path: Path, *, last_line: int = 3, **override) -> dict:
    """A pointer entry carrying the stat pair a pre-read capture would have left."""
    st = path.stat()
    entry = {
        "last_line": last_line,
        "last_uuid": f"{SID}-a1",
        "updated_at": "2026-08-10T00:00:00Z",
        "stat_mtime_ns": st.st_mtime_ns,
        "stat_size": st.st_size,
    }
    entry.update(override)
    return entry


def test_matching_stat_pair_skips_the_transcript_without_reading_it(tmp_path, monkeypatch):
    """AC-F4.1: current ``(st_mtime_ns, st_size)`` == the recorded pair → no read."""
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/work/repo")
    state = {str(t): _stat_entry(t)}
    _forbid_line_count(monkeypatch)

    report = sweep.run_sweep(cfg, state)

    assert (report.scanned, report.enqueued, report.skipped) == (1, 0, 1)
    assert _markers(cfg) == []


def test_stat_skipped_transcript_counts_into_the_last_run_record(tmp_path, monkeypatch):
    """p2-sweep-last-run-record: stat-skips are ``skipped``, not a new key."""
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/work/repo")
    _forbid_line_count(monkeypatch)

    sweep.run_sweep(cfg, {str(t): _stat_entry(t)})

    rec = json.loads(cfg.sweep_last_run_path.read_text(encoding="utf-8"))
    # AMENDED at wave g (2026-08-11, seam p4-sweep-last-run-additive): additive keys are
    # permitted; the frozen four and their values stay pinned.
    assert set(rec) >= {"finished_at", "scanned", "enqueued", "skipped"}
    assert (rec["scanned"], rec["enqueued"], rec["skipped"]) == (1, 0, 1)


def test_size_change_alone_defeats_the_skip(tmp_path):
    """Content appended and the mtime forced back to the recorded value: only the
    size differs, and that alone must force the full check."""
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/work/repo")
    entry = _stat_entry(t)
    with t.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "uuid": f"{SID}-a2", "cwd": "/work/repo",
                             "message": {"role": "assistant",
                                         "content": [{"type": "text", "text": "más"}]}}) + "\n")
    os.utime(t, ns=(entry["stat_mtime_ns"], entry["stat_mtime_ns"]))
    assert t.stat().st_mtime_ns == entry["stat_mtime_ns"]

    report = sweep.run_sweep(cfg, {str(t): entry})

    assert report.enqueued == 1
    assert _markers(cfg) == [f"{SID}.json"]


def test_mtime_change_alone_defeats_the_skip(tmp_path):
    """A rewrite that keeps the byte count (same size, new mtime) must not skip:
    ``read_delta`` reads by line, so identical size is no proof of identical bytes."""
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/work/repo")
    entry = _stat_entry(t, last_line=1)
    _age(t, 3599)  # settled, but a different mtime than the one recorded
    assert t.stat().st_size == entry["stat_size"]
    assert t.stat().st_mtime_ns != entry["stat_mtime_ns"]

    assert sweep.run_sweep(cfg, {str(t): entry}).enqueued == 1


def test_sub_second_mtime_difference_defeats_the_skip(tmp_path):
    """The comparison is the INTEGER ``st_mtime_ns``: a difference inside the same
    second (invisible to a seconds- or float-granularity comparison) must still
    force the full check."""
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/work/repo")
    entry = _stat_entry(t, last_line=1)
    actual = t.stat().st_mtime_ns
    recorded = actual - 1
    if recorded // 10**9 != actual // 10**9:  # coarse clock: stay in the same second
        recorded = actual + 1
    entry["stat_mtime_ns"] = recorded

    assert sweep.run_sweep(cfg, {str(t): entry}).enqueued == 1


def test_absent_or_partial_stat_fields_never_skip(tmp_path):
    """AC-F4.4: a pre-upgrade entry (and any half-written one) takes the full check."""
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/work/repo")
    full = _stat_entry(t, last_line=1)
    for dropped in ("stat_mtime_ns", "stat_size", "both"):
        entry = dict(full)
        for key in (("stat_mtime_ns", "stat_size") if dropped == "both" else (dropped,)):
            entry.pop(key)
        for marker in cfg.queue_dir.glob("*.json"):
            marker.unlink()

        report = sweep.run_sweep(cfg, {str(t): entry})

        assert report.enqueued == 1, f"dropping {dropped} must defeat the skip"


def test_non_integer_stat_fields_never_skip(tmp_path):
    """A float/str/bool pair is not the frozen integer contract: never trust it."""
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/work/repo")
    full = _stat_entry(t, last_line=1)
    corruptions = (
        {"stat_mtime_ns": float(full["stat_mtime_ns"])},
        {"stat_size": float(full["stat_size"])},
        {"stat_mtime_ns": str(full["stat_mtime_ns"])},
        {"stat_size": None},
        {"stat_mtime_ns": True, "stat_size": True},
    )
    for corruption in corruptions:
        entry = dict(full)
        entry.update(corruption)
        for marker in cfg.queue_dir.glob("*.json"):
            marker.unlink()

        report = sweep.run_sweep(cfg, {str(t): entry})

        assert report.enqueued == 1, f"{corruption} must defeat the skip"


def test_stat_pair_of_another_transcript_never_skips_this_one(tmp_path):
    """The pair is keyed by path: a matching pair under a different key is not ours."""
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/work/repo")
    state = {"/some/other/transcript.jsonl": _stat_entry(t, last_line=99)}

    assert sweep.run_sweep(cfg, state).enqueued == 1


def test_queued_marker_wins_over_a_mismatching_stat_pair(tmp_path, monkeypatch):
    """AC-F4.6: the skip must not be hoisted above the queued-marker guard — a stale
    pair plus a queued marker is still 'nothing to do', and costs no read."""
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/work/repo")
    entry = _stat_entry(t, last_line=1)
    entry["stat_size"] += 7
    marker_path = cfg.queue_dir / f"{SID}.json"
    marker_path.write_text(json.dumps({"session_id": SID, "cwd": "/hook"}), encoding="utf-8")
    raw = marker_path.read_bytes()
    _forbid_line_count(monkeypatch)

    report = sweep.run_sweep(cfg, {str(t): entry})

    assert (report.enqueued, report.skipped) == (0, 1)
    assert marker_path.read_bytes() == raw


def test_a_parked_session_stays_parked_when_its_transcript_has_grown(tmp_path, monkeypatch):
    """AC-F2.2 × AC-F4.6 — the interaction that actually matters. A ``failed/``
    tombstone plus a MISMATCHING pair is the live case: the session was parked, the
    user kept working, so the transcript grew and the recorded pair no longer matches.
    Every guard below the tombstone now says "there is new content", so the tombstone
    is the only thing standing between here and the enqueue → fail → park → enqueue
    loop AC-F2.2 closed. It must still win, and still cost no transcript read."""
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/work/repo")
    entry = _stat_entry(t, last_line=1)          # pointer well behind EOF
    with t.open("a", encoding="utf-8") as fh:    # …and the file has since grown
        fh.write(json.dumps({"type": "assistant", "uuid": f"{SID}-a2", "cwd": "/work/repo",
                             "message": {"role": "assistant",
                                         "content": [{"type": "text", "text": "más"}]}}) + "\n")
    _age(t, 3600)
    assert sweep._stat_unchanged({str(t): entry}, str(t), t) is False, (
        "setup: the pair must mismatch, or this test proves nothing")
    (cfg.failed_dir / f"{SID}.json").write_text(json.dumps(
        {"session_id": SID, "attempts": 3, "adapter_error": "line 3 has no 'uuid'"}),
        encoding="utf-8")
    _forbid_line_count(monkeypatch)

    report = sweep.run_sweep(cfg, {str(t): entry})

    assert (report.scanned, report.enqueued, report.skipped) == (1, 0, 1)
    assert _markers(cfg) == []
    assert (cfg.failed_dir / f"{SID}.json").exists(), "the sweep never clears a park"


def test_unsettled_transcript_is_not_skipped_by_a_matching_pair(tmp_path, monkeypatch):
    """AC-F4.6: the settle guard still runs first — a live session is neither read
    nor enqueued, whatever its recorded pair says."""
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/work/repo", aged=0)
    _forbid_line_count(monkeypatch)

    report = sweep.run_sweep(cfg, {str(t): _stat_entry(t, last_line=1)})

    assert (report.enqueued, report.skipped) == (0, 1)
    assert _markers(cfg) == []


def test_an_unstattable_transcript_falls_back_to_the_full_check(tmp_path):
    """If the stat itself fails, the conservative direction is 'do not skip'."""
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/work/repo")
    entry = _stat_entry(t)
    gone = t.parent / "vanished.jsonl"

    assert sweep._stat_unchanged({str(gone): entry}, str(gone), gone) is False


# --------------------------------------------------------------------------- #
# run_sweep — the stat skip, dev/ino half (P2.F4 amendment 2026-08-10)         #
#                                                                              #
# The comparison is per-entry: BOTH identity fields → all four must match;     #
# NEITHER → the legacy two-field comparison, unchanged; anything in between →  #
# the full check, like every other ambiguity here.                             #
# --------------------------------------------------------------------------- #
def _identity_entry(path: Path, *, last_line: int = 3, **override) -> dict:
    """A pointer entry carrying the full four-field record the amendment writes."""
    st = path.stat()
    entry = _stat_entry(path, last_line=last_line)
    entry["stat_dev"], entry["stat_ino"] = st.st_dev, st.st_ino
    entry.update(override)
    return entry


def test_matching_four_field_entry_skips_without_reading(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/work/repo")
    _forbid_line_count(monkeypatch)

    report = sweep.run_sweep(cfg, {str(t): _identity_entry(t)})

    assert (report.scanned, report.enqueued, report.skipped) == (1, 0, 1)
    assert _markers(cfg) == []


def test_a_replaced_inode_defeats_the_skip_even_with_identical_mtime_and_size(tmp_path):
    """The class the amendment exists for: a REAL ``os.replace`` of a same-size file
    whose mtime is then restored. ``(st_mtime_ns, st_size)`` alone calls this unchanged
    and skips a different file forever."""
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/work/repo")
    entry = _identity_entry(t, last_line=1)

    incoming = t.with_name("incoming.tmp")
    incoming.write_bytes(t.read_bytes())        # byte-identical → identical size
    os.replace(incoming, t)
    os.utime(t, ns=(entry["stat_mtime_ns"], entry["stat_mtime_ns"]))
    st = t.stat()
    assert (st.st_size, st.st_mtime_ns) == (entry["stat_size"], entry["stat_mtime_ns"])
    assert (st.st_dev, st.st_ino) != (entry["stat_dev"], entry["stat_ino"]), (
        "setup: os.replace must move the inode, or this test proves nothing")

    assert sweep._stat_unchanged({str(t): entry}, str(t), t) is False
    assert sweep.run_sweep(cfg, {str(t): entry}).enqueued == 1


def test_a_wrong_dev_or_ino_alone_defeats_the_skip(tmp_path):
    """Either half of the identity mismatching is enough: the file the pointer covered
    is not the file on disk."""
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/work/repo")
    full = _identity_entry(t, last_line=1)
    for field in ("stat_dev", "stat_ino"):
        entry = dict(full)
        entry[field] += 1
        for marker in cfg.queue_dir.glob("*.json"):
            marker.unlink()

        assert sweep.run_sweep(cfg, {str(t): entry}).enqueued == 1, (
            f"a wrong {field} must force the full check")


def test_a_half_present_or_non_integer_identity_never_skips(tmp_path):
    """An entry that mentions ONE identity field, or carries a non-``int`` one, is
    neither a legacy entry nor a valid four-field one. It must NOT silently degrade to
    the legacy comparison — the ambiguity resolves to re-reading."""
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/work/repo")
    full = _identity_entry(t, last_line=1)
    variants = (
        {k: v for k, v in full.items() if k != "stat_ino"},          # dev only
        {k: v for k, v in full.items() if k != "stat_dev"},          # ino only
        {**full, "stat_ino": float(full["stat_ino"])},
        {**full, "stat_dev": str(full["stat_dev"])},
        {**full, "stat_dev": True, "stat_ino": True},
        {**full, "stat_ino": None},
    )
    for entry in variants:
        for marker in cfg.queue_dir.glob("*.json"):
            marker.unlink()

        assert sweep.run_sweep(cfg, {str(t): entry}).enqueued == 1, (
            f"{sorted(entry)} must defeat the skip")


def test_a_legacy_two_field_entry_still_skips(tmp_path, monkeypatch):
    """AC-F4.4: entries written before the amendment carry neither identity field and
    keep EXACTLY the pre-amendment behaviour — a zero-read skip. Making them re-read
    would mean a full pass over every transcript on the machine after an upgrade, for
    no correctness gain (same inode), and would break the frozen goal fixtures that
    hand-craft the legacy shape on purpose."""
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/work/repo")
    entry = _stat_entry(t)
    assert "stat_dev" not in entry and "stat_ino" not in entry
    _forbid_line_count(monkeypatch)

    report = sweep.run_sweep(cfg, {str(t): entry})

    assert (report.enqueued, report.skipped) == (0, 1)


# --------------------------------------------------------------------------- #
# run_sweep — side effects (state, last-run record, logging)                   #
# --------------------------------------------------------------------------- #
def test_sweep_never_touches_the_pointer_state(tmp_path):
    cfg = _cfg(tmp_path)
    _orphan(cfg, "/work/repo")
    state: dict = {"pre": {"last_line": 1}}

    sweep.run_sweep(cfg, state)

    assert state == {"pre": {"last_line": 1}}   # not mutated
    assert not cfg.state_path.exists()          # not persisted either


def test_last_run_record_is_written_every_pass(tmp_path):
    cfg = _cfg(tmp_path)
    _orphan(cfg, "/work/repo")

    sweep.run_sweep(cfg, {})
    rec = json.loads(cfg.sweep_last_run_path.read_text(encoding="utf-8"))
    assert set(rec) >= {"finished_at", "scanned", "enqueued", "skipped"}
    assert (rec["scanned"], rec["enqueued"], rec["skipped"]) == (1, 1, 0)
    assert rec["finished_at"].endswith("Z")

    # A pass with nothing to do still records itself (E2's doctor reads its age).
    sweep.run_sweep(cfg, {})
    rec2 = json.loads(cfg.sweep_last_run_path.read_text(encoding="utf-8"))
    assert (rec2["scanned"], rec2["enqueued"], rec2["skipped"]) == (1, 0, 1)


def test_missing_transcripts_root_is_a_clean_noop(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.sweep_transcripts_root = tmp_path / "does" / "not" / "exist"

    report = sweep.run_sweep(cfg, {})

    assert (report.scanned, report.enqueued, report.skipped) == (0, 0, 0)
    assert cfg.sweep_last_run_path.exists()


def test_sweep_logs_the_pass_but_never_transcript_content(tmp_path):
    """I5/I6: the log carries session ids, paths and counters — never content."""
    cfg = _cfg(tmp_path)
    _write(cfg.sweep_transcripts_root / ENCODED_DIR / f"{SID}.jsonl", [
        {"type": "file-history-snapshot", "messageId": "m0"},
        {"type": "user", "uuid": "u1", "cwd": "/work/repo",
         "message": {"role": "user", "content": "SUPER-SECRET-PAYLOAD"}},
    ])
    _age(cfg.sweep_transcripts_root / ENCODED_DIR / f"{SID}.jsonl", 3600)

    sweep.run_sweep(cfg, {})

    log = cfg.log_file.read_text(encoding="utf-8")
    assert SID in log
    assert "scanned=1" in log and "enqueued=1" in log
    assert "SUPER-SECRET-PAYLOAD" not in log


def test_the_pass_log_counts_stat_skips_apart_from_other_skips(tmp_path):
    """P2.F4 amendment: ``skipped`` cannot distinguish a one-syscall stat-skip from a
    full-file-read-then-skip, so a stat pair that silently never matches looks exactly
    like a working one. The ``sweep_pass`` event carries the count separately — and only
    the count, off the frozen SweepReport / sweep_last_run.json seam."""
    cfg = _cfg(tmp_path)
    hit = _orphan(cfg, "/work/repo")
    miss = _orphan(cfg, "/work/repo", sid="sess-nostat")
    state = {
        str(hit): _identity_entry(hit),                        # skipped by the stat pair
        str(miss): {"last_line": 3, "last_uuid": None,         # skipped after a full read
                    "updated_at": "2026-08-10T00:00:00Z"},
    }

    report = sweep.run_sweep(cfg, state)

    assert (report.scanned, report.enqueued, report.skipped) == (2, 0, 2)
    log = cfg.log_file.read_text(encoding="utf-8")
    assert "skipped=2 stat_skips=1" in log, (
        f"one of the two skips avoided a read; log={log!r}")
    rec = json.loads(cfg.sweep_last_run_path.read_text(encoding="utf-8"))
    # AMENDED at wave g (2026-08-11, seam p4-sweep-last-run-additive), mirroring the
    # planner-side amendment of the same two pins in the frozen E6 goal: the
    # EXACTLY-four and the no-new-SweepReport-field assertions encoded phase 3's
    # deferral of the counter split, not a safety property. The load-bearing assertion
    # is the one above — `stat_skips=1`, NOT a mirror of `skipped` — and it is untouched.
    assert set(rec) >= {"finished_at", "scanned", "enqueued", "skipped"}, (
        "AC-F4.5: the frozen four survive in the last-run record")


def test_a_pass_with_no_stat_skips_still_reports_the_column(tmp_path):
    """Zero is the diagnostic that matters most on the pilot host: it says the skip is
    not firing. An absent column would be indistinguishable from an old client."""
    cfg = _cfg(tmp_path)
    _orphan(cfg, "/work/repo")

    sweep.run_sweep(cfg, {})

    assert "stat_skips=0" in cfg.log_file.read_text(encoding="utf-8")


def test_queue_dir_is_created_when_absent(tmp_path):
    cfg = _cfg(tmp_path)
    for path in sorted(cfg.queue_dir.iterdir()):
        path.unlink()
    cfg.queue_dir.rmdir()
    _orphan(cfg, "/work/repo")

    assert sweep.run_sweep(cfg, {}).enqueued == 1
    assert (cfg.queue_dir / f"{SID}.json").exists()
