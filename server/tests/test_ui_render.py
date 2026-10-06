"""Rendering untrusted entry text for the UI (D14, plan 2026-09-29 slice 2: AC-7, AC-10).

`content_md` and `text_md` are model output over transcripts: untrusted (I5). The golden
manifest (`golden/ui_render/<case>.md` → `<case>.html`, AGENTS.md rule 11) was written by
hand from the CommonMark spec with raw HTML disabled and the plan's image rule, never by
running the renderer. Frontmatter expectations are literals shaped exactly like
`client/loom/okf.py` emits them.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from markupsafe import Markup

from server import ui_render

GOLDEN = Path(__file__).with_name("golden") / "ui_render"
CASES = sorted(path.stem for path in GOLDEN.glob("*.md"))


# --------------------------------------------------------------------------- #
# AC-7 — hostile markdown renders inert                                        #
# --------------------------------------------------------------------------- #
def test_the_golden_manifest_is_not_empty():
    assert {"basic", "raw_html", "dangerous_links", "images", "attribute_breakout"} <= set(CASES)


@pytest.mark.parametrize("case", CASES)
def test_render_matches_the_golden_manifest(case):
    source = (GOLDEN / f"{case}.md").read_text(encoding="utf-8")
    expected = (GOLDEN / f"{case}.html").read_text(encoding="utf-8")

    rendered = ui_render.render_md(source)

    assert isinstance(rendered, Markup)
    assert str(rendered) == expected


@pytest.mark.parametrize("case", CASES)
def test_no_rendered_output_can_run_script_or_fetch_anything(case):
    """The property behind the manifest, so a regenerated golden cannot hide a hole."""
    rendered = str(ui_render.render_md((GOLDEN / f"{case}.md").read_text(encoding="utf-8")))

    tags = re.findall(r"<[a-zA-Z][^>]*>", rendered)   # real tags; escaped text has none
    names = {re.match(r"<([a-zA-Z][a-zA-Z0-9]*)", tag).group(1) for tag in tags}
    assert names <= {"h2", "p", "ul", "li", "strong", "code", "a", "span", "em"}
    assert not any(re.search(r"\son[a-z]+\s*=", tag) for tag in tags)
    for href in re.findall(r'href="([^"]*)"', rendered):
        assert href.startswith("https://"), href


def test_escaped_text_is_not_marked_safe_twice():
    """Jinja2 must not re-escape the renderer's output, and must escape everything else."""
    rendered = ui_render.render_md("a & b")
    assert str(rendered) == "<p>a &amp; b</p>\n"
    assert Markup("{}").format(rendered) == rendered


# --------------------------------------------------------------------------- #
# Frontmatter: split, digest detection                                         #
# --------------------------------------------------------------------------- #
ENTRY = (
    "---\n"
    "type: decision-log-entry\n"
    'title: "httpx instead of requests"\n'
    'timestamp: "2026-09-25T10:00:00Z"\n'
    "generated: true\n"
    "status: current\n"
    'project: "github.com/org/b"\n'
    'tags: ["b"]\n'
    'item_types: ["decision", "task"]\n'
    "sources:\n"
    '  - session: "s-1"\n'
    '    covered_from: null\n'
    '    covered_to: "u-9"\n'
    '    model: "sonnet"\n'
    '    prompt: "v3"\n'
    "---\n"
    "## Context\n"
    "Worked on B.\n"
)
DIGEST = (
    "---\n"
    "type: digest\n"
    'title: "Current state of decisions (2026-09-26)"\n'
    "status: current\n"
    "---\n"
    "## Current state\n"
)


def test_split_frontmatter_returns_the_fields_in_order_and_the_body():
    rows, body = ui_render.split_frontmatter(ENTRY)

    assert rows == [
        ("type", "decision-log-entry"),
        ("title", "httpx instead of requests"),
        ("timestamp", "2026-09-25T10:00:00Z"),
        ("generated", "true"),
        ("status", "current"),
        ("project", "github.com/org/b"),
        ("tags", '["b"]'),
        ("item_types", '["decision", "task"]'),
        ("sources", '  - session: "s-1"\n    covered_from: null\n    covered_to: "u-9"\n'
                    '    model: "sonnet"\n    prompt: "v3"'),
    ]
    assert body == "## Context\nWorked on B.\n"


@pytest.mark.parametrize("content", [
    "## Context\nA pre-OKF entry.\n",                        # no frontmatter
    "---\ntype: decision-log-entry\n## Context\nnever closed\n",  # unterminated
    "Intro\n---\ntype: digest\n---\n",                       # a block that does not OPEN it
])
def test_content_without_a_leading_closed_block_is_all_body(content):
    assert ui_render.split_frontmatter(content) == ([], content)


def test_digest_is_the_frontmatter_type_only():
    assert ui_render.is_digest(DIGEST) is True
    assert ui_render.is_digest(ENTRY) is False
    # A session entry that QUOTES a digest's frontmatter in its body is not a digest.
    assert ui_render.is_digest(ENTRY + "\n```\n---\ntype: digest\n---\n```\n") is False
    assert ui_render.is_digest("## Context\nno frontmatter\n") is False


# --------------------------------------------------------------------------- #
# AC-10 (unit) — the served status wins over the stored literal (D12)          #
# --------------------------------------------------------------------------- #
def _entry(status: str, links_in: list[dict]) -> dict:
    return {"id": 17, "content_md": ENTRY, "status": status, "links_in": links_in}


def _link(from_entry: int, kind: str, created_at: str) -> dict:
    return {"from_entry": from_entry, "kind": kind, "rationale": None, "created_at": created_at}


@pytest.mark.parametrize(("status", "links_in", "label"), [
    ("current", [], "current"),
    ("superseded", [_link(19, "supersedes", "2026-09-24T09:00:00Z"),
                    _link(21, "supersedes", "2026-09-25T09:00:00Z")], "superseded (by #21)"),
    ("duplicate", [_link(18, "duplicates", "2026-09-24T09:00:00Z")], "duplicate (of #18)"),
    # supersedes outranks duplicates (links.derive_status); the newer duplicate is not named.
    ("superseded", [_link(19, "supersedes", "2026-09-24T09:00:00Z"),
                    _link(22, "duplicates", "2026-09-26T09:00:00Z")], "superseded (by #19)"),
])
def test_served_status_label(status, links_in, label):
    assert ui_render.served_status_label(_entry(status, links_in)) == label


def test_frontmatter_rows_overlay_the_served_status_on_the_stored_literal():
    entry = _entry("superseded", [_link(21, "supersedes", "2026-09-25T09:00:00Z")])

    rows = dict(ui_render.frontmatter_rows(entry))

    assert rows["status"] == "superseded (by #21)"
    assert rows["title"] == "httpx instead of requests"


def test_frontmatter_rows_of_a_pre_okf_entry_are_empty():
    entry = {"id": 3, "content_md": "## Context\nold\n", "status": "current", "links_in": []}
    assert ui_render.frontmatter_rows(entry) == []
