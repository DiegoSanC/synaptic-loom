"""Orphan-transcript sweep (PHASE-2-3-SPEC R2.1) — FROZEN INTERFACE (Loom phase 2).

Sessions that die without a clean SessionEnd (crash, SIGKILL, WSL shutdown) never get
a marker. The sweep walks the transcripts on disk, compares them against the pointer
state and the queue, and enqueues a **synthetic marker** (hook-marker schema plus
``"origin": "sweep"``) for any transcript with unprocessed content that has settled
(mtime older than ``cfg.sweep_settle_s``).

Concurrency invariant (AC-2.1d): the sweep NEVER takes its own lock and never runs
outside one — it is invoked by ``processor.run_once`` while the existing ``flock`` is
held, so it cannot race another processor.

That lock does NOT cover the hook: ``sessionend.sh`` writes markers into the very same
queue **without taking it** (it must never block a session's shutdown), so it is an
unlocked concurrent writer and every queue write here has to be safe against one — a
temporary name that cannot collide with the hook's, and a publish step that fails
instead of overwriting when the marker already exists.

Frozen contract (bootstrap delta; epic E1 fills the bodies, never changes the shapes):

- ``run_sweep(cfg, state) -> SweepReport`` — one idempotent pass. ``state`` is the
  already-loaded ``state.json`` dict (the caller owns loading it). Writes markers
  atomically into ``cfg.queue_dir`` (never overwrites an existing marker for the same
  session), records the pass in ``cfg.sweep_last_run_path``, logs via the processor's
  logging convention.
- ``read_cwd(transcript_path) -> str | None`` — the session ``cwd`` derived from the
  transcript JSONL content (lines carry ``cwd``), NOT from the encoded directory name
  (AC-2.1c). ``None`` if no line carries one (the marker is then not enqueued; the
  transcript is retried on the next sweep).
- Non-goals here: DROPPING anything (every drop decision stays processor-owned, made
  when the synthetic marker is processed) and summarizing (the processor pipeline owns
  it). Since phase 4 the sweep does run an eligibility PRECHECK (P3.F2) at the point it
  would otherwise enqueue — read-only, off the registry cache the processor maintains,
  never over the network — but it only ever WITHHOLDS a marker, and re-decides from
  scratch on the next pass.
"""
from __future__ import annotations

import json
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

# Cycle by design: ``processor`` imports ``sweep`` to invoke it under its lock, and
# ``sweep`` reuses the processor's atomic-write/logging conventions. Only the MODULE is
# bound here; its attributes are read at call time, so neither half needs the other to
# be fully initialized at import time.
from loom import coverage_cache, processor, transcript_adapter
from loom.config import Config

# The transcript's file stem becomes the ``session_id`` AND a file name in ``queue/``:
# it is validated instead of trusted (no separators, no traversal, no dot-files, no
# control characters). Real session ids are uuids, so this rejects nothing legitimate.
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# What the synthetic marker adds on top of the hook's schema (AC-2.1d).
SWEEP_REASON = "sweep"
SWEEP_ORIGIN = "sweep"

# Maximum synthetic markers per pass, and the pass STOPS SCANNING once it is reached.
#
# Without it the first sweep after an upgrade is an unbounded backfill: no historical
# transcript has a pointer in state.json and all of them are long settled, so a single
# pass would enqueue every transcript on the machine (hundreds, hundreds of MB) and
# ``run_once`` would then summarize them one by one **inside the flock it holds** —
# hours during which every hook kick just finds the lock taken. Capping bounds both the
# lock hold and the per-pass read cost; successive passes drain the rest, newest first
# (see :func:`_iter_transcripts`), so the sessions a human actually remembers come back
# first. 10 keeps a typical pass to a few minutes while draining a few hundred
# transcripts within a day at the 15-minute cadence.
MAX_ENQUEUE_PER_PASS = 10


@dataclass
class SweepReport:
    """Result of one sweep pass (surfaced by ``loom process`` / ``loom status``).

    - ``scanned``: transcripts examined.
    - ``enqueued``: synthetic markers written this pass.
    - ``skipped``: transcripts with nothing to do (no new content, not settled,
      already queued, no cwd derivable yet, or withheld as ineligible).

    The rest are the ADDITIVE skip-class counters (P3.F3, seam
    ``p4-sweep-last-run-additive``). ``skipped`` keeps its meaning verbatim — the
    classes only say WHY, and they partition it: every skipped candidate falls in
    exactly one of ``stat_skips`` / ``cache_hits`` / ``ineligible`` / the unnamed
    residue (settle, already queued, parked, no new content, no cwd). Inside a bare
    ``skipped`` a one-syscall skip and a full-file read look identical, so nobody on
    the pilot host can tell whether the economics work at all.

    - ``stat_skips``: skipped by the delivery pointer's stat 4-tuple (P2.F4).
    - ``cache_hits``: skipped on the read-coverage cache's answer, with zero opens
      (P3.F1) — the class the RUNBOOK §9 residual moves into.
    - ``ineligible``: withheld by the enqueue precheck (P3.F2). NOT a slot.
    - ``not_scanned``: candidates the cap left unexamined. Deliberately NOT part of
      ``skipped``: they were never looked at, so they were never skipped either.
    """

    scanned: int = 0
    enqueued: int = 0
    skipped: int = 0
    enqueued_sessions: list[str] = field(default_factory=list)
    stat_skips: int = 0
    cache_hits: int = 0
    ineligible: int = 0
    not_scanned: int = 0


