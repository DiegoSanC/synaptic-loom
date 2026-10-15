"""``loom curate`` — client-side batch curation (RT.3.2/RT.3.3, E15) and the
threshold-gated per-project digest (RT.4.1, E16).

Runs ONLY on a dev machine (the server never runs an LLM, spec §1). Both LLM
passes reuse the frozen ``phase1-summarizer-invocation`` shape verbatim (claude -p
--safe-mode --tools "" --model X, payload via stdin, cwd=LOOM_HOME,
LOOM_SUMMARIZER=1); the deterministic code here — never the LLM — validates
verdicts, builds frontmatter, scans (I4, fail-closed) and POSTs.

State: ``~/.loom/curation_state.json`` (advance-only watermark + bounded attempts,
atomic writes) under an exclusive flock on ``~/.loom/curate.lock`` (its OWN lock:
a curate pass must not block capture passes). NOT state.json, which belongs to the
capture passes.

Frozen interfaces (t-curation-watermark, t-digest-entry): see ``docs/IMPORTED-IMPROVEMENTS-SPEC.md``
RT.3.2/RT.3.3 and RT.4.1.

The shape of the pass, and why it is shaped that way
----------------------------------------------------
One pass is: read the watermark → list the entries above it → take the OLDEST
``_MAX_ENTRIES_PER_BATCH`` of them → build a candidate pool per new entry (an FTS5
neighbour query, server-side) → ONE batched LLM call → **validate everything** →
scan every rationale → **record the batch as intent** → POST the links → advance the
watermark → **then the digest step**. The order is the design: nothing is posted until
every verdict in the batch has been proved to name a presented entry and a target from
THAT entry's own pool, and the watermark moves only after every POST came back 201 or
409.

The DIGEST step (T.F6) hangs off `_finish`, the one success tail, and re-reads the
project rather than reusing the listing above: the links this pass just posted are
exactly what moves entries out of ``current``, and "the digest summarises the CURRENT
entries" is a claim about the world after the batch landed. It is threshold-gated on its
OWN watermark (``digest_last_id``, advanced only on 201/409), so below
``curation.digest_threshold`` new current entries there is no second LLM call at all —
and a digest, being an entry, is excluded from that count, or every pass would find one
more "new" entry than the last and re-digest forever.

The intent log (``pending_batch`` in the state record) is what makes an INTERRUPTED
pass idempotent rather than merely repeatable. A partial batch is durable — there is
no ``DELETE /api/links`` — and the links that did land flip their targets out of
``current``, which is exactly what the candidate pool filters on; so a rerun that
asked the model again would be asking about a different logbook and could answer
differently, leaving a link set no single pass would produce. The rerun replays the
recorded verdicts instead: every link already posted answers 409, the rest are
created, and ``_finish`` is the only thing that clears the log (so a SIGKILL inside
the POST loop is covered too).

A project that fails ``MAX_ATTEMPTS`` times in a row is PARKED: no LLM pass runs for
it until a human clears the counter (``loom curate --reset`` / ``--from``). The
alternative is paying for a full pass on every run, forever.

The LLM is a pure function of stdin here. It gets no tools (``--tools ""``), it is
never told a path, it never sees a token and it never talks to the server; its
whole output is one line per entry in a grammar this module re-parses with a
regex. A verdict that does not fit, or that names an id nobody offered it, fails
the WHOLE batch closed: the alternative — posting the verdicts that did parse —
would make a partially-understood answer indistinguishable from a good one, and
the watermark would then step over the entries whose verdicts were dropped.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

from loom import okf, processor, scan_engine
from loom.api_client import ApiClient, ServerUnreachable
from loom.config import Config
from loom.normalize import normalize

VERDICT_KEEP = "keep"

# --- The two FROZEN grammars (a record-replay seam: E16/E17 build on them) ---------
# stdin: one block per new entry, each opening with ENTRY_MARKER; each candidate line
# opens with CANDIDATE_MARKER. Verdicts: one line per presented entry.
ENTRY_MARKER = "### ENTRY "
CANDIDATE_MARKER = "- CANDIDATE "
# What a marker found INSIDE embedded entry text is rewritten to (`_neutralize_markers`).
# Neither is a prefix of a real marker and neither reads as a heading or a list item, so
# nothing a model sees below one of these can be mistaken for the payload's own grammar.
NEUTRALIZED_ENTRY_MARKER = "[entry-marker] "
NEUTRALIZED_CANDIDATE_MARKER = "[candidate-marker] "
VERDICT_SUPERSEDES = "supersedes"
VERDICT_DUPLICATES = "duplicates"

# `[0-9]`, never `\d`: `\d` is Unicode-aware, so `١٠: supersedes:١` parses and
# `int()` happily converts it — a verdict naming an entry through digits nobody wrote
# in the payload. The ids in the payload are ASCII by construction, so the grammar is.
_VERDICT_RE = re.compile(
    r"^(?P<entry>[0-9]{1,18}):[ \t]*"
    rf"(?:(?P<keep>{VERDICT_KEEP})"
    rf"|{VERDICT_SUPERSEDES}:(?P<supersedes>[0-9]{{1,18}}(?:,[0-9]{{1,18}})*)"
    rf"|{VERDICT_DUPLICATES}:(?P<duplicates>[0-9]{{1,18}}))"
    r"(?:[ \t]+--[ \t]+(?P<rationale>.*))?$"
)

# The qualified form (D13 slice E, decision E5 — additive to the frozen grammar): an
# ITEMIZED entry is answered item by item, `<e>.<n>: keep|supersedes:<e>.<n>[,…]|
# duplicates:<e>.<n>`. Ids are the public item ids `loom log` prints: positive, ASCII,
# no padding — the shape the server's own `ITEM_ID_RE` accepts, so nothing the model
# writes can name an item through a spelling nobody presented.
_ITEM_ID = r"[1-9][0-9]{0,17}\.[1-9][0-9]{0,5}"
_ITEM_ID_RE = re.compile(_ITEM_ID)
_ITEM_VERDICT_RE = re.compile(
    rf"^(?P<item>{_ITEM_ID}):[ \t]*"
    rf"(?:(?P<keep>{VERDICT_KEEP})"
    rf"|{VERDICT_SUPERSEDES}:(?P<supersedes>{_ITEM_ID}(?:,{_ITEM_ID})*)"
    rf"|{VERDICT_DUPLICATES}:(?P<duplicates>{_ITEM_ID}))"
    r"(?:[ \t]+--[ \t]+(?P<rationale>.*))?$"
)
# The bullet and its label, stripped from an item before it is shown as
# `[<e>.<n>] [<type>] <text>`: the served `item_type` is the label, and a fallback item
# (`labelled = 0`) has none of its own.
_ITEM_LEAD_RE = re.compile(rf"^- (?:\[(?:{'|'.join(okf.ITEM_TYPES)})\] )?")

# What a candidate line shows of an entry: enough for the model to recognise the
# decision, flattened to ONE line because the grammar is line-oriented. The budget is
# spent on the entry's Decisions section (`_candidate_preview`), never on its
# frontmatter: a real one is ~430 characters on its own (F-1).
CANDIDATE_PREVIEW_CHARS = 400
# The same, per ITEM of an itemized candidate (slice E): every current item is shown,
# each with its id, so the budget is per item rather than per line.
CANDIDATE_ITEM_PREVIEW_CHARS = 200
# Longest rationale copied through to the POST. The FULL rationale is what gets
# secret-scanned (a prefix of clean text is clean, so truncating afterwards is safe);
# the server bounds the column at 2000.
MAX_RATIONALE_LEN = 500

# Candidate-pool query. `search_entries` rewrites a BARE term query into quoted terms
# joined by an implicit AND (necessary — raw FTS5 reads the `-` of `post-ensure` as a
# column filter), so passing an entry's text through would demand that a candidate
# contain EVERY word of it and the pool would come back empty. The query is therefore
# built as an explicit OR of quoted terms, which `_is_expression` passes through
# verbatim. `MAX_QUERY_LEN` server-side is 512: over it the request is a 422.
_MAX_QUERY_CHARS = 480
_MAX_QUERY_TERMS = 40
_MIN_TERM_LEN = 3
_TERM_RE = re.compile(r"[A-Za-z0-9]+")
# Words that match nearly every entry in a logbook of engineering summaries: they cost
# query budget and contribute no discrimination. Deliberately short — bm25 handles the
# rest, and a long hand-tuned list is a thing that rots.
_STOP_TERMS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "into", "was", "were", "are",
    "not", "but", "its", "our", "you", "your", "has", "have", "had", "will", "would",
    "can", "could", "should", "than", "then", "there", "their", "when", "what", "which",
    "who", "why", "how", "all", "any", "how", "out", "use", "used", "using", "one",
    "two", "new", "now", "get", "got", "see", "per", "via", "also", "only", "more",
    "most", "some", "such", "each", "over", "under", "after", "before", "because",
    # The OKF frontmatter/section furniture every single entry carries.
    "context", "decisions", "decision", "status", "current", "evidence", "sources",
    "session", "type", "title", "timestamp", "generated", "project", "tags", "model",
    "prompt", "covered", "from", "log", "entry",
})

# Bounds on ONE pass. The listing endpoint clamps `limit` to 500 and orders newest
# first, so a project that accumulates more than this many uncurated entries cannot be
# paged down to its watermark — see `_ERR_WATERMARK_GAP`, which is the loud, harmless
# way that ends.
_MAX_ENTRIES_PER_PASS = 500
# How many new entries ONE LLM call is allowed to carry. The listing above is what the
# WATERMARK is reasoned about; this is what the PASS costs. Without it a first curate
# on an existing logbook builds one prompt of (up to) 500 entry bodies plus 8 candidate
# previews each — which times out, freezes the watermark, and rebuilds exactly the same
# oversized batch on the next run: no progress, forever. Capping it makes a backlog
# drain in bounded steps instead, because the watermark advances by one batch each time.
_MAX_ENTRIES_PER_BATCH = 100
# How many ranked neighbours to ask for when building ONE entry's candidate pool.
# Deliberately the server's own `search.MAX_LIMIT`, not a small multiple of K: the age
# and status filters below are CLIENT-side post-filters over a rank-truncated page, and
# the wave-i tie-break (`rank ASC, created_at DESC, id DESC`) hands out the newest
# entries first whenever real entries about one subsystem tie on bm25 — which they do.
# Measured on a 400-entry index: a 32-row over-fetch left 54% of entries with an EMPTY
# pool (the 32 rows were nearly all newer than the entry and every one was discarded),
# and an empty pool means the batch can only answer `keep` while the watermark advances
# past it — an unrepeatable, silently lost decision. At 500 that is 0%. The ranking cost
# is server-side and already paid; this stays one query per entry either way. (The
# cleaner fix is a `before_id` bound on `search._SELECT`, which would change a FROZEN
# signature in a file this epic does not own: recorded as a follow-up.)
_POOL_FETCH_LIMIT = _MAX_ENTRIES_PER_PASS
# Failed passes after which a project is PARKED: no further LLM call runs for it until a
# human clears the counter. The P4 discipline `processor._bound_attempts` applies to
# markers, and the manifest asks for it here in the same words ("P4-style bounded
# attempts"). There is no marker to park — only this per-project counter — so the two
# knobs that clear it are `loom curate --reset` and `loom curate --from <entry_id>`.
MAX_ATTEMPTS = 3

# `last_error` values. CODES, never text: a rationale can quote a credential and the
# raw output of an LLM pass is attacker-influenceable content, and this string is
# persisted and printed by `loom status` (I5/I6).
_ERR_UNPARSEABLE = "unparseable_verdicts"
_ERR_UNKNOWN_ENTRY = "verdict_for_unpresented_entry"
_ERR_DUPLICATE_VERDICT = "duplicate_verdict_line"
_ERR_MISSING_VERDICT = "missing_verdict_line"
_ERR_TARGET_OUTSIDE_POOL = "verdict_target_outside_pool"
_ERR_SECRET_IN_RATIONALE = "secret_in_rationale"
_ERR_SCANNER_FAILED = "scanner_unavailable"
_ERR_LLM_FAILED = "curation_pass_failed"
_ERR_POST_FAILED = "link_post_failed"
_ERR_SERVER = "server_error"
_ERR_WATERMARK_GAP = "too_many_uncurated_entries"
# E16 (the digest step). Its own codes, so `loom status` distinguishes "the links are
# fine, the digest is not" from a failure of the curation pass itself.
_ERR_DIGEST_PROMPT = "digest_prompt_unreadable"
_ERR_DIGEST_FAILED = "digest_pass_failed"
_ERR_DIGEST_EMPTY = "digest_pass_empty"
_ERR_SECRET_IN_DIGEST = "secret_in_digest"
_ERR_DIGEST_POST_FAILED = "digest_post_failed"

# OKF `type:` of a digest entry (E16). Digests summarise other entries, so they are
# neither curated nor offered as candidates: a digest superseding the entries it was
# built from would collapse the whole logbook. Imported from `okf` rather than declared
# twice: `okf.wrap_digest` WRITES this literal and everything here READS it, and two
# copies is how a digest stops being recognised as one.
DIGEST_TYPE = okf.DIGEST_TYPE
_FRONTMATTER_TYPE_RE = re.compile(r"^type:[ \t]*(?P<type>\S+)[ \t]*$", re.MULTILINE)

# --- The digest pass (T.F6) -------------------------------------------------------
# `prompt_version` stamped on the digest entry. FROZEN: it is how a consumer (and a
# human reading `entries`) tells a digest row from a session summary without parsing
# markdown, and it enters the row's `content_hash` material.
DIGEST_PROMPT_VERSION = "digest-v1"
# The prompt file, shipped by this repo and copied into LOOM_HOME by install.sh. NOT a
# config key on purpose: `summarizer.prompt_path` exists for testability and is already
# documented as a way to point the summarizer at another file, and a second such knob
# here would be a config-driven arbitrary-file read whose content leaves the machine
# inside a prompt (I3). The only two places looked at are the installed copy and this
# checkout's copy.
DIGEST_PROMPT_FILENAME = "digest_v1.md"
# Longest stdin ONE digest pass may carry. The listing is already clamped to 500 entries,
# but 500 session summaries is megabytes of prompt: the call would exceed the model's
# context, fail, count an attempt, and park the project after three runs — the digest
# would then be permanently unreachable for exactly the projects that need it most.
# Entries are taken NEWEST-first until this is exhausted, so what falls off is the oldest
# history, and the operator is told how many were dropped.
_MAX_DIGEST_PAYLOAD_CHARS = 200_000
# Longest digest body accepted from the model. The prompt asks for under 6000 characters;
# this is the bound that does not depend on the model obeying it. Truncated rather than
# refused: a refusal would count an attempt and park the project over a formatting
# excess, while a truncated digest is stored WITH a visible marker and still readable.
_MAX_DIGEST_OUTPUT_CHARS = 20_000
_DIGEST_TRUNCATION_NOTE = "\n\n[... digest truncated at {limit} characters]"

# The prompt (an ARGUMENT to `claude -p`, exactly like the summarizer's). It carries
# the grammars and nothing else: no path, no token, no URL, no tool. Every id it may
# legitimately use is one that appears in the payload it is reading.
CURATION_PROMPT = """You are curating a team decision logbook.

