"""FROZEN GOAL — E10 processor-observability (P3.F5). Read-only to implementers.

Oracle properties: temp LOOM_HOME + byte-level JSONL fixtures on a real filesystem
drive the real ``processor.run_once`` pipeline; the summarizer is a scripted,
RECORDING ``claude`` shim (stdin captured — the leak oracle); the central server is
pytest-httpserver. No loom seam is mocked. Frozen interfaces exercised:
``p4-attention-record`` at ``cfg.state_path.parent / "attention.json"`` and
``p4-tear-escalation-event`` (``tear_ratio_high``, threshold from the additive config
key ``processor.tear_ratio_threshold``, float in (0,1], default 0.5).

Adversarial traps baked in (each kills a plausible-but-wrong implementation):
- BOTH ratio fixtures carry torn=3 — only ``parsed`` differs (9 vs 1) — so an event
  wired to the raw torn count (instead of ``torn/(torn+parsed) >= threshold``) fires
  on both or neither and dies either way;
- a boundary fixture with ratio EXACTLY 0.5: the frozen seam says ``>=``, so an
  implementation comparing with ``>`` stays silent and dies;
- every torn line carries ``ATTN_SECRET``; a full-home (attention.json included) +
  summarizer-stdin + wire sweep proves the bytes reach nothing (I5/I6);
- the wholly-unparseable transcript is re-enqueued MANUALLY across passes (its
  pointer equals its line count, so the sweep never would): an implementation that
  appends a new attention entry per pass — instead of updating one entry keyed by
  session_id — dies on the exact-key-set-of-one assertion;
- after a posted pass the same marker is re-enqueued: the new delta is EMPTY with
  held=0, so an implementation keying the attention record on "empty delta" alone
  (without ``held > 0`` and pointer 0) creates a bogus entry and dies;
- the attention record must exist under a NEVER-FIRING threshold too (the record's
  condition mentions no threshold): an implementation gating the record on the
  event dies in the AC-F5.6 matrix;
- the transcript path in AC-F5.5 contains a DOUBLE space: an implementation storing
  the raw path (not the whitespace-flattened one the existing log lines use) dies;
- AC-F5.6 runs the same three-marker corpus (posted / dropped_empty_delta /
  kept_adapter_error) under threshold=1.0-never-fires vs default-fires and compares
  outcomes, queue, failed/, pointers, attempts and POSTed payload projections —
  any capture-path coupling of the feature dies there.

AC map (auditable coverage — every AC has a named home):
  AC-F5.1  test_ac_f5_1_ratio_above_threshold_fires_once_counts_only
           test_ac_f5_1_ratio_below_threshold_same_torn_count_is_silent
           test_ac_f5_1_boundary_ratio_equal_to_default_threshold_fires
           test_ac_f5_1_threshold_config_key_additive[empty_section]
           test_ac_f5_1_threshold_config_key_additive[low_threshold]
  AC-F5.2  test_ac_f5_2_wholly_unparseable_single_entry_idempotent_across_passes
  AC-F5.3  test_ac_f5_3_heal_removes_entry_on_that_pass[delivers]
           test_ac_f5_3_heal_removes_entry_on_that_pass[no_entry]
  AC-F5.4  test_ac_f5_4_status_surfaces_attention_count
           test_ac_f5_4_status_byte_compatible_with_absent_or_empty_record
  AC-F5.5  test_ac_f5_5_counts_and_flattened_paths_never_content
           (leak sweeps also embedded in the AC-F5.1/F5.2 pipeline tests)
  AC-F5.6  test_ac_f5_6_capture_matrix_identical_with_never_firing_threshold

Expected against pre-E10 code (verified at authoring time, 2026-08-11): the two
regression pins PASS — ...below_threshold...is_silent (no event exists today either)
and ...status_byte_compatible... (today IS today) — plus the additive-default leg
...config_key_additive[empty_section] (default behavior == silence on a low ratio).
Everything else FAILS on new-contract assertions (no tear_ratio_high event, no
attention.json, no status attention row).

Run: .venv/bin/python -m pytest test/waves/g/test_goal_e10_attention.py -q
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _goalkit as kit  # noqa: E402

from loom import processor  # noqa: E402
from loom.config import Config  # noqa: E402
from loom.transcript_adapter import read_delta  # noqa: E402

SUMMARY = "## Decision\nGoal E10: attention-record summary."
# The planted "partial secret" inside every torn line. Matches NO regex family
# (no sk-/password=/@/AKIA shape): surfacing anywhere is a LEAK, never a quarantine.
ATTN_SECRET = "ATTNSECRETMARKER-9e2b7c41d0"
USER_TEXT = "decision: wire the attention record"
ASST_TEXT = "counts only, never transcript content"


# --------------------------------------------------------------------------- #
# Byte-level fixture builders (tears are raw bytes on disk)                    #
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


def _torn_line(uuid: str) -> bytes:
    """Crash-torn mid-line: truncated JSON, newline-terminated, secret inside."""
    return (
        b'{"type":"assistant","uuid":"' + uuid.encode("ascii")
        + b'","message":{"role":"assistant","content":[{"type":"text","text":"'
        + ATTN_SECRET.encode("ascii") + b"\n"
    )


def _write_fixture(path: Path, blob: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return path


def _fx_low_ratio(base: Path, sid: str, cwd: str) -> Path:
    """torn=3, parsed=9 → ratio 3/12 = 0.25 < 0.5. Same torn count as the high
    fixture on purpose: a raw-count trigger cannot tell them apart."""
    blob = _meta_line()
    for n in (1, 2):
        blob += _user_line(sid, cwd, USER_TEXT, n) + _asst_line(sid, cwd, ASST_TEXT, n)
    blob += _torn_line(f"{sid}-t1") + _torn_line(f"{sid}-t2") + _torn_line(f"{sid}-t3")
    for n in (3, 4):
        blob += _user_line(sid, cwd, USER_TEXT, n) + _asst_line(sid, cwd, ASST_TEXT, n)
    blob += _user_line(sid, cwd, USER_TEXT, 5)
    return _write_fixture(base / f"{sid}.jsonl", blob)


def _fx_high_ratio(base: Path, sid: str, cwd: str) -> Path:
    """torn=3, parsed=1 → ratio 3/4 = 0.75 >= 0.5 (default fires; 1.0 never)."""
    blob = (_meta_line() + _torn_line(f"{sid}-t1") + _torn_line(f"{sid}-t2")
            + _torn_line(f"{sid}-t3") + _user_line(sid, cwd, USER_TEXT))
    return _write_fixture(base / f"{sid}.jsonl", blob)


def _fx_boundary(base: Path, sid: str, cwd: str) -> Path:
    """torn=1, parsed=1 → ratio exactly 0.5: the frozen seam says >=, so it fires."""
    blob = _meta_line() + _torn_line(f"{sid}-t1") + _user_line(sid, cwd, USER_TEXT)
    return _write_fixture(base / f"{sid}.jsonl", blob)


def _fx_all_torn(base: Path, sid: str, n_lines: int = 3) -> Path:
    """EVERY line torn (complete-but-unparseable, newline-terminated): empty delta,
    torn=0 (nothing interior), held=n_lines, pointer 0 — the attention condition."""
    blob = b"".join(_torn_line(f"{sid}-g{i}") for i in range(1, n_lines + 1))
    return _write_fixture(base / f"{sid}.jsonl", blob)


def _fx_bad_schema(base: Path, sid: str, cwd: str) -> Path:
    """Parses and decodes but lacks uuid/message: AdapterError (kept_adapter_error)."""
    blob = (_meta_line() + _user_line(sid, cwd, USER_TEXT)
            + _jline({"type": "assistant"}))
    return _write_fixture(base / f"{sid}.jsonl", blob)


# --------------------------------------------------------------------------- #
# Boundary builders                                                            #
# --------------------------------------------------------------------------- #
def _mk_cfg(base: Path, *, server_url: str = "http://127.0.0.1:9",
            claude_bin: str = "claude-bin-never-invoked-e10") -> Config:
    """Temp LOOM_HOME under ``base`` + config.json through the production loader.
    The default claude_bin is a tripwire: any test that never expects a summarizer
    invocation crashes loudly (outcome 'error') if one happens anyway."""
    home = kit.loom_home(base)
    root = base / "transcripts"
    root.mkdir(parents=True, exist_ok=True)
    kit.write_config(home, server_url=server_url, claude_bin=claude_bin,
                     settle_s=1800, transcripts_root=root, sweep_enabled=False,
                     scanner_engine="gitleaks", gitleaks_version=None)
    return Config.load(home)


def _with_processor_section(cfg: Config, section: dict) -> Config:
    """Adds the ADDITIVE ``processor`` config section and reloads through the
    production loader (p2-config-additive-keys: unknown keys never break today)."""
    data = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    data["processor"] = section
    cfg.config_path.write_text(json.dumps(data), encoding="utf-8")
    return Config.load(cfg.loom_home)


def _claude_shim(base: Path, *, stdout: str = SUMMARY) -> tuple[str, Path]:
    """Recording ``claude`` shim: every invocation's stdin is captured (the
    summarizer-stdin leak oracle). Returns (shim_path, capture_dir)."""
    shims = kit.shim_dir(base)
    capture = base / "summarizer_stdin"
    capture.mkdir(exist_ok=True)
    body = f"""
