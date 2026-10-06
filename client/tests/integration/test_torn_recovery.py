"""End-to-end recovery from torn transcripts (P2.F1 / P2.F2 / P2.F3).

Same real boundaries as ``test_processor.py``: temporary LOOM_HOME, real git repo,
``pytest-httpserver`` as the central server, an executable ``claude`` stub. The
fixtures are written as RAW BYTES so the tears are physically real (truncated JSON,
split UTF-8 sequences), exactly as a SIGKILL leaves them.

Every torn line carries ``TORN_SECRET``: a torn line may hold half a credential
(I5/I6), so the tests sweep the whole LOOM_HOME, the summarizer's stdin and the wire
for it.
"""
from __future__ import annotations

import json
from pathlib import Path

from loom import processor

TEST_REMOTE_CANONICAL = "github.com/org/b"
CANNED_SUMMARY = (
    "## Contexto\nSe trabajó en el adaptador tolerante.\n"
    "## Decisiones\n- **Saltar líneas rotas** — evidencia: transcript_adapter.py.\n"
    "## Pendientes\n- ninguno\n"
)

# Planted partial secret. Matches no scanner regex family: if it shows up anywhere
# it is a LEAK, never a quarantine.
TORN_SECRET = "INTEGTORNSECRET-3b1a9e"
PRE_TEXT = "pre-tear: keep the flask blueprint"
POST_TEXT = "post-tear: split the auth module"


# --------------------------------------------------------------------------- #
# Byte-level fixtures                                                          #
# --------------------------------------------------------------------------- #
def _jline(obj: dict) -> bytes:
    return json.dumps(obj).encode("utf-8") + b"\n"


def _meta() -> bytes:
    return _jline({"type": "file-history-snapshot", "messageId": "m0", "snapshot": {}})


def _user(sid: str, cwd: Path, text: str) -> bytes:
    return _jline({"type": "user", "uuid": f"{sid}-u1", "cwd": str(cwd),
                   "sessionId": sid,
                   "message": {"role": "user", "content": text}})


def _asst(sid: str, cwd: Path, text: str) -> bytes:
    return _jline({"type": "assistant", "uuid": f"{sid}-a1", "cwd": str(cwd),
                   "sessionId": sid,
                   "message": {"role": "assistant",
                               "content": [{"type": "text", "text": text}]}})


def _torn_prefix(uuid: str) -> bytes:
    return (b'{"type":"assistant","uuid":"' + uuid.encode("ascii")
            + b'","message":{"role":"assistant","content":[{"type":"text","text":"'
            + TORN_SECRET.encode("ascii"))


def _torn_midline() -> bytes:
    return _torn_prefix("torn-mid") + b"\n"


def _torn_multibyte() -> bytes:
    """The tear splits a UTF-8 sequence: the line ends on a lone 0xC3."""
    return _torn_prefix("torn-mb") + b" caf\xc3\n"


def _torn_trailing() -> bytes:
    """A writer caught mid-append: no trailing newline, nothing after it."""
    return _torn_prefix("torn-tail")


def _write(path: Path, blob: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return path


# --------------------------------------------------------------------------- #
# Boundary helpers                                                             #
# --------------------------------------------------------------------------- #
def _recording_stub(env, *, extra: str = "") -> tuple[str, Path]:
    """A ``claude`` stub that captures every stdin it receives (the leak oracle).

    ``extra`` is python injected after the capture — used to race the processor with
    the unlocked hook (P2.F3).
    """
    capture = env.home.parent / "summarizer_stdin"
    capture.mkdir(parents=True, exist_ok=True)
    path = env.home / "claude_recording.py"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, time\n"
        "data = sys.stdin.read()\n"
        f"cap = {str(capture)!r}\n"
        "name = 'stdin-%d-%d.txt' % (os.getpid(), time.time_ns())\n"
        "open(os.path.join(cap, name), 'w', encoding='utf-8').write(data)\n"
        f"{extra}"
        f"sys.stdout.write({CANNED_SUMMARY!r})\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path), capture


def _stdin_text(capture: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(capture.glob("*.txt")))


def _leak_hits(home: Path, capture: Path, httpserver) -> list[str]:
    needle = TORN_SECRET.encode("utf-8")
    hits = []
    for base in (home, capture):
        for f in sorted(p for p in base.rglob("*") if p.is_file()):
            if needle in f.read_bytes():
                hits.append(str(f))
    for req, _resp in httpserver.log:
        if needle in (req.get_data() or b""):
            hits.append(f"HTTP {req.method} {req.path}")
    return hits


def _wire(env) -> None:
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201
    )


def _actions(report):
    return [o.action for o in report.outcomes]


