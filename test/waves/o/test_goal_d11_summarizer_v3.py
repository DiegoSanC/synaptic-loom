"""FROZEN GOAL — D11 summarizer v3 (wave o). Authored 2026-09-23 from the plan
`docs/plans/2026-09-23-fix-label-grammar-fidelity-plan.md` (slice B), BEFORE the
product change. READ-ONLY to implementers.

Real boundary: the REAL processor pipeline (run_once) over a real git repo, a real
transcript file and a real tmp LOOM_HOME. The claude CLI is replayed from two GOLDEN
FIXTURES recorded once with the real CLI under the exact I3 invocation and the v3
prompt (recipe below). The central server is a capturing HTTP stub, so the goal
asserts the exact payload the CLIENT sends. `install.sh` runs for real.

The expectations were written from the design of the two transcripts, not read off
the recorded output:

- `transcript_es_reversal.jsonl` (Spanish): an in-memory cache is proposed and then
  REPLACED by a SQLite table (a reversal inside the session); an index is deferred to
  Marta (pending work phrased as an aside); cache invalidation is left UNDECIDED.
  Under v3: one [decision] (the final choice, naming the discarded option in its
  rationale), one [task], and the question under `## Open questions`.
- `transcript_en_rule_fact.jsonl` (English): a finding (the pilot host is WSL2 with
  setsid) and a standing rule (hook tests pass under LOOM_GATE=1). Under v3: one
  [fact] and one [method], and no `## Open questions` section at all.

Re-record ONLY via the recipe, from a scratch directory, and only when the prompt
changes; the recorded output must still satisfy every assertion here:

    LOOM_SUMMARIZER=1 claude -p --safe-mode --tools "" --model sonnet \
        --output-format text "$(cat prompts/summarizer_v3.md)" \
        < test/waves/o/fixtures/delta_<name>.txt \
        > test/waves/o/fixtures/summary_v3_<name>.md

Kill-targets: v3 left out of the labelled allowlist (untyped entries); a v3 install
that replaces v1/v2; per-item `Status:` or `## Open items` in the v3 contract; a
translated heading; the discarded option kept as an item; pending work outside
[task].
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _goalkit as kit  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
PROMPT_V3 = kit.REPO_ROOT / "prompts" / "summarizer_v3.md"
ES = ("es_reversal", "wave-o-es")
EN = ("en_rule_fact", "wave-o-en")
LABELS = ("decision", "method", "task", "fact")
V3_HEADINGS = ("## Context", "## Decisions", "## Open questions")
# The phrases the gate's record-replay stub keys on to tell the curation and digest
# passes apart from a capture (`client/tests/e2e/test_blockt_e2e.py`). A summarizer
# prompt carrying one of them would be answered as curation.
STUB_CLASSIFIER_PHRASES = ("curating a team decision logbook",
                           "logbook entries of one software project")


def _golden(name: str) -> str:
    return (FIXTURES / f"summary_v3_{name}.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------- #
# Accessory: capturing central-server stub (the E14 pattern)                    #
# ---------------------------------------------------------------------------- #
class _Capture(ThreadingHTTPServer):
    def __init__(self, addr):
        super().__init__(addr, _Handler)
        self.entries: list[dict] = []


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: D102
        pass

    def _json(self, code: int, body: dict):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/api/health":
            self._json(200, {"status": "ok", "version": "goal"})
        elif self.path.startswith("/api/projects"):
            self._json(200, {"projects": [{
                "id": 1, "canonical_remote": kit.TEST_REMOTE_CANONICAL,
                "name": "phase2-goal"}]})
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/entries":
            self.server.entries.append(body)
            self._json(201, {"id": len(self.server.entries)})
        else:
            self._json(404, {"error": "not_found"})


@pytest.fixture()
def capture():
    srv = _Capture(("127.0.0.1", 0))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _replay(tmp: Path, base_url: str, case: tuple[str, str]):
    """One real `run_once` over the case's transcript, with the v3 config and the
    recorded golden standing in for the claude CLI."""
    from loom import processor
    from loom.config import Config

    name, session = case
    stub = kit.make_claude_stub(tmp / "bin" / "claude", stdout=_golden(name))
    home = kit.loom_home(tmp)
    shutil.copyfile(PROMPT_V3, home / "prompts" / "summarizer_v3.md")
    kit.write_config(home, server_url=base_url, claude_bin=stub,
                     scanner_engine="regex", gitleaks_version=None)
    cfg = json.loads((home / "config.json").read_text())
    cfg["summarizer"]["prompt_version"] = "v3"
    cfg.pop("prompt_path", None)   # derived from the version, as on a pilot machine
    (home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    repo = kit.make_git_repo(tmp / "repo")
    transcript = tmp / "transcripts" / f"{session}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / f"transcript_{name}.jsonl", transcript)
    marker = {"session_id": session, "transcript_path": str(transcript),
              "cwd": str(repo), "reason": "prompt_input_exit",
              "marked_at": kit.utc_now_iso()}
    loaded = Config.load(home)
    (loaded.queue_dir / f"{session}.json").write_text(json.dumps(marker), encoding="utf-8")
    report = processor.run_once(loaded)
    return loaded, report


def _frontmatter(content_md: str) -> dict[str, str]:
    m = re.match(r"^---\n(.*?)\n---\n", content_md, re.DOTALL)
    assert m, f"entry is not an OKF concept:\n{content_md[:200]}"
    return {k.strip(): v.strip() for k, _, v in
            (ln.partition(":") for ln in m.group(1).splitlines() if ":" in ln)}


def _body(content_md: str) -> str:
    return content_md.split("\n---\n", 1)[1]


def _section(body: str, heading: str) -> str | None:
    out, inside, found = [], False, False
    for line in body.splitlines():
        if line.startswith("## "):
            inside = line.strip() == heading
            found = found or inside
            continue
        if inside:
            out.append(line)
    return "\n".join(out) if found else None


def _items(body: str) -> list[tuple[str, str]]:
    """(label, text) of each top-level `- [label] ` bullet under `## Decisions`."""
    decisions = _section(body, "## Decisions") or ""
    items: list[list[str]] = []
    for line in decisions.splitlines():
        m = re.match(r"^- \[(decision|method|task|fact)\] ", line)
        if m:
            items.append([m.group(1), line])
        elif items and line.strip():
            items[-1][1] += "\n" + line
    return [(label, text) for label, text in items]


def _log_lines(cfg, action: str) -> list[str]:
    try:
        lines = cfg.log_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [ln for ln in lines
            if len(ln.split("\t")) >= 3 and ln.split("\t")[2] == action]


# ---------------------------------------------------------------------------- #
# B1 — the v3 prompt carries the D11 contract                                    #
# ---------------------------------------------------------------------------- #
def test_b1_v3_prompt_carries_the_d11_contract():
    text = PROMPT_V3.read_text(encoding="utf-8")
    for label in LABELS:
        assert f"[{label}]" in text, f"the closed vocabulary is kept: [{label}]"
    assert "NO_ENTRY" in text
    fmt = text.split("Output format", 1)[1]
    for heading in V3_HEADINGS:
        assert f"\n{heading}\n" in fmt, f"the output format names {heading!r}"
    assert "## Open items" not in text, "v3 has no 'Open items': pending work is [task]"
    assert "Status:" not in fmt, "v3 drops the per-item in-session Status field"
    assert "literal" in text.lower() and "heading" in text.lower(), (
        "the literal-English rule must cover the section headings"
    )
    for phrase in STUB_CLASSIFIER_PHRASES:
        assert phrase not in text, f"{phrase!r} would make the gate stub answer a capture"
    for older in ("summarizer_v1.md", "summarizer_v2.md"):
        assert (kit.REPO_ROOT / "prompts" / older).is_file(), f"{older} keeps shipping"


def test_b1_goldens_were_recorded_under_the_v3_contract():
    for name, _session in (ES, EN):
        text = _golden(name)
        assert text.strip() != "NO_ENTRY", f"{name}: the golden must be an entry"
        headings = [ln.strip() for ln in text.splitlines() if ln.startswith("## ")]
        assert headings and set(headings) <= set(V3_HEADINGS), (
            f"{name}: headings must be the literal-English set: {headings}"
        )
        assert headings.count("## Decisions") == 1, f"{name}: one Decisions list"
        assert "Status:" not in text, f"{name}: no per-item Status under v3"


# ---------------------------------------------------------------------------- #
# B2 — the goldens replayed through the REAL pipeline                           #
# ---------------------------------------------------------------------------- #
def test_b2_spanish_reversal_is_captured_as_its_final_state(tmp_path, capture):
    srv, base = capture
    cfg, report = _replay(tmp_path, base, ES)
    assert report.processed == 1 and report.errors == 0, report.outcomes
    [payload] = srv.entries
    assert payload["prompt_version"] == "v3"
    assert payload["item_types"] == ["decision", "task"], (
        "a v3 entry is TYPED: v3 must be a labelled prompt version"
    )
    assert _log_lines(cfg, "label_fallback") == [], "every item carried a valid label"

    fm = _frontmatter(payload["content_md"])
    assert fm["prompt"] == '"v3"'
    assert json.loads(fm["item_types"]) == ["decision", "task"]
    title = json.loads(fm["title"])
    assert any(t in title for t in ("SQLite", "stats_cache")), (
        f"the title is the FINAL decision, never the discarded one: {title!r}"
    )

    body = _body(payload["content_md"])
    assert "Status:" not in body and "## Open items" not in body
    items = _items(body)
    assert [label for label, _t in items] == ["decision", "task"], items
    decision, task = items[0][1], items[1][1]
    assert "memoria" in decision.lower(), (
        "the discarded option is named inside the final item's rationale"
    )
    assert not any("memoria" in text.lower() and "sqlite" not in text.lower()
                   and "stats_cache" not in text for _l, text in items), (
        "the discarded in-memory cache must never be an item of its own"
    )
    assert "Marta" in task and "project_id" in task, "the deferred index is a [task]"
    open_q = _section(body, "## Open questions")
    assert open_q is not None and "invalid" in open_q.lower(), (
        "the undecided invalidation question goes under '## Open questions'"
    )
    assert "Marta" not in open_q, "pending work never goes under Open questions"


def test_b2_english_rule_and_fact_are_typed_without_the_decision_catch_all(
        tmp_path, capture):
    srv, base = capture
    cfg, report = _replay(tmp_path, base, EN)
    assert report.processed == 1 and report.errors == 0, report.outcomes
    [payload] = srv.entries
    assert payload["prompt_version"] == "v3"
    assert payload["item_types"] == ["fact", "method"], (
        "a finding is a [fact] and a rule a [method]; nothing falls into [decision]"
    )
    assert _log_lines(cfg, "label_fallback") == []
    body = _body(payload["content_md"])
    items = dict((label, text) for label, text in _items(body))
    assert "LOOM_GATE" in items["method"]
    assert "setsid" in items["fact"] or "WSL2" in items["fact"]
    assert _section(body, "## Open questions") is None, (
        "nothing was left undecided, so the section is omitted"
    )
    fm = _frontmatter(payload["content_md"])
    assert "[" not in json.loads(fm["title"]), "the label never leaks into the title"


# ---------------------------------------------------------------------------- #
# B3 — v3 is labelled; an unknown version is still untyped                     #
# ---------------------------------------------------------------------------- #
def test_b3_v3_types_like_v2_and_an_unknown_version_stays_untyped():
    from loom import processor

    summary = ("## Decisions\n- [method] **Always pin gitleaks** — drift. Evidence: x.\n"
               "- **Bare bullet** — y. Evidence: z.\n")
    assert processor.parse_item_types(summary, "v3") == (["decision", "method"], 1)
    assert processor.parse_item_types(summary, "v2") == (["decision", "method"], 1)
    assert processor.parse_item_types(summary, "v9") == ([], 0)


# ---------------------------------------------------------------------------- #
# B4 — t-prompt-shipping: the REAL install.sh ships v3 beside v1 and v2         #
# ---------------------------------------------------------------------------- #
def test_b4_install_ships_v3_additively(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    loomhome = home / ".loom"
    shims = kit.shim_dir(tmp_path)
    kit.crontab_shim(shims, tmp_path / "cron_state.txt")
    path = kit.scheduler_free_path_with_gitleaks(shims)
    fake_loom = loomhome / "venv" / "bin" / "loom"
    fake_loom.parent.mkdir(parents=True, exist_ok=True)
    fake_loom.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(0)\n")
    fake_loom.chmod(0o755)
    env = {**os.environ, "HOME": str(home), "LOOM_HOME": str(loomhome),
           "PATH": path, "LOOM_SKIP_VENV": "1"}
    cp = subprocess.run(
        ["bash", str(kit.REPO_ROOT / "client" / "install.sh"), "--yes"],
        env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300,
    )
    assert cp.returncode == 0, f"install.sh --yes failed:\n{cp.stdout}\n{cp.stderr}"
    prompts = loomhome / "prompts"
    for name in ("summarizer_v1.md", "summarizer_v2.md", "summarizer_v3.md",
                 "digest_v1.md"):
        assert (prompts / name).is_file(), f"step 3 must ship {name}"
    assert (prompts / "summarizer_v3.md").read_bytes() == PROMPT_V3.read_bytes()


def test_b5_runbook_documents_the_v3_upgrade():
    text = (kit.REPO_ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    assert '"prompt_version": "v3"' in text and "summarizer_v3.md" in text, (
        "RUNBOOK must document how enrolled machines move to v3"
    )
