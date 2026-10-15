"""Integration tests for processor.run_once (PLAN.md §T4, error matrix).

Central server = pytest-httpserver (mock). Summarizer = an executable stub per scenario.
The real ``claude`` CLI is NEVER invoked and the real ``~/.loom`` is never touched.
"""
from __future__ import annotations

import fcntl
import json
import subprocess
from pathlib import Path

from werkzeug.wrappers import Response

from loom import processor, sweep
from loom.transcript_adapter import read_delta

# Canonical form of the test repo's remote (== conftest.TEST_REMOTE_CANONICAL).
TEST_REMOTE_CANONICAL = "github.com/org/b"

# Clean summary (no secrets) that a happy stub emits so the POST is reached.
CANNED_SUMMARY = (
    "## Contexto\nSe trabajó en el gate de proyectos.\n"
    "## Decisiones\n- **Usar SQLite** — equipo pequeño. Evidencia: db.py. Estado: vigente.\n"
    "## Pendientes\n- ninguno\n"
)


def _fixture_facts(env):
    """The fixture's real (new_last_line, new_last_uuid) — avoids hardcoding."""
    delta = read_delta(env.transcript, 0)
    return delta.new_last_line, delta.new_last_uuid


def _actions(report):
    return [o.action for o in report.outcomes]


# --- Happy path ------------------------------------------------------------------

def test_happy_path_posts_one_entry(env):
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 42}, status=201
    )
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    env.drop_marker("sess-happy")

    report = processor.run_once(cfg)

    wc, last_uuid = _fixture_facts(env)
    bodies = env.posted_entries()
    assert len(bodies) == 1
    body = bodies[0]
    assert body["project_remote"] == TEST_REMOTE_CANONICAL
    assert body["session_id"] == "sess-happy"
    assert body["covered_from"] is None          # fresh state: no previous uuid
    assert body["covered_to"] == last_uuid
    # D7: the entry is an OKF concept — deterministic frontmatter + the summary as-is.
    assert body["content_md"].startswith("---\n")
    assert "type: decision-log-entry" in body["content_md"]
    assert f'- session: "sess-happy"' in body["content_md"]
    assert body["content_md"].rstrip().endswith(CANNED_SUMMARY.strip())
    assert body["model"] == "sonnet"
    assert body["prompt_version"] == "v1"
    assert len(body["content_hash"]) == 64        # sha256 hex

    assert _actions(report) == ["posted"]
    assert env.pointer()["last_line"] == wc
    assert env.pointer()["last_uuid"] == last_uuid
    assert not (env.home / "queue" / "sess-happy.json").exists()


# --- Idempotency (the most important test) --------------------------------------

def test_idempotent_second_run_no_duplicate(env):
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201
    )
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))

    env.drop_marker("sess-idem")
    processor.run_once(cfg)
    wc, _ = _fixture_facts(env)
    assert len(env.posted_entries()) == 1
    assert env.pointer()["last_line"] == wc

    # The hook re-marks the same session (same transcript, no changes).
    env.drop_marker("sess-idem")
    report2 = processor.run_once(cfg)

    assert len(env.posted_entries()) == 1                 # exactly one entry
    assert _actions(report2) == ["dropped_empty_delta"]   # the pointer was already at EOF
    assert env.pointer()["last_line"] == wc               # the pointer does not go backwards
    assert not (env.home / "queue" / "sess-idem.json").exists()


# --- Resume (incremental delta) --------------------------------------------------

def test_resume_delta_only_posts_the_tail(env):
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201
    )
    # The stub re-emits its stdin → content_md == rendered delta → inspectable.
    cfg = env.config(env.make_stub(echo_stdin=True))

    env.drop_marker("sess-resume")
    processor.run_once(cfg)
    first = env.posted_entries()[0]

    # Two new turns are appended to the same transcript (as a `-r` would do).
    with env.transcript.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "uuid": "resume-u",
                             "message": {"role": "user", "content": "RESUME_TAIL_MARKER seguir"}}) + "\n")
        fh.write(json.dumps({"type": "assistant", "uuid": "resume-a",
                             "message": {"role": "assistant",
                                         "content": [{"type": "text", "text": "RESUME decision"}]}}) + "\n")

    env.drop_marker("sess-resume")
    report2 = processor.run_once(cfg)

    bodies = env.posted_entries()
    assert len(bodies) == 2
    second = bodies[1]
    assert _actions(report2) == ["posted"]
    # The range chains: covered_from of the 2nd == covered_to of the 1st.
    assert second["covered_from"] == first["covered_to"]
    assert second["covered_to"] == "resume-a"
    # The 2nd delta only holds the tail, not the start of the session.
    assert "RESUME_TAIL_MARKER" in second["content_md"]
    assert "hello-from-loom-fixture" not in second["content_md"]


# --- POST 500 → keep; recovery on the next run -----------------------------------

def test_server_500_keeps_marker_then_recovers(env):
    env.register_projects()
    calls = {"n": 0}

    def flaky(_request):
        calls["n"] += 1
        if calls["n"] == 1:
            return Response(json.dumps({"error": "server_error"}), status=500,
                            content_type="application/json")
        return Response(json.dumps({"id": 7}), status=201, content_type="application/json")

    env.httpserver.expect_request("/api/entries", method="POST").respond_with_handler(flaky)
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    marker = env.drop_marker("sess-500")

    report1 = processor.run_once(cfg)
    assert _actions(report1) == ["kept_network_error"]
    assert marker.exists()                                   # the marker survives
    assert "attempts" not in json.loads(marker.read_text())  # a 5xx does not consume attempts
    assert env.pointer() == {}                               # the pointer did not advance

    report2 = processor.run_once(cfg)
    assert _actions(report2) == ["posted"]
    assert not marker.exists()
    wc, _ = _fixture_facts(env)
    assert env.pointer()["last_line"] == wc


# --- Server down + expired cache → keep, server_unreachable ----------------------

