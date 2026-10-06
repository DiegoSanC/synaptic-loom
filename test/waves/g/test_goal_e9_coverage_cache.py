"""FROZEN GOAL — E9 sweep-economics-close (P3.F1..F4, AC-F1.1..AC-F4.3). Read-only
to implementers.

Oracle properties: real tmp LOOM_HOME + real transcript tree driving the REAL
``run_sweep`` (the caller owns loading state — p2-sweep-api). The cwd→remote boundary
is REAL: every eligibility fixture builds actual git repos (``git init`` + ``git
remote add``) so ``processor._git_remote`` runs against real repos, never a mock. The
project-registry cache is hand-written JSON at ``cfg.cache_path`` in the exact
``{fetched_at, projects[]}`` shape ``processor._load_registry`` persists; the coverage
cache is read and hand-written at the frozen derivation ``cfg.state_path.parent /
"coverage_cache.json"`` in the frozen per-entry shape (p4-coverage-cache). File-open
instrumentation reuses the wave-c gate-probe technique (patched ``Path.open`` +
``builtins.open`` + ``io.open``, dedup by path) from the frozen E6 goal.

Adversarial design — the wrong implementations this goal is built to break:

- Cache bolted to delivery AGAIN (entry written only when a marker is accepted —
  the exact hole this epic exists to close): AC-F1.2 uses an ABSENT registry
  (fail-open) so the sweep enqueues a marker that nothing ever consumes — no server,
  no processor, no delivery — and the entry must exist anyway. AC-F1.1's no-cwd
  fixture kills the sibling mistake "cache only reads that produced a usable cwd":
  the RUNBOOK §9 residual class (re-read forever) is exactly these files.
- 2- or 3-field stat matching: the AC-F1.3 inode swap is a REAL ``os.replace`` with
  the mtime_ns utime'd back and the SIZE collided by construction — only the
  ``st_ino`` half of the four moved, and the hand-written cache entry carries
  ``cwd: null``, so a 2/3-field "hit" reuses null, skips, and strands the restored
  session (enqueued == 0, dead). The sub-second test kills int/float-second mtime
  comparisons the same way E6's granularity test does.
- Tombstones (anything that remembers "don't enqueue X" beyond current stat +
  current registry — in the cache entry, in state, in a side file): AC-F2.2 runs TWO
  withheld passes over UNCHANGED files (stat-matched, cache-served by then), then
  changes only the registry: the historical orphans must deliver next pass.
- Pointer-stat reuse / state mutation for non-deliverables (owner hard constraint):
  AC-F1.5/AC-F2.5 deep-compare the state dict and byte-compare state.json across
  passes that hit the cache and withhold candidates.
- Eligibility consuming ``MAX_ENQUEUE_PER_PASS`` slots or writing markers: AC-F2.1
  is the VERIFIED starvation base (wave-f review) — 12 newer non-deliverables + 1
  older deliverable + a registry naming only the deliverable; the deliverable must
  land on the FIRST pass, and no noise marker may exist.
- ``processor._load_registry`` reuse (TTL + network): AC-F2.4's registry is
  present+parseable but EXPIRED by TTL and the server_url is unroutable — a reused
  loader refreshes, fails, reports unavailable, fails open and enqueues the
  unregistered candidate; the sweep must instead consume the stale cache read-only
  (bytes and mtime_ns pinned) and still withhold.
- Counters wired to ``skipped`` instead of classified: AC-F3.2 builds exactly ONE
  candidate per skip class (stat-skip / cache-hit / ineligible / settle-residue),
  disjoint by construction, so (1, 1, 1) is the only correct read and a mirror of
  ``skipped`` reads 4. Record assertions are superset-tolerant ONLY
  (``set(rec) >= {...}``) — the exactly-four mistake was amended once already and is
  not repeated here.
- ``errors="replace"`` surviving in ``read_cwd``: AC-F4.2's first fixture puts the
  bad byte in a NON-cwd line's string value BEFORE the only cwd line — replace
  launders it, parses past it and returns the late cwd; strict returns None. The
  second fixture launders the cwd VALUE itself into a U+FFFD path. AC-F4.3 kills the
  overcorrection (a whole-buffer strict ``open``): the torn multi-byte tail sits in
  the same read chunk as line 1, so only per-line strict decoding keeps today's
  green "cwd before the tear is returned".

AC map (greppable):
  AC-F1.1  test_ac_f1_1_unregistered_transcript_read_once_then_stat_only
           test_ac_f1_1_no_cwd_transcript_is_cached_and_never_reopened
  AC-F1.2  test_ac_f1_2_cache_entry_written_without_any_delivery
           test_ac_f1_2_coverage_cache_module_api_frozen
  AC-F1.3  test_ac_f1_3_inode_swap_same_size_same_mtime_invalidates_and_reenqueues
           test_ac_f1_3_sub_second_mtime_change_alone_invalidates
  AC-F1.4  test_ac_f1_4_corrupt_or_deleted_cache_changes_cost_never_decisions
  AC-F1.5  test_ac_f1_5_cache_passes_leave_state_dict_and_file_untouched
  AC-F1.6  test_ac_f1_6_cache_write_oserror_degrades_without_aborting
  AC-F2.1  test_ac_f2_1_starvation_corpus_deliverable_enqueued_first_pass
  AC-F2.2  test_ac_f2_2_late_registration_unblocks_historical_orphans
  AC-F2.3  test_ac_f2_3_absent_or_garbage_registry_fails_open_like_today
  AC-F2.4  test_ac_f2_4_registry_consumed_read_only_even_when_stale
  AC-F2.5  test_ac_f2_5_ineligible_candidates_cost_no_slot_marker_or_state
  AC-F3.1  test_ac_f3_1_record_gains_additive_ints_frozen_four_intact
           test_ac_f3_1_not_scanned_counts_capped_candidates_not_skipped
  AC-F3.2  test_ac_f3_2_skip_class_counters_partition_skipped
  AC-F3.3  test_ac_f3_3_run_log_sweep_pass_retains_existing_fields_verbatim
  AC-F3.4  test_ac_f3_4_doctor_sweep_row_identical_with_enriched_record
  AC-F4.1  test_ac_f4_1_today_green_read_cwd_behaviors_unchanged
  AC-F4.2  test_ac_f4_2_invalid_utf8_before_only_cwd_line_yields_none
  AC-F4.3  test_ac_f4_3_valid_cwd_before_torn_multibyte_tail_is_returned

Expected against TODAY's (pre-E9) tree: every F1 cache test except AC-F1.6, every F2
eligibility test except the fail-open AC-F2.3, every F3 record/counter test except
the doctor pin AC-F3.4, and AC-F4.2 FAIL for the right reasons; the
regression-direction tests (AC-F1.6, AC-F2.3, AC-F3.4, AC-F4.1, AC-F4.3, the module
API pin) PASS.

Run: .venv/bin/python -m pytest test/waves/g/test_goal_e9_coverage_cache.py -q
"""
from __future__ import annotations

import builtins
import copy
import inspect
import io
import json
import os
import pathlib
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _goalkit as kit  # noqa: E402

from loom import cli, coverage_cache, sweep  # noqa: E402
from loom.config import Config  # noqa: E402

# The frozen per-entry shape of coverage_cache.json (p4-coverage-cache).
COVERAGE_FIELDS = ("stat_dev", "stat_ino", "stat_mtime_ns", "stat_size",
                   "line_count", "cwd")