@dataclass
class _Pass:
    """Everything one pass carries across its candidates (never across passes).

    Deliberately per-pass and in memory only. ``registry`` is re-read from disk at the
    top of every pass and the eligibility answer is recomputed from it every time, so
    there is NOWHERE for a tombstone to live: registering a project makes its
    historical orphans deliverable on the very next pass, with no manual intervention
    (AC-F2.2). ``remotes`` memoizes only within the pass, for cost.
    """

    #: transcript_path -> frozen coverage entry (see :mod:`loom.coverage_cache`).
    cache: dict = field(default_factory=dict)
    #: Registered canonical remotes, or ``None`` when the precheck is DISABLED
    #: (registry cache absent/unparseable → fail open, exactly today's behavior).
    registry: set[str] | None = None
    #: cwd -> canonical remote (or ``None``), memoized for this pass only. Many
    #: transcripts share a cwd and the ineligible corpus is unbounded, so without
    #: this the precheck would replace the read cost we are removing with a fork
    #: cost — one ``git`` subprocess per candidate, every pass.
    remotes: dict[str, str | None] = field(default_factory=dict)
    counters: dict[str, int] = field(
        default_factory=lambda: {"stat_skips": 0, "cache_hits": 0, "ineligible": 0})
    #: Set when a full read completed, i.e. when the cache has something new to save.
    dirty: bool = False


def read_cwd(transcript_path: str | Path) -> str | None:
    """Session ``cwd`` from the transcript's JSONL lines (see the frozen contract).

    Streams the file and returns the first usable top-level ``cwd``; blank, malformed
    and non-object lines are skipped (a transcript from a crashed session may end
    mid-line). Nothing is ever derived from the encoded directory name (AC-2.1c).

    Decoding is STRICT (P3.F4), aligning with the adapter's policy that broken bytes
    are never silently laundered into parsed content. ``errors="replace"`` used to turn
    an invalid byte into U+FFFD, which has two failure modes that both end in the
    remote gate: a bad byte anywhere earlier in the file could be laundered into
    valid-looking JSON so the stream sailed past it, and a bad byte inside the ``cwd``
    VALUE could be handed back as a real-looking path with a replacement character in
    it. A ``UnicodeDecodeError`` is therefore treated exactly like an ``OSError`` is
    today — return ``None``, "no cwd derivable yet" — and the transcript is retried on
    a later pass, once the tail settles or heals.

    Read as BYTES and decoded per line, not through a text-mode handle: a text handle
    decodes a whole buffer at a time, so a torn multi-byte tail (SIGKILL mid-character)
    would raise before yielding line 1 and lose today's green behavior. Line by line,
    a usable cwd found BEFORE the broken bytes is still returned (AC-F4.3).

    Signature and semantics are frozen (``p4-read-cwd-strict``), which is why the
    "could I read it at all?" half lives in :func:`_read_cwd_checked` instead of being
    smuggled into this return value.
    """
    return _read_cwd_checked(Path(transcript_path))[1]


def _read_cwd_checked(path: Path) -> tuple[bool, str | None]:
    """``(read_completed, cwd)`` — :func:`read_cwd` plus the fact it cannot express.

    ``read_cwd`` returns ``None`` for two situations the CACHE must never confuse:

    - "this file completed a read and no line carried a cwd" — a true, cacheable fact,
      and precisely the RUNBOOK §9 residual class the cache exists to stop re-reading;
    - "I could not read this file" — a transient I/O failure (a chmod racing the pass,
      ``EIO``/``ESTALE`` on a network-mounted transcripts root, fd pressure), which
      says nothing about the file's contents.

    Recording the second as ``cwd: null`` is silent, permanent loss: the entry claims
    the file has no cwd, the stat is unchanged so nothing invalidates it, and the next
    pass answers from the cache with zero opens — forever, counted as a healthy cache
    hit. Before the coverage cache existed the next pass simply re-read and enqueued.

    ``UnicodeDecodeError`` deliberately reports ``(True, None)``: undecodable bytes are
    a property of the FILE, not of our ability to read it, so that ``None`` is a true
    fact about these bytes and is safe to cache. It self-heals — when the writer heals
    the tail the stat moves and the entry is invalidated.
    """
    try:
        with path.open("rb") as fh:
            for raw in fh:
                try:
                    line = raw.decode("utf-8").strip()
                except UnicodeDecodeError:
                    # A completed read over bytes we refuse to launder. Never "skip the
                    # line and keep going" — the bytes after a tear are not trustworthy
                    # content (the adapter's policy, P3.F4).
                    return True, None
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                cwd = obj.get("cwd")
                if isinstance(cwd, str) and cwd.strip():
                    return True, cwd
    except OSError:
        # Vanished/unreadable transcript: same outcome as "no cwd yet" — skip and retry
        # next pass. The caller must NOT cache anything derived from this.
        return False, None
    return True, None