def test_server_down_expired_cache_keeps_marker(env):
    # server_url on a dead port → ConnectionRefused → ServerUnreachable.
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY), server_url="http://127.0.0.1:1")
    marker = env.drop_marker("sess-down")

    report = processor.run_once(cfg)

    assert report.server_unreachable is True
    assert _actions(report) == ["kept_server_unreachable"]
    assert marker.exists()
    assert env.posted_entries() == []
    assert env.pointer() == {}


def test_non_object_registry_cache_degrades_to_an_absent_cache(env):
    env.register_projects()
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    cfg.cache_path.write_text("[1, 2]", encoding="utf-8")
    env.drop_marker("sess-cache", transcript=env.home / "missing.jsonl")

    report = processor.run_once(cfg)

    assert _actions(report) == ["dropped_no_transcript"]
    refreshed = json.loads(cfg.cache_path.read_text(encoding="utf-8"))
    assert isinstance(refreshed, dict)
    assert refreshed["projects"][0]["canonical_remote"] == TEST_REMOTE_CANONICAL


def test_non_object_state_degrades_to_empty_state_and_the_pass_completes(env):
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201
    )
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    cfg.state_path.write_text("[1, 2]", encoding="utf-8")
    env.drop_marker("sess-state")

    report = processor.run_once(cfg)

    assert _actions(report) == ["posted"]
    assert env.posted_entries()[0]["session_id"] == "sess-state"


# --- NO_ENTRY → advance the pointer, no POST -------------------------------------

def test_no_entry_advances_pointer_without_post(env):
    env.register_projects()
    cfg = env.config(env.make_stub(stdout="NO_ENTRY\n"))
    env.drop_marker("sess-noentry")

    report = processor.run_once(cfg)

    assert _actions(report) == ["no_entry"]
    assert env.posted_entries() == []
    wc, _ = _fixture_facts(env)
    assert env.pointer()["last_line"] == wc
    assert not (env.home / "queue" / "sess-noentry.json").exists()


# --- Secret in the output → quarantine, no POST ----------------------------------

def test_secret_in_summary_quarantines_and_does_not_post(env):
    env.register_projects()
    secret_summary = "## Decisiones\n- key filtrada: sk-test1234567890abcdef en el resumen\n"
    cfg = env.config(env.make_stub(stdout=secret_summary))
    env.drop_marker("sess-secret")

    report = processor.run_once(cfg)

    assert _actions(report) == ["quarantined"]
    assert env.posted_entries() == []                        # nothing left the machine (I4)
    quarantined = list((env.home / "quarantine").glob("*.json"))
    assert len(quarantined) == 1
    record = json.loads(quarantined[0].read_text())
    assert record["reason"] == "secret_scan"
    assert "openai_key" in {f["kind"] for f in record["findings"]}
    # Payload preserved for review (OKF-wrapped, D7); the secret is still inside.
    assert record["entry"]["content_md"].startswith("---\n")
    assert record["entry"]["content_md"].rstrip().endswith(secret_summary.strip())
    assert record["project_remote"] == TEST_REMOTE_CANONICAL
    # pointer advanced and marker cleaned up
    wc, _ = _fixture_facts(env)
    assert env.pointer()["last_line"] == wc
    assert not (env.home / "queue" / "sess-secret.json").exists()


def test_secret_scan_crash_is_fail_closed(env, monkeypatch):
    """If the scanner raises, it counts as a hit (fail-closed, I4): quarantine, no POST.

    D16: the extract is scanned too (step 5b), so the crash is armed for the STEP-8 scan
    only — the one over the summary — to keep this the step-8 guarantee it always was.
    A crash at step 5b is covered by ``test_pre_summary_redaction.py``.
    """
    env.register_projects()
    real_scan = processor.secret_scan.scan

    def boom(text):
        if "Usar SQLite" in text:  # the summary: this is the step-8 scan
            raise RuntimeError("scanner exploded")
        return real_scan(text)

    monkeypatch.setattr(processor.secret_scan, "scan", boom)
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    env.drop_marker("sess-crash")

    report = processor.run_once(cfg)

    assert _actions(report) == ["quarantined"]
    assert env.posted_entries() == []
    assert len(list((env.home / "quarantine").glob("*.json"))) == 1


# --- Unregistered remote → drop the marker ---------------------------------------

def test_unregistered_remote_drops_marker(env):
    env.register_projects(remotes=("github.com/org/other",))   # the repo's own is NOT there
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    env.drop_marker("sess-unreg")

    report = processor.run_once(cfg)

    assert _actions(report) == ["dropped_not_registered"]
    assert env.posted_entries() == []
    assert not (env.home / "queue" / "sess-unreg.json").exists()


def test_no_git_remote_drops_marker(env, tmp_path):
    """cwd with no git repo → no remote → drop (privacy gate)."""
    env.register_projects()
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    no_repo = tmp_path / "plain_dir"
    no_repo.mkdir()
    env.drop_marker("sess-noremote", cwd=no_repo)

    report = processor.run_once(cfg)

    assert _actions(report) == ["dropped_no_remote"]
    assert env.posted_entries() == []
    assert not (env.home / "queue" / "sess-noremote.json").exists()


# --- Attribution authority (issue #4, contract/ATTRIBUTION.md §2) -----------------
#
# The six rows of the authority table, fast and in isolation. The SAME rows are asserted
# at the real boundary — real worktree, real deletion, real hook — in
# `test/waves/n/test_goal_issue4_attribution.py`, which is the frozen oracle; these are
# the cheap table-driven twin that makes a red row point straight at `_canonical_from_marker`.

# A second project, spelled differently from TEST_REMOTE_CANONICAL on purpose.
OTHER_REMOTE_URL = "git@github.com:org/other.git"
OTHER_REMOTE_CANONICAL = "github.com/org/other"
# The SAME project as the env repo's `origin`, spelled over HTTPS instead of scp-like.
SAME_PROJECT_HTTPS = "https://github.com/org/b.git"


def _log_text(cfg) -> str:
    return cfg.log_file.read_text(encoding="utf-8") if cfg.log_file.exists() else ""


