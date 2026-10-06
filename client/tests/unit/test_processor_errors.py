"""Unit tests for the phase-2 error discipline (P2.F2 / P2.F3) and the pointer
units shared by the adapter and the sweep (P2.F1 rule 5).

Scope: the processor helpers in isolation (marker identity, adapter-failure
bounding) plus the ``sweep._line_count`` / ``read_delta.new_last_line`` agreement.
The end-to-end pipeline lives in ``client/tests/integration/test_torn_recovery.py``.
"""
from __future__ import annotations

import json
import os
import stat as stat_module
from pathlib import Path

import pytest

from loom import processor, sweep
from loom.config import Config
from loom.transcript_adapter import AdapterError, read_delta

SID = "sess-err-1"
TORN_MARK = "UNITTORNMARK-9c1f"


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _cfg(tmp_path: Path, **kwargs) -> Config:
    cfg = Config(
        loom_home=tmp_path / "loom_home",
        server_url="http://127.0.0.1:9",
        token="t",
        sweep_transcripts_root=tmp_path / "transcripts",
        **kwargs,
    )
    cfg.sweep_transcripts_root.mkdir(parents=True, exist_ok=True)
    cfg.ensure_dirs()
    return cfg


def _marker(cfg: Config, session_id: str = SID, **extra) -> Path:
    path = cfg.queue_dir / f"{session_id}.json"
    path.write_text(json.dumps({
        "session_id": session_id,
        "transcript_path": str(cfg.loom_home / "t.jsonl"),
        "cwd": str(cfg.loom_home),
        "reason": "prompt_input_exit",
        "marked_at": "2026-08-05T00:00:00Z",
        **extra,
    }), encoding="utf-8")
    return path


