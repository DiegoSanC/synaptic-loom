"""FROZEN GOAL — E5 capture-reliability (P2.F1 / P2.F2 / P2.F3). Read-only to implementers.

Oracle properties: temp LOOM_HOME + byte-level JSONL fixtures on a real filesystem
drive the real ``processor.run_once`` / ``sweep.run_sweep`` flow; the summarizer is a
scripted, RECORDING ``claude`` shim (stdin captured per invocation — the leak oracle);
the central server is pytest-httpserver. No adapter/processor seam is mocked. The
only instrumentation is observational (an open-counter around ``Path.open`` for the
AC-F2.2 cheapness clause, explicitly sanctioned by the goal mandate).

Adversarial traps baked in (each kills a plausible-but-wrong implementation):
- every torn line carries ``TORN_SECRET``; a full-home + stdin + wire sweep proves the
  bytes reach nothing (AC-F1.4 / I5-I6);
- a COMPLETE JSON line with one invalid UTF-8 byte: ``errors="replace"`` laundering
  would decode+parse it into a message — it must be SKIPPED as torn;
- a trailing torn line that later HEALS: advancing the pointer past the tear loses the
  healed content (AC-F1.3);
- the AC-F1.6 corpus holds interior AND trailing tears, so reconciling only one of
  ``read_delta.new_last_line`` / ``sweep._line_count`` dies (spec amendment 2026-08-05:
  ``_line_count`` adopts the trailing-hold; new AC-F1.7 closes the enqueue loop);
- a schema-invalid line (parses, decodes, no uuid/message) must STAY ``AdapterError``
  (rule 3) — over-tolerant "skip anything odd" implementations die on AC-F2.1.

AC map (auditable coverage — every AC has a named home):
  AC-F1.1  test_ac_f1_1_torn_midline_resumed_recovered_in_one_pass
  AC-F1.2  test_ac_f1_2_torn_mid_multibyte_delta_recovers_without_decode_error
           test_ac_f2_unicode_decode_error_never_hits_run_once_catch_all
  AC-F1.3  test_ac_f1_3_trailing_torn_pointer_holds_then_heals
  AC-F1.4  test_ac_f1_4_torn_bytes_reach_no_stdin_no_log_no_entry
           test_ac_f1_4_replace_laundering_is_skip_not_message
  AC-F1.5  test_ac_f1_5_healthy_kit_transcript_byte_identical
           test_ac_f1_5_healthy_real_session_golden_pins
  AC-F1.6  test_ac_f1_6_line_count_and_new_last_line_agree[<corpus>]
  AC-F1.7  test_ac_f1_7_processed_trailing_torn_is_not_reenqueued
  AC-F1.8  test_ac_f1_8_wholly_unparseable_transcript_is_never_silent
           test_ac_f1_8_held_counts_the_trailing_run_beside_interior_torn
           test_ac_f1_8_healthy_transcript_holds_nothing
  rule 3   test_ac_f1_rule3_schema_invalid_line_still_raises_adapter_error
  AC-F2.1  test_ac_f2_1_adapter_failure_attempts_then_parked_with_diagnostics
  AC-F2.2  test_ac_f2_2_sweep_skips_failed_parked_session_without_reading_transcript
  AC-F2.3  test_ac_f2_3_registry_down_keep_path_never_touches_attempts
           test_ac_f2_3_network_down_post_keep_path_never_touches_attempts
  AC-F2.4  test_ac_f2_4_pointer_never_advances_on_adapter_failure
  AC-F3.1  test_ac_f3_1_hook_replaced_marker_survives_drop_and_processes_next_pass
  AC-F3.2  test_ac_f3_2_unchanged_marker_drops_exactly_as_today
  AC-F3.3  test_ac_f3_3_identity_check_error_degrades_to_unlink_never_crashes

Expected against pre-E5 code (verified at authoring time, 2026-08-05): the AC-F1.5 /
rule-3 / AC-F2.3 / AC-F2.4 / AC-F3.2 / AC-F3.3 tests and the healthy AC-F1.6 case PASS
(frozen-matrix + byte-identity regressions); everything else FAILS on new-contract
assertions (kept_adapter_error forever, UnicodeDecodeError escaping, blind unlink).

Amendment 2026-08-10 (P2.F1, wave-d review — AC-F1.8, purely additive). ``Delta.torn``
counts INTERIOR tears only, so a transcript whose every line is torn produced
``held_at=0, torn=0``, an empty delta and outcome ``dropped_empty_delta``: a capture
lost with no log line, no counter, no diagnostic. ``Delta.held`` (additive, default 0)
reports the trailing held run — the lines from ``new_last_line`` to EOF the delta
deliberately did not consume — and the processor logs it when it is nonzero. It is
REPORTED, not acted upon: no torn-ratio escalation (deferred to phase 4), and the
AC-F1.6 ``_line_count``/``new_last_line`` agreement is unchanged and re-asserted here.

Run: .venv/bin/python -m pytest test/waves/d/test_goal_e5_recovery.py -q
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _goalkit as kit  # noqa: E402

from loom import processor, sweep  # noqa: E402
from loom.config import Config  # noqa: E402
from loom.transcript_adapter import (  # noqa: E402
    AdapterError,
    read_delta,
    render_for_summarizer,
)

SUMMARY = "## Decision\nGoal E5: capture-reliability summary."
# The planted "partial secret" inside every torn line. Chosen to match NO regex
# family (no sk-/password=/@/AKIA shape): if it surfaces anywhere it is a LEAK,
# never a quarantine.
TORN_SECRET = "TORNSECRETMARKER-5f4dcc3b5aa7"
PRE_TEXT = "pre-tear decision: keep the flask blueprint"
MID_TEXT = "mid decision: also captured"
POST_TEXT = "post-tear decision: split the auth module"
CORPUS_CWD = "/goal/e5/corpus"

REAL_SESSION = kit.REPO_ROOT / "contract" / "fixtures" / "real_session.jsonl"
# Golden pins computed against the pre-E5 adapter (2026-08-05) — AC-F1.5 byte-identity.
REAL_PIN_LAST_LINE = 24
REAL_PIN_LAST_UUID = "7fc32efa-c52e-4d17-83fc-68806a1c4d58"
REAL_PIN_SKIPPED = 16
REAL_PIN_N_MESSAGES = 8
REAL_PIN_RENDERED_SHA256 = (
    "1d6b3e3e3044edaef8026771df77c5f322d57eba4c04f1013f6ebc5fd8be70df"
)


# --------------------------------------------------------------------------- #
# Byte-level fixture builders (tears are constructed as raw bytes on disk)     #
# --------------------------------------------------------------------------- #
def _jline(obj: dict) -> bytes:
    return json.dumps(obj).encode("utf-8") + b"\n"


def _meta_line() -> bytes:
    return _jline({"type": "file-history-snapshot", "messageId": "m0", "snapshot": {}})


def _user_line(sid: str, cwd: str, text: str, n: int = 1) -> bytes:
    return _jline({
        "type": "user", "uuid": f"{sid}-u{n}", "cwd": cwd, "sessionId": sid,
        "message": {"role": "user", "content": text},
    })


def _asst_line(sid: str, cwd: str, text: str, n: int = 1) -> bytes:
    return _jline({
        "type": "assistant", "uuid": f"{sid}-a{n}", "cwd": cwd, "sessionId": sid,
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": text}]},
    })


def _torn_json_prefix(uuid: str) -> bytes:
    return (
        b'{"type":"assistant","uuid":"' + uuid.encode("ascii")
        + b'","message":{"role":"assistant","content":[{"type":"text","text":"'
    )


def _torn_midline_bytes(uuid: str = "torn-a8") -> bytes:
    """Crash-torn mid-line: truncated JSON, pure ASCII, newline-terminated."""
    return _torn_json_prefix(uuid) + TORN_SECRET.encode("ascii") + b"\n"


def _torn_multibyte_bytes(uuid: str = "torn-b9") -> bytes:
    """Crash-torn mid-multibyte: the line ends inside a UTF-8 sequence (lone 0xC3)."""
    return _torn_json_prefix(uuid) + TORN_SECRET.encode("ascii") + b" caf\xc3\n"


def _launder_line_bytes(cwd: str) -> bytes:
    """The laundering trap: a COMPLETE, schema-valid JSON line whose string value
    holds one invalid UTF-8 byte. ``errors='replace'`` decodes it, json parses it,
    and a lazy adapter turns it into a message — the spec says: fails strict UTF-8
    decode ⇒ torn ⇒ SKIPPED."""
    obj = {"type": "user", "uuid": "launder-u9", "cwd": cwd,
           "message": {"role": "user",
                       "content": f"{TORN_SECRET} @@BAD@@ laundered"}}
    return json.dumps(obj).encode("utf-8").replace(b"@@BAD@@", b"\xff") + b"\n"


def _trailing_torn_bytes(uuid: str = "trail-c7") -> bytes:
    """A writer-mid-append tail: truncated JSON, NO trailing newline."""
    return _torn_json_prefix(uuid) + TORN_SECRET.encode("ascii")


def _write_fixture(path: Path, blob: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return path


# --------------------------------------------------------------------------- #
# Boundary builders                                                            #
# --------------------------------------------------------------------------- #
def _mk_cfg(tmp_path, *, server_url: str = "http://127.0.0.1:9",
            claude_bin: str = "claude", sweep_enabled: bool = False,
            settle_s: int = 1800) -> Config:
    """Temp LOOM_HOME + written config.json loaded through the production loader.
    ``transcripts_root`` is ALWAYS pinned under tmp — never ~/.claude/projects."""
    home = kit.loom_home(tmp_path)
    root = tmp_path / "transcripts"
    root.mkdir(exist_ok=True)
    kit.write_config(home, server_url=server_url, claude_bin=claude_bin,
                     settle_s=settle_s, transcripts_root=root,
                     sweep_enabled=sweep_enabled, scanner_engine="regex",
                     gitleaks_version=None)
    return Config.load(home)


def _claude_shim(tmp_path: Path, *, stdout: str = SUMMARY,
                 extra: str = "") -> tuple[str, Path, Path]:
    """A recording ``claude`` shim: every invocation's stdin is captured to a file
    (the summarizer-stdin oracle for AC-F1.4). ``extra`` is python injected between
    the capture and the reply — used to simulate the unlocked hook racing the
    processor mid-summarize (AC-F3.1/AC-F3.3). Returns (shim_path, capture_dir, shims).
    """
    shims = kit.shim_dir(tmp_path)
    capture = tmp_path / "summarizer_stdin"
    capture.mkdir(exist_ok=True)
    body = f"""
