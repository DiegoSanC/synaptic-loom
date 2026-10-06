"""`loom feedback` and `loom stats` (slice F of the 2026-09-23 plan; F2).

`loom feedback --item 17.2 --verdict useful|not_applicable|stale --note "<one line>"`
files one verdict on one item of the cwd's project (or the named one): the id is the
one `loom log` printed. An unknown id exits 1 naming it; the same feedback twice is not
an error; a server that predates feedback says so instead of pretending. `loom stats`
prints the team's totals — never a per-person figure.

Every expected value is a literal.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from loom import cli
from test_cli_log import _json, _project, _projects_by_cwd, _setup

ARGS = ["feedback", "--item", "17.2", "--verdict", "useful", "--note", "saved a day"]


def _feedback_server(env, response, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(json.loads(request.data))
        return response
    env.httpserver.expect_request("/api/feedback", method="POST").respond_with_handler(handler)


def test_feedback_posts_the_item_of_the_cwds_project(env, monkeypatch, capsys):
    _setup(env, monkeypatch, env.repo)
    _projects_by_cwd(env, _project())
    seen = []
    _feedback_server(env, _json({"id": 1}, status=201), seen)

    rc = cli.main(ARGS)

    out = capsys.readouterr().out
    assert rc == 0
    assert seen == [{"project_id": 5, "item": "17.2", "verdict": "useful",
                     "note": "saved a day"}]
    assert out == "Feedback recorded: 17.2 useful\n"


def test_feedback_by_project_name_and_a_note_that_starts_with_a_dash(env, monkeypatch, capsys):
    _setup(env, monkeypatch)
    env.httpserver.expect_request("/api/projects", method="GET").respond_with_json(
        {"projects": [_project(2, "beta", "github.com/org/b")]})
    seen = []
    _feedback_server(env, _json({"id": 3}, status=201), seen)

    rc = cli.main(["feedback", "beta", "--item", "4.1", "--verdict", "stale",
                   "--note", "-- superseded by the new runner"])

    assert rc == 0
    assert seen == [{"project_id": 2, "item": "4.1", "verdict": "stale",
                     "note": "-- superseded by the new runner"}]


def test_an_unknown_item_exits_1_naming_it(env, monkeypatch, capsys):
    _setup(env, monkeypatch, env.repo)
    _projects_by_cwd(env, _project())
    _feedback_server(env, _json({"error": "item_not_found",
                                 "detail": "No item 17.2: the entry does not exist or has no items"},
                                status=404))

    rc = cli.main(ARGS)

    err = capsys.readouterr().err
    assert rc == 1
    assert "17.2" in err and "item_not_found" in err


def test_the_same_feedback_twice_is_not_an_error(env, monkeypatch, capsys):
    _setup(env, monkeypatch, env.repo)
    _projects_by_cwd(env, _project())
    _feedback_server(env, _json({"error": "duplicate_feedback",
                                 "detail": "This feedback is already recorded"}, status=409))

    rc = cli.main(ARGS)

    assert rc == 0
    assert capsys.readouterr().out == "Already recorded: 17.2 useful\n"


def test_a_server_without_feedback_says_so(env, monkeypatch, capsys):
    _setup(env, monkeypatch, env.repo)
    _projects_by_cwd(env, _project())
    # What a pre-slice-F Loom server really answers for an unknown route (app.py's 404
    # handler), not FastAPI's bare default.
    _feedback_server(env, _json({"error": "not_found", "detail": "Not Found"}, status=404))

    rc = cli.main(ARGS)

    assert rc == 1
    assert "does not support feedback yet" in capsys.readouterr().err


def test_a_refused_verdict_is_a_clean_exit_1(env, monkeypatch, capsys):
    _setup(env, monkeypatch, env.repo)
    _projects_by_cwd(env, _project())
    _feedback_server(env, _json({"error": "validation_error",
                                 "detail": "body.verdict: Value error, unknown verdict; "
                                           "allowed: useful, not_applicable, stale"},
                                status=422))

    rc = cli.main(["feedback", "--item", "17.2", "--verdict", "great", "--note", "x"])

    err = capsys.readouterr().err
    assert rc == 1 and "useful, not_applicable, stale" in err and "Traceback" not in err


def test_feedback_requires_item_verdict_and_note(env, monkeypatch, capsys):
    _setup(env, monkeypatch, env.repo)
    for missing in ("--item", "--verdict", "--note"):
        argv = list(ARGS)
        at = argv.index(missing)
        del argv[at:at + 2]
        try:
            cli.main(argv)
        except SystemExit as exc:
            assert exc.code == 2, missing
        else:
            raise AssertionError(f"{missing} must be required")
    assert env.httpserver.log == []


# --- loom stats --------------------------------------------------------------------------
STATS = {"since": None, "recalls": {"skill": 2, "cli": 1},
         "feedback": {"useful": 3, "not_applicable": 0, "stale": 1},
         "top_useful": [{"item": "1.2", "project": "queue", "item_type": "method", "useful": 2},
                        {"item": "3.1", "project": "ci", "item_type": "fact", "useful": 1}]}


def test_stats_prints_the_team_totals(env, monkeypatch, capsys):
    _setup(env, monkeypatch)
    env.httpserver.expect_request("/api/stats", method="GET").respond_with_json(STATS)

    assert cli.main(["stats"]) == 0

    assert capsys.readouterr().out == (
        "Team totals, all projects, since the start (never per person)\n"
        "Recalls:   skill 2 · cli 1\n"
        "Feedback:  useful 3 · not_applicable 0 · stale 1\n"
        "Most useful items:\n"
        "  1.2  queue · method · useful to 2 people\n"
        "  3.1  ci · fact · useful to 1 person\n"
    )


def test_stats_since_a_number_of_days_sends_that_instant(env, monkeypatch, capsys):
    _setup(env, monkeypatch)
    seen = []

    def handler(request):
        seen.append(request.args.get("since"))
        return _json(dict(STATS, since="2026-09-17T10:00:00Z", top_useful=[]))

    env.httpserver.expect_request("/api/stats", method="GET").respond_with_handler(handler)
    before = datetime.now(timezone.utc)

    assert cli.main(["stats", "--since", "7d"]) == 0

    sent = datetime.strptime(seen[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert abs(sent - (before - timedelta(days=7))) < timedelta(minutes=1)
    out = capsys.readouterr().out
    assert out.startswith("Team totals, all projects, since 2026-09-17T10:00:00Z")
    assert out.endswith("Most useful items: none yet\n")


@pytest.mark.parametrize("since", ["last tuesday", "\u00b2d"])
def test_stats_passes_anything_but_a_span_through_and_reports_a_refusal(
        since, env, monkeypatch, capsys):
    _setup(env, monkeypatch)
    seen = []

    def handler(request):
        seen.append(request.args.get("since"))
        return _json({"error": "invalid_since", "detail": "'since' must be an ISO-8601 "
                                                          "date or time"}, status=422)

    env.httpserver.expect_request("/api/stats", method="GET").respond_with_handler(handler)

    assert cli.main(["stats", "--since", since]) == 1

    assert seen == [since], "not an ASCII `<n>d`/`<n>h` span: the server decides"
    assert "invalid_since" in capsys.readouterr().err


@pytest.mark.parametrize("since", ["1000000d", "99999999999h"])
def test_stats_since_that_is_not_a_usable_span_is_a_usage_error(since, env, monkeypatch,
                                                                capsys):
    _setup(env, monkeypatch)

    assert cli.main(["stats", "--since", since]) == 2

    captured = capsys.readouterr()
    assert "--since" in captured.err and "Traceback" not in captured.err
    assert env.httpserver.log == []


def test_stats_with_an_unexpected_body_is_a_clean_exit_1(env, monkeypatch, capsys):
    _setup(env, monkeypatch)
    env.httpserver.expect_request("/api/stats", method="GET").respond_with_json(
        {"recalls": [], "feedback": {}, "top_useful": []})

    assert cli.main(["stats"]) == 1

    assert "did not return the expected totals" in capsys.readouterr().err