# The frozen four of sweep_last_run.json (p2-sweep-last-run-record) …
FROZEN_FOUR = ("finished_at", "scanned", "enqueued", "skipped")
# … and the additive integers (p4-sweep-last-run-additive).
ADDITIVE_INTS = ("stat_skips", "cache_hits", "ineligible", "not_scanned")
# A real remote URL whose canonical form is NEVER put in any registry fixture here.
NOISE_REMOTE_URL = "git@github.com:example-group/not-registered.git"


# --------------------------------------------------------------------------- #
# Harness                                                                      #
# --------------------------------------------------------------------------- #
def _mk_cfg(tmp: Path, *, settle_s: int = 1800) -> Config:
    """Temp LOOM_HOME; server_url deliberately unroutable — the sweep must never
    need a server (AC-F2.4: no network I/O ever)."""
    tmp.mkdir(parents=True, exist_ok=True)
    home = kit.loom_home(tmp)
    root = tmp / "transcripts"
    root.mkdir(exist_ok=True)
    return Config(
        loom_home=home, server_url="http://127.0.0.1:9", token="goal-token",
        claude_bin="claude", prompt_path=home / "prompts" / "summarizer_v1.md",
        sweep_enabled=True, sweep_settle_s=settle_s, sweep_transcripts_root=root,
        scanner_engine="regex",
    )


def _settled(cfg: Config, sid: str, cwd: str, *, aged: float = 7200,
             subdir: str = "-Users-e9-proj") -> Path:
    path = cfg.sweep_transcripts_root / subdir / f"{sid}.jsonl"
    kit.write_transcript(path, kit.transcript_lines(cwd, sid))
    kit.age_file(path, aged)
    return path


def _write_no_cwd(cfg: Config, sid: str, *, aged: float = 7200) -> Path:
    """A settled 3-line transcript where NO line carries a cwd — today's
    re-read-forever residual class (RUNBOOK §9)."""
    lines = [
        {"type": "file-history-snapshot", "messageId": "m0", "snapshot": {}},
        {"type": "user", "uuid": f"{sid}-u1", "sessionId": sid,
         "message": {"role": "user", "content": "no cwd was ever recorded"}},
        {"type": "assistant", "uuid": f"{sid}-a1", "sessionId": sid,
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "still no cwd"}]}},
    ]
    path = cfg.sweep_transcripts_root / "-Users-e9-nocwd" / f"{sid}.jsonl"
    kit.write_transcript(path, lines)
    kit.age_file(path, aged)
    return path


def _cov_path(cfg: Config) -> Path:
    """Frozen derivation (p4-coverage-cache): state.json's directory."""
    return cfg.state_path.parent / "coverage_cache.json"


def _load_cov(cfg: Config) -> dict:
    """The coverage cache read INDEPENDENTLY of the module under test."""
    path = _cov_path(cfg)
    assert path.exists(), (
        f"p4-coverage-cache: expected the sweep-owned cache at {path} "
        f"(state.json's directory) — no cache file was written")
    try:
        return json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AssertionError(
            f"p4-coverage-cache: {path} must parse as the frozen JSON map after a "
            f"pass (a pass over corrupt content rebuilds it); got unparseable "
            f"content ({exc})") from exc


def _write_cov(cfg: Config, mapping: dict) -> None:
    _cov_path(cfg).write_text(json.dumps(mapping), encoding="utf-8")


def _cov_entry(st, *, line_count, cwd) -> dict:
    return {"stat_dev": st.st_dev, "stat_ino": st.st_ino,
            "stat_mtime_ns": st.st_mtime_ns, "stat_size": st.st_size,
            "line_count": line_count, "cwd": cwd}


def _write_registry(cfg: Config, canonicals: list[str], *,
                    fetched_at: str | None = None) -> Path:
    """Registry cache in the exact shape ``processor._load_registry`` persists."""
    projects = [
        {"id": i + 1, "canonical_remote": c, "name": f"goal-proj-{i}",
         "created_by": "goal", "created_at": "2026-07-01T00:00:00Z"}
        for i, c in enumerate(canonicals)
    ]
    cfg.cache_path.write_text(
        json.dumps({"fetched_at": fetched_at or kit.utc_now_iso(),
                    "projects": projects}),
        encoding="utf-8",
    )
    return cfg.cache_path


def _marker_stems(cfg: Config) -> set[str]:
    return {p.stem for p in cfg.queue_dir.glob("*.json")}


def _clear_queue(cfg: Config) -> None:
    for p in cfg.queue_dir.glob("*.json"):
        p.unlink()


def _marker(cfg: Config, sid: str) -> dict:
    return json.loads((cfg.queue_dir / f"{sid}.json").read_text(encoding="utf-8"))


def _raw(lines: list[dict]) -> bytes:
    """The exact bytes ``kit.write_transcript`` would lay down for ``lines``."""
    return ("\n".join(json.dumps(x) for x in lines) + "\n").encode("utf-8")


@contextmanager
def _recorded_opens(root: Path):
    """Yields the set of ``*.jsonl`` paths under ``root`` opened inside the block
    (the wave-c gate-probe technique, as in the frozen E6 goal). Stats/lstat are
    deliberately NOT counted: they are the cheap currency the cache is allowed to
    spend."""
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
# AC-F1.1 — a non-deliverable transcript is fully read at most once            #
# --------------------------------------------------------------------------- #
def test_ac_f1_1_unregistered_transcript_read_once_then_stat_only(tmp_path):
    """AC-F1.1 (+AC-F2 seam): an UNREGISTERED project's transcript, unchanged across
    passes, pays exactly one full read; every later pass costs one stat and ZERO
    opens — and it is withheld (never enqueued) because the registry is present and
    does not list its remote. The cwd→remote boundary is a real git repo."""
    cfg = _mk_cfg(tmp_path)
    repo_no = kit.make_git_repo(tmp_path / "unregistered_repo", NOISE_REMOTE_URL)
    _write_registry(cfg, [kit.TEST_REMOTE_CANONICAL])
    t = _settled(cfg, "e9-f11-unreg-0001", str(repo_no), aged=7200)

    with _recorded_opens(cfg.sweep_transcripts_root) as opened:
        first = sweep.run_sweep(cfg, {})

    assert opened == {str(t)}, (
        f"harness: the FIRST pass must pay the one full read (eligibility needs the "
        f"cwd, and the cwd only exists inside the file); opened={sorted(opened)}")
    assert (first.scanned, first.enqueued, first.skipped) == (1, 0, 1), (
        "AC-F1.1/AC-F2: an unregistered project's transcript is INELIGIBLE while the "
        "registry is present — withheld, not enqueued (today it enqueues)")
    assert not list(cfg.queue_dir.glob("*.json")), (
        "an ineligible candidate must never leave a marker behind")
    entry = _load_cov(cfg).get(str(t))
    assert isinstance(entry, dict) and entry.get("cwd") == str(repo_no), (
        f"AC-F1.1: the completed read must be recorded (path + cwd) so pass 2 can "
        f"answer from the cache; entry={entry!r}")

    for pass_no in (2, 3):
        with _recorded_opens(cfg.sweep_transcripts_root) as opened:
            rep = sweep.run_sweep(cfg, {})
        assert opened == set(), (
            f"AC-F1.1: steady pass {pass_no} on the unchanged non-deliverable must "
            f"cost one stat and ZERO opens (today: a full _line_count + read_cwd "
            f"every pass, forever — RUNBOOK §9's 0.7 GiB/day residual); "
            f"opened={sorted(opened)}")
        assert (rep.scanned, rep.enqueued, rep.skipped) == (1, 0, 1)
        assert not list(cfg.queue_dir.glob("*.json"))


