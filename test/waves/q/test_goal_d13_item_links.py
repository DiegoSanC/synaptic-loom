"""FROZEN GOAL — D13 item links and item-level curation, slice E (wave q). Authored
2026-09-24 from the plan `docs/plans/2026-09-23-fix-label-grammar-fidelity-plan.md`
(slice E, criteria E1–E5), BEFORE the implementation. READ-ONLY to implementers; later
work extends it by adding cases.

Real boundary: a REAL `uvicorn server.app:app` subprocess over a temp LOOM_DB, real
`server.admin adduser` (two users: the author of a link and someone else), and the real
HTTP API for every write and read. The curation leg runs the REAL `loom curate`, `loom
log` and `loom unlink` against that server, with the claude CLI replaced by a SMART
replay stub that speaks the grammars below (the one seam this wave does not own).

The item-level grammars this wave fixes (additive to t-curation-watermark, decision E5):

- stdin, an ITEMIZED new entry: its `### ENTRY <id>` block lists the entry's items, one
  line each, `[<entry>.<n>] [<item_type>] <item text on one line>`; each candidate is
  one `- CANDIDATE <id>: ` line holding that candidate's CURRENT items, each as
  `[<entry>.<n>] [<item_type>] <preview>`. An un-itemized entry keeps the frozen entry
  form byte for byte (waves j, k and gate a).
- verdicts, an itemized entry: one line per ITEM, `<e>.<n>: keep`,
  `<e>.<n>: supersedes:<e>.<n>[,<e>.<n>...]`, `<e>.<n>: duplicates:<e>.<n>`, with the
  optional ` -- <rationale>`. Every id after the colon is an item of a candidate of
  the same block; every presented item gets exactly one verdict.

Server routes (API additions under D13, DESIGN §6.2):
`POST /api/item-links {from, to, kind, rationale}` → 201 {id} · 404 item_not_found ·
422 validation_error (shape, kind, another project, same entry, older → newer) ·
409 duplicate_link / contradictory_link among ACTIVE links. The author is the caller.
`POST /api/item-links/{id}/revoke {reason}` → 200 · 409 already_revoked · 404
link_not_found · 422 (a reason is one non-empty line). A revoked link stays as history
with who revoked it and why, and stops counting.

Every expected value below is a literal written by hand.

Kill-targets: an item link that changes an ENTRY's status (it would collapse the
siblings, the #6 bug this slice exists for); an item link posted to `/api/links` (a
permanent whole-entry link); the author taken from the body; a revoke that deletes
history, or that records the creator instead of the revoker; a revoked link that still
counts, or that blocks re-asserting it; qualified verdicts trusted without pool
validation; an itemized entry answered in entry form; a batch whose un-itemized
entries stop getting entry links.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _goalkit as kit  # noqa: E402

VENV_PY = str(kit.REPO_ROOT / ".venv" / "bin" / "python")
FRONTMATTER = "---\ntype: decision-log-entry\ntitle: \"{title}\"\nstatus: current\n---\n\n"

# An older entry with TWO items: the curation leg reverses the first and must leave the
# second alone.
OLD_ITEMS = [
    {"item_type": "decision", "labelled": True,
     "text_md": "- [decision] **Use REST for the sync API** — simplicity for the first "
                "clients. Evidence: api.md."},
    {"item_type": "method", "labelled": True,
     "text_md": "- [method] **Retry sync calls three times with backoff** — the office "
                "link drops. Evidence: retry.py."},
]
OLD_MD = (FRONTMATTER.format(title="Use REST for the sync API")
          + "## Context\nSync API and its retries.\n## Decisions\n"
          + "".join(i["text_md"] + "\n" for i in OLD_ITEMS))
NEW_ITEMS = [
    {"item_type": "decision", "labelled": True,
     "text_md": "- [decision] **Switch the sync API to gRPC** — reverses the earlier REST "
                "decision, streaming needs it. Evidence: api.md."},
]
NEW_MD = (FRONTMATTER.format(title="Switch the sync API to gRPC")
          + "## Context\nSync redesign.\n## Decisions\n"
          + "".join(i["text_md"] + "\n" for i in NEW_ITEMS))
# A new entry with two items, for the "every presented item gets a verdict" case.
TWO_NEW_ITEMS = NEW_ITEMS + [
    {"item_type": "task", "labelled": True,
     "text_md": "- [task] **Publish the gRPC schema** — owner Ana. Evidence: #31."},
]
TWO_NEW_MD = (FRONTMATTER.format(title="Switch the sync API to gRPC")
              + "## Context\nSync redesign.\n## Decisions\n"
              + "".join(i["text_md"] + "\n" for i in TWO_NEW_ITEMS))
# An un-itemized pair (a v1-style capture) on another subject, for the mixed batch.
PLAIN_OLD = ("## Context\nReports.\n## Decisions\n"
             "- **Keep the reports in Postgres** — one database to run.\n")
PLAIN_NEW = ("## Context\nReports again.\n## Decisions\n"
             "- **Move the reports to DuckDB** — reverses the Postgres choice, the "
             "queries are analytical.\n")


# ---------------------------------------------------------------------------- #
# Accessory: the real server, two users                                         #
# ---------------------------------------------------------------------------- #
def _adduser(db: Path, name: str) -> str:
    cp = subprocess.run(
        [VENV_PY, "-m", "server.admin", "adduser", "--name", name,
         "--email", f"{name}@goal.test"],
        capture_output=True, text=True, cwd=str(kit.REPO_ROOT),
        env={"LOOM_DB": str(db), "PATH": "/usr/bin:/bin"}, check=True)
    return kit.parse_admin_token(cp.stdout)


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("d13e")
    db = tmp / "loom.db"
    port = kit.find_free_port()
    proc = kit.start_server(db, port, VENV_PY)
    base = f"http://127.0.0.1:{port}"
    try:
        kit.wait_health(base)
        yield {"base": base, "db": db, "tok": _adduser(db, "goalq"),
               "tok2": _adduser(db, "goalq2")}
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _h(srv, who: str = "tok") -> dict:
    return {"Authorization": f"Bearer {srv[who]}"}


def _register(srv, name: str) -> int:
    r = requests.post(f"{srv['base']}/api/projects",
                      json={"remote_url": f"git@github.com:goalq/{name}.git"},
                      headers=_h(srv), timeout=10)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _post(srv, name: str, content: str, sid: str, items: list | None = None) -> int:
    body = {"project_remote": f"github.com/goalq/{name}", "session_id": sid,
            "content_md": content,
            "content_hash": hashlib.sha256(f"{name}|{sid}|{content}".encode()).hexdigest(),
            "model": "sonnet", "prompt_version": "v3" if items else "v1"}
    if items is not None:
        body["items"] = items
    r = requests.post(f"{srv['base']}/api/entries", json=body, headers=_h(srv),
                      timeout=10)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _link(srv, frm: str, to: str, kind: str = "supersedes", who: str = "tok",
          **extra) -> requests.Response:
    return requests.post(f"{srv['base']}/api/item-links",
                         json={"from": frm, "to": to, "kind": kind, **extra},
                         headers=_h(srv, who), timeout=10)


def _revoke(srv, link_id, reason, who: str = "tok") -> requests.Response:
    return requests.post(f"{srv['base']}/api/item-links/{link_id}/revoke",
                         json={"reason": reason}, headers=_h(srv, who), timeout=10)


def _items(srv, pid: int, *entry_ids: int) -> dict[str, dict]:
    r = requests.get(f"{srv['base']}/api/projects/{pid}/items",
                     params={"entry_ids": ",".join(map(str, entry_ids))},
                     headers=_h(srv), timeout=10)
    assert r.status_code == 200, r.text
    return {f"{i['entry_id']}.{i['ordinal']}": i for i in r.json()["items"]}


def _entries(srv, pid: int) -> dict[int, dict]:
    r = requests.get(f"{srv['base']}/api/projects/{pid}/entries",
                     params={"limit": 100}, headers=_h(srv), timeout=10)
    assert r.status_code == 200, r.text
    return {e["id"]: e for e in r.json()["entries"]}


def _rows(db: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _pair(srv, name: str, new_items=NEW_ITEMS, new_md=NEW_MD) -> tuple[int, int, int]:
    pid = _register(srv, name)
    old = _post(srv, name, OLD_MD, f"{name}-old", OLD_ITEMS)
    new = _post(srv, name, new_md, f"{name}-new", new_items)
    return pid, old, new


# ---------------------------------------------------------------------------- #
# E1 — POST /api/item-links                                                     #
# ---------------------------------------------------------------------------- #
def test_e1_a_link_names_its_author_and_marks_only_its_item(srv):
    pid, old, new = _pair(srv, "e1ok")
    r = _link(srv, f"{new}.1", f"{old}.1", rationale="gRPC replaces REST")
    assert r.status_code == 201 and isinstance(r.json().get("id"), int), r.text
    link_id = r.json()["id"]

    items = _items(srv, pid, old, new)
    target, sibling = items[f"{old}.1"], items[f"{old}.2"]
    assert target["status"] == "superseded", target
    [incoming] = target["links_in"]
    assert (incoming["id"], incoming["from"], incoming["kind"], incoming["rationale"],
            incoming["created_by"], incoming["revoked_at"]) == (
        link_id, f"{new}.1", "supersedes", "gRPC replaces REST", "goalq", None)
    assert sibling["status"] == "current" and sibling["links_in"] == [], (
        "a link hides ONE item, never its siblings (#6)"
    )
    [outgoing] = items[f"{new}.1"]["links_out"]
    assert (outgoing["to"], outgoing["kind"]) == (f"{old}.1", "supersedes")


def test_e1_the_author_is_the_caller_never_the_body(srv):
    pid, old, new = _pair(srv, "e1author")
    r = _link(srv, f"{new}.1", f"{old}.1", who="tok2", created_by="goalq")
    assert r.status_code == 201, r.text
    [incoming] = _items(srv, pid, old)[f"{old}.1"]["links_in"]
    assert incoming["created_by"] == "goalq2"


def test_e1_duplicate_and_contradictory_active_links_are_409(srv):
    _pid, old, new = _pair(srv, "e1conflict")
    assert _link(srv, f"{new}.1", f"{old}.1").status_code == 201
    dup = _link(srv, f"{new}.1", f"{old}.1")
    assert dup.status_code == 409 and dup.json()["error"] == "duplicate_link", dup.text
    other = _link(srv, f"{new}.1", f"{old}.1", kind="duplicates")
    assert other.status_code == 409 and other.json()["error"] == "contradictory_link", (
        other.text
    )


def test_e1_unknown_items_are_404(srv):
    pid = _register(srv, "e1missing")
    old = _post(srv, "e1missing", OLD_MD, "m-old", OLD_ITEMS)
    plain = _post(srv, "e1missing", PLAIN_OLD, "m-plain")
    new = _post(srv, "e1missing", NEW_MD, "m-new", NEW_ITEMS)
    for frm, to in ((f"{new}.1", f"{old}.9"), (f"{new}.1", f"{plain}.1"),
                    (f"{new}.1", "999999.1"), ("999999.1", f"{old}.1")):
        r = _link(srv, frm, to)
        assert r.status_code == 404 and r.json()["error"] == "item_not_found", (
            frm, to, r.text)
    assert _items(srv, pid, old)[f"{old}.1"]["links_in"] == []


def test_e1_links_that_would_break_the_logbook_are_422(srv):
    pid, old, new = _pair(srv, "e1bad")
    other_pid = _register(srv, "e1badother")
    foreign = _post(srv, "e1badother", NEW_MD, "bad-foreign", NEW_ITEMS)
    cases = {
        "another project": (f"{foreign}.1", f"{old}.1", "supersedes"),
        "same entry": (f"{old}.2", f"{old}.1", "supersedes"),
        "older to newer": (f"{old}.1", f"{new}.1", "supersedes"),
        "unknown kind": (f"{new}.1", f"{old}.1", "renames"),
        "not an item id": (f"{new}", f"{old}.1", "supersedes"),
        "zero ordinal": (f"{new}.0", f"{old}.1", "supersedes"),
    }
    for case, (frm, to, kind) in cases.items():
        r = _link(srv, frm, to, kind=kind)
        assert r.status_code == 422, (case, r.status_code, r.text)
    items = _items(srv, pid, old, new)
    assert all(i["links_in"] == [] and i["links_out"] == [] for i in items.values())
    assert _items(srv, other_pid, foreign)[f"{foreign}.1"]["links_out"] == []


def test_e1_concurrent_contradictory_posts_leave_one_link(srv):
    _pid, old, new = _pair(srv, "e1race")
    codes: list[int] = []
    barrier = threading.Barrier(2)

    def post(kind: str) -> None:
        barrier.wait()
        codes.append(_link(srv, f"{new}.1", f"{old}.1", kind=kind).status_code)

    threads = [threading.Thread(target=post, args=(k,))
               for k in ("supersedes", "duplicates")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(codes) == [201, 409], codes
    active = _rows(srv["db"], "SELECT count(*) FROM item_links l "
                              "JOIN entry_items i ON i.id = l.to_item "
                              f"WHERE i.entry_id = {old} AND l.revoked_at IS NULL")
    assert active == [(1,)]


# ---------------------------------------------------------------------------- #
# E2 — revoke                                                                   #
# ---------------------------------------------------------------------------- #
def test_e2_revoke_keeps_history_names_the_revoker_and_stops_counting(srv):
    pid, old, new = _pair(srv, "e2")
    link_id = _link(srv, f"{new}.1", f"{old}.1").json()["id"]

    r = _revoke(srv, link_id, "REST stays for batch clients", who="tok2")
    assert r.status_code == 200, r.text
    target = _items(srv, pid, old)[f"{old}.1"]
    assert target["status"] == "current", "a revoked link no longer counts"
    [history] = target["links_in"]
    assert (history["id"], history["created_by"], history["revoked_by"],
            history["revoke_reason"]) == (
        link_id, "goalq", "goalq2", "REST stays for batch clients"), history
    assert isinstance(history["revoked_at"], str) and history["revoked_at"]

    again = _revoke(srv, link_id, "twice")
    assert again.status_code == 409 and again.json()["error"] == "already_revoked"
    assert _revoke(srv, 999999, "nope").status_code == 404
    for bad in ("", "   ", "two\nlines"):
        assert _revoke(srv, link_id, bad).status_code == 422, repr(bad)

    reasserted = _link(srv, f"{new}.1", f"{old}.1")
    assert reasserted.status_code == 201 and reasserted.json()["id"] != link_id
    target = _items(srv, pid, old)[f"{old}.1"]
    assert target["status"] == "superseded"
    assert [(link["id"], link["revoked_at"] is None) for link in target["links_in"]] == [
        (link_id, False), (reasserted.json()["id"], True)]


# ---------------------------------------------------------------------------- #
# E3 — item links never touch an entry's status                                 #
# ---------------------------------------------------------------------------- #
def test_e3_item_links_leave_entry_status_and_entry_links_alone(srv):
    pid, old, new = _pair(srv, "e3")
    before = _rows(srv["db"], "SELECT * FROM entry_links ORDER BY id")
    assert _link(srv, f"{new}.1", f"{old}.1").status_code == 201
    assert _link(srv, f"{new}.1", f"{old}.2", kind="supersedes").status_code == 201
    entries = _entries(srv, pid)
    for eid in (old, new):
        assert (entries[eid]["status"], entries[eid]["links_in"],
                entries[eid]["links_out"]) == ("current", [], []), entries[eid]
    assert _rows(srv["db"], "SELECT * FROM entry_links ORDER BY id") == before


# ---------------------------------------------------------------------------- #
# E4 / E5 — item-level curation through the real client                         #
# ---------------------------------------------------------------------------- #
STUB_SRC = """
import sys, re, pathlib
rec = pathlib.Path({rec!r}); rec.mkdir(parents=True, exist_ok=True)
n = len(list(rec.glob("call-*-stdin.txt")))
data = sys.stdin.read()
(rec / f"call-{{n}}-stdin.txt").write_text(data)
mode = {mode!r}
REVERSALS = {{"gRPC": "REST", "DuckDB": "Postgres"}}

