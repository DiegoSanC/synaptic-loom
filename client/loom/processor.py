"""Decoupled processor (DESIGN.md §4.2, PLAN.md §T4).

Walks the markers in ``queue/`` (oldest first) and applies, to each one, the 9-step
pipeline: transcript exists → remote gate → registry cache → delta from the pointer →
summarizer (subprocess with no tools) → fail-closed secret scan → ``POST /api/entries``.
The pointer (``state.json``) advances **only** after a confirmed push, ``NO_ENTRY`` or
quarantine (I2/I6). A ``flock`` on ``lock`` serializes concurrent runs.

Invariants materialized here:
- **I1**: the summarizer runs with ``cwd = LOOM_HOME`` (never inside a project).
- **I3**: only this script talks to the API; the summarizer gets ``--tools ""`` (no net/shell).
- **I4**: if the secret scan fails or raises, it counts as a hit (quarantine).
- **I6**: reprocessing does not duplicate (pointer + unique ``content_hash`` server-side).

Normative behavior: the error matrix in PLAN.md §T4.
"""
from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from loom import auth, okf, projects_local, scan_engine, secret_scan, sweep
from loom.api_client import ApiClient, AuthRejected, ServerUnreachable
from loom.config import CAPTURE_MODE_OPT_IN, Config
from loom.normalize import normalize
from loom.transcript_adapter import AdapterError, read_delta, render_for_summarizer

# Number of summarizer failures after which the marker is parked in failed/ (P4).
MAX_ATTEMPTS = 3
# Exact summarizer output meaning "nothing to record" (DESIGN §5, rule 6).
NO_ENTRY = "NO_ENTRY"
# Timeout for the git remote command (seconds); resolving it must not hang the run.
GIT_TIMEOUT = 10

# --- Typed capture (T.F3, `t-label-grammar`) -------------------------------------
# The closed label vocabulary, re-exported from the module that owns the entry format:
# the same four literals drive the parse here, the `item_types` frontmatter and
# `extract_title`'s label tolerance, and three copies of a "closed" vocabulary is how
# it stops being closed.
ITEM_TYPES = okf.ITEM_TYPES
# What a bullet that does not match the grammar is typed as. An unlabeled or
# mislabeled item is still an item — dropping it would lose the capture, and failing
# the entry over a label would trade a whole session's summary for a formatting nit.
FALLBACK_ITEM_TYPE = "decision"
# The grammar, parsed HERE and never by the LLM: `- [<label>] ` at the head of the
# line, lowercase literals, trailing space included. Deliberately strict — a lenient
# match (case-insensitive, punctuation-tolerant) would type items from a model that
# did not follow the prompt, and a wrong type is worse than a counted fallback.
_LABELLED_BULLET_RE = re.compile(rf"^- \[({'|'.join(ITEM_TYPES)})\] ")
# What COUNTS as an item, as opposed to what gets typed. Deliberately wider than the
# grammar — `*`/`+`/`1.`/`1)` markers, any run of spaces after the marker — because a
# shape this does not recognize is not merely untyped: it is invisible, contributing
# neither a type nor a fallback, so a model that reshaped its bullets (or translated
# its labels: prompt rule 8 writes the prose in the session's language, and this
# pilot's sessions are Spanish) would read as "no drift" forever. Everything matched
# here that `_LABELLED_BULLET_RE` does not match is a counted fallback, which is the
# designed signal.
_ITEM_BULLET_RE = re.compile(r"^(?P<indent>[ \t]{0,3})(?:[-*+]|\d+[.)])[ \t]+\S")
# A bullet led by a bracketed token — `[método]`, `[banana]`, `[decision]`. Used ONLY
# to locate a Decisions section whose heading is not the English literal; see
# :func:`_decisions_section`. The token may not start with a space, so an `- [ ] `
# task checkbox in an "Open items" section can never be mistaken for a labeled item.
_BRACKET_LED_BULLET_RE = re.compile(
    r"^[ \t]{0,3}(?:[-*+]|\d+[.)])[ \t]+\[[^\]\s][^\]\n]{0,39}\]\s"
)
# Section boundary: an h2 heading. The v2 output format is a flat `## Context` /
# `## Decisions` / `## Open items`, so h2 is the only level that ever separates
# sections — and treating `#` as one too would let an evidence quote or a shell
# comment (`# contract`) at column 0 truncate the Decisions section mid-way.
_HEADING_RE = re.compile(r"^## ")
_DECISIONS_HEADING_RE = re.compile(r"^## Decisions\s*$")
# A fenced-code delimiter (``` or ~~~, up to 3 columns of indent). Fenced content is
# QUOTED, not structural: a session about editing the summarizer prompt quotes the
# output template, fences and all, and without this the quoted `## Decisions` example
# would win over the real one — the entry gets typed from an example.
_FENCE_RE = re.compile(r"^[ \t]{0,3}(```+|~~~+)")
# Prompt versions that carry the label grammar. An ALLOWLIST on purpose: an unknown
# version parsed as v2 would type every unlabeled bullet `[decision]` — data that
# looks right and is wrong, permanently, since `item_types` is written once at insert
# and there is no backfill route. Failing to UNTYPED is honest, visible, and still
# re-derivable from `content_md`. A future labeled version is one literal added here —
# as v3 was (D11) and v4 (D15): same four labels, same Decisions section, same fallback.
LABELLED_PROMPT_VERSIONS = frozenset({"v2", "v3", "v4"})


# --------------------------------------------------------------------------- #
# Result                                                                       #
# --------------------------------------------------------------------------- #
# Category of each outcome, for RunReport's aggregate counters.
_PROCESSED = "processed"   # an entry reached the server (201/409)
_SKIPPED = "skipped"       # marker resolved without transmitting (legit drop, NO_ENTRY, quarantine)
_ERRORS = "errors"         # marker kept because of a problem (to retry / needs intervention)

_CATEGORY: dict[str, str] = {
    "posted": _PROCESSED,
    "duplicate": _PROCESSED,
    "no_entry": _SKIPPED,
    "quarantined": _SKIPPED,
    "dropped_no_transcript": _SKIPPED,
    "dropped_no_remote": _SKIPPED,
    "dropped_not_registered": _SKIPPED,
    "dropped_not_enabled": _SKIPPED,   # registered, but THIS machine has not enabled it (D9)
    "dropped_empty_delta": _SKIPPED,
    "dropped_before_enable": _SKIPPED,  # every new turn predates the enable here (D9 amendment)
    "kept_server_unreachable": _ERRORS,
    "kept_adapter_error": _ERRORS,
    "kept_summarizer_failed": _ERRORS,
    "kept_pipeline_error": _ERRORS,
    "failed_parked": _ERRORS,
    "kept_network_error": _ERRORS,
    "kept_auth_rejected": _ERRORS,   # 401/403: the credential, not the network (issue #20)
    "kept_registry_error": _ERRORS,
    "error": _ERRORS,
}


@dataclass
class MarkerOutcome:
    """Pipeline result for one specific marker."""

    session_id: str
    action: str
    detail: str = ""


@dataclass
class RunReport:
    """Summary of one :func:`run_once` pass (consumed by ``loom process``/``status``, T6).

    ``sweep`` is additive (phase 2, R2.1): the orphan-sweep's counters when the sweep
    ran in this pass, ``None`` when it did not (disabled, lock held, or it crashed).
    """

    outcomes: list[MarkerOutcome] = field(default_factory=list)
    server_unreachable: bool = False
    lock_held: bool = False
    sweep: "sweep.SweepReport | None" = None

    def _count(self, category: str) -> int:
        return sum(1 for o in self.outcomes if _CATEGORY.get(o.action) == category)

    @property
    def processed(self) -> int:
        return self._count(_PROCESSED)

    @property
    def skipped(self) -> int:
        return self._count(_SKIPPED)

    @property
    def errors(self) -> int:
        return self._count(_ERRORS)