import os, sys, time
data = "" if sys.stdin.isatty() else sys.stdin.read()
cap = {str(capture)!r}
os.makedirs(cap, exist_ok=True)
name = "stdin-%d-%d.txt" % (os.getpid(), time.time_ns())
with open(os.path.join(cap, name), "w", encoding="utf-8") as fh:
    fh.write(data)
{extra}
sys.stdout.write({stdout!r})
sys.exit(0)
"""
    shim = kit.write_shim(shims, "claude", body)
    return str(shim), capture, shims


def _hook_marker(cfg: Config, sid: str, transcript: Path, cwd) -> Path:
    cfg.queue_dir.mkdir(parents=True, exist_ok=True)
    marker = {"session_id": sid, "transcript_path": str(transcript),
              "cwd": str(cwd), "reason": "prompt_input_exit",
              "marked_at": kit.utc_now_iso()}
    path = cfg.queue_dir / f"{sid}.json"
    path.write_text(json.dumps(marker), encoding="utf-8")
    return path


def _wire_server(httpserver, *, canonical: str = kit.TEST_REMOTE_CANONICAL) -> None:
    httpserver.expect_request("/api/projects", method="GET").respond_with_json(
        {"projects": [{"id": 1, "canonical_remote": canonical, "name": "phase2-goal",
                       "created_by": "goal", "created_at": "2026-07-01T00:00:00Z"}]}
    )
    httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201
    )


def _posts(httpserver) -> list[dict]:
    return [r.get_json() for r, _ in httpserver.log
            if r.method == "POST" and r.path == "/api/entries"]


def _stdin_text(capture: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8")
                     for p in sorted(capture.glob("*.txt")))


def _leak_hits(home: Path, capture: Path, httpserver) -> list[str]:
    """Every place the torn bytes could have leaked to: the whole LOOM_HOME tree
    (log/, state.json, queue/, failed/, quarantine/, cache), every recorded
    summarizer stdin, and every request that went over the wire."""
    needle = TORN_SECRET.encode("utf-8")
    hits: list[str] = []
    for base in (home, capture):
        for f in sorted(p for p in base.rglob("*") if p.is_file()):
            if needle in f.read_bytes():
                hits.append(str(f))
    for req, _resp in httpserver.log:
        if needle in (req.get_data() or b""):
            hits.append(f"HTTP {req.method} {req.path}")
    return hits


def _tolerant_delta(path: Path, last_line: int, ac: str):
    """read_delta under the NEW tolerance contract: any escape of AdapterError /
    UnicodeDecodeError on a torn (not schema-invalid) fixture is a goal failure."""
    try:
        return read_delta(path, last_line)
    except UnicodeDecodeError as exc:
        pytest.fail(f"{ac}: UnicodeDecodeError escaped read_delta's contract: {exc}")
    except AdapterError as exc:
        pytest.fail(f"{ac}: read_delta must tolerate torn lines, raised AdapterError: {exc}")


# =========================================================================== #
# P2.F1 — tolerant transcript adapter                                          #
# =========================================================================== #
def test_ac_f1_5_healthy_kit_transcript_byte_identical(tmp_path):
    """AC-F1.5: a healthy transcript is byte-identical to phase-1 behavior."""
    sid = "e5-healthy"
    t = tmp_path / "healthy.jsonl"
    kit.write_transcript(t, kit.transcript_lines("/goal/e5/healthy", sid))

    d = read_delta(t, 0)
    assert [(m.role, m.text, m.uuid) for m in d.messages] == [
        ("user", "Refactor the auth flow", f"{sid}-u1"),
        ("assistant", "Decided to keep bearer tokens.", f"{sid}-a1"),
    ]
    assert (d.new_last_line, d.new_last_uuid, d.skipped) == (3, f"{sid}-a1", 1)
    assert getattr(d, "torn", 0) == 0
    assert render_for_summarizer(d) == (
        "[user]: Refactor the auth flow\n"
        "[assistant]: Decided to keep bearer tokens."
    )


def test_ac_f1_5_healthy_real_session_golden_pins():
    """AC-F1.5 on the REAL contract fixture: pins frozen at authoring time against
    the pre-E5 adapter. Any drift in messages/pointer/rendering is a regression."""
    d = read_delta(REAL_SESSION, 0)
    assert d.new_last_line == REAL_PIN_LAST_LINE
    assert d.new_last_uuid == REAL_PIN_LAST_UUID
    assert d.skipped == REAL_PIN_SKIPPED
    assert len(d.messages) == REAL_PIN_N_MESSAGES
    rendered = render_for_summarizer(d)
    assert hashlib.sha256(rendered.encode("utf-8")).hexdigest() == (
        REAL_PIN_RENDERED_SHA256
    )
    assert getattr(d, "torn", 0) == 0
    # Pointer agreement on the healthy corpus (AC-F1.6's baseline) + idempotence.
    assert sweep._line_count(REAL_SESSION) == REAL_PIN_LAST_LINE
    d2 = read_delta(REAL_SESSION, d.new_last_line)
    assert d2.messages == [] and d2.new_last_line == REAL_PIN_LAST_LINE


def test_ac_f1_rule3_schema_invalid_line_still_raises_adapter_error(tmp_path):
    """P2.F1 rule 3: a line that parses AND decodes but lacks uuid/message is
    CORRUPTION, not a tear — AdapterError today, AdapterError after E5. Kills the
    over-tolerant implementation that skips anything it dislikes. A complete valid
    line follows it, so 'interior ⇒ skip' reasoning must not swallow it either."""
    sid = "e5-rule3"
    blob = (_meta_line() + _user_line(sid, CORPUS_CWD, PRE_TEXT)
            + _jline({"type": "assistant"})           # parses fine, schema-invalid
            + _asst_line(sid, CORPUS_CWD, POST_TEXT))
    t = _write_fixture(tmp_path / "rule3.jsonl", blob)
    with pytest.raises(AdapterError):
        read_delta(t, 0)


def test_ac_f1_1_torn_midline_resumed_recovered_in_one_pass(
        tmp_path, httpserver, monkeypatch):
    """AC-F1.1: torn mid-line then resumed — ONE pass captures pre-tear and
    post-tear content, the marker drains, the pointer lands at EOF."""
    shim, capture, shims = _claude_shim(tmp_path)
    monkeypatch.setenv("PATH", f"{shims}{os.pathsep}{os.environ['PATH']}")
    cfg = _mk_cfg(tmp_path, server_url=httpserver.url_for(""), claude_bin=shim)
    repo = kit.make_git_repo(tmp_path / "realrepo")
    sid = "e5-resume-0001"
    blob = (_meta_line() + _user_line(sid, str(repo), PRE_TEXT)
            + _torn_midline_bytes() + _asst_line(sid, str(repo), POST_TEXT))
    t = _write_fixture(tmp_path / "sessions" / f"{sid}.jsonl", blob)
    _hook_marker(cfg, sid, t, repo)
    _wire_server(httpserver)

    report = processor.run_once(cfg)

    posts = _posts(httpserver)
    assert len(posts) == 1 and posts[0]["session_id"] == sid, (
        f"AC-F1.1: one pass must recover the torn+resumed session; "
        f"outcomes={report.outcomes}"
    )
    assert not list(cfg.queue_dir.glob("*.json")), "the marker must drain"
    state = json.loads(cfg.state_path.read_text(encoding="utf-8"))
    assert state[str(t)]["last_line"] == 4, "pointer must land at EOF (4 lines)"
    stdin = _stdin_text(capture)
    assert f"[user]: {PRE_TEXT}" in stdin, "pre-tear content must be summarized"
    assert f"[assistant]: {POST_TEXT}" in stdin, "post-tear content must be summarized"
    assert TORN_SECRET not in stdin, "torn bytes must not reach the summarizer"


def test_ac_f1_2_torn_mid_multibyte_delta_recovers_without_decode_error(tmp_path):
    """AC-F1.2: a tear inside a UTF-8 sequence recovers exactly like AC-F1.1 and no
    UnicodeDecodeError escapes read_delta's contract."""
    sid = "e5-mb"
    blob = (_meta_line() + _user_line(sid, CORPUS_CWD, PRE_TEXT)
            + _torn_multibyte_bytes() + _asst_line(sid, CORPUS_CWD, POST_TEXT))
    t = _write_fixture(tmp_path / "mb.jsonl", blob)

    d = _tolerant_delta(t, 0, "AC-F1.2")
    assert [m.uuid for m in d.messages] == [f"{sid}-u1", f"{sid}-a1"], (
        "pre- and post-tear messages must both be captured"
    )
    assert d.new_last_line == 4
    assert getattr(d, "torn", None) == 1, (
        "Delta must expose the additive int field 'torn' (frozen seam "
        "p3-adapter-tolerance) counting the skipped torn line"
    )
    assert all(TORN_SECRET not in m.text for m in d.messages)