def _expect_entry(env) -> None:
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201
    )


def test_r1_live_remote_without_capture_is_todays_behavior(env):
    """R1: `cwd` resolves, no captured value. Unchanged, and NO diagnostic."""
    env.register_projects()
    _expect_entry(env)
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    env.drop_marker("sess-r1")

    report = processor.run_once(cfg)

    assert _actions(report) == ["posted"]
    assert env.posted_entries()[0]["project_remote"] == TEST_REMOTE_CANONICAL
    assert "attribution_superseded" not in _log_text(cfg)
    assert "attribution_from_marker" not in _log_text(cfg)


def test_r2_capture_equal_after_normalization_is_not_a_divergence(env):
    """R2: the captured url spells the SAME project a different way (HTTPS vs scp-like).

    Equality is decided after `normalize()`, so this is NOT a divergence. Comparing the
    raw strings would fire `attribution_superseded` on every session of every repo whose
    clone url and captured url differ in form — noise that trains an operator to ignore
    the one line that matters."""
    env.register_projects()
    _expect_entry(env)
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    env.drop_marker("sess-r2", cwd_remote_url=SAME_PROJECT_HTTPS)

    report = processor.run_once(cfg)

    assert _actions(report) == ["posted"]
    assert env.posted_entries()[0]["project_remote"] == TEST_REMOTE_CANONICAL
    assert "attribution_superseded" not in _log_text(cfg)


def test_r3_live_remote_beats_a_differing_capture_and_says_so(env):
    """R3: both resolve and they disagree → LIVE decides, and the divergence is logged.

    This is the row that keeps a captured value from ever pulling a session back onto an
    old project when an ephemeral path is recycled."""
    env.register_projects(remotes=(TEST_REMOTE_CANONICAL, OTHER_REMOTE_CANONICAL))
    _expect_entry(env)
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    env.drop_marker("sess-r3", cwd_remote_url=OTHER_REMOTE_URL)

    report = processor.run_once(cfg)

    assert _actions(report) == ["posted"]
    # Both are registered, so a wrong choice would still POST — it fails on WHICH.
    assert env.posted_entries()[0]["project_remote"] == TEST_REMOTE_CANONICAL
    assert "attribution_superseded" in _log_text(cfg)


def test_r3_an_unusable_capture_beside_a_live_cwd_is_a_divergence_not_a_drop(env):
    """The R1-R3 `except ValueError` branch, which nothing else reaches.

    `cwd` resolves AND the capture is garbage. It cannot be shown EQUAL to the live
    value, so it counts as differing: live decides, one diagnostic, no drop. An
    implementation that swallowed the exception as `superseded = False` would return the
    same canonical and pass every other test in this file — this is the only assertion
    that separates the two, and the difference is whether a marker carrying garbage next
    to a working cwd is visible at all."""
    env.register_projects()
    _expect_entry(env)
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    env.drop_marker("sess-r3bad", cwd_remote_url="::::not a url::::")

    report = processor.run_once(cfg)

    assert _actions(report) == ["posted"]
    assert env.posted_entries()[0]["project_remote"] == TEST_REMOTE_CANONICAL
    assert "attribution_superseded" in _log_text(cfg), (
        "an unusable capture beside a working cwd must still be surfaced")


def test_r4_dead_cwd_falls_back_to_the_captured_remote(env, tmp_path):
    """R4: the fix. `cwd` is gone, the captured value is usable → it is used, and said."""
    env.register_projects(remotes=(OTHER_REMOTE_CANONICAL,))
    _expect_entry(env)
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    env.drop_marker("sess-r4", cwd=tmp_path / "deleted-worktree",
                    cwd_remote_url=OTHER_REMOTE_URL)

    report = processor.run_once(cfg)

    assert _actions(report) == ["posted"]
    assert env.posted_entries()[0]["project_remote"] == OTHER_REMOTE_CANONICAL
    assert "attribution_from_marker" in _log_text(cfg)


def test_r5_unusable_capture_drops_with_its_own_detail(env, tmp_path):
    """R5: present but unusable → drop, never a guess, and DISTINGUISHABLE from R6."""
    env.register_projects()
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    env.drop_marker("sess-r5", cwd=tmp_path / "deleted-worktree",
                    cwd_remote_url="not a remote url")

    report = processor.run_once(cfg)

    assert _actions(report) == ["dropped_no_remote"]
    assert report.outcomes[0].detail == "fallback not normalizable"
    assert env.posted_entries() == []


def test_r5_non_string_capture_is_a_drop_not_a_crash(env, tmp_path):
    """R5, totality leg. A marker is a file anyone can write; a JSON number where a url
    belongs must resolve to a disposition, not to an exception inside the marker loop."""
    env.register_projects()
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    env.drop_marker("sess-r5b", cwd=tmp_path / "deleted-worktree", cwd_remote_url=7)

    report = processor.run_once(cfg)

    assert _actions(report) == ["dropped_no_remote"]
    assert report.outcomes[0].detail == "fallback not normalizable"


def test_r6_no_capture_keeps_todays_detail_string(env, tmp_path):
    """R6: nothing to fall back to. Today's detail verbatim, so every log reader and
    runbook entry that names it stays correct."""
    env.register_projects()
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    env.drop_marker("sess-r6", cwd=tmp_path / "deleted-worktree")

    report = processor.run_once(cfg)

    assert _actions(report) == ["dropped_no_remote"]
    assert report.outcomes[0].detail == "no git remote origin"


def test_unusable_live_remote_does_not_fall_back_to_the_capture(env, tmp_path):
    """`cwd` RESOLVED and gave something unusable. That is a live answer, not a missing
    one, so it drops exactly as it does today — it does not reach past live truth for a
    stale capture. Preferring the capture here would be the guess the contract forbids."""
    env.register_projects(remotes=(TEST_REMOTE_CANONICAL, OTHER_REMOTE_CANONICAL))
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    broken = tmp_path / "broken_repo"
    broken.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=broken, check=True)
    subprocess.run(["git", "remote", "add", "origin", "::not a url::"],
                   cwd=broken, check=True)
    env.drop_marker("sess-livebad", cwd=broken, cwd_remote_url=OTHER_REMOTE_URL)

    report = processor.run_once(cfg)

    assert _actions(report) == ["dropped_no_remote"]
    assert report.outcomes[0].detail == "remote not normalizable"
    assert env.posted_entries() == []


