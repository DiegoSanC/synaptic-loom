"""Unit tests for T.F3 (E14) — typed capture: strict label parse + ``item_types``.

Scope: the units behind the frozen ``t-label-grammar`` interface in isolation —
the PROCESSOR's strict label parse over the ``## Decisions`` section, the additive
``item_types`` frontmatter of :func:`okf.wrap_entry`, the label tolerance of
:func:`okf.extract_title`, and the version-derived default ``prompt_path``. The
real-boundary pipeline behavior (golden replay, payload, run-log event, the real
``install.sh``) is the frozen goal ``test/waves/i/test_goal_e14_typed_items.py``.

Two properties are load-bearing and are asserted from several angles here:

- **The parse never fails the entry.** It is total: any bullet shape that is not
  the strict grammar resolves to the ``[decision]`` fallback, and no input —
  empty, headingless, hostile — raises.
- **The parse never touches content.** It returns a label set and a COUNT; the
  summary body is passed through verbatim (D7), and the run-log detail the
  processor builds out of it is ``count=<n>`` (I5/I6 — never a byte of the label
  or the summary).
"""
from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from loom import okf, processor
from loom.api_client import ServerUnreachable
from loom.config import DEFAULT_PROMPT_VERSION, Config

V2 = "v2"
REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_V2 = REPO_ROOT / "prompts" / "summarizer_v2.md"
REMOTE_URL = "git@github.com:org/typed-items.git"
REMOTE_CANONICAL = "github.com/org/typed-items"


def _decisions(*bullets: str) -> str:
    return "## Context\nSomething happened.\n## Decisions\n" + "".join(
        b if b.endswith("\n") else b + "\n" for b in bullets
    )


# --------------------------------------------------------------------------- #
# The closed vocabulary                                                        #
# --------------------------------------------------------------------------- #
def test_all_four_labels_are_recognised_sorted_and_deduped():
    summary = _decisions(
        "- [task] **Ship it** — x. Status: current.",
        "- [method] **Always pin versions** — y. Status: current.",
        "- [decision] **Use X over Y** — z. Status: current.",
        "- [fact] **The host is WSL2** — w. Status: current.",
        "- [method] **Never edit schema.sql in place** — v. Status: current.",
    )
    assert processor.parse_item_types(summary, V2) == (
        ["decision", "fact", "method", "task"], 0
    )


def test_unknown_label_falls_back_to_decision_and_is_counted():
    summary = _decisions(
        "- [banana] **Use X over Y** — z. Status: current.",
        "- [method] **Always pin versions** — w. Status: current.",
    )
    assert processor.parse_item_types(summary, V2) == (["decision", "method"], 1)


def test_bare_bullet_falls_back_to_decision_and_is_counted():
    summary = _decisions("- **Bare bullet decision** — (rationale not stated).")
    assert processor.parse_item_types(summary, V2) == (["decision"], 1)


@pytest.mark.parametrize("label", ["Method", "METHOD", "Decision", "Fact", "Task"])
def test_labels_are_case_sensitive_lowercase(label):
    """``[Method]`` is NOT the frozen label: the grammar is lowercase literals."""
    summary = _decisions(f"- [{label}] **X** — y. Status: current.")
    assert processor.parse_item_types(summary, V2) == (["decision"], 1)


def test_label_must_be_followed_by_a_space():
    """The frozen regex is ``^- \\[(...)\\] `` — the trailing space is part of it."""
    summary = _decisions("- [method]**X** — y. Status: current.")
    assert processor.parse_item_types(summary, V2) == (["decision"], 1)


def test_label_must_lead_the_bullet():
    """A label anywhere but at the head of the bullet is not a label."""
    summary = _decisions("- **X** — [method] applies here. Status: current.")
    assert processor.parse_item_types(summary, V2) == (["decision"], 1)


# --------------------------------------------------------------------------- #
# What counts as an item                                                       #
# --------------------------------------------------------------------------- #
def test_only_the_decisions_section_is_parsed():
    """``## Open items`` bullets are not items: they would forge fallbacks."""
    summary = (
        "## Context\n- a context bullet\n"
        "## Decisions\n- [method] **X** — y. Status: current.\n"
        "## Open items\n- Runbook update pending.\n"
    )
    assert processor.parse_item_types(summary, V2) == (["method"], 0)


