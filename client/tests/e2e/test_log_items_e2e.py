"""`loom log` reads items across the REAL boundary (D13, slice D).

A live ``uvicorn server.app:app`` on a temp ``LOOM_DB`` (the same boundary as
``test_phase2_e2e.py``), entries posted through the real HTTP API — one itemized as the
processor posts it, one un-itemized as a pre-D13 capture — and the real ``loom`` CLI as a
subprocess. `test_cli_log.py` pins the rendering byte for byte against a stub; this pins
that the client asks the real ``/items`` the question the stub answers (the page's ids,
the same ``type``/``q``) and that the server's answer renders.

Every expected value is a literal.
"""
from __future__ import annotations

import hashlib
import json

import pytest
import requests

from test_phase2_e2e import (  # the same real boundary and helpers, not a copy of them
    Server,
    _adduser,
    _free_port,
    _loom,
    _register_project,
)

pytestmark = pytest.mark.e2e

PROJECT = "phase2-e2e"          # the name the server derives from test_phase2_e2e's remote
REMOTE = "github.com/example-group/phase2-e2e"

ITEMIZED_BODY = (
    "## Context\nQueue storage for the webhook sender.\n"
    "## Decisions\n"
    "- [decision] **Use SQLite with WAL for the queue** — concurrent readers. "
    "Evidence: queue.py.\n"
    "- [method] **Never vacuum during a flush** — it blocks writers.\n"
    "  - Evidence: flush.py, see the retro.\n"
    "- [task] **Benchmark checkpoint sizes** — owner Ana, next sprint.\n"
)
ITEMIZED = ("---\ntype: decision-log-entry\ntitle: \"Use SQLite with WAL for the queue\"\n"
            "status: current\nitem_types: [\"decision\", \"method\", \"task\"]\n---\n"
            + ITEMIZED_BODY)
ITEMS = [
    {"item_type": "decision", "labelled": True,
     "text_md": "- [decision] **Use SQLite with WAL for the queue** — concurrent readers. "
                "Evidence: queue.py."},
    {"item_type": "method", "labelled": True,
     "text_md": "- [method] **Never vacuum during a flush** — it blocks writers.\n"
                "  - Evidence: flush.py, see the retro."},
    {"item_type": "task", "labelled": True,
     "text_md": "- [task] **Benchmark checkpoint sizes** — owner Ana, next sprint."},
]
UNITEMIZED = ("## Decisions\n- [method] **Always run make contract after CLI changes** — "
              "the contract drifts otherwise.\n")


def _post(url: str, token: str, sid: str, content: str, **extra) -> int:
    body = {"project_remote": REMOTE, "session_id": sid, "content_md": content,
            "content_hash": hashlib.sha256(f"{sid}|{content}".encode()).hexdigest(),
            "model": "sonnet", "prompt_version": "v3", **extra}
    resp = requests.post(f"{url}/api/entries", json=body,
                         headers={"Authorization": f"Bearer {token}"}, timeout=10)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture
def logbook(tmp_path):
    db = tmp_path / "server" / "loom.db"
    db.parent.mkdir(parents=True)
    srv = Server(db, _free_port())
    try:
        token = _adduser(db, "reader")
        _register_project(srv.url, token)
        old = _post(srv.url, token, "s-old", UNITEMIZED, item_types=["method"])
        new = _post(srv.url, token, "s-new", ITEMIZED,
                    item_types=["decision", "method", "task"], items=ITEMS)
        home = tmp_path / "loom_home"
        home.mkdir()
        (home / "config.json").write_text(json.dumps(
            {"server_url": srv.url, "token": token}), encoding="utf-8")
        yield home, old, new
    finally:
        srv.terminate()


def _log(home, *args: str) -> str:
    cp = _loom(["log", PROJECT, "--no-digest", *args], home)
    assert cp.returncode == 0, cp.stderr
    assert cp.stderr == "", "the real server answers /items: nothing to explain"
    return cp.stdout


def test_default_view_prints_the_real_item_ids_in_place(logbook):
    home, old, new = logbook
    out = _log(home)
    assert (f"## Decisions\n"
            f"{new}.1  - [decision] **Use SQLite with WAL for the queue** — concurrent "
            f"readers. Evidence: queue.py.\n"
            f"{new}.2  - [method] **Never vacuum during a flush** — it blocks writers.\n"
            f"  - Evidence: flush.py, see the retro.\n"
            f"{new}.3  - [task] **Benchmark checkpoint sizes** — owner Ana, next sprint.\n"
            ) in out
    assert UNITEMIZED in out and f"{old}.1" not in out, "the pre-D13 entry prints whole"


def test_type_narrows_the_itemized_entry_to_its_matching_item(logbook):
    home, old, new = logbook
    out = _log(home, "--type", "method")
    assert ("\"Use SQLite with WAL for the queue\"  ·  1 of 3 items\n\n"
            f"{new}.2  - [method] **Never vacuum during a flush** — it blocks writers.\n"
            "  - Evidence: flush.py, see the retro.\n\n") in out
    assert "Benchmark checkpoint sizes" not in out
    assert UNITEMIZED in out, "an un-itemized entry carrying the label prints whole"


def test_grep_prints_the_matching_item_with_the_servers_snippet(logbook):
    home, _old, new = logbook
    out = _log(home, "--grep", "vacuum")
    head = "\"Use SQLite with WAL for the queue\"  ·  1 of 3 items\n\n"
    assert head in out
    # The snippet is the server's (FTS5 `snippet()`, its window and `**` markers); what
    # the client owes is to print it, on one line, right before the item it belongs to.
    snippet, item = out.split(head, 1)[1].split("\n")[:2]
    assert snippet.startswith("match: ") and "**vacuum**" in snippet, snippet
    assert item == f"{new}.2  - [method] **Never vacuum during a flush** — it blocks writers."
    assert "Benchmark checkpoint sizes" not in out


def test_whole_entries_prints_the_entries_without_ids(logbook):
    home, _old, new = logbook
    out = _log(home, "--whole-entries")
    assert ITEMIZED_BODY in out and f"{new}.1" not in out