def test_ac_f2_unicode_decode_error_never_hits_run_once_catch_all(
        tmp_path, httpserver, monkeypatch):
    """AC-F1.2 + P2.F2: today the mid-multibyte tear escapes to run_once's
    catch-all (action 'error', no diagnostic, invisible in status). After E5 the
    pass must recover it — and NO adapter-class failure may surface as 'error'."""
    shim, capture, shims = _claude_shim(tmp_path)
    monkeypatch.setenv("PATH", f"{shims}{os.pathsep}{os.environ['PATH']}")
    cfg = _mk_cfg(tmp_path, server_url=httpserver.url_for(""), claude_bin=shim)
    repo = kit.make_git_repo(tmp_path / "realrepo")
    sid = "e5-mb-pipeline"
    blob = (_meta_line() + _user_line(sid, str(repo), PRE_TEXT)
            + _torn_multibyte_bytes() + _asst_line(sid, str(repo), POST_TEXT))
    t = _write_fixture(tmp_path / "sessions" / f"{sid}.jsonl", blob)
    _hook_marker(cfg, sid, t, repo)
    _wire_server(httpserver)

    report = processor.run_once(cfg)

    actions = [o.action for o in report.outcomes]
    assert "error" not in actions, (
        "UnicodeDecodeError must be classified as an adapter failure, never reach "
        f"run_once's catch-all; outcomes={report.outcomes}"
    )
    assert len(_posts(httpserver)) == 1, (
        f"the mid-multibyte torn session must recover; outcomes={report.outcomes}"
    )
    assert not list(cfg.queue_dir.glob("*.json"))