import os, sys, time
data = "" if sys.stdin.isatty() else sys.stdin.read()
cap = {str(capture)!r}
os.makedirs(cap, exist_ok=True)
name = "stdin-%d-%d.txt" % (os.getpid(), time.time_ns())
with open(os.path.join(cap, name), "w", encoding="utf-8") as fh:
    fh.write(data)
sys.stdout.write({stdout!r})
sys.exit(0)
"""
    shim = kit.write_shim(shims, "claude", body)
    return str(shim), capture


def _hook_marker(cfg: Config, sid: str, transcript: Path, cwd) -> Path:
    cfg.queue_dir.mkdir(parents=True, exist_ok=True)
    marker = {"session_id": sid, "transcript_path": str(transcript),
              "cwd": str(cwd), "reason": "prompt_input_exit",
              "marked_at": kit.utc_now_iso()}
    path = cfg.queue_dir / f"{sid}.json"
    path.write_text(json.dumps(marker), encoding="utf-8")
    return path


def _wire_server(httpserver) -> None:
    httpserver.expect_request("/api/projects", method="GET").respond_with_json(
        {"projects": [{"id": 1, "canonical_remote": kit.TEST_REMOTE_CANONICAL,
                       "name": "e10-goal", "created_by": "goal",
                       "created_at": "2026-07-01T00:00:00Z"}]}
    )
    httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201
    )


def _posts(httpserver) -> list[dict]:
    return [r.get_json() for r, _ in httpserver.log
            if r.method == "POST" and r.path == "/api/entries"]


def _log_text(cfg: Config) -> str:
    return cfg.log_file.read_text(encoding="utf-8") if cfg.log_file.exists() else ""


def _event_lines(cfg: Config) -> list[str]:
    """Run-log lines whose ACTION field is the frozen event name. The log format is
    ``ts\\tsession\\taction\\tdetail`` — the event must be a distinct action, not a
    substring smuggled into another line's detail."""
    out = []
    for line in _log_text(cfg).splitlines():
        fields = line.split("\t")
        if len(fields) >= 3 and fields[2] == "tear_ratio_high":
            out.append(line)
    return out


