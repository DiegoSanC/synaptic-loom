"""Summarizer v4 (D15, 2026-10-01): v3 with every word of the entry in English.

The logbook is searched by keyword (`/loom --grep`), so an entry written in the
language of its session is invisible to a teammate who searches in another one. v4 is
v3 with exactly two rules rewritten — the language rule (9) and the evidence rule (4),
whose "verbatim quote" a translation would contradict — and nothing else: the grammar
every reader parses, the labels and the measured v3 behaviour are kept byte for byte.
"""
from pathlib import Path

from loom import processor

REPO_ROOT = Path(__file__).resolve().parents[3]
V3 = (REPO_ROOT / "prompts" / "summarizer_v3.md").read_text(encoding="utf-8")
V4 = (REPO_ROOT / "prompts" / "summarizer_v4.md").read_text(encoding="utf-8")

RULE_4_V3 = "4. Minimum evidence per item: a short verbatim quote, a file or a commit mentioned.\n"
RULE_4_V4 = ("4. Minimum evidence per item: a short quote (in English, rule 9), a file or a\n"
             "   commit mentioned.\n")
RULE_9_V3 = (
    "9. Write the prose in the language of the excerpt itself: an English excerpt\n"
    "   gets English prose, a Spanish excerpt Spanish prose. The section headings,\n"
    "   the four labels and the NO_ENTRY sentinel are ALWAYS literal English, exactly\n"
    "   as written in the format below, whatever the language of the excerpt.\n"
)
RULE_9_V4 = (
    "9. Write EVERYTHING in English, whatever the language of the excerpt: the prose,\n"
    "   the rationale and the evidence. A quote from a non-English excerpt is translated\n"
    "   into English and stays in quotation marks. File names, commits, identifiers,\n"
    "   ticket ids and code stay exactly as written. The section headings, the four\n"
    "   labels and the NO_ENTRY sentinel are literal English, exactly as written in the\n"
    "   format below.\n"
)


def test_v4_is_v3_with_only_the_language_and_evidence_rules_rewritten():
    assert V3.count(RULE_4_V3) == 1 and V3.count(RULE_9_V3) == 1
    assert V4 == V3.replace(RULE_4_V3, RULE_4_V4).replace(RULE_9_V3, RULE_9_V4)


def test_v4_is_a_labelled_version_parsed_like_v3():
    summary = ("## Context\nWork on the gate.\n## Decisions\n"
               "- [decision] **Use SQLite** — small team. Evidence: db.py.\n"
               "- [task] **Add a backup** — owner Ana. Evidence: \"we need it\".\n")

    assert "v4" in processor.LABELLED_PROMPT_VERSIONS
    assert processor.parse_item_types(summary, "v4") == (["decision", "task"], 0)
    assert ([i.text_md for i in processor.split_items(summary, "v4")]
            == [i.text_md for i in processor.split_items(summary, "v3")])