def _write(path: Path, blob: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return path


def _jline(obj: dict) -> bytes:
    return json.dumps(obj).encode() + b"\n"


def _meta() -> bytes:
    return _jline({"type": "file-history-snapshot", "messageId": "m0"})


def _user(uuid: str, cwd: str = "/tmp/p") -> bytes:
    return _jline({"type": "user", "uuid": uuid, "cwd": cwd,
                   "message": {"role": "user", "content": "hola"}})


def _asst(uuid: str, cwd: str = "/tmp/p") -> bytes:
    return _jline({"type": "assistant", "uuid": uuid, "cwd": cwd,
                   "message": {"role": "assistant",
                               "content": [{"type": "text", "text": "vale"}]}})


def _torn(uuid: str = "torn") -> bytes:
    return (b'{"type":"assistant","uuid":"' + uuid.encode()
            + b'","message":{"role":"assistant","content":[{"type":"text","text":"'
            + TORN_MARK.encode())


# --------------------------------------------------------------------------- #
# P2.F3 — marker identity                                                      #
# --------------------------------------------------------------------------- #
def test_drop_marker_without_identity_keeps_todays_blind_unlink(tmp_path):
    cfg = _cfg(tmp_path)
    path = _marker(cfg)
    processor._drop_marker(path)
    assert not path.exists()


def test_drop_marker_removes_the_marker_it_read(tmp_path):
    cfg = _cfg(tmp_path)
    path = _marker(cfg)
    identity = processor._identify_marker(path)
    processor._drop_marker(path, identity)
    assert not path.exists()


def test_drop_marker_keeps_a_marker_replaced_underneath(tmp_path):
    """The hook writes the queue WITHOUT the lock: a fresh marker may land between
    _read_marker and _drop_marker. Deleting it silently loses a real session."""
    cfg = _cfg(tmp_path)
    path = _marker(cfg)
    identity = processor._identify_marker(path)

    fresh = tmp_path / "fresh.json"
    fresh.write_text(json.dumps({"session_id": SID, "hook_generation": 2}),
                     encoding="utf-8")
    os.replace(fresh, path)                       # exactly what sessionend.sh does

    processor._drop_marker(path, identity)
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["hook_generation"] == 2


def test_drop_marker_detects_a_same_size_rewrite_by_content(tmp_path):
    """Inode/mtime may look unchanged on a coarse clock: content still decides."""
    cfg = _cfg(tmp_path)
    path = _marker(cfg)
    identity = processor._identify_marker(path)
    raw = path.read_bytes()
    path.write_bytes(raw[:-2] + b"9}")            # same length, different bytes
    os.utime(path, ns=(identity[2], identity[2]))  # and the same mtime

    processor._drop_marker(path, identity)
    assert path.exists()


def test_drop_marker_degrades_to_unlink_when_the_check_errors(tmp_path, monkeypatch):
    """AC-F3.3: the identity check must add no new failure mode."""
    cfg = _cfg(tmp_path)
    path = _marker(cfg)
    identity = processor._identify_marker(path)

    def boom(_path):
        raise RuntimeError("stat exploded")

    monkeypatch.setattr(processor, "_identify_marker", boom)
    processor._drop_marker(path, identity)        # must not raise
    assert not path.exists()


def _replace_after_the_first_read(monkeypatch, path: Path, replacement: Path) -> None:
    """Swaps ``path`` for ``replacement`` right after the pass FIRST reads that file.

    This is exactly where ``sessionend.sh``'s unlocked ``os.replace`` can land, and it
    is the only placement that separates "read once" from "read twice": a pass that
    derives the content and the identity from ONE read is unaffected by a swap that
    happens after it, while a pass that reads the file a second time picks up the
    fresh marker's identity and staples it to the old marker's content.

    Every byte-producing entry point is wrapped, so the probe does not encode which
    one the implementation happens to use.
    """
    fired = {"done": False}

    def fire() -> None:
        if not fired["done"]:
            fired["done"] = True
            os.replace(replacement, path)

    for name in ("read_text", "read_bytes", "open"):
        real = getattr(Path, name)

        def wrapper(self, *args, _real=real, **kwargs):
            out = _real(self, *args, **kwargs)
            if self == path:
                fire()
            return out

        monkeypatch.setattr(Path, name, wrapper)


def test_marker_content_and_identity_come_from_a_single_read(tmp_path, monkeypatch):
    """The content read and the identity capture must observe the SAME file.

    Reading twice — ``_read_marker`` then ``_identify_marker`` — is itself the race it
    was meant to close: a hook replace between the two hands the pass an OLD marker
    under the NEW file's identity, so the drop at the end of the pass matches, and
    deletes a fresh capture that was never processed. One read, one identity.
    """
    cfg = _cfg(tmp_path)
    path = _marker(cfg)                       # transcript_path points at a missing file
    fresh = tmp_path / "fresh.json"
    fresh.write_text(json.dumps({"session_id": SID, "hook_generation": 2}),
                     encoding="utf-8")
    _replace_after_the_first_read(monkeypatch, path, fresh)

    report = processor.RunReport()
    processor._process_marker(cfg, None, None, {}, report, path)

    assert [o.action for o in report.outcomes] == ["dropped_no_transcript"]
    assert path.exists(), "the hook's fresh marker was deleted without ever being read"
    assert json.loads(path.read_text(encoding="utf-8"))["hook_generation"] == 2


def test_identify_marker_of_a_vanished_marker_is_none(tmp_path):
    cfg = _cfg(tmp_path)
    assert processor._identify_marker(cfg.queue_dir / "nope.json") is None


def test_drop_marker_of_a_vanished_marker_never_raises(tmp_path):
    cfg = _cfg(tmp_path)
    path = _marker(cfg)
    identity = processor._identify_marker(path)
    path.unlink()
    processor._drop_marker(path, identity)        # AC-F3.3: no crash


# --------------------------------------------------------------------------- #
# P2.F2 — adapter-failure bounding                                             #
# --------------------------------------------------------------------------- #
def test_adapter_failure_increments_attempts_and_persists_the_diagnostic(tmp_path):
    cfg = _cfg(tmp_path)
    path = _marker(cfg)
    report = processor.RunReport()

    for expected in (1, 2):
        marker = processor._read_marker(path)
        processor._handle_adapter_failure(
            cfg, report, marker, path, SID, AdapterError("line 3 has no 'uuid'")
        )
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["attempts"] == expected
        assert on_disk["adapter_error"] == "line 3 has no 'uuid'"

    assert [o.action for o in report.outcomes] == ["kept_adapter_error"] * 2


def test_adapter_failure_parks_at_max_attempts_with_the_diagnostic(tmp_path):
    cfg = _cfg(tmp_path)
    path = _marker(cfg, attempts=processor.MAX_ATTEMPTS - 1)
    report = processor.RunReport()

    processor._handle_adapter_failure(
        cfg, report, processor._read_marker(path), path, SID, AdapterError("boom")
    )

    assert not path.exists()
    parked = json.loads((cfg.failed_dir / f"{SID}.json").read_text(encoding="utf-8"))
    assert parked["attempts"] == processor.MAX_ATTEMPTS
    assert parked["adapter_error"] == "boom"
    assert [o.action for o in report.outcomes] == ["failed_parked"]


def test_unicode_decode_error_is_classified_without_echoing_bytes(tmp_path):
    cfg = _cfg(tmp_path)
    path = _marker(cfg)
    report = processor.RunReport()
    exc = UnicodeDecodeError("utf-8", TORN_MARK.encode() + b"\xff", 16, 17, "invalid")

    processor._handle_adapter_failure(cfg, report, processor._read_marker(path),
                                      path, SID, exc)

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["adapter_error"]
    assert TORN_MARK not in on_disk["adapter_error"]
    assert TORN_MARK not in cfg.log_file.read_text(encoding="utf-8")


def test_recursion_error_is_classified_generically(tmp_path):
    """Same disclosure policy as the other bounded failures: the class, never content.

    A RecursionError's own message is generic today; a diagnostic that gets persisted
    on a marker and written to a log is not the place to discover it stopped being.
    """
    cfg = _cfg(tmp_path)
    path = _marker(cfg)
    report = processor.RunReport()

    processor._handle_adapter_failure(
        cfg, report, processor._read_marker(path), path, SID,
        RecursionError(f"maximum recursion depth exceeded near {TORN_MARK}"),
    )

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["attempts"] == 1
    assert on_disk["adapter_error"] == "transcript line nests too deeply to parse"
    assert TORN_MARK not in cfg.log_file.read_text(encoding="utf-8")


def test_adapter_failure_detail_is_a_single_log_safe_line(tmp_path):
    """A diagnostic read off disk must not be able to forge log columns."""
    cfg = _cfg(tmp_path)
    path = _marker(cfg)
    report = processor.RunReport()

    processor._handle_adapter_failure(
        cfg, report, processor._read_marker(path), path, SID,
        AdapterError("line 3\tfake\taction\nsecond line"),
    )

    logged = cfg.log_file.read_text(encoding="utf-8").splitlines()
    assert len(logged) == 1
    assert "\n" not in json.loads(path.read_text(encoding="utf-8"))["adapter_error"]


def test_bound_attempts_degrades_to_writing_when_the_check_errors(tmp_path, monkeypatch):
    """AC-F3.3, mirrored onto the rewrite path: the guard adds no failure mode.

    An identity the pass cannot evaluate must fall back to the historical
    unconditional write, exactly as ``_drop_marker`` falls back to the unconditional
    unlink — otherwise a stat that blows up would silently stop bounding attempts.
    """
    cfg = _cfg(tmp_path)
    path = _marker(cfg)
    identity = processor._identify_marker(path)
    report = processor.RunReport()

    def boom(_path):
        raise RuntimeError("stat exploded")

    monkeypatch.setattr(processor, "_identify_marker", boom)
    processor._bound_attempts(cfg, report, processor._read_marker(path), path, SID,
                              "kept_adapter_error", "d", identity)

    assert json.loads(path.read_text(encoding="utf-8"))["attempts"] == 1


def test_bound_attempts_without_an_identity_keeps_todays_blind_write(tmp_path):
    cfg = _cfg(tmp_path)
    path = _marker(cfg)
    report = processor.RunReport()

    processor._bound_attempts(cfg, report, processor._read_marker(path), path, SID,
                              "kept_adapter_error", "d")

    assert json.loads(path.read_text(encoding="utf-8"))["attempts"] == 1


def test_unreadable_transcript_is_an_adapter_failure_not_a_crash(tmp_path):
    """P2.F2: 'unreadable transcript' joins the adapter-failure class."""
    cfg = _cfg(tmp_path)
    path = _marker(cfg)
    report = processor.RunReport()

    processor._handle_adapter_failure(
        cfg, report, processor._read_marker(path), path, SID,
        OSError(13, "Permission denied"),
    )

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["attempts"] == 1
    assert "13" in on_disk["adapter_error"]


# --------------------------------------------------------------------------- #
# P2.F2 — the sweep's failed/ guard (AC-F2.2)                                   #
# --------------------------------------------------------------------------- #
def test_sweep_skips_a_session_parked_in_failed(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, sweep_settle_s=0)
    parked = _write(cfg.sweep_transcripts_root / f"{SID}.jsonl",
                    _meta() + _user("u1") + _asst("a1"))
    (cfg.failed_dir / f"{SID}.json").write_text(json.dumps({
        "session_id": SID, "attempts": processor.MAX_ATTEMPTS,
        "adapter_error": "line 3 has no 'uuid'",
    }), encoding="utf-8")

    opened: list[str] = []
    real_open = Path.open

    def counting_open(self, *args, **kwargs):
        opened.append(str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)
    report = sweep.run_sweep(cfg, {})
    monkeypatch.undo()

    assert report.enqueued == 0
    assert not (cfg.queue_dir / f"{SID}.json").exists()
    assert str(parked) not in opened, "the guard must sit BEFORE _line_count"


def test_sweep_considers_a_session_again_once_its_tombstone_is_cleared(tmp_path):
    """The other half of AC-F2.2: the guard is a state the pipeline can leave.

    Nothing but a human used to clear ``failed/``, so the guard that stops the
    enqueue → fail → park → enqueue loop also stopped a HEALED session from ever
    being swept again. Clearing the tombstone hands the session back to the sweep.
    """
    cfg = _cfg(tmp_path, sweep_settle_s=0)
    _write(cfg.sweep_transcripts_root / f"{SID}.jsonl",
           _meta() + _user("u1") + _asst("a1"))
    (cfg.failed_dir / f"{SID}.json").write_text("{}", encoding="utf-8")
    assert sweep.run_sweep(cfg, {}).enqueued == 0        # the guard, doing its job

    processor._clear_failed_tombstone(cfg, SID)

    assert sweep.run_sweep(cfg, {}).enqueued_sessions == [SID]


def test_clearing_an_absent_tombstone_is_a_silent_no_op(tmp_path):
    """Cleanup on the success path must never be able to fail a pass."""
    cfg = _cfg(tmp_path)
    processor._clear_failed_tombstone(cfg, "never-parked")


def test_sweep_failed_guard_is_per_session(tmp_path):
    cfg = _cfg(tmp_path, sweep_settle_s=0)
    _write(cfg.sweep_transcripts_root / f"{SID}.jsonl",
           _meta() + _user("u1") + _asst("a1"))
    (cfg.failed_dir / f"{SID}.json").write_text("{}", encoding="utf-8")
    _write(cfg.sweep_transcripts_root / "sess-healthy.jsonl",
           _meta() + _user("u2") + _asst("a2"))

    report = sweep.run_sweep(cfg, {})

    assert report.enqueued_sessions == ["sess-healthy"]


# --------------------------------------------------------------------------- #
# P2.F1 rule 5 — _line_count and new_last_line speak the same units            #
# --------------------------------------------------------------------------- #
_CORPUS = {
    "healthy": (lambda: _meta() + _user("u1") + _asst("a1"), 3),
    "interior": (lambda: _meta() + _user("u1") + _torn() + b"\n" + _asst("a1"), 4),
    "interior_multibyte": (
        lambda: _user("u1") + _torn() + b" caf\xc3\n" + _asst("a1"), 3),
    "trailing": (lambda: _meta() + _user("u1") + _torn(), 2),
    "trailing_run": (lambda: _user("u1") + _torn("t1") + b"\n" + _torn("t2"), 1),
    "mixed": (lambda: _meta() + _user("u1") + _torn() + b"\n" + _asst("a1") + _torn(), 4),
    "all_torn": (lambda: _torn(), 0),
    "empty": (lambda: b"", 0),
}


@pytest.mark.parametrize("name", list(_CORPUS))
def test_line_count_agrees_with_new_last_line(tmp_path, name):
    build, expected = _CORPUS[name]
    path = _write(tmp_path / f"{name}.jsonl", build())

    assert read_delta(path, 0).new_last_line == expected
    assert sweep._line_count(path) == expected
    # …and the pointer is stable: re-reading from it yields nothing new.
    assert read_delta(path, expected).new_last_line == expected


def test_line_count_of_an_unreadable_transcript_is_none(tmp_path):
    assert sweep._line_count(tmp_path / "missing.jsonl") is None


# --------------------------------------------------------------------------- #
# state.json durability (wave-e review)                                        #
# --------------------------------------------------------------------------- #
# `_atomic_write_json` is rename-atomic but was not DURABLE: without an fsync on
# the temp file and on the parent directory, a power loss or a host kill — failure
# modes this codebase designs for everywhere else — can make the rename durable
# while the data is not, leaving a truncated `state.json`. `_load_state` then
# swallowed the JSONDecodeError and returned `{}`: every pointer lost, so every
# session is re-summarized from a DIFFERENT `covered_from` and therefore with a
# different `content_hash` — the server's dedupe does not absorb that — and since
# E6 every stat pair is lost with them.


def _fsync_trace(monkeypatch) -> list:
    """Records ``('fsync', 'dir'|'file')`` and ``('replace',)`` in call order."""
    trace: list = []
    real_fsync, real_replace = os.fsync, os.replace

    def traced_fsync(fd):
        kind = "dir" if stat_module.S_ISDIR(os.fstat(fd).st_mode) else "file"
        trace.append(("fsync", kind))
        return real_fsync(fd)

    def traced_replace(src, dst):
        trace.append(("replace",))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", traced_fsync)
    monkeypatch.setattr(os, "replace", traced_replace)
    return trace


def test_atomic_write_fsyncs_the_data_before_the_rename_and_the_dir_after(
        tmp_path, monkeypatch):
    """The two syncs are not interchangeable: the data sync must precede the rename
    (or the rename can outlive the bytes) and the directory sync must follow it (or
    the rename itself can be lost)."""
    path = tmp_path / "home" / "state.json"
    trace = _fsync_trace(monkeypatch)

    processor._atomic_write_json(path, {"/t.jsonl": {"last_line": 3}})

    assert json.loads(path.read_text(encoding="utf-8")) == {"/t.jsonl": {"last_line": 3}}
    assert ("fsync", "file") in trace, "the temp file's data was never fsynced"
    assert ("fsync", "dir") in trace, "the parent directory was never fsynced"
    assert trace.index(("fsync", "file")) < trace.index(("replace",)) \
        < trace.index(("fsync", "dir")), f"wrong order: {trace}"


def test_pointer_write_goes_through_the_durable_path(tmp_path, monkeypatch):
    """The pointer is the thing whose loss costs mass re-summarization, so assert the
    durability at ``_advance_pointer``, not only at the helper."""
    cfg = _cfg(tmp_path)
    trace = _fsync_trace(monkeypatch)

    processor._advance_pointer(cfg, {}, "/t.jsonl", 3, "u-3", (16, 77, 1234, 56))

    assert ("fsync", "file") in trace and ("fsync", "dir") in trace
    assert json.loads(cfg.state_path.read_text(encoding="utf-8"))["/t.jsonl"][
        "last_line"] == 3


def test_a_rejected_directory_fsync_does_not_fail_the_write(tmp_path, monkeypatch):
    """Some filesystems reject fsync on a directory fd. That is best-effort hardening,
    not a reason to blow up a pass — the write itself already succeeded."""
    path = tmp_path / "home" / "state.json"
    real_fsync = os.fsync

    def picky_fsync(fd):
        if stat_module.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(22, "Invalid argument")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", picky_fsync)

    processor._atomic_write_json(path, {"ok": True})

    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}