def test_ac_f1_3_trailing_torn_pointer_holds_then_heals(tmp_path):
    """AC-F1.3: the delta ends BEFORE a trailing torn line and the valid prefix is
    captured; an unchanged re-read yields no duplicates; and when the tear HEALS
    (the writer finishes the line) the completed message is captured from the held
    pointer. Kills the implementation that skips the trailing tear and advances
    the pointer past it (the healed content would be lost forever)."""
    sid = "e5-heal"
    full_healed = _asst_line(sid, CORPUS_CWD, "part1 part2 healed", n=2)
    cut = len(full_healed) // 2
    blob = (_meta_line() + _user_line(sid, CORPUS_CWD, PRE_TEXT)
            + _asst_line(sid, CORPUS_CWD, MID_TEXT) + full_healed[:cut])
    t = _write_fixture(tmp_path / "heal.jsonl", blob)

    d1 = _tolerant_delta(t, 0, "AC-F1.3")
    texts = [m.text for m in d1.messages]
    assert PRE_TEXT in texts and MID_TEXT in texts, "valid prefix must be captured"
    assert all("healed" not in x for x in texts)
    assert d1.new_last_line == 3, (
        "AC-F1.3: new_last_line must stop BEFORE the trailing torn line "
        f"(got {d1.new_last_line})"
    )

    d2 = _tolerant_delta(t, d1.new_last_line, "AC-F1.3")
    assert d2.messages == [] and d2.new_last_line == 3, (
        "unchanged file re-read from the held pointer must yield no duplicates"
    )

    with t.open("ab") as fh:  # the writer finishes its append: the tear heals
        fh.write(full_healed[cut:])
    d3 = _tolerant_delta(t, d1.new_last_line, "AC-F1.3")
    assert [m.text for m in d3.messages] == ["part1 part2 healed"], (
        "a healed trailing line must be captured on the next pass — an "
        "implementation that advanced the pointer past the tear loses it"
    )
    assert d3.new_last_uuid == f"{sid}-a2" and d3.new_last_line == 4


def test_ac_f1_4_replace_laundering_is_skip_not_message(tmp_path):
    """AC-F1.4 kill-shot: a COMPLETE JSON line failing only strict UTF-8 decode.
    decoding with errors='replace' parses it into a schema-valid message — the
    laundered bytes (with the partial secret) would flow to the summarizer. The
    spec is strict: fails strict decode ⇒ torn ⇒ skipped, counted, never a Msg."""
    sid = "e5-launder"
    blob = (_meta_line() + _user_line(sid, CORPUS_CWD, PRE_TEXT)
            + _launder_line_bytes(CORPUS_CWD)
            + _asst_line(sid, CORPUS_CWD, POST_TEXT))
    t = _write_fixture(tmp_path / "launder.jsonl", blob)

    d = _tolerant_delta(t, 0, "AC-F1.4")
    assert [m.uuid for m in d.messages] == [f"{sid}-u1", f"{sid}-a1"], (
        "the invalid-byte line must be SKIPPED as torn, not laundered into a "
        f"message via errors='replace'; got uuids {[m.uuid for m in d.messages]}"
    )
    assert getattr(d, "torn", None) == 1
    assert all(TORN_SECRET not in m.text and "laundered" not in m.text
               for m in d.messages)
    assert d.new_last_line == 4


def test_ac_f1_4_torn_bytes_reach_no_stdin_no_log_no_entry(
        tmp_path, httpserver, monkeypatch):
    """AC-F1.4 / I5-I6, full pipeline: a transcript with every tear shape (interior
    mid-line, interior mid-multibyte, complete-but-invalid-byte, trailing partial),
    each carrying the planted partial secret. After a REAL recovery pass the secret
    bytes must appear in NO summarizer stdin, NO file under LOOM_HOME (log/, state,
    queue/, failed/, quarantine/), and NOTHING that went over the wire. Also pins
    P2.F1 rule 4: a nonzero torn count is surfaced in the run log."""
    shim, capture, shims = _claude_shim(tmp_path)
    monkeypatch.setenv("PATH", f"{shims}{os.pathsep}{os.environ['PATH']}")
    cfg = _mk_cfg(tmp_path, server_url=httpserver.url_for(""), claude_bin=shim)
    repo = kit.make_git_repo(tmp_path / "realrepo")
    sid = "e5-leaksweep-0001"
    blob = (_meta_line() + _user_line(sid, str(repo), PRE_TEXT)
            + _torn_midline_bytes() + _torn_multibyte_bytes()
            + _launder_line_bytes(str(repo))
            + _asst_line(sid, str(repo), POST_TEXT)
            + _trailing_torn_bytes())
    t = _write_fixture(tmp_path / "sessions" / f"{sid}.jsonl", blob)
    _hook_marker(cfg, sid, t, repo)
    _wire_server(httpserver)

    report = processor.run_once(cfg)

    assert len(_posts(httpserver)) == 1, (
        f"the multi-tear session must recover in one pass; outcomes={report.outcomes}"
    )
    assert not list(cfg.queue_dir.glob("*.json"))
    state = json.loads(cfg.state_path.read_text(encoding="utf-8"))
    assert state[str(t)]["last_line"] == 6, (
        "pointer must consume the 3 interior tears and hold before the trailing one"
    )
    stdin = _stdin_text(capture)
    assert f"[user]: {PRE_TEXT}" in stdin and f"[assistant]: {POST_TEXT}" in stdin
    assert "laundered" not in stdin, "the laundered line must not reach the summarizer"
    hits = _leak_hits(cfg.loom_home, capture, httpserver)
    assert hits == [], f"torn-line bytes (partial secret) leaked into: {hits}"
    assert "torn" in cfg.log_file.read_text(encoding="utf-8").lower(), (
        "P2.F1 rule 4: a nonzero torn count must be surfaced in the run log"
    )


# AC-F1.6 corpus: BOTH interior and trailing tears — reconciling only one of
# read_delta / _line_count dies here (spec amendment 2026-08-05, P2.F1 rule 5).
def _fx_healthy(base: Path) -> Path:
    sid = "c-healthy"
    return _write_fixture(base / "c_healthy.jsonl",
                          _meta_line() + _user_line(sid, CORPUS_CWD, PRE_TEXT)
                          + _asst_line(sid, CORPUS_CWD, POST_TEXT))


def _fx_interior_midline(base: Path) -> Path:
    sid = "c-mid"
    return _write_fixture(base / "c_mid.jsonl",
                          _meta_line() + _user_line(sid, CORPUS_CWD, PRE_TEXT)
                          + _torn_midline_bytes()
                          + _asst_line(sid, CORPUS_CWD, POST_TEXT))


def _fx_interior_multibyte(base: Path) -> Path:
    sid = "c-mb"
    return _write_fixture(base / "c_mb.jsonl",
                          _meta_line() + _user_line(sid, CORPUS_CWD, PRE_TEXT)
                          + _torn_multibyte_bytes()
                          + _asst_line(sid, CORPUS_CWD, POST_TEXT))


