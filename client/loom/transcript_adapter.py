"""Parsing of the Claude Code JSONL transcript (contract/SCHEMA.md, DESIGN.md §9).

Every dependency on the internal ``.jsonl`` format lives here, in a single module
with shape validation (DESIGN.md §9). If the shape does not fit → ``AdapterError``
(fail-loud, I4/P5): a "half-read" delta is never transmitted silently.

**Allowlist** semantics (corrected by T0/D-4): ONLY the lines whose ``type`` is in
{"user", "assistant"} and that carry a valid ``message`` are processed. Any other
``type`` (attachment, system, mode, file-history-snapshot, ai-title,
queue-operation, unknown future types…) is skipped silently and counted in
``Delta.skipped`` — in a real session most lines are legitimate metadata.

**Torn lines** (phase 2, P2.F1). A session that dies mid-write (SIGKILL, WSL
shutdown, a full disk) leaves half a line on disk. Such a *torn* line — one that
fails JSON parse **or** strict UTF-8 decode — is not a schema violation, it is a
crash artifact, and failing forever on it strands every later line of a resumed
session. Two rules, and they are asymmetric on purpose:

- **interior** (at least one complete line follows it): it can never become valid,
  because Claude Code only ever appends whole lines. Skip it, count it in
  ``Delta.torn``, keep reading.
- **trailing** (nothing complete after it): it is ambiguous — it may be a writer
  caught mid-append. The delta ends BEFORE it and ``new_last_line`` does not
  advance past it, so the next pass sees it again, healed or not. The length of
  that trailing run is reported in ``Delta.held``: if EVERY line of a transcript
  is torn there is no interior tear at all, ``torn`` is 0 and ``held`` is the
  only sign that the whole capture was held back.

A line that parses and decodes but lacks ``uuid``/``message`` is real corruption,
not a tear, and still raises ``AdapterError``.

Security (I5/I6): a torn line can hold half a credential. Its bytes are never
decoded leniently, never rendered into a ``Msg``, and never interpolated into an
exception, a log line or a diagnostic — only its line number ever escapes.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Tool text truncation (DESIGN.md §4.2 / PLAN.md §T3).
TOOL_TRUNCATE = 500
TRUNCATE_SUFFIX = "[…]"

# The only line `type`s that get extracted; everything else is skippable noise (D-4).
CONVERSATIONAL = frozenset({"user", "assistant"})

# Cap on how much of a line's ``type`` may appear in a diagnostic. The value comes
# from the transcript, so it is untrusted: it is length-bounded and stripped of
# separators before it can reach a marker field or a log column.
_TYPE_IN_DIAGNOSTIC = 40


class AdapterError(Exception):
    """The JSONL schema does not fit (fail-loud, I4/P5).

    Raised ONLY if a user/assistant line that parses and decodes lacks ``uuid``
    or ``message``. Metadata lines are never an error, and a torn line (bad JSON
    or bad UTF-8) is a crash artifact handled by the tolerance rules, not an error.

    Its message is safe to persist on a marker and to log: it carries line numbers
    and a sanitized ``type``, never transcript content.
    """


@dataclass
class Msg:
    """One extracted conversational turn.

    - ``role``: "user" | "assistant" (from ``message.role``).
    - ``text``: rendered content. Tool blocks are emitted inline as
      ``[tool:<name>]: …`` / ``[tool:result]: …`` lines.
    - ``uuid``: the line's top-level ``uuid`` (pointer anchor, D3).
    - ``at``: the line's ``timestamp`` (SCHEMA.md §3) as an aware UTC datetime, or
      ``None`` when it is absent or not an ISO-8601 instant with a zone. Read only by the
      capture cutoff (D9 amendment of 2026-09-30), which keeps what it cannot date.
    """

    role: str
    text: str
    uuid: str
    at: datetime | None = None


@dataclass
class Delta:
    """Result of reading the new lines of a transcript.

    - ``messages``: conversational turns, in file order.
    - ``new_last_line``: total number of lines in the file (robust pointer
      anchor; it advances even if the delta had no conversational content).
    - ``new_last_uuid``: uuid of the last conversational message, or ``None``
      if the delta contained none.
    - ``skipped``: number of metadata lines skipped in the delta.
    - ``torn``: number of *interior* torn lines skipped in the delta (P2.F1).
      A trailing torn line is held, not skipped, so it is not counted here.
    - ``held``: number of physical lines in the *trailing held run* — the lines
      from ``new_last_line`` to EOF that this delta deliberately did not consume
      because they are torn. 0 whenever the file ends on a complete line. It is
      the counterpart of ``torn``: with every line of a transcript torn, nothing
      is interior, ``torn`` is 0 and only ``held`` shows the capture was lost.
    - ``bytes_read``: total bytes of every PHYSICAL line this read iterated over —
      metadata, blank, torn and held lines included. ``read_delta`` always walks the
      file from index 0 to EOF (the lines below ``last_line`` are ``continue``d, never
      seeked past), so this is the file's size **as the read actually saw it**, and it
      is what lets a caller prove that a stat taken around the read describes the very
      bytes the read consumed. Deliberately independent of ``last_line``.
    """

    messages: list[Msg]
    new_last_line: int
    new_last_uuid: str | None
    skipped: int
    torn: int = 0
    held: int = 0
    bytes_read: int = 0


def _truncate(text: str) -> str:
    """Truncates to ``TOOL_TRUNCATE`` chars, adding the suffix if it overflows."""
    if len(text) > TOOL_TRUNCATE:
        return text[:TOOL_TRUNCATE] + TRUNCATE_SUFFIX
    return text


def _render_content(content: object) -> str:
    """Renders ``message.content`` (SCHEMA.md §4).

    ``content`` is either a string (user text) or a list of blocks (assistant,
    or user with a tool_result). Returns the parts joined by ``\\n``; tool
    markers go on their own ``[tool:…]:`` lines.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        # Unexpected content shape inside a valid message: no text.
        return ""

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text", "")
            if text:
                parts.append(text)
        elif btype == "thinking":
            # v1: reasoning is ignored (SCHEMA.md §4.3).
            continue
        elif btype == "tool_use":
            name = block.get("name") or "tool"
            raw_input = block.get("input", {})
            summary = raw_input if isinstance(raw_input, str) else json.dumps(
                raw_input, ensure_ascii=False, sort_keys=True
            )
            parts.append(f"[tool:{name}]: {_truncate(summary)}")
        elif btype == "tool_result":
            result = block.get("content")
            result = result if isinstance(result, str) else json.dumps(
                result, ensure_ascii=False
            )
            parts.append(f"[tool:result]: {_truncate(result)}")
    return "\n".join(parts)