def test_missing_transcript_drops_marker(env, tmp_path):
    env.register_projects()
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    env.drop_marker("sess-notx", transcript=tmp_path / "does_not_exist.jsonl")

    report = processor.run_once(cfg)

    assert _actions(report) == ["dropped_no_transcript"]
    assert not (env.home / "queue" / "sess-notx.json").exists()


# --- AdapterError → keep, tag it with adapter_error ------------------------------

def test_adapter_error_keeps_marker_and_tags_it(env, tmp_path):
    """A SCHEMA-INVALID line — one that parses and decodes but violates the contract.

    The fixture is deliberately not `{not valid json`: under P2.F1 that is a *torn*
    line (a crash artifact), and a trailing one at that, so the adapter holds it and
    the delta comes back empty instead of raising. Real corruption is a line the
    adapter can read and still refuses: a conversational `type` with no `uuid` and no
    `message` (frozen adapter rule 3). It must land on the P2.F2 discipline —
    attempts bounded, the diagnostic persisted on the marker, the pointer untouched.
    """
    env.register_projects()
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    bad = tmp_path / "corrupt.jsonl"
    bad.write_text('{"type":"user"}\n', encoding="utf-8")
    marker = env.drop_marker("sess-adapter", transcript=bad)

    report = processor.run_once(cfg)

    assert _actions(report) == ["kept_adapter_error"]
    assert marker.exists()
    on_disk = json.loads(marker.read_text())
    assert on_disk["adapter_error"]
    assert on_disk["attempts"] == 1               # P2.F2: bounded, not retried forever
    assert env.posted_entries() == []
    assert env.state().get(str(bad)) is None      # nothing confirmed → pointer parked


# --- Summarizer exit≠0 three times → failed/ -------------------------------------

def test_summarizer_fails_three_times_parks_in_failed(env):
    env.register_projects()
    cfg = env.config(env.make_stub(exit_code=1))
    marker = env.drop_marker("sess-fail")

    r1 = processor.run_once(cfg)
    assert _actions(r1) == ["kept_summarizer_failed"]
    assert json.loads(marker.read_text())["attempts"] == 1

    r2 = processor.run_once(cfg)
    assert _actions(r2) == ["kept_summarizer_failed"]
    assert json.loads(marker.read_text())["attempts"] == 2

    r3 = processor.run_once(cfg)
    assert _actions(r3) == ["failed_parked"]
    assert not marker.exists()
    parked = env.home / "failed" / "sess-fail.json"
    assert parked.exists()
    assert json.loads(parked.read_text())["attempts"] == 3
    assert env.posted_entries() == []


def test_unexpected_pipeline_failure_is_bounded_and_parks_at_max_attempts(env):
    env.register_projects()
    missing_summarizer = env.home / "missing-summarizer"
    cfg = env.config(str(missing_summarizer))
    marker = env.drop_marker("sess-pipeline")

    for expected in (1, 2):
        report = processor.run_once(cfg)
        assert _actions(report) == ["kept_pipeline_error"]
        assert report.errors == 1
        on_disk = json.loads(marker.read_text(encoding="utf-8"))
        assert on_disk["attempts"] == expected
        assert on_disk["pipeline_error"] == "pipeline failure: FileNotFoundError errno=2"
        assert str(missing_summarizer) not in report.outcomes[0].detail
        assert "\n" not in report.outcomes[0].detail

    report = processor.run_once(cfg)

    assert _actions(report) == ["failed_parked"]
    assert not marker.exists()
    parked = json.loads((env.home / "failed" / "sess-pipeline.json").read_text(
        encoding="utf-8"))
    assert parked["attempts"] == processor.MAX_ATTEMPTS
    assert parked["pipeline_error"] == "pipeline failure: FileNotFoundError errno=2"


def test_unwritable_processor_log_does_not_fail_captures_or_abort_the_pass(
        env, monkeypatch):
    import shutil

    env.register_projects(remotes=(TEST_REMOTE_CANONICAL, OTHER_REMOTE_CANONICAL))
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201
    )
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    transcript2 = env.transcript.parent / "transcript-log-2.jsonl"
    shutil.copyfile(env.transcript, transcript2)
    env.drop_marker("sess-log-1", cwd_remote_url=OTHER_REMOTE_URL)
    env.drop_marker("sess-log-2", transcript=transcript2, cwd_remote_url=OTHER_REMOTE_URL)

    real_open = Path.open

    def fail_log_open(path, *args, **kwargs):
        if path == cfg.log_file:
            raise OSError(28, "No space left on device")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_log_open)

    report = processor.run_once(cfg)

    assert _actions(report) == ["posted", "posted"]
    assert len(env.posted_entries()) == 2


# --- The hook replaces the marker DURING a failing pass (P2.F3, failure paths) ---
#
# The summarizer window is the long one — minutes — and `sessionend.sh` writes this
# queue without the lock. Every failure path that runs AFTER that window therefore
# acts on a file that may no longer be the marker this pass read: the attempts
# rewrite, the park into failed/ and the 404 registry_error stamp. Only the drop was
# guarded, so the named gap was a race driven through a FAILING pass.

def _replacing_stub(env, marker_path, fresh: dict, *, exit_code: int = 1,
                    stdout: str = "") -> str:
    """A summarizer stub that replaces the queue marker while it runs, then exits.

    The replacement is an `os.replace` of a uniquely named temp file — byte for byte
    what `sessionend.sh` does when a new session ends mid-pass.
    """
    path = env.home / "claude_replacing_stub.py"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, tempfile, pathlib\n"
        f"marker = pathlib.Path({str(marker_path)!r})\n"
        "fd, tmp = tempfile.mkstemp(dir=str(marker.parent), suffix='.tmp')\n"
        "with os.fdopen(fd, 'w') as fh:\n"
        f"    json.dump({fresh!r}, fh)\n"
        "os.replace(tmp, str(marker))\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path)