def test_every_decisions_section_is_parsed_not_just_the_first():
    """A restated heading otherwise drops every item after it — invisibly, since the
    entry still looks correctly typed from the half that survived."""
    summary = (
        "## Context\nX.\n"
        "## Decisions\n- [decision] **Use X over Y** — z. Status: current.\n"
        "## Notes\nsomething\n"
        "## Decisions\n- [method] **Always run make contract** — y. Status: current.\n"
    )
    assert processor.parse_item_types(summary, V2) == (["decision", "method"], 0)


def test_localized_heading_still_finds_the_decisions_section():
    """Prompt v2 rule 8: the PROSE follows the conversation's language, so the heading
    can read ``## Decisiones``. The section is found by its bracket-led bullets."""
    summary = (
        "## Contexto\nSe eligió una librería.\n"
        "## Decisiones\n- [decision] **Usar httpx** — async. Estado: vigente.\n"
        "## Pendientes\n- ninguno\n"
    )
    assert processor.parse_item_types(summary, V2) == (["decision"], 0)


def test_translated_labels_under_a_translated_heading_are_counted_not_invisible():
    """The most likely drift for this pilot (Spanish sessions): the model translates
    the labels too. Keying the section lookup on the English literals would report
    "untyped, zero fallbacks" — the one reading the operator must never get, because
    it is indistinguishable from a clean capture."""
    summary = (
        "## Contexto\nSe trabajó en el CLI.\n"
        "## Decisiones\n"
        "- [método] **Siempre correr make contract** — regla. Estado: vigente.\n"
        "- [decisión] **Usar argparse** — sin dependencias. Estado: vigente.\n"
        "- [tarea] **Actualizar el runbook** — (no consta). Estado: vigente.\n"
        "## Pendientes\n- ninguno\n"
    )
    assert processor.parse_item_types(summary, V2) == (["decision"], 3)


def test_a_localized_section_is_never_confused_with_open_items():
    """`- [ ]` task checkboxes are not bracket-led labels: an Open-items list must not
    be mistaken for the Decisions section when the heading is not the English one."""
    summary = (
        "## Contexto\nX.\n"
        "## Pendientes\n- [ ] revisar el runbook\n- [ ] avisar a Diego\n"
    )
    assert processor.parse_item_types(summary, V2) == ([], 0)


def test_continuation_and_nested_bullets_are_not_items():
    """A deeper-indented bullet is the body of the item above it — evidence, a
    sub-list. Typing it would add a phantom [decision] and a phantom fallback."""
    summary = _decisions(
        "- [fact] **X** — y. Status: current.",
        "  continued on the next line",
        "  - a nested bullet",
        "    - deeper still",
        "",
    )
    assert processor.parse_item_types(summary, V2) == (["fact"], 0)


# --------------------------------------------------------------------------- #
# What counts as an item: every bullet shape, or drift is invisible            #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bullet", [
    "-  [method] **X** — y.",     # two spaces after the marker
    "* [method] **X** — y.",      # asterisk marker
    "+ [method] **X** — y.",      # plus marker
    "1. [method] **X** — y.",     # ordered list
    "1) [method] **X** — y.",     # ordered list, paren
    "-\t[method] **X** — y.",     # tab after the marker
    " - [method] **X** — y.",     # one column of indent
])
def test_off_grammar_bullet_shapes_are_counted_not_invisible(bullet):
    """The spec is "any other bullet shape -> [decision] fallback". A shape the parse
    does not even SEE is worse than a mistyped one: it reports zero drift."""
    assert processor.parse_item_types(_decisions(bullet), V2) == (["decision"], 1)


def test_a_wholly_reshaped_summary_reports_its_drift():
    summary = _decisions(
        "* [method] **Always run make contract** — rule. Status: current.",
        "1. [decision] **Use argparse** — no dep. Status: current.",
        "-  [task] **Update the runbook** — (not stated). Status: current.",
    )
    assert processor.parse_item_types(summary, V2) == (["decision"], 3)