def run_sweep(cfg: Config, state: dict) -> SweepReport:
    """One idempotent sweep pass under the caller's lock (see the frozen contract).

    Bounded by :data:`MAX_ENQUEUE_PER_PASS`: the pass stops scanning as soon as the cap
    is reached and the rest of the backlog is left for the next pass.
    """
    report = SweepReport()
    cfg.queue_dir.mkdir(parents=True, exist_ok=True)

    cache_path = coverage_cache_path(cfg)
    pass_state = _Pass(
        cache=coverage_cache.load(cache_path),
        registry=_registry_remotes(cfg),
    )

    candidates = _iter_transcripts(Path(cfg.sweep_transcripts_root))
    for index, (path, mtime) in enumerate(candidates):
        if report.enqueued >= MAX_ENQUEUE_PER_PASS:
            report.not_scanned = len(candidates) - index
            break
        report.scanned += 1
        if _sweep_one_guarded(cfg, state, path, mtime, report, pass_state):
            report.enqueued += 1
        else:
            report.skipped += 1

    report.stat_skips = pass_state.counters["stat_skips"]
    report.cache_hits = pass_state.counters["cache_hits"]
    report.ineligible = pass_state.counters["ineligible"]

    # After the loop, and never inside it: the cache is pure, so one write per pass is
    # both cheaper and exactly as correct as one per entry. A crash before this point
    # costs the next pass some re-reads and nothing else.
    _persist_coverage(pass_state, candidates, cache_path)
    _write_last_run(cfg, report)
    # The skip classes, on the line that already existed (P2.F4 amendment 2026-08-10,
    # extended by P3.F3). Without them the pilot host cannot tell a working sweep from
    # a broken one: a stat pair that silently never matches, a coverage cache that
    # never hits and an eligibility precheck that withholds EVERYTHING all look exactly
    # like "nothing to sweep" inside a bare `skipped`. The existing four fields keep
    # their names, order and spelling verbatim — the wave-e goal pins `stat_skips=`
    # here. Counts only, like every other column: never a path beyond what the sweep
    # already logs, never transcript content (I5/I6).
    detail = (
        f"scanned={report.scanned} enqueued={report.enqueued} "
        f"skipped={report.skipped} stat_skips={report.stat_skips} "
        f"cache_hits={report.cache_hits} ineligible={report.ineligible}"
    )
    if report.not_scanned:
        # Visible backlog: this pass hit the cap, so N transcripts were not even looked
        # at. They are picked up by the next passes (newest first).
        detail += f" capped={MAX_ENQUEUE_PER_PASS} not_scanned={report.not_scanned}"
    processor._log(cfg, "sweep", "sweep_pass", detail)
    return report


# --------------------------------------------------------------------------- #
# One transcript                                                               #
# --------------------------------------------------------------------------- #
def _sweep_one_guarded(
    cfg: Config, state: dict, path: Path, mtime: float, report: SweepReport,
    pass_state: _Pass,
) -> bool:
    """:func:`_sweep_one`, but one candidate can never take the pass down with it.

    The blast radius of an escaping exception here is the whole pass, not the
    candidate: ``processor.run_once`` isolates the sweep, so the loop stops, the
    coverage cache is never saved, ``sweep_last_run.json`` is never written (doctor
    then reads the pass as one that never ran), and every candidate behind this one is
    left unexamined. The candidate order is deterministic, so it would fail at exactly
    the same file on every future pass — a permanently unswept tail.

    Skipping the candidate instead costs one transcript one pass, and it is retried
    from scratch on the next one: nothing here is remembered. The failure is logged
    because a swallowed exception that leaves no trace is indistinguishable from a
    healthy "nothing to do". Type name and flattened path only — never the exception's
    message, which can quote content the sweep must not log (I5/I6).

    ``AssertionError`` is deliberately NOT contained. Nothing in this package raises
    one as a runtime failure mode, but the test suite raises it as instrumentation —
    the read-guards around the stat skip replace ``_line_count`` with a function that
    asserts it must not be called. Swallowing that would turn every one of those
    guards into a test that passes whether or not the read happened, which is the
    exact class of invisible regression this epic exists to prevent. A genuine
    ``AssertionError`` from a dependency is a programming error and should be loud.
    """
    try:
        return _sweep_one(cfg, state, path, mtime, report, pass_state)
    except AssertionError:
        raise
    except Exception as exc:
        processor._log(cfg, "sweep", "sweep_candidate_failed",
                       f"{type(exc).__name__} {_one_line(str(path))}")
        return False