def test_a_directory_that_cannot_be_opened_does_not_fail_the_write(tmp_path, monkeypatch):
    """Same tolerance one step earlier: ``os.open`` of the directory may itself fail."""
    path = tmp_path / "home" / "state.json"
    real_open = os.open

    def picky_open(target, flags, *args, **kwargs):
        if Path(target) == path.parent:
            raise OSError(13, "Permission denied")
        return real_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", picky_open)

    processor._atomic_write_json(path, {"ok": True})

    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}


def test_an_unparseable_state_is_logged_and_kept_aside_not_silently_dropped(tmp_path):
    """A truncated ``state.json`` still resets every pointer — that cannot be helped
    at read time — but it must leave evidence: a log line and the bytes themselves."""
    cfg = _cfg(tmp_path)
    cfg.state_path.write_text('{"/t.jsonl": {"last_li', encoding="utf-8")

    assert processor._load_state(cfg) == {}

    assert not cfg.state_path.exists(), "the unparseable file must be moved aside"
    corrupt = cfg.state_path.with_name("state.json.corrupt-1")
    assert corrupt.read_text(encoding="utf-8") == '{"/t.jsonl": {"last_li'
    log = cfg.log_file.read_text(encoding="utf-8")
    assert "state_unparseable" in log
    assert "state.json.corrupt-1" in log
    assert "last_li" not in log, "the log must carry the event, not the file's content"


def test_successive_corruptions_never_overwrite_the_previous_evidence(tmp_path):
    cfg = _cfg(tmp_path)
    for n in (1, 2, 3):
        cfg.state_path.write_text(f"not json {n}", encoding="utf-8")
        assert processor._load_state(cfg) == {}
    for n in (1, 2, 3):
        assert cfg.state_path.with_name(f"state.json.corrupt-{n}").read_text(
            encoding="utf-8") == f"not json {n}"


def test_a_missing_state_is_not_treated_as_corruption(tmp_path):
    cfg = _cfg(tmp_path)

    assert processor._load_state(cfg) == {}

    assert list(cfg.loom_home.glob("state.json.corrupt-*")) == []
    assert not cfg.log_file.exists() or "state_unparseable" not in cfg.log_file.read_text(
        encoding="utf-8")


def test_a_failed_set_aside_still_returns_an_empty_state(tmp_path, monkeypatch):
    """The rename is diagnostics. If it cannot happen, the pass still goes on with an
    empty state rather than crashing on the way in."""
    cfg = _cfg(tmp_path)
    cfg.state_path.write_text("{{{", encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(processor.os, "replace", boom)
    monkeypatch.setattr(Path, "rename", boom)

    assert processor._load_state(cfg) == {}