def _fresh_marker(env, session_id: str) -> dict:
    return {
        "session_id": session_id,
        "transcript_path": str(env.transcript),
        "cwd": str(env.repo),
        "reason": "prompt_input_exit",
        "marked_at": "2026-07-24T23:59:00Z",
        "hook_generation": 2,
    }


def test_pipeline_failure_does_not_stamp_a_marker_the_hook_replaced(env, monkeypatch):
    env.register_projects()
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    marker = env.drop_marker("sess-pipeline-race")
    fresh = _fresh_marker(env, "sess-pipeline-race")

    real_run = subprocess.run

    def replace_then_fail(cmd, *args, **kwargs):
        if cmd[0] == cfg.claude_bin:
            replacement = marker.with_suffix(".fresh")
            replacement.write_text(json.dumps(fresh), encoding="utf-8")
            replacement.replace(marker)
            raise OSError(5, "external summarizer I/O failure")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(processor.subprocess, "run", replace_then_fail)

    report = processor.run_once(cfg)

    assert _actions(report) == ["kept_pipeline_error"]
    assert "attempts=1 not persisted (marker replaced by the hook)" in report.outcomes[0].detail
    on_disk = json.loads(marker.read_text(encoding="utf-8"))
    assert on_disk["hook_generation"] == 2
    assert "attempts" not in on_disk
    assert "pipeline_error" not in on_disk


def test_summarizer_failure_does_not_stamp_a_marker_the_hook_replaced(env):
    """The attempts rewrite must not clobber a fresh capture with a stale payload.

    `_write_marker` is an unconditional `os.replace`: run on a marker the hook swapped
    in mid-pass, it overwrites the new session's transcript_path/cwd with the old
    one's and stamps it with an attempts count it never earned.
    """
    env.register_projects()
    marker = env.drop_marker("sess-race")
    fresh = _fresh_marker(env, "sess-race")
    cfg = env.config(_replacing_stub(env, marker, fresh))

    report = processor.run_once(cfg)

    assert _actions(report) == ["kept_summarizer_failed"]
    on_disk = json.loads(marker.read_text())
    assert on_disk["hook_generation"] == 2, "the hook's fresh marker was overwritten"
    assert "attempts" not in on_disk, "a fresh capture must start its own attempt count"


def test_failure_at_max_attempts_does_not_park_a_marker_the_hook_replaced(env):
    """Parking a fresh capture is the worst outcome of the three.

    `failed/` is a tombstone the sweep refuses to look past (AC-F2.2), so a hook
    marker moved there is not merely delayed — it is unreachable until a human clears
    the directory, for a failure it was never part of.
    """
    env.register_projects()
    marker = env.drop_marker("sess-race-park", attempts=processor.MAX_ATTEMPTS - 1)
    fresh = _fresh_marker(env, "sess-race-park")
    cfg = env.config(_replacing_stub(env, marker, fresh))

    report = processor.run_once(cfg)

    assert _actions(report) == ["kept_summarizer_failed"]
    assert marker.exists(), "the hook's fresh marker was parked in failed/"
    assert json.loads(marker.read_text())["hook_generation"] == 2
    assert not (env.home / "failed" / "sess-race-park.json").exists()


def test_registry_error_is_not_stamped_on_a_marker_the_hook_replaced(env):
    """Same window, the other rewrite: the 404 `registry_error` stamp."""
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"error": "project_not_found"}, status=404
    )
    marker = env.drop_marker("sess-race-404")
    fresh = _fresh_marker(env, "sess-race-404")
    cfg = env.config(
        _replacing_stub(env, marker, fresh, exit_code=0, stdout=CANNED_SUMMARY)
    )

    report = processor.run_once(cfg)

    assert _actions(report) == ["kept_registry_error"]
    on_disk = json.loads(marker.read_text())
    assert on_disk["hook_generation"] == 2, "the hook's fresh marker was overwritten"
    assert "registry_error" not in on_disk


def test_a_pathologically_nested_line_is_bounded_not_retried_forever(env, tmp_path):
    """`json.loads` raises RecursionError, which is not a ValueError.

    So it slips past the adapter's torn-line classifier AND past the bounded
    adapter-failure tuple, and lands in `run_once`'s catch-all — where the marker is
    kept with no attempt counted and no diagnostic, and every later pass reaches the
    same line and does the same thing. That is exactly the stranded-marker class P2.F2
    was built to eliminate, arriving through a door it did not cover.
    """
    env.register_projects()
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    deep = tmp_path / "deep.jsonl"
    deep.write_bytes(b"[" * 200_000 + b"]" * 200_000 + b"\n")
    marker = env.drop_marker("sess-deep", transcript=deep)

    report = processor.run_once(cfg)

    assert _actions(report) == ["kept_adapter_error"]
    on_disk = json.loads(marker.read_text())
    assert on_disk["attempts"] == 1                  # bounded: it will park, not strand
    assert on_disk["adapter_error"]
    assert env.posted_entries() == []


def test_a_successful_pass_clears_the_session_s_failed_tombstone(env):
    """`failed/<sid>.json` is a state, not a life sentence.

    The sweep refuses to enqueue any session that has a tombstone (AC-F2.2), and the
    tombstone was never removed by anything but a human. So one transient park — a
    summarizer that was down for 45 minutes, a transcript that was mid-write — made a
    session permanently invisible to the sweep, even after a later hook marker for the
    very same session had been summarized and posted successfully.
    """
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201
    )
    tombstone = env.home / "failed" / "sess-heal.json"

    # Park it: MAX_ATTEMPTS failing passes over the same marker.
    failing = env.config(env.make_stub(name="failing_stub.py", exit_code=1))
    env.drop_marker("sess-heal")
    for _ in range(processor.MAX_ATTEMPTS):
        processor.run_once(failing)
    assert tombstone.exists(), "fixture: the session must be parked first"

    # The next hook marker for the same session goes through cleanly.
    cfg = env.config(env.make_stub(name="happy_stub.py", stdout=CANNED_SUMMARY))
    env.drop_marker("sess-heal")
    report = processor.run_once(cfg)

    assert _actions(report) == ["posted"]
    assert not tombstone.exists(), (
        "the park outlived the healing: the sweep stays blocked on this session forever"
    )