def _sweep_one(
    cfg: Config, state: dict, path: Path, mtime: float, report: SweepReport,
    pass_state: _Pass | None = None,
) -> bool:
    """Enqueues a synthetic marker for ``path``; ``False`` if there is nothing to do.

    Checks run cheapest-first: name → already queued → parked → settled → unchanged
    stat pair → read coverage (cached or read) → new content → cwd → eligible. Only the
    coverage step can open the transcript, which is why both stat comparisons sit above
    it. ``state`` is READ-ONLY here: pointers advance only in the normal pipeline, after
    a confirmed push/NO_ENTRY/quarantine (I2/I6), and the read-coverage cache is a
    separate file that never touches it.

    ``pass_state`` carries the pass's coverage cache, registry snapshot, per-cwd remote
    memo and skip-class tally, all owned by :func:`run_sweep`. Optional so the decision
    logic stays callable — and testable — without it; a default :class:`_Pass` is an
    empty cache and a disabled precheck, i.e. exactly the pre-phase-4 behavior.
    """
    ps = pass_state if pass_state is not None else _Pass()
    session_id = path.stem
    if not _SAFE_SESSION_ID.match(session_id):
        processor._log(cfg, "sweep", "sweep_skipped_unsafe_name", repr(path.name)[:120])
        return False

    marker_path = cfg.queue_dir / f"{session_id}.json"
    if marker_path.exists():
        # NEVER overwrite: a hook marker may carry a fresher cwd/reason.
        return False

    if (cfg.failed_dir / f"{session_id}.json").exists():
        # Parked after MAX_ATTEMPTS (P2.F2): re-enqueuing it would close the loop
        # enqueue → fail → park → enqueue, and the failure is not transient. One
        # `exists()`, deliberately BEFORE `_line_count`, so a parked session costs
        # no transcript read at all (AC-F2.2). A human clears failed/ to retry.
        return False

    if time.time() - mtime < cfg.sweep_settle_s:
        return False  # still alive: the settle heuristic, Q-2.2.

    transcript_path = str(path)
    if _stat_unchanged(state, transcript_path, path):
        # P2.F4: the transcript is byte-for-byte the file the pointer's read covered,
        # so _line_count would spend a full read to reach the conclusion the pointer
        # already states.
        #
        # On the ordering, precisely — do NOT read a safety property into it. The
        # queued-marker, failed/ and settle guards above are pure short-circuits that
        # `return False` exactly like this one does, so permuting this check with them
        # cannot change what _sweep_one DECIDES; it changes only what the decision
        # COSTS. Concretely:
        # - COST is why it sits last among the cheap checks: those three are two
        #   `exists()` calls and an in-memory comparison, while this one is a `stat`
        #   syscall, so answering from the cheaper ones first spends less on the common
        #   "nothing to do" outcome.
        # - CORRECTNESS gives exactly ONE ordering constraint, and it is the line below:
        #   this check must stay ABOVE `_line_count`, which is a full read of the file.
        #   That is the whole of P2.F4, and the only ordering here a change can break.
        ps.counters["stat_skips"] += 1
        return False

    # P3.F1: the pointer could not answer — either there is no pointer (the transcript
    # never delivered) or it carries no stat fields. THAT is the RUNBOOK §9 residual:
    # a transcript nothing will ever publish has no record anywhere that it was read,
    # so today it is read in full on every pass, forever. The read-coverage cache is
    # the sweep's own answer, and it is consulted only on an ALL-FOUR stat match.
    #
    # `lstat`, not `stat`: `_iter_transcripts` already established this is a regular
    # file, and never following a symlink keeps a path swapped under us from being
    # answered with the target's identity.
    try:
        st = path.lstat()
    except OSError:
        return False  # vanished between the walk and here: not ours to sweep.

    entry = coverage_cache.lookup(ps.cache, transcript_path, st)
    if entry is not None:
        cached = True
        total_lines = entry.get("line_count")
        cwd = entry.get("cwd")
    else:
        cached = False
        total_lines = _line_count(path)
        # Both halves of the read, unconditionally — the cwd too, even when the line
        # count already proves there is nothing new. A half-filled entry would be the
        # silent-corruption bug this epic exists to avoid: a later pass hitting it
        # would read `cwd: null` as "no cwd derivable" and skip a transcript that has
        # one, with no read to correct it and no stat change to invalidate it. One
        # extra open, once per file, buys an entry that is true.
        #
        # `_read_cwd_checked`, not `read_cwd`: the entry must be TRUE, not merely
        # complete. These are two separate opens, and the second can fail on its own
        # (a chmod racing the pass, EIO/ESTALE on a network-mounted root, fd
        # pressure). `read_cwd` reports that failure as the same `None` it reports for
        # "this file has no cwd", so recording it would assert something the read
        # never established — and with the stat unchanged, nothing would ever correct
        # it. When either half fails, nothing is recorded and the next pass re-reads,
        # exactly as it did before this cache existed.
        read_ok, cwd = _read_cwd_checked(path) if total_lines is not None \
            else (False, None)
        if total_lines is not None and read_ok:
            # A COMPLETED read — the write condition, with no delivery semantics in it
            # (AC-F1.2). `st` was taken before the read, so a writer appending during
            # it leaves an entry describing the older file: it will not match next
            # pass and the transcript is re-read (the safe direction).
            coverage_cache.record(ps.cache, transcript_path, st, total_lines, cwd)
            ps.dirty = True

    if total_lines is None or total_lines <= _last_line(state, transcript_path):
        if cached:
            ps.counters["cache_hits"] += 1
        return False  # nothing the normal pipeline has not already consumed.

    if not cwd:
        if cached:
            ps.counters["cache_hits"] += 1
        return False  # retried next pass, once the session has written a cwd.

    if not _is_eligible(cwd, ps):
        # P3.F2, and the ONLY place the precheck runs: the sweep would otherwise
        # enqueue right here. Withheld, not dropped — no marker, no state mutation, and
        # deliberately NOT `report.enqueued`, so it consumes no MAX_ENQUEUE_PER_PASS
        # slot and does not stop the scan. That is the whole feature: today the ten
        # newest non-delivering transcripts eat all ten slots every pass and a
        # registered project's older transcript starves forever (wave-f review).
        # Every drop stays processor-owned.
        ps.counters["ineligible"] += 1
        # L-1 (contract/ATTRIBUTION.md §6) — name the ONE ineligible class that is data
        # loss rather than routine. "Resolvable but not registered" is the expected
        # majority and stays quiet; "not resolvable at all" is a session stranded
        # forever, and until this line its only trace was a counter nobody reads as loss.
        # The memo is already populated by `_is_eligible`, so this costs no fork. The
        # line REPEATS every pass by construction (the verdict carries no tombstone, which
        # is what lets a late registration recover ordinary orphans) — a recurring line is
        # the intended trade against a silent one.
        if ps.remotes.get(cwd) is None:
            processor._log(cfg, session_id, "sweep_unresolvable_cwd", _one_line(cwd))
        return False

    marker = {
        "session_id": session_id,
        "transcript_path": transcript_path,
        "cwd": cwd,
        "reason": SWEEP_REASON,
        "marked_at": processor._now_iso(),
        "origin": SWEEP_ORIGIN,
    }
    if not _enqueue_marker(cfg, marker_path, marker):
        return False  # already marked, or the queue would not take it: retry next pass.

    report.enqueued_sessions.append(session_id)
    processor._log(cfg, session_id, "sweep_enqueued", _one_line(transcript_path))
    return True