def _attn_path(cfg: Config) -> Path:
    # Frozen seam p4-attention-record: <state.json's directory>/attention.json.
    return cfg.state_path.parent / "attention.json"


def _load_attention(cfg: Config) -> dict | None:
    p = _attn_path(cfg)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _flat(path) -> str:
    """The whitespace-flattening the existing log lines apply to paths."""
    return " ".join(str(path).split())


def _assert_iso_z(ts, what: str) -> datetime:
    assert isinstance(ts, str) and ts.endswith("Z"), (
        f"{what} must be an ISO-Z timestamp string; got {ts!r}"
    )
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _assert_entry(entry: dict, transcript: Path, held: int) -> None:
    """Superset-tolerant schema check on one attention entry (frozen fields only —
    NEVER the exact key set: the record may grow additively)."""
    assert isinstance(entry, dict), f"attention entry must be an object; got {entry!r}"
    required = {"transcript_path", "held", "first_seen", "last_seen"}
    assert set(entry) >= required, (
        f"attention entry must carry at least {sorted(required)}; got {sorted(entry)}"
    )
    assert entry["transcript_path"] == _flat(transcript), (
        "transcript_path must be the whitespace-flattened path "
        f"({_flat(transcript)!r}); got {entry['transcript_path']!r}"
    )
    assert isinstance(entry["held"], int) and entry["held"] == held, (
        f"held must be the int trailing-held count ({held}); got {entry['held']!r}"
    )
    _assert_iso_z(entry["first_seen"], "first_seen")
    _assert_iso_z(entry["last_seen"], "last_seen")


def _leak_hits(home: Path, capture: Path | None, httpserver) -> list[str]:
    """Everywhere the torn bytes could leak: the whole LOOM_HOME tree (log/, state,
    attention.json, queue/, failed/, quarantine/), every recorded summarizer stdin,
    and everything that went over the wire."""
    needle = ATTN_SECRET.encode("utf-8")
    hits: list[str] = []
    bases = [home] + ([capture] if capture is not None else [])
    for base in bases:
        for f in sorted(p for p in base.rglob("*") if p.is_file()):
            if needle in f.read_bytes():
                hits.append(str(f))
    for req, _resp in httpserver.log:
        if needle in (req.get_data() or b""):
            hits.append(f"HTTP {req.method} {req.path}")
    return hits


def _run_status(home: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "LOOM_HOME": str(home)}
    env["PYTHONPATH"] = (
        f"{kit.REPO_ROOT / 'client'}{os.pathsep}{env.get('PYTHONPATH', '')}"
    )
    return subprocess.run(
        [sys.executable, "-m", "loom.cli", "status"],
        env=env, cwd=str(kit.REPO_ROOT), capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=60,
    )


