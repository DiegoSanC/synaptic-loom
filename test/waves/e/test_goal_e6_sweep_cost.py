"""FROZEN GOAL — E6 sweep-economics (P2.F4, AC-F4.1..F4.6). Read-only to implementers.

Oracle properties: real tmp filesystem (temp LOOM_HOME + real transcript tree) driving
the REAL ``run_sweep`` / ``processor.run_once`` flow; the summarizer and the central
server (not owned by this wave) are a stub binary and pytest-httpserver. File-open
instrumentation reuses the wave-c gate-probe technique (patched ``Path.open`` +
``builtins.open`` + ``io.open``, dedup by path) that measured the 225.4 MiB/pass /
~21 GiB/day steady-state read cost (the wave-c build review, §2).

Adversarial design — the wrong implementations this goal is built to break:

- POST-read stat capture (the hole reintroduced): the AC-F4.2 test makes the append
  race REAL by wrapping ``read_delta``: the wrapped call performs the real read, then
  appends one line to the transcript before returning — i.e. the append lands after
  the bytes were read and BEFORE any post-read code (stat capture, summarizer, pointer
  persist) runs. That is exactly the SIGKILL victim of the wave-a build review. A pre-read
  stat is stale (recorded == the tree BEFORE the append) → mismatch → re-read → the
  appended line lands. ANY post-read capture records the appended file → skip forever
  → the POST-count and re-sweep assertions fail. The state-entry assertions pin the
  recorded pair to the byte-exact PRE-append stat, so a post-read capture also dies
  at the data level with a precise diff.
- ``mtime <= updated_at`` (the naive design wave-a warned about): in the race test the
  append happens BEFORE the pointer write, so mtime < updated_at and the naive skip
  eats the lost line; in AC-F4.3/AC-F4.4/compose-B the appended file is ``os.utime``-d
  into the past (older than ``updated_at``) — the naive skip fires, the enqueue
  assertions fail.
- float/int-second mtime comparison: AC-F4.2's granularity test crafts a recorded
  ``stat_mtime_ns`` in the SAME second as (and float-colliding with) the file's
  current mtime, same size, pointer behind EOF — only an ``st_mtime_ns`` integer
  comparison sees the mismatch and re-reads.
- skip-check hoisted above the marker/settle/failed guards: AC-F4.6 tests pin that
  markered, fresh and failed/-parked transcripts are never even OPENED.

Expected against TODAY's (pre-E5/pre-E6) tree: AC-F4.1 (zero-read), AC-F4.2
(stat fields), the failed/ guard (E5, merges in wave d) and compose-A (stat skip)
FAIL for the right reasons; all regression-direction tests PASS.

Amendment 2026-08-10 (P2.F4, wave-e review — purely additive, nothing above changed).
Two owner-approved strengthenings:

(1) The recorded stat grows the INODE half. ``(st_mtime_ns, st_size)`` alone calls a
    same-size replacement that preserves mtime — ``cp -p``, ``rsync -a``, ``tar -xp``,
    a Time-Machine restore, any ``os.replace`` followed by ``utimensat`` — "unchanged",
    and skips it permanently: unrecoverable silent content loss on the transcript, while
    ``processor._identity_of`` already spends ``(st_dev, st_ino, …)`` to guard a
    *recoverable* marker. ``_advance_pointer`` now also records additive ``stat_dev`` /
    ``stat_ino``, and ``sweep._stat_unchanged`` compares all four WHEN THE ENTRY CARRIES
    THEM. The comparison is deliberately GRACEFUL, not strict: an entry written before
    the amendment (or by an older client) carries neither field and keeps exactly
    today's two-field comparison. That fallback is load-bearing, not politeness — the
    two hand-crafted pre-amendment entries below
    (``test_compose_e5_trailing_hold_unchanged_file_is_skipped_without_reads`` and
    ``test_compose_e5_new_content_after_tear_defeats_the_skip``) declare only
    ``stat_mtime_ns``/``stat_size`` on purpose and must keep asserting exactly what they
    assert today. A half-carrying entry (one field of the pair, hand-edited or
    half-migrated) resolves to the full check, like every other ambiguity here. Net
    effect: monotonically stronger than the pre-amendment behaviour, never weaker, and
    the failure direction stays "one extra read", never "an improper skip".
(2) Observability WITHOUT touching the frozen seam. ``SweepReport`` and
    ``sweep_last_run.json`` are unchanged (AC-F4.5 still pins the four-key schema
    byte-identically, and E8 consumes it): the number of stat-skips in a pass is emitted
    as an additive ``stat_skips=N`` column on the existing ``sweep_pass`` run-log event.
    Counts only — no paths beyond what the sweep already logs, never content (I5/I6).
    Purpose: on the pilot host a one-syscall stat-skip and a full-file-read-then-skip are
    otherwise indistinguishable inside ``skipped``.

AC map for the amendment (additive tests only — nothing above was modified):
  AC-F4.2  test_p2f4_amendment_pointer_records_all_four_stat_fields_pre_read
           test_p2f4_amendment_uncorroborated_read_records_none_of_the_four_fields
           test_p2f4_amendment_replaced_inode_same_mtime_and_size_is_never_skipped
  AC-F4.1  test_p2f4_amendment_matching_four_field_entry_skips_without_reading
  AC-F4.4  test_p2f4_amendment_legacy_two_field_entry_still_skips
  AC-F4.5  test_p2f4_amendment_stat_skips_are_counted_in_the_run_log_not_the_record

Run: .venv/bin/python -m pytest test/waves/e/test_goal_e6_sweep_cost.py -q
"""
from __future__ import annotations

import builtins
import inspect
import io
import json
import os
import pathlib
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _goalkit as kit  # noqa: E402

from loom import processor, sweep, transcript_adapter  # noqa: E402
from loom.config import Config  # noqa: E402

# Unique needle for the raced append; deliberately matches NO secret-scan pattern.
RACE_TOKEN = "E6-RACE-APPENDED-CONTENT-9f3a2b"
CALL_SEP = "\n===LOOM-SUMMARIZER-CALL===\n"
# The exact phase-1 state-entry keys: stripping to these simulates a pre-upgrade file.
PRE_UPGRADE_KEYS = ("last_line", "last_uuid", "updated_at")
# Frozen additive field names (p3-state-stat-fields, the phase-2 build manifest, P2.F4).
STAT_FIELDS = ("stat_mtime_ns", "stat_size")
# P2.F4 amendment (2026-08-10): the inode half of the recorded stat, additive on top of
# STAT_FIELDS. Entries that carry BOTH are compared on all four; entries that carry
# NEITHER keep the legacy two-field comparison (see the amendment note in the docstring).
STAT_IDENTITY_FIELDS = ("stat_dev", "stat_ino")