def test_ac_f1_1_no_cwd_transcript_is_cached_and_never_reopened(tmp_path):
    """AC-F1.1 (cwd|null): a settled transcript with NO cwd line is today's
    read-forever class even without any registry. The first full read must be
    recorded with ``cwd: null`` and later passes must not open the file. Kills an
    implementation that only records entries whose read produced a usable cwd."""
    cfg = _mk_cfg(tmp_path)  # no registry: fail-open — pure cache economics
    t = _write_no_cwd(cfg, "e9-f11-nocwd-0001", aged=7200)

    with _recorded_opens(cfg.sweep_transcripts_root) as opened:
        first = sweep.run_sweep(cfg, {})
    assert opened == {str(t)}, f"harness: pass 1 reads in full; opened={sorted(opened)}"
    assert (first.scanned, first.enqueued, first.skipped) == (1, 0, 1)

    entry = _load_cov(cfg).get(str(t))
    assert isinstance(entry, dict), (
        "AC-F1.1: a read that found NO cwd still completed — it must be recorded "
        "(cwd is nullable in the frozen entry shape), otherwise this class is "
        "re-read every pass forever")
    assert entry.get("cwd") is None and entry.get("line_count") == 3

    for pass_no in (2, 3):
        with _recorded_opens(cfg.sweep_transcripts_root) as opened:
            rep = sweep.run_sweep(cfg, {})
        assert opened == set(), (
            f"AC-F1.1: pass {pass_no} must be served by the cached (line_count, "
            f"cwd=null) pair with zero opens; opened={sorted(opened)}")
        assert (rep.scanned, rep.enqueued, rep.skipped) == (1, 0, 1)


# --------------------------------------------------------------------------- #
# AC-F1.2 — the write condition carries NO delivery semantics                  #
# --------------------------------------------------------------------------- #
def test_ac_f1_2_cache_entry_written_without_any_delivery(tmp_path):
    """AC-F1.2: the registry is ABSENT (fail-open), so the sweep enqueues a
    synthetic marker — and NOTHING ever consumes it: no server, no processor run,
    no accepted entry, no pointer advance. The cache entry must exist anyway,
    carrying the frozen fields from the read that just completed. Kills the exact
    original sin: coupling "have I looked at it?" to "did the server accept it?"."""
    cfg = _mk_cfg(tmp_path)
    sid = "e9-f12-orphan-0001"
    t = _settled(cfg, sid, "/repo/f12", aged=7200)
    pre = t.stat()  # the file is never modified: the read's stat is this stat

    report = sweep.run_sweep(cfg, {})

    assert report.enqueued == 1 and (cfg.queue_dir / f"{sid}.json").exists(), (
        f"harness: fail-open enqueue must happen (today's behavior); report={report}")
    cov = _load_cov(cfg)
    entry = cov.get(str(t))
    assert isinstance(entry, dict), (
        f"AC-F1.2: the full read completed, so the entry must exist even though the "
        f"marker was never accepted by any server (no delivery semantics in the "
        f"write condition); cache keys={sorted(cov)}")
    assert set(entry) >= set(COVERAGE_FIELDS), (
        f"p4-coverage-cache: entry must carry the frozen fields; got {sorted(entry)}")
    for name, expect in (("stat_dev", pre.st_dev), ("stat_ino", pre.st_ino),
                         ("stat_mtime_ns", pre.st_mtime_ns),
                         ("stat_size", pre.st_size)):
        assert type(entry[name]) is int and entry[name] == expect, (
            f"AC-F1.2: '{name}' must be the exact int from the read's lstat; "
            f"got {entry[name]!r}, expected {expect}")
    assert entry["line_count"] == 3 and entry["cwd"] == "/repo/f12"


def test_ac_f1_2_coverage_cache_module_api_frozen():
    """p4-coverage-cache: the module API is frozen as the existing stub —
    ``load``/``lookup``/``record``/``save`` with these exact parameters (E12
    consumes them next wave)."""
    sigs = {name: list(inspect.signature(getattr(coverage_cache, name)).parameters)
            for name in ("load", "lookup", "record", "save")}
    assert sigs == {
        "load": ["cache_path"],
        "lookup": ["cache", "transcript_path", "st"],
        "record": ["cache", "transcript_path", "st", "line_count", "cwd"],
        "save": ["cache", "cache_path"],
    }, f"p4-coverage-cache: frozen module API drifted; got {sigs}"


# --------------------------------------------------------------------------- #
# AC-F1.3 — ALL FOUR stat fields participate in the match                      #
# --------------------------------------------------------------------------- #
def _same_size_swap_blobs(cwd_after: str) -> tuple[bytes, bytes]:
    """(before, after) transcript blobs of IDENTICAL byte length: before has no
    usable cwd anywhere; after carries ``cwd_after``. The size collision is built,
    not accidental — a comparison that got the size right by luck proves nothing."""
    meta = {"type": "file-history-snapshot", "messageId": "m0", "snapshot": {}}
    asst = {"type": "assistant", "uuid": "e9-swap-a1",
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "no cwd here"}]}}

    def user(text: str, cwd: str | None = None) -> dict:
        obj: dict = {"type": "user", "uuid": "e9-swap-u1"}
        if cwd is not None:
            obj["cwd"] = cwd
        obj["message"] = {"role": "user", "content": text}
        return obj

    after_user = user("restored session tail", cwd_after)
    pad = len(json.dumps(after_user)) - len(json.dumps(user("restored session tail")))
    before_user = user("restored session tail" + "P" * pad)
    before, after = _raw([meta, before_user, asst]), _raw([meta, after_user, asst])
    assert len(before) == len(after), "harness: the swap must collide in SIZE"
    return before, after


def test_ac_f1_3_inode_swap_same_size_same_mtime_invalidates_and_reenqueues(tmp_path):
    """AC-F1.3 — THE four-field pin. A real ``os.replace`` swaps a different file
    into the path; mtime_ns is utime'd back and the size collides by construction:
    only (st_dev, st_ino) moved. The hand-written cache entry says ``cwd: null`` —
    a 2- or 3-field match calls the swap a HIT, reuses null, skips, and the restored
    session (which now HAS a cwd) is stranded forever. All four must mismatch →
    full read → enqueue with the replacement's cwd → entry repopulated."""
    cfg = _mk_cfg(tmp_path)  # registry absent: enqueue decisions are today's
    sid = "e9-f13-swap-0001"
    t = cfg.sweep_transcripts_root / "-Users-e9-proj" / f"{sid}.jsonl"
    before, after = _same_size_swap_blobs("/repo/restored")
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_bytes(before)
    kit.age_file(t, 7200)
    st = t.stat()
    _write_cov(cfg, {str(t): _cov_entry(st, line_count=3, cwd=None)})

    incoming = t.with_name(f"{sid}.restored")
    incoming.write_bytes(after)
    os.replace(incoming, t)
    os.utime(t, ns=(st.st_mtime_ns, st.st_mtime_ns))
    now = t.stat()
    assert (now.st_size, now.st_mtime_ns) == (st.st_size, st.st_mtime_ns), (
        "harness: size and mtime_ns must be indistinguishable from the cached ones")
    assert (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino), (
        "harness: os.replace must have moved the inode")

    with _recorded_opens(cfg.sweep_transcripts_root) as opened:
        report = sweep.run_sweep(cfg, {})

    assert report.enqueued == 1, (
        "AC-F1.3: only the inode half of the four changed — the entry must be "
        "invalidated and the file RE-READ; a dev/ino-blind match reuses the cached "
        "cwd=null and silently strands the replaced session forever")
    assert str(t) in opened, f"AC-F1.3: the mismatch must force a full read; opened={sorted(opened)}"
    marker = _marker(cfg, sid)
    assert marker["origin"] == "sweep" and marker["cwd"] == "/repo/restored", (
        "the enqueue must come from the REPLACEMENT's content, not the cached entry")
    fresh = _load_cov(cfg)[str(t)]
    assert fresh["stat_ino"] == now.st_ino and fresh["cwd"] == "/repo/restored", (
        f"AC-F1.3: the re-read must repopulate the entry with the new identity; "
        f"got {fresh!r}")