# =========================================================================== #
# AC-F5.1 — tear_ratio_high: ratio-triggered, counts-only, once per marker     #
# =========================================================================== #
def test_ac_f5_1_ratio_above_threshold_fires_once_counts_only(tmp_path, httpserver):
    """AC-F5.1: torn=3, parsed=1 (ratio 0.75, default threshold 0.5, key absent)
    emits EXACTLY ONE ``tear_ratio_high`` per processed marker, carrying counts
    only (torn=, parsed=, ratio=) — no transcript bytes, no message text. A
    re-enqueued marker over the unchanged transcript (empty delta, held=0) emits
    nothing more and must NOT create an attention entry."""
    shim, capture = _claude_shim(tmp_path)
    cfg = _mk_cfg(tmp_path, server_url=httpserver.url_for(""), claude_bin=shim)
    repo = kit.make_git_repo(tmp_path / "realrepo")
    sid = "e10-hi-0001"
    t = _fx_high_ratio(tmp_path / "sessions", sid, str(repo))

    d = read_delta(t, 0)  # fixture sanity, anchored to the real adapter
    assert (d.torn, len(d.messages), d.new_last_line, d.held) == (3, 1, 5, 0)

    _hook_marker(cfg, sid, t, repo)
    _wire_server(httpserver)
    report = processor.run_once(cfg)

    assert any(o.action == "posted" for o in report.outcomes), (
        f"fixture sanity: the high-ratio session must deliver; outcomes={report.outcomes}"
    )
    events = _event_lines(cfg)
    assert len(events) == 1, (
        "AC-F5.1: a processed delta with ratio 0.75 >= 0.5 must emit exactly one "
        f"tear_ratio_high run-log event; got {events!r}; log={_log_text(cfg)!r}"
    )
    ev = events[0]
    assert ev.split("\t")[1] == sid, f"the event must name the session; got {ev!r}"
    assert "torn=3" in ev and "parsed=1" in ev and "ratio=" in ev, (
        f"the event carries counts only, as torn=/parsed=/ratio= fields; got {ev!r}"
    )
    assert USER_TEXT not in ev and ATTN_SECRET not in ev, (
        f"counts only — never message text or torn bytes; got {ev!r}"
    )
    assert _leak_hits(cfg.loom_home, capture, httpserver) == [], (
        "torn-line bytes leaked out of the transcript (AC-F5.5/I5-I6)"
    )

    # Re-enqueue over the unchanged transcript: pointer at EOF ⇒ empty delta with
    # held=0 ⇒ no second event, and NO attention entry (empty delta alone is not
    # the wholly-unparseable condition).
    _hook_marker(cfg, sid, t, repo)
    report2 = processor.run_once(cfg)
    assert any(o.action == "dropped_empty_delta" for o in report2.outcomes), (
        f"fixture sanity: the re-run must be an empty delta; outcomes={report2.outcomes}"
    )
    assert len(_event_lines(cfg)) == 1, (
        "AC-F5.1: at most once per processed marker — an empty re-read delta "
        "(torn=0) must not re-emit the event"
    )
    rec = _load_attention(cfg) or {}
    assert sid not in rec, (
        "an empty delta with held=0 must NOT create an attention entry "
        f"(condition is empty delta AND held>0 AND pointer 0); record={rec}"
    )


def test_ac_f5_1_ratio_below_threshold_same_torn_count_is_silent(
        tmp_path, httpserver):
    """AC-F5.1 (regression pin — passes today): torn=3, parsed=9 (ratio 0.25) emits
    NOTHING new. Same torn count as the firing fixture: an event wired to raw torn
    counts instead of the ratio fires here and dies."""
    shim, capture = _claude_shim(tmp_path)
    cfg = _mk_cfg(tmp_path, server_url=httpserver.url_for(""), claude_bin=shim)
    repo = kit.make_git_repo(tmp_path / "realrepo")
    sid = "e10-lo-0001"
    t = _fx_low_ratio(tmp_path / "sessions", sid, str(repo))

    d = read_delta(t, 0)
    assert (d.torn, len(d.messages), d.new_last_line, d.held) == (3, 9, 13, 0)

    _hook_marker(cfg, sid, t, repo)
    _wire_server(httpserver)
    report = processor.run_once(cfg)

    assert any(o.action == "posted" for o in report.outcomes), (
        f"fixture sanity: the low-ratio session must deliver; outcomes={report.outcomes}"
    )
    assert _event_lines(cfg) == [], (
        "AC-F5.1: ratio 3/12 = 0.25 < 0.5 — same torn count as the firing fixture; "
        "an implementation triggering on raw torn counts (not the ratio) dies here; "
        f"log={_log_text(cfg)!r}"
    )
    assert _leak_hits(cfg.loom_home, capture, httpserver) == []


