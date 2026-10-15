"""FROZEN GOAL — E16 digest (wave k). Authored at plan time (2026-08-12, strong
model, adversarial mandate). READ-ONLY to implementers.

Real boundary: REAL uvicorn on a temp LOOM_DB (waves i–j merged), the REAL
`loom curate` / `loom log` clients, a recording claude stub that distinguishes
the verdict pass from the digest pass by the prompt it is handed (record-replay
of the one unowned seam). The current-entries-only AC is pinned at the LLM
PAYLOAD boundary (the stub records stdin), not just in the output.

Kill-targets: a digest generated below threshold (LLM cost leak); superseded
text fed to the digest pass; a digest that skips the secret scan; a budget that
only truncates entries but lets the total run over; --no-digest that still
injects the digest; a latest_digest reachable only by scraping CLI output.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _goalkit as kit  # noqa: E402

VENV_PY = str(kit.REPO_ROOT / ".venv" / "bin" / "python")
PLANTED_AWS = "AKIAABCDEFGHIJKLMNOP"
DIGEST_MARKER = "GOAL-DIGEST-MARKER"
DIGEST_PROMPT = (kit.REPO_ROOT / "prompts" / "digest_v1.md").read_text(encoding="utf-8")

OLD_REST = ("## Decisions\n- [decision] **Use REST for the sync API** — "
            "simplicity. Status: current.\n")
NEW_GRPC = ("## Decisions\n- [decision] **Switch the sync API to gRPC** — "
            "streaming. Status: current.\n")


def _noise(i: int, pad: int = 0) -> str:
    return (f"## Decisions\n- [fact] **Observation number {i} about subsystem "
            f"s{i}** — noted. Status: current.\n" + ("filler line\n" * pad))


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
    tmp = tmp_path_factory.mktemp("e16")
    db = tmp / "loom.db"
    port = kit.find_free_port()
    proc = kit.start_server(db, port, VENV_PY)
    base = f"http://127.0.0.1:{port}"
    try:
        kit.wait_health(base)
        yield {"base": base, "tok": _adduser(db, "goal16"), "db": db}
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _h(srv):
    return {"Authorization": f"Bearer {srv['tok']}"}


def _register(srv, name: str) -> tuple[str, int]:
    r = requests.post(f"{srv['base']}/api/projects",
                      json={"remote_url": f"git@github.com:goal16/{name}.git"},
                      headers=_h(srv), timeout=10)
    assert r.status_code == 201, r.text
    return name, r.json()["id"]


def _post(srv, name: str, content: str, sid: str) -> int:
    r = requests.post(f"{srv['base']}/api/entries", json={
        "project_remote": f"github.com/goal16/{name}", "session_id": sid,
        "content_md": content,
        "content_hash": hashlib.sha256(f"{name}|{sid}|{content}".encode()).hexdigest(),
        "model": "sonnet", "prompt_version": "v2"},
        headers=_h(srv), timeout=10)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _link(srv, frm: int, to: int) -> None:
    r = requests.post(f"{srv['base']}/api/links",
                      json={"from_entry": frm, "to_entry": to,
                            "kind": "supersedes"},
                      headers=_h(srv), timeout=10)
    assert r.status_code in (201, 409), r.text


STUB_SRC = """
import sys, re, pathlib
rec = pathlib.Path({rec!r}); rec.mkdir(parents=True, exist_ok=True)
n = len(list(rec.glob("call-*-stdin.txt")))
data = sys.stdin.read()
argv = "\\n".join(sys.argv[1:])
(rec / f"call-{{n}}-stdin.txt").write_text(data)
(rec / f"call-{{n}}-argv.txt").write_text(argv)
if "Current state of decisions" in argv:      # the digest prompt
    if {secret!r}:
        print("# Current state of decisions\\n\\nkey {aws}\\n")
    else:
        print("# Current state of decisions\\n\\n{marker}\\n"
              "- **Switch the sync API to gRPC** — streaming.\\n")
    sys.exit(0)
out = []
for block in re.split(r"^### ENTRY ", data, flags=re.M)[1:]:
    out.append(block.split("\\n", 1)[0].strip() + ": keep")