# --------------------------------------------------------------------------- #
# Harness                                                                      #
# --------------------------------------------------------------------------- #
def _mk_cfg(tmp_path, *, settle_s: int = 1800,
            server_url: str = "http://127.0.0.1:9", claude_bin: str = "claude") -> Config:
    home = kit.loom_home(tmp_path)
    root = tmp_path / "transcripts"
    root.mkdir(exist_ok=True)
    return Config(
        loom_home=home, server_url=server_url, token="goal-token",
        claude_bin=claude_bin, prompt_path=home / "prompts" / "summarizer_v1.md",
        sweep_enabled=True, sweep_settle_s=settle_s, sweep_transcripts_root=root,
        scanner_engine="gitleaks",
    )


def _settled_orphan(cfg: Config, cwd: str, sid: str, *, aged: float = 7200) -> Path:
    path = cfg.sweep_transcripts_root / "-Users-e6-proj" / f"{sid}.jsonl"
    kit.write_transcript(path, kit.transcript_lines(cwd, sid))
    kit.age_file(path, aged)
    return path


def _assistant_line(sid: str, n: int, text: str, cwd: str) -> dict:
    return {"type": "assistant", "uuid": f"{sid}-a{n}", "cwd": cwd, "sessionId": sid,
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": text}]}}


def _append_line(path: Path, obj: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj) + "\n")


def _wire_server(httpserver, *, canonical: str = kit.TEST_REMOTE_CANONICAL):
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