def _fx_interior_invalid_byte(base: Path) -> Path:
    sid = "c-launder"
    return _write_fixture(base / "c_launder.jsonl",
                          _meta_line() + _user_line(sid, CORPUS_CWD, PRE_TEXT)
                          + _launder_line_bytes(CORPUS_CWD)
                          + _asst_line(sid, CORPUS_CWD, POST_TEXT))


def _fx_trailing(base: Path) -> Path:
    sid = "c-trail"
    return _write_fixture(base / "c_trail.jsonl",
                          _meta_line() + _user_line(sid, CORPUS_CWD, PRE_TEXT)
                          + _trailing_torn_bytes())


def _fx_mixed(base: Path) -> Path:
    sid = "c-mixed"
    return _write_fixture(base / "c_mixed.jsonl",
                          _meta_line() + _user_line(sid, CORPUS_CWD, PRE_TEXT)
                          + _torn_midline_bytes() + _torn_multibyte_bytes()
                          + _launder_line_bytes(CORPUS_CWD)
                          + _asst_line(sid, CORPUS_CWD, POST_TEXT)
                          + _trailing_torn_bytes())


# name -> (builder, agreed pointer value: interior tears COUNT, a trailing tear
# does NOT — for both read_delta.new_last_line and sweep._line_count).
_CORPUS = {
    "healthy": (_fx_healthy, 3),
    "interior_midline": (_fx_interior_midline, 4),
    "interior_multibyte": (_fx_interior_multibyte, 4),
    "interior_invalid_byte": (_fx_interior_invalid_byte, 4),
    "trailing": (_fx_trailing, 2),
    "mixed": (_fx_mixed, 6),
}


@pytest.mark.parametrize("name", list(_CORPUS))
def test_ac_f1_6_line_count_and_new_last_line_agree(tmp_path, name):
    """AC-F1.6: sweep._line_count and read_delta.new_last_line agree on every
    fixture, torn ones included — same units, same trailing-hold (P2.F1 rule 5).
    Disagreement in either direction is an enqueue loop or a stall."""
    build, expected = _CORPUS[name]
    t = build(tmp_path / "corpus")
    d = _tolerant_delta(t, 0, f"AC-F1.6[{name}]")
    lc = sweep._line_count(t)
    assert d.new_last_line == expected, (
        f"AC-F1.6[{name}]: read_delta.new_last_line={d.new_last_line}, "
        f"expected {expected}"
    )
    assert lc == expected, (
        f"AC-F1.6[{name}]: sweep._line_count={lc} disagrees with the pointer "
        f"contract ({expected}) — trailing torn lines are not counted, interior "
        f"ones are"
    )
    d2 = _tolerant_delta(t, d.new_last_line, f"AC-F1.6[{name}]")
    assert d2.messages == [] and d2.new_last_line == expected, (
        f"AC-F1.6[{name}]: re-read from the pointer must be an empty, stable delta"
    )


def test_ac_f1_7_processed_trailing_torn_is_not_reenqueued(
        tmp_path, httpserver, monkeypatch):
    """AC-F1.7 (amendment 2026-08-05): a never-resumed trailing-torn transcript is
    processed once (valid prefix captured, pointer parked before the tear) and then
    NOT re-enqueued by any subsequent sweep pass while the file is unchanged —
    the trailing-hold must not become an infinite enqueue loop."""
    shim, capture, shims = _claude_shim(tmp_path)
    monkeypatch.setenv("PATH", f"{shims}{os.pathsep}{os.environ['PATH']}")
    cfg = _mk_cfg(tmp_path, server_url=httpserver.url_for(""), claude_bin=shim,
                  sweep_enabled=True)
    repo = kit.make_git_repo(tmp_path / "realrepo")
    sid = "e5-trailhold-0001"
    t = _write_fixture(cfg.sweep_transcripts_root / "d" / f"{sid}.jsonl",
                       _meta_line() + _user_line(sid, str(repo), PRE_TEXT)
                       + _trailing_torn_bytes())
    kit.age_file(t, 3600)
    mtime_before = t.stat().st_mtime
    _wire_server(httpserver)

    r1 = processor.run_once(cfg)  # sweep enqueues + the same pass processes

    posts = _posts(httpserver)
    assert len(posts) == 1 and posts[0]["session_id"] == sid, (
        f"the valid prefix must be captured once; outcomes={r1.outcomes}, "
        f"sweep={r1.sweep}"
    )
    assert not list(cfg.queue_dir.glob("*.json")), "the synthetic marker must drain"
    state = json.loads(cfg.state_path.read_text(encoding="utf-8"))
    assert state[str(t)]["last_line"] == 2, "pointer parks BEFORE the trailing tear"
    assert t.stat().st_mtime == mtime_before, "processing must never write the transcript"

    r2 = processor.run_once(cfg)
    assert r2.sweep is not None and r2.sweep.enqueued == 0, (
        "AC-F1.7: an unchanged, already-processed trailing-torn transcript must "
        f"not be re-enqueued (sweep={r2.sweep})"
    )
    assert len(_posts(httpserver)) == 1, "no duplicate content may be produced"
    assert not list(cfg.queue_dir.glob("*.json"))

    rep3 = sweep.run_sweep(cfg, json.loads(cfg.state_path.read_text(encoding="utf-8")))
    assert rep3.enqueued == 0 and not list(cfg.queue_dir.glob("*.json")), (
        "AC-F1.7: every later sweep pass must keep skipping it (no enqueue loop)"
    )


# --------------------------------------------------------------------------- #
# AC-F1.8 (amendment 2026-08-10): the trailing held run is reported, never      #
# silent. Fixtures live here so the frozen AC-F1.6 corpus is untouched.         #
# --------------------------------------------------------------------------- #
def _wholly_unparseable_bytes() -> bytes:
    """Every physical line torn — a format change, a foreign .jsonl under the sweep
    root, tail bit-rot spanning the whole file. Three complete-but-unparseable lines
    plus a partial tail: nothing complete ever follows, so the WHOLE file is one
    trailing held run (``torn`` stays 0 — that is exactly the reporting hole)."""
    return (_torn_midline_bytes("gone-a1") + _torn_midline_bytes("gone-a2")
            + _torn_multibyte_bytes("gone-a3") + _trailing_torn_bytes("gone-a4"))


def _fx_trailing_run(base: Path) -> Path:
    """Good prefix, then a trailing run of THREE torn lines (not one): an
    implementation that reports a bool, or only the last line, dies here."""
    sid = "c-trailrun"
    return _write_fixture(base / "c_trailrun.jsonl",
                          _meta_line() + _user_line(sid, CORPUS_CWD, PRE_TEXT)
                          + _asst_line(sid, CORPUS_CWD, POST_TEXT)
                          + _torn_midline_bytes("run-a1")
                          + _torn_multibyte_bytes("run-a2")
                          + _trailing_torn_bytes("run-a3"))


def _held_log_lines(cfg: Config) -> list[str]:
    if not cfg.log_file.exists():
        return []
    return [ln for ln in cfg.log_file.read_text(encoding="utf-8").splitlines()
            if "held" in ln]