def _parse_timestamp(value: object) -> datetime | None:
    """``timestamp`` → aware UTC datetime; ``None`` for anything that is not an ISO-8601
    string carrying a zone (a naive instant cannot be compared with the enable time)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)

class _TornLine(Exception):
    """Internal signal: this physical line is torn (never carries its bytes)."""


def _parse_line(raw: bytes) -> object | None:
    """Classifies ONE physical line, read as bytes.

    Returns the parsed object, or ``None`` for a blank line. Raises ``_TornLine``
    if the line is torn (bad UTF-8 or bad JSON). The bytes are decoded **strictly**
    on purpose: ``errors="replace"`` would launder a complete JSON line holding an
    invalid byte into a perfectly valid-looking message, and the mangled content —
    possibly half a secret — would flow on to the summarizer (I5).
    """
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        # The UnicodeDecodeError object carries the offending bytes, so it is
        # swallowed here and never chained: nothing derived from the line's content
        # can propagate to a caller, a log or a diagnostic.
        raise _TornLine from None
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        raise _TornLine from None


def is_torn_line(raw: bytes) -> bool:
    """``True`` if a physical line is torn (P2.F1): bad UTF-8 or bad JSON.

    Shared with ``sweep._line_count`` so the sweep's line units and the pointer's
    are the same by construction, torn fixtures included (P2.F1 rule 5). Blank
    lines are not torn.
    """
    try:
        _parse_line(raw)
    except _TornLine:
        return True
    return False


def _safe_type(obj: object) -> str:
    """The line's ``type`` for a diagnostic: bounded and free of log separators."""
    value = obj.get("type") if isinstance(obj, dict) else None
    if not isinstance(value, str):
        return "?"
    cleaned = "".join(c for c in value if c.isprintable())
    return cleaned[:_TYPE_IN_DIAGNOSTIC] or "?"