# --------------------------------------------------------------------------- #
# Time / state / cache / marker helpers (atomic I/O)                           #
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _new_tmp_json(directory: Path, data: object) -> Path:
    """Serializes ``data`` into a FRESH temporary file in ``directory``.

    The name must be unique, never derived from the destination: ``sessionend.sh``
    writes its markers as ``<session_id>.json.tmp`` **without taking the lock**, so a
    derived name lets the two writers truncate each other's in-flight file and delete
    each other's temp. ``mkstemp`` also never collides with ``*.json``, so a
    half-written file is invisible to :func:`_iter_markers` and ``loom status``.

    The data is ``fsync``ed before the fd closes. ``os.replace`` makes the swap atomic
    with respect to *readers*, not with respect to a power loss: without this the rename
    can reach the disk while the bytes it points at have not, and the file comes back
    truncated. See :func:`_atomic_write_json` for the other half.
    """
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=directory, prefix=".loom-", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:  # closes the fd it took over
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def _fsync_dir(directory: Path) -> None:
    """``fsync``s a directory so a rename inside it survives a power loss.

    Best-effort by construction: some filesystems reject opening or syncing a directory
    fd outright, and the write this protects has already succeeded — it is hardening, so
    it must never be able to turn a good pass into a failed one. Guarded exactly like
    :func:`_clear_failed_tombstone`'s cleanup, and for the same reason.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_json(path: Path, data: object) -> None:
    """Writes JSON atomically AND durably (unique tmp + fsync + ``os.replace`` + fsync).

    Atomicity alone is not enough for what this writes. ``state.json`` holds every
    transcript's pointer, and a host kill or a power loss — failure modes this codebase
    designs for throughout — can otherwise land the rename without the data. What comes
    back is a truncated file, :func:`_load_state` cannot parse it, and every pointer is
    lost at once: each session is then re-summarized from a DIFFERENT ``covered_from``,
    so with a different ``content_hash``, which the server's dedupe does NOT absorb —
    and since E6 the stat pairs go with them, so the next sweep re-reads everything too.

    The two syncs are not interchangeable: the data sync (in :func:`_new_tmp_json`) has
    to precede the rename, and the directory sync has to follow it — a rename is a
    directory operation, so syncing the file says nothing about the name reaching disk.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _new_tmp_json(path.parent, data)
    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def _set_aside_unparseable_state(cfg: Config) -> Path | None:
    """Moves an unparseable ``state.json`` to the first free ``state.json.corrupt-<n>``.

    Never overwrites an earlier one: each reset is its own piece of evidence. Returns
    the destination, or ``None`` if it could not be moved — this is diagnostics for a
    human reading the log afterwards, so failing to produce it must not stop the pass
    that is already recovering.
    """
    for n in range(1, 1000):
        target = cfg.state_path.with_name(f"{cfg.state_path.name}.corrupt-{n}")
        if target.exists():
            continue
        try:
            os.replace(cfg.state_path, target)
        except OSError:
            return None
        return target
    return None


def _load_state(cfg: Config) -> dict:
    """The pointer state, or ``{}`` when there is none that can be read.

    An unparseable file is NOT silent. Returning ``{}`` is the only safe thing to do
    with it, but it is also a catastrophic event — every pointer resets, every session
    is re-summarized from a different ``covered_from`` and so past the server's dedupe —
    and a silent catastrophe is undiagnosable after the fact. The event is logged and
    the bytes are kept aside as ``state.json.corrupt-<n>`` for a human. Only the event
    and the destination NAME are logged: the file itself holds transcript paths.
    """
    try:
        parsed = json.loads(cfg.state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        kept = _set_aside_unparseable_state(cfg)
        _log(cfg, "processor", "state_unparseable",
             f"every pointer reset; kept_as={kept.name if kept else 'none'}")
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _pointer(state: dict, transcript_path: str) -> tuple[int, str | None]:
    """``(last_line, last_uuid)`` for a transcript; ``(0, None)`` if it is new."""
    entry = state.get(transcript_path) or {}
    return entry.get("last_line", 0), entry.get("last_uuid")


def _stat_pair(transcript_path: str | Path) -> tuple[int, int, int, int] | None:
    """``(st_dev, st_ino, st_mtime_ns, st_size)`` of a transcript, or ``None``.

    Integers on purpose (p3-state-stat-fields): ``st_mtime`` is a float and loses
    sub-second precision, so two different writes inside the same second can compare
    equal — and "equal" is what makes the sweep skip a file forever.

    ``(st_dev, st_ino)`` is the P2.F4 amendment (2026-08-10) and is the same identity
    :func:`_identity_of` already spends on a *marker*. It catches the one class
    ``(st_mtime_ns, st_size)`` cannot see: a same-size replacement that preserves the
    mtime — ``cp -p``, ``rsync -a``, ``tar -xp``, a backup/Time-Machine restore, any
    ``os.replace`` followed by ``utimensat``. Those compare equal on the old pair, and
    the sweep then skips a *different file* forever. Losing a marker is recoverable (the
    session can be re-swept); losing a transcript's tail is not, so the transcript may
    not carry the weaker identity of the two.

    This is only ever called BEFORE the read whose pointer it will describe; see
    :func:`_advance_pointer`. ``None`` on any OSError: no fields are then recorded and
    the sweep falls back to the full check, which is the safe direction.
    """
    try:
        st = os.stat(transcript_path)
    except OSError:
        return None
    return st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size


def _advance_pointer(
    cfg: Config,
    state: dict,
    transcript_path: str,
    new_last_line: int,
    new_last_uuid: str | None,
    pre_read_stat: tuple[int, int, int, int] | None = None,
) -> None:
    """Advances the pointer and persists ``state.json`` atomically.

    ``new_last_uuid is None`` (a delta with no conversation) keeps the previous uuid: the
    position-in-file advances but the evidence anchor does not go backwards.

    ``pre_read_stat`` is the ``(st_dev, st_ino, st_mtime_ns, st_size)`` tuple taken by
    the caller **before** the ``read_delta`` this pointer came from
    (p3-state-stat-fields, P2.F4; dev/ino added by the 2026-08-10 amendment). It is what
    lets the sweep skip re-reading an unchanged transcript, so WHEN it was taken is the
    whole correctness argument:

    - PRE-read (here): a writer that appends while we are reading — the SIGKILL victim
      of the wave-a build review — leaves the recorded pair describing the file *before* its
      append. The next sweep sees a mismatch, re-reads, and the appended line lands.
      A stale record costs one wasted read; that is the safe direction.
    - POST-read (never): the recorded pair would describe the already-appended file,
      the sweep would consider it processed, and the raced line would be skipped
      forever — silent, permanent content loss.

    Timing is necessary but not sufficient, so the caller ALSO gates on the read having
    corroborated the pair (``Delta.bytes_read == st_size``): the stat and the read
    resolve the path independently, and an over-describing pair is the same permanent
    loss as a post-read capture. See the gate at the ``read_delta`` call site.

    ``None`` (the stat failed, the read did not corroborate it, or a caller that has no
    pre-read pair) records no fields at all: an entry without them never skips.
    """
    prev = state.get(transcript_path) or {}
    entry = {
        "last_line": new_last_line,
        "last_uuid": new_last_uuid if new_last_uuid is not None else prev.get("last_uuid"),
        "updated_at": _now_iso(),
    }
    if pre_read_stat is not None:
        # Additive fields, all four written together or none at all: the sweep decides
        # which comparison to run from what the entry carries, so a half-written entry
        # would be indistinguishable from a pre-amendment one.
        (entry["stat_dev"], entry["stat_ino"],
         entry["stat_mtime_ns"], entry["stat_size"]) = pre_read_stat
    state[transcript_path] = entry
    _atomic_write_json(cfg.state_path, state)


def _read_marker(path: Path) -> dict | None:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _identity_of(st: os.stat_result, raw: bytes) -> tuple:
    """The identity tuple of a marker, from a stat and the bytes it describes.

    Both halves earn their place: the stat identity catches an ``os.replace`` (new
    inode) and the digest catches a same-size rewrite under a coarse mtime clock.
    """
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size,
            hashlib.sha256(raw).hexdigest())


def _identify_marker(path: Path) -> tuple | None:
    """Identity of the marker file *as it is right now*, or ``None`` if unreadable.

    ``sessionend.sh`` writes this queue WITHOUT the lock (``phase1-marker-queue``
    freezes that), so a fresh marker can land on the same path while the processor
    is summarizing — minutes, in practice. Comparing this tuple before the drop is
    what stops :func:`_drop_marker` from deleting a marker it never read (P2.F3).
    """
    try:
        raw = path.read_bytes()
        st = path.stat()
    except OSError:
        return None
    return _identity_of(st, raw)


def _read_marker_with_identity(path: Path) -> tuple[dict | None, tuple | None]:
    """The marker AND its identity, both derived from ONE read of the file.

    Reading the marker twice — once to parse it, once to identify it — reopens the
    very race P2.F3 closed, one step earlier: the hook's unlocked ``os.replace`` can
    land BETWEEN the two reads, and the pass then holds the old marker's content
    stapled to the fresh marker's identity. Every identity check downstream then
    agrees that the file on disk is "ours", so the drop at the end of the pass
    deletes a capture that was never processed.

    One ``open`` pins the inode, so the bytes and the ``fstat`` provably describe the
    same file no matter what lands on the path afterwards.
    """
    try:
        with path.open("rb") as fh:
            raw = fh.read()
            st = os.fstat(fh.fileno())
    except OSError:
        return None, None
    identity = _identity_of(st, raw)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, identity
    return (parsed if isinstance(parsed, dict) else None), identity


def _write_marker(path: Path, marker: dict) -> None:
    """Rewrites a marker in place (attempts, adapter_error, registry_error).

    The flock does not serialize this against ``sessionend.sh``, which writes the same
    queue without taking it — hence the unique temporary name in
    :func:`_new_tmp_json`. The rename itself stays a plain ``os.replace``: this path
    UPDATES a marker the processor already owns, so overwriting is the intent.
    """
    _atomic_write_json(path, marker)


def _park_marker(cfg: Config, marker_path: Path) -> None:
    """Moves an unprocessable marker into ``failed/`` for operator recovery."""
    cfg.failed_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(marker_path), str(cfg.failed_dir / marker_path.name))


def _iter_markers(cfg: Config) -> list[Path]:
    """``queue/*.json`` markers sorted by ascending ``marked_at`` (oldest first)."""
    markers = [p for p in cfg.queue_dir.glob("*.json") if not p.name.endswith(".tmp")]

    def sort_key(p: Path):
        marker = _read_marker(p) or {}
        return (str(marker.get("marked_at") or ""), p.name)

    return sorted(markers, key=sort_key)