Standard input contains one block per NEW entry. A block opens with a line
"### ENTRY <id>". What follows depends on the entry:

  - An ITEMIZED entry lists its decisions one per line, each opening with its
    item id: "[<id>.<n>] [<type>] <text>". Its candidate lines have the form
    "- CANDIDATE <id>: [<id>.<n>] [<type>] <preview> [<id>.<n>] ..." and list the
    still-current items of an EARLIER entry of the same project that might be
    related.
  - Any other entry is given as its markdown, and its candidate lines have the
    form "- CANDIDATE <id>: <one-line preview>": whole EARLIER entries.

Judge every item of an itemized entry on its own, and every other entry as a
whole. Decide whether it:
  - REPLACES one or more candidates, because it reverses, overrides or obsoletes
    what they recorded                                             -> supersedes
  - RECORDS THE SAME THING as exactly one candidate, with no new decision
    -> duplicates
  - stands on its own                                               -> keep

Answer with ONE line per item of an itemized entry and ONE line per other
"### ENTRY <id>" block, in the order they appear, and with NOTHING else — no
preamble, no commentary, no blank lines, no markdown. Each line must be exactly
one of:

  <id>.<n>: keep
  <id>.<n>: supersedes:<id>.<n>[,<id>.<n>...]
  <id>.<n>: duplicates:<id>.<n>
  <id>: keep
  <id>: supersedes:<id>[,<id>...]
  <id>: duplicates:<id>

optionally followed by " -- <short rationale>" (one line, no secrets, no file
paths, no credentials — quote nothing you would not publish).

Hard rules:
  - An itemized entry is answered item by item, never as a whole, and every item
    listed under it gets exactly one line. Any other entry gets one "<id>:" line.
  - Every id after the colon MUST be listed on a "- CANDIDATE" line inside THAT
    SAME block: an item id "<id>.<n>" when you answer an item, an entry id when
    you answer a whole entry. Never invent an id, never reuse one from another
    block.
  - An item replaces only the candidate items it really reverses: leave the other
    items of that earlier entry alone.
  - When in doubt, answer "keep". A wrong link hides a decision from the team.