def read_delta(transcript_path: str | Path, last_line: int) -> Delta:
    """Reads the lines whose (1-based) line number is > ``last_line``.

    That is, it processes the line with 0-based index ``idx`` only if
    ``idx >= last_line``. ``new_last_line`` is the number of lines the delta
    consumed up to — the total in the file, or the index of the first line of a
    trailing torn run (P2.F1 rule 2) — so re-reading with
    ``last_line == new_last_line`` yields an empty delta (idempotent pointer, D3/I6).

    Raises ``AdapterError`` if a user/assistant line that parses and decodes lacks
    ``uuid`` or ``message``. Torn lines never raise (see the module docstring).
    """
    path = Path(transcript_path)
    messages: list[Msg] = []
    new_last_uuid: str | None = None
    skipped = 0
    torn = 0
    total = 0
    # A run of torn lines is only interior once something complete follows it, so it
    # is counted provisionally: ``held_at`` is where the delta would end if the file
    # stopped here, and ``held`` is how many tears that run holds.
    held_at: int | None = None
    held = 0
    bytes_read = 0

    with path.open("rb") as fh:
        for idx, raw in enumerate(fh):
            total = idx + 1
            bytes_read += len(raw)
            if idx < last_line:
                continue
            try:
                obj = _parse_line(raw)
            except _TornLine:
                if held_at is None:
                    held_at = idx
                held += 1
                continue

            # A complete line closes the run before it: those tears are interior.
            torn += held
            held_at, held = None, 0
            if obj is None:
                continue

            if not isinstance(obj, dict) or obj.get("type") not in CONVERSATIONAL:
                skipped += 1
                continue

            if "uuid" not in obj or "message" not in obj:
                raise AdapterError(
                    f"line {idx + 1} ({_safe_type(obj)}) has no 'uuid' or 'message'"
                )

            message = obj["message"]
            role = message.get("role") if isinstance(message, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            messages.append(
                Msg(role=role or obj["type"], text=_render_content(content), uuid=obj["uuid"],
                    at=_parse_timestamp(obj.get("timestamp")))
            )
            new_last_uuid = obj["uuid"]

    return Delta(
        messages=messages,
        new_last_line=total if held_at is None else held_at,
        new_last_uuid=new_last_uuid,
        skipped=skipped,
        torn=torn,
        held=held,
        bytes_read=bytes_read,
    )


def render_for_summarizer(delta: Delta) -> str:
    """Plain text for the summarizer's stdin (DESIGN.md §4.3, §5).

    Format, in file order: ``[user]: …`` / ``[assistant]: …`` for the turn's
    text, and the tool markers as their own top-level lines
    ``[tool:<name>]: …`` / ``[tool:result]: …``. Empty lines (e.g. a
    thinking-only turn) are omitted.
    """
    out: list[str] = []
    for msg in delta.messages:
        for line in msg.text.split("\n"):
            if not line.strip():
                continue
            if line.startswith("[tool:"):
                out.append(line)
            else:
                out.append(f"[{msg.role}]: {line}")
    return "\n".join(out)