# --------------------------------------------------------------------------- #
# Read coverage (P3.F1) and enqueue eligibility (P3.F2)                        #
# --------------------------------------------------------------------------- #
def coverage_cache_path(cfg: Config) -> Path:
    """The sweep-owned cache file: ``state.json``'s directory (seam p4-coverage-cache).

    DERIVED, never configured: this is an internal, rebuildable artifact of the sweep,
    and a config key for it would be a knob whose only correct value is this one. It
    sits beside ``state.json`` because that is where LOOM_HOME's per-host mutable state
    lives — beside it, never inside it.
    """
    return cfg.state_path.parent / "coverage_cache.json"


def _registry_remotes(cfg: Config) -> set[str] | None:
    """Registered canonical remotes from the project-registry cache, READ-ONLY.

    ``None`` means "precheck DISABLED (fail open)" and is the answer for an absent or
    unparseable cache. That default is load-bearing in two directions:

    - fail-open is today's behavior verbatim (enqueue; the processor decides the drop),
      so a host that has never talked to the server keeps working;
    - reading garbage as an EMPTY registry instead would silently withhold every
      transcript on the machine — the sweep would go quiet and look healthy.

    Present-and-parseable is the WHOLE condition. Freshness deliberately plays no part:
    ``processor._load_registry`` refreshes over the network on a TTL miss, and the
    sweep must never do network I/O (it runs inside the processor's flock), never
    refresh, and never rewrite this file. A stale registry is still the best answer
    available, and the processor re-derives the real decision when it picks the marker
    up. ``_remotes_of`` is imported, not copied, so the sweep and the processor cannot
    drift on what "registered" means.
    """
    try:
        raw = cfg.cache_path.read_bytes()
    except OSError:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            return None
        projects = data.get("projects")
        if not isinstance(projects, list):
            return None
        # Members are filtered down to the shape ``_remotes_of`` can actually consume,
        # BEFORE it sees them — the INPUT is narrowed, the derivation is still the
        # processor's, so the two cannot drift on what "registered" means.
        #
        # The ``canonical_remote`` type check is load-bearing, not defensive padding:
        # ``_remotes_of`` builds a SET, so a value that is a JSON array or object is
        # unhashable and raises ``TypeError`` from inside the comprehension. This
        # function runs before the first candidate is scanned, so that raise used to
        # end the pass with zero transcripts examined and no record written — the one
        # outcome the seam forbids ("absent/unparseable → precheck DISABLED"). And the
        # shape is not hypothetical: this cache is written from the server's
        # ``resp.json()``, so it is remote-controlled.
        return processor._remotes_of(
            [p for p in projects
             if isinstance(p, dict) and isinstance(p.get("canonical_remote"), str)])
    except Exception:
        # Same doctrine as the cache file's "corrupt ≡ absent", applied to the whole
        # parse: a registry this function cannot fully understand is an unparseable
        # one, and an unparseable registry disables the precheck. Never fatal — the
        # sweep must degrade to today's behavior, not stop sweeping.
        return None