def test_summarizer_timeout_increments_attempts(env):
    env.register_projects()
    cfg = env.config(env.make_stub(sleep=5, stdout=CANNED_SUMMARY), timeout_s=1)
    marker = env.drop_marker("sess-timeout")

    report = processor.run_once(cfg)

    assert _actions(report) == ["kept_summarizer_failed"]
    assert marker.exists()
    assert json.loads(marker.read_text())["attempts"] == 1
    assert env.posted_entries() == []


# --- POST 404 project_not_found → keep, registry_error ---------------------------

def test_post_404_keeps_marker_with_registry_error(env):
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"error": "project_not_found"}, status=404
    )
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    marker = env.drop_marker("sess-404")

    report = processor.run_once(cfg)

    assert _actions(report) == ["kept_registry_error"]
    assert marker.exists()
    assert json.loads(marker.read_text())["registry_error"] == "project_not_found"
    assert env.pointer() == {}


# --- POST 409 duplicate → advance the pointer (server idempotency) ---------------

def test_post_409_duplicate_advances_pointer(env):
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"error": "duplicate_entry"}, status=409
    )
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    env.drop_marker("sess-dup")

    report = processor.run_once(cfg)

    assert _actions(report) == ["duplicate"]
    wc, _ = _fixture_facts(env)
    assert env.pointer()["last_line"] == wc
    assert not (env.home / "queue" / "sess-dup.json").exists()


# --- Concurrent lock → exit without processing -----------------------------------

def test_concurrent_lock_exits_without_processing(env):
    env.register_projects()
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    marker = env.drop_marker("sess-lock")

    # A "second processor" holds the lock while we run run_once.
    cfg.ensure_dirs()
    holder = open(cfg.lock_path, "w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        report = processor.run_once(cfg)
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    assert report.lock_held is True
    assert report.outcomes == []
    assert marker.exists()                 # nothing was processed
    assert env.posted_entries() == []


# --- Order: oldest marker first --------------------------------------------------

def test_non_object_marker_does_not_starve_a_healthy_marker_in_the_same_pass(env):
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201
    )
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    bad = env.home / "queue" / "bad.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("[1, 2]", encoding="utf-8")
    env.drop_marker("sess-healthy", marked_at="2026-07-24T10:00:00Z")

    report = processor.run_once(cfg)

    assert "posted" in _actions(report)
    assert env.posted_entries()[0]["session_id"] == "sess-healthy"


def test_non_object_marker_is_parked_once_as_unreadable(env):
    env.register_projects()
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    marker = env.home / "queue" / "bad.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("[1, 2]", encoding="utf-8")

    first = processor.run_once(cfg)

    assert [(o.action, o.detail) for o in first.outcomes] == [
        ("failed_parked", "marker unreadable")
    ]
    assert not marker.exists()
    assert (env.home / "failed" / "bad.json").read_text(encoding="utf-8") == "[1, 2]"

    second = processor.run_once(cfg)
    assert second.outcomes == []


def test_marker_that_cannot_be_read_is_reported_without_being_parked(env, monkeypatch):
    env.register_projects()
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    marker = env.drop_marker("sess-io")
    real_open = Path.open
    marker_opens = 0

    def fail_after_listing(path, *args, **kwargs):
        nonlocal marker_opens
        if path == marker:
            marker_opens += 1
            if marker_opens >= 2:
                raise OSError(5, "transient marker I/O failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_after_listing)

    report = processor.run_once(cfg)

    assert [(o.action, o.detail) for o in report.outcomes] == [
        ("error", "marker unreadable")
    ]
    assert marker.exists()
    assert not (env.home / "failed" / marker.name).exists()


def test_non_string_marked_at_does_not_abort_the_pass(env):
    env.register_projects()
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    env.drop_marker("sess-number", marked_at=5, transcript=env.home / "missing-1.jsonl")
    env.drop_marker("sess-string", marked_at="2026-07-24T10:00:00Z",
                    transcript=env.home / "missing-2.jsonl")

    report = processor.run_once(cfg)

    assert sorted(_actions(report)) == ["dropped_no_transcript", "dropped_no_transcript"]


def test_markers_processed_oldest_first(env):
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201
    )
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    # two different transcripts so that both produce an entry
    import shutil
    tx2 = env.transcript.parent / "transcript2.jsonl"
    shutil.copyfile(env.transcript, tx2)
    env.drop_marker("sess-new", marked_at="2026-07-24T10:00:00Z")
    env.drop_marker("sess-old", marked_at="2026-07-24T08:00:00Z", transcript=tx2)

    report = processor.run_once(cfg)

    sessions = [o.session_id for o in report.outcomes]
    assert sessions == ["sess-old", "sess-new"]   # oldest (lower marked_at) first


# --- The recorded stat pair must be corroborated by the read (wave-e review) ------
# The pair at ``_advance_pointer`` is what lets the sweep skip a transcript without
# reading it, so it MUST describe the same bytes the delta read consumed. ``os.stat``
# and ``Path.open`` are two independent resolutions of the path: on a filesystem whose
# attribute and page caches revalidate separately (9p ``cache=loose``, some NFS
# setups) the stat can report a post-append size while the read still returns the
# pre-append content, and a rename-over between the two staples the old inode's pair
# to the new inode's pointer. Either way the pair would describe MORE content than the
# pointer covers, every later sweep would match it, and the extra content would never
# be re-read: permanent, silent loss. ``Delta.bytes_read`` is the corroboration.