# --------------------------------------------------------------------------- #
# P2.F1 — recovery                                                             #
# --------------------------------------------------------------------------- #
def test_torn_midline_then_resumed_recovers_in_one_pass(env, tmp_path):
    sid = "sess-torn-mid"
    t = _write(tmp_path / "torn_mid.jsonl",
               _meta() + _user(sid, env.repo, PRE_TEXT) + _torn_midline()
               + _asst(sid, env.repo, POST_TEXT))
    stub, capture = _recording_stub(env)
    cfg = env.config(stub)
    env.drop_marker(sid, transcript=t)
    _wire(env)

    report = processor.run_once(cfg)

    assert _actions(report) == ["posted"]
    assert len(env.posted_entries()) == 1
    assert not list(cfg.queue_dir.glob("*.json"))
    assert env.state()[str(t)]["last_line"] == 4      # the interior tear IS consumed

    stdin = _stdin_text(capture)
    assert f"[user]: {PRE_TEXT}" in stdin and f"[assistant]: {POST_TEXT}" in stdin
    assert _leak_hits(env.home, capture, env.httpserver) == []


def test_torn_mid_multibyte_never_reaches_the_catch_all(env, tmp_path):
    """Today a split UTF-8 sequence raises UnicodeDecodeError out of read_delta and
    lands in run_once's catch-all as a bare 'error' with no diagnostic."""
    sid = "sess-torn-mb"
    t = _write(tmp_path / "torn_mb.jsonl",
               _meta() + _user(sid, env.repo, PRE_TEXT) + _torn_multibyte()
               + _asst(sid, env.repo, POST_TEXT))
    stub, capture = _recording_stub(env)
    cfg = env.config(stub)
    env.drop_marker(sid, transcript=t)
    _wire(env)

    report = processor.run_once(cfg)

    assert "error" not in _actions(report)
    assert len(env.posted_entries()) == 1
    assert _leak_hits(env.home, capture, env.httpserver) == []


def test_trailing_tear_parks_the_pointer_and_does_not_duplicate(env, tmp_path):
    sid = "sess-torn-tail"
    t = _write(tmp_path / "torn_tail.jsonl",
               _meta() + _user(sid, env.repo, PRE_TEXT) + _torn_trailing())
    stub, capture = _recording_stub(env)
    cfg = env.config(stub)
    env.drop_marker(sid, transcript=t)
    _wire(env)

    processor.run_once(cfg)
    assert env.state()[str(t)]["last_line"] == 2       # parks BEFORE the tear

    # A second marker over the unchanged file must not produce a second entry.
    env.drop_marker(sid, transcript=t)
    processor.run_once(cfg)
    assert len(env.posted_entries()) == 1
    assert not list(cfg.queue_dir.glob("*.json"))
    assert _leak_hits(env.home, capture, env.httpserver) == []


def test_healed_trailing_tear_is_captured_on_the_next_pass(env, tmp_path):
    sid = "sess-heal"
    complete = _asst(sid, env.repo, POST_TEXT)
    cut = len(complete) // 2
    t = _write(tmp_path / "heal.jsonl",
               _meta() + _user(sid, env.repo, PRE_TEXT) + complete[:cut])
    stub, capture = _recording_stub(env)
    cfg = env.config(stub)
    env.drop_marker(sid, transcript=t)
    _wire(env)

    processor.run_once(cfg)
    assert env.state()[str(t)]["last_line"] == 2

    with t.open("ab") as fh:                            # the writer finishes
        fh.write(complete[cut:])
    env.drop_marker(sid, transcript=t)
    processor.run_once(cfg)

    assert env.state()[str(t)]["last_line"] == 3
    assert f"[assistant]: {POST_TEXT}" in _stdin_text(capture)


def test_nonzero_torn_count_is_surfaced_in_the_run_log(env, tmp_path):
    sid = "sess-torn-log"
    t = _write(tmp_path / "torn_log.jsonl",
               _meta() + _user(sid, env.repo, PRE_TEXT) + _torn_midline()
               + _torn_multibyte() + _asst(sid, env.repo, POST_TEXT))
    stub, capture = _recording_stub(env)
    cfg = env.config(stub)
    env.drop_marker(sid, transcript=t)
    _wire(env)

    processor.run_once(cfg)

    log = cfg.log_file.read_text(encoding="utf-8")
    assert "torn=2" in log
    assert TORN_SECRET not in log