def target(text, candidates):
    for new_word, old_word in REVERSALS.items():
        if new_word in text:
            return next((cid for cid, ctext in candidates if old_word in ctext), None)
    return None

out = []
for block in re.split(r"^### ENTRY ", data, flags=re.M)[1:]:
    eid = block.split("\\n", 1)[0].strip()
    units = re.findall(r"^\\[(\\d+\\.\\d+)\\] (.*)$", block, re.M)
    cand_lines = re.findall(r"^- CANDIDATE (\\d+): (.*)$", block, re.M)
    if not units:
        own = re.split(r"^- CANDIDATE ", block, flags=re.M)[0]
        t = target(own, cand_lines)
        out.append(f"{{eid}}: supersedes:{{t}} -- reversal" if t else f"{{eid}}: keep")
        continue
    if mode == "entry_form":
        out.append(f"{{eid}}: keep"); continue
    items = [pair for _cid, line in cand_lines
             for pair in re.findall(r"\\[(\\d+\\.\\d+)\\] (.*?)(?= \\[\\d+\\.\\d+\\] |$)", line)]
    for k, (uid, text) in enumerate(units):
        if mode == "missing_unit" and k > 0:
            continue
        if mode == "silent_on_empty" and not items:
            continue
        if mode == "outsider" and items:
            out.append(f"{{uid}}: supersedes:999999.1"); continue
        t = target(text, items)
        out.append(f"{{uid}}: supersedes:{{t}} -- reversal" if t else f"{{uid}}: keep")