def test_no_decisions_section_yields_nothing():
    assert processor.parse_item_types("## Context\nJust prose.\n", V2) == ([], 0)
    assert processor.parse_item_types("", V2) == ([], 0)


def test_empty_decisions_section_yields_nothing():
    assert processor.parse_item_types("## Decisions\n\n## Open items\n- x\n", V2) == ([], 0)


# --------------------------------------------------------------------------- #
# Fenced content is quoted, not structural                                     #
# --------------------------------------------------------------------------- #
def test_a_fenced_template_example_never_beats_the_real_decisions_section():
    """A session about editing prompts/summarizer_v2.md quotes the output template.
    Typing the entry from the EXAMPLE is unrecoverable: item_types is written once at
    insert, a re-POST is a 409, and there is no backfill route."""
    summary = (
        "## Context\nWe edited the summarizer prompt.\n"
        "```markdown\n"
        "## Decisions\n"
        "- [fact] **<the finding>** — <why>. Status: current.\n"
        "```\n"
        "## Decisions\n"
        "- [method] **Always run make contract** — rule. Status: current.\n"
    )
    assert processor.parse_item_types(summary, V2) == (["method"], 0)


def test_a_fenced_bullet_inside_the_decisions_section_is_not_an_item():
    summary = (
        "## Decisions\n"
        "- [method] **Always run make contract** — rule. Status: current.\n"
        "```\n- [banana] not a real item\n- also not one\n```\n"
    )
    assert processor.parse_item_types(summary, V2) == (["method"], 0)


def test_tilde_fences_toggle_too_and_backticks_do_not_close_them():
    summary = (
        "## Decisions\n"
        "~~~\n## Decisions\n- [fact] **quoted** — x.\n~~~\n"
        "- [task] **The real item** — y. Status: current.\n"
    )
    assert processor.parse_item_types(summary, V2) == (["task"], 0)


def test_a_hash_comment_inside_the_section_does_not_truncate_it():
    """`# contract` at column 0 is an h1 in markdown, but the v2 format only ever uses
    h2 for sections — ending the section there would drop every item below it."""
    summary = (
        "## Decisions\n"
        "- [decision] **Use argparse** — no dep. Status: current.\n"
        "# contract\n"
        "- [method] **Always run make contract** — rule. Status: current.\n"
    )
    assert processor.parse_item_types(summary, V2) == (["decision", "method"], 0)


# --------------------------------------------------------------------------- #
# Only a known labeled version is parsed (allowlist)                           #
# --------------------------------------------------------------------------- #
def test_v1_summaries_are_never_parsed():
    """A v1 summary has no labels by construction: parsing it would stamp every
    entry ``[decision]`` and log a fallback per bullet for a prompt that never
    promised labels (AC: v1 entries keep rendering untouched)."""
    v1_summary = (
        "## Decisions\n- **Use SQLite** — small team. Status: current.\n"
        "- **Keep bearer tokens** — x. Status: current.\n"
    )
    assert processor.parse_item_types(v1_summary, "v1") == ([], 0)
    # …and even a labeled body under a v1 config stays untyped.
    assert processor.parse_item_types(_decisions("- [method] **X** — y."), "v1") == ([], 0)


@pytest.mark.parametrize("version", ["v9", "v2.1", "digest-v1", "", "sonnet"])
def test_an_unknown_version_is_left_untyped_rather_than_parsed_as_v2(version):
    """An ALLOWLIST, not an exclusion set. Parsing an unknown version as v2 types every
    unlabeled bullet ``[decision]`` — data that looks right and is wrong, permanently
    (``item_types`` is written once at insert; there is no backfill). Untyped is
    honest, visible, and still re-derivable from ``content_md``. A future labeled
    version is one literal added to LABELLED_PROMPT_VERSIONS — deliberately."""
    assert processor.parse_item_types(_decisions("- [task] **X** — y."), version) == (
        [], 0
    )
    assert {"v2", "v3"} <= processor.LABELLED_PROMPT_VERSIONS


