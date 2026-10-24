"""The retrieval benchmark (`test/retrieval_eval/`, RT.1's measure — DESIGN §12.3).

What is asserted here, each against something other than the search itself:

- the metrics and the word comparison, on worked examples computed by hand;
- the benchmark's inputs: the corpus is well formed, and every query satisfies the
  definition of its category (each rule has a passing and a failing literal case);
- the control category stays at full recall, whatever the search becomes;
- `RESULTS.md` as a GOLDEN of the current search (AGENTS.md rule 11). A change to
  retrieval must regenerate it, so its effect on every judged query is in the diff.

The harness is loaded from its file because `test/` is not a package and is not on the
suite's paths; it drives the real routes in-process (see its docstring).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HARNESS = REPO / "test" / "retrieval_eval" / "run_eval.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("retrieval_run_eval", HARNESS)
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: its dataclasses resolve their module through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bench = _load_harness()


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    return bench.evaluate(tmp_path_factory.mktemp("retrieval_eval"))


# --------------------------------------------------------------------------- #
# Metrics and words: worked examples                                           #
# --------------------------------------------------------------------------- #
def test_recall_at_5_counts_relevant_items_among_the_first_five():
    ranked = ["a", "b", "c", "d", "e", "f"]
    assert bench.recall_at_k(ranked, ("b", "f"), 5) == 0.5   # f is sixth
    assert bench.recall_at_k(ranked, ("a", "e"), 5) == 1.0
    assert bench.recall_at_k(ranked, ("x",), 5) == 0.0
    assert bench.recall_at_k([], ("a",), 5) == 0.0


def test_reciprocal_rank_is_one_over_the_first_relevant_position():
    ranked = ["a", "b", "c"]
    assert bench.reciprocal_rank(ranked, ("c",)) == pytest.approx(1 / 3)
    assert bench.reciprocal_rank(ranked, ("c", "b")) == 0.5
    assert bench.reciprocal_rank(ranked, ("z",)) == 0.0
    assert bench.reciprocal_rank([], ("a",)) == 0.0
    eleven = [f"x{n}" for n in range(10)] + ["hit"]
    assert bench.first_relevant(eleven, ("hit",)) == 11
    assert bench.reciprocal_rank(eleven, ("hit",)) == pytest.approx(1 / 11)
    assert bench.first_relevant(eleven, ("miss",)) is None


def test_shown_items_reads_the_ids_loom_log_prints_in_order():
    printed = (
        "Automatically generated reference content …\n"
        "────\n"
        '"Refunds are new negative invoices"  ·  1 of 2 items\n'
        "\n"
        "match: …**refund**…\n"
        "16.1  - [decision] **Refunds are new negative invoices** — …\n"
        "────\n"
        "3.2  - [fact] **API tokens expire after 30 days** — …\n"
        "  12.4  not at the start of a line, so not an item\n"
    )
    keys = {(16, 1): "e16.1", (3, 2): "e03.2"}
    assert bench.shown_items(printed, keys) == ["e16.1", "e03.2"]
    with pytest.raises(bench.BenchmarkError):
        bench.shown_items("99.9  - [fact] **x**\n", keys)


def test_words_are_lowercased_without_accents_and_split_on_punctuation():
    assert bench.words("Fly.io's `pg_dump` 1.0-0 Migración") == {
        "fly", "io", "s", "pg", "dump", "1", "0", "migracion"}


# --------------------------------------------------------------------------- #
# The benchmark's inputs                                                       #
# --------------------------------------------------------------------------- #
ITEMS = {
    "a.1": bench.CorpusItem("a", "2026-01-01", "Schema migrations run at startup"),
    "a.2": bench.CorpusItem("a", "2026-01-01", "Workers hold one pool each"),
    "b.1": bench.CorpusItem("b", "2026-02-01", "Schema migrations run on deploy"),
    "c.1": bench.CorpusItem("c", "2026-03-01", "PDF rendering crashed in the Docker image"),
    "d.1": bench.CorpusItem("d", "2026-03-02", "Device tokens expire after 90 days"),
    "e.1": bench.CorpusItem("e", "2026-03-03", "PostgreSQL connection limit exhausted"),
    # An entry whose indexed text (frontmatter, Context) holds a word its item does not.
    "f.1": bench.CorpusItem("f", "2026-03-04", "Deploys run nightly",
                            entry_text="---\ntitle: schedule\n---\nDeploys run nightly"),
}


def _q(category, terms, relevant, distractors=()):
    return bench.Query("q", category, "need", terms, tuple(relevant), tuple(distractors))


@pytest.mark.parametrize("query, ok", [
    (_q("exact", "migrations startup", ["a.1"]), True),
    (_q("exact", "migration startup", ["a.1"]), False),
    (_q("morphology", "migration startup", ["a.1"]), True),
    (_q("morphology", "migrations startup", ["a.1"]), False),       # nothing differs
    (_q("morphology", "pool startup", ["a.1"]), False),             # pool: not a form
    (_q("split", "startup pool", ["a.1", "a.2"]), True),
    (_q("split", "migrations startup", ["a.1"]), False),            # a.1 holds both
    (_q("split", "startup deploy", ["a.1"]), False),                # deploy: not in a
    (_q("extra", "startup latency", ["a.1"]), True),
    (_q("extra", "startup workers", ["a.1"]), False),               # workers: in a.2
    (_q("extra", "pdf docker crash", ["c.1"]), False),              # crash: a form
    (_q("extra", "postgres connection limit", ["e.1"]), False),     # postgres: a form
    (_q("synonym", "boot sequence", ["a.1"]), True),
    (_q("synonym", "token TTL", ["d.1"]), False),                   # token: a form
    (_q("synonym", "boot schedule", ["f.1"]), False),               # in f's frontmatter
    (_q("extra", "deploy schedule", ["f.1"]), False),               # likewise
    (_q("stale", "schema migrations run", ["b.1"], ["a.1"]), True),
    (_q("stale", "schema migrations run", ["a.1"], ["b.1"]), False),  # distractor newer
    (_q("stale", "schema startup", ["b.1"], ["a.1"]), False),         # startup: not in b.1
    (_q("stale", "schema migrations", ["b.1"]), False),               # no distractor
    (_q("durable", "schema migrations run", ["a.1"], ["b.1"]), True),
    (_q("durable", "schema migrations run", ["b.1"], ["a.1"]), False),  # distractor older
    (_q("error", "migrations failed: exit 1", ["a.1"]), True),
    (_q("error", "schema migrations", ["a.1"]), False),             # that is exact
    (_q("exact", "migrations", ["a.1"], ["b.1"]), False),           # distractors: no
])
def test_each_category_rule_on_worked_examples(query, ok):
    problems = bench.validate([query], ITEMS)
    assert (problems == []) is ok, problems


def test_validate_names_every_generic_problem():
    queries = [
        bench.Query("q1", "exact", "need", "migrations", ("a.1",)),
        bench.Query("q1", "exact", "need", "migrations", ("a.1",)),   # duplicate id
        bench.Query("q2", "vibes", "need", "migrations", ("a.1",)),   # unknown category
        bench.Query("q3", "exact", "need", "migrations", ("e09.9",)),  # no such item
        bench.Query("q4", "exact", "need", "migrations", ()),         # nothing judged
        bench.Query("q5", "exact", "need", " - ", ("a.1",)),          # no words
        bench.Query("q6", "exact", "need", "migrations", ("a.1", "a.1")),  # named twice
    ]
    problems = bench.validate(queries, ITEMS)
    for fragment in ("q1: duplicate", "vibes", "e09.9", "q4:", "q5:", "q6:"):
        assert any(fragment in p for p in problems), (fragment, problems)
    assert bench.validate(queries[:1], ITEMS) == []


def test_check_corpus_catches_what_would_silently_skew_the_run():
    good = ("<!-- entry e01 2026-01-02 -->\n## Context\nx\n## Decisions\n"
            "- [fact] **a** — b. Evidence: c.\n")
    assert bench.check_corpus(good) == []
    # A mistyped "## Decisions": the splitter's fallback reads only the first section
    # with labelled bullets, so the second section's bullet silently stops being an item.
    bad = (good
           + "<!-- entry e01 2026-01-01 -->\n## Context\nx\n## Decision\n"
             "- [fact] **a** — b. Evidence: c.\n## Tasks\n- [task] **d** — e. Evidence: f.\n"
           + "<!--entry e03 2026-01-03 -->\n## Context\nx\n"
           + "<!-- entry e04 2026-01-04 -->\n## Context\nNothing decided.\n")
    problems = bench.check_corpus(bad)
    for fragment in ("duplicate entry key", "before the previous",
                     "labelled bullets 2, items 1", "4 entry markers but 3", "e04: no item"):
        assert any(fragment in p for p in problems), (fragment, problems)


def test_the_corpus_and_queries_satisfy_their_own_definitions():
    text = bench.CORPUS.read_text(encoding="utf-8")
    assert bench.check_corpus(text) == []
    items = bench.corpus_items(bench.load_corpus(text))
    assert bench.validate(bench.load_queries(), items) == []


def test_every_category_is_exercised(report):
    counts = {c: 0 for c in bench.CATEGORIES}
    for outcome in report.outcomes:
        counts[outcome.query.category] += 1
    assert all(n >= 3 for n in counts.values()), counts


# --------------------------------------------------------------------------- #
# Results                                                                      #
# --------------------------------------------------------------------------- #
def test_a_query_in_an_items_own_words_finds_it(report):
    # The control category: any retrieval change must keep these at full recall, or it
    # broke the case search already handled.
    exact = [o for o in report.outcomes if o.query.category == "exact"]
    assert exact
    for outcome in exact:
        assert outcome.recall == 1.0, (outcome.query.id, outcome.ranked)


def test_results_md_is_what_the_current_search_produces(report):
    expected = bench.render(report)
    actual = bench.RESULTS.read_text(encoding="utf-8")
    assert actual == expected, (
        "test/retrieval_eval/RESULTS.md does not match the current search. If the change "
        "to retrieval is intended, regenerate it and commit the diff:\n"
        "  .venv/bin/python test/retrieval_eval/run_eval.py "
        "--out test/retrieval_eval/RESULTS.md"
    )