def test_ac_f5_1_boundary_ratio_equal_to_default_threshold_fires(
        tmp_path, httpserver):
    """AC-F5.1 boundary: torn=1, parsed=1 → ratio exactly 0.5. The frozen seam is
    ``torn / (torn + parsed) >= threshold`` — a ``>`` comparison stays silent and
    dies. Also pins ``parsed`` = conversational messages (skipped metadata lines
    are not parsed messages: counting the meta line would give 1/3 < 0.5)."""
    shim, capture = _claude_shim(tmp_path)
    cfg = _mk_cfg(tmp_path, server_url=httpserver.url_for(""), claude_bin=shim)
    repo = kit.make_git_repo(tmp_path / "realrepo")
    sid = "e10-eq-0001"
    t = _fx_boundary(tmp_path / "sessions", sid, str(repo))

    d = read_delta(t, 0)
    assert (d.torn, len(d.messages), d.held) == (1, 1, 0)

    _hook_marker(cfg, sid, t, repo)
    _wire_server(httpserver)
    report = processor.run_once(cfg)

    assert any(o.action == "posted" for o in report.outcomes), (
        f"fixture sanity; outcomes={report.outcomes}"
    )
    events = _event_lines(cfg)
    assert len(events) == 1, (
        "AC-F5.1: ratio 0.5 EQUALS the default threshold 0.5 and the frozen seam "
        f"says >= — the event must fire; log={_log_text(cfg)!r}"
    )
    assert "torn=1" in events[0] and "parsed=1" in events[0]


@pytest.mark.parametrize(
    ("case", "section", "fires"),
    [
        ("empty_section", {}, False),
        ("low_threshold", {"tear_ratio_threshold": 0.1}, True),
    ],
    ids=["empty_section", "low_threshold"],
)
def test_ac_f5_1_threshold_config_key_additive(tmp_path, httpserver, case,
                                               section, fires):
    """AC-F5.1 + p2-config-additive-keys, on the LOW fixture (ratio 0.25):
    - an empty ``processor`` section behaves exactly like the absent key (default
      0.5 → silence) — additive keys preserve prior behavior;
    - ``tear_ratio_threshold: 0.1`` makes the same fixture fire — kills a
      hardcoded default that never reads the config key."""
    shim, capture = _claude_shim(tmp_path)
    cfg = _mk_cfg(tmp_path, server_url=httpserver.url_for(""), claude_bin=shim)
    cfg = _with_processor_section(cfg, section)
    repo = kit.make_git_repo(tmp_path / "realrepo")
    sid = f"e10-cfg-{case}"
    t = _fx_low_ratio(tmp_path / "sessions", sid, str(repo))
    _hook_marker(cfg, sid, t, repo)
    _wire_server(httpserver)

    report = processor.run_once(cfg)

    assert any(o.action == "posted" for o in report.outcomes), (
        f"fixture sanity: capture semantics never change; outcomes={report.outcomes}"
    )
    events = _event_lines(cfg)
    if fires:
        assert len(events) == 1, (
            "processor.tear_ratio_threshold=0.1 must make ratio 0.25 fire — the "
            f"threshold comes from the config key, not a constant; log={_log_text(cfg)!r}"
        )
        assert "torn=3" in events[0] and "parsed=9" in events[0]
    else:
        assert events == [], (
            "an empty 'processor' config section must behave as the absent key "
            f"(default 0.5; ratio 0.25 stays silent); got {events!r}"
        )