# --------------------------------------------------------------------------- #
# Totality: the parse may never fail the entry                                 #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("summary", [
    "",
    "NO_ENTRY",
    "- [method] **X** — y.\n",                       # no headings at all
    "## Decisions\n" + "- " * 5000 + "\n",           # degenerate bullet
    "## Decisions\n- [method] " + "á" * 10000 + "\n",  # long unicode
    "## Decisions\n- [\x00] **X** — y.\n",           # NUL in the label
    "## Decisions\n- [decision] \n",                 # label with an empty body
    "#Decisions\n- [task] **X** — y.\n",             # malformed heading
])
def test_parse_is_total(summary):
    types, fallbacks = processor.parse_item_types(summary, V2)
    assert isinstance(types, list) and isinstance(fallbacks, int)
    assert all(t in ("decision", "method", "task", "fact") for t in types)
    assert types == sorted(set(types)) and fallbacks >= 0


def test_parse_returns_counts_only_never_content():
    """I5/I6: what leaves the parse is a closed vocabulary and an integer, so the
    run-log detail the processor builds from it cannot carry summary bytes."""
    secret = "sk-live-DEADBEEFDEADBEEF"
    types, fallbacks = processor.parse_item_types(
        _decisions(f"- [{secret}] **X** — {secret}."), V2)
    assert (types, fallbacks) == (["decision"], 1)
    assert secret not in f"count={fallbacks}"


def test_parse_does_not_modify_the_summary():
    """D7: the body is immutable — the parse only reads it."""
    summary = _decisions("- [banana] **X** — y.", "- [method] **Z** — w.")
    before = summary
    processor.parse_item_types(summary, V2)
    assert summary == before


# --------------------------------------------------------------------------- #
# The run-log event: one line per CAPTURED entry, never one per attempt        #
# --------------------------------------------------------------------------- #
class _FakeApi:
    """The POST seam only. ``responses`` are status codes or exceptions to raise."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.posted: list[dict] = []

    def post_entry(self, payload: dict):
        self.posted.append(payload)
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(status_code=outcome)


def _pipeline(tmp_path: Path, summary: str) -> tuple[Config, Path, dict]:
    """A real git repo + transcript + LOOM_HOME, with the summarizer stubbed."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", REMOTE_URL], cwd=repo, check=True)

    transcript = tmp_path / "transcripts" / "s1.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("\n".join(json.dumps(line) for line in [
        {"type": "file-history-snapshot", "messageId": "m0", "snapshot": {}},
        {"type": "user", "uuid": "s1-u1", "cwd": str(repo), "sessionId": "s1",
         "message": {"role": "user", "content": "Refactor the auth flow"}},
        {"type": "assistant", "uuid": "s1-a1", "cwd": str(repo), "sessionId": "s1",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "Decided to keep tokens."}]}},
    ]) + "\n", encoding="utf-8")

    stub = tmp_path / "bin" / "claude"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text("#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\n"
                    f"sys.stdout.write({summary!r})\n", encoding="utf-8")
    stub.chmod(0o755)

    cfg = Config(
        loom_home=tmp_path / "loom_home", server_url="http://127.0.0.1:9", token="t",
        claude_bin=str(stub), prompt_path=PROMPT_V2, prompt_version="v2",
        scanner_engine="gitleaks", sweep_transcripts_root=tmp_path / "transcripts",
    )
    cfg.ensure_dirs()
    marker_path = cfg.queue_dir / "s1.json"
    marker_path.write_text(json.dumps({
        "session_id": "s1", "transcript_path": str(transcript), "cwd": str(repo),
        "reason": "prompt_input_exit", "marked_at": "2026-08-12T00:00:00Z",
    }), encoding="utf-8")
    return cfg, marker_path, {}


def _fallback_lines(cfg: Config) -> list[str]:
    if not cfg.log_file.exists():
        return []
    return [ln for ln in cfg.log_file.read_text(encoding="utf-8").splitlines()
            if len(ln.split("\t")) >= 3 and ln.split("\t")[2] == "label_fallback"]


