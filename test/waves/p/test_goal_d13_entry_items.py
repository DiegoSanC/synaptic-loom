"""FROZEN GOAL — D13 addressable items, slice C (wave p). Authored 2026-09-23 from the
plan `docs/plans/2026-09-23-fix-label-grammar-fidelity-plan.md` (slice C, criteria
C2, C3, C4, C7, and B2's "posted items" half), BEFORE the implementation. READ-ONLY to
implementers; later slices extend it by adding cases.

Real boundary: a REAL `uvicorn server.app:app` subprocess over a temp LOOM_DB, real
`server.admin adduser`, and the real HTTP API for every write and read. The capture
leg runs the REAL processor (`run_once`) against that same server, with the claude
CLI replayed from wave o's two recorded v3 goldens.

What an item is (D13): one top-level bullet of `## Decisions` in an entry captured
under a labelled prompt version, with its continuation lines, VERBATIM. Its public id
is `<entry_id>.<ordinal>`, ordinals 1..n in document order. The client sends the
items; the server checks them without understanding markdown — every `text_md` must be
a substring of `content_md` — and stores them in the entry's own transaction.

Every expected value below is a literal: the item texts of the capture leg were
copied by hand from wave o's golden files, not recomputed with the product's splitter.

Kill-targets: items outside the entry's transaction (an entry stored when its items
are rejected); an item whose text is not in the entry; a re-POST that rewrites the
first POST's items; `/entries` responses changed by the feature; `/items` that hides
the non-matching items (the reader could not tell "nothing matched" from "not
itemized"); a v3 capture that posts no items.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _goalkit as kit  # noqa: E402

VENV_PY = str(kit.REPO_ROOT / ".venv" / "bin" / "python")
WAVE_O = kit.REPO_ROOT / "test" / "waves" / "o" / "fixtures"
PROMPT_V3 = kit.REPO_ROOT / "prompts" / "summarizer_v3.md"
# The seven frozen entry fields plus the three t-entry-links fields: what every entry of
# a plain listing carried before this wave, and must keep carrying.
ENTRY_KEYS = {"id", "author", "session_id", "created_at", "covered_from", "covered_to",
              "content_md", "links_in", "links_out", "status"}
ITEM_KEYS = {"entry_id", "ordinal", "item_type", "labelled", "text_md", "match",
             "status", "links_in", "links_out"}

SUMMARY = (
    "## Context\nQueue storage for the webhook sender.\n"
    "## Decisions\n"
    "- [decision] **Use SQLite with WAL for the queue** — concurrent readers; Redis was "
    "dropped to avoid new infrastructure. Evidence: queue.py.\n"
    "- [method] **Never vacuum during a flush** — it blocks writers.\n"
    "  - Evidence: flush.py, see the retro.\n"
    "- [task] **Benchmark checkpoint sizes** — owner Ana, next sprint. Evidence: #12.\n"
    "## Open questions\n- Should the queue be sharded?\n"
)
ENTRY_MD = ("---\ntype: decision-log-entry\ntitle: \"Use SQLite with WAL for the queue\"\n"
            "status: current\n---\n\n" + SUMMARY)
ITEMS = [
    {"item_type": "decision", "labelled": True,
     "text_md": "- [decision] **Use SQLite with WAL for the queue** — concurrent readers; "
                "Redis was dropped to avoid new infrastructure. Evidence: queue.py."},
    {"item_type": "method", "labelled": True,
     "text_md": "- [method] **Never vacuum during a flush** — it blocks writers.\n"
                "  - Evidence: flush.py, see the retro."},
    {"item_type": "task", "labelled": True,
     "text_md": "- [task] **Benchmark checkpoint sizes** — owner Ana, next sprint. "
                "Evidence: #12."},
]

# Wave o's goldens, item by item, copied by hand (B2's "posted items" half).
GOLDEN_ITEMS = {
    "es_reversal": [
        ("decision", True,
         "- [decision] **Cachear en una tabla `stats_cache` en SQLite con TTL de 60 s, en "
         "`server/stats.py`** — se descartó la caché en memoria del proceso porque en Fly "
         "puede haber más de una máquina y cada una tendría su propia caché. Evidence: "
         "\"Mejor no en memoria: en Fly podemos tener más de una máquina y cada una "
         "tendría su propia caché.\""),
        ("task", True,
         "- [task] **Añadir índice por `project_id` a `stats_cache`** — asignado a Marta, "
         "la semana que viene, en otro PR. Evidence: \"El índice por project_id lo dejamos "
         "para otro PR; que lo haga Marta la semana que viene.\""),
    ],
    "en_rule_fact": [
        ("fact", True,
         "- [fact] **`setsid` is unavailable on macOS, so the e2e gate fails there** — the "
         "pilot host is WSL2, where `setsid` exists and the hook works; on darwin this is "
         "treated as the one sanctioned known failure. Evidence: \"macOS has no setsid... "
         "on darwin this is the one sanctioned known failure.\""),
        ("method", True,
         "- [method] **Every new hook test must pass under `LOOM_GATE=1` before it is "
         "merged** — standing rule, written into AGENTS.md. Evidence: \"every new hook "
         "test must pass under LOOM_GATE=1 before it is merged.\""),
    ],
}


# ---------------------------------------------------------------------------- #
# Accessory: the real server                                                    #
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
    tmp = tmp_path_factory.mktemp("d13")
    db = tmp / "loom.db"
    port = kit.find_free_port()
    proc = kit.start_server(db, port, VENV_PY)
    base = f"http://127.0.0.1:{port}"
    try:
        kit.wait_health(base)
        yield {"base": base, "tok": _adduser(db, "goalp"), "db": db}
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _h(srv) -> dict:
    return {"Authorization": f"Bearer {srv['tok']}"}


def _register(srv, name: str) -> int:
    r = requests.post(f"{srv['base']}/api/projects",
                      json={"remote_url": f"git@github.com:goalp/{name}.git"},
                      headers=_h(srv), timeout=10)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _post(srv, name: str, content: str, sid: str, **extra) -> requests.Response:
    body = {"project_remote": f"github.com/goalp/{name}", "session_id": sid,
            "content_md": content,
            "content_hash": hashlib.sha256(f"{name}|{sid}|{content}".encode()).hexdigest(),
            "model": "sonnet", "prompt_version": "v3", **extra}
    return requests.post(f"{srv['base']}/api/entries", json=body, headers=_h(srv),
                         timeout=10)


def _entries(srv, pid: int, **params) -> list[dict]:
    r = requests.get(f"{srv['base']}/api/projects/{pid}/entries",
                     params={"limit": 100, **params}, headers=_h(srv), timeout=10)
    assert r.status_code == 200, r.text
    return r.json()["entries"]


def _items_resp(srv, pid: int, entry_ids, **params) -> requests.Response:
    ids = entry_ids if isinstance(entry_ids, str) else ",".join(map(str, entry_ids))
    return requests.get(f"{srv['base']}/api/projects/{pid}/items",
                        params={"entry_ids": ids, **params}, headers=_h(srv), timeout=10)


def _items(srv, pid: int, entry_ids, **params) -> list[dict]:
    r = _items_resp(srv, pid, entry_ids, **params)
    assert r.status_code == 200, r.text
    return r.json()["items"]


# ---------------------------------------------------------------------------- #
# C2 — items round-trip, in order, byte-equal                                   #
# ---------------------------------------------------------------------------- #
def test_c2_items_round_trip_with_ordinals_in_payload_order(srv):
    pid = _register(srv, "c2")
    r = _post(srv, "c2", ENTRY_MD, "s-c2", items=ITEMS,
              item_types=["decision", "method", "task"])
    assert r.status_code == 201, r.text
    eid = r.json()["id"]

    got = _items(srv, pid, [eid])
    assert [set(i) >= ITEM_KEYS for i in got] == [True] * 3, got
    assert [(i["entry_id"], i["ordinal"]) for i in got] == [(eid, 1), (eid, 2), (eid, 3)]
    assert [(i["item_type"], i["labelled"], i["text_md"]) for i in got] == [
        (i["item_type"], i["labelled"], i["text_md"]) for i in ITEMS
    ], "text_md is stored and served byte-for-byte"
    assert all(i["match"] is True for i in got), "no filter given: every item matches"
    assert all(i["status"] == "current" and i["links_in"] == [] and i["links_out"] == []
               for i in got), "no item link exists yet"


def test_c2_item_types_are_derived_from_the_items_when_absent(srv):
    pid = _register(srv, "c2derive")
    r = _post(srv, "c2derive", ENTRY_MD, "s-derive", items=ITEMS)
    assert r.status_code == 201, r.text
    eid = r.json()["id"]
    for label in ("decision", "method", "task"):
        assert [e["id"] for e in _entries(srv, pid, type=label)] == [eid], (
            f"?type={label} must find the entry through the item-derived label set"
        )
    assert _entries(srv, pid, type="fact") == []


def test_c2_filters_flag_matching_items_but_return_every_item(srv):
    pid = _register(srv, "c2filter")
    eid = _post(srv, "c2filter", ENTRY_MD, "s-filter", items=ITEMS).json()["id"]

    typed = _items(srv, pid, [eid], type="method")
    assert [(i["ordinal"], i["match"]) for i in typed] == [(1, False), (2, True), (3, False)], (
        "every item of the listed entries comes back; `match` says which answer ?type="
    )
    searched = _items(srv, pid, [eid], q="vacuum")
    assert [(i["ordinal"], i["match"]) for i in searched] == [(1, False), (2, True), (3, False)]
    hit = searched[1]
    assert "snippet" in hit and "vacuum" in hit["snippet"].lower(), hit
    both = _items(srv, pid, [eid], q="Redis", type="method")
    assert [i["match"] for i in both] == [False, False, False], (
        "`match` requires EVERY filter: the Redis item is a decision, not a method"
    )


# ---------------------------------------------------------------------------- #
# C3 — invalid items are refused, and nothing is stored                         #
# ---------------------------------------------------------------------------- #
@pytest.mark.parametrize("case, items, item_types", [
    ("not_in_content", [{**ITEMS[0], "text_md": "- [decision] **Use Postgres** — x."}],
     None),
    ("unknown_type", [{**ITEMS[0], "item_type": "banana"}], None),
    ("empty_text", [{**ITEMS[0], "text_md": ""}], None),
    ("too_many", [ITEMS[0]] * 51, None),
    ("inconsistent_item_types", ITEMS, ["decision"]),
])
def test_c3_invalid_items_are_422_and_leave_no_entry(srv, case, items, item_types):
    name = f"c3{case.replace('_', '')}"
    pid = _register(srv, name)
    extra = {"items": items}
    if item_types is not None:
        extra["item_types"] = item_types
    r = _post(srv, name, ENTRY_MD, f"s-{case}", **extra)
    assert r.status_code == 422, (case, r.status_code, r.text)
    assert r.json().get("error") == "invalid_items", r.text
    assert _entries(srv, pid) == [], f"{case}: a refused entry must not be stored"


def test_c3_a_rejected_item_carrying_a_secret_is_not_echoed(srv):
    _register(srv, "c3echo")
    marker = "sk-live-NOTINCONTENT-0123456789"
    r = _post(srv, "c3echo", ENTRY_MD, "s-echo",
              items=[{**ITEMS[0], "text_md": f"- [fact] **{marker}**"}])
    assert r.status_code == 422 and marker not in r.text, r.text


# ---------------------------------------------------------------------------- #
# C4 — a re-POST is a 409 and keeps the first POST's items                      #
# ---------------------------------------------------------------------------- #
def test_c4_repost_of_the_same_hash_is_409_and_changes_nothing(srv):
    pid = _register(srv, "c4")
    first = _post(srv, "c4", ENTRY_MD, "s-c4", items=ITEMS)
    assert first.status_code == 201, first.text
    eid = first.json()["id"]
    again = _post(srv, "c4", ENTRY_MD, "s-c4", items=[ITEMS[2]])
    assert again.status_code == 409 and again.json().get("error") == "duplicate_entry"
    assert [i["text_md"] for i in _items(srv, pid, [eid])] == [i["text_md"] for i in ITEMS]
    assert len(_entries(srv, pid)) == 1


# ---------------------------------------------------------------------------- #
# C7 — an entry posted WITHOUT items is served exactly as before                #
# ---------------------------------------------------------------------------- #
def test_c7_an_entry_without_items_is_unchanged_everywhere(srv):
    pid = _register(srv, "c7")
    plain = _post(srv, "c7", ENTRY_MD, "s-c7", item_types=["method"])
    assert plain.status_code == 201, plain.text
    eid = plain.json()["id"]

    [listed] = _entries(srv, pid)
    assert set(listed) == ENTRY_KEYS, "the listing's key set is frozen"
    assert listed["content_md"] == ENTRY_MD and listed["status"] == "current"
    assert [e["id"] for e in _entries(srv, pid, type="method")] == [eid]
    [hit] = _entries(srv, pid, q="vacuum")
    assert set(hit) == ENTRY_KEYS | {"snippet", "rank"}, "the ranked key set is frozen"
    assert _items(srv, pid, [eid]) == [], "an un-itemized entry has no item rows"


# ---------------------------------------------------------------------------- #
# The read route's own errors                                                   #
# ---------------------------------------------------------------------------- #
def test_items_route_validates_like_entries(srv):
    pid = _register(srv, "errs")
    other = _register(srv, "errsother")
    eid = _post(srv, "errs", ENTRY_MD, "s-errs", items=ITEMS).json()["id"]
    foreign = _post(srv, "errsother", ENTRY_MD, "s-foreign", items=ITEMS).json()["id"]

    r = _items_resp(srv, pid, [eid], type="banana")
    assert r.status_code == 422 and r.json()["error"] == "invalid_type", r.text
    r = _items_resp(srv, pid, [eid], q='"unbalanced')
    assert r.status_code == 422 and r.json()["error"] == "invalid_query", r.text
    r = _items_resp(srv, 999999, [eid])
    assert r.status_code == 404 and r.json()["error"] == "project_not_found", r.text
    for bad in ("", "1,x", "-3", ",".join(str(n) for n in range(1, 502))):
        r = _items_resp(srv, pid, bad)
        assert r.status_code == 422, (bad[:20], r.status_code, r.text)
    assert _items(srv, pid, [foreign]) == [], (
        "another project's entry ids answer nothing through this project"
    )
    assert _items(srv, other, [foreign, eid]) and all(
        i["entry_id"] == foreign for i in _items(srv, other, [foreign, eid]))


# ---------------------------------------------------------------------------- #
# B2 (items half) — a real v3 capture posts its items                           #
# ---------------------------------------------------------------------------- #
@pytest.mark.parametrize("name, session", [("es_reversal", "wave-p-es"),
                                           ("en_rule_fact", "wave-p-en")])
def test_b2_a_v3_capture_posts_its_items(srv, tmp_path, name, session):
    from loom import processor
    from loom.config import Config

    requests.post(f"{srv['base']}/api/projects",
                  json={"remote_url": kit.TEST_REMOTE_URL}, headers=_h(srv), timeout=10)
    pid = requests.get(f"{srv['base']}/api/projects",
                       params={"remote": kit.TEST_REMOTE_URL}, headers=_h(srv),
                       timeout=10).json()["project"]["id"]

    golden = (WAVE_O / f"summary_v3_{name}.md").read_text(encoding="utf-8")
    stub = kit.make_claude_stub(tmp_path / "bin" / "claude", stdout=golden)
    home = kit.loom_home(tmp_path)
    shutil.copyfile(PROMPT_V3, home / "prompts" / "summarizer_v3.md")
    kit.write_config(home, server_url=srv["base"], token=srv["tok"], claude_bin=stub,
                     scanner_engine="regex", gitleaks_version=None)
    cfg = json.loads((home / "config.json").read_text())
    cfg["summarizer"]["prompt_version"] = "v3"
    cfg.pop("prompt_path", None)
    (home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    repo = kit.make_git_repo(tmp_path / "repo")
    transcript = tmp_path / "transcripts" / f"{session}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(WAVE_O / f"transcript_{name}.jsonl", transcript)
    loaded = Config.load(home)
    (loaded.queue_dir / f"{session}.json").write_text(json.dumps({
        "session_id": session, "transcript_path": str(transcript), "cwd": str(repo),
        "reason": "prompt_input_exit", "marked_at": kit.utc_now_iso()}), encoding="utf-8")
    report = processor.run_once(loaded)
    assert report.processed == 1 and report.errors == 0, report.outcomes

    [entry] = [e for e in _entries(srv, pid, limit=500) if e["session_id"] == session]
    got = _items(srv, pid, [entry["id"]])
    assert [(i["ordinal"], i["item_type"], i["labelled"], i["text_md"]) for i in got] == [
        (n, t, lab, text) for n, (t, lab, text) in enumerate(GOLDEN_ITEMS[name], 1)
    ]
    for item in got:
        assert item["text_md"] in entry["content_md"]