def test_ac_f1_8_wholly_unparseable_transcript_is_never_silent(
        tmp_path, httpserver, monkeypatch):
    """AC-F1.8: a transcript in which EVERY line is torn yields an empty delta and
    ``dropped_empty_delta`` — that part is unchanged and deliberate (no torn-ratio
    escalation). What must NOT happen is silence: ``Delta.held`` must report the
    whole trailing run and the run log must name the held count and the transcript,
    so a human can see the capture was lost. Kills the implementation where the
    hole stays invisible because interior-only ``torn`` is 0 here."""
    shim, capture, shims = _claude_shim(tmp_path)
    monkeypatch.setenv("PATH", f"{shims}{os.pathsep}{os.environ['PATH']}")
    cfg = _mk_cfg(tmp_path, server_url=httpserver.url_for(""), claude_bin=shim)
    repo = kit.make_git_repo(tmp_path / "realrepo")
    sid = "e5-allTorn-0001"
    t = _write_fixture(tmp_path / "sessions" / f"{sid}.jsonl",
                       _wholly_unparseable_bytes())

    d = _tolerant_delta(t, 0, "AC-F1.8")
    assert d.messages == [] and d.new_last_line == 0, (
        "the pointer must not advance over a wholly torn file"
    )
    assert getattr(d, "torn", None) == 0, (
        "AC-F1.8: 'torn' keeps its exact meaning — INTERIOR tears only; nothing "
        "here is interior, which is precisely why 'held' has to exist"
    )
    assert getattr(d, "held", None) == 4, (
        "Delta must expose the additive int field 'held' counting the lines of the "
        f"trailing held run (4 here); got {getattr(d, 'held', None)!r}"
    )

    _hook_marker(cfg, sid, t, repo)
    _wire_server(httpserver)
    report = processor.run_once(cfg)

    assert not list(cfg.queue_dir.glob("*.json")), "the marker must still drain"
    assert _posts(httpserver) == [], "an empty delta must produce no entry"
    held_lines = _held_log_lines(cfg)
    assert held_lines, (
        "AC-F1.8: a wholly unparseable transcript must NOT be silent — the run log "
        f"needs a held-line record; log={cfg.log_file.read_text(encoding='utf-8')!r} "
        f"outcomes={report.outcomes}"
    )
    assert any("4" in ln and str(t) in ln for ln in held_lines), (
        f"the held log line must name the COUNT (4) and the transcript; got {held_lines}"
    )
    # I5/I6: the count and the path escape, never a byte of the torn lines.
    assert _leak_hits(cfg.loom_home, capture, httpserver) == [], (
        "the held-line diagnostic must never carry torn content"
    )


def test_ac_f1_8_held_counts_the_trailing_run_beside_interior_torn(tmp_path):
    """AC-F1.8: on mixed transcripts ``held`` is the trailing run's length and
    ``torn`` stays the interior count — the two are disjoint and both exact.
    Re-asserts the AC-F1.6 agreement (``held`` must not perturb the pointer):
    ``new_last_line + held == total physical lines`` and ``_line_count`` still
    agrees with ``new_last_line``."""
    base = tmp_path / "heldcorpus"

    t = _fx_trailing_run(base)              # 3 good lines, then 3 trailing tears
    d = _tolerant_delta(t, 0, "AC-F1.8")
    assert [m.text for m in d.messages] == [PRE_TEXT, POST_TEXT]
    assert d.new_last_line == 3 and getattr(d, "torn", None) == 0
    assert getattr(d, "held", None) == 3, (
        f"the trailing run holds 3 lines; got {getattr(d, 'held', None)!r}"
    )
    assert sweep._line_count(t) == d.new_last_line, (
        "AC-F1.6 agreement must survive the AC-F1.8 amendment"
    )

    m = _fx_mixed(base)                     # 3 interior tears + 1 trailing tear
    dm = _tolerant_delta(m, 0, "AC-F1.8")
    assert dm.new_last_line == 6 and getattr(dm, "torn", None) == 3, (
        "the frozen AC-F1.6 'mixed' meaning must not change"
    )
    assert getattr(dm, "held", None) == 1, (
        f"one trailing tear is held, not skipped; got {getattr(dm, 'held', None)!r}"
    )
    assert sweep._line_count(m) == dm.new_last_line

    # Idempotence: re-reading from the held pointer re-holds the same run.
    d2 = _tolerant_delta(m, dm.new_last_line, "AC-F1.8")
    assert d2.messages == [] and d2.new_last_line == 6
    assert getattr(d2, "held", None) == 1, (
        "held is a property of the file's tail, not of how far the pointer moved"
    )


def test_ac_f1_8_healthy_transcript_holds_nothing(tmp_path):
    """AC-F1.8: a file that ends on a complete line holds nothing — ``held == 0``
    on both a synthetic healthy transcript and the REAL contract fixture. Kills the
    off-by-one that reports the last complete line as held."""
    t = _fx_healthy(tmp_path / "heldhealthy")
    d = _tolerant_delta(t, 0, "AC-F1.8")
    assert d.new_last_line == 3 and getattr(d, "torn", None) == 0
    assert getattr(d, "held", None) == 0, (
        f"a healthy transcript holds nothing; got {getattr(d, 'held', None)!r}"
    )

    real = read_delta(REAL_SESSION, 0)
    assert getattr(real, "held", None) == 0, (
        "the real-session golden fixture ends on a complete line"
    )
    # And an empty delta at EOF holds nothing either.
    assert getattr(read_delta(REAL_SESSION, real.new_last_line), "held", None) == 0


