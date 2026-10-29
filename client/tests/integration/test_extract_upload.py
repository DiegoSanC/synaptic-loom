"""D22 — the entry carries the redacted extract the summarizer read.

Asserted through the real CLI entry point, the REAL gitleaks 8.30.1 and a mock server:

- when the project's listing says ``store_extracts: true``, ``POST /api/entries`` carries
  ``extract``, and it is byte for byte the text the summarizer received on stdin: the
  step 5b output, so no detected secret is in it;
- ``store_extracts: false``, or a listing without the key (a server from before D22),
  sends no ``extract`` at all;
- an entry the step 8 scan quarantines is written without its extract;
- an extract longer than two halves is cut to its first and last
  ``EXTRACT_HEAD_CHARS`` characters around one line saying how much was left out.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from loom import cli, processor

# Validated against gitleaks 8.30.1 (test_pre_summary_redaction.py): rule generic-api-key.
API_SECRET = "zX9j2kQ8vB4nM7cL1wR5tY6uI3oP0aSdFgHjKlZxCvB"
SECRET_TURNS = [
    ("user", f'Set api_key = "{API_SECRET}" in the env; the importer emits None.'),
    ("assistant", f"Done. total() now skips None. The key again: {API_SECRET}."),
]
CANNED_SUMMARY = "## Context\nThe importer emits None.\n## Decisions\n- total() skips None.\n"
REAL_GITLEAKS = shutil.which("gitleaks")
REMOTE = "github.com/org/b"     # the integration env's repo remote (conftest)


def _transcript(env, turns: list[tuple[str, str]]) -> None:
    lines = [{"type": "file-history-snapshot", "messageId": "m0", "snapshot": {}}]
    for i, (role, text) in enumerate(turns):
        content = text if role == "user" else [{"type": "text", "text": text}]
        lines.append({"type": role, "uuid": f"t-{i}", "cwd": str(env.repo),
                      "timestamp": "2026-07-24T10:00:00Z",
                      "message": {"role": role, "content": content}})
    env.transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\n",
                              encoding="utf-8")


def _recording_stub(env, received: Path, *, stdout: str) -> str:
    path = env.home / "claude_recorder.py"
    path.write_text(
        f"#!{sys.executable}\nimport pathlib, sys\n"
        f"pathlib.Path({str(received)!r}).write_text(sys.stdin.read())\n"
        f"sys.stdout.write({stdout!r})\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path)


def _scanner(env) -> None:
    data = env.config_json()
    data["scanner"] = {"engine": "gitleaks", "gitleaks_version": "8.30.1"}
    (env.home / "config.json").write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def run_cli(env, monkeypatch, capsys):
    monkeypatch.setenv("LOOM_HOME", str(env.home))

    def _run(*argv: str) -> tuple[int, str]:
        rc = cli.main(list(argv))
        out, err = capsys.readouterr()
        return rc, out + err
    return _run


def _listing(env, **flags) -> None:
    project = {"id": 1, "canonical_remote": REMOTE, "name": "loom-test",
               "created_by": "dev2", "created_at": "2026-07-01T00:00:00Z", **flags}
    env.httpserver.expect_request("/api/projects", method="GET").respond_with_json(
        {"projects": [project]})
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1, "extract_stored": True}, status=201)


def _capture(env, run_cli, tmp_path, *, stdout: str = CANNED_SUMMARY, **flags) -> str:
    assert REAL_GITLEAKS, "the suite requires the real gitleaks (AGENTS.md)"
    received = tmp_path / "summarizer_stdin.txt"
    _transcript(env, SECRET_TURNS)
    env.config(_recording_stub(env, received, stdout=stdout))
    _scanner(env)
    _listing(env, **flags)
    env.drop_marker("sess-extract")
    rc, output = run_cli("process")
    assert rc == 0, output
    return received.read_text(encoding="utf-8")


def test_the_entry_carries_exactly_what_the_summarizer_read(env, run_cli, tmp_path):
    read = _capture(env, run_cli, tmp_path, store_extracts=True)

    [posted] = env.posted_entries()
    assert posted["extract"] == read
    assert "[REDACTED:generic-api-key]" in posted["extract"]
    assert "importer emits None" in posted["extract"]
    assert API_SECRET not in json.dumps(posted)


@pytest.mark.parametrize("flags", [{"store_extracts": False}, {}],
                         ids=["switched-off", "server-before-D22"])
def test_no_extract_is_sent_unless_the_project_keeps_them(env, run_cli, tmp_path, flags):
    _capture(env, run_cli, tmp_path, **flags)

    [posted] = env.posted_entries()
    assert "extract" not in posted


def test_a_quarantined_entry_is_written_without_its_extract(env, run_cli, tmp_path):
    leaky = CANNED_SUMMARY + f'- Set api_key = "{API_SECRET}"\n'

    _capture(env, run_cli, tmp_path, stdout=leaky, store_extracts=True)

    assert env.posted_entries() == []
    [record] = list((env.home / "quarantine").glob("*.json"))
    entry = json.loads(record.read_text(encoding="utf-8"))["entry"]
    assert "content_md" in entry
    assert "extract" not in entry


# --------------------------------------------------------------------------- #
# The cut                                                                      #
# --------------------------------------------------------------------------- #
def test_an_extract_within_two_halves_is_sent_whole():
    half = processor.EXTRACT_HEAD_CHARS
    assert half == 250_000
    whole = "a" * half + "c" * half

    assert processor.cap_extract(whole) == whole


def test_a_longer_extract_keeps_its_first_and_last_halves():
    half = processor.EXTRACT_HEAD_CHARS
    text = "a" * half + "bb" + "c" * half

    assert processor.cap_extract(text) == (
        "a" * half + "\n\n[loom: 2 characters omitted here]\n\n" + "c" * half)


def test_the_cut_fits_what_the_server_accepts():
    from server import extracts

    half = processor.EXTRACT_HEAD_CHARS
    longest = processor.cap_extract("x" * (10 * half + 7))
    assert len(longest) <= extracts.MAX_CHARS
