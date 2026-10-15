"""FROZEN GOAL — E14 typed-capture (wave i). Authored at plan time (2026-08-12,
strong model, adversarial mandate). READ-ONLY to implementers.

Real boundary: the REAL processor pipeline (run_once) over a real git repo, a
real transcript file and a real tmp LOOM_HOME. The claude CLI is replayed from
the GOLDEN FIXTURE recorded at plan time with the real CLI (2.1.228) under the
exact I3 invocation — `test/waves/i/fixtures/summary_v2_golden.md`. The central
server is a capturing HTTP stub (the sanctioned unit-scope seam), so the goal
asserts the exact payload the CLIENT sends — E14 must never depend on E13's
server-side work (parallel, file-disjoint wave).

Kill-targets: a parser that edits the summary body; a fallback that fails the
entry or logs the label text; case-insensitive label matching; item_types
leaking into v1 frontmatter; a title that keeps the label; a prompt_path default
that ignores prompt_version; an install that replaces v1 instead of adding v2.
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
GOLDEN = FIXTURES / "summary_v2_golden.md"
TRANSCRIPT = FIXTURES / "transcript_sop.jsonl"
ALL_LABELS = ["decision", "fact", "method", "task"]  # frozen sorted order


# ---------------------------------------------------------------------------- #
# Accessory: capturing central-server stub (sanctioned unit-scope seam)         #
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


def _v2_home(tmp: Path, base_url: str, claude_bin: str) -> Path:
    """Real LOOM_HOME with a v2 config: prompt_version v2, v2 prompt file, regex
    scanner (deterministic across hosts), replay claude."""
    home = kit.loom_home(tmp)
    shutil.copyfile(kit.REPO_ROOT / "prompts" / "summarizer_v2.md",
                    home / "prompts" / "summarizer_v2.md")
    kit.write_config(home, server_url=base_url, claude_bin=claude_bin,
                     scanner_engine="gitleaks", gitleaks_version=None)
    cfg = json.loads((home / "config.json").read_text())
    cfg["summarizer"]["prompt_version"] = "v2"
    cfg["prompt_path"] = str(home / "prompts" / "summarizer_v2.md")
    (home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return home


def _run_processor(tmp: Path, base_url: str, summary: str,
                   transcript_src: Path | None = None) -> tuple:
    from loom.config import Config
    from loom import processor

    stub = kit.make_claude_stub(tmp / "bin" / "claude", stdout=summary)
    home = _v2_home(tmp, base_url, stub)
    repo = kit.make_git_repo(tmp / "repo")
    transcript = tmp / "transcripts" / "s1.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    if transcript_src is not None:
        shutil.copyfile(transcript_src, transcript)
    else:
        kit.write_transcript(transcript, kit.transcript_lines(str(repo), "s1"))
    marker = {"session_id": "s1", "transcript_path": str(transcript),
              "cwd": str(repo), "reason": "prompt_input_exit",
              "marked_at": kit.utc_now_iso()}
    cfg = Config.load(home)
    (cfg.queue_dir / "s1.json").write_text(json.dumps(marker), encoding="utf-8")
    report = processor.run_once(cfg)
    return cfg, report


def _frontmatter(content_md: str) -> str:
    m = re.match(r"^---\n(.*?)\n---\n", content_md, re.DOTALL)
    assert m, f"entry is not an OKF concept:\n{content_md[:200]}"
    return m.group(1)


def _log_lines(cfg, action: str) -> list[str]:
    try:
        lines = cfg.log_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [ln for ln in lines
            if len(ln.split("\t")) >= 3 and ln.split("\t")[2] == action]


# ---------------------------------------------------------------------------- #
# Golden fixture integrity (guards the recorded oracle itself)                  #
# ---------------------------------------------------------------------------- #
def test_golden_fixture_recorded_and_well_formed():
    assert GOLDEN.is_file() and TRANSCRIPT.is_file(), (
        "the plan-time golden fixture must exist; re-record ONLY via the recipe "
        "in test/waves/README.md"
    )
    text = GOLDEN.read_text(encoding="utf-8")
    for label in ALL_LABELS:
        assert re.search(rf"^- \[{label}\] ", text, re.MULTILINE), (
            f"golden must carry a [{label}] bullet"
        )
    method = [ln for ln in text.splitlines() if ln.startswith("- [method] ")]
    assert any("make contract" in ln for ln in method), (
        "spec RT.2 AC anchor: the SOP must be labeled [method]"
    )


# ---------------------------------------------------------------------------- #
# AC RT.2.1/RT.2.2 — golden replay through the REAL pipeline                    #
# ---------------------------------------------------------------------------- #
def test_ac_rt2_golden_replay_payload_and_frontmatter(tmp_path, capture):
    srv, base = capture
    cfg, report = _run_processor(tmp_path, base,
                                 GOLDEN.read_text(encoding="utf-8"), TRANSCRIPT)
    assert report.processed == 1 and report.errors == 0, report.outcomes
    [payload] = srv.entries
    assert payload["prompt_version"] == "v2"
    assert payload["item_types"] == ALL_LABELS, (
        "payload item_types must be the sorted, deduped label set"
    )
    fm = _frontmatter(payload["content_md"])
    line = [ln for ln in fm.splitlines() if ln.startswith("item_types:")]
    assert line, "frontmatter must carry the item_types field"
    assert json.loads(line[0].split(":", 1)[1].strip()) == ALL_LABELS
    body = payload["content_md"].split("---\n", 2)[2]
    assert re.search(r"^- \[method\] ", body, re.MULTILINE), (
        "labels must stay verbatim in the body (D7: the parse never edits content)"
    )
    title = [ln for ln in fm.splitlines() if ln.startswith("title:")][0]
    assert "[method]" not in title and "[decision]" not in title, (
        "the label must never leak into the extracted title"
    )
    assert json.loads(title.split(":", 1)[1].strip()).startswith("Always run")


def test_ac_rt2_method_item_present_under_v2(tmp_path, capture):
    srv, base = capture
    _run_processor(tmp_path, base, GOLDEN.read_text(encoding="utf-8"), TRANSCRIPT)
    [payload] = srv.entries
    assert "method" in payload["item_types"], (
        "spec RT.2 AC: the SOP fixture must yield at least one [method] item"
    )


# ---------------------------------------------------------------------------- #
# AC RT.2.2 — garbage labels fall back, counted, entry never fails              #
# ---------------------------------------------------------------------------- #
GARBAGE = (
    "## Context\nX.\n## Decisions\n"
    "- [banana] **Use X over Y** — z. Status: current.\n"
    "- **Bare bullet decision** — (rationale not stated). Status: current.\n"
    "- [method] **Always pin versions** — w. Status: current.\n"
)


def test_ac_rt2_garbage_label_falls_back_counted_entry_posts(tmp_path, capture):
    srv, base = capture
    cfg, report = _run_processor(tmp_path, base, GARBAGE)
    assert report.processed == 1 and report.errors == 0, (
        "a garbage label must NEVER fail the entry"
    )
    [payload] = srv.entries
    assert payload["item_types"] == ["decision", "method"]
    lines = _log_lines(cfg, "label_fallback")
    assert len(lines) == 1, "at most one label_fallback line per entry"
    assert lines[0].split("\t")[3] == "count=2", (
        "detail must be exactly count=<n> (banana + bare = 2)"
    )
    assert "banana" not in lines[0], "never a byte of the label text in the log (I5/I6)"


def test_labels_are_case_sensitive_lowercase(tmp_path, capture):
    srv, base = capture
    cfg, _ = _run_processor(
        tmp_path, base,
        "## Decisions\n- [Method] **X** — y. Status: current.\n")
    [payload] = srv.entries
    assert payload["item_types"] == ["decision"], (
        "[Method] is NOT a valid label; strict lowercase match only"
    )
    assert _log_lines(cfg, "label_fallback")[0].split("\t")[3] == "count=1"


def test_no_entry_disposition_unchanged(tmp_path, capture):
    srv, base = capture
    cfg, report = _run_processor(tmp_path, base, "NO_ENTRY")
    assert not srv.entries and report.skipped == 1
    assert any(o.action == "no_entry" for o in report.outcomes), (
        "the parse insertion must not disturb the NO_ENTRY disposition"
    )


# ---------------------------------------------------------------------------- #
# AC RT.2 — v1 untouched: frontmatter byte-identity + title tolerance           #
# ---------------------------------------------------------------------------- #
V1_EXPECTED = """---
type: decision-log-entry
title: "T"
timestamp: "2026-08-12T00:00:00Z"
generated: true
status: current
project: "github.com/org/repo"
tags: ["repo"]
sources:
  - session: "s"
    covered_from: null
    covered_to: null
    model: "sonnet"
    prompt: "v1"