# =========================================================================== #
# P2.F2 — adapter-failure bounding & diagnostics                               #
# =========================================================================== #
def test_ac_f2_1_adapter_failure_attempts_then_parked_with_diagnostics(
        tmp_path, httpserver, monkeypatch):
    """AC-F2.1: a persistently schema-invalid transcript increments ``attempts``
    each pass and is parked in failed/ at MAX_ATTEMPTS with ``adapter_error``
    intact; ``loom status`` shows it under Parked with the adapter_error tag.
    Kills: increment-but-never-park, park-without-diagnostic, and treating
    schema-invalid lines as skippable tears (the marker would drain instead)."""
    shim, capture, shims = _claude_shim(tmp_path)
    monkeypatch.setenv("PATH", f"{shims}{os.pathsep}{os.environ['PATH']}")
    cfg = _mk_cfg(tmp_path, server_url=httpserver.url_for(""), claude_bin=shim)
    repo = kit.make_git_repo(tmp_path / "realrepo")
    sid = "e5-parked-0001"
    blob = (_meta_line() + _user_line(sid, str(repo), PRE_TEXT)
            + _jline({"type": "user"}))               # schema-invalid: corruption
    t = _write_fixture(tmp_path / "sessions" / f"{sid}.jsonl", blob)
    marker_path = _hook_marker(cfg, sid, t, repo)
    _wire_server(httpserver)

    all_actions: list[str] = []
    for expected_attempts in range(1, processor.MAX_ATTEMPTS):
        report = processor.run_once(cfg)
        all_actions += [o.action for o in report.outcomes]
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        assert marker.get("attempts") == expected_attempts, (
            f"pass {expected_attempts}: adapter failures must increment attempts "
            f"like summarizer failures do (AC-F2.1); marker={marker}"
        )
        assert marker.get("adapter_error"), (
            "the adapter_error diagnostic must persist on the marker each pass"
        )

    report = processor.run_once(cfg)  # pass MAX_ATTEMPTS: park
    all_actions += [o.action for o in report.outcomes]
    assert not marker_path.exists() and not list(cfg.queue_dir.glob("*.json")), (
        f"at MAX_ATTEMPTS={processor.MAX_ATTEMPTS} the marker must leave the queue "
        f"(parked), not retry forever; outcomes={report.outcomes}"
    )
    parked = cfg.failed_dir / f"{sid}.json"
    assert parked.exists(), "the marker must be parked in failed/ (AC-F2.1)"
    parked_marker = json.loads(parked.read_text(encoding="utf-8"))
    assert parked_marker.get("attempts") == processor.MAX_ATTEMPTS
    assert parked_marker.get("adapter_error"), (
        "parking must keep the adapter_error diagnostic intact"
    )
    assert "error" not in all_actions, (
        f"no adapter failure may fall through to the catch-all; actions={all_actions}"
    )

    out = subprocess.run(
        [sys.executable, "-m", "loom.cli", "status"],
        env={**os.environ, "LOOM_HOME": str(cfg.loom_home)},
        cwd=str(kit.REPO_ROOT), capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    sid_lines = [ln for ln in out.stdout.splitlines() if sid in ln]
    assert sid_lines and any("adapter_error" in ln for ln in sid_lines), (
        "loom status must list the parked session with the adapter_error tag; "
        f"stdout:\n{out.stdout}"
    )


def test_ac_f2_2_sweep_skips_failed_parked_session_without_reading_transcript(
        tmp_path, monkeypatch):
    """AC-F2.2: a session whose marker sits in failed/ is never re-enqueued
    (park → re-enqueue → fail → park would be a loop), and the check is CHEAP:
    the parked session's transcript is never even opened (before _line_count).
    A healthy companion orphan in the same pass proves the guard is per-session."""
    cfg = _mk_cfg(tmp_path, sweep_enabled=True)
    repo = kit.make_git_repo(tmp_path / "realrepo")

    sid = "e5-failedpark-0001"
    parked_t = _write_fixture(cfg.sweep_transcripts_root / "d" / f"{sid}.jsonl",
                              _meta_line() + _user_line(sid, str(repo), PRE_TEXT)
                              + _asst_line(sid, str(repo), POST_TEXT))
    kit.age_file(parked_t, 3600)
    cfg.failed_dir.mkdir(parents=True, exist_ok=True)
    (cfg.failed_dir / f"{sid}.json").write_text(json.dumps({
        "session_id": sid, "transcript_path": str(parked_t), "cwd": str(repo),
        "reason": "sweep", "marked_at": kit.utc_now_iso(), "origin": "sweep",
        "attempts": processor.MAX_ATTEMPTS,
        "adapter_error": "line 3 does not parse as JSON",
    }), encoding="utf-8")

    sid2 = "e5-companion-0002"
    companion_t = _write_fixture(cfg.sweep_transcripts_root / "d" / f"{sid2}.jsonl",
                                 _meta_line() + _user_line(sid2, str(repo), PRE_TEXT)
                                 + _asst_line(sid2, str(repo), POST_TEXT))
    kit.age_file(companion_t, 3600)

    opened: list[str] = []
    real_open = Path.open

    def counting_open(self, *args, **kwargs):
        opened.append(str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)
    report = sweep.run_sweep(cfg, {})
    monkeypatch.undo()
    parked_transcript_opens = [p for p in opened if p == str(parked_t)]

    assert sid not in report.enqueued_sessions and not (
        cfg.queue_dir / f"{sid}.json"
    ).exists(), (
        "AC-F2.2: a failed/-parked session must NOT be re-enqueued "
        f"(report={report})"
    )
    assert (cfg.queue_dir / f"{sid2}.json").exists(), (
        "the failed/ guard must be per-session — the healthy companion still enqueues"
    )
    assert parked_transcript_opens == [], (
        "AC-F2.2 cheapness: the parked session must be skipped WITHOUT opening its "
        f"transcript (before _line_count); opens={len(parked_transcript_opens)}"
    )


def test_ac_f2_3_registry_down_keep_path_never_touches_attempts(tmp_path):
    """AC-F2.3 (frozen matrix row): registry unavailable (no cache, server down)
    keeps the marker byte-identical — no attempts, no adapter_error, no pointer."""
    cfg = _mk_cfg(tmp_path, server_url="http://127.0.0.1:9")
    repo = kit.make_git_repo(tmp_path / "realrepo")
    sid = "e5-regdown"
    t = tmp_path / "sessions" / f"{sid}.jsonl"
    kit.write_transcript(t, kit.transcript_lines(str(repo), sid))
    marker_path = _hook_marker(cfg, sid, t, repo)
    original = marker_path.read_bytes()

    for _ in range(2):
        report = processor.run_once(cfg)
        assert any(o.action == "kept_server_unreachable" for o in report.outcomes), (
            f"expected the registry-down keep path; outcomes={report.outcomes}"
        )
        assert marker_path.read_bytes() == original, (
            "AC-F2.3: the registry-down keep path must not rewrite the marker "
            "(attempts stays untouched)"
        )
    assert not cfg.state_path.exists(), "no pointer may be written on a keep path"


def test_ac_f2_3_network_down_post_keep_path_never_touches_attempts(
        tmp_path, monkeypatch):
    """AC-F2.3 (frozen matrix row): server down at POST time (registry cache is
    fresh) keeps the marker byte-identical and the pointer unwritten."""
    shim, capture, shims = _claude_shim(tmp_path)
    monkeypatch.setenv("PATH", f"{shims}{os.pathsep}{os.environ['PATH']}")
    cfg = _mk_cfg(tmp_path, server_url="http://127.0.0.1:9", claude_bin=shim)
    cfg.cache_path.write_text(json.dumps({
        "fetched_at": kit.utc_now_iso(),
        "projects": [{"id": 1, "canonical_remote": kit.TEST_REMOTE_CANONICAL,
                      "name": "phase2-goal", "created_by": "goal",
                      "created_at": "2026-07-01T00:00:00Z"}],
    }), encoding="utf-8")
    repo = kit.make_git_repo(tmp_path / "realrepo")
    sid = "e5-netdown"
    t = tmp_path / "sessions" / f"{sid}.jsonl"
    kit.write_transcript(t, kit.transcript_lines(str(repo), sid))
    marker_path = _hook_marker(cfg, sid, t, repo)
    original = marker_path.read_bytes()

    for _ in range(2):
        report = processor.run_once(cfg)
        assert any(o.action == "kept_network_error" for o in report.outcomes), (
            f"expected the network-down keep path; outcomes={report.outcomes}"
        )
        assert marker_path.read_bytes() == original, (
            "AC-F2.3: the network keep path must not rewrite the marker "
            "(attempts stays untouched)"
        )
    assert not cfg.state_path.exists(), (
        "AC-F2.4/I2: the pointer must not advance without a confirmed push"
    )


def test_ac_f2_4_pointer_never_advances_on_adapter_failure(
        tmp_path, httpserver, monkeypatch):
    """AC-F2.4 (frozen invariant): across every adapter-failure pass — including
    the pass that parks the marker — the pre-existing pointer never moves."""
    shim, capture, shims = _claude_shim(tmp_path)
    monkeypatch.setenv("PATH", f"{shims}{os.pathsep}{os.environ['PATH']}")
    cfg = _mk_cfg(tmp_path, server_url=httpserver.url_for(""), claude_bin=shim)
    repo = kit.make_git_repo(tmp_path / "realrepo")
    sid = "e5-ptrhold"
    blob = (_meta_line() + _user_line(sid, str(repo), PRE_TEXT)
            + _jline({"type": "user"}))               # schema-invalid at line 3
    t = _write_fixture(tmp_path / "sessions" / f"{sid}.jsonl", blob)
    cfg.state_path.write_text(json.dumps({
        str(t): {"last_line": 1, "last_uuid": "seed-u0",
                 "updated_at": kit.utc_now_iso()},
    }), encoding="utf-8")
    _hook_marker(cfg, sid, t, repo)
    _wire_server(httpserver)

    for pass_no in range(1, processor.MAX_ATTEMPTS + 2):
        processor.run_once(cfg)
        state = json.loads(cfg.state_path.read_text(encoding="utf-8"))
        assert state[str(t)]["last_line"] == 1, (
            f"pass {pass_no}: the pointer must NEVER advance on an adapter-failure "
            f"path (AC-F2.4); state={state[str(t)]}"
        )
        assert state[str(t)]["last_uuid"] == "seed-u0"


# =========================================================================== #
# P2.F3 — marker lifecycle integrity (identity-checked drop)                   #
# =========================================================================== #
def test_ac_f3_1_hook_replaced_marker_survives_drop_and_processes_next_pass(
        tmp_path, httpserver, monkeypatch):
    """AC-F3.1: the unlocked hook atomically renames a NEW marker onto the path
    while the processor is summarizing (between _read_marker and _drop_marker).
    The fresh marker must SURVIVE the drop and be processed next pass. Today's
    blind unlink deletes it — a real session capture silently lost."""
    cfg = _mk_cfg(tmp_path, server_url=httpserver.url_for(""))
    repo = kit.make_git_repo(tmp_path / "realrepo")
    sid = "e5-hookswap-0001"
    t = tmp_path / "sessions" / f"{sid}.jsonl"
    kit.write_transcript(t, kit.transcript_lines(str(repo), sid))
    marker_path = _hook_marker(cfg, sid, t, repo)
    fresh_marker = {"session_id": sid, "transcript_path": str(t), "cwd": str(repo),
                    "reason": "prompt_input_exit",
                    "marked_at": "2026-08-05T23:59:59Z", "hook_generation": 2}
    swap_code = (
        f"_mp = {str(marker_path)!r}\n"
        f"_tmp = _mp + '.hookswap'\n"
        f"with open(_tmp, 'w', encoding='utf-8') as _fh:\n"
        f"    _fh.write({json.dumps(fresh_marker)!r})\n"
        f"os.replace(_tmp, _mp)\n"
    )
    shim, capture, shims = _claude_shim(tmp_path, extra=swap_code)
    monkeypatch.setenv("PATH", f"{shims}{os.pathsep}{os.environ['PATH']}")
    cfg.claude_bin = shim
    _wire_server(httpserver)

    report1 = processor.run_once(cfg)
    assert len(_posts(httpserver)) == 1, (
        f"the original marker must still process normally; outcomes={report1.outcomes}"
    )
    assert marker_path.exists(), (
        "AC-F3.1: the hook-replaced marker must SURVIVE the drop — _drop_marker "
        "deleted a marker it never read"
    )
    assert json.loads(marker_path.read_text(encoding="utf-8")) == fresh_marker, (
        "the surviving marker must be the hook's fresh one, byte-for-byte"
    )

    report2 = processor.run_once(cfg)
    assert not list(cfg.queue_dir.glob("*.json")), (
        f"the fresh marker must be processed on the next pass; "
        f"outcomes={report2.outcomes}"
    )
    assert len(_posts(httpserver)) == 1, (
        "the fresh marker covers an already-pointed transcript: no duplicate POST"
    )


def test_ac_f3_2_unchanged_marker_drops_exactly_as_today(
        tmp_path, httpserver, monkeypatch):
    """AC-F3.2 (regression, passes today): an unchanged marker is dropped after a
    confirmed push, the queue drains, the pointer advances — identical to today."""
    shim, capture, shims = _claude_shim(tmp_path)
    monkeypatch.setenv("PATH", f"{shims}{os.pathsep}{os.environ['PATH']}")
    cfg = _mk_cfg(tmp_path, server_url=httpserver.url_for(""), claude_bin=shim)
    repo = kit.make_git_repo(tmp_path / "realrepo")
    sid = "e5-plaindrop"
    t = tmp_path / "sessions" / f"{sid}.jsonl"
    kit.write_transcript(t, kit.transcript_lines(str(repo), sid))
    _hook_marker(cfg, sid, t, repo)
    _wire_server(httpserver)

    report = processor.run_once(cfg)
    assert any(o.action == "posted" for o in report.outcomes), (
        f"outcomes={report.outcomes}"
    )
    assert not list(cfg.queue_dir.glob("*.json")), "the unchanged marker must drop"
    state = json.loads(cfg.state_path.read_text(encoding="utf-8"))
    assert state[str(t)]["last_line"] == 3


def test_ac_f3_3_identity_check_error_degrades_to_unlink_never_crashes(
        tmp_path, httpserver, monkeypatch):
    """AC-F3.3: the identity check must add NO new failure mode. The marker
    VANISHES mid-processing (hook consumed/moved it), so any stat/read inside the
    check errors — the pass must complete and record the push, never crash."""
    cfg = _mk_cfg(tmp_path, server_url=httpserver.url_for(""))
    repo = kit.make_git_repo(tmp_path / "realrepo")
    sid = "e5-vanish-0001"
    t = tmp_path / "sessions" / f"{sid}.jsonl"
    kit.write_transcript(t, kit.transcript_lines(str(repo), sid))
    marker_path = _hook_marker(cfg, sid, t, repo)
    shim, capture, shims = _claude_shim(
        tmp_path, extra=f"os.remove({str(marker_path)!r})\n"
    )
    monkeypatch.setenv("PATH", f"{shims}{os.pathsep}{os.environ['PATH']}")
    cfg.claude_bin = shim
    _wire_server(httpserver)

    report = processor.run_once(cfg)
    assert not any(o.action == "error" for o in report.outcomes), (
        "AC-F3.3: a failing identity check must degrade to today's unlink, never "
        f"crash the pass; outcomes={report.outcomes}"
    )
    assert any(o.action == "posted" for o in report.outcomes), (
        f"the entry must still be recorded; outcomes={report.outcomes}"
    )
    assert not list(cfg.queue_dir.glob("*.json"))