print("\\n".join(out))
"""


def _mk_home(tmp: Path, srv, mode: str = "smart") -> tuple[Path, Path]:
    home = kit.loom_home(tmp)
    rec = tmp / "calls"
    stub = tmp / "bin" / "claude"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text("#!/usr/bin/env python3\n" + STUB_SRC.format(rec=str(rec), mode=mode),
                    encoding="utf-8")
    stub.chmod(0o755)
    kit.write_config(home, server_url=srv["base"], token=srv["tok"],
                     claude_bin=str(stub), scanner_engine="gitleaks", gitleaks_version=None)
    return home, rec


def _loom(home: Path, *argv: str) -> int:
    from loom import cli

    os.environ["LOOM_HOME"] = str(home)
    try:
        return cli.main(list(argv))
    finally:
        os.environ.pop("LOOM_HOME", None)


def _state(home: Path, name: str) -> dict:
    path = home / "curation_state.json"
    if not path.exists():
        return {}
    return (json.loads(path.read_text()).get("projects") or {}).get(
        f"github.com/goalq/{name}", {})


def test_e4_curation_links_the_reversed_item_only_and_unlink_restores_it(
        srv, tmp_path, capsys):
    pid, old, new = _pair(srv, "e4")
    home, rec = _mk_home(tmp_path, srv)

    assert _loom(home, "curate", "e4") == 0
    capsys.readouterr()

    stdin = (rec / "call-0-stdin.txt").read_text()
    assert f"\n[{new}.1] [decision] **Switch the sync API to gRPC**" in stdin, stdin
    [candidate] = [line for line in stdin.split("\n") if line.startswith("- CANDIDATE ")]
    assert candidate.startswith(f"- CANDIDATE {old}: [{old}.1] [decision] **Use REST"), (
        candidate)
    assert f" [{old}.2] [method] **Retry sync calls" in candidate
    assert "type: decision-log-entry" not in stdin, "items, never frontmatter"

    items = _items(srv, pid, old, new)
    assert items[f"{old}.1"]["status"] == "superseded"
    assert [link["from"] for link in items[f"{old}.1"]["links_in"]] == [f"{new}.1"]
    assert items[f"{old}.2"]["status"] == "current", "the sibling stays current"
    assert _rows(srv["db"], "SELECT count(*) FROM entry_links") == [(0,)], (
        "an item verdict never becomes a whole-entry link"
    )
    assert _entries(srv, pid)[old]["status"] == "current"
    assert _state(home, "e4")["watermark_id"] >= new

    link_id = items[f"{old}.1"]["links_in"][0]["id"]
    assert _loom(home, "log", "e4") == 0
    out = capsys.readouterr().out
    assert f"{old}.1 → replaced by {new}.1 on " in out and f"(link {link_id})" in out, out
    assert "**Use REST for the sync API** — simplicity" not in out, (
        "the replaced item prints as its note (the title in the frontmatter stays)"
    )
    assert "Retry sync calls three times with backoff" in out, (
        "the sibling item is still printed in full"
    )

    assert _loom(home, "unlink", str(link_id), "--reason", "REST stays for batch") == 0
    capsys.readouterr()
    target = _items(srv, pid, old)[f"{old}.1"]
    assert target["status"] == "current"
    assert target["links_in"][0]["revoke_reason"] == "REST stays for batch"
    assert _loom(home, "log", "e4") == 0
    assert "**Use REST for the sync API** — simplicity" in capsys.readouterr().out

    assert _loom(home, "curate", "e4") == 0
    assert len(list(rec.glob("call-*-stdin.txt"))) == 1, (
        "the watermark still guards reruns: no second LLM call"
    )


def test_e5_a_mixed_batch_links_items_and_entries_each_in_its_own_table(srv, tmp_path):
    pid = _register(srv, "e5mixed")
    old = _post(srv, "e5mixed", OLD_MD, "x-old", OLD_ITEMS)
    plain_old = _post(srv, "e5mixed", PLAIN_OLD, "x-plain-old")
    new = _post(srv, "e5mixed", NEW_MD, "x-new", NEW_ITEMS)
    plain_new = _post(srv, "e5mixed", PLAIN_NEW, "x-plain-new")
    home, _rec = _mk_home(tmp_path, srv)

    assert _loom(home, "curate", "e5mixed") == 0
    assert _rows(srv["db"], "SELECT from_entry, to_entry, kind FROM entry_links "
                            f"WHERE from_entry = {plain_new}") == [
        (plain_new, plain_old, "supersedes")], "the un-itemized reversal: an entry link"
    items = _items(srv, pid, old)
    assert [link["from"] for link in items[f"{old}.1"]["links_in"]] == [f"{new}.1"]
    assert items[f"{old}.2"]["status"] == "current"
    assert _entries(srv, pid)[old]["status"] == "current"


@pytest.mark.parametrize("mode", ["outsider", "entry_form", "missing_unit"])
def test_e4_hostile_item_verdicts_fail_closed(srv, tmp_path, mode, capsys):
    name = f"e4hostile{mode.replace('_', '')}"
    _pid, _old, _new = _pair(srv, name, new_items=TWO_NEW_ITEMS, new_md=TWO_NEW_MD)
    before = (_rows(srv["db"], "SELECT * FROM item_links ORDER BY id"),
              _rows(srv["db"], "SELECT * FROM entry_links ORDER BY id"))
    home, _rec = _mk_home(tmp_path, srv, mode)

    assert _loom(home, "curate", name) == 1, f"{mode}: fail closed"
    capsys.readouterr()
    after = (_rows(srv["db"], "SELECT * FROM item_links ORDER BY id"),
             _rows(srv["db"], "SELECT * FROM entry_links ORDER BY id"))
    assert after == before, f"{mode}: NOTHING may be posted"
    state = _state(home, name)
    assert (state.get("watermark_id", 0), state.get("attempts")) == (0, 1), state
    assert re.fullmatch(r"[a-z_]+", state.get("last_error") or ""), state


# ---------------------------------------------------------------------------- #
# E4, added 2026-09-25 — the item form of the empty-pool exemption.              #
# Sibling of wave j's test_ac_rt3_a_unit_with_no_candidates_needs_no_verdict_    #
# line: the same guard reads `item_pools`, so the same exemption must hold for   #
# an itemized entry whose block was offered no candidate items.                  #
# ---------------------------------------------------------------------------- #
def test_e4_items_with_no_candidate_items_need_no_verdict_line(srv, tmp_path, capsys):
    name = "e4emptyitempool"
    _pid, _old, new = _pair(srv, name, new_items=TWO_NEW_ITEMS, new_md=TWO_NEW_MD)
    home, rec = _mk_home(tmp_path, srv, "silent_on_empty")

    rc = _loom(home, "curate", name)
    capsys.readouterr()

    stdin = (rec / "call-0-stdin.txt").read_text()
    assert "(no candidates)" in stdin, (
        "the fixture is only meaningful if some block really is offered none"
    )
    assert rc == 0, "silence about an item with an empty pool must not fail the batch"
    state = _state(home, name)
    assert state["watermark_id"] >= new, "the watermark advances on success"
    assert not state.get("attempts"), "a success clears the attempt counter"