def _is_eligible(cwd: str, ps: _Pass) -> bool:
    """Would the processor be able to deliver this transcript's project TODAY?

    Answered from the registry snapshot this pass read and the cwd's git remote —
    nothing else, and nothing remembered. There is deliberately no tombstone: an
    ineligible verdict is recomputed on every pass over the same unchanged file, so
    registering a project (which the processor writes into the registry cache) makes
    its historical orphans deliverable on the next pass with no manual intervention.
    """
    if ps.registry is None:
        return True  # precheck disabled: fail open, exactly today's behavior.
    canonical = _canonical_remote(cwd, ps.remotes)
    return canonical is not None and canonical in ps.registry


def _canonical_remote(cwd: str, memo: dict[str, str | None]) -> str | None:
    """Canonical remote of ``cwd`` (``None``: no repo, no remote, not normalizable).

    Memoized per pass on the exact cwd string. The derivation forks ``git``, and a
    machine's ineligible corpus is unbounded and heavily shares cwds (every session of
    one project) — without the memo the precheck would trade the per-pass READ cost
    this epic removes for a per-pass FORK cost, which is worse.

    Both helpers come from the processor by import: the sweep's "would this deliver?"
    must be the same question the processor's gate asks, and a copy would drift.

    The derivation is wrapped WHOLE, and broadly, because "no remote derivable" is
    already a first-class answer here (it means ineligible) while an escaping exception
    is not survivable: this runs inside the per-candidate loop, and ``run_once``
    isolates the sweep, so a raise aborts the whole PASS — no record, no cache save,
    and every candidate behind this one in the deterministic newest-first order is
    never examined. It would die on the same candidate every pass, forever.

    ``_git_remote`` is not as total as it looks. Two confirmed escapes:

    - ``Path.is_dir()`` re-raises an ``OSError`` whose errno is outside its swallow
      list, so ``EACCES``/``EPERM``/``ENAMETOOLONG`` come through — and macOS TCC
      answers ``EPERM`` for ``~/Documents``, ``~/Desktop``, ``~/Downloads`` and iCloud
      Drive to a process without Full Disk Access, which is exactly a cron/launchd-run
      ``loom``.
    - ``subprocess.run(..., text=True)`` raises ``UnicodeDecodeError`` while decoding
      the child's output, which ``_git_remote``'s ``(OSError, TimeoutExpired)`` does
      not catch: a ``.git/config`` with non-UTF-8 bytes in the origin URL is enough.

    The ``None`` is memoized like any other answer, so a pathological cwd costs one
    failed derivation per pass rather than one per candidate.
    """
    if cwd in memo:
        return memo[cwd]
    canonical: str | None = None
    try:
        # bounded, list-arg subprocess; never a shell.
        url = processor._git_remote(cwd)
        if url:
            canonical = processor.normalize(url)
    except Exception:
        # Deliberately broad, deliberately not BaseException: KeyboardInterrupt and
        # SystemExit must still stop the process. Everything else resolves to the
        # answer that withholds the marker — the processor re-derives the real decision
        # if the transcript is enqueued on a later pass.
        canonical = None
    memo[cwd] = canonical
    return canonical


def _persist_coverage(ps: _Pass, candidates: list[tuple[Path, float]],
                      cache_path: Path) -> None:
    """Saves the pass's coverage, dropping entries for transcripts that are gone.

    The prune keeps the file proportional to the transcripts that exist rather than to
    every transcript that ever existed. It is safe precisely because the cache is
    lossy: a wrongly dropped entry costs one re-read and nothing else. It is skipped
    entirely when the pass listed no candidates at all (an unmounted or misconfigured
    transcripts root), because wiping the whole cache on a transient empty listing
    would cost a full re-read of the machine.
    """
    if not candidates:
        return
    live = {str(path) for path, _ in candidates}
    stale = [key for key in ps.cache if key not in live]
    for key in stale:
        del ps.cache[key]
    if stale or ps.dirty:
        coverage_cache.save(ps.cache, cache_path)