def _short_read_delta(env, deficit: int = 1):
    """``read_delta`` that under-reports ``bytes_read`` — a read that saw fewer bytes
    than the stat around it reported (stale page cache, or a rename-over)."""
    real = read_delta

    def wrapper(transcript_path, last_line):
        delta = real(transcript_path, last_line)
        if str(transcript_path) == str(env.transcript):
            delta.bytes_read -= deficit
        return delta

    return wrapper


# The full record `_advance_pointer` writes: all four together, or none at all.
_STAT_FIELDS = ("stat_dev", "stat_ino", "stat_mtime_ns", "stat_size")


def test_stat_pair_is_recorded_when_the_read_corroborates_it(env):
    """The normal case: nothing raced, ``bytes_read`` == the pre-read ``st_size``, so
    the pair travels with the pointer and the sweep can skip the file."""
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201
    )
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    st = env.transcript.stat()
    env.drop_marker("sess-corroborated")

    processor.run_once(cfg)

    entry = env.pointer()
    assert entry["stat_mtime_ns"] == st.st_mtime_ns
    assert entry["stat_size"] == st.st_size
    # The inode half (P2.F4 amendment): recording it at all also pins that the gate
    # above compares `bytes_read` against the SIZE element of the widened tuple.
    assert entry["stat_dev"] == st.st_dev and entry["stat_ino"] == st.st_ino
    assert sweep._stat_unchanged({str(env.transcript): entry}, str(env.transcript),
                                 env.transcript) is True


def test_a_read_that_saw_fewer_bytes_than_the_stat_records_no_pair(env, monkeypatch):
    """The stat says N bytes, the read consumed N-1: the pair would over-describe the
    pointer and the sweep would skip the missing tail forever. Record nothing — the
    sweep falls back to the full check, at the cost of one wasted read."""
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201
    )
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    monkeypatch.setattr(processor, "read_delta", _short_read_delta(env))
    env.drop_marker("sess-uncorroborated")

    report = processor.run_once(cfg)

    assert _actions(report) == ["posted"]          # the pass still succeeds
    entry = env.pointer()
    wc, _ = _fixture_facts(env)
    assert entry["last_line"] == wc                # the pointer still advances
    assert not any(f in entry for f in _STAT_FIELDS), (
        "an uncorroborated stat record must not be recorded — not even partially: the "
        "sweep would skip the bytes the read never saw")
    assert sweep._stat_unchanged({str(env.transcript): entry}, str(env.transcript),
                                 env.transcript) is False


def test_an_uncorroborated_pair_is_not_recorded_on_the_empty_delta_path_either(
        env, monkeypatch, tmp_path):
    """The empty-delta drop advances the pointer through the same helper, so it has to
    honour the same gate — otherwise the cheapest path is the leaky one."""
    env.register_projects()
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    env.transcript.write_text(
        json.dumps({"type": "file-history-snapshot", "uuid": "m1"}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(processor, "read_delta", _short_read_delta(env))
    env.drop_marker("sess-emptydelta")

    report = processor.run_once(cfg)

    assert _actions(report) == ["dropped_empty_delta"]
    entry = env.pointer()
    assert entry["last_line"] == 1
    assert not any(f in entry for f in _STAT_FIELDS)


def test_a_stale_pair_from_an_earlier_pass_does_not_survive_an_uncorroborated_read(
        env, monkeypatch):
    """``_advance_pointer`` rebuilds the entry, so the gate must also ERASE the pair a
    previous corroborated pass left behind — a kept-and-stale pair matches nothing
    useful and can only mislead."""
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201
    )
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY))
    env.drop_marker("sess-stale-1")
    processor.run_once(cfg)
    assert "stat_size" in env.pointer()

    with env.transcript.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "uuid": "u-late",
                             "message": {"role": "user", "content": "una línea más"}})
                 + "\n")
    monkeypatch.setattr(processor, "read_delta", _short_read_delta(env))
    env.drop_marker("sess-stale-2")
    processor.run_once(cfg)

    entry = env.pointer()
    assert not any(f in entry for f in _STAT_FIELDS)


# --- D9: the local opt-in gate (registered on the server AND enabled here) -------------

def test_opt_in_registered_but_undecided_drops_marker(env):
    """Registered by a teammate, never enabled on this machine → skipped, never
    summarized, marker resolved, pointer untouched."""
    env.register_projects()
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY), capture_mode="opt_in")
    env.drop_marker("sess-undecided")

    report = processor.run_once(cfg)

    assert _actions(report) == ["dropped_not_enabled"]
    assert report.skipped == 1 and report.errors == 0
    assert env.posted_entries() == []
    assert not (env.home / "queue" / "sess-undecided.json").exists()
    assert env.state() == {}
    log = (env.home / "log" / "processor.log").read_text(encoding="utf-8")
    assert f"\tdropped_not_enabled\t{TEST_REMOTE_CANONICAL}" in log


def test_opt_in_disabled_here_drops_marker(env):
    env.register_projects()
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY), capture_mode="opt_in")
    env.set_local_state("disabled")
    env.drop_marker("sess-disabled")

    report = processor.run_once(cfg)

    assert _actions(report) == ["dropped_not_enabled"]
    assert env.posted_entries() == []


def test_opt_in_enabled_here_posts(env):
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 43}, status=201
    )
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY), capture_mode="opt_in")
    env.set_local_state("enabled")
    env.drop_marker("sess-enabled")

    report = processor.run_once(cfg)

    assert _actions(report) == ["posted"]
    assert len(env.posted_entries()) == 1


# --- D9 amendment (2026-09-30): capture starts when the project was enabled here ------
ENABLED_AT = "2026-09-30T09:00:00Z"


def _turn(uuid: str, role: str, text: str, timestamp: str | None) -> str:
    line = {"type": role, "uuid": uuid, "message": {"role": role, "content": text}}
    if timestamp is not None:
        line["timestamp"] = timestamp
    return json.dumps(line) + "\n"


def _write_turns(env, *turns, mode: str = "w") -> None:
    with env.transcript.open(mode, encoding="utf-8") as fh:
        for turn in turns:
            fh.write(_turn(*turn))