GARBAGE_SUMMARY = (
    "## Context\nX.\n## Decisions\n"
    "- [banana] **Use X over Y** — z. Status: current.\n"
    "- **Bare bullet decision** — (rationale not stated). Status: current.\n"
    "- [method] **Always pin versions** — w. Status: current.\n"
)


def _run(cfg: Config, api, state: dict, marker_path: Path) -> processor.RunReport:
    report = processor.RunReport()
    registry = processor.Registry(True, {REMOTE_CANONICAL})
    processor._process_marker(cfg, api, registry, state, report, marker_path)
    return report


def test_one_label_fallback_line_per_captured_entry(tmp_path):
    cfg, marker_path, state = _pipeline(tmp_path, GARBAGE_SUMMARY)
    api = _FakeApi(201)

    report = _run(cfg, api, state, marker_path)

    assert [o.action for o in report.outcomes] == ["posted"]
    assert api.posted[0]["item_types"] == ["decision", "method"]
    assert [ln.split("\t")[3] for ln in _fallback_lines(cfg)] == ["count=2"]


def test_a_retried_delivery_does_not_inflate_the_fallback_count(tmp_path):
    """One entry, two fallback items, a server that is down for the first pass. The
    count is a MODEL-drift indicator; if it were logged where it is computed, an
    outage would read as drift — `loom status` would report 4 fallbacks for an entry
    that has 2, and the operator's only signal for "the summarizer stopped following
    the grammar" would move for reasons unrelated to the summarizer."""
    cfg, marker_path, state = _pipeline(tmp_path, GARBAGE_SUMMARY)
    api = _FakeApi(ServerUnreachable("down"), 201)

    first = _run(cfg, api, state, marker_path)
    assert [o.action for o in first.outcomes] == ["kept_network_error"]
    assert _fallback_lines(cfg) == [], "nothing was captured: nothing to report yet"

    second = _run(cfg, api, state, marker_path)
    assert [o.action for o in second.outcomes] == ["posted"]
    assert [ln.split("\t")[3] for ln in _fallback_lines(cfg)] == ["count=2"]


def test_a_quarantined_entry_still_reports_its_fallbacks(tmp_path):
    secret = "AKIAIOSFODNN7EXAMPLE"  # matches the regex engine's aws_access_key rule
    cfg, marker_path, state = _pipeline(
        tmp_path, GARBAGE_SUMMARY + f"## Open items\n- {secret}\n")
    api = _FakeApi()

    report = _run(cfg, api, state, marker_path)

    assert [o.action for o in report.outcomes] == ["quarantined"]
    assert api.posted == [], "a quarantined entry never leaves the machine"
    assert [ln.split("\t")[3] for ln in _fallback_lines(cfg)] == ["count=2"]


def test_the_run_log_never_carries_label_or_summary_text(tmp_path):
    """I5/I6: the detail is `count=<n>` and nothing else."""
    cfg, marker_path, state = _pipeline(tmp_path, GARBAGE_SUMMARY)
    _run(cfg, api := _FakeApi(201), state, marker_path)

    assert api.posted
    log = cfg.log_file.read_text(encoding="utf-8")
    for leaked in ("banana", "Bare bullet decision", "Always pin versions", "[method]"):
        assert leaked not in log


# --------------------------------------------------------------------------- #
# okf.wrap_entry — additive item_types                                         #
# --------------------------------------------------------------------------- #
def _wrap(summary_md: str, **kwargs) -> str:
    return okf.wrap_entry(
        summary_md, canonical_remote="github.com/org/repo", session_id="s",
        covered_from=None, covered_to=None, model="sonnet", prompt_version="v2",
        timestamp="2026-08-12T00:00:00Z", **kwargs,
    )


def _frontmatter(entry: str) -> str:
    return entry.split("---\n", 2)[1]


def test_wrap_without_item_types_is_byte_identical_to_the_v1_shape():
    assert _wrap("- **T** — x.") == _wrap("- **T** — x.", item_types=None)
    assert "item_types" not in _wrap("- **T** — x.")


def test_wrap_with_empty_item_types_emits_no_line():
    assert "item_types" not in _wrap("- **T** — x.", item_types=[])


