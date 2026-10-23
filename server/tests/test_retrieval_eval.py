"""The retrieval benchmark (`test/retrieval_eval/`, RT.1's measure — DESIGN §12.3).

Three things are asserted here, each against something other than the search itself:

- the two metrics, on worked examples whose values are computed by hand;
- the benchmark's own inputs: closed categories, judged items that exist in the corpus;
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
# Metrics: worked examples                                                     #
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


# --------------------------------------------------------------------------- #
# The benchmark's inputs                                                       #
# --------------------------------------------------------------------------- #
def test_validate_names_every_problem_with_a_query():
    known = {"e01.1", "e01.2"}
    queries = [
        bench.Query("q1", "exact", "need", "terms", ("e01.1",)),
        bench.Query("q1", "exact", "need", "terms", ("e01.2",)),     # duplicate id
        bench.Query("q2", "vibes", "need", "terms", ("e01.1",)),     # unknown category
        bench.Query("q3", "exact", "need", "terms", ("e09.9",)),     # no such item
        bench.Query("q4", "exact", "need", "terms", ()),             # nothing judged
        bench.Query("q5", "exact", "need", "  ", ("e01.1",)),        # no terms
    ]
    problems = bench.validate(queries, known)
    assert len(problems) == 5, problems
    for fragment in ("duplicate", "vibes", "e09.9", "q4", "q5"):
        assert any(fragment in p for p in problems), (fragment, problems)
    assert bench.validate(queries[:1], known) == []


def test_every_category_is_exercised(report):
    counts = {c: 0 for c in bench.CATEGORIES}
    for outcome in report.outcomes:
        counts[outcome.query.category] += 1
    assert all(n >= 3 for n in counts.values()), counts


def test_every_corpus_bullet_is_a_labelled_item(report):
    # The corpus is written in summarizer-v4 form: an unlabelled bullet would be typed by
    # the processor's fallback and stand for an output v4 does not produce.
    assert report.items > report.entries > 0
    assert report.unlabelled == 0


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