"""


# --------------------------------------------------------------------------- #
# Small helpers                                                                #
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _git_remote_url(cwd: str) -> str | None:
    """Raw URL of the ``origin`` remote in ``cwd``; ``None`` if there is none.

    A local copy rather than an import from `loom.cli`: `cli` imports THIS module to
    wire the subcommand, so importing it back would be a cycle, and reaching into a
    private of the CLI from a library module is worse than six lines.
    """
    try:
        cp = subprocess.run(  # noqa: S603 — fixed argv, no shell, bounded
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    return cp.stdout.strip() or None


def entry_status(entry: dict) -> str:
    """Derived status of an entry as served by the API — DEFAULTING to ``current``.

    The endpoint carries `status` on every entry (t-entry-links), so the default is not
    normally reached. It is kept, and it is `"current"`, for the two cases that outlive
    any one release: an entry dict from a server that predates Block T, and a hand-built
    dict in a test. Any OTHER default — or `entry["status"]` — turns those into an empty
    candidate pool and a `loom log` that collapses everything.

    The ONE derivation on the client side: `cli._entry_status` is this function, not a
    copy of it. Two copies of a three-line rule is how the collapsed rendering and the
    candidate pool stop agreeing about what `current` means.
    """
    status = entry.get("status")
    return status if isinstance(status, str) and status else "current"


def _frontmatter_type(content_md: str) -> str | None:
    """The OKF ``type:`` of an entry, read from its leading frontmatter block.

    Only the block delimited by the first two ``---`` lines is inspected: a ``type:``
    line further down is body text (an entry that QUOTES frontmatter, which a session
    about this very format will), and treating it as metadata would silently drop a
    real entry out of every candidate pool.
    """
    if not content_md.startswith("---\n"):
        return None
    end = content_md.find("\n---", 3)
    if end == -1:
        return None
    match = _FRONTMATTER_TYPE_RE.search(content_md[4:end + 1])
    return match.group("type") if match else None


def is_digest(entry: dict) -> bool:
    """Is this entry a per-project digest (E16's ``type: digest``)?"""
    return _frontmatter_type(entry.get("content_md") or "") == DIGEST_TYPE


def _flatten(text: str, limit: int) -> str:
    """One line, collapsed whitespace, truncated — safe to put in a grammar line."""
    flat = " ".join((text or "").split())
    if len(flat) > limit:
        flat = flat[:limit - 1].rstrip() + "…"
    return flat


def _candidate_preview(content_md: str) -> str:
    """What the model sees of a candidate: its Decisions section, one line (F-1).

    The frontmatter is dropped first — it is metadata the processor wrote, and at ~430
    characters it used to fill the whole preview, so the model judged candidates by
    their title alone. Then the Decisions section is preferred over the rest of the body
    because it is what a new entry can supersede or duplicate; an entry without one (a
    v1 or pre-OKF capture) is previewed from its whole body. The section is read with
    the PROCESSOR's own reader, so "the Decisions section" means the same lines here as
    it does for the entry's `item_types`.
    """
    text = okf.body(content_md or "")
    decisions = "\n".join(processor._decisions_section(text))
    return _flatten(decisions if decisions.strip() else text, CANDIDATE_PREVIEW_CHARS)


def _neutralize_markers(text: str) -> str:
    """REWRITES any line of embedded entry text that opens with a grammar marker.

    Entry bodies are LLM summaries of arbitrary sessions: a session about this feature
    (or a hostile one) can contain a line starting with ``### ENTRY `` or
    ``- CANDIDATE ``. Left alone it forges a block header or an extra candidate, and the
    model answers about entries nobody offered it.

    The marker is REPLACED, not indented. The consumer is a language model, not a
    regex: ``\\t### ENTRY 11`` still reads as a heading to a model — and so does
    `` ### ENTRY 11``, which is exactly what an indenting neutralizer PRODUCES. Since
    validation bounds every verdict to (presented entry, target from that entry's own
    pool), the residual case is narrow but real: entry text that fabricates a block for
    another presented entry, naming a candidate genuinely in *that* entry's pool, can
    talk the model into a durable link on fabricated evidence. Rewriting the marker
    leaves nothing that reads as one; the text itself is never censored.

    ``split("\\n")``, not ``splitlines()``: the latter also splits on VT, FF, NEL and
    U+2028, so rejoining with ``\\n`` would silently rewrite entry content that merely
    contains one of them.
    """
    out = []
    for line in (text or "").split("\n"):
        stripped = line.lstrip()
        if stripped.startswith(ENTRY_MARKER):
            out.append(NEUTRALIZED_ENTRY_MARKER + stripped[len(ENTRY_MARKER):])
        elif stripped.startswith(CANDIDATE_MARKER):
            out.append(NEUTRALIZED_CANDIDATE_MARKER + stripped[len(CANDIDATE_MARKER):])
        else:
            out.append(line)
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Candidate pool                                                               #
# --------------------------------------------------------------------------- #
def pool_query(content_md: str) -> str | None:
    """An explicit FTS5 OR expression over the entry's distinctive terms.

    Returns ``None`` when the entry yields no usable term (an entry of pure
    punctuation, or one whose every word is furniture): there is then no question to
    ask the index, and asking with an empty string would be a 422.

    Terms come from the BODY (:func:`okf.body`): the frontmatter's timestamp digits,
    uuid fragments, model name and forge host match every entry in the logbook, and
    first-appearance order used to spend most of the query on them (F-1).

    Terms are taken in order of first appearance, deduplicated, stop-listed and capped
    by BOTH a term count and a character budget under the server's ``MAX_QUERY_LEN``.
    First-appearance order means a very long entry contributes only its head to the
    query — accepted (bm25 ranks whatever the OR admits, and pilot entries are one
    session's summary), and the alternative heuristics all rank common words up.
    """
    seen: set[str] = set()
    terms: list[str] = []
    length = 0
    for match in _TERM_RE.finditer(okf.body(content_md or "")):
        term = match.group(0).lower()
        if len(term) < _MIN_TERM_LEN or term in _STOP_TERMS or term in seen:
            continue
        seen.add(term)
        # `"term"` plus the ` OR ` that joins it to the previous one.
        cost = len(term) + 2 + (4 if terms else 0)
        if length + cost > _MAX_QUERY_CHARS:
            break
        terms.append(term)
        length += cost
        if len(terms) >= _MAX_QUERY_TERMS:
            break
    if not terms:
        return None
    # The terms are `[A-Za-z0-9]+` by construction, so nothing inside a quoted string
    # can close it; the value is bound to MATCH server-side either way.
    return " OR ".join(f'"{term}"' for term in terms)


def _candidates_for(
    api: ApiClient, project_id: int, entry: dict, k: int,
    items_of: dict[int, list[dict]] | None = None,
) -> list[dict]:
    """Top-K FTS5 neighbours of ``entry`` among CURRENT, non-digest, OLDER entries.

    OLDER is the load-bearing filter, and it is what keeps the contract's "from_entry
    is the NEWER entry" true without the endpoint having to police it: a pool that
    could offer a newer entry lets the batch produce `old supersedes new`, which reads
    as "the reversal was reverted" in `loom log`. Ids are monotonic, so id order is
    the arrival order.

    CURRENT excludes entries already superseded or marked duplicate — linking to them
    again adds nothing the reader can act on. Digests are excluded because they
    summarise other entries (E16).

    With ``items_of`` (slice E) the items decide too, and it is filled here for every
    hit that passed the filters above: an itemized candidate qualifies while at least
    one of its items is still current, and an entry is only offered candidates of its
    own kind: an itemized entry's verdicts name items (an un-itemized entry has none),
    and an un-itemized entry's verdict is a whole-entry link, which on an itemized
    candidate would hide every item of it, siblings included (#6). The query of an
    itemized entry is drawn from its items' text.

    All of these filters are CLIENT-side, which is why the over-fetch is the server's
    whole page (:data:`_POOL_FETCH_LIMIT`) rather than a small multiple of K: a
    truncated page is ranked newest-first among bm25 ties, so for an older entry nearly
    every row it returns is one this filter must discard. See the constant for the
    measurement.
    """
    own = (items_of or {}).get(entry["id"]) or []
    query = pool_query("\n".join(item["text_md"] for item in own) if own
                       else entry.get("content_md") or "")
    if query is None:
        return []
    resp = api._request(
        "GET", f"/api/projects/{project_id}/entries",
        params={"q": query, "limit": _POOL_FETCH_LIMIT},
    )
    if resp.status_code != 200:
        raise _HttpFailure(f"GET entries?q= returned {resp.status_code}")
    hits = _json_body(resp, "the candidate search").get("entries") or []

    eligible = [hit for hit in hits
                if isinstance(hit, dict) and isinstance(hit.get("id"), int)
                and hit["id"] < entry["id"]
                and entry_status(hit) == "current" and not is_digest(hit)]
    if items_of is not None:
        _fetch_items(api, project_id, [hit["id"] for hit in eligible], items_of)
    pool: list[dict] = []
    for hit in eligible:
        if items_of is not None:
            its = items_of.get(hit["id"]) or []
            if its and not _current_items(its):
                continue
            if bool(own) != bool(its):
                # An item verdict can only name items, and an ENTRY verdict on an
                # itemized entry would hide every item of it, siblings included (#6):
                # an entry is only compared with entries of its own kind.
                continue
        pool.append(hit)
        if len(pool) >= k:
            break
    return pool


def _current_items(items: list[dict] | None) -> list[dict]:
    """The items no active link has replaced — the ones a reader still acts on."""
    return [item for item in items or () if item.get("status") == "current"]


def _error_code(resp) -> str | None:
    """The ``error`` code of a JSON error body, or ``None``."""
    try:
        body = resp.json()
    except ValueError:
        return None
    return body.get("error") if isinstance(body, dict) else None


def _valid_item(row: object) -> bool:
    return (isinstance(row, dict)
            and all(isinstance(row.get(key), int) and not isinstance(row.get(key), bool)
                    and row[key] >= 1 for key in ("entry_id", "ordinal"))
            and row.get("item_type") in okf.ITEM_TYPES
            and isinstance(row.get("text_md"), str) and row["text_md"].strip() != ""
            and isinstance(row.get("status"), str))


def _fetch_items(api: ApiClient, project_id: int, entry_ids: list[int],
                 items_of: dict[int, list[dict]]) -> None:
    """Fill ``items_of`` with ``{entry id: its items, by ordinal}`` for ``entry_ids``.

    Entries already in the map are not asked for again (the batch and every candidate
    pool share one map), and an entry with no items maps to ``[]``. A server from before
    D13 answers the route with ``404 not_found``: nothing there is itemized. Anything
    else that is not a 200 carrying well-formed items stops the pass — a missing item
    would leave an itemized entry partly judged, and treating the entry as un-itemized
    would post a whole-entry link where the reader expected one item.
    """
    missing = [entry_id for entry_id in dict.fromkeys(entry_ids)
               if entry_id not in items_of]
    for start in range(0, len(missing), _MAX_ENTRIES_PER_PASS):
        chunk = missing[start:start + _MAX_ENTRIES_PER_PASS]
        resp = api._request("GET", f"/api/projects/{project_id}/items",
                            params={"entry_ids": ",".join(map(str, chunk))})
        found: dict[int, list[dict]] = {entry_id: [] for entry_id in chunk}
        if resp.status_code == 404 and _error_code(resp) == "not_found":
            items_of.update(found)
            continue
        if resp.status_code != 200:
            raise _HttpFailure(f"listing the entries' items returned HTTP {resp.status_code}")
        rows = _json_body(resp, "the items listing").get("items")
        if not isinstance(rows, list) or not all(_valid_item(row) for row in rows):
            raise _HttpFailure("the items listing did not return well-formed items")
        for row in rows:
            if row["entry_id"] in found:
                found[row["entry_id"]].append(row)
        for entry_items in found.values():
            entry_items.sort(key=lambda row: row["ordinal"])
        items_of.update(found)


def _item_id(item: dict) -> str:
    return f"{item['entry_id']}.{item['ordinal']}"


def _item_line(item: dict, limit: int | None = None) -> str:
    """``[<e>.<n>] [<type>] <text>``: one item on one line (the grammar is
    line-oriented), its bullet and label replaced by the served id and type."""
    text = _ITEM_LEAD_RE.sub("", " ".join(item["text_md"].split()), count=1)
    if limit is not None:
        text = _flatten(text, limit)
    return f"[{_item_id(item)}] [{item['item_type']}] {text}"


def _list_entries(api: ApiClient, project_id: int) -> tuple[list[dict], bool]:
    """``(one page of the project's entries, has_more)``, newest first.

    Entries are validated into dicts carrying an int id; ``has_more`` is the server's own
    "there are older rows below this page", which both callers need — the curation pass
    to refuse a backlog it cannot page down to, the digest step to say out loud that it
    summarised one page of a larger project.

    Raises :class:`_HttpFailure` for anything that is not a 200 carrying a JSON object.
    """
    resp = api._request("GET", f"/api/projects/{project_id}/entries",
                        params={"limit": _MAX_ENTRIES_PER_PASS})
    if resp.status_code != 200:
        raise _HttpFailure(
            f"listing the project's entries returned HTTP {resp.status_code}")
    body = _json_body(resp, "the project's entry listing")
    entries = [e for e in (body.get("entries") or [])
               if isinstance(e, dict) and isinstance(e.get("id"), int)]
    return entries, bool(body.get("has_more"))


class _HttpFailure(Exception):
    """The server answered, but not with something this pass can build on."""


class CurationStateError(Exception):
    """``curation_state.json`` exists but cannot be read as curation state."""


def _json_body(resp, what: str) -> dict:
    """The JSON object of a 200, or :class:`_HttpFailure`.

    A body that is not JSON (a proxy's HTML error page served with a 200, a truncated
    response) must not become a traceback halfway through a pass: it is the same class
    of event as a 5xx, and the pass ends the same way.
    """
    try:
        body = resp.json()
    except ValueError as exc:
        raise _HttpFailure(f"{what} did not return JSON") from exc
    if not isinstance(body, dict):
        raise _HttpFailure(f"{what} did not return an object")
    return body


# --------------------------------------------------------------------------- #
# Payload (the frozen stdin grammar)                                           #
# --------------------------------------------------------------------------- #
def build_payload(items: list[tuple[dict, list[dict]]],
                  items_of: dict[int, list[dict]] | None = None) -> str:
    """The batched stdin payload: one ``### ENTRY <id>`` block per new entry.

    An entry with items in ``items_of`` (slice E) is presented ITEM by item, one
    ``[<e>.<n>] [<type>] <text>`` line each, and its candidates as their current items;
    every other entry keeps the frozen entry form byte for byte.

    Entry text ONLY (I3). No path, no URL, no token, no filename ever enters this
    string — the LLM's whole world is the logbook text it is asked about.
    """
    items_of = items_of or {}
    blocks = []
    for entry, candidates in items:
        own = items_of.get(entry["id"]) or []
        if own:
            lines = [f"{ENTRY_MARKER}{entry['id']}"] + [_item_line(item) for item in own]
            offered = [
                f"{CANDIDATE_MARKER}{c['id']}: " + " ".join(
                    _item_line(item, CANDIDATE_ITEM_PREVIEW_CHARS)
                    for item in _current_items(items_of.get(c["id"])))
                for c in candidates
            ]
        else:
            lines = [f"{ENTRY_MARKER}{entry['id']}",
                     _neutralize_markers((entry.get("content_md") or "").strip())]
            offered = [
                f"{CANDIDATE_MARKER}{c['id']}: {_candidate_preview(c.get('content_md'))}"
                for c in candidates
            ]
        lines.append("")
        lines += offered or ["(no candidates)"]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def _run_llm(cfg: Config, prompt: str, payload: str) -> subprocess.CompletedProcess:
    """The frozen ``phase1-summarizer-invocation`` shape, verbatim (I3).

    Same flags, same env, same cwd as `processor._run_summarizer` — only the prompt
    and the stdin differ. `--tools ""` is the real tool block: the model cannot post,
    cannot read a file and cannot reach the server. The payload goes in through stdin,
    never as an argument (arguments are world-readable in `ps`).

    ONE function for both of this module's passes (the verdict pass and E16's digest
    pass): the invocation is the frozen I3 profile, and two copies of it is two places
    a flag can be dropped. The PROMPT is the only thing that varies, and it is always a
    string this repo ships — never a path, never anything read out of an entry.

    The timeout is `curation.timeout_s`, NOT the summarizer's. `summarizer.timeout_s`
    (300s) budgets one session delta; these calls carry up to
    :data:`_MAX_ENTRIES_PER_BATCH` entry bodies with K candidate previews each (verdict
    pass) or a whole project's current entries (digest pass). Reusing the smaller budget
    is a loop with no exit: the pass times out, the watermark stays, and the next run
    rebuilds the very same batch.
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
        prompt,
    ]
    env = {**os.environ, "LOOM_SUMMARIZER": "1"}
    return subprocess.run(  # noqa: S603 — fixed argv, no shell, bounded by timeout
        cmd,
        input=payload,
        env=env,
        cwd=str(cfg.loom_home),
        timeout=cfg.curation_timeout_s,
        capture_output=True,
        text=True,
    )


def _run_curator(cfg: Config, payload: str) -> subprocess.CompletedProcess:
    """The verdict pass (T.F5): :data:`CURATION_PROMPT` over the batch payload."""
    return _run_llm(cfg, CURATION_PROMPT, payload)


# --------------------------------------------------------------------------- #
# Verdicts (the frozen verdict grammar) — validated BEFORE any POST            #
# --------------------------------------------------------------------------- #
class VerdictError(ValueError):
    """A batch that must not be posted. ``code`` is what reaches ``last_error``."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


def parse_verdicts(stdout: str, pools: dict[int, set[int]],
                   item_pools: dict[str, set[str]] | None = None) -> list[dict]:
    """Parse and FULLY validate one batch of verdicts. Raises :class:`VerdictError`.

    ``pools`` maps each entry presented WHOLE to the set of candidate ids that entry was
    actually offered; ``item_pools`` (slice E) maps each presented ITEM, ``"<e>.<n>"``,
    to the candidate item ids of its block. An itemized entry is in ``item_pools``
    through its items and never in ``pools``, so an entry-form line for it is a verdict
    for something nobody presented. Four independent things are checked, and any one of
    them fails the whole batch:

    1. every non-blank line matches one of the two grammars exactly;
    2. every verdict names a unit (entry or item) this pass presented — and appears
       once;
    3. every presented unit THAT WAS OFFERED AT LEAST ONE CANDIDATE gets a verdict. A
       truncated answer is otherwise indistinguishable from a batch of `keep`s, and
       since the watermark advances on success those units would never be examined
       again. A unit with an empty pool is exempt: `keep` is its only legal verdict
       (rule 4 rejects every target), so silence about it carries no information to
       lose. A line for it is still accepted, and still validated;
    4. every target belongs to THAT unit's pool. This is the hostile-fixture case:
       an id the model invented, or borrowed from another block, names a real row in
       the database that nobody proposed linking.

    Returns one dict per verdict, ``{"entry": <id>, …}`` or ``{"item": "<e>.<n>", …}``,
    with ``kind`` (``None`` for `keep`), ``targets`` and ``rationale``.
    """
    item_pools = item_pools or {}
    verdicts: list[dict] = []
    seen: set[int | str] = set()
    # `split("\n")`, not `splitlines()`: the latter also breaks on VT, FF, NEL and
    # U+2028, so ONE line of model output could become two verdict lines whose halves
    # were never written as verdicts.
    for raw in (stdout or "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        match = _VERDICT_RE.match(line)
        if match is not None:
            unit: int | str = int(match.group("entry"))
            pool: set = pools.get(unit) if unit in pools else None
            to_target = int
        else:
            match = _ITEM_VERDICT_RE.match(line)
            if match is None:
                raise VerdictError(_ERR_UNPARSEABLE)
            unit = match.group("item")
            pool = item_pools.get(unit)
            to_target = str
        if pool is None:
            raise VerdictError(_ERR_UNKNOWN_ENTRY)
        if unit in seen:
            raise VerdictError(_ERR_DUPLICATE_VERDICT)
        seen.add(unit)

        if match.group("keep"):
            kind, targets = None, []
        elif match.group("supersedes"):
            kind = VERDICT_SUPERSEDES
            targets = [to_target(t) for t in match.group("supersedes").split(",")]
        else:
            kind = VERDICT_DUPLICATES
            targets = [to_target(match.group("duplicates"))]

        for target in targets:
            if target not in pool:
                raise VerdictError(_ERR_TARGET_OUTSIDE_POOL)
        if len(set(targets)) != len(targets):
            raise VerdictError(_ERR_UNPARSEABLE)

        rationale = (match.group("rationale") or "").strip() or None
        key = "entry" if to_target is int else "item"
        verdicts.append({key: unit, "kind": kind, "targets": targets,
                         "rationale": rationale})

    # Only units that were OFFERED something can be silent about it. A unit with an
    # empty pool has exactly one legal verdict — `keep` — because rule 4 rejects every
    # target, so its absent line is not ambiguous and cannot hide a truncated answer.
    # Demanding it anyway is what made the first real pass of a project impossible: its
    # oldest entry is always offered zero candidates.
    presented = {unit for unit, pool in pools.items() if pool}
    presented |= {unit for unit, pool in item_pools.items() if pool}
    missing = presented - seen
    if missing:
        raise VerdictError(_ERR_MISSING_VERDICT)
    return verdicts


def _scan_rationales(cfg: Config, verdicts: list[dict]) -> None:
    """I4, fail-closed: a rationale that may carry a secret fails the WHOLE batch.

    The rationale is the one field of a link whose text was written by the LLM out of
    entry content, so it is the one field that can carry a credential the capture-side
    scan never saw (a quarantined entry's content never reaches the server, but a
    curation pass reads the entries that DID). A scanner that could not produce a
    verdict counts as a hit — the same policy, and the same reason, as processor step 8
    and `push-quarantine`.

    Nothing about the finding is recorded or printed beyond the code: the redacted
    value would still be a channel out of the fail-closed path.
    """
    for verdict in verdicts:
        if not verdict["rationale"]:
            continue
        try:
            findings = scan_engine.scan(verdict["rationale"], cfg)
        except Exception as exc:  # noqa: BLE001 — no verdict == a hit (I4).
            raise VerdictError(_ERR_SCANNER_FAILED, type(exc).__name__) from exc
        if findings:
            raise VerdictError(_ERR_SECRET_IN_RATIONALE)


# --------------------------------------------------------------------------- #
# State (per-project watermark, advance-only, atomic)                          #
# --------------------------------------------------------------------------- #
def _load_state(cfg: Config) -> dict:
    """The curation state, or a fresh one. An unreadable file is NOT overwritten.

    Returning ``{}`` for a corrupt file would reset every watermark to 0 and re-bill a
    full LLM pass over the whole logbook — so a file that cannot be parsed raises and
    the pass stops, leaving the bytes for a human. (The processor's `state.json` takes
    the opposite route because there the alternative is capturing nothing at all; here
    the alternative is spending money and posting links that were already posted.)
    """
    try:
        raw = cfg.curation_state_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"projects": {}, "updated_at": _now_iso()}
    except OSError as exc:
        raise CurationStateError(type(exc).__name__) from exc
    try:
        data = json.loads(raw)  # a corrupt file stops the pass; it is NEVER rewritten
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CurationStateError(type(exc).__name__) from exc
    if not isinstance(data, dict):
        raise CurationStateError("not an object")
    projects = data.get("projects")
    if projects is None and "projects" not in data:
        # A state file this module wrote always has the key; one written by a future
        # version might legitimately not. An ABSENT map is an empty map.
        data["projects"] = {}
    elif not isinstance(projects, dict):
        # A `projects` value that is a list, a string or null is NOT an empty state:
        # replacing it with {} makes the first `_save` PERSIST that reset, and every
        # watermark in the file is gone. Reproduced: `watermark_id: 4242` came back as
        # 7, so the whole logbook was re-curated from zero. Same rule as an unparseable
        # file — stop the pass and leave the bytes for a human.
        raise CurationStateError("'projects' is not an object")
    return data


def _record(state: dict, remote: str) -> dict:
    """This project's record, with the four known fields NORMALIZED and the rest kept.

    Preserve-and-merge, because `_save` replaces the whole project record with what this
    returns: rebuilding it from four literals silently erases every field another epic
    added (E16's digest bookkeeping, E17's) on the next curate pass.
    """
    stored = state["projects"].get(remote)
    if not isinstance(stored, dict):
        stored = {}
    record = dict(stored)
    record.update({
        "watermark_id": _non_negative_int(stored.get("watermark_id")),
        "digest_last_id": _non_negative_int(stored.get("digest_last_id")),
        "attempts": _non_negative_int(stored.get("attempts")),
        "last_error": stored.get("last_error") if isinstance(
            stored.get("last_error"), str) else None,
    })
    return record


def _non_negative_int(value: object) -> int:
    """A hand-editable file may hold anything; a watermark is a count or it is 0."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def digest_attempts(record: dict) -> int:
    """The digest step's own failure counter, read totally (E16).

    Deliberately NOT one of `_record`'s normalized fields: it rides on the
    preserve-and-merge that E15 built for exactly this — a field a later epic adds —
    so a record that predates it, or one a human edited into nonsense, reads as 0 here
    instead of changing the shape every consumer of `_record` sees.
    """
    return _non_negative_int(record.get("digest_attempts"))


def _save(cfg: Config, state: dict, remote: str, record: dict) -> None:
    """Persists one project's record atomically and durably.

    Reuses `processor._atomic_write_json` on purpose (the same reach the CLI already
    makes into that module): unique temp + fsync + `os.replace` + directory fsync is
    exactly what this file needs, and a second copy of it would be a second chance to
    get it subtly weaker. mkstemp's 0600 travels with the rename, so the state file is
    never world-readable.
    """
    state["projects"][remote] = record
    state["updated_at"] = _now_iso()
    processor._atomic_write_json(cfg.curation_state_path, state)


def _fail(cfg: Config, state: dict, remote: str, record: dict, code: str,
          message: str, posted: int = 0, *, digest_step: bool = False) -> int:
    """Fail closed: the watermark stays put, the attempt is counted, exit 1.

    ``posted`` is how many links this pass DID create before it gave up. The old text
    said "nothing was posted" unconditionally, which is a false statement on exactly the
    paths where it matters (`_ERR_POST_FAILED`, and a server that dies mid-loop): those
    links are durable and there is no `DELETE /api/links`. When links were posted the
    pass leaves its `pending_batch` behind, so the next run replays the SAME verdicts
    rather than asking the model to judge a logbook the half-posted batch has changed.

    ``digest_step`` is the THIRD framing, and it exists because the first two are both
    false there: by the time the digest runs, `_finish` has printed a success line and
    persisted an advanced watermark, so "nothing was posted; watermark unchanged" would
    contradict the line above it — and the links really did land. It also charges the
    SEPARATE `digest_attempts` counter: `attempts` is zeroed by every successful
    `_finish` (correctly — the linking succeeded), so a digest failure that only
    incremented `attempts` could never reach `MAX_ATTEMPTS` and the digest step would
    re-bill a full LLM pass on every run, forever. `attempts` is incremented too, because
    the pass as a whole did fail and that is what `loom status` reports.
    """
    record["attempts"] = record["attempts"] + 1
    if digest_step:
        record["digest_attempts"] = digest_attempts(record) + 1
    record["last_error"] = code
    _save(cfg, state, remote, record)
    if digest_step:
        print(f"loom curate: the curation pass succeeded; the DIGEST step did not — "
              f"{message}.", file=sys.stderr)
        print("  the links and the watermark above stand; no digest was posted and the "
              "digest watermark is unchanged.", file=sys.stderr)
    elif posted:
        print(f"loom curate: {message}.", file=sys.stderr)
        print(f"  {posted} link(s) were already posted; the watermark is unchanged and "
              "the next run posts the rest of this exact batch (no new LLM call).",
              file=sys.stderr)
    else:
        print(f"loom curate: {message} (nothing was posted; watermark unchanged).",
              file=sys.stderr)
    counter = (f"digest_attempts={record['digest_attempts']} " if digest_step else "")
    print(f"  recorded as {counter}attempts={record['attempts']} last_error={code} "
          "(see 'loom status').", file=sys.stderr)
    if digest_step and record["digest_attempts"] >= MAX_ATTEMPTS:
        print(f"  the DIGEST step is now PARKED (digest_attempts >= {MAX_ATTEMPTS}): "
              "curation keeps running, but no digest will be generated for this "
              "project.", file=sys.stderr)
        print(f"  {_UNDIGEST_HINT}", file=sys.stderr)
    elif not digest_step and record["attempts"] >= MAX_ATTEMPTS:
        print(f"  this project is now PARKED (attempts >= {MAX_ATTEMPTS}): no further "
              "curation pass will run for it.", file=sys.stderr)
        print(f"  {_UNPARK_HINT}", file=sys.stderr)
    return 1


# How a human un-parks a project, in the ONE place both the failure path and
# `loom status` read it from.
_UNPARK_HINT = ("clear it with 'loom curate --reset [<project>]', or skip past the "
                "entries it cannot reach with 'loom curate --from <entry_id> "
                "[<project>]'.")
# The same, for the digest step's own counter. `--redigest` is the knob that also puts
# `digest_last_id` back: nothing else can, because that watermark only moves forward, and
# a state file that ran ahead of the server (a hand edit, a restored backup) would
# otherwise make a digest permanently unreachable with no supported way out.
_UNDIGEST_HINT = ("clear it with 'loom curate --reset [<project>]', or force the next "
                  "pass to rebuild the digest with 'loom curate --redigest "
                  "[<project>]'.")


def _finish(cfg: Config, state: dict, remote: str, record: dict, *,
            watermark: int, message: str, api: ApiClient | None = None,
            project_id: int | None = None, backlog: int = 0) -> int:
    """The ONE success tail of a pass: advance, clear, persist, report — then digest.

    Every successful return of `_curate_locked` goes through here — E16's per-project
    digest step is a threshold check on the state this function has just written, so it
    belongs at this single point rather than at each of the four places a pass can end
    well. The watermark is ADVANCE-ONLY (`max`), which is what makes a hand-edited or
    racing state file unable to move it backwards.

    The linking result is persisted and REPORTED BEFORE the digest step runs, and that
    ordering is deliberate: the links are already on the server and the watermark must
    not be re-earned by a later failure. A digest that then fails returns 1 from here
    with the curation message already printed — "the links landed, the digest did not"
    is exactly what happened, and the next pass (which has nothing left to curate) comes
    straight back through this function and retries only the digest.

    ``api``/``project_id`` are what the digest step needs; a caller that has neither
    (there is none today) simply finishes without one rather than guessing. ``backlog``
    is how many entries this pass could NOT reach — see :func:`_maybe_digest`, which
    refuses to summarise a project that is still half-curated.
    """
    record.update(watermark_id=max(record["watermark_id"], watermark),
                  attempts=0, last_error=None)
    record.pop("pending_batch", None)
    _save(cfg, state, remote, record)
    print(message)
    if api is None or project_id is None:
        return 0
    return _maybe_digest(cfg, api, state, remote, record, project_id, backlog=backlog)


def _link_bodies(verdicts: list[dict]) -> list[dict]:
    """The validated verdicts, flattened into the exact POST bodies they become.

    One body per link, so the intent log below stores what will be sent rather than
    something a replay has to re-derive (and could re-derive differently).
    """
    bodies = []
    for verdict in verdicts:
        if verdict["kind"] is None:
            continue
        rationale = (verdict["rationale"] or "")[:MAX_RATIONALE_LEN] or None
        for target in verdict["targets"]:
            if "item" in verdict:
                # Slice E: an item verdict becomes an ITEM link, never an entry link.
                bodies.append({"from": verdict["item"], "to": target,
                               "kind": verdict["kind"], "rationale": rationale})
            else:
                bodies.append({"from_entry": verdict["entry"], "to_entry": target,
                               "kind": verdict["kind"], "rationale": rationale})
    return bodies


def _pending_batch(record: dict) -> dict | None:
    """The intent log left by a pass that started posting, VALIDATED. Total.

    ``None`` when there is none. Raises :class:`CurationStateError` when there is one
    this module will not post — the same discipline as an unparseable state file, and
    for the same reason: silently dropping it would leave the half-posted link set in
    place with nothing to reconcile it against.
    """
    raw = record.get("pending_batch")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise CurationStateError("'pending_batch' is not an object")
    high_water = raw.get("high_water")
    bodies = raw.get("links")
    if isinstance(high_water, bool) or not isinstance(high_water, int) or high_water < 0:
        raise CurationStateError("'pending_batch.high_water' is not a watermark")
    if not isinstance(bodies, list):
        raise CurationStateError("'pending_batch.links' is not a list")
    clean: list[dict] = []
    for body in bodies:
        if not isinstance(body, dict):
            raise CurationStateError("'pending_batch.links' holds a non-object")
        kind, rationale = body.get("kind"), body.get("rationale")
        if kind not in (VERDICT_SUPERSEDES, VERDICT_DUPLICATES):
            raise CurationStateError("'pending_batch' holds an unknown link kind")
        if rationale is not None and not isinstance(rationale, str):
            raise CurationStateError("'pending_batch' holds a non-text rationale")
        rationale = (rationale or "")[:MAX_RATIONALE_LEN] or None
        if "from" in body or "to" in body:
            # An ITEM link (slice E), `<e>.<n>` on both ends.
            source, target = body.get("from"), body.get("to")
            if not all(isinstance(v, str) and _ITEM_ID_RE.fullmatch(v)
                       for v in (source, target)):
                raise CurationStateError(
                    "'pending_batch' names something that is not an item id")
            if int(source.split(".")[0]) <= int(target.split(".")[0]):
                # `from` is an item of the NEWER entry; the endpoint rejects the rest.
                raise CurationStateError("'pending_batch' holds a backwards link")
            clean.append({"from": source, "to": target, "kind": kind,
                          "rationale": rationale})
            continue
        source, target = body.get("from_entry"), body.get("to_entry")
        if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0
               for v in (source, target)):
            raise CurationStateError("'pending_batch' names something that is not an id")
        if source <= target:
            # `from_entry` is the NEWER entry; the endpoint rejects the reverse anyway.
            raise CurationStateError("'pending_batch' holds a backwards link")
        clean.append({"from_entry": source, "to_entry": target, "kind": kind,
                      "rationale": rationale})
    return {"high_water": high_water, "links": clean}


def _post_links(cfg: Config, api: ApiClient, state: dict, remote: str, record: dict,
                batch: dict, *, project_id: int, what: str, tail: str = "",
                backlog: int = 0) -> int:
    """POST every link of ``batch``, then advance — or fail with what really happened.

    EVERY link is attempted, not just the ones before the first failure: with the intent
    log in place a rerun replays this same batch, so landing more of it now only means
    more 409s later, and stopping early for no reason leaves a link set no single pass
    would produce. A server that has gone away is the one exception — every remaining
    POST would just burn a timeout, and the replay covers them.
    """
    posted, failures, created = 0, [], []
    remaining = list(batch["links"])
    for body in batch["links"]:
        item_link = "from" in body
        try:
            resp = api._request("POST", "/api/item-links" if item_link else "/api/links",
                                json=body)
        except ServerUnreachable as exc:
            failures.append(f"the server became unreachable ({exc})")
            break
        if resp.status_code == 201:
            posted += 1
            if item_link:
                # Printed with its id: the id is what `loom unlink` takes back.
                try:
                    link_id = resp.json().get("id")
                except (ValueError, AttributeError):
                    link_id = None
                created.append(f"  {body['from']} {body['kind']} {body['to']}"
                               + (f" (link {link_id})" if isinstance(link_id, int) else ""))
        elif item_link and resp.status_code == 404 and _error_code(resp) == "not_found":
            # The ROUTE is missing, not an item: a server from before slice E. The item
            # verdicts stay in the intent log and are replayed once it is deployed.
            failures.append("the server does not support item links yet; deploy it "
                            "before curating itemized entries")
        elif resp.status_code != 409:
            # 409 is "already linked" (or "already linked the other way") — the whole
            # point of posting 409-tolerantly, and what makes a replay idempotent.
            failures.append(f"HTTP {resp.status_code}")
        if resp.status_code in (201, 409):
            # Landed: it leaves the intent log at once. A replay posts only what is still
            # to post — an item link can be REVOKED between the two runs (`loom unlink`),
            # and re-posting it would silently undo that correction.
            remaining = [other for other in remaining if other is not body]
            if record.get("pending_batch") is not None:
                record["pending_batch"] = {"high_water": batch["high_water"],
                                           "links": remaining}
                _save(cfg, state, remote, record)
    if failures:
        for line in created:
            print(line)   # durable already, and what `loom unlink` would take back
        return _fail(cfg, state, remote, record, _ERR_POST_FAILED,
                     f"{len(failures)} of {len(batch['links'])} link POST(s) failed "
                     f"({failures[0]})", posted=posted)
    watermark = max(record["watermark_id"], batch["high_water"])
    return _finish(cfg, state, remote, record, watermark=batch["high_water"],
                   message=f"loom curate: {what}, {posted} new link(s); "
                           f"watermark → {watermark}{tail}."
                           + "".join(f"\n{line}" for line in created),
                   api=api, project_id=project_id, backlog=backlog)


# --------------------------------------------------------------------------- #
# Project resolution                                                           #
# --------------------------------------------------------------------------- #
def _resolve_project(api: ApiClient, project: str | None) -> dict | None:
    """The project dict (id + canonical_remote) to curate, or ``None`` + a message.

    The canonical remote — not the name and not the id — is what keys the state, so it
    has to come from the server's own registry rather than from what the user typed.
    """
    if project:
        projects = api.list_projects()
        exact = [p for p in projects if p.get("name") == project]
        matches = exact or [
            p for p in projects
            if project.lower() in (p.get("name") or "").lower()
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            print(f"No project matches '{project}'.", file=sys.stderr)
        else:
            print(f"'{project}' is ambiguous; several projects match:", file=sys.stderr)
            for match in matches:
                print(f"  - {match.get('name')}  (id {match.get('id')})",
                      file=sys.stderr)
        return None

    remote_url = _git_remote_url(os.getcwd())
    if not remote_url:
        print("This directory has no git remote 'origin'.", file=sys.stderr)
        print("Pass a project name: loom curate <project>", file=sys.stderr)
        return None
    try:
        canonical = normalize(remote_url)
    except ValueError:
        print(f"The remote '{remote_url}' has no recognizable shape.", file=sys.stderr)
        return None
    found = api.get_project_by_remote(canonical)
    if not found:
        print(f"This repo's project ({canonical}) is not registered.", file=sys.stderr)
        return None
    return found


# --------------------------------------------------------------------------- #
# Public entry point                                                           #
# --------------------------------------------------------------------------- #
def run_curate(cfg: Config, project: str | None = None, *,
               seed_from: int | None = None, reset: bool = False,
               redigest: bool = False) -> int:
    """One curation pass: candidates via FTS5, one batched verdict pass (I3),
    deterministic link POSTs (409-tolerant), then the digest threshold check.
    Returns a process exit code.

    ``seed_from`` moves the project's watermark FORWARD to that entry id (never
    backwards — the watermark is advance-only) and clears the attempt counters; it is
    the documented way out of `_ERR_WATERMARK_GAP` and of a parked project.
    ``reset`` clears the attempt counters alone. ``redigest`` additionally puts
    ``digest_last_id`` back to 0, which is the ONLY supported way to make a digest that
    has been claimed (a hand-edited state file, a restored backup, a digest whose row
    never landed) reachable again — that watermark otherwise only moves forward.

    IMPORTANT for anything that extends this pass (E16's digest step): the flock is
    held for the WHOLE pass, right here, and must not be re-acquired further in. `flock`
    is not re-entrant across file descriptors within one process — a second `open()` +
    `LOCK_EX|LOCK_NB` on the same path raises `BlockingIOError` (verified) — so an entry
    point that re-locks would read that as "another pass holds it" and silently exit 0.
    Extend `_finish`, or call `_curate_locked`; never wrap `run_curate`.
    """
    cfg.loom_home.mkdir(parents=True, exist_ok=True)
    lock_fd = open(cfg.curate_lock_path, "w")
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Its OWN lock, and NON-blocking: a second curate yields immediately
            # instead of queueing behind an LLM call, and a capture pass (which holds
            # `lock`, not this one) is never blocked by curation at all.
            print("loom curate: another curate pass holds the lock; nothing to do.")
            return 0
        # D16: every rationale and the digest are scanned with gitleaks before they
        # are posted. With no usable gitleaks that scan can only fail, so refuse HERE —
        # before the state file, the server or the LLM — rather than pay for a pass
        # whose every verdict would then fail closed and park the project.
        reason = processor.scanner_pause_reason(cfg)
        if reason is not None:
            print(f"loom curate: refusing to run: {reason}. Nothing was sent to the "
                  "model or posted, and no attempt was charged.", file=sys.stderr)
            return 1
        try:
            return _curate_locked(cfg, project, seed_from=seed_from, reset=reset,
                                  redigest=redigest)
        except ServerUnreachable as exc:
            # Only reachable while RESOLVING the project — from there on the pass knows
            # which record to charge and `_fail`s instead, so the attempt is visible in
            # `loom status`. Before that there is no project key to record it under.
            print(f"loom curate: could not reach the server: {exc}", file=sys.stderr)
            return 1
        except CurationStateError as exc:
            # The file is left EXACTLY as it is. Rewriting it would reset every
            # watermark to 0 and re-bill a full LLM pass over the whole logbook.
            print(f"loom curate: {cfg.curation_state_path} cannot be read as curation "
                  f"state ({exc}); refusing to run so the file is not overwritten.",
                  file=sys.stderr)
            return 1
        except _HttpFailure as exc:
            print(f"loom curate: {exc}.", file=sys.stderr)
            return 1
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        lock_fd.close()


def _curate_locked(cfg: Config, project: str | None, *, seed_from: int | None = None,
                   reset: bool = False, redigest: bool = False) -> int:
    # The state is read FIRST, before a single network call: a file that cannot be
    # parsed must stop the pass whatever the server is doing, and finding that out
    # only after resolving a project would make the refusal depend on connectivity.
    state = _load_state(cfg)

    api = ApiClient(cfg.server_url, cfg.token)
    found = _resolve_project(api, project)
    if found is None:
        return 1
    project_id = found.get("id")
    remote = found.get("canonical_remote")
    if not isinstance(project_id, int) or not isinstance(remote, str) or not remote:
        # A project registered by PATH has no canonical remote, so it has nothing to
        # key a per-project watermark by. Curation of those waits for the phase-3
        # path-registration story, and saying so is better than inventing a key.
        print("loom curate: this project has no canonical remote to key its "
              "curation state by; nothing to do.", file=sys.stderr)
        return 1

    record = _record(state, remote)
    if seed_from is not None or reset or redigest:
        _apply_operator_knobs(cfg, state, remote, record, seed_from=seed_from,
                              reset=reset, redigest=redigest)
    if record["attempts"] >= MAX_ATTEMPTS:
        # Bounded attempts, the P4 discipline the manifest asks for by name. Without it
        # a project stuck on a bad model, an oversized batch or a server that answers
        # 500 pays for a full LLM pass on EVERY run, forever.
        print(f"loom curate: {remote} is parked after {record['attempts']} failed "
              f"attempt(s) (last_error={record['last_error'] or 'unknown'}); no "
              "curation pass will run for it.", file=sys.stderr)
        print(f"  {_UNPARK_HINT}", file=sys.stderr)
        return 1

    try:
        return _curate_project(cfg, api, state, remote, record, project_id)
    except _HttpFailure as exc:
        # From here on the pass knows which project record to charge, so an HTTP-shaped
        # fault is COUNTED rather than escaping to `run_curate` invisibly.
        return _fail(cfg, state, remote, record, _ERR_SERVER, str(exc))
    except ServerUnreachable as exc:
        return _fail(cfg, state, remote, record, _ERR_SERVER,
                     f"the server became unreachable ({exc})")


def _apply_operator_knobs(cfg: Config, state: dict, remote: str, record: dict, *,
                          seed_from: int | None, reset: bool,
                          redigest: bool = False) -> None:
    """``--from`` / ``--reset`` / ``--redigest``, under the lock, persisted immediately.

    Persisted BEFORE the pass runs so the un-parking survives whatever the pass then
    does; a human who has just fixed the cause should not have to re-run the flag
    because the following pass failed for an unrelated reason.

    ``--from`` only ever moves the watermark FORWARD. Moving it back would break the
    advance-only invariant of `t-curation-watermark` and re-bill an LLM pass over
    entries that were already curated; the problem this flag exists for (a watermark
    the listing cannot page down to) is only ever solved by moving forward anyway.

    ``--redigest`` is the ONE exception, and it is deliberately a separate flag rather
    than a behaviour of the other two: it puts ``digest_last_id`` back to 0, which is
    the only way to recover a digest watermark that ran ahead of the server. It cannot
    lose data — the digest input is "the current entries", not "the new ones" — and at
    worst it re-bills one LLM pass whose result 409s if it comes out identical.
    """
    before = record["watermark_id"]
    if seed_from is not None:
        record["watermark_id"] = max(before, _non_negative_int(seed_from))
    record.update(attempts=0, last_error=None, digest_attempts=0)
    if redigest:
        record["digest_last_id"] = 0
    _save(cfg, state, remote, record)
    if seed_from is not None and record["watermark_id"] != before:
        print(f"loom curate: watermark for {remote} seeded {before} → "
              f"{record['watermark_id']}.")
    elif seed_from is not None:
        print(f"loom curate: watermark for {remote} already at "
              f"{record['watermark_id']}; left alone (it never moves backwards).")
    if redigest:
        print(f"loom curate: digest watermark for {remote} cleared; the next pass "
              "rebuilds the digest.")
    print(f"loom curate: attempt counters for {remote} cleared.")


def _curate_project(cfg: Config, api: ApiClient, state: dict, remote: str,
                    record: dict, project_id: int) -> int:
    watermark = record["watermark_id"]

    pending = _pending_batch(record)
    if pending is not None:
        # A previous pass validated these verdicts and started posting them. Replaying
        # the INTENT — rather than asking the model again — is what makes an interrupted
        # pass idempotent: the links that did land answer 409, the rest are created, and
        # the final state is the one that single pass meant to produce. Asking again
        # instead would ask about a logbook the half-posted links have already changed
        # (their targets are no longer `current`, so they leave the pools), and no
        # single pass could produce the answer that comes back.
        print(f"loom curate: replaying {len(pending['links'])} link(s) left by an "
              f"interrupted pass for {remote} (no LLM call).")
        return _post_links(cfg, api, state, remote, record, pending,
                           project_id=project_id,
                           what="the interrupted batch was replayed")

    try:
        listed, has_more = _list_entries(api, project_id)
    except _HttpFailure as exc:
        # Charged to this project's record rather than escaping to `run_curate`: from
        # here the pass knows which record to bill.
        return _fail(cfg, state, remote, record, _ERR_SERVER, str(exc))

    new_entries = sorted((e for e in listed if e["id"] > watermark),
                         key=lambda e: e["id"])
    if not new_entries:
        return _finish(cfg, state, remote, record, watermark=watermark,
                       message=f"loom curate: nothing new to curate for {remote} "
                               f"(watermark {watermark}).",
                       api=api, project_id=project_id)
    if has_more and min(e["id"] for e in listed) > watermark:
        # The newest page is entirely above the watermark AND there is more below it.
        # The listing is newest-first and clamps at `_MAX_ENTRIES_PER_PASS`, so entries
        # between the watermark and this page cannot be reached — and advancing to the
        # top of the page would step over them permanently. The pass stops instead.
        #
        # Conservative by necessity, and knowingly so: the frozen listing has no way to
        # page BELOW the newest 500, so "exactly 500 uncurated entries with the 501st
        # already curated" is indistinguishable from a real gap and is refused too. That
        # is why `--from` exists and why this message names it — the alternative reading
        # of the same evidence would step over real entries. A `before_id` bound on the
        # listing would make the test exact; it is recorded as a follow-up.
        return _fail(cfg, state, remote, record, _ERR_WATERMARK_GAP,
                     f"{_MAX_ENTRIES_PER_PASS} or more entries are newer than the "
                     "curation watermark and the listing cannot page below them; seed "
                     f"the watermark with 'loom curate --from <entry_id>' (e.g. "
                     f"{min(e['id'] for e in listed) - 1}) to curate from there on")

    # Digests are not curated (they summarise the entries they were built from), and ONE
    # pass answers for at most `_MAX_ENTRIES_PER_BATCH` of them: the watermark advances
    # to the top of the BATCH, so a backlog drains in bounded steps instead of building
    # a prompt that can only time out. Both lists are id-ascending, so the batch is
    # always the OLDEST slice and the watermark stays monotone.
    batch = [e for e in new_entries if not is_digest(e)][:_MAX_ENTRIES_PER_BATCH]
    high_water = max(e["id"] for e in batch) if batch else max(
        e["id"] for e in new_entries)
    remaining = sum(1 for e in new_entries if e["id"] > high_water)
    more = (f" ({remaining} more above it — run 'loom curate' again)"
            if remaining else "")

    k = cfg.curation_candidate_k
    # Slice E: one map of every item this pass reads — the batch's first, then each
    # pool's candidates as `_candidates_for` meets them.
    items_of: dict[int, list[dict]] = {}
    _fetch_items(api, project_id, [entry["id"] for entry in batch], items_of)
    items = [(entry, _candidates_for(api, project_id, entry, k, items_of))
             for entry in batch]

    if not any(candidates for _entry, candidates in items):
        # Nothing could be linked even in principle, so there is no question worth
        # paying an LLM to answer. The watermark still advances: these entries HAVE
        # been curated — the answer was "keep", derived rather than asked for.
        return _finish(
            cfg, state, remote, record, watermark=high_water,
            message=f"loom curate: {len(batch)} new entrie(s), no candidates to compare "
                    f"them against; watermark → {max(watermark, high_water)}{more}.",
            api=api, project_id=project_id, backlog=remaining)

    payload = build_payload(items, items_of)
    try:
        cp = _run_curator(cfg, payload)
    except subprocess.TimeoutExpired:
        return _fail(cfg, state, remote, record, _ERR_LLM_FAILED,
                     "the curation pass timed out")
    except OSError as exc:
        return _fail(cfg, state, remote, record, _ERR_LLM_FAILED,
                     f"the curation pass could not be started ({type(exc).__name__})")
    if cp.returncode != 0:
        # stderr is NOT echoed: it can quote the payload back, and the payload is
        # entry content (I5/I6).
        return _fail(cfg, state, remote, record, _ERR_LLM_FAILED,
                     f"the curation pass exited {cp.returncode}")

    pools = {entry["id"]: {c["id"] for c in candidates} for entry, candidates in items
             if not items_of.get(entry["id"])}
    item_pools = {
        _item_id(item): {_item_id(offered) for c in candidates
                         for offered in _current_items(items_of.get(c["id"]))}
        for entry, candidates in items for item in items_of.get(entry["id"]) or ()
    }
    try:
        verdicts = parse_verdicts(cp.stdout, pools, item_pools)
        _scan_rationales(cfg, verdicts)
    except VerdictError as exc:
        return _fail(cfg, state, remote, record, exc.code,
                     f"the curation verdicts were rejected ({exc.code})")

    # Everything below this line has been validated. Only now does anything get POSTed.
    pending = {"high_water": high_water, "links": _link_bodies(verdicts)}
    if not pending["links"]:
        return _finish(
            cfg, state, remote, record, watermark=high_water,
            message=f"loom curate: {len(batch)} entrie(s) curated, 0 new link(s); "
                    f"watermark → {max(watermark, high_water)}{more}.",
            api=api, project_id=project_id, backlog=remaining)

    # The INTENT LOG, written before the first POST. A partial batch is durable — there
    # is no `DELETE /api/links` — and without this the rerun could not reproduce the same
    # verdicts: the links that DID land flip their targets out of `current`, so those
    # targets leave the pools `_candidates_for` builds and the model answers a different
    # question. Reproduced: `6 supersedes 4,5` half-posted came back as `6 duplicates 5`
    # on the rerun, leaving 4 superseded AND 5 duplicate — a link set no single pass
    # would produce, with a clean exit 0 and attempts=0 to show for it. `_finish` is the
    # only thing that clears it, so a SIGKILL inside the POST loop is covered too.
    record["pending_batch"] = pending
    _save(cfg, state, remote, record)
    return _post_links(cfg, api, state, remote, record, pending,
                       project_id=project_id,
                       what=f"{len(batch)} entrie(s) curated", tail=more,
                       backlog=remaining)


def latest_digest(entries: list[dict]) -> dict | None:
    """The newest entry whose OKF frontmatter is ``type: digest`` (client-side
    detection, t-digest-entry). Kept importable — Block M's RM.1 (ON HOLD) would
    consume this seam; never scrape CLI output for it.

    "Newest" is decided by ``created_at`` and then by ``id``, in that order, because the
    caller may hand over a page the server ranked by relevance (``--grep``) rather than
    by date. Both keys are compared as their own types with a total fallback — an entry
    dict can come from a hand-edited fixture or from a server older than Block T, and a
    ``None`` timestamp must not raise where a missing digest is the honest answer.

    Total in the other direction too: an empty list, a list with no digest, and a list of
    things that are not entries all return ``None``.
    """
    digests = [entry for entry in entries
               if isinstance(entry, dict) and is_digest(entry)]
    if not digests:
        return None
    return max(digests, key=lambda entry: (
        str(entry.get("created_at") or ""),
        entry["id"] if isinstance(entry.get("id"), int) else 0,
    ))


# --------------------------------------------------------------------------- #
# The digest pass (T.F6 / t-digest-entry)                                      #
# --------------------------------------------------------------------------- #
def digest_prompt_path(cfg: Config) -> "os.PathLike[str] | None":
    """Where :data:`DIGEST_PROMPT_FILENAME` is, or ``None`` if it is nowhere.

    The INSTALLED copy first (``<LOOM_HOME>/prompts/``, written by install.sh step 3
    alongside the summarizer prompts), then this checkout's ``prompts/`` — the same
    two-place resolution `cli._repo_root` already uses for ``contract/VERSION``, and the
    reason a developer running from a git clone gets a digest at all.

    Neither location is configurable: see :data:`DIGEST_PROMPT_FILENAME`.
    """
    installed = cfg.loom_home / "prompts" / DIGEST_PROMPT_FILENAME
    if installed.is_file():
        return installed
    try:
        from pathlib import Path

        shipped = Path(__file__).resolve().parents[2] / "prompts" / DIGEST_PROMPT_FILENAME
    except (IndexError, OSError):  # pragma: no cover — a checkout that cannot resolve
        return None
    return shipped if shipped.is_file() else None


def _read_digest_prompt(cfg: Config) -> str | None:
    path = digest_prompt_path(cfg)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return text if text.strip() else None


def build_digest_payload(entries: list[dict]) -> str:
    """The digest stdin: one ``### ENTRY <id>`` block per CURRENT entry, oldest first.

    Entry TEXT only (I3) — the same rule, and the same marker neutralization, as
    :func:`build_payload`: a session summary can contain a line that opens with the
    block marker, and left alone it forges a block for an entry nobody put in this
    payload. Nothing else about the machine (no path, no URL, no token, no filename)
    ever enters this string.

    Ids travel with the blocks because the prompt asks the model to cite them as
    evidence anchors; they are the only metadata a digest is allowed to carry over.
    """
    blocks = []
    for entry in entries:
        body = _neutralize_markers((entry.get("content_md") or "").strip())
        blocks.append(f"{ENTRY_MARKER}{entry['id']}\n{body}")
    return "\n\n".join(blocks) + "\n"


def _digest_input(entries: list[dict]) -> tuple[list[dict], int]:
    """The current entries that fit in one payload, oldest first, + how many were cut.

    NEWEST-first accumulation under :data:`_MAX_DIGEST_PAYLOAD_CHARS`, then reversed:
    when a project has more history than one prompt can hold, the digest is built from
    the most recent state rather than from a prefix of ancient decisions. At least one
    entry is always kept, so a single oversized entry produces a digest instead of an
    empty payload.
    """
    kept: list[dict] = []
    total = 0
    for entry in sorted(entries, key=lambda e: e["id"], reverse=True):
        cost = len(entry.get("content_md") or "") + 32  # + the block marker line
        if kept and total + cost > _MAX_DIGEST_PAYLOAD_CHARS:
            break
        kept.append(entry)
        total += cost
    kept.reverse()
    return kept, len(entries) - len(kept)


def _without_replaced_items(entry: dict, items: list[dict] | None) -> dict:
    """``entry`` with the lines of every item that is no longer current removed.

    The items are located the way `loom log` locates them (`processor.locate_items`:
    from the Decisions section on, as whole lines, in order), so a sentence in
    ``## Context`` that quotes an item, or a line it merely ends, is never touched. An
    entry with nothing replaced — or whose items cannot all be located, which every
    reader then treats as un-itemized — is returned unchanged.
    """
    items = list(items or ())
    if not any(item.get("status") != "current" for item in items):
        return entry
    content = entry.get("content_md") or ""
    offsets = processor.locate_items(content, [item["text_md"] for item in items])
    if offsets is None:
        return entry
    out: list[str] = []
    cursor = 0
    for item, at in zip(items, offsets):
        if item.get("status") == "current":
            continue
        end = at + len(item["text_md"])
        if content[end:end + 1] == "\n":
            end += 1
        out.append(content[cursor:at])
        cursor = end
    out.append(content[cursor:])
    return {**entry, "content_md": "".join(out)}


def _digest_body(stdout: str) -> str | None:
    """The model's digest, stripped and BOUNDED. ``None`` when it said nothing.

    The bound is applied here, before the text is wrapped, scanned or hashed, so that
    every downstream step sees exactly the bytes that will be stored (the scan in
    particular: what is not stored cannot leak, and what IS stored is what gets scanned).
    """
    body = (stdout or "").strip()
    if not body:
        return None
    if len(body) > _MAX_DIGEST_OUTPUT_CHARS:
        body = (body[:_MAX_DIGEST_OUTPUT_CHARS].rstrip()
                + _DIGEST_TRUNCATION_NOTE.format(limit=_MAX_DIGEST_OUTPUT_CHARS))
    return body


def _digest_anchor(remote: str, entry_id: int) -> str:
    """The ``covered_from``/``covered_to`` value of a digest — and its hash material.

    ``digest:<remote>:<id>`` rather than a bare id, and the remote is in it on purpose:
    ``entries.content_hash`` is UNIQUE over the WHOLE table, not per project, so two
    projects whose digests happened to come out byte-identical (two young projects, one
    template-following model) would collide — the second POST would answer 409, the
    second project would advance its watermark, and it would have no digest row at all
    and no error to show for it.
    """
    return f"digest:{remote}:{entry_id}"


def _maybe_digest(cfg: Config, api: ApiClient, state: dict, remote: str, record: dict,
                  project_id: int, *, backlog: int = 0) -> int:
    """The threshold-gated digest step (T.F6), AFTER linking, in the same pass.

    Deliberately re-reads the project's entries instead of reusing the listing the
    curation pass already fetched: the links this pass just posted are precisely what
    moves entries out of ``current``, and "the digest input is CURRENT entries only" is
    a statement about the world AFTER the batch landed, not before it.

    TWO things bound WHICH entries it may summarise, and both exist because a digest
    claims coverage of everything below its watermark:

    - a digest is only ever built from entries **at or below the curation watermark**.
      E15 caps a batch at :data:`_MAX_ENTRIES_PER_BATCH`, so on a backlog the listing
      holds hundreds of entries whose supersession verdicts have not been asked for yet.
      Summarising those and then advancing `digest_last_id` past them produced a digest
      that asserted a reversed decision as current — and `fresh` could never re-fire,
      because every entry that would have triggered it was already below the watermark.
      The stale digest was then permanent, and `loom log` printed it FIRST, above the
      very entry the next pass collapsed to "superseded by #150".
    - while a backlog remains the step does not run at all. The filter above already
      makes the digest correct; this makes it cheap, because the digest built from the
      first 100 of 250 entries would be superseded by the next pass anyway.

    Everything else is fail-closed and ordered the same way the capture path is: build →
    scan → POST → advance. `digest_last_id` moves only on 201/409, so every failure mode
    (LLM, scanner, server) leaves the project exactly where it was — and charges
    `digest_attempts`, which is what stops a permanently failing digest from re-billing
    an LLM pass on every run.
    """
    if digest_attempts(record) >= MAX_ATTEMPTS:
        # Bounded attempts for the SECOND LLM call, the wave-j B4 discipline applied
        # where it now belongs. Exit 0, not 1: the curation pass succeeded, and a step
        # that is deliberately parked is not a failure of the run that skipped it.
        print(f"loom curate: the digest step for {remote} is parked after "
              f"{digest_attempts(record)} failed attempt(s) "
              f"(last_error={record['last_error'] or 'unknown'}); no digest pass will "
              "run for it.", file=sys.stderr)
        print(f"  {_UNDIGEST_HINT}", file=sys.stderr)
        return 0
    if backlog:
        print(f"loom curate: {backlog} entrie(s) are still uncurated for {remote}; the "
              "digest waits until the backlog is drained (run 'loom curate' again).")
        return 0

    threshold = cfg.curation_digest_threshold
    try:
        entries, has_more = _list_entries(api, project_id)
    except (_HttpFailure, ServerUnreachable) as exc:
        # Caught HERE rather than in `_curate_locked`: from there this reads as a pass
        # that posted nothing, and the pass has already printed its success line.
        return _fail(cfg, state, remote, record, _ERR_SERVER,
                     f"the project's entries could not be re-read ({exc})",
                     digest_step=True)
    # `record["watermark_id"]` is what `_finish` has just persisted — the top of the
    # region whose supersession verdicts have actually been asked for.
    current = [e for e in entries
               if entry_status(e) == "current" and not is_digest(e)
               and e["id"] <= record["watermark_id"]]
    fresh = [e for e in current if e["id"] > record["digest_last_id"]]
    if len(fresh) < threshold:
        # The whole cost gate: no LLM call, no state change, nothing to undo.
        print(f"loom curate: {len(fresh)}/{threshold} new current entrie(s) since the "
              f"last digest for {remote}; no digest this pass.")
        return 0

    high_water = max(e["id"] for e in current)
    covered_to = _digest_anchor(remote, high_water)
    if any(is_digest(e) and e.get("covered_to") == covered_to for e in entries):
        # Another machine already digested exactly this range. `digest_last_id` is
        # per-LOOM_HOME (the frozen state seam), so on an N-developer team every machine
        # crosses the threshold at the same entry and each one would pay for its own LLM
        # pass and post its own near-identical row. Checking the listing we have already
        # fetched costs nothing and collapses that to the first machine there.
        record["digest_last_id"] = max(record["digest_last_id"], high_water)
        record["digest_attempts"] = 0
        _save(cfg, state, remote, record)
        print(f"loom curate: a digest covering {remote} up to entry {high_water} is "
              f"already on the server; digest watermark → {record['digest_last_id']} "
              "(no LLM call).")
        return 0

    prompt = _read_digest_prompt(cfg)
    if prompt is None:
        return _fail(cfg, state, remote, record, _ERR_DIGEST_PROMPT,
                     f"the digest prompt ({DIGEST_PROMPT_FILENAME}) could not be read "
                     f"from {cfg.loom_home / 'prompts'} or from this checkout",
                     digest_step=True)

    # Slice E (E6): an item another item replaced is not part of the current state, so
    # its text leaves its entry before the entry reaches the digest; the entry itself
    # stays (its other items are current).
    items_of: dict[int, list[dict]] = {}
    try:
        _fetch_items(api, project_id, [e["id"] for e in current], items_of)
    except (_HttpFailure, ServerUnreachable) as exc:
        return _fail(cfg, state, remote, record, _ERR_SERVER,
                     f"the entries' items could not be read ({exc})", digest_step=True)
    current = [_without_replaced_items(e, items_of.get(e["id"])) for e in current]

    covered, dropped = _digest_input(current)
    if dropped:
        print(f"  the digest covers the {len(covered)} most recent current entrie(s); "
              f"{dropped} older one(s) did not fit the payload budget.")
    if has_more:
        # The listing is ONE page of the newest `_MAX_ENTRIES_PER_PASS` rows, so on a
        # project bigger than that there are older current entries the payload budget was
        # never even consulted about. Saying so is the difference between "the digest is
        # a summary of this project" and "of as much of it as one page holds".
        print(f"  {remote} has more than {_MAX_ENTRIES_PER_PASS} entries; the digest is "
              "built from the newest page only.")

    try:
        cp = _run_llm(cfg, prompt, build_digest_payload(covered))
    except subprocess.TimeoutExpired:
        return _fail(cfg, state, remote, record, _ERR_DIGEST_FAILED,
                     "the digest pass timed out", digest_step=True)
    except OSError as exc:
        return _fail(cfg, state, remote, record, _ERR_DIGEST_FAILED,
                     f"the digest pass could not be started ({type(exc).__name__})",
                     digest_step=True)
    if cp.returncode != 0:
        # stderr is NOT echoed: it can quote the payload back, and the payload is entry
        # content (I5/I6).
        return _fail(cfg, state, remote, record, _ERR_DIGEST_FAILED,
                     f"the digest pass exited {cp.returncode}", digest_step=True)

    body = _digest_body(cp.stdout)
    if body is None:
        return _fail(cfg, state, remote, record, _ERR_DIGEST_EMPTY,
                     "the digest pass produced no text", digest_step=True)

    # D7: the digest travels as a self-contained OKF concept whose frontmatter the
    # SCRIPT builds out of data it already knows — the model is never asked for YAML and
    # not one byte of its output reaches a metadata field.
    low_water = min(e["id"] for e in covered)
    covered_from = _digest_anchor(remote, low_water)
    covered_to = _digest_anchor(remote, high_water)
    # The "session" a digest comes from is the entry range it consolidates — there is no
    # transcript behind it. Derived once, because the frontmatter and the POST body must
    # name the same one.
    session_id = f"digest-{low_water}-{high_water}"
    content_md = okf.wrap_digest(
        body,
        canonical_remote=remote,
        session_id=session_id,
        covered_from=covered_from,
        covered_to=covered_to,
        model=cfg.model,
        prompt_version=DIGEST_PROMPT_VERSION,
    )

    # I4, fail-closed, over the FINAL bytes that would leave the machine (frontmatter
    # included) — the same rule, and the same reason, as processor step 8. The digest is
    # written by a model out of entry text, so it is the one place a credential that was
    # already published inside an entry can be copied into a brand-new row.
    try:
        findings = scan_engine.scan(content_md, cfg)
    except Exception as exc:  # noqa: BLE001 — no verdict == a hit (I4).
        return _fail(cfg, state, remote, record, _ERR_SCANNER_FAILED,
                     f"the digest could not be scanned ({type(exc).__name__}); nothing "
                     "was posted (a scanner failure counts as a hit)", digest_step=True)
    if findings:
        # The finding itself is NOT printed: a redacted secret is still a channel out of
        # the fail-closed path, and this text is persisted by `_fail` and reprinted by
        # `loom status`.
        return _fail(cfg, state, remote, record, _ERR_SECRET_IN_DIGEST,
                     f"the generated digest tripped the secret scanner "
                     f"({len(findings)} finding(s)); it was NOT posted",
                     digest_step=True)

    payload = {
        "project_remote": remote,
        "session_id": session_id,
        "covered_from": covered_from,
        "covered_to": covered_to,
        "content_md": content_md,
        "content_hash": processor.compute_content_hash(
            covered_from, covered_to, DIGEST_PROMPT_VERSION, cfg.model, body),
        "model": cfg.model,
        "prompt_version": DIGEST_PROMPT_VERSION,
        # No `item_types`: a digest is not a session's decisions, and typing it would
        # make it answer `loom log --type decision` as though it were one.
    }
    try:
        resp = api.post_entry(payload)
    except ServerUnreachable as exc:
        return _fail(cfg, state, remote, record, _ERR_DIGEST_POST_FAILED,
                     f"the digest could not be posted ({exc})", digest_step=True)
    if resp.status_code not in (201, 409):
        return _fail(cfg, state, remote, record, _ERR_DIGEST_POST_FAILED,
                     f"posting the digest returned HTTP {resp.status_code}",
                     digest_step=True)

    # 409 counts: the row is on the server (this pass produced a digest byte-identical to
    # one already stored), so the watermark has been earned exactly as much as by a 201.
    record["digest_last_id"] = max(record["digest_last_id"], high_water)
    record["digest_attempts"] = 0
    _save(cfg, state, remote, record)
    what = "posted" if resp.status_code == 201 else "already on the server"
    print(f"loom curate: digest {what} for {remote} over {len(covered)} current "
          f"entrie(s); digest watermark → {record['digest_last_id']}.")
    return 0