def test_ac_f1_3_sub_second_mtime_change_alone_invalidates(tmp_path):
    """AC-F1.3 (granularity): the cached entry differs from disk ONLY in sub-second
    mtime — same integer second (float-colliding where the filesystem allows), same
    size, same dev/ino. Only an integer ``st_mtime_ns`` comparison sees it; the
    cached ``line_count: 0`` means a false HIT concludes "nothing to do" and skips
    a transcript with unswept content."""
    cfg = _mk_cfg(tmp_path)
    sid = "e9-f13-ns-0001"
    t = _settled(cfg, sid, "/repo/nsbump", aged=7200)
    base_ns = (int(time.time()) - 7200) * 10**9 + 987_654_321
    os.utime(t, ns=(base_ns, base_ns))
    st = t.stat()
    actual = st.st_mtime_ns
    second = actual // 10**9
    recorded = next((actual - d for d in range(1, 5000)
                     if (actual - d) // 10**9 == second
                     and (actual - d) / 1e9 == actual / 1e9), None)
    if recorded is None:  # coarse-timestamp filesystem: keep the same-second kill
        recorded = next(actual - d for d in range(1, 5000)
                        if (actual - d) // 10**9 == second)
    assert recorded != actual and recorded // 10**9 == second
    entry = _cov_entry(st, line_count=0, cwd=None)
    entry["stat_mtime_ns"] = recorded
    _write_cov(cfg, {str(t): entry})

    report = sweep.run_sweep(cfg, {})

    assert report.enqueued == 1, (
        f"AC-F1.3: cached mtime_ns {recorded} != {actual} (same second, same size, "
        f"same inode) MUST invalidate — an int-second, float-second or "
        f"mtime-blind comparison 'hits' the stale line_count=0 and strands the file")
    assert (cfg.queue_dir / f"{sid}.json").exists()
    assert _load_cov(cfg)[str(t)]["stat_mtime_ns"] == actual, (
        "AC-F1.3: the re-read must repopulate the entry with the actual mtime_ns")


# --------------------------------------------------------------------------- #
# AC-F1.4 — corrupt cache ≡ absent cache ≡ cost, never decisions               #
# --------------------------------------------------------------------------- #
def test_ac_f1_4_corrupt_or_deleted_cache_changes_cost_never_decisions(tmp_path):
    """AC-F1.4: pass 1 (no cache at all) is the uncached CONTROL. Then the cache is
    replaced with garbage bytes, and then deleted outright; each subsequent pass
    must complete normally, enqueue EXACTLY the control's marker set, and leave a
    valid, rebuilt cache behind."""
    cfg = _mk_cfg(tmp_path)  # registry absent throughout
    x1 = _settled(cfg, "e9-f14-x1", "/repo/x1", aged=7200)
    x2 = _settled(cfg, "e9-f14-x2", "/repo/x2", aged=8000)
    y = _write_no_cwd(cfg, "e9-f14-nocwd", aged=9000)

    control = sweep.run_sweep(cfg, {})
    assert _marker_stems(cfg) == {"e9-f14-x1", "e9-f14-x2"}, (
        f"harness/control: the uncached pass defines the decisions; report={control}")
    assert (control.scanned, control.enqueued, control.skipped) == (3, 2, 1)

    for tag, sabotage in (
        ("garbage", lambda: _cov_path(cfg).write_bytes(b"\x00\xff{{{ not json")),
        ("deleted", lambda: _cov_path(cfg).unlink(missing_ok=True)),
    ):
        _clear_queue(cfg)
        sabotage()
        rep = sweep.run_sweep(cfg, {})  # must not raise
        assert _marker_stems(cfg) == {"e9-f14-x1", "e9-f14-x2"}, (
            f"AC-F1.4({tag}): enqueue DECISIONS must be identical to the uncached "
            f"control — the cache changes read COST only, never outcomes")
        assert (rep.scanned, rep.enqueued, rep.skipped) == (3, 2, 1), (
            f"AC-F1.4({tag}): the pass must complete normally; report={rep}")
        cov = _load_cov(cfg)
        assert set(cov) >= {str(x1), str(x2), str(y)}, (
            f"AC-F1.4({tag}): the pass re-read everything, so it must REBUILD the "
            f"cache (corrupt ≡ absent ≡ empty); keys={sorted(cov)}")


# --------------------------------------------------------------------------- #
# AC-F1.5 — cache purity: state.json is sacred                                 #
# --------------------------------------------------------------------------- #
def test_ac_f1_5_cache_passes_leave_state_dict_and_file_untouched(tmp_path):
    """AC-F1.5: passes that hit the cache never read from it into ``state`` nor
    write coverage into ``state`` — the dict is deep-equal and state.json is
    byte-identical after two passes. Kills the owner-forbidden design of reusing
    the pointer's stat pair (or the pointer file) for non-deliverable coverage."""
    cfg = _mk_cfg(tmp_path)
    a = _settled(cfg, "e9-f15-deliv", "/repo/a", aged=7200)
    b = _write_no_cwd(cfg, "e9-f15-nocwd", aged=8000)
    st_a = a.stat()
    state = {str(a): {"last_line": 3, "last_uuid": "e9-f15-deliv-a1",
                      "updated_at": kit.utc_now_iso(),
                      "stat_dev": st_a.st_dev, "stat_ino": st_a.st_ino,
                      "stat_mtime_ns": st_a.st_mtime_ns, "stat_size": st_a.st_size}}
    cfg.state_path.write_text(json.dumps(state), encoding="utf-8")
    state_bytes = cfg.state_path.read_bytes()
    snapshot = copy.deepcopy(state)

    r1 = sweep.run_sweep(cfg, state)  # pass 1: A stat-skips, B is read + cached
    with _recorded_opens(cfg.sweep_transcripts_root) as opened:
        r2 = sweep.run_sweep(cfg, state)  # pass 2: both served without a read

    assert state == snapshot, (
        "AC-F1.5: run_sweep must NEVER mutate the state dict — read coverage lives "
        "in its own file, never in pointer entries (owner hard constraint)")
    assert cfg.state_path.read_bytes() == state_bytes, (
        "AC-F1.5: state.json must be byte-identical after cache-hitting passes")
    assert str(b) not in state, (
        "AC-F1.5: the non-deliverable transcript must not have grown a state entry")
    assert (r1.scanned, r1.enqueued, r1.skipped) == (2, 0, 2)
    assert opened == set(), (
        f"AC-F1.5/AC-F1.1: pass 2 is a stat-skip for A and a cache answer for B — "
        f"zero opens; opened={sorted(opened)}")
    assert (r2.scanned, r2.enqueued, r2.skipped) == (2, 0, 2)
    assert str(b) in _load_cov(cfg), "B's coverage must live in the cache file"
    assert _cov_path(cfg) != cfg.state_path


# --------------------------------------------------------------------------- #
# AC-F1.6 — a failing cache write degrades, never aborts                       #
# --------------------------------------------------------------------------- #
def test_ac_f1_6_cache_write_oserror_degrades_without_aborting(tmp_path):
    """AC-F1.6: the cache path is a DIRECTORY, so every save attempt raises a real
    OSError (and every load reads "corrupt ≡ absent"). The pass must complete with
    unchanged decisions; the next pass simply pays the read again (uncached)."""
    cfg = _mk_cfg(tmp_path)
    sid = "e9-f16-orphan-0001"
    t = _settled(cfg, sid, "/repo/f16", aged=7200)
    _cov_path(cfg).mkdir()  # both open-for-write and os.replace onto it are OSErrors

    report = sweep.run_sweep(cfg, {})  # must not raise

    assert report.enqueued == 1 and (cfg.queue_dir / f"{sid}.json").exists(), (
        f"AC-F1.6: the OSError on the cache write must not change the enqueue "
        f"decision nor abort the pass; report={report}")
    assert _cov_path(cfg).is_dir()

    _clear_queue(cfg)
    with _recorded_opens(cfg.sweep_transcripts_root) as opened:
        rep2 = sweep.run_sweep(cfg, {})
    assert str(t) in opened, (
        "AC-F1.6: nothing was persisted, so the next pass is UNCACHED — it re-reads "
        "and reaches the same decision")
    assert rep2.enqueued == 1 and (cfg.queue_dir / f"{sid}.json").exists()


# --------------------------------------------------------------------------- #
# AC-F2.1 — the VERIFIED starvation base, closed on the first pass             #
# --------------------------------------------------------------------------- #
def test_ac_f2_1_starvation_corpus_deliverable_enqueued_first_pass(tmp_path):
    """AC-F2.1: 12 newer transcripts of a real-but-unregistered repo + 1 older
    transcript of the registered repo. Today the ten newest non-deliverables eat
    all ten slots and STOP THE SCAN every pass, forever (wave-f review). Ineligible
    candidates must be withheld WITHOUT consuming a slot, so the scan reaches the
    older deliverable on the FIRST pass."""
    cfg = _mk_cfg(tmp_path)
    repo_ok = kit.make_git_repo(tmp_path / "registered_repo")
    repo_no = kit.make_git_repo(tmp_path / "unregistered_repo", NOISE_REMOTE_URL)
    _write_registry(cfg, [kit.TEST_REMOTE_CANONICAL])
    noise = [f"e9-f21-noise-{i:02d}" for i in range(12)]
    for i, sid in enumerate(noise):
        _settled(cfg, sid, str(repo_no), aged=1900 + i * 60,
                 subdir="-Users-e9-noise")
    old_sid = "e9-f21-deliverable-0001"
    _settled(cfg, old_sid, str(repo_ok), aged=86400)

    report = sweep.run_sweep(cfg, {})

    assert (cfg.queue_dir / f"{old_sid}.json").exists(), (
        "AC-F2.1: the older DELIVERABLE transcript must be enqueued on the FIRST "
        "pass — if the 12 newer ineligible candidates consumed slots (or stopped "
        "the scan at the cap), the registered project starves forever")
    assert _marker_stems(cfg) == {old_sid}, (
        f"AC-F2.1/AC-F2.5: no ineligible candidate may leave a marker; "
        f"markers={sorted(_marker_stems(cfg))}")
    assert (report.scanned, report.enqueued, report.skipped) == (13, 1, 12), (
        f"AC-F2.1: withheld candidates are skipped, cost no slot, and do not stop "
        f"the scan; report={report}")
    assert _marker(cfg, old_sid)["cwd"] == str(repo_ok)


# --------------------------------------------------------------------------- #
# AC-F2.2 — no tombstone: late registration unblocks history                   #
# --------------------------------------------------------------------------- #
def test_ac_f2_2_late_registration_unblocks_historical_orphans(tmp_path):
    """AC-F2.2: two passes withhold the orphans of a not-yet-registered project;
    the files never change (by pass 2 they are stat-matched, cache-served
    candidates). Then ONLY the registry changes. The next pass must enqueue the
    historical orphans — killing every tombstone: an "ineligible" flag in the cache
    entry, a skip-set in state, a side file, anything remembered beyond current
    stat + current registry content."""
    cfg = _mk_cfg(tmp_path)
    repo = kit.make_git_repo(tmp_path / "late_repo")  # canonical: TEST_REMOTE_CANONICAL
    other = "github.com/example-group/some-other-project"
    _write_registry(cfg, [other])
    sids = ("e9-f22-hist-0001", "e9-f22-hist-0002")
    _settled(cfg, sids[0], str(repo), aged=14400)
    _settled(cfg, sids[1], str(repo), aged=10800)

    for pass_no in (1, 2):
        rep = sweep.run_sweep(cfg, {})
        assert rep.enqueued == 0 and not list(cfg.queue_dir.glob("*.json")), (
            f"pass {pass_no}: the project is not in the present registry, so its "
            f"orphans are withheld (today they enqueue immediately); report={rep}")

    # The processor registers the project (only the registry cache changes).
    _write_registry(cfg, [other, kit.TEST_REMOTE_CANONICAL])

    rep3 = sweep.run_sweep(cfg, {})

    assert rep3.enqueued == 2 and _marker_stems(cfg) == set(sids), (
        f"AC-F2.2 (anti-tombstone): nothing about the FILES changed between passes "
        f"— eligibility must be re-evaluated from CURRENT registry content even on "
        f"stat-matched, cache-served candidates, so the historical orphans deliver "
        f"on the next pass with no manual intervention; report={rep3}, "
        f"markers={sorted(_marker_stems(cfg))}")
    assert _marker(cfg, sids[0])["cwd"] == str(repo)
    assert _marker(cfg, sids[0])["origin"] == "sweep"


# --------------------------------------------------------------------------- #
# AC-F2.3 — fail-open: no registry (or an unparseable one) is TODAY, verbatim  #
# --------------------------------------------------------------------------- #
def test_ac_f2_3_absent_or_garbage_registry_fails_open_like_today(tmp_path):
    """AC-F2.3: with no registry cache file the precheck is DISABLED — a settled
    orphan whose cwd has NO git remote still enqueues (the processor owns the
    drop). Same with an unparseable registry and an unregistered remote. Kills an
    implementation that gates on "no remote" unconditionally, or that reads
    garbage as an empty registry and withholds everything."""
    # (a) registry ABSENT + a cwd that is a real directory with NO git remote.
    cfg_a = _mk_cfg(tmp_path / "absent")
    plain = tmp_path / "absent" / "plain_dir"
    plain.mkdir()
    _settled(cfg_a, "e9-f23-noremote-0001", str(plain), aged=7200)
    assert not cfg_a.cache_path.exists()

    rep_a = sweep.run_sweep(cfg_a, {})

    assert rep_a.enqueued == 1, (
        "AC-F2.3: no registry file → precheck disabled → byte-for-byte today's "
        "behavior: the settled orphan enqueues and the PROCESSOR decides the drop")
    marker_a = _marker(cfg_a, "e9-f23-noremote-0001")
    assert marker_a["cwd"] == str(plain) and marker_a["origin"] == "sweep"

    # (b) UNPARSEABLE registry + a real repo whose remote is not registered.
    cfg_b = _mk_cfg(tmp_path / "garbage")
    repo_no = kit.make_git_repo(tmp_path / "garbage" / "unregistered_repo",
                                NOISE_REMOTE_URL)
    cfg_b.cache_path.write_bytes(b"\x00\xff{{{ not a registry")
    reg_bytes = cfg_b.cache_path.read_bytes()
    _settled(cfg_b, "e9-f23-garbage-0001", str(repo_no), aged=7200)

    rep_b = sweep.run_sweep(cfg_b, {})

    assert rep_b.enqueued == 1 and (cfg_b.queue_dir / "e9-f23-garbage-0001.json").exists(), (
        "AC-F2.3: an unparseable registry disables the precheck (fail-open) — "
        "treating it as an EMPTY registry would silently withhold every transcript "
        "on the machine")
    assert cfg_b.cache_path.read_bytes() == reg_bytes, (
        "the sweep must not rewrite or 'repair' the garbage registry file")


# --------------------------------------------------------------------------- #
# AC-F2.4 — registry consumed read-only; never refreshed, never networked      #
# --------------------------------------------------------------------------- #
def test_ac_f2_4_registry_consumed_read_only_even_when_stale(tmp_path):
    """AC-F2.4: the registry is present + parseable but EXPIRED by cache_ttl_s, and
    the server_url is unroutable. The sweep must still use it (present+parseable is
    the whole condition — the sweep NEVER refreshes): the unregistered candidate is
    withheld, and the file's bytes and mtime are untouched. An implementation that
    reuses ``processor._load_registry`` refreshes, gets ServerUnreachable, reports
    the registry unavailable, fails open — and enqueues both."""
    cfg = _mk_cfg(tmp_path)  # server_url http://127.0.0.1:9 — connection refused
    repo_ok = kit.make_git_repo(tmp_path / "registered_repo")
    repo_no = kit.make_git_repo(tmp_path / "unregistered_repo", NOISE_REMOTE_URL)
    reg = _write_registry(cfg, [kit.TEST_REMOTE_CANONICAL],
                          fetched_at="2026-01-01T00:00:00Z")  # long past cache_ttl_s
    reg_bytes = reg.read_bytes()
    reg_mtime_ns = reg.stat().st_mtime_ns
    _settled(cfg, "e9-f24-eligible-0001", str(repo_ok), aged=9000)
    _settled(cfg, "e9-f24-withheld-0001", str(repo_no), aged=3600)

    report = sweep.run_sweep(cfg, {})

    assert _marker_stems(cfg) == {"e9-f24-eligible-0001"}, (
        f"AC-F2.4: a stale-but-parseable registry is still the eligibility source — "
        f"the sweep never refreshes and never performs network I/O; enqueuing the "
        f"unregistered candidate means the precheck fell open on TTL; "
        f"markers={sorted(_marker_stems(cfg))}")
    assert (report.scanned, report.enqueued, report.skipped) == (2, 1, 1)
    assert reg.read_bytes() == reg_bytes, (
        "AC-F2.4: registry content must be byte-identical after the pass")
    assert reg.stat().st_mtime_ns == reg_mtime_ns, (
        "AC-F2.4: registry mtime must be untouched (read-only consumption)")


# --------------------------------------------------------------------------- #
# AC-F2.5 — ineligible: no slot, no marker, no state mutation                  #
# --------------------------------------------------------------------------- #
def test_ac_f2_5_ineligible_candidates_cost_no_slot_marker_or_state(tmp_path):
    """AC-F2.5: both ineligibility flavors — (a) canonical remote not in the
    registry, (b) cwd with no git remote at all — are withheld with no marker; the
    eligible candidates all land; the state dict and state.json are untouched."""
    cfg = _mk_cfg(tmp_path)
    repo_ok = kit.make_git_repo(tmp_path / "registered_repo")
    repo_no = kit.make_git_repo(tmp_path / "unregistered_repo", NOISE_REMOTE_URL)
    plain = tmp_path / "no_remote_dir"
    plain.mkdir()
    _write_registry(cfg, [kit.TEST_REMOTE_CANONICAL])
    _settled(cfg, "e9-f25-elig-0001", str(repo_ok), aged=9000)
    _settled(cfg, "e9-f25-elig-0002", str(repo_ok), aged=8000)
    _settled(cfg, "e9-f25-inelig-0001", str(repo_no), aged=4000)
    _settled(cfg, "e9-f25-inelig-0002", str(repo_no), aged=4100)
    _settled(cfg, "e9-f25-inelig-0003", str(plain), aged=4200)
    state = {"/somewhere/else.jsonl": {"last_line": 5, "last_uuid": "x-a5",
                                       "updated_at": kit.utc_now_iso()}}
    cfg.state_path.write_text(json.dumps(state), encoding="utf-8")
    state_bytes = cfg.state_path.read_bytes()
    snapshot = copy.deepcopy(state)

    report = sweep.run_sweep(cfg, state)

    assert _marker_stems(cfg) == {"e9-f25-elig-0001", "e9-f25-elig-0002"}, (
        f"AC-F2.5: ineligible candidates (unregistered remote OR no remote, with a "
        f"registry present) must never write a marker — a synthetic marker would "
        f"hand the drop to the processor, and the withhold IS the feature; "
        f"markers={sorted(_marker_stems(cfg))}")
    assert (report.scanned, report.enqueued, report.skipped) == (5, 2, 3)
    assert state == snapshot and cfg.state_path.read_bytes() == state_bytes, (
        "AC-F2.5: the precheck must never advance or mutate state.json — drops "
        "remain processor-owned")


# --------------------------------------------------------------------------- #
# AC-F3.1 — the record: frozen four intact, additive ints present              #
# --------------------------------------------------------------------------- #
def test_ac_f3_1_record_gains_additive_ints_frozen_four_intact(tmp_path):
    """AC-F3.1: the frozen four keep their exact types/semantics; the additive
    integer keys exist (superset-tolerant assertions ONLY — the exactly-four pin
    was amended at plan time and is not re-imposed here); SweepReport carries the
    same additive fields; ``enqueued + skipped == scanned`` still holds."""
    cfg = _mk_cfg(tmp_path)
    _settled(cfg, "e9-f31-orphan-0001", "/repo/f31", aged=7200)
    _write_no_cwd(cfg, "e9-f31-nocwd-0001", aged=8000)

    report = sweep.run_sweep(cfg, {})

    rec = json.loads(cfg.sweep_last_run_path.read_text(encoding="utf-8"))
    # Deliberately >= and NEVER ==: additive keys are the point of this seam.
    assert set(rec) >= set(FROZEN_FOUR), (
        f"p2-sweep-last-run-record: the frozen four must survive; got {sorted(rec)}")
    assert isinstance(rec["finished_at"], str) and rec["finished_at"].endswith("Z")
    for key in ("scanned", "enqueued", "skipped"):
        assert type(rec[key]) is int
    assert (rec["scanned"], rec["enqueued"], rec["skipped"]) == (2, 1, 1)
    assert rec["enqueued"] + rec["skipped"] == rec["scanned"]
    for key in ADDITIVE_INTS:
        assert key in rec, (
            f"AC-F3.1 (p4-sweep-last-run-additive): additive int key '{key}' must "
            f"be in sweep_last_run.json; got {sorted(rec)}")
        assert type(rec[key]) is int, f"'{key}' must be an exact int"
    assert (rec["stat_skips"], rec["cache_hits"], rec["ineligible"],
            rec["not_scanned"]) == (0, 0, 0, 0), (
        "first uncached pass with no registry: no stat-skip, no cache hit, no "
        "ineligible, nothing capped")
    for key in ADDITIVE_INTS:
        got = getattr(report, key, None)
        assert type(got) is int and got == rec[key], (
            f"AC-F3.1: SweepReport must gain the same additive field '{key}' "
            f"(got {got!r}, record says {rec[key]})")


def test_ac_f3_1_not_scanned_counts_capped_candidates_not_skipped(tmp_path):
    """AC-F3.1/AC-F3.2 (cap semantics): ``not_scanned`` counts the candidates the
    cap left unexamined and is NOT part of ``skipped`` (the frozen E6 goal pins the
    cap stopping the scan; this pins where those candidates are counted)."""
    cfg = _mk_cfg(tmp_path)
    for i in range(13):
        _settled(cfg, f"e9-f31-cap-{i:02d}", "/repo/cap", aged=3600 + i * 60)

    report = sweep.run_sweep(cfg, {})

    assert (report.scanned, report.enqueued, report.skipped) == (10, 10, 0), (
        f"harness (frozen E6 cap behavior): the cap bounds the pass AND the scan; "
        f"report={report}")
    rec = json.loads(cfg.sweep_last_run_path.read_text(encoding="utf-8"))
    assert type(rec.get("not_scanned")) is int and rec["not_scanned"] == 3, (
        f"AC-F3.1: the 3 cap-unscanned candidates are counted as not_scanned; "
        f"got {rec.get('not_scanned')!r}")
    assert rec["skipped"] == 0 and rec["enqueued"] + rec["skipped"] == rec["scanned"], (
        "not_scanned candidates were never examined: they are NOT skipped")
    assert getattr(report, "not_scanned", None) == 3


# --------------------------------------------------------------------------- #
# AC-F3.2 — the skip classes partition `skipped`                               #
# --------------------------------------------------------------------------- #
def test_ac_f3_2_skip_class_counters_partition_skipped(tmp_path):
    """AC-F3.2: exactly ONE candidate per class, disjoint by construction (no
    candidate can legally land in two classes):

    - A stat-skips (pointer carries the four matching fields);
    - B is a cache answer: pointer WITHOUT stat fields (uncorroborated read) but a
      hand-written coverage entry says line_count == last_line → nothing new,
      decided with zero opens;
    - C is fresh (not cached), fully read, then withheld as ineligible;
    - D is unsettled → the explicit `other` residue;
    - E enqueues (not a skip at all).

    A counter wired to ``skipped`` reads 4; the correct classification reads
    (1, 1, 1) with a residue of exactly 1."""
    cfg = _mk_cfg(tmp_path)
    repo_ok = kit.make_git_repo(tmp_path / "registered_repo")
    repo_no = kit.make_git_repo(tmp_path / "unregistered_repo", NOISE_REMOTE_URL)
    _write_registry(cfg, [kit.TEST_REMOTE_CANONICAL])

    a = _settled(cfg, "e9-f32-statskip", "/repo/a", aged=7000)
    b = _settled(cfg, "e9-f32-cachehit", "/repo/b", aged=6000)
    c = _settled(cfg, "e9-f32-inelig", str(repo_no), aged=5000)
    _settled(cfg, "e9-f32-fresh", "/repo/d", aged=10)  # unsettled: `other`
    e = _settled(cfg, "e9-f32-enq", str(repo_ok), aged=9000)

    st_a, st_b = a.stat(), b.stat()
    state = {
        str(a): {"last_line": 3, "last_uuid": "e9-f32-statskip-a1",
                 "updated_at": kit.utc_now_iso(),
                 "stat_dev": st_a.st_dev, "stat_ino": st_a.st_ino,
                 "stat_mtime_ns": st_a.st_mtime_ns, "stat_size": st_a.st_size},
        # Uncorroborated pointer: no stat fields, so only the coverage cache can
        # answer "nothing new" for B without a read.
        str(b): {"last_line": 3, "last_uuid": "e9-f32-cachehit-a1",
                 "updated_at": kit.utc_now_iso()},
    }
    _write_cov(cfg, {str(b): _cov_entry(st_b, line_count=3, cwd="/repo/b")})

    with _recorded_opens(cfg.sweep_transcripts_root) as opened:
        report = sweep.run_sweep(cfg, state)

    assert _marker_stems(cfg) == {"e9-f32-enq"}, (
        f"AC-F2/AC-F3.2: only E is deliverable — the ineligible candidate must be "
        f"withheld before its skip can be classified; "
        f"markers={sorted(_marker_stems(cfg))}")
    assert (report.scanned, report.enqueued, report.skipped) == (5, 1, 4)
    assert opened == {str(c), str(e)}, (
        f"harness: only the fresh ineligible C and the enqueued E cost a read — "
        f"A stat-skips, B is answered from the cache, D is unsettled; "
        f"opened={sorted(opened)}")

    rec = json.loads(cfg.sweep_last_run_path.read_text(encoding="utf-8"))
    for key in ADDITIVE_INTS:
        assert type(rec.get(key)) is int, (
            f"AC-F3.2: '{key}' must be an int in the record; got {rec.get(key)!r}")
    assert rec["stat_skips"] + rec["cache_hits"] + rec["ineligible"] <= rec["skipped"], (
        "AC-F3.2: the named classes can never exceed skipped (each skipped "
        "candidate is counted in exactly one class)")
    assert (rec["stat_skips"], rec["cache_hits"], rec["ineligible"],
            rec["not_scanned"]) == (1, 1, 1, 0), (
        f"AC-F3.2: one candidate per class — a counter wired to `skipped` reads 4 "
        f"here, a counter that double-counts reads >1 somewhere; record={rec}")
    assert rec["skipped"] - (rec["stat_skips"] + rec["cache_hits"]
                             + rec["ineligible"]) == 1, (
        "AC-F3.2: the residue (the unsettled candidate) is the explicit `other` "
        "class — exactly 1 here")
    assert rec["enqueued"] + rec["skipped"] == rec["scanned"]
    for key in ADDITIVE_INTS:
        assert getattr(report, key, None) == rec[key], (
            f"SweepReport.{key} must agree with the record")


# --------------------------------------------------------------------------- #
# AC-F3.3 — the sweep_pass run-log line: old fields verbatim, new counts added #
# --------------------------------------------------------------------------- #
def test_ac_f3_3_run_log_sweep_pass_retains_existing_fields_verbatim(tmp_path):
    """AC-F3.3: the ``sweep_pass`` event keeps ``scanned=/enqueued=/skipped=/
    stat_skips=`` byte-verbatim (the frozen E6 goal already pins ``stat_skips=``;
    this pins all four against a log-line rewrite) and appends the new counts."""
    cfg = _mk_cfg(tmp_path)
    a = _settled(cfg, "e9-f33-statskip", "/repo/f33", aged=7200)
    _settled(cfg, "e9-f33-enq", "/repo/f33", aged=9000)
    st = a.stat()
    state = {str(a): {"last_line": 3, "last_uuid": "e9-f33-statskip-a1",
                      "updated_at": kit.utc_now_iso(),
                      "stat_dev": st.st_dev, "stat_ino": st.st_ino,
                      "stat_mtime_ns": st.st_mtime_ns, "stat_size": st.st_size}}

    sweep.run_sweep(cfg, state)

    passes = [ln for ln in cfg.log_file.read_text(encoding="utf-8").splitlines()
              if "\tsweep_pass\t" in ln]
    assert len(passes) == 1, f"one pass, one sweep_pass event; got {passes}"
    line = passes[0]
    for token in ("scanned=2", "enqueued=1", "skipped=1", "stat_skips=1"):
        assert token in line, (
            f"AC-F3.3: '{token}' must survive VERBATIM on the sweep_pass line "
            f"(the wave-e goal pins stat_skips= there); line={line!r}")
    for token in ("cache_hits=0", "ineligible=0"):
        assert token in line, (
            f"p4-sweep-last-run-additive: the sweep_pass line appends the new "
            f"counts; '{token}' missing from line={line!r}")


# --------------------------------------------------------------------------- #
# AC-F3.4 — doctor is untouched by the enriched record                         #
# --------------------------------------------------------------------------- #
def test_ac_f3_4_doctor_sweep_row_identical_with_enriched_record(tmp_path):
    """AC-F3.4: the doctor row that consumes sweep_last_run.json must produce the
    IDENTICAL (status, message) whether the record carries only the frozen four or
    the additive superset — doctor is not touched by this phase."""
    cfg = _mk_cfg(tmp_path)
    finished = (datetime.now(timezone.utc) - timedelta(seconds=120)) \
        .isoformat().replace("+00:00", "Z")
    frozen = {"finished_at": finished, "scanned": 7, "enqueued": 1, "skipped": 6}
    cfg.sweep_last_run_path.write_text(json.dumps(frozen), encoding="utf-8")
    baseline = cli._sweep_check(cfg)
    assert baseline[0] == "ok", f"harness: a 2-minute-old record is healthy; got {baseline}"

    enriched = dict(frozen, stat_skips=4, cache_hits=1, ineligible=1, not_scanned=0)
    cfg.sweep_last_run_path.write_text(json.dumps(enriched), encoding="utf-8")

    assert cli._sweep_check(cfg) == baseline, (
        "AC-F3.4: additive record keys must be invisible to doctor — same status, "
        "same message, byte-identical row")


# --------------------------------------------------------------------------- #
# AC-F4.1 — today-green read_cwd behaviors, unchanged                          #
# --------------------------------------------------------------------------- #
def test_ac_f4_1_today_green_read_cwd_behaviors_unchanged(tmp_path):
    """AC-F4.1: cwd comes from CONTENT (never the encoded dirname); blank,
    malformed, non-object, non-string-cwd and empty-cwd lines are skipped; no cwd
    → None; unreadable path → None."""
    root = tmp_path / "transcripts" / "-Users-not-the-real-cwd"
    root.mkdir(parents=True)
    t = root / "e9-f41-mixed.jsonl"
    t.write_bytes(b"\n".join([
        json.dumps({"type": "file-history-snapshot", "messageId": "m0",
                    "snapshot": {}}).encode("utf-8"),
        b"",
        b"{this is not json",
        json.dumps([1, 2, 3]).encode("utf-8"),
        json.dumps({"type": "user", "uuid": "u0", "cwd": 42,
                    "message": {}}).encode("utf-8"),
        json.dumps({"type": "user", "uuid": "u1", "cwd": "   ",
                    "message": {}}).encode("utf-8"),
        json.dumps({"type": "user", "uuid": "u2", "cwd": "/repo/from-content",
                    "sessionId": "s",
                    "message": {"role": "user", "content": "hi"}}).encode("utf-8"),
    ]) + b"\n")
    assert sweep.read_cwd(t) == "/repo/from-content", (
        "AC-F4.1: junk lines are skipped and the cwd comes from content, never "
        "from the '-Users-…' directory name")

    t2 = root / "e9-f41-nocwd.jsonl"
    t2.write_bytes(json.dumps({"type": "assistant", "uuid": "a1",
                               "message": {}}).encode("utf-8") + b"\n")
    assert sweep.read_cwd(t2) is None, "AC-F4.1: no cwd anywhere → None"
    assert sweep.read_cwd(root / "e9-f41-does-not-exist.jsonl") is None, (
        "AC-F4.1: OSError (missing file) → None, exactly as today")


# --------------------------------------------------------------------------- #
# AC-F4.2 — strict decode: broken bytes are never laundered into a cwd         #
# --------------------------------------------------------------------------- #
def test_ac_f4_2_invalid_utf8_before_only_cwd_line_yields_none(tmp_path):
    """AC-F4.2: two fixtures where ``errors='replace'`` PARSES a cwd and strict
    decode must return None with no exception escaping.

    (1) The bad byte sits in a NON-cwd line's string value; the file's only cwd
        line comes AFTER it. Replace launders line 1 into valid-but-cwd-less JSON,
        keeps streaming, and returns the late cwd — strict must stop and return
        None ("no cwd derivable yet", retried once the bytes heal).
    (2) The bad byte sits inside the cwd VALUE itself: replace returns a U+FFFD
        path — laundered garbage the downstream remote gate would then stat."""
    root = tmp_path / "transcripts" / "-Users-e9-strict"
    root.mkdir(parents=True)

    bad_line = json.dumps(
        {"type": "assistant", "uuid": "e9-f42-a1",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "caf@@BAD@@ decision"}]}}
    ).encode("utf-8").replace(b"@@BAD@@", b"\xff")
    cwd_line = json.dumps(
        {"type": "user", "uuid": "e9-f42-u1", "cwd": "/repo/late-cwd",
         "message": {"role": "user", "content": "late"}}).encode("utf-8")
    t = root / "e9-f42-late.jsonl"
    t.write_bytes(bad_line + b"\n" + cwd_line + b"\n")

    got = sweep.read_cwd(t)  # must not raise (UnicodeDecodeError handled like OSError)
    assert got is None, (
        f"AC-F4.2: the only cwd line sits AFTER invalid UTF-8 — errors='replace' "
        f"launders the bad byte and returns {got!r}; strict decode must treat the "
        f"broken bytes like an OSError and return None (retried next pass)")

    laundered = json.dumps(
        {"type": "user", "uuid": "e9-f42-u2", "cwd": "/repo/caf@@BAD@@e",
         "message": {"role": "user", "content": "x"}}
    ).encode("utf-8").replace(b"@@BAD@@", b"\xff")
    t2 = root / "e9-f42-laundered.jsonl"
    t2.write_bytes(laundered + b"\n")

    got2 = sweep.read_cwd(t2)
    assert got2 is None, (
        f"AC-F4.2: a cwd whose bytes fail strict UTF-8 must never be returned — "
        f"errors='replace' would launder it into the U+FFFD path {got2!r}")