# --------------------------------------------------------------------------- #
# P2.F2 — bounding                                                             #
# --------------------------------------------------------------------------- #
def test_schema_invalid_transcript_is_bounded_and_parked(env, tmp_path):
    """Rule 3: a line that parses and decodes but has no uuid/message is corruption.
    It stays an AdapterError — and now it is BOUNDED instead of retried forever."""
    sid = "sess-schema"
    t = _write(tmp_path / "schema.jsonl",
               _meta() + _user(sid, env.repo, PRE_TEXT) + _jline({"type": "user"}))
    stub, _capture = _recording_stub(env)
    cfg = env.config(stub)
    marker_path = env.drop_marker(sid, transcript=t)
    _wire(env)

    actions = []
    for expected in range(1, processor.MAX_ATTEMPTS):
        actions += _actions(processor.run_once(cfg))
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        assert marker["attempts"] == expected
        assert marker["adapter_error"]

    actions += _actions(processor.run_once(cfg))

    assert actions == ["kept_adapter_error"] * (processor.MAX_ATTEMPTS - 1) + [
        "failed_parked"
    ]
    assert not marker_path.exists()
    parked = json.loads((cfg.failed_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert parked["attempts"] == processor.MAX_ATTEMPTS
    assert parked["adapter_error"]
    assert env.state() == {}, "no pointer may be written on an adapter-failure path"


def test_adapter_failure_never_advances_an_existing_pointer(env, tmp_path):
    sid = "sess-ptr"
    t = _write(tmp_path / "ptr.jsonl",
               _meta() + _user(sid, env.repo, PRE_TEXT) + _jline({"type": "user"}))
    stub, _capture = _recording_stub(env)
    cfg = env.config(stub)
    cfg.state_path.write_text(json.dumps({
        str(t): {"last_line": 1, "last_uuid": "seed-u0", "updated_at": "2026-08-05T00:00:00Z"}
    }), encoding="utf-8")
    env.drop_marker(sid, transcript=t)
    _wire(env)

    for _ in range(processor.MAX_ATTEMPTS + 1):
        processor.run_once(cfg)
        entry = env.state()[str(t)]
        assert entry["last_line"] == 1 and entry["last_uuid"] == "seed-u0"


def test_vanished_transcript_is_dropped_not_an_adapter_failure(env, tmp_path):
    """Frozen matrix row: 'transcript missing' still drops the marker (step 1)."""
    sid = "sess-gone"
    stub, _capture = _recording_stub(env)
    cfg = env.config(stub)
    env.drop_marker(sid, transcript=tmp_path / "never_existed.jsonl")

    report = processor.run_once(cfg)

    assert _actions(report) == ["dropped_no_transcript"]


# --------------------------------------------------------------------------- #
# P2.F3 — marker identity                                                      #
# --------------------------------------------------------------------------- #
def test_hook_rewritten_marker_survives_the_drop(env, tmp_path):
    sid = "sess-hookswap"
    t = _write(tmp_path / "hookswap.jsonl",
               _meta() + _user(sid, env.repo, PRE_TEXT) + _asst(sid, env.repo, POST_TEXT))
    marker_path = env.home / "queue" / f"{sid}.json"
    fresh = {"session_id": sid, "transcript_path": str(t), "cwd": str(env.repo),
             "reason": "prompt_input_exit", "marked_at": "2026-08-05T23:59:59Z",
             "hook_generation": 2}
    swap = (
        f"_mp = {str(marker_path)!r}\n"
        f"open(_mp + '.swap', 'w', encoding='utf-8').write({json.dumps(fresh)!r})\n"
        f"os.replace(_mp + '.swap', _mp)\n"
    )
    stub, _capture = _recording_stub(env, extra=swap)
    cfg = env.config(stub)
    env.drop_marker(sid, transcript=t)
    _wire(env)

    report = processor.run_once(cfg)

    assert _actions(report) == ["posted"]
    assert marker_path.exists(), "the hook's fresh marker must survive the drop"
    assert json.loads(marker_path.read_text(encoding="utf-8")) == fresh

    # …and it is processed normally on the next pass (no duplicate content).
    processor.run_once(cfg)
    assert not list(cfg.queue_dir.glob("*.json"))
    assert len(env.posted_entries()) == 1


def test_marker_vanishing_mid_pass_does_not_break_the_drop(env, tmp_path):
    sid = "sess-vanish"
    t = _write(tmp_path / "vanish.jsonl",
               _meta() + _user(sid, env.repo, PRE_TEXT) + _asst(sid, env.repo, POST_TEXT))
    marker_path = env.home / "queue" / f"{sid}.json"
    stub, _capture = _recording_stub(env, extra=f"os.remove({str(marker_path)!r})\n")
    cfg = env.config(stub)
    env.drop_marker(sid, transcript=t)
    _wire(env)

    report = processor.run_once(cfg)

    assert _actions(report) == ["posted"]
    assert not list(cfg.queue_dir.glob("*.json"))