# =========================================================================== #
# AC-F5.2 — attention record: one entry, updated in place, never duplicated    #
# =========================================================================== #
def test_ac_f5_2_wholly_unparseable_single_entry_idempotent_across_passes(
        tmp_path, httpserver):
    """AC-F5.2: a wholly-unparseable transcript (empty delta, held>0, pointer 0)
    creates ONE attention entry keyed by session_id at
    ``state.json``'s directory / attention.json. A second processed pass over the
    (grown) file UPDATES ``last_seen``/``held`` in place — same single key, no
    duplicate. The marker is re-enqueued manually: pointer == line_count, so the
    sweep never would (an implementation appending entries per pass dies here)."""
    cfg = _mk_cfg(tmp_path, server_url=httpserver.url_for(""))
    repo = kit.make_git_repo(tmp_path / "realrepo")
    sid = "e10-attn-0001"
    t = _fx_all_torn(tmp_path / "sessions", sid, n_lines=3)

    d = read_delta(t, 0)  # fixture sanity: the exact attention condition
    assert (d.messages, d.new_last_line, d.torn, d.held) == ([], 0, 0, 3)

    _hook_marker(cfg, sid, t, repo)
    _wire_server(httpserver)
    report1 = processor.run_once(cfg)
    assert any(o.action == "dropped_empty_delta" for o in report1.outcomes), (
        f"fixture sanity: capture semantics unchanged; outcomes={report1.outcomes}"
    )

    rec1 = _load_attention(cfg)
    assert rec1 is not None, (
        "AC-F5.2: a processed wholly-unparseable transcript must create the "
        f"attention record at {_attn_path(cfg)} (p4-attention-record)"
    )
    assert list(rec1) == [sid], (
        f"exactly ONE entry keyed by session_id; got keys {list(rec1)}"
    )
    _assert_entry(rec1[sid], t, held=3)
    first1 = _assert_iso_z(rec1[sid]["first_seen"], "first_seen")
    last1 = _assert_iso_z(rec1[sid]["last_seen"], "last_seen")
    assert last1 >= first1

    # The file grows another torn line (held 3 → 4) and the marker is re-enqueued.
    with t.open("ab") as fh:
        fh.write(_torn_line(f"{sid}-g4"))
    time.sleep(1.05)  # so last_seen provably advances even at 1s clock resolution
    _hook_marker(cfg, sid, t, repo)
    report2 = processor.run_once(cfg)
    assert any(o.action == "dropped_empty_delta" for o in report2.outcomes), (
        f"outcomes={report2.outcomes}"
    )

    rec2 = _load_attention(cfg)
    assert rec2 is not None and list(rec2) == [sid], (
        "AC-F5.2: repeated passes must UPDATE the single entry in place, never "
        f"duplicate it; got keys {list(rec2 or {})}"
    )
    _assert_entry(rec2[sid], t, held=4)
    assert rec2[sid]["first_seen"] == rec1[sid]["first_seen"], (
        "first_seen is when the condition was first observed — it must not move"
    )
    last2 = _assert_iso_z(rec2[sid]["last_seen"], "last_seen")
    assert last2 > last1, (
        f"last_seen must advance on the updating pass; {last1} -> {last2}"
    )
    assert _leak_hits(cfg.loom_home, None, httpserver) == [], (
        "the attention record holds counts and paths only — never torn bytes"
    )


# =========================================================================== #
# AC-F5.3 — the entry is removed on the pass where the condition heals         #
# =========================================================================== #
@pytest.mark.parametrize(
    ("shim_stdout", "healed_action"),
    [(SUMMARY, "posted"), ("NO_ENTRY", "no_entry")],
    ids=["delivers", "no_entry"],
)
def test_ac_f5_3_heal_removes_entry_on_that_pass(tmp_path, httpserver,
                                                 shim_stdout, healed_action):
    """AC-F5.3: once the transcript parses again (the writer finishes: valid lines
    appended after the torn run), the attention entry is REMOVED on that processing
    pass — both when the delta delivers (posted) and when it resolves without a
    POST (NO_ENTRY): the healing condition is 'delta parses or delivers'."""
    shim, capture = _claude_shim(tmp_path, stdout=shim_stdout)
    cfg = _mk_cfg(tmp_path, server_url=httpserver.url_for(""), claude_bin=shim)
    repo = kit.make_git_repo(tmp_path / "realrepo")
    sid = f"e10-heal-{healed_action}"
    t = _fx_all_torn(tmp_path / "sessions", sid, n_lines=3)
    _hook_marker(cfg, sid, t, repo)
    _wire_server(httpserver)

    processor.run_once(cfg)
    rec1 = _load_attention(cfg)
    assert rec1 is not None and sid in rec1, (
        f"precondition (AC-F5.2): the unhealed pass must create the entry; got {rec1}"
    )

    # The tail heals: complete conversational lines land after the torn run.
    with t.open("ab") as fh:
        fh.write(_user_line(sid, str(repo), USER_TEXT)
                 + _asst_line(sid, str(repo), ASST_TEXT))
    _hook_marker(cfg, sid, t, repo)
    report2 = processor.run_once(cfg)

    assert any(o.action == healed_action for o in report2.outcomes), (
        f"fixture sanity: the healed pass must end as {healed_action}; "
        f"outcomes={report2.outcomes}"
    )
    rec2 = _load_attention(cfg) or {}
    assert sid not in rec2, (
        "AC-F5.3: the attention entry must be removed ON the pass where the "
        f"transcript parses again ({healed_action}); record={rec2}"
    )