def _recording_claude(path: Path, record: Path, stdout: str) -> str:
    """Claude stub that RECORDS each stdin (so the goal can prove which delta was
    actually read and summarized) and prints ``stdout``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        "rec = pathlib.Path(" + repr(str(record)) + ")\n"
        "data = sys.stdin.read()\n"
        "with rec.open('a', encoding='utf-8') as fh:\n"
        "    fh.write(data + " + repr(CALL_SEP) + ")\n"
        "sys.stdout.write(" + repr(stdout) + ")\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path)


def _summarizer_calls(record: Path) -> list[str]:
    if not record.exists():
        return []
    return [c for c in record.read_text(encoding="utf-8").split(CALL_SEP) if c.strip()]


@contextmanager
def _recorded_opens(root: Path):
    """Yields the set of ``*.jsonl`` paths under ``root`` opened inside the block.

    The wave-c gate-probe technique: ``Path.open`` is patched, plus ``builtins.open``
    and ``io.open`` so an implementation that drifts to a different open style is
    still measured. A set (not a counter) so the Path.open→io.open chain never
    double-counts. Stats/lstat are deliberately NOT counted: AC-F4.1 bounds READS,
    stat calls are the cheap currency the fix is allowed to spend.
    """
    opened: set[str] = set()
    root_prefix = str(root) + os.sep

    def _note(target) -> None:
        try:
            name = os.fspath(target)
        except TypeError:
            return
        if isinstance(name, bytes):
            name = os.fsdecode(name)
        name = os.path.abspath(name)
        if name.endswith(".jsonl") and name.startswith(root_prefix):
            opened.add(name)

    real_path_open, real_open = pathlib.Path.open, builtins.open

    def spy_path_open(self, *args, **kwargs):
        _note(self)
        return real_path_open(self, *args, **kwargs)

    def spy_open(file, *args, **kwargs):
        if not isinstance(file, int):
            _note(file)
        return real_open(file, *args, **kwargs)

    pathlib.Path.open = spy_path_open
    builtins.open = spy_open
    io.open = spy_open
    try:
        yield opened
    finally:
        pathlib.Path.open = real_path_open
        builtins.open = real_open
        io.open = real_open


# --------------------------------------------------------------------------- #
# AC-F4.1 — zero-read steady state                                             #
# --------------------------------------------------------------------------- #
def test_acf41_steady_state_sweep_opens_zero_transcripts(tmp_path, httpserver):
    """AC-F4.1: on a settled, fully-processed tree a sweep pass opens ZERO transcript
    files; every transcript counts into ``skipped``; the last-run record keeps the
    frozen schema. The pointers (and, post-E6, the recorded stat pairs) are written by
    the REAL processing path — never hand-crafted — so the skip decision is fed by
    exactly what ``_advance_pointer`` persists in production."""
    cfg = _mk_cfg(tmp_path, server_url=httpserver.url_for(""))
    repo = kit.make_git_repo(tmp_path / "realrepo")
    sids = [f"e6-steady-{i}" for i in range(3)]
    paths = [_settled_orphan(cfg, str(repo), sid) for sid in sids]
    cfg.claude_bin = kit.make_claude_stub(
        tmp_path / "claude_stub.py", stdout="## Decision\nSteady tree settled.")
    _wire_server(httpserver)

    setup = processor.run_once(cfg)
    assert setup.lock_held is False
    assert len(_posts(httpserver)) == 3, (
        f"setup: all three orphans must land via the real pipeline; "
        f"outcomes={setup.outcomes}")
    assert not list(cfg.queue_dir.glob("*.json")), "setup: queue must drain"
    state = processor._load_state(cfg)
    for p in paths:
        entry = state.get(str(p))
        assert isinstance(entry, dict) and entry.get("last_line") == 3, (
            f"setup: pointer for {p} must come from the real _advance_pointer path")

    # 'Once per pass, forever' (the wave-c build review, §2): the cost must be zero on EVERY
    # steady pass, not amortized on the first.
    for pass_no in (1, 2):
        state = processor._load_state(cfg)
        with _recorded_opens(cfg.sweep_transcripts_root) as opened:
            report = sweep.run_sweep(cfg, state)
        assert opened == set(), (
            f"AC-F4.1: steady-state pass {pass_no} must open ZERO transcript files "
            f"(today: every settled transcript is _line_count'ed in full, 225.4 MiB "
            f"/pass on the pilot host); opened={sorted(opened)}")
        assert (report.scanned, report.enqueued, report.skipped) == (3, 0, 3), (
            "AC-F4.1: stat-skipped transcripts count into SweepReport.skipped")
        rec = json.loads(cfg.sweep_last_run_path.read_text(encoding="utf-8"))
        # AMENDED at the phase-4 plan (2026-08-11, seam p4-sweep-last-run-additive):
        # the original EXACTLY-four assertion encoded phase 3's deferral of the counter
        # split, not a safety property. The frozen four and their semantics stay pinned;
        # additive keys are now permitted. (Clerical completion at wave g: the plan
        # amended the two occurrences its grep found and missed this third one.)
        assert set(rec) >= {"finished_at", "scanned", "enqueued", "skipped"}, (
            "p2-sweep-last-run-record: the frozen four must survive verbatim")
        assert (rec["scanned"], rec["enqueued"], rec["skipped"]) == (3, 0, 3)
    assert not list(cfg.queue_dir.glob("*.json"))


# --------------------------------------------------------------------------- #
# AC-F4.2 — THE HOLE: the append race is closed in the safe direction          #
# --------------------------------------------------------------------------- #
def test_acf42_pre_read_stat_recorded_and_sigkill_append_race_reswept(
        tmp_path, httpserver, monkeypatch):
    """AC-F4.2: a session that appends one line between the delta read and the
    pointer write, then dies (SIGKILL), IS re-swept and its content lands.

    The interleaving is real: ``read_delta`` is wrapped so the append happens after
    the actual read returns and before ANY later step (stat capture at pointer-write
    time, summarizer, persist) runs. Kills, by construction:
    - post-read stat capture: records the post-append file → recorded == current →
      skip forever → the appended line is lost (fails the field pins AND the re-sweep
      asserts);
    - ``mtime <= updated_at``: the append precedes the pointer write, so
      mtime < updated_at and the naive skip also eats the line.
    ``settle_s=0`` on purpose: nothing touches the file's post-append mtime, so a
    wrong implementation cannot be accidentally saved by a test-side utime."""
    cfg = _mk_cfg(tmp_path, settle_s=0, server_url=httpserver.url_for(""))
    repo = kit.make_git_repo(tmp_path / "realrepo")
    sid = "e6-race-0001"
    t = _settled_orphan(cfg, str(repo), sid, aged=10)
    record = tmp_path / "summarizer_calls.txt"
    cfg.claude_bin = _recording_claude(
        tmp_path / "claude_rec.py", record, "## Decision\nRace pass summary.")
    _wire_server(httpserver)

    pre_stat = t.stat()  # the file as it is BEFORE the read: the only safe capture
    appended = _assistant_line(sid, 2, f"Decided: {RACE_TOKEN}.", str(repo))
    fired = {"count": 0}
    real_read_delta = transcript_adapter.read_delta

    def racing_read_delta(transcript_path, last_line):
        delta = real_read_delta(transcript_path, last_line)
        if fired["count"] == 0 and str(transcript_path) == str(t):
            fired["count"] = 1
            _append_line(t, appended)  # lands between the read and everything after
        return delta

    with monkeypatch.context() as m:
        m.setattr(transcript_adapter, "read_delta", racing_read_delta)
        m.setattr(processor, "read_delta", racing_read_delta, raising=False)
        pass1 = processor.run_once(cfg)

    # Harness tripwires: prove the interleaving actually happened before judging.
    assert fired["count"] == 1, (
        f"harness: the race probe did not fire; outcomes={pass1.outcomes}")
    assert len(t.read_text(encoding="utf-8").splitlines()) == 4
    assert len(_posts(httpserver)) == 1, f"setup: outcomes={pass1.outcomes}"
    calls = _summarizer_calls(record)
    assert len(calls) == 1 and RACE_TOKEN not in calls[0], (
        "harness: the append must land AFTER the read — pass-1 stdin cannot see it")

    entry = processor._load_state(cfg).get(str(t))
    assert entry is not None and entry.get("last_line") == 3, (
        "pointer must cover exactly the read delta (3 lines), never the raced append")
    # p3-state-stat-fields — FAILS today (fields do not exist yet).
    for field in STAT_FIELDS:
        assert field in entry, (
            f"AC-F4.2: state entry must record additive '{field}' "
            f"(frozen p3-state-stat-fields; captured PRE-read by _advance_pointer)")
        assert type(entry[field]) is int
    # The pre-read pin: the recorded pair must describe the file BEFORE the read it
    # covers. A post-read capture records the appended file here and dies with a
    # byte-exact diff (size 4-lines vs 3-lines).
    assert entry["stat_size"] == pre_stat.st_size, (
        "AC-F4.2: recorded stat_size covers the APPENDED file — post-read capture; "
        "the raced append would be skipped forever (silent content loss)")
    assert entry["stat_mtime_ns"] == pre_stat.st_mtime_ns, (
        "AC-F4.2: recorded stat_mtime_ns covers the APPENDED file — post-read capture")

    # The SIGKILL victim goes silent; the next timer pass must find the lost line.
    pass2 = processor.run_once(cfg)
    assert pass2.sweep is not None and pass2.sweep.enqueued == 1, (
        f"AC-F4.2: the raced transcript must be re-swept (stale pre-read stat fails "
        f"the comparison → full check → enqueue); sweep={pass2.sweep}")
    posted = _posts(httpserver)
    assert len(posted) == 2, (
        f"AC-F4.2: the appended content must reach the server; outcomes={pass2.outcomes}")
    assert posted[1]["covered_from"] == f"{sid}-a1"
    assert posted[1]["covered_to"] == f"{sid}-a2"
    calls = _summarizer_calls(record)
    assert len(calls) == 2 and RACE_TOKEN in calls[1], (
        "AC-F4.2: the re-read delta must contain exactly the raced line")
    assert processor._load_state(cfg)[str(t)]["last_line"] == 4
    assert not list(cfg.queue_dir.glob("*.json"))


def test_acf42_same_second_same_size_stat_mismatch_still_forces_full_check(tmp_path):
    """AC-F4.2 (granularity): the frozen comparison is the integer
    ``(st_mtime_ns, st_size)`` pair. Recorded pair differs from the file only in
    sub-second mtime (same integer second; float-colliding where the filesystem
    allows), size identical, pointer behind EOF: an ``st_mtime_ns`` comparison
    mismatches and re-reads (enqueue); an int-second or float-second comparison —
    or a size-only one — 'matches', skips, and silently strands the content."""
    cfg = _mk_cfg(tmp_path)
    sid = "e6-nsgran-0001"
    t = cfg.sweep_transcripts_root / "-Users-e6-proj" / f"{sid}.jsonl"
    lines = kit.transcript_lines("/repo/nsgran", sid)
    lines.append(_assistant_line(sid, 2, "Fourth line: unswept content.", "/repo/nsgran"))
    kit.write_transcript(t, lines)  # 4 physical lines
    base_ns = (int(time.time()) - 7200) * 10**9 + 987_654_321  # settled, mid-second
    os.utime(t, ns=(base_ns, base_ns))
    actual = t.stat().st_mtime_ns
    second = actual // 10**9
    # Prefer a recorded value that ALSO collides in float seconds (kills float
    # comparisons); at minimum stay inside the same integer second (kills int ones).
    recorded = next((actual - d for d in range(1, 5000)
                     if (actual - d) // 10**9 == second
                     and (actual - d) / 1e9 == actual / 1e9), None)
    if recorded is None:  # coarse-timestamp filesystem: keep the same-second kill
        recorded = next(actual - d for d in range(1, 5000)
                        if (actual - d) // 10**9 == second)
    assert recorded != actual and recorded // 10**9 == second
    state = {str(t): {"last_line": 3, "last_uuid": f"{sid}-a1",
                      "updated_at": kit.utc_now_iso(),
                      "stat_mtime_ns": recorded, "stat_size": t.stat().st_size}}

    report = sweep.run_sweep(cfg, state)

    assert report.enqueued == 1, (
        f"AC-F4.2: mtime_ns {recorded} != {actual} (same second, same size) MUST fail "
        f"the comparison and force the full check — a seconds-granularity or "
        f"size-only comparison silently strands line 4 forever")
    assert (cfg.queue_dir / f"{sid}.json").exists()


# --------------------------------------------------------------------------- #
# AC-F4.3 — append after the pointer write is swept exactly as today           #
# --------------------------------------------------------------------------- #
def test_acf43_append_after_pointer_write_is_swept_even_with_old_mtime(
        tmp_path, httpserver):
    """AC-F4.3: content appended after the pointer write defeats the skip. The
    appended file is deliberately utime'd OLDER than the pointer's ``updated_at``:
    the size/mtime_ns pair still mismatches (correct), while the wave-a naive design
    (skip when mtime <= updated_at) skips and loses the session tail."""
    cfg = _mk_cfg(tmp_path, server_url=httpserver.url_for(""))
    repo = kit.make_git_repo(tmp_path / "realrepo")
    sid = "e6-post-0001"
    t = _settled_orphan(cfg, str(repo), sid)
    cfg.claude_bin = kit.make_claude_stub(
        tmp_path / "claude_stub.py", stdout="## Decision\nFirst chunk.")
    _wire_server(httpserver)
    setup = processor.run_once(cfg)
    assert len(_posts(httpserver)) == 1, f"setup: outcomes={setup.outcomes}"
    assert processor._load_state(cfg)[str(t)]["last_line"] == 3

    _append_line(t, _assistant_line(sid, 2, "Post-pointer decision.", str(repo)))
    kit.age_file(t, 3600)  # settled AND mtime < updated_at: the naive-design trap

    report = sweep.run_sweep(cfg, processor._load_state(cfg))

    assert report.enqueued == 1, (
        "AC-F4.3: post-pointer append (size+mtime changed) must be swept exactly as "
        "today; a mtime<=updated_at skip would silently drop this content")
    marker = json.loads((cfg.queue_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert marker["origin"] == "sweep" and marker["transcript_path"] == str(t)


# --------------------------------------------------------------------------- #
# AC-F4.4 — pre-upgrade state entries: load unchanged, never skip              #
# --------------------------------------------------------------------------- #
def test_acf44_pre_upgrade_state_entry_never_skips_and_loads_unchanged(
        tmp_path, httpserver):
    """AC-F4.4: entries without the new fields (a state.json written before the
    upgrade) load unchanged and NEVER skip — both when that costs a wasted read
    (fully-processed A is still opened) and when it saves content (appended B is
    opened, detected and enqueued)."""
    cfg = _mk_cfg(tmp_path, server_url=httpserver.url_for(""))
    repo = kit.make_git_repo(tmp_path / "realrepo")
    a = _settled_orphan(cfg, str(repo), "e6-preup-a")
    b = _settled_orphan(cfg, str(repo), "e6-preup-b")
    cfg.claude_bin = kit.make_claude_stub(
        tmp_path / "claude_stub.py", stdout="## Decision\nPre-upgrade tree.")
    _wire_server(httpserver)
    setup = processor.run_once(cfg)
    assert len(_posts(httpserver)) == 2, f"setup: outcomes={setup.outcomes}"
    assert not list(cfg.queue_dir.glob("*.json"))

    # Hand-strip to the exact phase-1 key set: a faithful pre-upgrade state.json.
    raw = json.loads(cfg.state_path.read_text(encoding="utf-8"))
    stripped = {p: {k: v for k, v in e.items() if k in PRE_UPGRADE_KEYS}
                for p, e in raw.items()}
    cfg.state_path.write_text(json.dumps(stripped), encoding="utf-8")

    _append_line(b, _assistant_line("e6-preup-b", 2, "New content in B.", str(repo)))
    kit.age_file(b, 3600)  # settled; also older than updated_at (naive-design trap)

    state = processor._load_state(cfg)
    assert state == stripped, "AC-F4.4: a pre-upgrade state file must load UNCHANGED"

    with _recorded_opens(cfg.sweep_transcripts_root) as opened:
        report = sweep.run_sweep(cfg, state)

    # AMENDED at wave g (2026-08-11, registered reconciliation, seam p4-coverage-cache):
    # this pinned phase 3's pre-cache COST topology — with no state stat fields the only
    # way to answer "anything new?" was a full read. The sweep-owned coverage cache is a
    # second, independent answer, so a legacy pointer may now be decided with zero opens
    # (E9's frozen goal AC-F3.2 candidate B requires exactly that). Relaxed in the
    # cost direction ONLY: opens may drop, never grow. Every DECISION assertion below is
    # untouched, and the eight `opened == set()` must-NOT-read pins in this file stay
    # verbatim — those are the safety direction.
    assert opened <= {str(a), str(b)}, (
        "AC-F4.4: entries without the stat fields must never read MORE than today's "
        f"full check; opened={sorted(opened)}")
    assert report.enqueued == 1 and report.skipped == 1
    markers = list(cfg.queue_dir.glob("*.json"))
    assert [m.stem for m in markers] == ["e6-preup-b"], (
        "AC-F4.4: only B (real new content) is enqueued; A is skipped by the check")


# --------------------------------------------------------------------------- #
# AC-F4.5 — frozen API surface                                                 #
# --------------------------------------------------------------------------- #
def test_acf45_run_sweep_signature_and_last_run_schema_frozen(tmp_path):
    """AC-F4.5: ``run_sweep(cfg, state) -> SweepReport`` (p2-sweep-api) and the
    ``sweep_last_run.json`` record stay byte-identical."""
    sig = inspect.signature(sweep.run_sweep)
    params = list(sig.parameters.values())
    assert [p.name for p in params] == ["cfg", "state"], (
        f"AC-F4.5: run_sweep signature is frozen; got {sig}")
    assert all(p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
               and p.default is inspect.Parameter.empty for p in params), (
        f"AC-F4.5: no new defaults/kinds on the frozen signature; got {sig}")
    assert sig.return_annotation in ("SweepReport", sweep.SweepReport)

    cfg = _mk_cfg(tmp_path)
    _settled_orphan(cfg, "/repo/schema", "e6-schema-0001")
    report = sweep.run_sweep(cfg, {})
    assert isinstance(report, sweep.SweepReport)

    rec = json.loads(cfg.sweep_last_run_path.read_text(encoding="utf-8"))
    # AMENDED at the phase-4 plan (2026-08-11, seam p4-sweep-last-run-additive): the
    # original EXACTLY-four assertion encoded phase 3's deferral of the counter split,
    # not a safety property. The frozen four and their semantics stay pinned; additive
    # keys are now permitted.
    assert set(rec) >= {"finished_at", "scanned", "enqueued", "skipped"}, (
        f"AC-F4.5: sweep_last_run.json keys must contain the frozen four; "
        f"got {sorted(rec)}")
    assert isinstance(rec["finished_at"], str) and rec["finished_at"].endswith("Z")
    for key in ("scanned", "enqueued", "skipped"):
        assert type(rec[key]) is int
    assert (rec["scanned"], rec["enqueued"], rec["skipped"]) == (1, 1, 0)


# --------------------------------------------------------------------------- #
# AC-F4.6 — cap / order / guard regressions                                    #
# --------------------------------------------------------------------------- #
def test_acf46_cap_bounds_pass_and_stops_scan_newest_first(tmp_path):
    """AC-F4.6: MAX_ENQUEUE_PER_PASS still bounds the pass AND stops the scan at the
    cap; candidates are visited newest-mtime-first."""
    assert sweep.MAX_ENQUEUE_PER_PASS == 10, "AC-F4.6: the cap constant is preserved"
    cfg = _mk_cfg(tmp_path)
    sids = [f"e6-cap-{i:02d}" for i in range(13)]
    for i, sid in enumerate(sids):  # i=0 newest, strictly decreasing mtime
        t = cfg.sweep_transcripts_root / "-Users-e6-cap" / f"{sid}.jsonl"
        kit.write_transcript(t, kit.transcript_lines("/repo/cap", sid))
        kit.age_file(t, 3600 + i * 60)

    report = sweep.run_sweep(cfg, {})

    assert report.enqueued == 10, "AC-F4.6: the cap must bound the pass"
    assert report.scanned == 10, (
        f"AC-F4.6: the pass must STOP SCANNING at the cap (bounding read cost, not "
        f"just enqueues — the wave-a build review, S7); scanned={report.scanned}")
    assert report.skipped == 0
    assert report.enqueued_sessions == sids[:10], (
        f"AC-F4.6: newest-mtime-first order; got {report.enqueued_sessions}")
    assert {p.stem for p in cfg.queue_dir.glob("*.json")} == set(sids[:10])


def test_acf46_marker_and_settle_checks_precede_any_stat_shortcut(tmp_path):
    """AC-F4.6: the queued-marker and settle guards keep running BEFORE anything
    else short-circuits. Both fixtures carry a stat MISMATCH plus content beyond the
    pointer — an implementation that hoists the stat comparison above the guards and
    enqueues (or reads) on mismatch overwrites the hook marker's crash context or
    reads a live session file; both must stay untouched AND unread."""
    cfg = _mk_cfg(tmp_path)
    # (a) already-queued session: stale stats + content beyond the pointer.
    sid_m = "e6-markered-0001"
    tm = _settled_orphan(cfg, "/repo/markered", sid_m)
    st = tm.stat()
    state = {str(tm): {"last_line": 1, "last_uuid": f"{sid_m}-u1",
                       "updated_at": kit.utc_now_iso(),
                       "stat_mtime_ns": st.st_mtime_ns - 12345,
                       "stat_size": st.st_size - 7}}
    hook_marker = {"session_id": sid_m, "transcript_path": "/from/hook.jsonl",
                   "cwd": "/hook/cwd", "reason": "prompt_input_exit",
                   "marked_at": "2026-08-01T00:00:00Z"}
    marker_path = cfg.queue_dir / f"{sid_m}.json"
    marker_path.write_text(json.dumps(hook_marker), encoding="utf-8")
    before = marker_path.read_bytes()
    # (b) fresh (unsettled) session: no pointer at all, content on disk.
    tf = cfg.sweep_transcripts_root / "-Users-e6-fresh" / "e6-fresh-0001.jsonl"
    kit.write_transcript(tf, kit.transcript_lines("/repo/fresh", "e6-fresh-0001"))

    with _recorded_opens(cfg.sweep_transcripts_root) as opened:
        report = sweep.run_sweep(cfg, state)

    assert report.enqueued == 0 and report.scanned == 2 and report.skipped == 2
    assert opened == set(), (
        f"AC-F4.6: markered and unsettled transcripts must be skipped BEFORE any "
        f"read; opened={sorted(opened)}")
    assert marker_path.read_bytes() == before, (
        "AC-F4.6: the hook marker must survive byte-identical (never overwritten)")


def test_acf46_failed_parked_session_is_not_reenqueued(tmp_path):
    """AC-F4.6 + p3-adapter-failure-bounding: a session whose marker is parked in
    failed/ is NOT re-enqueued (and costs no reads: the guard sits before
    _line_count). E5 lands this guard in wave d, BEFORE E6 starts — against today's
    pre-E5 tree this fails for exactly that reason, and E6 must not undo it."""
    cfg = _mk_cfg(tmp_path)
    sid = "e6-failed-0001"
    t = _settled_orphan(cfg, "/repo/failedcase", sid)
    parked = {"session_id": sid, "transcript_path": str(t), "cwd": "/repo/failedcase",
              "reason": "sweep", "origin": "sweep",
              "marked_at": "2026-08-01T00:00:00Z", "attempts": 3,
              "adapter_error": "line 2 does not parse as JSON"}
    (cfg.failed_dir / f"{sid}.json").write_text(json.dumps(parked), encoding="utf-8")

    with _recorded_opens(cfg.sweep_transcripts_root) as opened:
        report = sweep.run_sweep(cfg, {})

    assert report.enqueued == 0, (
        "AC-F4.6/E5: a failed/-parked session must NOT be re-enqueued — without the "
        "guard, parking creates an enqueue→fail→park→re-enqueue loop")
    assert not list(cfg.queue_dir.glob("*.json"))
    assert opened == set(), (
        f"the failed/ guard sits before _line_count: parked sessions cost no reads; "
        f"opened={sorted(opened)}")


# --------------------------------------------------------------------------- #
# Compose with E5 — the trailing-torn-hold, both directions                    #
# (maps to AC-F4.1/AC-F4.2 under the p3-adapter-tolerance seam)                #
# --------------------------------------------------------------------------- #
def _torn_tail_transcript(cfg: Config, sid: str, cwd: str) -> Path:
    """3 complete lines + a SIGKILL tear: a trailing partial line, no newline."""
    t = cfg.sweep_transcripts_root / "-Users-e6-torn" / f"{sid}.jsonl"
    kit.write_transcript(t, kit.transcript_lines(cwd, sid))
    with t.open("a", encoding="utf-8") as fh:
        fh.write('{"type":"assistant","uuid":"' + sid + '-a2","message"')
    return t


def test_compose_e5_trailing_hold_unchanged_file_is_skipped_without_reads(tmp_path):
    """Compose direction 1 (skip is CORRECT): E5's trailing-torn-hold leaves the
    pointer legitimately BEFORE EOF; the recorded stat covers exactly the read that
    produced that pointer and the file has not changed since. The skip must fire —
    no enqueue, no read — instead of misreading the held-back tear as 'new content'
    and re-reading/enqueue-looping the file forever. (AC-F4.1 economics on the
    p3-adapter-tolerance seam; state entry hand-crafted with the frozen field names
    because the E5 hold does not exist in the pre-wave-d tree.)"""
    cfg = _mk_cfg(tmp_path)
    sid = "e6-torn-hold-0001"
    t = _torn_tail_transcript(cfg, sid, "/repo/torn")
    kit.age_file(t, 7200)
    st = t.stat()  # the stat the pointer-producing read would have captured pre-read
    state = {str(t): {"last_line": 3, "last_uuid": f"{sid}-a1",
                      "updated_at": kit.utc_now_iso(),
                      "stat_mtime_ns": st.st_mtime_ns, "stat_size": st.st_size}}

    with _recorded_opens(cfg.sweep_transcripts_root) as opened:
        report = sweep.run_sweep(cfg, state)

    assert report.enqueued == 0 and report.skipped == 1, (
        "compose/E5: pointer-before-EOF on an UNCHANGED file is the torn-hold "
        "steady state — the matching stat pair must skip it, not enqueue-loop it")
    assert not list(cfg.queue_dir.glob("*.json"))
    assert opened == set(), (
        f"compose/E5: the held file must not be re-read every pass; "
        f"opened={sorted(opened)}")


def test_compose_e5_new_content_after_tear_defeats_the_skip(tmp_path):
    """Compose direction 2 (skip must NOT hide content): the session resumes and
    appends real lines after the tear. The file changed since the recorded pre-read
    stat → mismatch → full check → enqueue. Kills a skip keyed on anything but the
    stat pair (e.g. pointer presence or updated_at: the file is utime'd older than
    updated_at again). (AC-F4.2 on the p3-adapter-tolerance seam.)"""
    cfg = _mk_cfg(tmp_path)
    sid = "e6-torn-resume-0001"
    t = _torn_tail_transcript(cfg, sid, "/repo/torn")
    kit.age_file(t, 7200)
    st = t.stat()
    state = {str(t): {"last_line": 3, "last_uuid": f"{sid}-a1",
                      "updated_at": kit.utc_now_iso(),
                      "stat_mtime_ns": st.st_mtime_ns, "stat_size": st.st_size}}

    # Resumed writer appends complete lines; physically the first one merges into
    # the un-terminated torn tail (now an INTERIOR torn line), the next is clean.
    with t.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_assistant_line(sid, 3, "Post-tear decision.", "/repo/torn"))
                 + "\n"
                 + json.dumps(_assistant_line(sid, 4, "Second post-tear turn.",
                                              "/repo/torn")) + "\n")
    kit.age_file(t, 3600)  # settled again; still older than updated_at (naive trap)

    report = sweep.run_sweep(cfg, state)

    assert report.enqueued == 1, (
        "compose/E5: content appended after the tear MUST defeat the skip — a skip "
        "that keys on the pointer or updated_at hides the resumed session forever")
    marker = json.loads((cfg.queue_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert marker["origin"] == "sweep" and marker["cwd"] == "/repo/torn"


# --------------------------------------------------------------------------- #
# P2.F4 amendment 2026-08-10 (1) — the recorded stat grows the inode half      #
# --------------------------------------------------------------------------- #
def _raw_bytes(lines: list[dict]) -> bytes:
    """The exact bytes ``kit.write_transcript`` would lay down for ``lines``."""
    return ("\n".join(json.dumps(x) for x in lines) + "\n").encode("utf-8")


def test_p2f4_amendment_pointer_records_all_four_stat_fields_pre_read(
        tmp_path, httpserver):
    """AC-F4.2 (amendment): the pointer records ``stat_dev``/``stat_ino`` BESIDE
    ``stat_mtime_ns``/``stat_size``, all four taken from the same PRE-read stat, all four
    exact ``int``.

    This also pins the corroboration gate's operand after the tuple grew. The gate is
    ``delta.bytes_read == <the SIZE element>``; with a four-element tuple a bare
    positional index that still points at element 1 compares the read length against the
    INODE NUMBER, which is essentially never equal — the gate then silently suppresses
    every recording, no entry ever carries the fields and the skip is dead code that
    looks implemented. Recording happening AT ALL, with ``stat_size`` equal to the real
    size, is the assertion that catches it."""
    cfg = _mk_cfg(tmp_path, server_url=httpserver.url_for(""))
    repo = kit.make_git_repo(tmp_path / "realrepo")
    sid = "e6-amend-four-0001"
    t = _settled_orphan(cfg, str(repo), sid)
    cfg.claude_bin = kit.make_claude_stub(
        tmp_path / "claude_stub.py", stdout="## Decision\nFour-field stat.")
    _wire_server(httpserver)
    pre = t.stat()  # the only safe capture: the file BEFORE the read it will describe

    setup = processor.run_once(cfg)
    assert len(_posts(httpserver)) == 1, f"setup: outcomes={setup.outcomes}"

    entry = processor._load_state(cfg).get(str(t))
    assert entry is not None and entry.get("last_line") == 3
    for field in STAT_FIELDS + STAT_IDENTITY_FIELDS:
        assert field in entry, (
            f"AC-F4.2 (amendment): the state entry must record additive '{field}' — "
            f"the transcript's stat must be at least as strong as the one "
            f"processor._identity_of already spends on a (recoverable) marker")
        assert type(entry[field]) is int, (
            f"'{field}' is an int on purpose: a JSON true must never read as a number")
    assert entry["stat_size"] == pre.st_size, (
        "AC-F4.2 (amendment): stat_size must be the SIZE. If this is absent or wrong "
        "while the others are fine, the corroboration gate is comparing "
        "delta.bytes_read against the wrong tuple element (see the docstring)")
    assert entry["stat_mtime_ns"] == pre.st_mtime_ns
    assert entry["stat_dev"] == pre.st_dev and entry["stat_ino"] == pre.st_ino, (
        "AC-F4.2 (amendment): dev/ino must come from the SAME pre-read stat as the "
        "mtime/size pair, not from a second stat taken after the read")

    # And the four-field entry is a working skip, not just recorded ballast.
    with _recorded_opens(cfg.sweep_transcripts_root) as opened:
        report = sweep.run_sweep(cfg, processor._load_state(cfg))
    assert (report.enqueued, report.skipped) == (0, 1) and opened == set(), (
        f"AC-F4.1: the four-field entry the real pipeline just wrote must still skip "
        f"with zero reads; opened={sorted(opened)}")


def test_p2f4_amendment_uncorroborated_read_records_none_of_the_four_fields(
        tmp_path, httpserver, monkeypatch):
    """AC-F4.2 (amendment): the corroboration gate survives the tuple growing — a read
    that saw FEWER bytes than the stat reported records NO stat field at all (not the
    inode half either), and the transcript takes the full check next pass.

    The gate is what stops an over-describing pair from being stapled to a pointer that
    covers less; dropping it (or widening it to "any element matches") is silent,
    permanent loss, and a four-element tuple is exactly where such a rewrite happens."""
    cfg = _mk_cfg(tmp_path, server_url=httpserver.url_for(""))
    repo = kit.make_git_repo(tmp_path / "realrepo")
    sid = "e6-amend-gate-0001"
    t = _settled_orphan(cfg, str(repo), sid)
    cfg.claude_bin = kit.make_claude_stub(
        tmp_path / "claude_stub.py", stdout="## Decision\nUncorroborated read.")
    _wire_server(httpserver)
    real_read_delta = transcript_adapter.read_delta

    def short_read_delta(transcript_path, last_line):
        delta = real_read_delta(transcript_path, last_line)
        if str(transcript_path) == str(t):
            delta.bytes_read -= 1  # the stat says N, the read walked N-1
        return delta

    with monkeypatch.context() as m:
        m.setattr(transcript_adapter, "read_delta", short_read_delta)
        m.setattr(processor, "read_delta", short_read_delta, raising=False)
        pass1 = processor.run_once(cfg)

    assert len(_posts(httpserver)) == 1, f"setup: outcomes={pass1.outcomes}"
    entry = processor._load_state(cfg).get(str(t))
    assert entry is not None and entry.get("last_line") == 3, (
        "the pointer still advances: only the stat fields are withheld")
    for field in STAT_FIELDS + STAT_IDENTITY_FIELDS:
        assert field not in entry, (
            f"AC-F4.2 (amendment): an uncorroborated read must record NO stat field — "
            f"'{field}' present means the gate no longer covers the whole tuple")

    with _recorded_opens(cfg.sweep_transcripts_root) as opened:
        report = sweep.run_sweep(cfg, processor._load_state(cfg))
    # AMENDED at wave g (2026-08-11, registered reconciliation, seam p4-coverage-cache):
    # cost pin only. The corroboration GATE this test exists for is untouched and still
    # asserted verbatim above — an uncorroborated read still records NO stat field on the
    # pointer. What changed is that the sweep's own coverage entry can now answer
    # "nothing new" without a read; that entry stats itself BEFORE its own read, so the
    # append race stays closed in the safe direction (a race invalidates the entry rather
    # than being masked by it). Relaxed in the cost direction ONLY.
    assert opened <= {str(t)}, (
        f"AC-F4.2 (amendment): with no recorded stat the transcript must never take MORE "
        f"than the full check; opened={sorted(opened)}")
    assert report.enqueued == 0


def test_p2f4_amendment_matching_four_field_entry_skips_without_reading(tmp_path):
    """AC-F4.1 (amendment): an entry carrying all four MATCHING fields skips the
    transcript with zero reads — the strengthened comparison must not cost the economics
    it exists to protect."""
    cfg = _mk_cfg(tmp_path)
    sid = "e6-amend-skip-0001"
    t = _settled_orphan(cfg, "/repo/amend", sid)
    st = t.stat()
    state = {str(t): {"last_line": 3, "last_uuid": f"{sid}-a1",
                      "updated_at": kit.utc_now_iso(),
                      "stat_dev": st.st_dev, "stat_ino": st.st_ino,
                      "stat_mtime_ns": st.st_mtime_ns, "stat_size": st.st_size}}

    with _recorded_opens(cfg.sweep_transcripts_root) as opened:
        report = sweep.run_sweep(cfg, state)

    assert (report.scanned, report.enqueued, report.skipped) == (1, 0, 1)
    assert opened == set(), (
        f"AC-F4.1 (amendment): a fully-matching four-field entry must still skip "
        f"without opening the transcript; opened={sorted(opened)}")
    assert not list(cfg.queue_dir.glob("*.json"))


def test_p2f4_amendment_replaced_inode_same_mtime_and_size_is_never_skipped(tmp_path):
    """AC-F4.2 (amendment) — THE CLASS THIS AMENDMENT EXISTS FOR. A REAL ``os.replace``
    swaps a different transcript into the path; the replacement is then ``os.utime``-d
    back so mtime AND size are byte-identical to the recorded ones. Only the inode moved.

    That is ``cp -p`` / ``rsync -a`` / ``tar -xp`` / a backup restore / any
    replace-then-utimensat. Under the pre-amendment two-field comparison this file
    compares EQUAL and is skipped permanently, and the replacement's extra content — a
    whole session tail — is never captured. The size collision is constructed on purpose
    (the replacement pads one text field by exactly the bytes of the extra line), because
    a comparison that got the size right by accident proves nothing here."""
    cfg = _mk_cfg(tmp_path)
    cwd = "/repo/replaced"
    sid = "e6-amend-inode-0001"
    t = cfg.sweep_transcripts_root / "-Users-e6-proj" / f"{sid}.jsonl"

    extra = _assistant_line(sid, 7, "Restored tail only a full check can find.", cwd)
    extra_len = len(json.dumps(extra).encode("utf-8")) + 1  # + the newline
    pad = extra_len + 16
    before_lines = kit.transcript_lines(cwd, sid, assistant_text="Decision " + "P" * pad)
    after_lines = kit.transcript_lines(
        cwd, sid, assistant_text="Decision " + "P" * (pad - extra_len)) + [extra]
    assert len(_raw_bytes(before_lines)) == len(_raw_bytes(after_lines)), (
        "harness: the replacement must collide in SIZE, otherwise the two-field "
        "comparison would catch it and the test proves nothing")
    assert len(after_lines) == len(before_lines) + 1

    kit.write_transcript(t, before_lines)
    kit.age_file(t, 7200)
    st = t.stat()  # what the pointer-producing read would have recorded, pre-read
    state = {str(t): {"last_line": 3, "last_uuid": f"{sid}-a1",
                      "updated_at": kit.utc_now_iso(),
                      "stat_dev": st.st_dev, "stat_ino": st.st_ino,
                      "stat_mtime_ns": st.st_mtime_ns, "stat_size": st.st_size}}

    # The replacement: a real rename over the path, then mtime restored.
    incoming = t.with_name(f"{sid}.restored")
    incoming.write_bytes(_raw_bytes(after_lines))
    os.replace(incoming, t)
    os.utime(t, ns=(st.st_mtime_ns, st.st_mtime_ns))
    now = t.stat()
    assert (now.st_size, now.st_mtime_ns) == (st.st_size, st.st_mtime_ns), (
        "harness: size and mtime must be indistinguishable from the recorded ones")
    assert (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino), (
        "harness: os.replace must have moved the inode, otherwise there is nothing "
        "for the dev/ino half of the comparison to notice")

    report = sweep.run_sweep(cfg, state)

    assert report.enqueued == 1, (
        "AC-F4.2 (amendment): a same-size, same-mtime replacement is a DIFFERENT file. "
        "The recorded (st_dev, st_ino) no longer match, so the skip must not fire and "
        "the full check must find the extra line. A two-field comparison calls this "
        "'unchanged' and strands the restored tail forever — unrecoverable, silent")
    marker = json.loads((cfg.queue_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert marker["origin"] == "sweep" and marker["transcript_path"] == str(t)


def test_p2f4_amendment_legacy_two_field_entry_still_skips(tmp_path):
    """AC-F4.4 (amendment): a state entry carrying ONLY ``stat_mtime_ns``/``stat_size``
    — written before this amendment, or by an older client — keeps EXACTLY the
    pre-amendment behaviour and still skips an unchanged transcript.

    This pins the backward-compatible fallback so a later "simplification" to a strict
    all-four rule cannot silently turn the two hand-crafted pre-amendment entries in the
    compose/E5 tests above (which declare only the legacy pair, deliberately) from green
    to red — nor make every real pre-upgrade state.json pay a full re-read forever."""
    cfg = _mk_cfg(tmp_path)
    sid = "e6-amend-legacy-0001"
    t = _settled_orphan(cfg, "/repo/legacy", sid)
    st = t.stat()
    entry = {"last_line": 3, "last_uuid": f"{sid}-a1", "updated_at": kit.utc_now_iso(),
             "stat_mtime_ns": st.st_mtime_ns, "stat_size": st.st_size}
    assert all(f not in entry for f in STAT_IDENTITY_FIELDS), "harness: legacy shape"

    with _recorded_opens(cfg.sweep_transcripts_root) as opened:
        report = sweep.run_sweep(cfg, {str(t): entry})

    assert (report.enqueued, report.skipped) == (0, 1), (
        "AC-F4.4 (amendment): an entry that carries NEITHER identity field falls back "
        "to the legacy two-field comparison — the amendment is additive, never a "
        "retroactive invalidation of every entry already on disk")
    assert opened == set(), (
        f"the legacy fallback must still be a zero-read skip; opened={sorted(opened)}")
    assert not list(cfg.queue_dir.glob("*.json"))


# --------------------------------------------------------------------------- #
# P2.F4 amendment 2026-08-10 (2) — stat-skips are observable, off the seam     #
# --------------------------------------------------------------------------- #
def test_p2f4_amendment_stat_skips_are_counted_in_the_run_log_not_the_record(tmp_path):
    """AC-F4.5 (amendment): the stat-skip count is emitted on the ``sweep_pass`` run-log
    event and NOWHERE else. ``SweepReport`` and ``sweep_last_run.json`` are a phase-1
    frozen seam consumed by E8 — they must stay byte-schema-identical — but inside
    ``skipped`` a one-syscall stat-skip and a full-file-read-then-skip look the same, so
    a human on the pilot host cannot tell whether the optimisation fires at all.

    Fixture: one stat-skipping transcript (four matching fields) and one that is skipped
    for an unrelated reason (nothing beyond the pointer), so a counter wired to
    ``skipped`` rather than to the stat-skip itself reads 2 and fails."""
    cfg = _mk_cfg(tmp_path)
    skipper = _settled_orphan(cfg, "/repo/obs", "e6-amend-obs-hit")
    other = _settled_orphan(cfg, "/repo/obs", "e6-amend-obs-miss")
    st = skipper.stat()
    state = {
        str(skipper): {"last_line": 3, "last_uuid": "e6-amend-obs-hit-a1",
                       "updated_at": kit.utc_now_iso(),
                       "stat_dev": st.st_dev, "stat_ino": st.st_ino,
                       "stat_mtime_ns": st.st_mtime_ns, "stat_size": st.st_size},
        # No stat fields: read in full, found fully processed — skipped, but NOT a
        # stat-skip. This is the pair the counter has to tell apart.
        str(other): {"last_line": 3, "last_uuid": "e6-amend-obs-miss-a1",
                     "updated_at": kit.utc_now_iso()},
    }

    with _recorded_opens(cfg.sweep_transcripts_root) as opened:
        report = sweep.run_sweep(cfg, state)

    assert (report.scanned, report.enqueued, report.skipped) == (2, 0, 2)
    assert opened == {str(other)}, f"harness: opened={sorted(opened)}"

    passes = [ln for ln in cfg.log_file.read_text(encoding="utf-8").splitlines()
              if "\tsweep_pass\t" in ln]
    assert len(passes) == 1, f"one pass, one sweep_pass event; got {passes}"
    assert "stat_skips=1" in passes[0], (
        f"AC-F4.5 (amendment): the sweep_pass run-log event must carry the stat-skip "
        f"count (stat_skips=1 here: one of the two skips avoided a read). "
        f"'stat_skips=2' means it is just mirroring SweepReport.skipped; "
        f"line={passes[0]!r}")
    assert RACE_TOKEN not in passes[0], "counts only — never transcript content (I5/I6)"

    # The frozen four survive with their semantics. AMENDED at the phase-4 plan
    # (2026-08-11, seam p4-sweep-last-run-additive): the EXACTLY-four and
    # no-new-SweepReport-field assertions encoded phase 3's deferral of the counter
    # split; additive keys/fields are now permitted. The load-bearing assertion above
    # (stat_skips=1, not 2 — the count is NOT a mirror of `skipped`) is unchanged.
    rec = json.loads(cfg.sweep_last_run_path.read_text(encoding="utf-8"))
    assert set(rec) >= {"finished_at", "scanned", "enqueued", "skipped"}, (
        f"AC-F4.5: sweep_last_run.json keeps the frozen four; got {sorted(rec)}")