def test_opt_in_capture_starts_when_the_project_was_enabled_here(env):
    """A session that was already running at `loom enable` contributes only what came
    after it; the entry's range starts at the last turn left out."""
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 44}, status=201
    )
    cfg = env.config(env.make_stub(echo_stdin=True), capture_mode="opt_in")
    env.set_local_state("enabled", since=ENABLED_AT)
    _write_turns(env,
                 ("u-old", "user", "OLD_TOPIC_BEFORE_ENABLE", "2026-09-30T08:59:58.100Z"),
                 ("a-old", "assistant", "OLD_ANSWER_BEFORE_ENABLE", "2026-09-30T08:59:59.900Z"),
                 ("u-new", "user", "NEW_TOPIC_AFTER_ENABLE", "2026-09-30T09:00:00.000Z"),
                 ("a-new", "assistant", "NEW_ANSWER_AFTER_ENABLE", "2026-09-30T09:00:03.000Z"))
    env.drop_marker("sess-cut")

    report = processor.run_once(cfg)

    assert _actions(report) == ["posted"]
    body = env.posted_entries()[0]
    assert "NEW_TOPIC_AFTER_ENABLE" in body["content_md"]
    assert "NEW_ANSWER_AFTER_ENABLE" in body["content_md"]
    assert "OLD_" not in body["content_md"]
    assert body["covered_from"] == "a-old"
    assert body["covered_to"] == "a-new"
    assert env.pointer()["last_line"] == 4
    log = (env.home / "log" / "processor.log").read_text(encoding="utf-8")
    assert "\tbefore_enable_skipped\tmessages=2" in log


def test_opt_in_session_entirely_before_enable_is_never_summarized(env):
    """History is not backfilled: a transcript older than the enable is read, the
    pointer moves past it, and nothing is summarized or sent. What is appended to it
    later is captured, and only that."""
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 45}, status=201
    )
    # A summarizer that fails: reaching it would turn the drop into kept_summarizer_failed.
    cfg = env.config(env.make_stub(exit_code=1), capture_mode="opt_in")
    env.set_local_state("enabled", since=ENABLED_AT)
    _write_turns(env,
                 ("u-1", "user", "OLD_ONE", "2026-09-12T10:00:00Z"),
                 ("a-1", "assistant", "OLD_TWO", "2026-09-12T10:00:05Z"))
    env.drop_marker("sess-history")

    report = processor.run_once(cfg)

    assert _actions(report) == ["dropped_before_enable"]
    assert report.skipped == 1 and report.errors == 0
    assert env.posted_entries() == []
    assert not (env.home / "queue" / "sess-history.json").exists()
    assert env.pointer()["last_line"] == 2
    assert env.pointer()["last_uuid"] == "a-1"

    # The session is resumed after the enable: only the new turn is summarized.
    cfg = env.config(env.make_stub(name="echo.py", echo_stdin=True), capture_mode="opt_in")
    _write_turns(env, ("u-2", "user", "RESUMED_AFTER_ENABLE", "2026-09-30T10:00:00Z"),
                 mode="a")
    env.drop_marker("sess-history")

    report = processor.run_once(cfg)

    assert _actions(report) == ["posted"]
    body = env.posted_entries()[0]
    assert "RESUMED_AFTER_ENABLE" in body["content_md"] and "OLD_" not in body["content_md"]
    assert body["covered_from"] == "a-1"


def test_opt_in_turns_without_a_timestamp_are_captured(env):
    """The cutoff drops only what is PROVABLY older than the enable. A turn with no
    usable timestamp ends the dropped run and is kept with everything after it —
    the behaviour before the cutoff existed."""
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 46}, status=201
    )
    cfg = env.config(env.make_stub(echo_stdin=True), capture_mode="opt_in")
    env.set_local_state("enabled", since=ENABLED_AT)
    _write_turns(env,
                 ("u-old", "user", "OLD_DROPPED", "2026-09-29T10:00:00Z"),
                 ("u-none", "user", "NO_TIMESTAMP_KEPT", None),
                 ("a-bad", "assistant", "UNPARSEABLE_TIMESTAMP_KEPT", "yesterday"),
                 ("a-late", "assistant", "OLD_BUT_AFTER_THE_RUN", "2026-09-29T10:00:09Z"))
    env.drop_marker("sess-nots")

    report = processor.run_once(cfg)

    assert _actions(report) == ["posted"]
    body = env.posted_entries()[0]
    assert "OLD_DROPPED" not in body["content_md"]
    for kept in ("NO_TIMESTAMP_KEPT", "UNPARSEABLE_TIMESTAMP_KEPT", "OLD_BUT_AFTER_THE_RUN"):
        assert kept in body["content_md"]
    assert body["covered_from"] == "u-old"


def test_all_registered_mode_has_no_capture_cutoff(env):
    """Legacy mode (no capture_mode key) never had a per-machine enable, so a stale
    projects_local.json must not start cutting its captures."""
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 47}, status=201
    )
    cfg = env.config(env.make_stub(echo_stdin=True))
    env.set_local_state("enabled", since=ENABLED_AT)
    _write_turns(env, ("u-old", "user", "LEGACY_OLD_TURN", "2026-09-12T10:00:00Z"))
    env.drop_marker("sess-legacy")

    report = processor.run_once(cfg)

    assert _actions(report) == ["posted"]
    assert "LEGACY_OLD_TURN" in env.posted_entries()[0]["content_md"]


def test_opt_in_unregistered_stays_not_registered(env):
    """The second gate is evaluated after the first: an unregistered project never
    produces the new drop, so nothing about it — not even a log line — appears."""
    env.register_projects(remotes=("github.com/org/other",))
    cfg = env.config(env.make_stub(stdout=CANNED_SUMMARY), capture_mode="opt_in")
    env.drop_marker("sess-unreg2")

    report = processor.run_once(cfg)

    assert _actions(report) == ["dropped_not_registered"]
    log = (env.home / "log" / "processor.log").read_text(encoding="utf-8")
    assert "dropped_not_enabled" not in log