# =========================================================================== #
# AC-F5.4 — loom status: attention count; absent/empty record == today         #
# =========================================================================== #
def test_ac_f5_4_status_surfaces_attention_count(tmp_path):
    """AC-F5.4: with two attention entries on disk (frozen record schema, plus an
    additive extra field an implementation must tolerate), ``loom status`` renders
    an attention summary row carrying the count 2."""
    cfg = _mk_cfg(tmp_path, claude_bin="/nonexistent/claude-e10")
    now = kit.utc_now_iso()
    _attn_path(cfg).write_text(json.dumps({
        "e10-s1": {"transcript_path": "/tmp/e10/s1.jsonl", "held": 3,
                   "first_seen": now, "last_seen": now},
        "e10-s2": {"transcript_path": "/tmp/e10/s2.jsonl", "held": 7,
                   "first_seen": now, "last_seen": now, "extra_additive": True},
    }), encoding="utf-8")

    out = _run_status(cfg.loom_home)
    assert out.returncode == 0, out.stderr
    assert re.search(r"attention\D*2", out.stdout, re.IGNORECASE), (
        "AC-F5.4: loom status must surface the attention count (2 entries) as an "
        f"'attention: N' summary row; stdout:\n{out.stdout}"
    )


def test_ac_f5_4_status_byte_compatible_with_absent_or_empty_record(tmp_path):
    """AC-F5.4 (regression pin — passes today): with NO attention.json, and again
    with an EMPTY record, ``loom status`` output is byte-identical between the two
    runs and mentions no attention row at all — i.e. byte-compatible with today.
    Kills the unconditional 'attention: 0' row."""
    cfg = _mk_cfg(tmp_path, claude_bin="/nonexistent/claude-e10")

    absent = _run_status(cfg.loom_home)
    assert absent.returncode == 0, absent.stderr

    _attn_path(cfg).write_text("{}", encoding="utf-8")
    empty = _run_status(cfg.loom_home)
    assert empty.returncode == 0, empty.stderr

    assert absent.stdout == empty.stdout, (
        "AC-F5.4: an EMPTY attention record must render byte-identically to the "
        f"ABSENT one;\n--- absent ---\n{absent.stdout}\n--- empty ---\n{empty.stdout}"
    )
    for label, out in (("absent", absent), ("empty", empty)):
        assert "attention" not in out.stdout.lower(), (
            f"AC-F5.4: with the record {label} the status output is today's — no "
            f"attention row may appear; stdout:\n{out.stdout}"
        )


# =========================================================================== #
# AC-F5.5 — counts and flattened paths only; never a byte of content           #
# =========================================================================== #
def test_ac_f5_5_counts_and_flattened_paths_never_content(tmp_path, httpserver):
    """AC-F5.5: the transcript lives under a directory with a DOUBLE space in its
    name. After the pass that creates its attention entry: the record stores the
    whitespace-FLATTENED path (like the existing log lines), the raw double-space
    path appears in neither the record nor the log, and the planted secret in the
    torn lines appears nowhere under LOOM_HOME nor on the wire."""
    cfg = _mk_cfg(tmp_path, server_url=httpserver.url_for(""))
    repo = kit.make_git_repo(tmp_path / "realrepo")
    sid = "e10-flat-0001"
    t = _fx_all_torn(tmp_path / "se  ss" / "sessions", sid, n_lines=3)
    raw = str(t)
    assert "  " in raw  # fixture sanity: flattening is observable
    _hook_marker(cfg, sid, t, repo)
    _wire_server(httpserver)

    processor.run_once(cfg)

    rec = _load_attention(cfg)
    assert rec is not None and sid in rec, (
        f"precondition (AC-F5.2): the entry must exist; record={rec}"
    )
    _assert_entry(rec[sid], t, held=3)
    attn_text = _attn_path(cfg).read_text(encoding="utf-8")
    assert raw not in attn_text, (
        "AC-F5.5: the record must store the flattened path, never the raw one"
    )
    assert ATTN_SECRET not in attn_text, (
        "AC-F5.5: the record holds counts and paths only — never transcript bytes"
    )
    log = _log_text(cfg)
    assert raw not in log, (
        "AC-F5.5: paths in the run log are whitespace-flattened (existing policy) "
        f"— the raw double-space path may not appear; log={log!r}"
    )
    assert _leak_hits(cfg.loom_home, None, httpserver) == [], (
        "torn-line bytes (partial secret) leaked (I5/I6)"
    )


# =========================================================================== #
# AC-F5.6 — REPORT-ONLY: capture behavior identical, firing vs never-firing    #
# =========================================================================== #
_MX_HI = "e10-mx-post"        # posted (ratio 0.75: fires under 0.5, silent under 1.0)
_MX_AT = "e10-mx-alltorn"     # dropped_empty_delta + attention entry (both runs)
_MX_BAD = "e10-mx-badschema"  # kept_adapter_error, attempts=1, marker stays queued