def test_wrap_emits_a_sorted_deduped_json_array():
    out = _wrap("- [task] **T** — x.", item_types=["task", "decision", "task"])
    [line] = [ln for ln in _frontmatter(out).splitlines()
              if ln.startswith("item_types:")]
    assert line == 'item_types: ["decision", "task"]'
    assert json.loads(line.split(":", 1)[1].strip()) == ["decision", "task"]


def test_wrap_keeps_item_types_inside_the_frontmatter_before_sources():
    fm = _frontmatter(_wrap("- [fact] **T** — x.", item_types=["fact"]))
    lines = fm.splitlines()
    assert lines.index("item_types: [\"fact\"]") < lines.index("sources:")


def test_wrap_never_edits_the_body_labels():
    body = "## Decisions\n- [method] **Always run make contract** — y. Status: current."
    out = _wrap(body, item_types=["method"])
    assert out.split("---\n", 2)[2].strip() == body.strip()


# --------------------------------------------------------------------------- #
# okf.extract_title — the label is transparent                                 #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("label", ["decision", "method", "task", "fact"])
def test_extract_title_strips_the_leading_label(label):
    assert okf.extract_title(f"- [{label}] **T** — x.") == "T"


def test_extract_title_is_identical_with_and_without_a_label():
    assert okf.extract_title("- **T** — x.") == okf.extract_title("- [task] **T** — x.")


def test_extract_title_head_is_the_closed_vocabulary_only():
    """Widening the head to any bracketed token changes v1 titles: extract_title scans
    the WHOLE document, so an "Open items" checkbox or a priority tag would start
    producing (or outranking) titles that have nothing to do with the grammar."""
    assert okf.extract_title("- [banana] **Use X over Y** — z.") is None
    assert okf.extract_title("- [ ] **Fix the flake**") is None
    assert okf.extract_title("- [P0] **Ship it**\n- **Second thing** — x.") == (
        "Second thing"
    )
    assert okf.extract_title("- [2026-08-12] **Adopted X**") is None


def test_extract_title_truncation_applies_to_the_label_free_title():
    title = okf.extract_title("- [method] **" + "palabra " * 20 + "**")
    assert title is not None and len(title) <= 60 and title.endswith("…")
    assert "[method]" not in title


def test_extract_title_keeps_brackets_that_are_part_of_the_title():
    assert okf.extract_title("- **[WIP] keep this**") == "[WIP] keep this"


# --------------------------------------------------------------------------- #
# config — version-derived prompt_path                                         #
# --------------------------------------------------------------------------- #
def test_default_prompt_version_is_v2():
    assert DEFAULT_PROMPT_VERSION == "v2"


def _cfg(tmp_path: Path, **kwargs) -> Config:
    return Config(loom_home=tmp_path, server_url="http://x", token="t", **kwargs)


def test_prompt_path_is_derived_from_the_prompt_version(tmp_path):
    assert _cfg(tmp_path).prompt_path == tmp_path / "prompts" / "summarizer_v2.md"
    assert _cfg(tmp_path, prompt_version="v1").prompt_path == (
        tmp_path / "prompts" / "summarizer_v1.md"
    )


def test_explicit_prompt_path_always_wins(tmp_path):
    cfg = _cfg(tmp_path, prompt_version="v2", prompt_path=tmp_path / "own.md")
    assert cfg.prompt_path == tmp_path / "own.md"


@pytest.mark.parametrize("hostile", [
    "../../../etc/passwd", "/etc/passwd", "..", "v2/../../secret", "~", "",
    "v2\x00", "a b", None, 2,
])
def test_a_hostile_prompt_version_cannot_escape_the_prompts_directory(tmp_path, hostile):
    """``prompt_version`` comes from a hand-editable config.json and is joined onto
    ``<LOOM_HOME>/prompts``: a traversing value would make the summarizer read an
    arbitrary file and paste it into the ``claude -p`` argument."""
    cfg = _cfg(tmp_path, prompt_version=hostile)
    assert cfg.prompt_path.parent == tmp_path / "prompts"
    assert cfg.prompt_path.name == f"summarizer_{DEFAULT_PROMPT_VERSION}.md"


