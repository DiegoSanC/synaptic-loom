"""D16 part 3 — the extract is redacted BEFORE the summarizer reads it (step 5b).

Invariant under test: *the summarizer never receives text in which the scanner finds a
secret.* Every value the active engine (gitleaks ∪ regex) detects is replaced, in every
place it occurs, by ``[REDACTED:<kind>]``; a redaction that does not reach a fixpoint
(or a scan that raises) means the summarizer is NOT run and the marker takes the
bounded-attempts path. ``content_hash`` stays over the RAW extract (I6).

Boundaries: real processor through the real CLI entry point, the REAL gitleaks 8.30.1
(required by the suite, AGENTS.md) for the redaction cases, pytest-httpserver as the
server, and a ``claude`` stub that records exactly what it received on stdin into a
file OUTSIDE ``LOOM_HOME``. The fixpoint failures need a gitleaks that misbehaves in a
specific way, so those use a shim at ``$LOOM_HOME/bin`` (a boundary this client does
not own).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

from loom import cli, processor

# Validated against gitleaks 8.30.1 (test/waves/_goalkit.py): rule generic-api-key; the
# regex set alone returns nothing for it.
API_SECRET = "zX9j2kQ8vB4nM7cL1wR5tY6uI3oP0aSdFgHjKlZxCvB"
# Probed against gitleaks 8.30.1 on 2026-10-15: `DB_PASSWORD=<this>` → generic-api-key,
# `sk_live_<this>` → stripe-access-token. Neither is caught by the phase-1 regex set.
DB_SECRET = "Xk9vQ2mLp7RtW4zN"
# Split so the literal in this file is not itself a Stripe-shaped token (GitHub push
# protection); the runtime value is the full key the scanners must catch.
STRIPE_KEY = "sk_" + "live_51HxQ2mLp7RtW4zNa8bC3dE5fG6hJ7kL9"
EMAIL = "dev2@example.test"
SECRETS = (API_SECRET, DB_SECRET, STRIPE_KEY, EMAIL)

CANNED_SUMMARY = "## Context\nRotated credentials.\n## Decisions\n- Keep bearer tokens.\n"
REAL_GITLEAKS = shutil.which("gitleaks")


def _transcript(env, turns: list[tuple[str, str]]) -> None:
    lines = [{"type": "file-history-snapshot", "messageId": "m0", "snapshot": {}}]
    for i, (role, text) in enumerate(turns):
        content = text if role == "user" else [{"type": "text", "text": text}]
        lines.append({"type": role, "uuid": f"t-{i}", "cwd": str(env.repo),
                      "timestamp": "2026-07-24T10:00:00Z",
                      "message": {"role": role, "content": content}})
    env.transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\n",
                              encoding="utf-8")


SECRET_TURNS = [
    ("user", f'Set api_key = "{API_SECRET}" and DB_PASSWORD={DB_SECRET} in the env.'),
    ("assistant", f"Done. I will repeat the key for clarity: {API_SECRET} and "
                  f"Stripe uses {STRIPE_KEY} ; ping {EMAIL} when rotated."),
    ("user", f"Thanks, and the db one was {DB_SECRET} right?"),
]


def _recording_stub(env, received: Path, *, stdout: str = CANNED_SUMMARY) -> str:
    path = env.home / "claude_recorder.py"
    path.write_text(
        f"#!{sys.executable}\nimport pathlib, sys\n"
        f"pathlib.Path({str(received)!r}).write_text(sys.stdin.read())\n"
        f"sys.stdout.write({stdout!r})\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path)


def _scanner(env, engine: str = "gitleaks"):
    data = env.config_json()
    data["scanner"] = {"engine": engine, "gitleaks_version": "8.30.1"}
    (env.home / "config.json").write_text(json.dumps(data), encoding="utf-8")


def _accept_posts(env) -> None:
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201)


def _every_file_under(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and not p.is_symlink()]


def _assert_no_secret_anywhere(env, *texts: str, secrets=SECRETS) -> None:
    for value in secrets:
        for text in texts:
            assert value not in text
        for path in _every_file_under(env.home):
            if path.suffix == ".py" or path.parent == env.home / "bin":
                continue  # the test's own stub scripts, which hold the values on purpose
            assert value.encode() not in path.read_bytes(), f"{value[:4]}… leaked into {path}"


@pytest.fixture
def run_cli(env, monkeypatch, capsys):
    monkeypatch.setenv("LOOM_HOME", str(env.home))

    def _run(*argv: str) -> tuple[int, str]:
        rc = cli.main(list(argv))
        out, err = capsys.readouterr()
        return rc, out + err
    return _run


# --------------------------------------------------------------------------- #
# Redaction with the real gitleaks                                             #
# --------------------------------------------------------------------------- #
def test_summarizer_never_receives_a_detected_secret(env, run_cli, tmp_path):
    assert REAL_GITLEAKS, "the suite requires the real gitleaks (AGENTS.md)"
    received = tmp_path / "summarizer_stdin.txt"   # outside LOOM_HOME on purpose
    _transcript(env, SECRET_TURNS)
    env.config(_recording_stub(env, received))
    _scanner(env)
    _accept_posts(env)
    env.drop_marker("sess-redact")

    rc, output = run_cli("process")

    assert rc == 0, output
    stdin = received.read_text(encoding="utf-8")
    for value in SECRETS:
        assert value not in stdin, f"{value[:4]}… reached the summarizer"
    # Every occurrence is gone, and the labels say what was there.
    assert stdin.count("[REDACTED:generic-api-key]") >= 4   # key twice, db password twice
    assert "[REDACTED:stripe-access-token]" in stdin
    assert "[REDACTED:email]" in stdin
    assert "Set api_key" in stdin and "Thanks" in stdin      # the prose survives
    assert [b["session_id"] for b in env.posted_entries()] == ["sess-redact"]
    _assert_no_secret_anywhere(env, output, json.dumps(env.posted_entries()))


def test_the_run_log_names_counts_and_kinds_only(env, run_cli, tmp_path):
    _transcript(env, SECRET_TURNS)
    env.config(_recording_stub(env, tmp_path / "stdin.txt"))
    _scanner(env)
    _accept_posts(env)
    env.drop_marker("sess-log")

    run_cli("process")

    log = (env.home / "log" / "processor.log").read_text(encoding="utf-8")
    lines = [line for line in log.splitlines() if "\tredacted_before_summary" in line]
    assert len(lines) == 1, log
    detail = lines[0].split("\t", 3)[3]
    assert "generic-api-key" in detail and "stripe-access-token" in detail
    assert "email" in detail
    _assert_no_secret_anywhere(env, log)


def test_clean_extract_reaches_the_summarizer_unchanged(env, run_cli, tmp_path):
    received = tmp_path / "stdin.txt"
    turns = [("user", "Refactor the auth flow"), ("assistant", "Decided to keep bearer tokens.")]
    _transcript(env, turns)
    env.config(_recording_stub(env, received))
    _scanner(env)
    _accept_posts(env)
    env.drop_marker("sess-clean")

    run_cli("process")

    stdin = received.read_text(encoding="utf-8")
    assert "Refactor the auth flow" in stdin and "Decided to keep bearer tokens." in stdin
    assert "[REDACTED:" not in stdin
    log = (env.home / "log" / "processor.log").read_text(encoding="utf-8")
    assert "redacted_before_summary" not in log


def test_content_hash_is_computed_over_the_raw_extract(env, run_cli, tmp_path):
    """I6 unchanged: the hash is DESIGN §4.2's formula over the UNREDACTED extract, so a
    range captured before D16 dedupes exactly as it did, whatever gitleaks finds."""
    _transcript(env, SECRET_TURNS)
    env.config(_recording_stub(env, tmp_path / "stdin.txt"))
    _scanner(env)
    _accept_posts(env)
    env.drop_marker("sess-hash")

    run_cli("process")

    body = env.posted_entries()[0]
    raw = processor.render_for_summarizer(processor.read_delta(env.transcript, 0))
    assert API_SECRET in raw                       # the extract really is the raw one
    inner = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    material = f"None|{body['covered_to']}|v1|sonnet|{inner}"
    assert body["content_hash"] == hashlib.sha256(material.encode("utf-8")).hexdigest()


def test_hash_differs_from_a_transcript_that_already_held_the_labels(env, run_cli, tmp_path):
    """If the hash were taken over the REDACTED text, a transcript that literally
    contains the labels would collide with one that held the secrets."""
    _transcript(env, SECRET_TURNS)
    env.config(_recording_stub(env, tmp_path / "a.txt"))
    _scanner(env)
    _accept_posts(env)
    env.drop_marker("sess-a")
    run_cli("process")
    redacted_stdin = (tmp_path / "a.txt").read_text(encoding="utf-8")

    labelled = [(role, text.replace(API_SECRET, "[REDACTED:generic-api-key]")
                 .replace(DB_SECRET, "[REDACTED:generic-api-key]")
                 .replace(STRIPE_KEY, "[REDACTED:stripe-access-token]")
                 .replace(EMAIL, "[REDACTED:email]")) for role, text in SECRET_TURNS]
    env.transcript.unlink()
    (env.home / "state.json").unlink()
    _transcript(env, labelled)
    env.config(_recording_stub(env, tmp_path / "b.txt"))
    _scanner(env)
    env.drop_marker("sess-a")
    run_cli("process")

    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == redacted_stdin
    first, second = env.posted_entries()
    assert first["content_hash"] != second["content_hash"]


# --------------------------------------------------------------------------- #
# Fixpoint failure and scan errors: the summarizer is NOT run                  #
# --------------------------------------------------------------------------- #
# A gitleaks whose finding is not a literal substring of the text (its decoders can
# report a value found inside base64, for instance): redaction cannot remove it, so
# the re-scan still finds it.
UNREMOVABLE = "Qm8vR2nLx7TtW4zPa8bC3dE5fG6hJ7kLz"
STUCK_GITLEAKS = f"""
if sys.argv[1:2] == ['version']:
    print('8.30.1'); sys.exit(0)
