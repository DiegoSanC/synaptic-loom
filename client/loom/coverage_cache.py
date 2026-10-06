"""Sweep-owned read-coverage cache (P3.F1) — "what have I looked at?".

Decoupled from the delivery pointer's "what have I published?" (`state.json`,
frozen seam `p3-state-stat-fields`): entries here carry NO delivery semantics,
are written whenever ANY full read of a transcript completes, and are lossy and
rebuildable — deleting or corrupting the file may cost re-reads but never
changes what the sweep decides. Filled by epic E9 (wave g).

Why the file exists at all (RUNBOOK §9, ✅ VERIFIED): today the only stat pair the
sweep can consult lives in the delivery pointer, which is written **only** when the
server accepts an entry. A transcript that never delivers — an unregistered project,
a session that never wrote a ``cwd`` — therefore has no record anywhere that it was
ever read, and the sweep reads it in full on every pass, forever (7.5 MiB/pass,
~0.7 GiB/day residual on the pilot host). Read coverage is a different question from
delivery, so it gets its own answer here.

The failure mode this module is written against is NOT a lost cache — that is free,
it costs one re-read — but a **false hit**: serving a stale ``line_count``/``cwd`` for
a file that has changed makes the transcript permanently invisible to the sweep. So
every ambiguity (a missing field, a field that is not an exact ``int``, a corrupt
file, an unreadable stat) resolves to "no entry", i.e. to re-reading.

Never holds transcript CONTENT: only the four stat fields, a line count and the
session ``cwd`` the sweep already puts in the markers it writes (I5/I6).
"""

from __future__ import annotations

import json
from pathlib import Path

from loom import processor

# The frozen per-entry stat 4-tuple (p4-coverage-cache): cache key → ``os.stat_result``
# attribute. ALL FOUR must match for the entry to be usable — see :func:`lookup`.
_STAT_FIELDS = (
    ("stat_dev", "st_dev"),
    ("stat_ino", "st_ino"),
    ("stat_mtime_ns", "st_mtime_ns"),
    ("stat_size", "st_size"),
)


def load(cache_path: Path) -> dict:
    """Cache content, ``{}`` on absent/corrupt file (corrupt ≡ absent, AC-F1.4).

    The bytes are decoded strictly and parsed defensively: this file is rewritten by
    every pass and read by the next one, so a torn write, a half-migrated shape or a
    hand-edited file must degrade to "no coverage known" (one wasted read per
    transcript) instead of raising inside a pass that is holding the processor's lock.
    A non-object top level is treated the same way — ``lookup`` would otherwise index
    a list.
    """
    try:
        raw = Path(cache_path).read_bytes()
    except OSError:
        return {}  # absent, unreadable, or a directory (AC-F1.6): no coverage known.
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}  # corrupt ≡ absent: the pass rebuilds it from the reads it performs.
    return data if isinstance(data, dict) else {}


def lookup(cache: dict, transcript_path: str, st) -> dict | None:
    """The entry for ``transcript_path`` iff ALL four stat fields match ``st``.

    All four, never a subset (AC-F1.3). ``(st_mtime_ns, st_size)`` alone cannot see a
    same-size, mtime-preserving replacement (``cp -p``, ``rsync -a``, ``tar -xp``, a
    backup restore, ``os.replace`` + ``utimensat``): the identity half is what catches
    a different file wearing the same name. And ``st_mtime_ns`` — integer nanoseconds —
    never ``st_mtime``, whose float loses sub-second precision, so two writes inside
    the same second compare equal.

    ``type(...) is int`` rather than ``isinstance``: ``True`` is an ``int`` and must
    not be read as inode 1 or as a timestamp of 1. The payload is validated too — a
    ``line_count``/``cwd`` of the wrong type is an unusable answer, and returning it
    would push the type error into the caller's decision.

    A wrong ``None`` costs one re-read; a wrong hit strands a transcript forever, so
    everything ambiguous answers ``None``.
    """
    entry = cache.get(transcript_path)
    if not isinstance(entry, dict):
        return None
    for key, attr in _STAT_FIELDS:
        recorded = entry.get(key)
        if type(recorded) is not int or recorded != getattr(st, attr):
            return None
    # ``line_count`` must be a real count. The frozen entry shape permits ``null`` and
    # E12 consumes this API next wave, so a seam-conformant writer may well emit one —
    # but a null count is indistinguishable from "not yet read", and serving it as a
    # HIT makes the caller conclude "nothing to do" with zero opens and nothing left to
    # invalidate the entry. Every transcript written that way would be stranded
    # forever. Treat it as absent and let the next full read fill it in.
    if type(entry.get("line_count")) is not int:
        return None
    # ``cwd``, by contrast, is legitimately nullable: "this read completed and found no
    # cwd" is a real, useful answer — it is precisely the RUNBOOK §9 residual class the
    # cache exists to stop re-reading.
    cwd = entry.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        return None
    return entry


def record(cache: dict, transcript_path: str, st, line_count: int | None,
           cwd: str | None) -> None:
    """Record a completed full read (delivery-independent, AC-F1.2).

    Called whenever a full read COMPLETES — including reads whose marker is never
    written, never accepted, or later dropped. Coupling this to delivery is the exact
    original sin this cache exists to undo, so there is deliberately no success
    parameter to condition on.

    ``st`` must be the stat taken BEFORE the read (the same discipline as
    ``processor._advance_pointer``): a writer appending during the read then leaves an
    entry describing the older file, which does NOT match on the next pass and the
    transcript is re-read — the ambiguity resolves towards re-reading, not towards a
    silent skip.
    """
    cache[transcript_path] = {
        "stat_dev": st.st_dev,
        "stat_ino": st.st_ino,
        "stat_mtime_ns": st.st_mtime_ns,
        "stat_size": st.st_size,
        "line_count": line_count,
        "cwd": cwd,
    }


def save(cache: dict, cache_path: Path) -> None:
    """Atomic best-effort write; OSError degrades to uncached (AC-F1.6).

    Atomic because the next pass reads this file and a torn write would cost a full
    re-read of every transcript in it. Best-effort because the cache is pure: a
    read-only LOOM_HOME, a full disk or a path occupied by a directory must cost
    re-reads, never a failed sweep pass — the pass is running inside the processor's
    ``flock`` and every decision it makes is already final without this file.

    ``TypeError``/``ValueError`` are caught alongside ``OSError`` for the same reason:
    a non-serializable value that somehow reached the map is a corrupt cache, and a
    corrupt cache is defined as absent.
    """
    try:
        processor._atomic_write_json(Path(cache_path), cache)
    except (OSError, TypeError, ValueError):
        return  # uncached this pass: the next one simply pays the reads again.