# --------------------------------------------------------------------------- #
# AC-F4.3 — a usable cwd BEFORE the broken bytes is still returned             #
# --------------------------------------------------------------------------- #
def test_ac_f4_3_valid_cwd_before_torn_multibyte_tail_is_returned(tmp_path):
    """AC-F4.3: valid cwd line, then a crash-torn tail that ends mid-multi-byte
    sequence (lone 0xC3, no newline). The stream returns at the first hit, so the
    cwd survives. Kills the overcorrected strict implementation (one whole-buffer
    strict ``open``): the file is far smaller than a decoder chunk, so the torn
    tail sits in the same read as line 1 and a whole-buffer decode raises before
    yielding anything — losing today's green behavior."""
    root = tmp_path / "transcripts" / "-Users-e9-torn"
    root.mkdir(parents=True)
    good = json.dumps(
        {"type": "user", "uuid": "e9-f43-u1", "cwd": "/repo/torn-tail",
         "message": {"role": "user", "content": "before the tear"}}).encode("utf-8")
    torn = (b'{"type":"assistant","uuid":"e9-f43-a1","message":{"role":"assistant",'
            b'"content":[{"type":"text","text":"caf\xc3')  # SIGKILL mid-char, no \n
    t = root / "e9-f43-torn.jsonl"
    t.write_bytes(good + b"\n" + torn)

    assert sweep.read_cwd(t) == "/repo/torn-tail", (
        "AC-F4.3: a usable cwd BEFORE the broken bytes is still returned — the "
        "stream must return at the first hit instead of decoding the torn tail")