# --------------------------------------------------------------------------- #
# Filesystem helpers                                                           #
# --------------------------------------------------------------------------- #
def _iter_transcripts(root: Path) -> list[tuple[Path, float]]:
    """``(path, mtime)`` of the ``*.jsonl`` regular files under ``root``, NEWEST FIRST.

    Newest-first is what makes :data:`MAX_ENQUEUE_PER_PASS` usable: on the first sweep
    after an upgrade every historical transcript is a candidate, and the ones a human
    might still act on are the recent ones — they must not sit behind a year of
    archaeology. ``lstat`` also settles "is it a regular file?" in the same syscall, so
    symlinks (files and directories alike) are never swept and the sweep cannot be
    pointed at a transcript outside the configured root.
    """
    if not root.is_dir():
        return []
    try:
        candidates = sorted(root.rglob("*.jsonl"))
    except OSError:
        return []

    found: list[tuple[Path, float]] = []
    for path in candidates:
        try:
            st = path.lstat()
        except OSError:
            continue  # vanished between the walk and the stat: not ours to sweep.
        if not stat.S_ISREG(st.st_mode):
            continue
        found.append((path, st.st_mtime))
    found.sort(key=lambda item: (-item[1], str(item[0])))
    return found


def _line_count(path: Path) -> int | None:
    """Number of lines, counted exactly like ``transcript_adapter.read_delta`` does.

    Streamed (a transcript can be large) and compared against the pointer's
    ``last_line``, so both sides speak the same units — including the trailing-hold
    (P2.F1 rule 5): a torn last line is NOT countable, because ``read_delta`` parks
    the pointer before it. Counting it would leave ``line_count > last_line`` forever
    on a session that never resumes, and this sweep would re-enqueue it every pass.

    Interior tears ARE counted: ``read_delta`` consumes them, so the pointer passes
    them and the two sides stay equal.

    Cost: the common case (a healthy tail) classifies exactly one line — the rare
    torn-tailed file pays a second pass to find the last countable line.
    """
    total = 0
    last_raw = b""
    try:
        with path.open("rb") as fh:
            for raw in fh:
                total += 1
                last_raw = raw
    except OSError:
        return None
    if total and transcript_adapter.is_torn_line(last_raw):
        return _count_before_trailing_tear(path)
    return total


def _count_before_trailing_tear(path: Path) -> int | None:
    """Lines up to and including the LAST non-torn one (0 if every line is torn)."""
    countable = 0
    try:
        with path.open("rb") as fh:
            for idx, raw in enumerate(fh):
                if not transcript_adapter.is_torn_line(raw):
                    countable = idx + 1
    except OSError:
        return None
    return countable


def _stat_unchanged(state: dict, transcript_path: str, path: Path) -> bool:
    """Is the transcript byte-identical to the file the pointer's read covered?

    ``True`` only when the entry carries the frozen fields (p3-state-stat-fields) AND
    every field it carries still matches the file on disk. Those fields were captured by
    :func:`processor._advance_pointer` from a stat taken BEFORE the delta read the
    pointer came from, which is what makes the answer trustworthy: a writer appending
    during that read leaves the record describing the older file, so it does NOT match
    here and the transcript is re-read (wave-a's append race, closed in the safe
    direction).

    Two shapes are accepted, and the choice between them is per-entry (P2.F4 amendment,
    2026-08-10):

    - entry carries BOTH ``stat_dev`` and ``stat_ino`` → all FOUR fields must match.
      The inode half catches the class the mtime/size pair cannot see: a same-size
      replacement that preserves the mtime (``cp -p``, ``rsync -a``, ``tar -xp``, a
      backup restore, ``os.replace`` + ``utimensat``). Without it such a file compares
      equal and is skipped permanently — silent, unrecoverable loss of a whole tail.
    - entry carries NEITHER → the legacy two-field comparison, i.e. exactly the
      pre-amendment behaviour. Every entry already on disk was written that way, and
      invalidating them wholesale would mean a full re-read of every transcript on the
      machine after an upgrade for no correctness gain (they are the same inode). The
      amendment is therefore monotonic: everything the code writes from now on gets the
      stronger check, nothing already written gets weaker than it was.

    Everything else answers ``False`` — no entry, a missing or half-present identity
    pair, a field that is not an exact ``int`` (a hand-edited or half-migrated
    state.json), an unreadable stat. The cost of a wrong ``False`` is one wasted read;
    the cost of a wrong ``True`` is a session's tail lost forever, so every ambiguity
    resolves to re-reading.

    Comparing ``st_mtime_ns`` (integer nanoseconds) and not ``st_mtime`` is load-bearing:
    the float loses sub-second precision, so two writes in the same second can compare
    equal. ``type(...) is int`` and not ``isinstance``: ``True`` is an ``int`` and must
    not be read as a timestamp of 1 — or as inode 1.

    Note this does NOT mean "fully processed": with E5's trailing-torn-hold the pointer
    legitimately sits before EOF on a file whose tail is a half-written line. The record
    covers exactly the read that produced the pointer, so an unchanged torn file is
    correctly skipped (it would otherwise be re-read every pass forever), and the moment
    the writer resumes, size and mtime move and the full check comes back.
    """
    entry = state.get(transcript_path)
    if not isinstance(entry, dict):
        return False
    recorded_mtime_ns = entry.get("stat_mtime_ns")
    recorded_size = entry.get("stat_size")
    if type(recorded_mtime_ns) is not int or type(recorded_size) is not int:
        return False
    # "Carries the identity" is keyed on the KEYS, not on their values: an entry that
    # mentions either field is claiming the strong shape, so a half-present or
    # non-``int`` pair is an ambiguity and resolves to the full check rather than
    # silently degrading to the legacy comparison.
    recorded_dev = entry.get("stat_dev")
    recorded_ino = entry.get("stat_ino")
    claims_identity = "stat_dev" in entry or "stat_ino" in entry
    if claims_identity and (type(recorded_dev) is not int
                            or type(recorded_ino) is not int):
        return False
    try:
        st = path.stat()
    except OSError:
        return False
    if st.st_mtime_ns != recorded_mtime_ns or st.st_size != recorded_size:
        return False
    if not claims_identity:
        return True  # pre-amendment entry: the legacy comparison, unchanged.
    return st.st_dev == recorded_dev and st.st_ino == recorded_ino