# --------------------------------------------------------------------------- #
# Logging (human-readable; never secrets or summary content)                   #
# --------------------------------------------------------------------------- #
def _log(cfg: Config, session_id: str, action: str, detail: str = "") -> None:
    line = f"{_now_iso()}\t{session_id}\t{action}"
    if detail:
        line += f"\t{detail}"
    try:
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        with cfg.log_file.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        # Diagnostics may disappear on a full or unwritable disk; they may never turn
        # an otherwise successful capture into a pipeline failure.
        pass


def _flatten(value: object) -> str:
    """Collapses every whitespace run to a single space (the existing path policy).

    Log lines are tab-separated columns and the attention record is read by humans
    next to them, so a path carrying a tab, a newline or a double space would either
    forge a column or forge a whole row. The same collapsing the ``held_lines_at_eof``
    line already applies, factored out so the record and the log cannot drift.
    """
    return " ".join(str(value).split())


# --------------------------------------------------------------------------- #
# Observability: tear-ratio escalation + attention record (P3.F5, REPORT-ONLY)  #
# --------------------------------------------------------------------------- #
# Everything below OBSERVES a delta the capture path has already computed. It may
# write the run log and the attention record, and nothing else: no pointer, no
# marker, no queue entry, no payload. That is the load-bearing property of the
# feature (AC-F5.6) — capture behavior has to be identical with the event firing and
# with a threshold that never fires — so the single entry point,
# :func:`_observe_delta`, is also isolated: a hostile record, a full disk or a
# read-only LOOM_HOME must not be able to turn a good pass into a failed one.
def _load_attention(cfg: Config) -> dict:
    """The attention record, or ``{}`` when there is none that can be read.

    Unlike ``state.json`` an unparseable record is NOT an event worth preserving:
    it holds no pointer and losing it costs nothing but a diagnostic that the very
    next pass over an unhealed transcript writes again. Rebuilding beats stranding a
    file that would otherwise never be rewritten.
    """
    try:
        data = json.loads(cfg.attention_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_attention(cfg: Config, record: dict) -> None:
    """Persists the attention record with the same atomic+durable write as the state.

    Called only from :func:`_observe_delta`, i.e. inside the processor flock, so two
    passes can never interleave a read-modify-write of the same map.
    """
    _atomic_write_json(cfg.attention_path, record)


def _attention_entry(previous: object, transcript_path: object, held: int,
                     now: str) -> dict:
    """One record entry: the frozen fields, plus whatever an earlier version added.

    ``previous`` is whatever was on disk under this session id — a well-formed
    entry, a scalar a human left behind, anything. Only its ``first_seen`` (when the
    condition was FIRST observed, which must not move) and its additive extra fields
    are carried over; the counts and the path are always recomputed from this pass.
    """
    entry = dict(previous) if isinstance(previous, dict) else {}
    first_seen = entry.get("first_seen")
    entry.update({
        "transcript_path": _flatten(transcript_path),
        "held": int(held),
        "first_seen": first_seen if isinstance(first_seen, str) and first_seen else now,
        "last_seen": now,
    })
    return entry


def _update_attention(cfg: Config, session_id: str, transcript_path: object,
                      delta, last_line: int) -> None:
    """Upserts / removes this session's entry in the attention record (P3.F5).

    The condition is deliberately narrower than "the delta was empty": it is a
    transcript NOTHING has ever been parsed out of — no conversational message in
    this delta (``not delta.messages``), a trailing held run (``delta.held > 0``),
    and a pointer still at 0 both before and after the read. A session whose delta
    is simply at EOF (the common re-enqueue) or one holding a torn tail after a
    parsed prefix is healthy: the capture already happened, or will on the next pass.

    The entry is removed on the pass where the transcript parses again — one
    conversational message is enough, which also covers "or delivers", since a
    delivered delta had messages by construction. Any other shape (an empty delta
    at EOF, a held tail past a parsed prefix) leaves the record untouched: it is
    neither evidence of the condition nor evidence of its healing.

    The record is only written when it actually changes, so a run over healthy
    sessions never creates the file — an absent record and an empty one must stay
    indistinguishable for ``loom status`` (AC-F5.4).
    """
    wholly_unparseable = (
        not delta.messages
        and delta.held > 0
        and last_line == 0
        and delta.new_last_line == 0
    )
    record = _load_attention(cfg)
    if wholly_unparseable:
        record[session_id] = _attention_entry(
            record.get(session_id), transcript_path, delta.held, _now_iso()
        )
    elif delta.messages and session_id in record:
        del record[session_id]
    else:
        return
    _write_attention(cfg, record)


def _report_tear_ratio(cfg: Config, session_id: str, delta) -> None:
    """Emits ``tear_ratio_high`` when this delta lost too much of itself to tears.

    The trigger is the RATIO ``torn / (torn + parsed)``, not the count: three torn
    lines in a 200-message delta is a crash artifact the tolerance rules already
    absorbed, while three torn lines around a single parsed one is a transcript that
    is mostly gone. ``>=`` is the frozen comparison, so a delta exactly at the
    threshold fires. Counts only ever leave — the torn bytes may hold half a
    credential (I5/I6) and are never even decoded.
    """
    parsed = len(delta.messages)
    total = delta.torn + parsed
    if delta.torn <= 0 or total <= 0:
        return
    ratio = delta.torn / total
    if ratio >= cfg.tear_ratio_threshold:
        _log(cfg, session_id, "tear_ratio_high",
             f"torn={delta.torn} parsed={parsed} ratio={ratio:.2f}")


def _observe_delta(cfg: Config, session_id: str, transcript_path: object, delta,
                   last_line: int) -> None:
    """The REPORT-ONLY observation of one processed delta (P3.F5).

    Called exactly once per processed marker, which is what makes the event "at most
    once per marker". Every failure is contained here and reported as its own log
    line: the alternative is an exception reaching :func:`run_once`'s catch-all,
    which would record an ``error`` outcome and leave the marker queued — a capture
    difference caused purely by the observability feature, i.e. exactly what AC-F5.6
    forbids. Only the exception TYPE is logged: its text can quote a path or a value
    from the record.
    """
    try:
        _report_tear_ratio(cfg, session_id, delta)
    except Exception as exc:  # noqa: BLE001 — report-only: never affect the capture.
        _report_observation_failure(cfg, session_id, "tear_ratio_error", exc)
    try:
        _update_attention(cfg, session_id, transcript_path, delta, last_line)
    except Exception as exc:  # noqa: BLE001 — report-only: never affect the capture.
        _report_observation_failure(cfg, session_id, "attention_record_error", exc)


def _report_observation_failure(cfg: Config, session_id: str, action: str,
                                exc: BaseException) -> None:
    """Logs an observation failure — and swallows a failure to log it.

    The diagnostic is the last thing standing between a swallowed exception and a
    silent one, but it must not become the exception that escapes: the whole point
    of the isolation above is that nothing on this path can reach the caller.
    """
    try:
        _log(cfg, session_id, action, type(exc).__name__)
    except Exception:  # noqa: BLE001 — a diagnostic may never break a capture.
        pass


# --------------------------------------------------------------------------- #
# Remote gate                                                                  #
# --------------------------------------------------------------------------- #
def _git_remote(cwd: str) -> str | None:
    """Raw URL of the ``origin`` remote in ``cwd``, or ``None`` (no repo/remote/dir)."""
    if not cwd or not Path(cwd).is_dir():
        return None
    try:
        cp = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None
    url = cp.stdout.strip()
    return url or None


# Drop details. R6 keeps today's string verbatim so every existing assertion, log reader
# and runbook entry that names it stays correct; R5 is deliberately DIFFERENT, because
# "the hook captured nothing" and "the hook captured garbage" send an operator to two
# different places.
_DETAIL_NO_REMOTE = "no git remote origin"
_DETAIL_LIVE_UNUSABLE = "remote not normalizable"
_DETAIL_FALLBACK_UNUSABLE = "fallback not normalizable"


def _canonical_from_marker(
    cfg: Config, session_id: str, marker: dict, remote_url: str | None
) -> tuple[str | None, str]:
    """Project identity for this marker: ``(canonical, drop_detail)`` (issue #4).

    **``contract/ATTRIBUTION.md`` §2 is the authority** for the six-row table this
    implements (R1-R6) and for every argument behind it: why live always wins, why
    equality is decided after ``normalize()``, and why an unusable identity drops rather
    than guesses. That document is frozen read-only (``AGENTS.md`` §13); restating its
    table here would only give it somewhere to drift to. Read it before changing a row.

    ``drop_detail`` is meaningful only when ``canonical`` is ``None``; the success paths
    return ``""`` and the caller does not read it.

    Two notes that belong with the CODE rather than with the contract:

    - **Bounded, not merely correct.** This runs inside ``run_once``'s per-marker loop.
      That loop isolates each marker, so an escape here would not abort the pass — but it
      would turn a disposition the contract specifies (``dropped_no_remote`` with a
      distinguishing detail) into an undiagnosed ``error``, and the marker would meet the
      same input and the same fate on every later pass. ``normalize`` raises only
      ``ValueError``, including for a non-``str`` — exactly what a hand-edited or
      foreign-writer marker can hold — and all three call sites bound it. The ``_log``
      calls are not bounded here and can still raise ``OSError`` on a full or unwritable
      disk; the loop's handler is what covers that, and it is the same exposure every
      other ``_record``/``_log`` site in this module already has.
    - **The R1-R3 ``except`` is not dead.** A captured value that will not normalize
      cannot be shown equal to the live one, so it counts as differing and takes R3's
      path: live decides, one diagnostic, no drop. Silently treating it as equal would
      suppress the only signal that a marker carries garbage while its ``cwd`` still works.
    """
    captured = marker.get("cwd_remote_url")

    if remote_url is not None:
        # R1 / R2 / R3 — the session's own directory answered, so it decides.
        try:
            canonical = normalize(remote_url)
        except ValueError:
            return None, _DETAIL_LIVE_UNUSABLE
        if captured is not None:
            try:
                superseded = normalize(captured) != canonical
            except ValueError:
                superseded = True
            if superseded:
                _log(cfg, session_id, "attribution_superseded", canonical)
        return canonical, ""

    # `cwd` is gone — the ephemeral-worktree case this helper exists for.
    if captured is None:
        return None, _DETAIL_NO_REMOTE                      # R6
    try:
        canonical = normalize(captured)                     # R4
    except ValueError:
        return None, _DETAIL_FALLBACK_UNUSABLE              # R5
    _log(cfg, session_id, "attribution_from_marker", canonical)
    return canonical, ""


# --------------------------------------------------------------------------- #
# Registry cache                                                               #
# --------------------------------------------------------------------------- #
@dataclass
class Registry:
    """Result of loading/refreshing the project registry cache.

    - ``available``: ``True`` if we have a trustworthy registry (fresh cache, or a
      successful refresh). ``False`` if the server does not answer and the cache is
      expired/absent → the gate cannot be decided and the markers are kept.
    - ``remotes``: set of registered ``canonical_remote`` (empty if not available).
    """

    available: bool
    remotes: set[str]
    projects: list[dict] = field(default_factory=list)
    # Set when the refresh was refused with 401/403: the interactive callers say so
    # (`loom log` keeps its "HTTP 401 … loom enroll" line) instead of "unreachable".
    auth_status: int | None = None


def _load_registry(cfg: Config, api: ApiClient, *, force: bool = False,
                   stale_ok: bool = False) -> Registry:
    """The registry, from the cache when fresh, else refreshed from the server.

    ``force`` skips the freshness check: ``loom enable`` / ``loom register`` call it with
    the person at the keyboard, where a cache that predates a teammate's registration
    would wrongly refuse them. On a failed forced refresh a fresh cache is still the
    answer. ``stale_ok`` (``loom check``, ``loom log``) accepts a cache of ANY age when
    the refresh fails: an interactive read must answer from what it has rather than
    claim the project does not exist (#13). The processor keeps neither: an expired
    cache and a dead server leave the markers for the next pass.
    """
    cache = _read_marker(cfg.cache_path)  # {"fetched_at", "projects"} or None
    fresh = False
    cached_projects: list[dict] | None = None
    if cache and isinstance(cache.get("projects"), list):
        cached_projects = cache["projects"]
        fetched_at = cache.get("fetched_at", "")
        fresh = _age_seconds(fetched_at) < cfg.cache_ttl_s

    if fresh and not force:
        return Registry(True, _remotes_of(cached_projects), cached_projects)

    # Cache absent or expired → try to refresh.
    try:
        projects = api.list_projects()
    except AuthRejected as exc:
        # The registry is unavailable exactly as for a dead server, and the markers are
        # kept the same way — but THIS cause is recorded, because nothing else on an
        # unattended path would ever say so (issue #20).
        auth.record_auth_rejected(cfg, exc.status, exc.what)
        _log(cfg, "auth", "auth_rejected", f"{exc.what} → {exc.status}")
        return Registry(False, set(), auth_status=exc.status)
    except ServerUnreachable:
        if cached_projects is not None and (fresh or stale_ok):
            return Registry(True, _remotes_of(cached_projects), cached_projects)
        return Registry(False, set())

    _atomic_write_json(cfg.cache_path, {"fetched_at": _now_iso(), "projects": projects})
    return Registry(True, _remotes_of(projects), projects)


def _remotes_of(projects: list[dict]) -> set[str]:
    return {p["canonical_remote"] for p in projects if p.get("canonical_remote")}


def _age_seconds(iso_ts: str) -> float:
    """Seconds elapsed since ``iso_ts`` (∞ if it does not parse)."""
    if not iso_ts:
        return float("inf")
    try:
        parsed = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds()


# --------------------------------------------------------------------------- #
# content_hash                                                                 #
# --------------------------------------------------------------------------- #
def compute_content_hash(
    covered_from: str | None,
    covered_to: str | None,
    prompt_version: str,
    model: str,
    delta_text: str,
) -> str:
    """``sha256(covered_from|covered_to|prompt_version|model|sha256(delta_text))`` in hex (PLAN §T4).

    Deterministic: same delta + same range + same model/prompt ⇒ same hash ⇒ the server
    dedupes with a 409 (idempotency, I6).
    """
    inner = hashlib.sha256(delta_text.encode("utf-8")).hexdigest()
    material = f"{covered_from}|{covered_to}|{prompt_version}|{model}|{inner}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Typed capture: the label parse (T.F3)                                        #
# --------------------------------------------------------------------------- #
def _live_sections(summary_md: str) -> list[tuple[str, list[str]]]:
    """``[(heading, body lines)]`` of the summary, ignoring everything inside a fence.

    A summary is a MARKDOWN DOCUMENT, not a line-oriented record, and the one
    structure that changes what a line means is the code fence: inside it, ``##
    Decisions`` is a quoted example and ``- [method] x`` is sample text. A session
    about editing ``prompts/summarizer_v2.md`` — or any session quoting the output
    template — contains exactly that, so a fence-blind scan types the entry from the
    example and never sees its real items.

    Fenced lines are dropped rather than kept: they are neither headings nor items,
    at any level. An unterminated fence therefore swallows the rest of the document,
    which is the safe direction — an untyped entry is visibly untyped and
    re-derivable from ``content_md``, whereas items parsed out of quoted sample text
    are wrong in a way nothing downstream can detect.
    """
    return [(section.heading, [line for line, _offset in section.lines])
            for section in _scan_sections(summary_md)]


class _Section(NamedTuple):
    """One h2 section of a summary: its heading line, its LIVE lines with the offset
    at which each starts, and the offset at which the section's text ends (where the
    next heading starts, or the end of the document)."""

    heading: str
    lines: list[tuple[str, int]]
    end: int


def _scan_sections(summary_md: str) -> list[_Section]:
    """:func:`_live_sections` with offsets — the one reader both views are built on.

    The offsets are what let an item be cut out of the summary VERBATIM
    (:func:`split_items`): its text is a slice of the document, never a re-join of
    parsed lines, so it is always a substring of what was scanned for secrets and
    posted as ``content_md``.
    """
    sections: list[list] = [["", [], len(summary_md)]]
    fence: str | None = None
    offset = 0
    for raw, line in zip(summary_md.splitlines(keepends=True), summary_md.splitlines()):
        start, offset = offset, offset + len(raw)
        delimiter = _FENCE_RE.match(line)
        if delimiter:
            marker = delimiter.group(1)[0]
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is not None:
            continue
        if _HEADING_RE.match(line):
            sections[-1][2] = start
            sections.append([line, [], len(summary_md)])
        else:
            sections[-1][1].append((line, start))
    return [_Section(*section) for section in sections]


def _decisions_section(summary_md: str) -> list[str]:
    """The lines of the summary's Decisions section(s) — its bullets are the items.

    Scoped rather than whole-document because the other sections legitimately hold
    bullets — ``## Open items`` always does — and counting those would forge both a
    type and a fallback for prose that was never an item.

    EVERY ``## Decisions`` section is concatenated, not just the first. A model that
    emits the heading twice (a long session, a restated template) otherwise has
    everything after the second heading silently dropped, which is invisible in the
    payload: the entry looks correctly typed, from half its items.

    The second tier exists for the shape prompt rule 8 makes likely — the prose
    follows the session's language, so a Spanish session yields ``## Decisiones`` —
    and it deliberately keys on a BRACKET-LED bullet rather than a strictly labeled
    one: if the model also translated the labels (``[método]``), the section must
    still be found, so that its items are counted as fallbacks. That is the whole
    drift signal; keying on the English literals would report "untyped, no drift" for
    the most likely drift there is. It cannot select an "Open items" section, whose
    bullets are prose rather than bracket-led.
    """
    return [line for section in _decisions_sections(summary_md)
            for line, _offset in section.lines]


def _decisions_sections(summary_md: str) -> list[_Section]:
    """The section(s) :func:`_decisions_section` reads, with their offsets."""
    sections = _scan_sections(summary_md)
    named = [s for s in sections if _DECISIONS_HEADING_RE.match(s.heading)]
    if named:
        return named
    for section in sections:
        if any(_BRACKET_LED_BULLET_RE.match(line) for line, _offset in section.lines):
            return [section]
    return []


def locate_items(content_md: str, texts: list[str]) -> list[int] | None:
    """Where each of ``texts`` (an entry's items, in order) sits in ``content_md``.

    Items are verbatim slices of the entry's Decisions section(s) — the server checked
    the substring on POST. So the search starts where :func:`_decisions_sections` says
    that section starts (never in the frontmatter or in ``## Context``, which may quote
    the same words), each text is found as WHOLE lines (a bullet that merely prefixes,
    or ends, a longer line is not it), and each after the previous one. ``None`` when
    one cannot be found: a reader then treats the entry as un-itemized rather than
    touching the wrong lines. Shared by `loom log` (ids in place) and the digest
    (replaced items removed), so the two can never locate an item differently.
    """
    head_len = len(content_md) - len(okf.body(content_md))
    starts = [offset for section in _decisions_sections(content_md[head_len:])
              for _line, offset in section.lines]
    if not starts:
        return None
    cursor = head_len + min(starts)
    found: list[int] = []
    for text in texts:
        at = _find_lines(content_md, text, cursor)
        if at < 0:
            return None
        found.append(at)
        cursor = at + len(text)
    return found


def _find_lines(content: str, text: str, start: int) -> int:
    """Where ``text`` occurs from ``start`` as whole lines (it starts a line and ends one,
    or the document), or ``-1``."""
    at = content.find(text, start)
    while at >= 0:
        end = at + len(text)
        if ((at == 0 or content[at - 1] == "\n")
                and (end == len(content) or content[end] in "\r\n")):
            return at
        at = content.find(text, at + 1)
    return -1


# The most items the server stores for one entry (`server/items.MAX_ITEMS`; the client
# cannot import it — separate packages, the ITEM_TYPES discipline). Over it the server
# answers 422, which this processor keeps as `error` and would re-summarize on every
# pass forever; `postable_items` sends such an entry un-itemized instead.
MAX_POSTED_ITEMS = 50


class Item(NamedTuple):
    """One item of an entry (D13): what the client posts in ``items``."""

    item_type: str
    labelled: bool   # False = typed by FALLBACK_ITEM_TYPE: the bullet missed the grammar
    text_md: str     # the item's lines, verbatim — a substring of the summary


def split_items(summary_md: str, prompt_version: str) -> list[Item]:
    """The items of a summary, in document order (D13, ``t-entry-items``).

    An item is one top-level bullet of the Decisions section(s) together with
    everything up to the next top-level bullet or the end of its section: continuation
    lines, sub-bullets, a fenced block it quotes. What counts as a bullet, what is
    top-level and how it is typed are exactly the rules of :func:`parse_item_types`,
    which is derived from this function so the two can never disagree — the server
    refuses an entry whose ``item_types`` and ``items`` say different things.

    ``text_md`` is a SLICE of ``summary_md`` (trailing whitespace dropped), never a
    reconstruction. The server stores an item only if its text is a substring of
    ``content_md``, so the caller must split the very text it wraps into the entry.

    Labelled prompt versions only; ``[]`` otherwise (no retroactive typing of v1).
    """
    if prompt_version not in LABELLED_PROMPT_VERSIONS:
        return []
    starts: list[tuple[int, str, bool, int]] = []
    item_indent: int | None = None
    for section in _decisions_sections(summary_md):
        for line, offset in section.lines:
            bullet = _ITEM_BULLET_RE.match(line)
            if not bullet:
                continue
            indent = len(bullet.group("indent"))
            if item_indent is not None and indent > item_indent:
                continue
            item_indent = indent if item_indent is None else min(item_indent, indent)
            label = _LABELLED_BULLET_RE.match(line)
            starts.append((offset, label.group(1) if label else FALLBACK_ITEM_TYPE,
                           label is not None, section.end))
    items = []
    for n, (offset, item_type, labelled, end) in enumerate(starts):
        if n + 1 < len(starts) and starts[n + 1][0] < end:
            end = starts[n + 1][0]
        items.append(Item(item_type, labelled, summary_md[offset:end].rstrip()))
    return items


def postable_items(items: list[Item]) -> list[dict] | None:
    """The ``items`` field of a POST, or ``None`` when it must be left out (D13).

    Left out when there are none (a v1 payload stays byte-compatible) and when there
    are more than the server stores: that entry still posts, typed by ``item_types``,
    and every reader treats it as a whole — exactly like an entry from before D13.
    """
    if not items or len(items) > MAX_POSTED_ITEMS:
        return None
    return [item._asdict() for item in items]


def parse_item_types(summary_md: str, prompt_version: str) -> tuple[list[str], int]:
    """``(sorted deduped item types, fallback count)`` of a summary (T.F3).

    The PROCESSOR owns this parse — the LLM is never asked for the aggregate, which
    it could malform, exactly as it is never asked for the frontmatter (D7). The
    summary is READ, never rewritten: the bullets keep their labels verbatim in
    ``content_md``.

    Two regexes with two different jobs. :data:`_ITEM_BULLET_RE` decides what is an
    ITEM (wide: any list marker, any spacing) and :data:`_LABELLED_BULLET_RE` decides
    what its TYPE is (the strict frozen grammar). Widening the first without widening
    the second is the point: an item the grammar does not cover becomes a counted
    fallback instead of vanishing, and "vanishing" is the failure mode with no signal
    attached to it.

    Sub-bullets are not items. A bullet indented deeper than the section's own items
    is the body of the item above it — evidence, a sub-list — and typing it would add
    a phantom ``[decision]`` to an entry and a phantom count to the drift signal.

    Total by construction. Every item resolves to a type — the grammar's label when it
    matches, :data:`FALLBACK_ITEM_TYPE` when it does not — so no input can fail an
    entry over a label, and nothing but the closed vocabulary and an integer ever
    comes out (the run-log detail built from the count therefore cannot carry a byte
    of the summary; I5/I6).
    """
    items = split_items(summary_md, prompt_version)
    return sorted({item.item_type for item in items}), sum(not i.labelled for i in items)


# --------------------------------------------------------------------------- #
# Summarizer                                                                   #
# --------------------------------------------------------------------------- #
def _run_summarizer(cfg: Config, delta_text: str) -> subprocess.CompletedProcess:
    """Invokes the summarizer (fresh session, no tools; corrected invocation per FLAGS.md).

    ``cwd = LOOM_HOME`` (I1), ``LOOM_SUMMARIZER=1`` (the hook's anti-recursion guard),
    ``--tools ""`` (real tool blocking, I3). The delta text goes in through stdin.
    May raise ``subprocess.TimeoutExpired``.
    """
    cmd = [
        cfg.claude_bin,
        "-p",
        "--safe-mode",
        "--tools",
        "",
        "--model",
        cfg.model,
        "--output-format",
        "text",
        cfg.read_prompt(),
    ]
    env = {**os.environ, "LOOM_SUMMARIZER": "1"}
    return subprocess.run(
        cmd,
        input=delta_text,
        env=env,
        cwd=str(cfg.loom_home),
        timeout=cfg.timeout_s,
        capture_output=True,
        text=True,
    )


# --------------------------------------------------------------------------- #
# Per-marker pipeline                                                          #
# --------------------------------------------------------------------------- #
def _record(report: RunReport, cfg: Config, session_id: str, action: str, detail: str = "") -> None:
    report.outcomes.append(MarkerOutcome(session_id, action, detail))
    _log(cfg, session_id, action, detail)


def _log_label_fallbacks(cfg: Config, session_id: str, count: int) -> None:
    """One ``label_fallback`` line for an entry that RESOLVED with ≥1 fallback (T.F3).

    Emitted at the terminal disposition — posted, duplicate, quarantined — and never
    at parse time. The parse runs once per PASS, but a marker that cannot be delivered
    (server down, 5xx) is kept and re-summarized on the next pass, so logging where
    the count is computed makes one entry with two fallbacks report `count=2` once per
    retry. ``loom status`` sums these, so an outage would render as model drift: the
    operator's one indicator for "the summarizer stopped following the grammar" would
    move for a reason that has nothing to do with the summarizer.

    Counts only. The bullet that fell back may quote a credential the secret scan has
    not even run over yet, so neither the label nor the text it leads is ever
    formatted into the log (I5/I6).
    """
    if count > 0:
        _log(cfg, session_id, "label_fallback", f"count={count}")


def _drop_marker(marker_path: Path, identity: tuple | None = None) -> None:
    """Removes a processed marker — but never one the processor did not read (P2.F3).

    ``identity`` is what :func:`_identify_marker` returned when this pass read the
    marker. If the file on disk is no longer that one, the hook replaced it while we
    were summarizing: keeping it costs one extra (idempotent) pass, deleting it
    loses a real session capture.

    The check never adds a failure mode (AC-F3.3): if it cannot be evaluated — the
    marker vanished, the stat blew up — the drop degrades to the historical
    unconditional unlink.
    """
    if not _marker_is_still_ours(marker_path, identity):
        return
    marker_path.unlink(missing_ok=True)


def _clear_failed_tombstone(cfg: Config, session_id: str) -> None:
    """Removes ``failed/<session_id>.json`` once that session resolves cleanly.

    A park is a STATE, not a verdict. The sweep refuses to enqueue any session that
    has a tombstone (AC-F2.2) — the guard that closes the enqueue → fail → park →
    enqueue loop — and nothing but a human ever removed one. So a single transient
    park (a summarizer that was down for an hour, a transcript caught mid-write) made
    a session invisible to the sweep for the lifetime of the machine, even after a
    later hook marker for that same session had been summarized and posted.

    Healing the session heals its tombstone. Best-effort by construction: this is
    cleanup that runs after the outcome is already decided, so it must never be able
    to turn a successful pass into a failed one.
    """
    try:
        (cfg.failed_dir / f"{session_id}.json").unlink(missing_ok=True)
    except OSError:
        pass


def _resolve_marker(cfg: Config, marker_path: Path, identity: tuple | None,
                    session_id: str) -> None:
    """A successful disposition: the marker goes, and so does any stale tombstone.

    Every ending in :func:`_process_marker` that is not an error goes through here —
    posted, duplicate, no_entry, quarantined and each of the legitimate drops — so
    "the session got through" and "the session is no longer parked" cannot drift
    apart at one call site and not another.
    """
    _drop_marker(marker_path, identity)
    _clear_failed_tombstone(cfg, session_id)


def _marker_is_still_ours(marker_path: Path, identity: tuple | None) -> bool:
    """Is the file on disk still the marker this pass read? (P2.F3)

    Every disposition — the drop, the attempts rewrite, the park into ``failed/`` —
    has to ask this, and for the same reason: the summarizer window is minutes long
    and ``sessionend.sh`` writes this queue without the lock, so by the time a pass
    disposes of "its" marker the path may already hold a fresh capture. Rewriting one
    stamps a new session with an old payload and an attempt count it never earned;
    moving one into ``failed/`` is worse still, because the sweep refuses to look past
    a tombstone (AC-F2.2) — the fresh capture would be unreachable until a human
    cleared the directory.

    The check never adds a failure mode (AC-F3.3): an identity that cannot be
    evaluated — never captured, the marker vanished, the stat blew up — degrades to
    the historical unconditional behavior.
    """
    if identity is None:
        return True
    try:
        current = _identify_marker(marker_path)
    except Exception:  # noqa: BLE001 — the check must never crash a pass.
        current = None
    return current is None or current == identity


def _quarantine(cfg: Config, marker: dict, canonical: str, payload: dict, findings) -> Path:
    """Writes the entry payload (summary included) to ``quarantine/`` for manual review.

    The file holds the content with the secret in it: that is intentional (local review or
    redaction via ``loom push-quarantine``). It is never uploaded. The metadata only lists
    families.
    """
    session_id = marker.get("session_id", "unknown")
    ts = _now_iso().replace(":", "").replace("-", "")
    out = cfg.quarantine_dir / f"{session_id}-{ts}.json"
    record = {
        "quarantined_at": _now_iso(),
        "reason": "secret_scan",
        "findings": [{"kind": f.kind, "redacted": f.redacted} for f in findings],
        "project_remote": canonical,
        "entry": payload,
    }
    _atomic_write_json(out, record)
    return out


def _process_marker(
    cfg: Config,
    api: ApiClient,
    registry: Registry,
    state: dict,
    report: RunReport,
    marker_path: Path,
) -> None:
    # ONE read yields both: every disposition below can prove it is acting on the
    # marker this pass processed and not on one the hook wrote in the meantime.
    marker, identity = _read_marker_with_identity(marker_path)
    if marker is None:
        # A parsed non-object or invalid JSON is durable corruption: the hook writes
        # atomically, so park the bytes for inspection instead of retrying forever.
        # A vanished/unreadable path has no identity and may be transient, so keep it.
        if identity is not None and _marker_is_still_ours(marker_path, identity):
            _park_marker(cfg, marker_path)
            _record(report, cfg, marker_path.stem, "failed_parked", "marker unreadable")
        else:
            _record(report, cfg, marker_path.stem, "error", "marker unreadable")
        return

    session_id = marker.get("session_id", marker_path.stem)
    transcript_path = marker.get("transcript_path", "")
    cwd = marker.get("cwd", "")

    # Step 1: does the transcript exist?
    if not transcript_path or not Path(transcript_path).exists():
        _resolve_marker(cfg, marker_path, identity, session_id)
        _record(report, cfg, session_id, "dropped_no_transcript")
        return

    # Step 2: remote gate. The whole authority rule — live remote, captured fallback and
    # the three ways there is no usable identity at all — is in `_canonical_from_marker`
    # (contract/ATTRIBUTION.md §2). No remote → drop (the path fallback is v1.5; only
    # logged, DESIGN §4.2.2).
    remote_url = _git_remote(cwd)
    canonical, drop_detail = _canonical_from_marker(cfg, session_id, marker, remote_url)
    if canonical is None:
        _resolve_marker(cfg, marker_path, identity, session_id)
        _record(report, cfg, session_id, "dropped_no_remote", drop_detail)
        return

    # Step 3: registry (cache).
    if not registry.available:
        report.server_unreachable = True
        _record(report, cfg, session_id, "kept_server_unreachable", canonical)
        return
    if canonical not in registry.remotes:
        _resolve_marker(cfg, marker_path, identity, session_id)
        _record(report, cfg, session_id, "dropped_not_registered", canonical)
        return

    # Step 3b: the local gate (D9). Registered by the team is necessary, not sufficient:
    # in opt-in mode THIS machine's owner must have enabled the project here. Evaluated
    # after step 3 and only locally, so an unregistered project never produces this
    # drop and nothing about it is logged. In legacy mode (absent capture_mode) the step
    # is a no-op, which is what every pre-D9 config and the frozen waves expect.
    #
    # D9 amendment (2026-09-30): enabling captures what happens from then on, never the
    # repo's history — step 4b drops the turns older than the enable.
    capture_since = None
    if cfg.capture_mode == CAPTURE_MODE_OPT_IN:
        if projects_local.state_of(cfg, canonical) != projects_local.ENABLED:
            _resolve_marker(cfg, marker_path, identity, session_id)
            _record(report, cfg, session_id, "dropped_not_enabled", canonical)
            return
        capture_since = projects_local.capture_since(cfg, canonical)

    # Step 4: delta from the pointer. Every adapter-class failure — schema corruption,
    # an unreadable file, any residual decode error — lands in one bounded handler, so
    # none of them can reach run_once's catch-all undiagnosed (P2.F2). The pointer is
    # untouched on all of them (frozen phase1-processor-matrix invariant).
    #
    # RecursionError belongs in this tuple even though nothing in the adapter raises it
    # deliberately: `json.loads` raises it on a pathologically nested line, and it is
    # NOT a ValueError, so it slips past the adapter's torn-line classifier as well. Out
    # here it would reach run_once's catch-all — marker kept, no attempt counted, no
    # diagnostic — and every later pass would reach the same line and do the same thing.
    # It is bounded here rather than classified as a tear in the adapter: a line that
    # nests 200k deep is not a writer caught mid-append, and treating it as one would
    # silently skip it forever instead of surfacing it to a human.
    last_line, prev_uuid = _pointer(state, transcript_path)
    # P2.F4: the stat that will travel with the pointer is taken HERE — the statement
    # before the read, with nothing in between that can touch the file. Everything below
    # (summarizer, scan, POST, the pointer write itself) runs after the bytes have been
    # read, so a stat taken there would already cover a concurrent append and the sweep
    # would skip that append forever. See _advance_pointer for the full argument. WHEN
    # it is taken is only half of it, though — see the corroboration gate below the read.
    pre = _stat_pair(transcript_path)
    try:
        delta = read_delta(transcript_path, last_line)
    except (AdapterError, UnicodeDecodeError, OSError, RecursionError) as exc:
        _handle_adapter_failure(cfg, report, marker, marker_path, session_id, exc,
                                identity)
        return
    # …and the pair is only recorded if the read CORROBORATES it. Taking the stat first
    # is necessary but not sufficient: `os.stat(path)` and `Path(path).open()` resolve
    # the path twice, and nothing so far proves the read saw a file of that size.
    # - Filesystem-specific: where the attribute cache and the page cache revalidate
    #   independently (9p `cache=loose`, some NFS setups), the stat can report a
    #   post-append size while the read still returns the pre-append content.
    # - Filesystem-independent: an `os.replace` onto the path between the two calls
    #   staples the OLD inode's pair to the NEW inode's pointer.
    # Either way the pair would describe MORE content than the pointer covers, every
    # later sweep would match it, and the surplus would never be re-read — permanent,
    # silent loss. `delta.bytes_read` is the size of the file the read actually walked
    # (index 0 → EOF, always), so equality is the proof. When it fails, record NO pair:
    # the sweep falls back to the full check, which costs one wasted read and is the
    # safe direction — the same trade `_stat_pair` already takes on an OSError.
    #
    # Unpacked rather than indexed on purpose: the tuple grew a (dev, ino) head in the
    # 2026-08-10 amendment, and a bare `pre[1]` left behind by that growth compares
    # `bytes_read` against the INODE NUMBER. That never matches, so no entry would ever
    # carry the fields and the whole skip would quietly become dead code that still
    # looks implemented. Naming the element is what keeps the gate readable-correct.
    pre_read_stat = None
    if pre is not None:
        *_pre_identity, _pre_mtime_ns, pre_size = pre
        if delta.bytes_read == pre_size:
            pre_read_stat = pre

    if delta.torn:
        # P2.F1 rule 4: torn lines are silent data loss otherwise. Only the COUNT is
        # logged — the bytes may hold half a credential (I5).
        _log(cfg, session_id, "torn_lines_skipped", f"torn={delta.torn}")

    if delta.held:
        # The trailing held run (P2.F1 amendment). ``torn`` covers interior tears only,
        # so a transcript in which EVERY line is torn — a Claude Code format change, a
        # foreign .jsonl under the sweep root, tail bit-rot — yields torn=0 and an empty
        # delta, and the capture would disappear into dropped_empty_delta with nothing
        # written down. The count is REPORTED, not acted upon: the held lines may still
        # heal, so no escalation is triggered here. Like the torn line above, only the
        # COUNT and the path escape — never a byte of the held lines (I5/I6). The path
        # comes from the marker, so its whitespace is collapsed to keep it one log column.
        _log(cfg, session_id, "held_lines_at_eof",
             f"held={delta.held} transcript={_flatten(transcript_path)}")

    # P3.F5: the REPORT-ONLY observation of this delta — the tear-ratio escalation
    # and the attention record. It runs before the dispositions below and touches
    # none of them: every branch from here on behaves exactly as it did before the
    # feature existed (AC-F5.6), which is why the call is a statement of its own
    # rather than a condition anything downstream reads.
    _observe_delta(cfg, session_id, transcript_path, delta, last_line)

    # Step 4b (D9 amendment): the leading turns PROVABLY older than the enable are left
    # out, and the entry's range starts after the last of them. A turn with no usable
    # timestamp ends the run: what cannot be dated is kept, as before the amendment. The
    # pointer still advances over the whole delta, so they are never read for capture
    # again — a transcript of the repo's history costs one read and no summary.
    before_enable = _turns_before(delta.messages, capture_since)
    if before_enable:
        _log(cfg, session_id, "before_enable_skipped", f"messages={before_enable}")
        prev_uuid = delta.messages[before_enable - 1].uuid
        delta = dataclasses.replace(delta, messages=delta.messages[before_enable:])

    # Step 5: delta with no conversational content → advance the pointer, drop.
    if not delta.messages:
        _advance_pointer(cfg, state, transcript_path, delta.new_last_line,
                         delta.new_last_uuid, pre_read_stat)
        _resolve_marker(cfg, marker_path, identity, session_id)
        _record(report, cfg, session_id,
                "dropped_before_enable" if before_enable else "dropped_empty_delta")
        return

    delta_text = render_for_summarizer(delta)

    # Step 6: summarizer.
    try:
        cp = _run_summarizer(cfg, delta_text)
    except subprocess.TimeoutExpired:
        _handle_summarizer_failure(cfg, report, marker, marker_path, session_id,
                                   "timeout", identity)
        return
    if cp.returncode != 0:
        _handle_summarizer_failure(
            cfg, report, marker, marker_path, session_id, f"exit={cp.returncode}",
            identity,
        )
        return

    summary = cp.stdout

    # Step 7: NO_ENTRY → advance the pointer, drop, no POST.
    if summary.strip() == NO_ENTRY:
        _advance_pointer(cfg, state, transcript_path, delta.new_last_line,
                         delta.new_last_uuid, pre_read_stat)
        _resolve_marker(cfg, marker_path, identity, session_id)
        _record(report, cfg, session_id, "no_entry")
        return

    # Typed capture (T.F3): the labels of the v2 summary, aggregated by US. This is
    # the ONLY thing the parse produces — a sorted label set and a count — and it
    # feeds nothing but the additive `item_types`: the disposition below, the pointer,
    # the marker and `content_hash`'s material are all exactly what they were before
    # the feature existed, and a summary that follows no grammar at all still posts.
    # The count is carried down to whichever disposition ENDS this entry rather than
    # logged here; see :func:`_log_label_fallbacks`.
    #
    # D13: the same parse also yields the ITEMS themselves, cut from the exact text
    # `wrap_entry` embeds (it strips the summary), so every item is a substring of
    # `content_md` — the one property the server checks. `item_types` is derived from
    # the same list, so the two statements the payload makes about the entry agree.
    items = split_items(summary.strip(), cfg.prompt_version)
    item_types = sorted({item.item_type for item in items})
    label_fallbacks = sum(not item.labelled for item in items)

    # The payload that would be uploaded (also what gets quarantined on a secret).
    covered_from = prev_uuid
    covered_to = delta.new_last_uuid
    content_hash = compute_content_hash(
        covered_from, covered_to, cfg.prompt_version, cfg.model, delta_text
    )
    # D7: the entry travels as a self-contained OKF concept — the frontmatter (provenance,
    # status, sources) is prepended by the processor from deterministic data, not the LLM.
    content_md = okf.wrap_entry(
        summary,
        canonical_remote=canonical,
        session_id=session_id,
        covered_from=covered_from,
        covered_to=covered_to,
        model=cfg.model,
        prompt_version=cfg.prompt_version,
        item_types=item_types,
    )
    payload = {
        "project_remote": canonical,
        "session_id": session_id,
        "covered_from": covered_from,
        "covered_to": covered_to,
        "content_md": content_md,
        "content_hash": content_hash,
        "model": cfg.model,
        "prompt_version": cfg.prompt_version,
        # Additive (T.F3). Always present, empty for an untyped (v1) entry: a
        # consumer reading the field must not have to tell "no labels" apart from
        # "an older client that did not send them".
        "item_types": item_types,
    }
    posted_items = postable_items(items)
    if posted_items is not None:
        # Additive (D13), and absent when there is nothing to send: an unlabelled (v1)
        # payload stays byte-compatible with what every server has always read.
        payload["items"] = posted_items

    # Step 8: secret scan (fail-closed, I4) BEFORE any POST, over the FINAL content that
    # would leave the machine (frontmatter included). Engine selection lives in
    # scan_engine (R2.3); a raise here still counts as a hit.
    try:
        findings = scan_engine.scan(content_md, cfg)
    except Exception as exc:  # noqa: BLE001 — fail-closed: a scanner failure == a hit.
        findings = [secret_scan.Finding(kind="scan_error", redacted=type(exc).__name__)]
    if findings:
        _quarantine(cfg, marker, canonical, payload, findings)
        _advance_pointer(cfg, state, transcript_path, delta.new_last_line,
                         delta.new_last_uuid, pre_read_stat)
        _resolve_marker(cfg, marker_path, identity, session_id)
        _log_label_fallbacks(cfg, session_id, label_fallbacks)
        _record(report, cfg, session_id, "quarantined", f"{len(findings)} finding(s)")
        return

    # Step 9: POST /api/entries.
    try:
        resp = api.post_entry(payload)
    except ServerUnreachable:
        # Network/server down → keep the marker, do NOT touch attempts (cheap retry).
        _record(report, cfg, session_id, "kept_network_error", "server unreachable")
        return

    if resp.status_code in (201, 409):
        # Success or duplicate: persist the pointer first, then clean up the marker.
        _advance_pointer(cfg, state, transcript_path, delta.new_last_line,
                         delta.new_last_uuid, pre_read_stat)
        _resolve_marker(cfg, marker_path, identity, session_id)
        action = "posted" if resp.status_code == 201 else "duplicate"
        _log_label_fallbacks(cfg, session_id, label_fallbacks)
        _record(report, cfg, session_id, action)
        auth.clear_auth_rejected(cfg)  # an entry landed: the credential works again
        return
    if resp.status_code in (401, 403):
        # The credential, not the network. Keep the marker (nothing is lost, the next
        # pass retries) but say so where a human will see it without running doctor by
        # chance — status, doctor, and the hint on every CLI server error (issue #20).
        auth.record_auth_rejected(cfg, resp.status_code, "POST /api/entries")
        _record(report, cfg, session_id, "kept_auth_rejected", f"{resp.status_code}")
        return
    if resp.status_code == 404:
        # The stamp is diagnosis, not state: if the hook replaced the marker while we
        # were summarizing, the fresh capture keeps its own payload and gets its own
        # pass. The outcome is reported either way.
        detail = "404 project_not_found"
        if _marker_is_still_ours(marker_path, identity):
            marker["registry_error"] = "project_not_found"
            _write_marker(marker_path, marker)
        else:
            detail += ", marker replaced by the hook"
        _record(report, cfg, session_id, "kept_registry_error", detail)
        return
    if resp.status_code >= 500:
        _record(report, cfg, session_id, "kept_network_error", f"{resp.status_code}")
        return

    # Other 4xx (401/403/422): likely misconfig/bug. Keep and report (visible in status).
    _record(report, cfg, session_id, "error", f"unexpected status {resp.status_code}")


def _turns_before(messages: list, since: datetime | None) -> int:
    """How many leading ``messages`` are dated strictly before ``since`` (0 without one)."""
    if since is None:
        return 0
    count = 0
    for msg in messages:
        if msg.at is None or msg.at >= since:
            break
        count += 1
    return count


def _handle_summarizer_failure(
    cfg: Config,
    report: RunReport,
    marker: dict,
    marker_path: Path,
    session_id: str,
    detail: str,
    identity: tuple | None = None,
) -> None:
    """attempts+1 on the marker; from ``MAX_ATTEMPTS`` on it is parked in ``failed/`` (P4)."""
    _bound_attempts(cfg, report, marker, marker_path, session_id,
                    "kept_summarizer_failed", detail, identity)


def _adapter_detail(exc: BaseException) -> str:
    """A diagnostic for an adapter-class failure — never transcript content (I5).

    ``UnicodeDecodeError`` carries the offending bytes on the exception object and
    renders the offending byte in ``str()``; ``OSError`` renders a path. Neither is
    formatted: only the class of failure and an errno reach the marker and the log,
    which are both read by humans and swept for secrets.
    """
    if isinstance(exc, UnicodeDecodeError):
        return "transcript bytes are not valid UTF-8"
    if isinstance(exc, OSError):
        return f"transcript unreadable: {type(exc).__name__} errno={exc.errno}"
    if isinstance(exc, RecursionError):
        # Fixed text, not str(exc): the interpreter's message is generic today, but a
        # diagnostic that reaches a marker and a log is not the place to find out that
        # it stopped being. Nothing about the line's content is ever needed here.
        return "transcript line nests too deeply to parse"
    # AdapterError messages are built by the adapter from line numbers and a
    # sanitized ``type``; collapsing whitespace keeps them to one log column anyway.
    return " ".join(str(exc).split())[:160]


def _handle_adapter_failure(
    cfg: Config,
    report: RunReport,
    marker: dict,
    marker_path: Path,
    session_id: str,
    exc: BaseException,
    identity: tuple | None = None,
) -> None:
    """Adapter failures join the summarizer's attempt discipline (P2.F2).

    Before phase 2 they were retried forever with no attempt bound, so a genuinely
    corrupt transcript kept a marker in the queue for the lifetime of the machine
    (✅ VERIFIED at the phase gate) and a ``UnicodeDecodeError`` escaped to
    ``run_once``'s catch-all with no diagnostic at all. The ``adapter_error`` field is
    what ``loom status`` renders next to the parked marker.

    The pointer is deliberately NOT touched here: nothing was confirmed pushed.
    """
    detail = _adapter_detail(exc)
    marker["adapter_error"] = detail
    _bound_attempts(cfg, report, marker, marker_path, session_id,
                    "kept_adapter_error", detail, identity)


def _pipeline_detail(exc: BaseException) -> str:
    """A one-line pipeline diagnostic that never echoes paths or input values."""
    detail = f"pipeline failure: {type(exc).__name__}"
    if isinstance(exc, OSError) and exc.errno is not None:
        detail += f" errno={exc.errno}"
    return detail


def _handle_pipeline_failure(
    cfg: Config,
    report: RunReport,
    marker_path: Path,
    exc: BaseException,
    identity: tuple | None,
) -> None:
    """Bounds an exception escaping the per-marker pipeline without aborting the pass."""
    detail = _pipeline_detail(exc)
    outcomes_before = len(report.outcomes)
    try:
        marker, _current_identity = _read_marker_with_identity(marker_path)
        if marker is None:
            if identity is not None and _marker_is_still_ours(marker_path, identity):
                _park_marker(cfg, marker_path)
                _record(report, cfg, marker_path.stem, "failed_parked",
                        f"{detail}, marker unreadable")
            else:
                _record(report, cfg, marker_path.stem, "kept_pipeline_error", detail)
            return

        session_id = str(marker.get("session_id") or marker_path.stem)
        marker["pipeline_error"] = detail
        _bound_attempts(cfg, report, marker, marker_path, session_id,
                        "kept_pipeline_error", detail, identity)
    except Exception:  # noqa: BLE001 — even failure reporting must not abort the pass.
        if len(report.outcomes) == outcomes_before:
            report.outcomes.append(
                MarkerOutcome(marker_path.stem, "kept_pipeline_error", detail)
            )


def _bound_attempts(
    cfg: Config,
    report: RunReport,
    marker: dict,
    marker_path: Path,
    session_id: str,
    kept_action: str,
    detail: str,
    identity: tuple | None = None,
) -> None:
    """attempts+1 on the marker; at ``MAX_ATTEMPTS`` it is parked in ``failed/`` (P4).

    Shared by the summarizer and adapter failure paths so the two can never drift:
    one counter on the marker, one bound, one parking rule.

    Both endings write to the queue path, and both run AFTER the summarizer window,
    so both ask :func:`_marker_is_still_ours` first: this pass's attempt count belongs
    to the marker this pass read, and a fresh capture that landed underneath it owes
    nothing to a failure it was not part of.
    """
    attempts = int(marker.get("attempts", 0)) + 1
    marker["attempts"] = attempts
    if not _marker_is_still_ours(marker_path, identity):
        # The hook replaced the marker mid-pass. Nothing is written and nothing is
        # parked: the fresh marker gets its own pass. The failure is still reported —
        # a run must never look cleaner than it was.
        _record(report, cfg, session_id, kept_action,
                f"{detail}, attempts={attempts} not persisted (marker replaced by the hook)")
        return
    if attempts >= MAX_ATTEMPTS:
        _write_marker(marker_path, marker)
        _park_marker(cfg, marker_path)
        _record(report, cfg, session_id, "failed_parked", f"{detail}, attempts={attempts}")
    else:
        _write_marker(marker_path, marker)
        _record(report, cfg, session_id, kept_action, f"{detail}, attempts={attempts}")


# --------------------------------------------------------------------------- #
# Public entry point                                                           #
# --------------------------------------------------------------------------- #
def _renew_device_token(cfg: Config, api: ApiClient) -> None:
    """Renew when less than a quarter of the token's life remains; never abort the pass.

    A failure to renew is logged and the pass goes on with the token it has — that token
    is still valid (renewal fires with ~22 days to spare), so nothing is lost today and
    the next pass tries again. Only a 401/403 is recorded as a rejection.
    """
    if not auth.renewal_due(cfg):
        return
    try:
        resp = api.renew_device_token()
    except ServerUnreachable as exc:
        _log(cfg, "auth", "token_renewal_failed", str(exc)[:160])
        return
    if resp.status_code == 201:
        try:
            auth.persist_token(cfg, api, resp.json())
        except (ValueError, KeyError, TypeError, OSError) as exc:
            _log(cfg, "auth", "token_renewal_failed", f"{type(exc).__name__}: {exc}"[:160])
            return
        auth.clear_auth_rejected(cfg)
        _log(cfg, "auth", "token_renewed", f"expires {cfg.token_expires_at}")
        return
    if resp.status_code in (401, 403):
        auth.record_auth_rejected(cfg, resp.status_code, "POST /api/device-tokens/renew")
        _log(cfg, "auth", "auth_rejected", f"POST /api/device-tokens/renew → {resp.status_code}")
        return
    _log(cfg, "auth", "token_renewal_failed", f"HTTP {resp.status_code}")


def run_once(cfg: Config) -> RunReport:
    """Processes every pending marker once. Serialized by ``flock``.

    If another processor holds the lock → returns ``RunReport(lock_held=True)`` without
    processing (equivalent to "exit 0 silently"). Summarizer, network and disk are isolated
    per marker: an unexpected exception in one is logged and does not abort the rest.
    """
    cfg.ensure_dirs()
    report = RunReport()

    lock_fd = open(cfg.lock_path, "w")
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            report.lock_held = True
            return report

        api = ApiClient(cfg.server_url, cfg.token)
        # Silent device-token renewal (issue #20) rides on a pass that was already going
        # to talk to the server: no timer of its own, no human. Both unattended entry
        # points (`loom process --quiet`, `python -m loom.processor --quiet`) land here.
        _renew_device_token(cfg, api)
        registry = _load_registry(cfg, api)
        state = _load_state(cfg)

        # Orphan-transcript sweep (R2.1): it runs HERE, inside the lock we already hold
        # and before the queue is listed, so its synthetic markers are processed in this
        # same pass and it can never race the hook's kick (AC-2.1d). Isolated like a
        # marker: a sweep crash must not stop the hook markers from being processed.
        if cfg.sweep_enabled:
            try:
                report.sweep = sweep.run_sweep(cfg, state)
            except Exception as exc:  # noqa: BLE001 — isolate the sweep, don't abort the run.
                _record(report, cfg, "sweep", "error", f"{type(exc).__name__}: {exc}"[:160])

        for marker_path in _iter_markers(cfg):
            identity = _identify_marker(marker_path)
            try:
                _process_marker(cfg, api, registry, state, report, marker_path)
            except Exception as exc:  # noqa: BLE001 — isolate the marker, don't abort the run.
                _handle_pipeline_failure(cfg, report, marker_path, exc, identity)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        lock_fd.close()

    return report


def main(argv: list[str] | None = None) -> int:
    """``python -m loom.processor`` — one processor pass. Exits 0 unless the config fails."""
    argv = sys.argv[1:] if argv is None else argv
    quiet = "--quiet" in argv
    try:
        cfg = Config.load()
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as exc:
        print(f"loom.processor: config not available: {exc}", file=sys.stderr)
        return 2
    report = run_once(cfg)
    if not quiet:
        if report.lock_held:
            print("loom.processor: another processor holds the lock; nothing to do.")
        else:
            line = (
                f"loom.processor: processed={report.processed} "
                f"skipped={report.skipped} errors={report.errors}"
            )
            if report.sweep is not None:
                line += (
                    f" swept={report.sweep.scanned}"
                    f" enqueued={report.sweep.enqueued}"
                )
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