print("\\n".join(out))
"""


def _mk_home(tmp: Path, srv, *, secret: bool = False) -> tuple[Path, Path]:
    home = kit.loom_home(tmp)
    rec = tmp / "calls"
    stub = tmp / "bin" / "claude"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text("#!/usr/bin/env python3\n" + STUB_SRC.format(
        rec=str(rec), secret=secret, aws=PLANTED_AWS, marker=DIGEST_MARKER),
        encoding="utf-8")
    stub.chmod(0o755)
    kit.write_config(home, server_url=srv["base"], token=srv["tok"],
                     claude_bin=str(stub), scanner_engine="gitleaks",
                     gitleaks_version=None)
    return home, rec


def _curate(home: Path, project: str) -> int:
    from loom import cli

    os.environ["LOOM_HOME"] = str(home)
    try:
        return cli.main(["curate", project])
    finally:
        os.environ.pop("LOOM_HOME", None)


def _log(home: Path, args: list[str], capsys) -> tuple[int, str]:
    from loom import cli

    os.environ["LOOM_HOME"] = str(home)
    try:
        rc = cli.main(["log", *args])
    finally:
        os.environ.pop("LOOM_HOME", None)
    return rc, capsys.readouterr().out


def _digest_calls(rec: Path) -> list[Path]:
    out = []
    for argv in sorted(rec.glob("call-*-argv.txt")) if rec.exists() else []:
        if "Current state of decisions" in argv.read_text():
            out.append(argv.with_name(argv.name.replace("argv", "stdin")))
    return out


def _digest_rows(db: Path, pid: int) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT id, prompt_version FROM entries WHERE project_id = ? "
            "AND content_md LIKE '%type: digest%' ORDER BY id", (pid,)).fetchall()
    finally:
        conn.close()


def _state(home: Path, name: str) -> dict:
    """The PER-PROJECT curation state dict (t-curation-watermark shape)."""
    p = home / "curation_state.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    return (data.get("projects") or {}).get(f"github.com/goal16/{name}", {})


# ---------------------------------------------------------------------------- #
# AC RT.4 — threshold gating at the LLM-call boundary                           #
# ---------------------------------------------------------------------------- #
def test_ac_rt4_below_threshold_no_digest(srv, tmp_path):
    name, pid = _register(srv, "below")
    for i in range(14):
        _post(srv, name, _noise(i), f"b-{i}")
    home, rec = _mk_home(tmp_path, srv)
    assert _curate(home, name) == 0
    assert _digest_calls(rec) == [], (
        "14 current entries < threshold 15 → the digest LLM pass must NOT run"
    )
    assert _digest_rows(srv["db"], pid) == []
    assert _state(home, name).get("digest_last_id", 0) == 0


def test_ac_rt4_at_threshold_digest_current_only(srv, tmp_path):
    name, pid = _register(srv, "atth")
    old = _post(srv, name, OLD_REST, "t-old")
    new = _post(srv, name, NEW_GRPC, "t-new")
    _link(srv, new, old)                      # 16 entries, 15 current
    for i in range(14):
        _post(srv, name, _noise(i), f"t-{i}")
    home, rec = _mk_home(tmp_path, srv)
    assert _curate(home, name) == 0

    calls = _digest_calls(rec)
    assert len(calls) == 1, "15 current entries ≥ threshold → exactly one digest pass"
    stdin = calls[0].read_text()
    assert "Switch the sync API to gRPC" in stdin
    assert "Observation number 3" in stdin
    assert "Use REST for the sync API" not in stdin, (
        "spec AC: superseded text must NOT enter the digest input"
    )

    rows = _digest_rows(srv["db"], pid)
    assert len(rows) == 1 and rows[0][1] == "digest-v1", (
        "the digest is a normal entries row with prompt_version digest-v1"
    )
    assert _state(home, name)["digest_last_id"] > 0

    assert _curate(home, name) == 0
    assert len(_digest_calls(rec)) == 1, (
        "spec AC (watermark): immediate rerun must NOT regenerate the digest"
    )
    assert len(_digest_rows(srv["db"], pid)) == 1


def test_ac_rt4_secret_digest_fails_closed(srv, tmp_path):
    name, pid = _register(srv, "secret")
    for i in range(15):
        _post(srv, name, _noise(i), f"s-{i}")
    home, rec = _mk_home(tmp_path, srv, secret=True)
    rc = _curate(home, name)
    assert rc == 1, "a digest that trips the scanner must fail the run closed"
    assert _digest_rows(srv["db"], pid) == [], "NOTHING may be posted (I4)"
    assert _state(home, name).get("digest_last_id", 0) == 0
    assert _state(home, name).get("attempts", 0) >= 1


# ---------------------------------------------------------------------------- #
# t-digest-entry — importable client-side detection (Block M's seam)            #
# ---------------------------------------------------------------------------- #
def test_latest_digest_importable_and_correct():
    from loom.curate import latest_digest

    def entry(eid, ts, md):
        return {"id": eid, "created_at": ts, "content_md": md}

    d1 = entry(5, "2026-08-01T00:00:00Z",
               "---\ntype: digest\ntitle: \"d1\"\n---\n\nold digest\n")
    d2 = entry(9, "2026-08-10T00:00:00Z",
               "---\ntype: digest\ntitle: \"d2\"\n---\n\nnew digest\n")
    normal = entry(7, "2026-08-05T00:00:00Z",
                   "---\ntype: decision-log-entry\ntitle: \"n\"\n---\n\n- **X**\n")
    assert latest_digest([d1, normal, d2])["id"] == 9
    assert latest_digest([normal]) is None
    assert latest_digest([]) is None


# ---------------------------------------------------------------------------- #
# AC RT.4 — budgeted rendering (t-budgeted-log)                                 #
# ---------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def big_project(srv):
    name, pid = _register(srv, "big")
    for i in range(60):
        _post(srv, name, _noise(i, pad=8), f"big-{i}")
    digest_md = ("---\ntype: digest\ntitle: \"State of decisions\"\n---\n\n"
                 f"# Current state of decisions\n\n{DIGEST_MARKER}\n"
                 "- **Switch the sync API to gRPC** — streaming.\n")
    _post(srv, name, digest_md, "big-digest")
    return name


def test_ac_rt4_budgeted_output_digest_first(srv, big_project, tmp_path, capsys):
    from loom import cli

    home, _rec = _mk_home(tmp_path, srv)
    rc, out = _log(home, [big_project, "--limit", "60"], capsys)
    assert rc == 0
    assert out.startswith(cli.REFERENCE_FRAMING), "I5 framing survives the budget"
    assert len(out) <= 8000, (
        f"default log.char_budget is 8000; got {len(out)} chars"
    )
    assert "truncated at budget" in out, "an explicit truncation marker is required"
    assert DIGEST_MARKER in out, "the latest digest must be present"
    first_entry = out.find("Observation number")
    assert first_entry == -1 or out.find(DIGEST_MARKER) < first_entry, (
        "the digest renders BEFORE any non-digest entry"
    )


def test_ac_rt4_no_digest_and_limit_restore_old_behavior(srv, big_project,
                                                         tmp_path, capsys):
    home, _rec = _mk_home(tmp_path, srv)
    rc, out = _log(home, [big_project, "--no-digest", "--limit", "2"], capsys)
    assert rc == 0
    assert DIGEST_MARKER not in out, "--no-digest must not inject the digest"
    assert out.count("─" * 72) == 2, "--limit is honored in the old rendering"


# ---------------------------------------------------------------------------- #
# Consumer-surface refresh legs owned by E16                                    #
# ---------------------------------------------------------------------------- #
def test_doctor_no_config_names_v2_prompt(tmp_path, monkeypatch, capsys):
    from loom import cli

    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "summarizer_v2.md" in out, (
        "the no-config doctor fallback literal is refreshed to v2 in this epic"
    )
    assert "summarizer_v1.md" not in out


def test_skill_documents_digest_and_budget():
    text = (kit.REPO_ROOT / "client" / "skills" / "loom" / "SKILL.md").read_text()
    assert re.search(r"digest", text, re.IGNORECASE), (
        "/loom must mention the digest-first budgeted output"
    )