def _last_line(state: dict, transcript_path: str) -> int:
    """The pointer's ``last_line`` for a transcript; 0 if it has no entry yet."""
    entry = state.get(transcript_path)
    if not isinstance(entry, dict):
        return 0
    last_line = entry.get("last_line", 0)
    return last_line if isinstance(last_line, int) and last_line > 0 else 0


def _enqueue_marker(cfg: Config, marker_path: Path, marker: dict) -> bool:
    """Publishes the marker atomically, WITHOUT ever clobbering an existing one.

    Two properties, both against ``sessionend.sh`` — which writes this same queue
    without holding the lock, so nothing here may assume exclusivity:

    - the temporary file gets a UNIQUE name (:func:`processor._new_tmp_json`), never
      the hook's ``<session_id>.json.tmp``; sharing that path would let each writer
      truncate the other's in-flight file and delete the other's temp;
    - it is published with ``os.link``, which is atomic *and* fails outright if the
      destination already exists. There is deliberately NO ``os.replace`` fallback: any
      "check then replace" reintroduces the very race the link exists to close, and
      overwriting a hook marker would swap its payload-derived ``cwd`` for a
      transcript-derived one, which is what the remote gate keys off.

    A link failure is therefore never fatal and never destructive: the transcript is
    simply left for the next pass.
    """
    tmp = processor._new_tmp_json(marker_path.parent, marker)
    try:
        os.link(tmp, marker_path)
    except FileExistsError:
        return False  # the hook marked it between our check and here: its marker wins.
    except OSError as exc:
        # No hard links on this filesystem, permissions, ENOSPC… Log it (a queue that
        # silently never accepts markers would look exactly like "nothing to sweep")
        # and retry next pass.
        processor._log(
            cfg, marker.get("session_id", "sweep"), "sweep_enqueue_failed",
            f"{type(exc).__name__}: errno={exc.errno}",
        )
        return False
    finally:
        # Unconditional: with a unique name this only ever removes OUR temp, and after
        # a successful link the marker survives as the second name of the same inode.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return True


def _write_last_run(cfg: Config, report: SweepReport) -> None:
    """Records the pass at ``cfg.sweep_last_run_path`` (consumed by E2's doctor).

    ADDITIVE only (seam ``p4-sweep-last-run-additive``): the frozen four keep their
    exact keys, types and semantics, so a consumer that parses only them — doctor does
    — parses this byte-compatibly and cannot tell the difference. The new keys are the
    skip classes, which partition ``skipped``, plus ``not_scanned``, which does not:
    those candidates were never examined, so ``enqueued + skipped == scanned`` still
    holds. Counts only; no path, no session id, no content.
    """
    processor._atomic_write_json(
        cfg.sweep_last_run_path,
        {
            "finished_at": processor._now_iso(),
            "scanned": report.scanned,
            "enqueued": report.enqueued,
            "skipped": report.skipped,
            "stat_skips": report.stat_skips,
            "cache_hits": report.cache_hits,
            "ineligible": report.ineligible,
            "not_scanned": report.not_scanned,
        },
    )


def _one_line(text: str) -> str:
    """Collapses separators so a path read off disk cannot forge log columns.

    Only paths, session ids and counters are ever logged — never transcript content
    (I5/I6).
    """
    return re.sub(r"[\t\r\n]+", " ", text)[:200]
