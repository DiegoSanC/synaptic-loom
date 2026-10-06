"""FROZEN GOAL — E15 curation-links (wave j). Authored at plan time (2026-08-12,
strong model, adversarial mandate). READ-ONLY to implementers.

Real boundary: REAL uvicorn on a temp LOOM_DB (waves i merged: FTS5 + typed
items live), the REAL `loom curate` client driven by a SMART replay claude stub
speaking the FROZEN stdin/verdict grammars (record-replay of the one seam this
wave does not own). Each test registers its own project for isolation.

Kill-targets: a stored (mutable) status column; verdicts trusted without pool
validation; the LLM given tools or file paths; a watermark that advances on
failure; a curate that mutates entries rows; two racing curates double-linking;
a rerun that re-bills the LLM.
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
PLANTED_AWS = "AKIAABCDEFGHIJKLMNOP"  # matches the frozen regex rule aws_access_key

OLD_REST = ("## Context\nSync design.\n## Decisions\n"
            "- [decision] **Use REST for the sync API** — simplicity. "
            "Evidence: api.md. Status: current.\n")
NEW_GRPC = ("## Context\nSync redesign.\n## Decisions\n"
            "- [decision] **Switch the sync API to gRPC** — reverses the earlier "
            "REST decision for streaming. Evidence: api.md. Status: current.\n")
NOISE = ["## Decisions\n- [fact] **CI runs on 3.12** — x. Status: current.\n",
         "## Decisions\n- [method] **Squash merges only** — y. Status: current.\n"]


# ---------------------------------------------------------------------------- #
# Accessory                                                                     #
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
    tmp = tmp_path_factory.mktemp("e15")
    db = tmp / "loom.db"
    port = kit.find_free_port()
    proc = kit.start_server(db, port, VENV_PY)
    base = f"http://127.0.0.1:{port}"
    try:
        kit.wait_health(base)
        yield {"base": base, "tok": _adduser(db, "goal15"), "db": db}
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _register(srv, name: str) -> tuple[str, int]:
    url = f"git@github.com:goal15/{name}.git"
    r = requests.post(f"{srv['base']}/api/projects", json={"remote_url": url},
                      headers=_h(srv), timeout=10)
    assert r.status_code == 201, r.text
    return name, r.json()["id"]


def _h(srv) -> dict:
    return {"Authorization": f"Bearer {srv['tok']}"}


def _post(srv, name: str, content: str, sid: str) -> int:
    r = requests.post(f"{srv['base']}/api/entries", json={
        "project_remote": f"github.com/goal15/{name}", "session_id": sid,
        "content_md": content,
        "content_hash": hashlib.sha256(f"{name}|{content}".encode()).hexdigest(),
        "model": "sonnet", "prompt_version": "v2"},
        headers=_h(srv), timeout=10)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _entries(srv, pid: int, **params) -> list[dict]:
    r = requests.get(f"{srv['base']}/api/projects/{pid}/entries",
                     params={"limit": 100, **params}, headers=_h(srv), timeout=10)
    assert r.status_code == 200, r.text
    return r.json()["entries"]


def _post_link(srv, body: dict) -> requests.Response:
    return requests.post(f"{srv['base']}/api/links", json=body,
                         headers=_h(srv), timeout=10)


STUB_SRC = """
import sys, re, time, pathlib
rec = pathlib.Path({rec!r}); rec.mkdir(parents=True, exist_ok=True)
n = len(list(rec.glob("call-*-stdin.txt")))
data = sys.stdin.read()
(rec / f"call-{{n}}-stdin.txt").write_text(data)
(rec / f"call-{{n}}-argv.txt").write_text("\\n".join(sys.argv[1:]))
mode = {mode!r}
if mode == "slow":
    time.sleep(1.5)
if mode == "garbage":
    print("I looked at everything and it all seems fine!"); sys.exit(0)
out = []
for block in re.split(r"^### ENTRY ", data, flags=re.M)[1:]:
    eid = block.split("\\n", 1)[0].strip()
    cands = re.findall(r"^- CANDIDATE (\\d+): (.*)$", block, re.M)
    if mode == "outsider":
        out.append(f"{{eid}}: supersedes:999999"); continue
    if mode == "secret" and cands:
        out.append(f"{{eid}}: supersedes:{{cands[0][0]}} -- key {aws}"); continue
    if mode == "silent_on_empty" and not cands:
        continue
    if mode == "silent_on_candidate" and cands:
        continue
    target = next((cid for cid, txt in cands if "REST" in txt),
                  cands[0][0] if cands else None)
    if "gRPC" in block and target is not None:
        out.append(f"{{eid}}: supersedes:{{target}} -- reverses the REST decision")
    else:
        out.append(f"{{eid}}: keep")
