"""Rendering untrusted entry text for the read-only UI (D14; plan 2026-09-29, slice 2).

``content_md`` and ``text_md`` are model output over transcripts, i.e. untrusted (I5).
:func:`render_md` is the ONLY producer of markup the templates treat as safe:

- CommonMark with raw HTML **disabled**, so any tag in the text is escaped as text;
- markdown-it's link validation left on, so ``javascript:``, ``vbscript:``, ``file:``
  and non-image ``data:`` destinations never become links;
- images are never ``<img>``: they render as their alt text plus the URL as text, so a
  summary cannot make a reader's browser fetch anything.

The frontmatter helpers reimplement, for the server, the readers the client owns
(``okf.body``, ``curate.is_digest``): the image does not ship ``client/``. The status
overlay is D12's, in the words ``loom log`` prints: the stored ``status: current`` is a
frozen literal, the links decide.
"""
from __future__ import annotations

import json
import re

from markdown_it import MarkdownIt
from markupsafe import Markup, escape

from server import links

# A leading frontmatter block, exactly ``okf._FRONTMATTER_RE``: the opening fence, the
# shortest run of lines up to the first line that is exactly ``---``, and its line break.
_FRONTMATTER_RE = re.compile(r"---\n(.*?)\n---(?:\n|\Z)", re.DOTALL)
_FIELD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):[ \t]*(.*?)[ \t]*$")
DIGEST_TYPE = "digest"  # okf.DIGEST_TYPE — frozen there, read here


def _render_image(self, tokens, idx, options, env) -> str:
    token = tokens[idx]
    text = self.renderInlineAsText(token.children or [], options, env)
    return (f'<span class="md-image">[image: {escape(text)}] '
            f'<code>{escape(token.attrGet("src") or "")}</code></span>')


_MD = MarkdownIt("commonmark", {"html": False})
_MD.add_render_rule("image", _render_image)


def render_md(text: str) -> Markup:
    """Untrusted markdown → HTML that cannot run script or fetch a resource."""
    return Markup(_MD.render(text or ""))


def split_frontmatter(content_md: str) -> tuple[list[tuple[str, str]], str]:
    """``([(key, value)], body)`` of a document; ``([], content_md)`` without a block.

    Top-level ``key: value`` lines become fields in document order; a JSON string scalar
    (every string ``okf`` writes) is shown decoded, anything else verbatim. Indented
    lines belong to the field above them (``sources``), kept as written. Only a block
    that OPENS the document and is closed counts — dropping text on a guess is worse
    than showing a little metadata (``okf.body``).
    """
    match = _FRONTMATTER_RE.match(content_md or "")
    if match is None:
        return [], content_md
    rows: list[tuple[str, list[str]]] = []
    for line in match.group(1).splitlines():
        field = _FIELD_RE.match(line)
        if field is not None:
            rows.append((field.group(1), [field.group(2)] if field.group(2) else []))
        elif rows:
            rows[-1][1].append(line)
    return [(key, _scalar("\n".join(lines))) for key, lines in rows], content_md[match.end():]


def _scalar(raw: str) -> str:
    if raw.startswith('"'):
        try:
            value = json.loads(raw)
        except ValueError:
            return raw
        if isinstance(value, str):
            return value
    return raw


def is_digest(content_md: str) -> bool:
    """Is this entry a per-project digest? Its leading frontmatter says ``type: digest``."""
    rows, _ = split_frontmatter(content_md)
    return dict(rows).get("type") == DIGEST_TYPE


def served_status_label(entry: dict) -> str:
    """``current`` · ``superseded (by #N)`` · ``duplicate (of #N)``, as ``loom log`` says it.

    ``entry`` carries the served ``status`` and ``links_in`` (``links.links_for_entries``
    shape). The link named is the most recent incoming one of the kind that decided the
    status — the reversal the reader is being pointed at.
    """
    status = entry.get("status") or links.STATUS_CURRENT
    kind, word = {links.STATUS_SUPERSEDED: ("supersedes", "by"),
                  links.STATUS_DUPLICATE: ("duplicates", "of")}.get(status, (None, None))
    matching = [link for link in entry.get("links_in") or () if link.get("kind") == kind]
    if not matching:
        return status
    newest = max(matching, key=lambda link: (str(link.get("created_at") or ""),
                                              link["from_entry"]))
    return f"{status} ({word} #{newest['from_entry']})"


def frontmatter_rows(entry: dict) -> list[tuple[str, str]]:
    """The entry's frontmatter fields with ``status`` replaced by the served one (D12)."""
    rows, _ = split_frontmatter(entry.get("content_md") or "")
    label = served_status_label(entry)
    return [(key, label if key == "status" else value) for key, value in rows]