# --------------------------------------------------------------------------- #
# config — the version that is RECORDED is the version that RAN                #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("written,resolved", [
    ("v1 ", "v1"),        # stray space: a typo, not an intention
    (" v2", "v2"),
    ("V1", "v1"),         # case-insensitive FS would run v1 while recording "V1"
    ("V2", "v2"),
    ("v1\n", "v1"),
    ("../../etc/passwd", DEFAULT_PROMPT_VERSION),   # not a version at all
    (None, DEFAULT_PROMPT_VERSION),
])
def test_prompt_version_is_resolved_once_for_every_consumer(tmp_path, written, resolved):
    """The filename, the ``prompt:`` provenance, ``content_hash``'s material and the
    parse gate all read ``cfg.prompt_version``. Normalizing for the filename only is
    what lets a machine capture with v2 while stamping every entry "v1 " — or run the
    UNLABELLED v1 prompt (case-insensitive FS) and parse its output as v2, typing
    every bullet ``[decision]``. One resolved value, or none."""
    cfg = _cfg(tmp_path, prompt_version=written)
    assert cfg.prompt_version == resolved
    assert cfg.prompt_path.name == f"summarizer_{resolved}.md"
    # the gate reads the same string the file name was built from
    assert (processor.parse_item_types(_decisions("- **bare** — x."),
                                       cfg.prompt_version) != ([], 0)) == (
        resolved in processor.LABELLED_PROMPT_VERSIONS
    )


def test_the_version_that_names_the_prompt_is_the_version_that_is_recorded(tmp_path):
    """The end-to-end shape of the asymmetry: what config.json said, what was read,
    and what an entry will be stamped with must be one value."""
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "summarizer_v1.md").write_text("v1 prompt", encoding="utf-8")
    cfg = _cfg(tmp_path, prompt_version="V1 ")
    assert cfg.read_prompt() == "v1 prompt"
    assert cfg.prompt_version == "v1"
    assert processor.parse_item_types(_decisions("- [method] **X** — y."),
                                      cfg.prompt_version) == ([], 0)


# --------------------------------------------------------------------------- #
# D13 (slice C, C1): items — what `split_items` cuts, verbatim                 #
# --------------------------------------------------------------------------- #
ITEMS_MANIFEST = Path(__file__).parent / "unit" / "fixtures" / "items_manifest.json"
_MANIFEST = {k: v for k, v in json.loads(ITEMS_MANIFEST.read_text(encoding="utf-8")).items()
             if not k.startswith("_")}


@pytest.mark.parametrize("golden", sorted(_MANIFEST))
def test_split_items_matches_the_hand_written_golden_manifest(golden):
    """The manifest was written by hand from each golden file: ordinals, labels,
    `labelled`, and every item's text VERBATIM. A splitter that trims, re-joins or
    re-orders lines fails here byte for byte."""
    expected = _MANIFEST[golden]
    summary = (REPO_ROOT / golden).read_text(encoding="utf-8")
    got = processor.split_items(summary, expected["prompt_version"])
    assert [[n, i.item_type, i.labelled, i.text_md] for n, i in enumerate(got, 1)] == \
        expected["items"]
    assert all(i.text_md in summary for i in got)


def test_an_item_keeps_its_continuation_lines_and_sub_bullets_verbatim():
    summary = _decisions(
        "- [fact] **X** — y.",
        "  continued on the next line",
        "  - a nested bullet",
        "    - deeper still",
        "",
        "- [method] **Z** — w.",
    )
    assert [(i.item_type, i.labelled, i.text_md)
            for i in processor.split_items(summary, V2)] == [
        ("fact", True, "- [fact] **X** — y.\n  continued on the next line\n"
                       "  - a nested bullet\n    - deeper still"),
        ("method", True, "- [method] **Z** — w."),
    ]


def test_items_of_every_decisions_section_are_numbered_in_document_order():
    summary = ("## Decisions\n- [decision] **A** — a.\n## Open questions\n- Why?\n"
               "## Decisions\n- [task] **B** — b.\n")
    assert [(i.item_type, i.text_md) for i in processor.split_items(summary, V2)] == [
        ("decision", "- [decision] **A** — a."), ("task", "- [task] **B** — b.")]