---
- **T** — x.
"""


def test_ac_rt2_v1_wrap_entry_byte_identical():
    from loom import okf

    out = okf.wrap_entry(
        "- **T** — x.", canonical_remote="github.com/org/repo", session_id="s",
        covered_from=None, covered_to=None, model="sonnet", prompt_version="v1",
        timestamp="2026-08-12T00:00:00Z",
    )
    assert out == V1_EXPECTED, (
        "wrap_entry WITHOUT item_types must stay byte-identical to today "
        "(no retroactive re-typing of v1 entries)"
    )


def test_ac_rt2_extract_title_tolerates_leading_label():
    from loom import okf

    assert okf.extract_title("- **T** — x.") == "T"
    assert okf.extract_title("- [method] **T** — x.") == "T", (
        "the v2 label must be transparent to title extraction"
    )


# ---------------------------------------------------------------------------- #
# Config: version-derived prompt_path default                                   #
# ---------------------------------------------------------------------------- #
def test_config_defaults_v2_and_version_derived_prompt_path(tmp_path):
    from loom.config import Config

    fresh = Config(loom_home=tmp_path, server_url="http://x", token="t")
    assert fresh.prompt_version == "v2"
    assert fresh.prompt_path.name == "summarizer_v2.md"
    pinned_v1 = Config(loom_home=tmp_path, server_url="http://x", token="t",
                       prompt_version="v1")
    assert pinned_v1.prompt_path.name == "summarizer_v1.md", (
        "a v1 config must keep resolving the v1 file (pilot compatibility)"
    )
    explicit = Config(loom_home=tmp_path, server_url="http://x", token="t",
                      prompt_version="v2", prompt_path=tmp_path / "own.md")
    assert explicit.prompt_path == tmp_path / "own.md", "explicit path always wins"


def test_repo_prompts_exist_with_frozen_grammar():
    v2 = (kit.REPO_ROOT / "prompts" / "summarizer_v2.md").read_text(encoding="utf-8")
    for label in ALL_LABELS:
        assert f"[{label}]" in v2, "the closed vocabulary is frozen in the prompt"
    assert "NO_ENTRY" in v2, "the sentinel survives v2"
    assert (kit.REPO_ROOT / "prompts" / "summarizer_v1.md").is_file(), (
        "v1 stays in the repo (provenance; pinned configs)"
    )
    assert (kit.REPO_ROOT / "prompts" / "digest_v1.md").is_file()


# ---------------------------------------------------------------------------- #
# t-prompt-shipping — REAL install.sh, step 3 additive copy                     #
# ---------------------------------------------------------------------------- #
def test_install_ships_v2_additively(tmp_path):
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
           "PATH": path, "LOOM_SKIP_VENV": "1", "LOOM_SKIP_GITLEAKS": "1"}
    cp = subprocess.run(
        ["bash", str(kit.REPO_ROOT / "client" / "install.sh"), "--yes"],
        env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL,
        timeout=300,
    )
    assert cp.returncode == 0, f"install.sh --yes failed:\n{cp.stdout}\n{cp.stderr}"
    prompts = loomhome / "prompts"
    assert (prompts / "summarizer_v1.md").is_file(), "v1 must KEEP shipping"
    assert (prompts / "summarizer_v2.md").is_file(), "step 3 must ship v2"
    assert (prompts / "digest_v1.md").is_file(), "step 3 must ship the digest prompt"


def test_runbook_documents_pilot_upgrade():
    text = (kit.REPO_ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    assert "prompt_version" in text and '"v2"' in text, (
        "RUNBOOK must document the pilot config upgrade to v2"
    )