sys.stdin.read()
sys.stdout.write({json.dumps(json.dumps([{"RuleID": "generic-api-key", "Secret": UNREMOVABLE,
                                           "Match": "token=" + UNREMOVABLE}]))})
sys.exit(2)
"""
CRASHING_GITLEAKS = """
if sys.argv[1:2] == ['version']:
    print('8.30.1'); sys.exit(0)
sys.stdin.read()
sys.stderr.write('internal error\\n')
sys.exit(3)
"""


def _home_gitleaks(env, body: str) -> None:
    path = env.home / "bin" / "gitleaks"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\nimport sys\n" + body, encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.parametrize("shim, expected_detail", [
    (STUCK_GITLEAKS, "generic-api-key"),
    (CRASHING_GITLEAKS, "GitleaksError"),
])
def test_unfinished_redaction_never_runs_the_summarizer(env, run_cli, tmp_path, shim,
                                                         expected_detail):
    received = tmp_path / "stdin.txt"
    _home_gitleaks(env, shim)
    _transcript(env, SECRET_TURNS)
    env.config(_recording_stub(env, received))
    _scanner(env)
    _accept_posts(env)
    marker = env.drop_marker("sess-stuck")

    outcomes = []
    for attempt in range(1, processor.MAX_ATTEMPTS + 1):
        rc, output = run_cli("process")
        assert not received.exists(), "the summarizer must never run on an unredacted extract"
        assert env.posted_entries() == []
        _assert_no_secret_anywhere(env, output, secrets=(*SECRETS, UNREMOVABLE))
        if attempt < processor.MAX_ATTEMPTS:
            data = json.loads(marker.read_text(encoding="utf-8"))
            assert data["attempts"] == attempt          # counted, bounded
            assert expected_detail in data["pipeline_error"]
        outcomes.append(output)

    assert not marker.exists()
    assert (env.home / "failed" / "sess-stuck.json").exists(), "parked after the limit"
    assert "failed_parked" in outcomes[-1]
    assert env.pointer() == {}, "the pointer never moves past an unsummarized range"
    log = (env.home / "log" / "processor.log").read_text(encoding="utf-8")
    assert expected_detail in log