def _matrix_home(base: Path, httpserver) -> tuple[Config, dict[str, Path]]:
    """One complete three-marker corpus under its own LOOM_HOME. Same session ids
    and same byte content in every home, so every deterministic artifact —
    content_hash included — must come out identical."""
    shim, _capture = _claude_shim(base)
    cfg = _mk_cfg(base, server_url=httpserver.url_for(""), claude_bin=shim)
    repo = kit.make_git_repo(base / "realrepo")
    paths = {
        _MX_HI: _fx_high_ratio(base / "sessions", _MX_HI, str(repo)),
        _MX_AT: _fx_all_torn(base / "sessions", _MX_AT, n_lines=3),
        _MX_BAD: _fx_bad_schema(base / "sessions", _MX_BAD, str(repo)),
    }
    for sid, t in paths.items():
        _hook_marker(cfg, sid, t, repo)
    return cfg, paths


def _terminal_state(cfg: Config, paths: dict[str, Path], report) -> dict:
    """The capture-relevant terminal state, projected onto home-independent keys."""
    state = (json.loads(cfg.state_path.read_text(encoding="utf-8"))
             if cfg.state_path.exists() else {})
    pointers = {}
    for sid, t in paths.items():
        entry = state.get(str(t))
        pointers[sid] = (None if entry is None
                         else (entry.get("last_line"), entry.get("last_uuid")))
    queued = {}
    for p in sorted(cfg.queue_dir.glob("*.json")):
        m = json.loads(p.read_text(encoding="utf-8"))
        queued[p.name] = (m.get("attempts"), bool(m.get("adapter_error")))
    return {
        "outcomes": sorted((o.session_id, o.action) for o in report.outcomes),
        "pointers": pointers,
        "queued": queued,
        "failed": sorted(p.name for p in cfg.failed_dir.glob("*.json")),
        "quarantine": sorted(p.name for p in cfg.quarantine_dir.glob("*.json")),
    }


def test_ac_f5_6_capture_matrix_identical_with_never_firing_threshold(
        tmp_path, httpserver):
    """AC-F5.6: the SAME three-marker corpus (posted / dropped_empty_delta /
    kept_adapter_error) runs under threshold=1.0 (ratio 0.75 < 1.0 — the event
    never fires; 1.0 is the top of the frozen (0,1] domain) and under the firing
    default. Everything capture-shaped must be identical: outcomes, pointers,
    queue contents and attempts, failed/, quarantine/, and the POSTed payload
    projection (content_hash included). Only the run-log event differs — and the
    attention record must exist in BOTH runs (its condition names no threshold)."""
    _wire_server(httpserver)

    cfg_a, paths_a = _matrix_home(tmp_path / "A", httpserver)
    cfg_a = _with_processor_section(cfg_a, {"tear_ratio_threshold": 1.0})
    report_a = processor.run_once(cfg_a)
    posts_a = _posts(httpserver)

    cfg_b, paths_b = _matrix_home(tmp_path / "B", httpserver)
    report_b = processor.run_once(cfg_b)
    posts_b = _posts(httpserver)[len(posts_a):]

    term_a = _terminal_state(cfg_a, paths_a, report_a)
    term_b = _terminal_state(cfg_b, paths_b, report_b)
    assert term_a == term_b, (
        "AC-F5.6: capture behavior must be byte-identical with the feature firing "
        f"vs never-firing;\nA(threshold=1.0)={term_a}\nB(default)={term_b}"
    )

    def proj(posts):
        return sorted((p["session_id"], p["covered_from"], p["covered_to"],
                       p["content_hash"]) for p in posts)

    assert proj(posts_a) == proj(posts_b) and len(posts_a) == 1, (
        "AC-F5.6: what reaches the server must be identical (same session, same "
        f"range, same content_hash);\nA={proj(posts_a)}\nB={proj(posts_b)}"
    )

    for label, cfg in (("A(threshold=1.0)", cfg_a), ("B(default)", cfg_b)):
        rec = _load_attention(cfg)
        assert rec is not None and _MX_AT in rec, (
            "the attention record's condition names NO threshold — the entry must "
            f"exist in run {label} too; record={rec}"
        )

    assert _event_lines(cfg_a) == [], (
        "threshold=1.0 with ratio 0.75 must never fire (frozen domain (0,1]); "
        f"log={_log_text(cfg_a)!r}"
    )
    events_b = _event_lines(cfg_b)
    assert len(events_b) == 1 and events_b[0].split("\t")[1] == _MX_HI, (
        "fixture sanity + AC-F5.1: the default run fires exactly once, on the "
        f"high-ratio marker; got {events_b!r}"
    )