def test_a_fenced_block_inside_an_item_is_part_of_its_text_not_an_item():
    summary = _decisions(
        "- [method] **Quote the template** — like this:",
        "  ```",
        "- [decision] **not an item**",
        "  ```",
        "- [fact] **After the fence** — x.",
    )
    got = processor.split_items(summary, V2)
    assert [i.item_type for i in got] == ["method", "fact"]
    assert "- [decision] **not an item**" in got[0].text_md


def test_an_off_grammar_bullet_is_an_unlabelled_decision_item():
    got = processor.split_items(_decisions("- [método] **No editar schema.sql** — x."), V2)
    assert [(i.item_type, i.labelled) for i in got] == [("decision", False)]


@pytest.mark.parametrize("version", ["v1", "v9", ""])
def test_split_items_is_empty_for_an_unlabelled_version(version):
    assert processor.split_items(_decisions("- [task] **X** — y."), version) == []


@pytest.mark.parametrize("summary", [
    (REPO_ROOT / golden).read_text(encoding="utf-8") for golden in sorted(_MANIFEST)
] + [
    GARBAGE_SUMMARY,
    _decisions("* [method] **A** — x.", "1. [decision] **B** — y.", "-  [task] **C** — z."),
    "## Decisiones\n- [método] **X** — y.\n- [task] **Y** — z.\n",
    "## Context\n```\n## Decisions\n- [method] **quoted**\n```\n## Decisions\n- [fact] **F**\n",
    "## Context\nJust prose.\n",
])
def test_parse_item_types_is_the_label_set_of_split_items(summary):
    """Storage-time typing (`item_types`) and the item rows can never disagree: the
    server refuses an entry whose two statements differ."""
    items = processor.split_items(summary, V2)
    assert processor.parse_item_types(summary, V2) == (
        sorted({i.item_type for i in items}), sum(not i.labelled for i in items))


def test_a_labelled_capture_posts_its_items_and_a_consistent_label_set(tmp_path):
    cfg, marker_path, state = _pipeline(tmp_path, GARBAGE_SUMMARY)
    api = _FakeApi(201)
    _run(cfg, api, state, marker_path)
    [payload] = api.posted
    assert payload["items"] == [
        {"item_type": "decision", "labelled": False,
         "text_md": "- [banana] **Use X over Y** — z. Status: current."},
        {"item_type": "decision", "labelled": False,
         "text_md": "- **Bare bullet decision** — (rationale not stated). Status: current."},
        {"item_type": "method", "labelled": True,
         "text_md": "- [method] **Always pin versions** — w. Status: current."},
    ]
    assert payload["item_types"] == ["decision", "method"]
    assert all(i["text_md"] in payload["content_md"] for i in payload["items"])


def test_an_unlabelled_capture_sends_no_items_key(tmp_path):
    """A v1 payload stays byte-compatible with what an old server has always read."""
    cfg, marker_path, state = _pipeline(tmp_path, "## Decisions\n- **X** — y.\n")
    cfg = dataclasses.replace(cfg, prompt_version="v1")
    api = _FakeApi(201)
    _run(cfg, api, state, marker_path)
    [payload] = api.posted
    assert "items" not in payload and payload["item_types"] == []


def test_a_summary_with_more_items_than_the_server_stores_posts_un_itemized(tmp_path):
    """The server refuses more than 50 items with a 422, which the processor keeps as
    `error` and re-summarizes on every pass, forever. Over the bound the entry travels
    WITHOUT items — typed, and read whole like any pre-D13 entry — instead."""
    summary = _decisions(*(f"- [fact] **Finding {n}** — x." for n in range(51)))
    cfg, marker_path, state = _pipeline(tmp_path, summary)
    api = _FakeApi(201)
    _run(cfg, api, state, marker_path)
    [payload] = api.posted
    assert "items" not in payload and payload["item_types"] == ["fact"]
    assert processor.MAX_POSTED_ITEMS == 50
