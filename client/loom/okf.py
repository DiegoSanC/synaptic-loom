"""OKF format of the entries (DESIGN.md D7 / §5.1).

Every logbook entry is uploaded as a self-contained OKF "concept": a markdown
document with YAML frontmatter following Google's Open Knowledge Format (v0.2; the
only mandatory field is ``type``). The block is built by the PROCESSOR from
deterministic data it already knows (session, uuid range, model, prompt) — the
summarizer is never asked for YAML, which it could malform.

No PyYAML dependency: the frontmatter has a fixed shape and the scalars are emitted
with ``json.dumps`` (JSON is a subset of YAML), which takes care of escaping
quotes, colons and unicode. ``None`` is emitted as ``null``.

The ``status: current`` field is the hook for phase-3 curation (marking things
``superseded`` without inventing a schema of our own), and ``sources`` carries the
evidence anchors (session + covered uuid range + model + prompt version).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

# OKF type of the logbook entries (mandatory field per the spec).
ENTRY_TYPE = "decision-log-entry"
# OKF type of a per-project digest (T.F6/E16). A digest is a normal, append-only entry
# — no schema change anywhere — and this single frontmatter field is the ONLY thing that
# tells it apart from a session entry. Every consumer reads it client-side
# (``loom.curate.is_digest`` / ``latest_digest``), so it is FROZEN: changing this literal
# turns every digest ever written into an ordinary entry, which would then be offered as
# a curation candidate and could supersede the very entries it summarises.
DIGEST_TYPE = "digest"
# Title of a digest. A LITERAL, never derived from the model's output: everything else in
# this frontmatter is deterministic data the script already knows, and lifting a line of
# LLM text up into a metadata field is how untrusted content ends up somewhere consumers
# treat as trustworthy. The date makes successive digests distinguishable in a listing.
DIGEST_TITLE = "Current state of decisions"

# The closed label vocabulary of prompt v2 (T.F3 ``t-label-grammar``), owned here
# because it is part of the entry FORMAT: the ``item_types`` field below, the title
# tolerance right after it and the processor's parse are three readings of one
# vocabulary, and they may not drift. FROZEN — consumers key off these literals.
ITEM_TYPES = ("decision", "method", "task", "fact")

# First bold decision in the summary ("- **<decision>** — ...", DESIGN §5).
#
# The optional head is the v2 label: under prompt v2 EVERY Decisions bullet leads
# with one, so without this the first bold item would no longer match at all and
# every v2 entry would fall back to a "Session log <date>" title. It is restricted to
# the closed vocabulary on purpose. This regex scans the WHOLE document, including a
# v1 summary's "Open items", where a wider bracket class silently changes titles that
# have nothing to do with the grammar: ``- [ ] **Fix the flake**`` used to yield no
# title and would start yielding one, and ``- [P0] **Ship it**`` would outrank the
# first real decision below it. A label outside the vocabulary is a bullet the
# processor counts as a fallback, not a title prefix to be trusted.
_TITLE_RE = re.compile(
    rf"^- (?:\[(?:{'|'.join(ITEM_TYPES)})\] )?\*\*(.+?)\*\*", re.MULTILINE
)
_TITLE_MAX = 60
# A leading frontmatter block: the opening fence, the shortest run of lines up to the
# first closing fence, and that fence's own line break (absent at end of document).
_FRONTMATTER_RE = re.compile(r"---\n.*?\n---(?:\n|\Z)", re.DOTALL)


def _y(value: object) -> str:
    """YAML-safe scalar via JSON (a subset of YAML)."""
    return json.dumps(value, ensure_ascii=False)


def extract_title(summary_md: str) -> str | None:
    """Deterministic title: the first bold decision in the summary, truncated.

    Returns ``None`` if the summary contains none (the caller decides the fallback).
    """
    m = _TITLE_RE.search(summary_md)
    if not m:
        return None
    title = " ".join(m.group(1).split())
    if len(title) > _TITLE_MAX:
        title = title[: _TITLE_MAX - 1].rstrip() + "…"
    return title


def body(content_md: str) -> str:
    """The document without its leading ``---`` frontmatter block.

    Only a block that OPENS the document counts, and it ends at the first line that is
    exactly ``---`` — so a body that QUOTES frontmatter further down keeps it. A document
    with no frontmatter (a pre-OKF entry) or with an unterminated one is returned
    unchanged: dropping text on a guess is worse than showing a little metadata.
    """
    match = _FRONTMATTER_RE.match(content_md)
    return content_md[match.end():] if match else content_md


def wrap_entry(
    summary_md: str,
    *,
    canonical_remote: str,
    session_id: str,
    covered_from: str | None,
    covered_to: str | None,
    model: str,
    prompt_version: str,
    item_types: list[str] | None = None,
    timestamp: str | None = None,
) -> str:
    """Returns the summary wrapped as an OKF concept (frontmatter + body).

    ``timestamp`` is injectable for tests; by default, UTC now (ISO, Z suffix).

    ``item_types`` (T.F3) is the aggregated label set of the summary's Decisions
    bullets, parsed by the PROCESSOR — never by the LLM, which does not produce
    YAML here for the same reason it never has. It is ADDITIVE in the strict sense:
    ``None`` or an empty list emits no line at all, so an entry captured under a
    prompt version that predates the label grammar keeps frontmatter byte-identical
    to what it had before this field existed (no retroactive re-typing of v1). The
    value is sorted and deduped here as well as at the call site: the field is a
    set, and two entries with the same labels must not differ by the order the
    parser happened to see them in.
    """
    ts = timestamp or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    title = extract_title(summary_md) or f"Session log {ts[:10]}"
    tag = canonical_remote.rsplit("/", 1)[-1] or canonical_remote
    types = sorted({t for t in (item_types or []) if t})
    lines = [
        "---",
        f"type: {ENTRY_TYPE}",
        f"title: {_y(title)}",
        f"timestamp: {_y(ts)}",
        "generated: true",
        "status: current",
        f"project: {_y(canonical_remote)}",
        f"tags: [{_y(tag)}]",
    ]
    if types:
        lines.append(f"item_types: {_y(types)}")
    lines += [
        "sources:",
        f"  - session: {_y(session_id)}",
        f"    covered_from: {_y(covered_from)}",
        f"    covered_to: {_y(covered_to)}",
        f"    model: {_y(model)}",
        f"    prompt: {_y(prompt_version)}",
        "---",
        "",
    ]
    return "\n".join(lines) + summary_md.strip() + "\n"


def wrap_digest(
    digest_md: str,
    *,
    canonical_remote: str,
    session_id: str,
    covered_from: str | None,
    covered_to: str | None,
    model: str,
    prompt_version: str,
    timestamp: str | None = None,
) -> str:
    """Returns a digest body wrapped as an OKF concept with ``type: digest`` (T.F6).

    Its own wrapper rather than a flag on :func:`wrap_entry`, because the two documents
    are different in every field that matters and a shared function would have to be
    told which of them it is building anyway:

    - the ``type`` is what every consumer keys on, and it must be a constant here rather
      than an argument a caller could get wrong;
    - the title is a LITERAL (see :data:`DIGEST_TITLE`) instead of the first bold line of
      the body. The body is model output *about* other entries, and
      :func:`extract_title` would copy a line of it into metadata;
    - there are no ``item_types``: a digest is not a session's decisions, and typing it
      would make it answer ``--type decision`` queries as if it were one.

    ``covered_from``/``covered_to`` carry the ENTRY-ID range the digest was built from
    (the analogue of a session entry's uuid range): a digest whose evidence anchor is
    unreadable is a claim about the project with nothing behind it.

    The frontmatter shape — the ``---`` fences, ``type`` on the first line inside them —
    is exactly :func:`wrap_entry`'s, because ``curate._frontmatter_type`` parses both
    with the same reader.
    """
    ts = timestamp or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    tag = canonical_remote.rsplit("/", 1)[-1] or canonical_remote
    lines = [
        "---",
        f"type: {DIGEST_TYPE}",
        f"title: {_y(f'{DIGEST_TITLE} ({ts[:10]})')}",
        f"timestamp: {_y(ts)}",
        "generated: true",
        "status: current",
        f"project: {_y(canonical_remote)}",
        f"tags: [{_y(tag)}]",
        "sources:",
        f"  - session: {_y(session_id)}",
        f"    covered_from: {_y(covered_from)}",
        f"    covered_to: {_y(covered_to)}",
        f"    model: {_y(model)}",
        f"    prompt: {_y(prompt_version)}",
        "---",
        "",
    ]
    return "\n".join(lines) + digest_md.strip() + "\n"