print("\\n".join(out))
"""


def _mk_home(tmp: Path, srv, mode: str) -> tuple[Path, Path]:
    home = kit.loom_home(tmp)
    rec = tmp / "calls"
    stub = tmp / "bin" / "claude"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text("#!/usr/bin/env python3\n"
                    + STUB_SRC.format(rec=str(rec), mode=mode, aws=PLANTED_AWS),
                    encoding="utf-8")
    stub.chmod(0o755)
    kit.write_config(home, server_url=srv["base"], token=srv["tok"],
                     claude_bin=str(stub), scanner_engine="regex",
                     gitleaks_version=None)
    return home, rec


def _curate(home: Path, project: str) -> int:
    from loom import cli

    os.environ["LOOM_HOME"] = str(home)
    try:
        return cli.main(["curate", project])
    finally:
        os.environ.pop("LOOM_HOME", None)


def _state(home: Path, name: str) -> dict:
    """The PER-PROJECT curation state dict (t-curation-watermark shape)."""
    p = home / "curation_state.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    return (data.get("projects") or {}).get(f"github.com/goal15/{name}", {})


def _links_rows(db: Path) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT from_entry, to_entry, kind FROM entry_links ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _entries_dump(db: Path) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT * FROM entries ORDER BY id").fetchall()
    finally:
        conn.close()


def _calls(rec: Path) -> list[Path]:
    return sorted(rec.glob("call-*-stdin.txt")) if rec.exists() else []


# ---------------------------------------------------------------------------- #
# t-entry-links — the HTTP contract matrix                                      #
# ---------------------------------------------------------------------------- #
def test_links_endpoint_contract_matrix(srv):
    name, pid = _register(srv, "matrix")
    a = _post(srv, name, OLD_REST, "m-a")
    b = _post(srv, name, NEW_GRPC, "m-b")
    other_name, _ = _register(srv, "matrix2")
    c = _post(srv, other_name, NOISE[0], "m-c")

    ok = _post_link(srv, {"from_entry": b, "to_entry": a, "kind": "supersedes",
                          "rationale": "reversal"})
    assert ok.status_code == 201 and "id" in ok.json(), ok.text
    dup = _post_link(srv, {"from_entry": b, "to_entry": a, "kind": "supersedes"})
    assert dup.status_code == 409 and dup.json()["error"] == "duplicate_link"
    assert _post_link(srv, {"from_entry": b, "to_entry": 999999,
                            "kind": "supersedes"}).status_code == 404
    assert _post_link(srv, {"from_entry": b, "to_entry": a,
                            "kind": "renames"}).status_code == 422
    assert _post_link(srv, {"from_entry": a, "to_entry": a,
                            "kind": "duplicates"}).status_code == 422
    assert _post_link(srv, {"from_entry": b, "to_entry": c,
                            "kind": "duplicates"}).status_code == 422, (
        "cross-project links must be rejected"
    )


def test_get_entries_carries_links_and_derived_status(srv):
    name, pid = _register(srv, "derived")
    a = _post(srv, name, OLD_REST, "d-a")
    b = _post(srv, name, NEW_GRPC, "d-b")
    assert _post_link(srv, {"from_entry": b, "to_entry": a,
                            "kind": "supersedes"}).status_code == 201
    by_id = {e["id"]: e for e in _entries(srv, pid)}
    assert by_id[a]["status"] == "superseded"
    assert by_id[a]["links_in"] and by_id[a]["links_in"][0]["from_entry"] == b
    assert by_id[a]["links_in"][0]["kind"] == "supersedes"
    assert by_id[b]["status"] == "current"
    assert by_id[b]["links_out"] and by_id[b]["links_out"][0]["to_entry"] == a
    conn = sqlite3.connect(srv["db"])
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(entries)")}
        assert "status" not in cols, (
            "Q-3.5 resolution: status is DERIVED, never a stored entries column"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------- #
# AC RT.3 — reversal end-to-end through the real curate                         #
# ---------------------------------------------------------------------------- #
def test_ac_rt3_reversal_curate_links_and_renders(srv, tmp_path, capsys):
    from loom import cli

    name, pid = _register(srv, "reversal")
    old = _post(srv, name, OLD_REST, "r-old")
    for i, md in enumerate(NOISE):
        _post(srv, name, md, f"r-n{i}")
    new = _post(srv, name, NEW_GRPC, "r-new")

    home, rec = _mk_home(tmp_path, srv, "smart")
    rc = _curate(home, name)
    capsys.readouterr()
    assert rc == 0
    assert (new, old, "supersedes") in _links_rows(srv["db"]), (
        "curate must link the reversal old←new"
    )
    st = _state(home, name)
    assert st["watermark_id"] >= new, "watermark advances after confirmed posts"

    argv = (rec / "call-0-argv.txt").read_text()
    assert "--safe-mode" in argv and "--tools" in argv, "I3 invocation verbatim"
    stdin = (rec / "call-0-stdin.txt").read_text()
    assert "Use REST for the sync API" in stdin, "entry text travels via stdin"
    assert str(home) not in stdin, "no local paths in the LLM payload"

    os.environ["LOOM_HOME"] = str(home)
    try:
        assert cli.main(["log", name]) == 0
        out = capsys.readouterr().out
        assert out.startswith(cli.REFERENCE_FRAMING)
        assert f"superseded by #{new}" in out, "collapsed annotation required"
        assert out.count("Use REST for the sync API") == 0, (
            "the superseded body is not printed by default"
        )
        assert cli.main(["log", name, "--all"]) == 0
        assert "Use REST for the sync API" in capsys.readouterr().out, (
            "--all must show everything"
        )
    finally:
        os.environ.pop("LOOM_HOME", None)


# ---------------------------------------------------------------------------- #
# AC RT.3 — fail-closed validation before any POST                              #
# ---------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["outsider", "garbage", "secret"])
def test_ac_rt3_hostile_verdicts_fail_closed(srv, tmp_path, mode, capsys):
    name, pid = _register(srv, f"hostile-{mode}")
    _post(srv, name, OLD_REST, f"h-{mode}-old")
    _post(srv, name, NEW_GRPC, f"h-{mode}-new")
    before = _links_rows(srv["db"])

    if mode == "secret":
        from loom import scan_engine
        from loom.config import Config

        cfg = Config(loom_home=tmp_path, server_url="http://x", token="t",
                     scanner_engine="regex")
        assert scan_engine.scan(f"key {PLANTED_AWS}", cfg), (
            "precondition: the planted rationale secret must be detectable"
        )

    home, rec = _mk_home(tmp_path, srv, mode)
    rc = _curate(home, name)
    capsys.readouterr()
    assert rc == 1, f"{mode}: fail-closed runs must exit 1"
    assert _links_rows(srv["db"]) == before, f"{mode}: NOTHING may be posted"
    st = _state(home, name)
    assert st.get("watermark_id", 0) == 0, f"{mode}: watermark must not advance"
    assert st.get("attempts") == 1, f"{mode}: the failure must be counted"

    from loom import cli

    os.environ["LOOM_HOME"] = str(home)
    try:
        assert cli.main(["status"]) == 0
        out = capsys.readouterr().out
        assert "curation" in out.lower() and re.search(r"attempts\s*=\s*1", out), (
            "loom status must surface the failed curation attempts (attempts=1)"
        )
    finally:
        os.environ.pop("LOOM_HOME", None)


# ---------------------------------------------------------------------------- #
# AC RT.3 — entries are never mutated                                           #
# ---------------------------------------------------------------------------- #
def test_ac_rt3_curation_never_mutates_entries(srv, tmp_path):
    name, _ = _register(srv, "immutable")
    _post(srv, name, OLD_REST, "i-old")
    _post(srv, name, NEW_GRPC, "i-new")
    before = _entries_dump(srv["db"])
    home, _rec = _mk_home(tmp_path, srv, "smart")
    assert _curate(home, name) == 0
    assert _entries_dump(srv["db"]) == before, (
        "AC verbatim: entries rows byte-identical before/after; only entry_links grew"
    )


# ---------------------------------------------------------------------------- #
# AC RT.3 — two concurrent curates cannot double-link                           #
# ---------------------------------------------------------------------------- #
def test_ac_rt3_concurrent_curates_single_link_set(srv, tmp_path):
    name, _ = _register(srv, "race")
    old = _post(srv, name, OLD_REST, "c-old")
    new = _post(srv, name, NEW_GRPC, "c-new")
    home, _rec = _mk_home(tmp_path, srv, "slow")
    env = {**os.environ, "LOOM_HOME": str(home),
           "PYTHONPATH": f"{kit.REPO_ROOT}/client:{kit.REPO_ROOT}"}
    procs = [subprocess.Popen([VENV_PY, "-m", "loom.cli", "curate", name],
                              env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
             for _ in range(2)]
    codes = [p.wait(timeout=120) for p in procs]
    assert sorted(codes) == [0, 0], (
        "both runs exit 0: one curates, the loser reports lock-held and yields"
    )
    rows = [r for r in _links_rows(srv["db"]) if r == (new, old, "supersedes")]
    assert len(rows) == 1, "flock + UNIQUE must leave exactly one link"


# ---------------------------------------------------------------------------- #
# Idempotency + advance-only watermark                                          #
# ---------------------------------------------------------------------------- #
def test_rerun_is_idempotent_and_free(srv, tmp_path):
    name, _ = _register(srv, "rerun")
    _post(srv, name, OLD_REST, "e-old")
    _post(srv, name, NEW_GRPC, "e-new")
    home, rec = _mk_home(tmp_path, srv, "smart")
    assert _curate(home, name) == 0
    first_rows = _links_rows(srv["db"])
    first_calls = len(_calls(rec))
    assert first_calls == 1
    assert _curate(home, name) == 0
    assert _links_rows(srv["db"]) == first_rows, "rerun adds nothing"
    assert len(_calls(rec)) == first_calls, (
        "a watermark-guarded rerun must make ZERO new LLM calls"
    )


def test_watermark_is_advance_only(srv, tmp_path):
    name, _ = _register(srv, "advance")
    _post(srv, name, OLD_REST, "a-old")
    home, rec = _mk_home(tmp_path, srv, "smart")
    (home / "curation_state.json").write_text(json.dumps(
        {"projects": {f"github.com/goal15/{name}": {
            "watermark_id": 10**9, "digest_last_id": 10**9, "attempts": 0,
            "last_error": None}},
         "updated_at": kit.utc_now_iso()}))
    assert _curate(home, name) == 0
    assert len(_calls(rec)) == 0, "nothing newer than the watermark → no LLM call"
    assert _state(home, name)["watermark_id"] == 10**9, (
        "the watermark NEVER moves back"
    )


# ---------------------------------------------------------------------------- #
# AC RT.3 — a unit with an EMPTY pool is not a missing verdict                   #
# (added 2026-09-25: the first real `loom curate` pass of the pilot failed with  #
#  missing_verdict_line. The oldest entry of any project is offered zero         #
#  candidates, the payload renders it "(no candidates)", and the model answers   #
#  only the blocks it can decide. The stub above always answered every block,    #
#  so no existing case could observe it.)                                       #
# ---------------------------------------------------------------------------- #
def test_ac_rt3_a_unit_with_no_candidates_needs_no_verdict_line(srv, tmp_path, capsys):
    """The oldest entry has nothing to be linked to, so `keep` is its ONLY legal
    verdict — an absent line cannot be confused with a truncated answer."""
    name, _pid = _register(srv, "empty-pool")
    old = _post(srv, name, OLD_REST, "e-old")
    new = _post(srv, name, NEW_GRPC, "e-new")

    home, rec = _mk_home(tmp_path, srv, "silent_on_empty")
    rc = _curate(home, name)
    capsys.readouterr()

    stdin = (rec / "call-0-stdin.txt").read_text()
    assert "(no candidates)" in stdin, (
        "the fixture is only meaningful if the oldest entry really is offered none"
    )
    assert rc == 0, "a batch whose only silence is an empty-pool unit must succeed"
    assert (new, old, "supersedes") in _links_rows(srv["db"]), (
        "the units that COULD be judged are still linked"
    )
    assert _state(home, name)["watermark_id"] >= new, "watermark advances on success"
    assert not _state(home, name).get("attempts"), "a success clears the attempt counter"


def test_ac_rt3_a_unit_with_candidates_still_needs_its_verdict_line(srv, tmp_path, capsys):
    """The guard is NARROWED, not removed: silence about a unit that was offered
    candidates is still indistinguishable from a truncated answer, and still fails
    the whole batch before any POST."""
    name, _pid = _register(srv, "silent-pool")
    _post(srv, name, OLD_REST, "s-old")
    _post(srv, name, NEW_GRPC, "s-new")
    before = _links_rows(srv["db"])

    home, _rec = _mk_home(tmp_path, srv, "silent_on_candidate")
    rc = _curate(home, name)
    capsys.readouterr()

    assert rc != 0, "a unit with a non-empty pool and no verdict must fail the batch"
    st = _state(home, name)
    assert st["last_error"] == "missing_verdict_line", st
    assert st["watermark_id"] == 0, "the watermark must not advance on failure"
    assert _links_rows(srv["db"]) == before, "nothing is POSTed by a rejected batch"

