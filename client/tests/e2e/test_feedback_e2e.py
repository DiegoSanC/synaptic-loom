"""Item feedback and team stats across the REAL boundary (slice F of the 2026-09-23 plan).

The same logbook as ``test_log_items_e2e.py`` — a live uvicorn, one itemized entry, one
pre-D13 entry — and the real ``loom`` CLI as a subprocess: the agent reads with
``--source skill``, files feedback on an item id ``loom log`` printed, and ``loom stats``
reports it as a team total.

Every expected value is a literal.
"""
from __future__ import annotations

import pytest

from test_log_items_e2e import PROJECT, logbook  # noqa: F401  (the fixture, not a copy)
from test_phase2_e2e import _loom

pytestmark = pytest.mark.e2e


def _cli(home, *args: str):
    return _loom(list(args), home)


def test_feedback_on_a_printed_item_id_is_recorded_once_and_counted(logbook):  # noqa: F811
    home, old, new = logbook
    read = _cli(home, "log", PROJECT, "--source", "skill", "--no-digest")
    assert read.returncode == 0 and f"{new}.2  - [method]" in read.stdout

    first = _cli(home, "feedback", PROJECT, "--item", f"{new}.2", "--verdict", "useful",
                 "--note", "kept the flush from blocking writers")
    again = _cli(home, "feedback", PROJECT, "--item", f"{new}.2", "--verdict", "useful",
                 "--note", "kept the flush from blocking writers")

    assert (first.returncode, first.stdout) == (0, f"Feedback recorded: {new}.2 useful\n")
    assert (again.returncode, again.stdout) == (0, f"Already recorded: {new}.2 useful\n")

    stats = _cli(home, "stats")
    assert stats.returncode == 0, stats.stderr
    assert stats.stdout == (
        "Team totals, all projects, since the start (never per person)\n"
        "Recalls:   skill 1 · cli 0\n"
        "Feedback:  useful 1 · not_applicable 0 · stale 0\n"
        "Most useful items:\n"
        f"  {new}.2  {PROJECT} · method · useful to 1 person\n"
    )


def test_an_unknown_item_or_an_unitemized_entry_exits_1_naming_it(logbook):  # noqa: F811
    home, old, new = logbook
    for item in (f"{new}.9", f"{old}.1"):
        cp = _cli(home, "feedback", PROJECT, "--item", item, "--verdict", "stale",
                  "--note", "no longer true")
        assert cp.returncode == 1, cp.stdout
        assert item in cp.stderr and "item_not_found" in cp.stderr
