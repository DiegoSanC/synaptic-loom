"""FROZEN GOAL — issue #4, project attribution survives an ephemeral worktree.

Read-only to implementers (`test/waves/README.md`, `AGENTS.md` §12): extend by ADDING
cases, never by weakening one of these assertions. Authored 2026-09-13, before any
implementation, from `contract/ATTRIBUTION.md` — which is the specification these
assertions are derived from, not a description of code that exists.

BOUNDARY. Everything this wave owns is real: a real `git init` repo, a real
`git worktree add` linked worktree, a real `rm -rf` of it, the real
`client/hooks/sessionend.sh` run as a subprocess against the RECORDED SessionEnd payload
(`contract/sessionend_payload.json`), and the real `processor.run_once`. Only the two
seams this wave does not own are simulated: the Claude CLI (a stub binary) and the central
server (pytest-httpserver). The marker is never hand-written where the hook could write it
— AC2 is specifically the claim that the HOOK is the capture mechanism, and a hand-written
marker would assert the processor against a fixture instead of against the hook.

INDEPENDENT ORACLE. Every expected identity below is a LITERAL (`github.com/...`), never
recomputed by calling `normalize()`. An oracle that derives its expectation from the
function under test proves only that the function is self-consistent.

ADVERSARIAL CASES. Three of these exist to break a plausible-but-wrong implementation:
  - `test_reused_worktree_path_is_not_attributed_to_the_dead_session` (AC3a) breaks any
    implementation that prefers the captured value over the live one. BOTH projects are
    registered, so a wrong choice still passes the registry gate and still POSTs — it
    fails on WHICH project, which is the only thing that matters.
  - `test_unusable_fallback_does_not_starve_the_next_marker` (AC4b) breaks a helper that
    raises instead of returning None. `run_once` isolates each marker, so the raise does
    not abort the pass — it converts a disposition the contract SPECIFIES into an
    undiagnosed `error`, on every pass, forever. The bad marker is ordered first by
    `marked_at` so the marker behind it is asserted too.
  - `test_tokenized_remote_never_reaches_disk` (AC8) breaks the naive version of this
    feature, which writes the raw `origin` verbatim and leaves a live credential at rest
    in `queue/`, and indefinitely in `failed/` after a park.

Run: .venv/bin/python -m pytest test/waves/n/test_goal_issue4_attribution.py -q
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _goalkit as kit  # noqa: E402

from loom import processor  # noqa: E402
from loom.config import Config  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOK_SCRIPT = REPO_ROOT / "client" / "hooks" / "sessionend.sh"
PAYLOAD_FILE = REPO_ROOT / "contract" / "sessionend_payload.json"
RECORDED_PAYLOAD = json.loads(PAYLOAD_FILE.read_text(encoding="utf-8"))

# --------------------------------------------------------------------------- #
# Independent literals. NEVER derived by calling normalize().                  #
# --------------------------------------------------------------------------- #
ALPHA_URL = "git@github.com:example-group/wave-n-alpha.git"
ALPHA_CANONICAL = "github.com/example-group/wave-n-alpha"

BETA_URL = "git@github.com:example-group/wave-n-beta.git"
BETA_CANONICAL = "github.com/example-group/wave-n-beta"

# Never registered on the stub server — AC5a's fail-closed leg.
STRANGER_URL = "git@github.com:example-group/wave-n-stranger.git"

# AC8. The userinfo span is deliberately LOW-ENTROPY and self-describing: this file is
# committed, and CI gates on gitleaks 8.30.1 (`AGENTS.md` §Environment). The assertion is
# "no userinfo substring survives into the marker", which does not depend on the secret
# being realistic — so making it realistic would buy nothing and break the suite.
TOKEN_USERINFO = "x-access-token:redact-me-0000"
TOKEN_URL = f"https://{TOKEN_USERINFO}@github.com/example-group/wave-n-token.git"
TOKEN_CANONICAL = "github.com/example-group/wave-n-token"
TOKEN_REDACTED = "https://github.com/example-group/wave-n-token.git"

# AC8, the forms a single anchored `scheme://userinfo@` pattern gets WRONG. Added
# 2026-09-14 after a security review demonstrated both against the first implementation.
# Extending a frozen wave by ADDING cases is the sanctioned way to strengthen it
# (`test/waves/README.md`, `AGENTS.md` §12); nothing here weakens an existing assertion.
#
#   compound scheme — `git remote get-url` returns whatever is in .git/config, with NO
#   scheme validation, and `url.<base>.insteadOf` rewriting can produce exactly this at
#   get-url time. A pattern anchored on `^[a-zA-Z0-9+.-]+://` never matches `git::https://`
#   (the scheme run cannot contain `:`), so the credential passes through untouched.
#
#   multi-`@` userinfo — a non-greedy stop at the FIRST `@` strips only the first span and
#   leaves the remainder of the secret sitting in the marker.
COMPOUND_USERINFO = "x-access-token:redact-me-compound"
COMPOUND_URL = (f"git::https://{COMPOUND_USERINFO}@github.com/"
                "example-group/wave-n-token.git")
COMPOUND_REDACTED = "git::https://github.com/example-group/wave-n-token.git"

MULTI_AT_USERINFO = "user:redact-me-a@redact-me-b"
MULTI_AT_URL = (f"https://{MULTI_AT_USERINFO}@github.com/"
                "example-group/wave-n-token.git")
MULTI_AT_REDACTED = "https://github.com/example-group/wave-n-token.git"

CANNED_SUMMARY = (
    "## Contexto\nWorktree efímero.\n"
    "## Decisiones\n- **Capturar el remote en el hook** — el cwd ya no existe. "
    "Evidencia: sessionend.sh. Estado: vigente.\n"
    "## Pendientes\n- ninguno\n"
)

# Every binary the hook shells out to, INCLUDING git (which this feature adds). AC7's
# "git absent" leg builds this same list minus git — an otherwise-complete PATH.
HOOK_TOOLS = ("bash", "sh", "env", "jq", "mkdir", "date", "cat", "mv", "rm",
              "setsid", "python3", "git")


# --------------------------------------------------------------------------- #
# Real boundaries                                                              #
# --------------------------------------------------------------------------- #
def _git(*args: str, cwd: Path | str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.email=goal@wave-n.test", "-c", "user.name=wave-n",
         "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=True,
    )


def make_repo(path: Path, remote_url: str) -> Path:
    """A REAL git repo with one commit and a real ``origin`` — enough to add a worktree."""
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", str(path))
    (path / "README.md").write_text("wave n\n", encoding="utf-8")
    _git("add", "README.md", cwd=path)
    _git("commit", "-qm", "init", cwd=path)
    _git("remote", "add", "origin", remote_url, cwd=path)
    return path


def add_worktree(repo: Path, wt: Path, branch: str = "agent") -> Path:
    """A REAL linked worktree. It shares ``origin`` with ``repo`` and dies with `rm -rf`."""
    _git("worktree", "add", "-q", "-b", branch, str(wt), cwd=repo)
    return wt


def delete_worktree(wt: Path) -> None:
    """Exactly what an agent harness does: unlink the directory. No `git worktree remove`,
    no prune — the repo is left believing the worktree still exists, which is the real
    post-condition this wave is about."""
    shutil.rmtree(wt)
    assert not wt.exists()


def fakebin(tmp_path: Path, *, omit: tuple[str, ...] = ()) -> Path:
    """A PATH directory holding ``HOOK_TOOLS`` minus ``omit``."""
    name = "fakebin_without_" + "_".join(omit) if omit else "fakebin_full"
    b = tmp_path / name
    b.mkdir(exist_ok=True)
    for tool in HOOK_TOOLS:
        if tool in omit or (b / tool).exists():
            continue
        real = shutil.which(tool)
        if real:
            (b / tool).symlink_to(real)
    for tool in omit:
        assert shutil.which(tool, path=str(b)) is None, (
            f"{tool} must NOT be reachable through this PATH — the shim is the point")
    return b


def run_hook(loom_home: Path, payload: dict, *, path: str | None = None,
             timeout: float = 30.0) -> subprocess.CompletedProcess:
    """The REAL hook, as a subprocess, exactly as Claude Code invokes it.

    ``$LOOM_HOME/venv/bin/python`` is deliberately absent, so the hook takes its
    "kick skipped" branch: the marker is written and NOTHING starts processing it. That
    is what lets the worktree be deleted strictly between the hook and the first pass
    (AC2) instead of racing a background processor.
    """
    env = {**os.environ, "HOME": str(loom_home.parent), "LOOM_HOME": str(loom_home)}
    env.pop("LOOM_SUMMARIZER", None)
    if path is not None:
        env["PATH"] = path
    return subprocess.run(["bash", str(HOOK_SCRIPT)], input=json.dumps(payload),
                          text=True, capture_output=True, env=env, timeout=timeout)


def payload_for(session_id: str, transcript_path: Path, cwd: Path | str) -> dict:
    """The RECORDED SessionEnd payload with only the three per-session fields swapped.

    Built from `contract/sessionend_payload.json` rather than hand-rolled so that a
    future Claude Code payload change reaches this wave through the recorded fixture the
    contract test already guards, and not through a shape invented here.
    """
    return {**RECORDED_PAYLOAD, "session_id": session_id,
            "transcript_path": str(transcript_path), "cwd": str(cwd)}


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
def mk_cfg(tmp_path: Path, server_url: str, *, claude_stdout: str = CANNED_SUMMARY,
           claude_exit: int = 0) -> Config:
    home = kit.loom_home(tmp_path)
    stub = kit.make_claude_stub(tmp_path / "claude_stub.py", stdout=claude_stdout,
                                exit_code=claude_exit)
    return Config(
        loom_home=home, server_url=server_url, token="wave-n-token",
        claude_bin=stub, prompt_path=home / "prompts" / "summarizer_v1.md",
        # The sweep is OFF: this wave is about the hook→marker→processor path, and a
        # sweep pass would enqueue synthetic markers that are not what is under test.
        sweep_enabled=False,
        scanner_engine="gitleaks",
    )


def wire_server(httpserver, *canonicals: str, entry_status: int = 201):
    httpserver.expect_request("/api/projects", method="GET").respond_with_json(
        {"projects": [
            {"id": i, "canonical_remote": c, "name": c.rsplit("/", 1)[-1],
             "created_by": "goal", "created_at": "2026-09-01T00:00:00Z"}
            for i, c in enumerate(canonicals, start=1)]}
    )
    httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=entry_status)


def posted(httpserver) -> list[dict]:
    return [r.get_json() for r, _ in httpserver.log
            if r.method == "POST" and r.path == "/api/entries"]


def transcript_for(tmp_path: Path, session_id: str, cwd: Path) -> Path:
    """The transcript lives OUTSIDE the worktree — as it really does, under
    ``~/.claude/projects/<encoded-cwd>/``. That is why it survives the deletion and why
    the capture is recoverable at all."""
    path = tmp_path / "transcripts" / f"enc-{session_id}" / f"{session_id}.jsonl"
    return kit.write_transcript(path, kit.transcript_lines(str(cwd), session_id))


def marker_of(cfg: Config, session_id: str) -> dict:
    return json.loads((cfg.queue_dir / f"{session_id}.json").read_text(encoding="utf-8"))


def log_text(cfg: Config) -> str:
    return cfg.log_file.read_text(encoding="utf-8") if cfg.log_file.exists() else ""


def actions(report) -> list[str]:
    return [o.action for o in report.outcomes]


# =========================================================================== #
# AC1 + AC2 — the fix, end to end, through the real hook                      #
# =========================================================================== #
def test_deleted_worktree_session_still_reaches_its_project(tmp_path, httpserver):
    """AC1 + AC2. The worktree is created, used, closed through the REAL hook, and then
    DELETED strictly before the first processor pass. The session must still be filed
    under its own project."""
    sid = "wave-n-ac1"
    cfg = mk_cfg(tmp_path, httpserver.url_for(""))
    wire_server(httpserver, ALPHA_CANONICAL)

    repo = make_repo(tmp_path / "alpha", ALPHA_URL)
    wt = add_worktree(repo, tmp_path / "wt-alpha")
    transcript = transcript_for(tmp_path, sid, wt)

    proc = run_hook(cfg.loom_home, payload_for(sid, transcript, wt))
    assert proc.returncode == 0, proc.stderr

    # The capture happened while the worktree existed — this is the whole mechanism.
    marker = marker_of(cfg, sid)
    assert marker["cwd"] == str(wt)
    assert marker["cwd_remote_url"] == ALPHA_URL, (
        "the hook must capture the RAW origin url of the session's cwd while it exists")

    # AC2: the deletion lands strictly between the hook and the first pass.
    delete_worktree(wt)
    assert processor._git_remote(str(wt)) is None, (
        "precondition: the live remote must be genuinely unresolvable, or this wave "
        "proves nothing")

    report = processor.run_once(cfg)

    entries = posted(httpserver)
    assert len(entries) == 1, f"expected exactly one POST; outcomes={report.outcomes}"
    assert entries[0]["session_id"] == sid
    assert entries[0]["project_remote"] == ALPHA_CANONICAL
    assert "attribution_from_marker" in log_text(cfg), (
        "R4 must be visible in the log: a fallback attribution is never silent")
    assert not list(cfg.queue_dir.glob("*.json")), "the marker must be consumed"


# =========================================================================== #
# AC3a / AC3b — live remote always wins (R3)                                  #
# =========================================================================== #
def test_reused_worktree_path_is_not_attributed_to_the_dead_session(tmp_path, httpserver):
    """AC3a — THE adversarial case. Path reuse: alpha's worktree is deleted and a BETA
    worktree is created at the same path before the pass.

    Both projects are registered, so an implementation that prefers the captured value
    still passes the registry gate and still POSTs. It fails here on WHICH project — the
    only difference that matters, and the one a weaker oracle would miss."""
    sid = "wave-n-ac3a"
    cfg = mk_cfg(tmp_path, httpserver.url_for(""))
    wire_server(httpserver, ALPHA_CANONICAL, BETA_CANONICAL)

    shared_path = tmp_path / "wt-reused"
    alpha = make_repo(tmp_path / "alpha", ALPHA_URL)
    wt = add_worktree(alpha, shared_path)
    transcript = transcript_for(tmp_path, sid, wt)

    assert run_hook(cfg.loom_home, payload_for(sid, transcript, wt)).returncode == 0
    assert marker_of(cfg, sid)["cwd_remote_url"] == ALPHA_URL

    # The path is recycled for a DIFFERENT repo, exactly as an agent harness recycles it.
    delete_worktree(shared_path)
    beta = make_repo(tmp_path / "beta", BETA_URL)
    add_worktree(beta, shared_path)
    assert processor._git_remote(str(shared_path)) == BETA_URL

    report = processor.run_once(cfg)

    entries = posted(httpserver)
    assert len(entries) == 1, f"outcomes={report.outcomes}"
    assert entries[0]["project_remote"] == BETA_CANONICAL, (
        "R3: the LIVE remote decides. The captured value may never pull a session back "
        "onto the project whose worktree used to live at this path")
    assert entries[0]["project_remote"] != ALPHA_CANONICAL
    assert "attribution_superseded" in log_text(cfg), (
        "R3 divergence must be VISIBLE — silent supersession is the failure mode "
        "contract/ATTRIBUTION.md §6 L-2 exists to make observable")


def test_renamed_remote_is_decided_by_the_live_value(tmp_path, httpserver):
    """AC3b. `origin` is rewritten between SessionEnd and the pass; the worktree still
    exists. A genuinely renamed repository must keep working with no intervention."""
    sid = "wave-n-ac3b"
    cfg = mk_cfg(tmp_path, httpserver.url_for(""))
    wire_server(httpserver, ALPHA_CANONICAL, BETA_CANONICAL)

    repo = make_repo(tmp_path / "alpha", ALPHA_URL)
    wt = add_worktree(repo, tmp_path / "wt-alpha")
    transcript = transcript_for(tmp_path, sid, wt)

    assert run_hook(cfg.loom_home, payload_for(sid, transcript, wt)).returncode == 0
    assert marker_of(cfg, sid)["cwd_remote_url"] == ALPHA_URL

    _git("remote", "set-url", "origin", BETA_URL, cwd=repo)
    assert processor._git_remote(str(wt)) == BETA_URL

    report = processor.run_once(cfg)

    entries = posted(httpserver)
    assert len(entries) == 1, f"outcomes={report.outcomes}"
    assert entries[0]["project_remote"] == BETA_CANONICAL
    assert "attribution_superseded" in log_text(cfg)


def test_live_remote_path_is_byte_for_byte_unchanged(tmp_path, httpserver):
    """AC6, wave-local half. R1/R2: when `cwd` resolves and the captured value is absent
    or agrees, the disposition and the identity are exactly today's. The fallback widens
    WHICH sessions reach the gate; it must not perturb the ones that already did."""
    cfg = mk_cfg(tmp_path, httpserver.url_for(""))
    wire_server(httpserver, ALPHA_CANONICAL)

    repo = make_repo(tmp_path / "alpha", ALPHA_URL)
    wt = add_worktree(repo, tmp_path / "wt-alpha")

    # R2: captured present and equal (the hook wrote it; the worktree is still alive).
    sid = "wave-n-ac6-r2"
    transcript = transcript_for(tmp_path, sid, wt)
    assert run_hook(cfg.loom_home, payload_for(sid, transcript, wt)).returncode == 0
    assert marker_of(cfg, sid)["cwd_remote_url"] == ALPHA_URL
    report = processor.run_once(cfg)
    assert actions(report) == ["posted"], f"outcomes={report.outcomes}"
    assert posted(httpserver)[0]["project_remote"] == ALPHA_CANONICAL

    # R1: captured ABSENT (a pre-fix hook), cwd alive. Identical outcome, no diagnostic.
    sid2 = "wave-n-ac6-r1"
    transcript2 = transcript_for(tmp_path, sid2, wt)
    cfg.ensure_dirs()
    (cfg.queue_dir / f"{sid2}.json").write_text(json.dumps({
        "session_id": sid2, "transcript_path": str(transcript2), "cwd": str(wt),
        "reason": "prompt_input_exit", "marked_at": kit.utc_now_iso()}), encoding="utf-8")
    report2 = processor.run_once(cfg)
    assert actions(report2) == ["posted"], f"outcomes={report2.outcomes}"
    assert posted(httpserver)[1]["project_remote"] == ALPHA_CANONICAL
    assert "attribution_superseded" not in log_text(cfg), (
        "R1/R2 must emit NO divergence diagnostic — a line on every healthy session is "
        "noise that trains an operator to ignore the one that matters")
    assert "attribution_from_marker" not in log_text(cfg), (
        "R1/R2 resolve from the LIVE remote; the fallback was never used")


# =========================================================================== #
# AC4a / AC4b — absent and unusable identity fail CLOSED                      #
# =========================================================================== #
def test_pre_fix_marker_with_dead_cwd_is_dropped_not_guessed(tmp_path, httpserver):
    """AC4a. A marker written by a hook that predates this feature carries no
    `cwd_remote_url`. With the worktree gone there is no identity at all — and the
    session is dropped, loudly, never attributed to a guess (R6)."""
    sid = "wave-n-ac4a"
    cfg = mk_cfg(tmp_path, httpserver.url_for(""))
    wire_server(httpserver, ALPHA_CANONICAL)

    repo = make_repo(tmp_path / "alpha", ALPHA_URL)
    wt = add_worktree(repo, tmp_path / "wt-alpha")
    transcript = transcript_for(tmp_path, sid, wt)
    cfg.ensure_dirs()
    # Hand-written ON PURPOSE: this is the one case the current hook cannot produce.
    (cfg.queue_dir / f"{sid}.json").write_text(json.dumps({
        "session_id": sid, "transcript_path": str(transcript), "cwd": str(wt),
        "reason": "prompt_input_exit", "marked_at": kit.utc_now_iso()}), encoding="utf-8")
    delete_worktree(wt)

    report = processor.run_once(cfg)

    assert actions(report) == ["dropped_no_remote"], f"outcomes={report.outcomes}"
    assert posted(httpserver) == []
    assert sid in log_text(cfg), "the drop must name the session it lost"
    assert "no git remote origin" in log_text(cfg), "R6 keeps today's detail string"


@pytest.mark.parametrize("bad_value, label", [
    ("not a url at all", "unnormalizable string"),
    (12345, "not a str"),
    ({"url": ALPHA_URL}, "not a str (object)"),
    ("", "empty string"),
])
def test_unusable_fallback_does_not_starve_the_next_marker(tmp_path, httpserver,
                                                           bad_value, label):
    """AC4b — adversarial. An unusable `cwd_remote_url` must resolve to a DROP, never to
    an exception.

    `run_once` wraps each marker in its own handler, so a raise here does NOT abort the
    pass — which is precisely why the disposition assertion below is the load-bearing
    one: a helper that raises turns `dropped_no_remote` + the R5 detail into an
    undiagnosed `error`, and does so on every pass over this marker, forever. The bad
    marker is ordered FIRST (`_iter_markers` sorts by `marked_at`) and the healthy one
    behind it is asserted too, so a future change that DOES break that isolation is
    caught here rather than in production."""
    bad_sid, good_sid = "wave-n-ac4b-bad", "wave-n-ac4b-good"
    cfg = mk_cfg(tmp_path, httpserver.url_for(""))
    wire_server(httpserver, ALPHA_CANONICAL)

    repo = make_repo(tmp_path / "alpha", ALPHA_URL)
    dead = add_worktree(repo, tmp_path / "wt-dead")
    alive = add_worktree(repo, tmp_path / "wt-alive", branch="alive")
    bad_transcript = transcript_for(tmp_path, bad_sid, dead)
    good_transcript = transcript_for(tmp_path, good_sid, alive)
    cfg.ensure_dirs()

    (cfg.queue_dir / f"{bad_sid}.json").write_text(json.dumps({
        "session_id": bad_sid, "transcript_path": str(bad_transcript), "cwd": str(dead),
        "reason": "prompt_input_exit", "marked_at": "2026-09-13T00:00:00Z",
        "cwd_remote_url": bad_value}), encoding="utf-8")
    (cfg.queue_dir / f"{good_sid}.json").write_text(json.dumps({
        "session_id": good_sid, "transcript_path": str(good_transcript), "cwd": str(alive),
        "reason": "prompt_input_exit", "marked_at": "2026-09-13T00:00:01Z"}),
        encoding="utf-8")
    delete_worktree(dead)

    report = processor.run_once(cfg)

    by_session = {o.session_id: o for o in report.outcomes}
    assert by_session[bad_sid].action == "dropped_no_remote", (
        f"{label}: an unusable fallback drops (R5); outcomes={report.outcomes}")
    assert "fallback not normalizable" in by_session[bad_sid].detail, (
        f"{label}: R5 must be DISTINGUISHABLE from R6 — an operator has to be able to "
        f"tell 'the hook captured nothing' from 'the hook captured garbage'")
    assert by_session[good_sid].action == "posted", (
        f"{label}: the marker BEHIND the unusable one must still be processed in the "
        f"SAME pass — an escaping exception starves it forever")
    entries = posted(httpserver)
    assert len(entries) == 1 and entries[0]["session_id"] == good_sid


# =========================================================================== #
# AC5a / AC5b — the gate and idempotency are untouched                        #
# =========================================================================== #
def test_fallback_still_fails_closed_for_an_unregistered_project(tmp_path, httpserver):
    """AC5a. The fallback widens WHICH sessions reach the registry gate, never WHAT the
    gate allows. An unregistered project is still dropped, with nothing transmitted."""
    sid = "wave-n-ac5a"
    cfg = mk_cfg(tmp_path, httpserver.url_for(""))
    wire_server(httpserver, ALPHA_CANONICAL)  # STRANGER is deliberately not registered

    repo = make_repo(tmp_path / "stranger", STRANGER_URL)
    wt = add_worktree(repo, tmp_path / "wt-stranger")
    transcript = transcript_for(tmp_path, sid, wt)

    assert run_hook(cfg.loom_home, payload_for(sid, transcript, wt)).returncode == 0
    assert marker_of(cfg, sid)["cwd_remote_url"] == STRANGER_URL
    delete_worktree(wt)

    report = processor.run_once(cfg)

    assert actions(report) == ["dropped_not_registered"], f"outcomes={report.outcomes}"
    assert posted(httpserver) == [], "an unregistered project transmits NOTHING (I5)"


def test_fallback_attributed_session_is_idempotent(tmp_path, httpserver):
    """AC5b. Attribution is not in `content_hash`, so changing how a project is resolved
    can never create duplicates. Two legs:

      1. the marker reappears with the pointer intact → no second POST at all;
      2. the pointer is rewound and the marker re-dropped → the delta is re-sent, the
         server answers 409, the client records `duplicate`, and the pointer ends where
         it was rather than regressing.
    """
    sid = "wave-n-ac5b"
    cfg = mk_cfg(tmp_path, httpserver.url_for(""))
    seen: list[int] = []

    def entries_handler(request):
        from werkzeug.wrappers import Response
        seen.append(1)
        status = 201 if len(seen) == 1 else 409
        return Response(json.dumps({"id": 1}), status=status,
                        content_type="application/json")

    httpserver.expect_request("/api/projects", method="GET").respond_with_json(
        {"projects": [{"id": 1, "canonical_remote": ALPHA_CANONICAL, "name": "alpha",
                       "created_by": "goal", "created_at": "2026-09-01T00:00:00Z"}]})
    httpserver.expect_request("/api/entries", method="POST").respond_with_handler(
        entries_handler)

    repo = make_repo(tmp_path / "alpha", ALPHA_URL)
    wt = add_worktree(repo, tmp_path / "wt-alpha")
    transcript = transcript_for(tmp_path, sid, wt)
    marker_path = cfg.queue_dir / f"{sid}.json"

    assert run_hook(cfg.loom_home, payload_for(sid, transcript, wt)).returncode == 0
    captured_marker = marker_path.read_text(encoding="utf-8")
    delete_worktree(wt)

    report1 = processor.run_once(cfg)
    assert actions(report1) == ["posted"], f"outcomes={report1.outcomes}"
    state_after_1 = json.loads(cfg.state_path.read_text(encoding="utf-8"))
    pointer_1 = state_after_1[str(transcript)]["last_line"]
    assert pointer_1 > 0

    # Leg 1: same marker again, pointer intact → nothing left to send.
    marker_path.write_text(captured_marker, encoding="utf-8")
    report2 = processor.run_once(cfg)
    assert actions(report2) == ["dropped_empty_delta"], f"outcomes={report2.outcomes}"
    assert len(seen) == 1, "an untouched pointer means there is nothing to re-post"

    # Leg 2: pointer rewound (a restored state.json, a re-swept host) → the delta is
    # re-sent and the SERVER is what refuses it. This is the 409 path.
    state_rewound = {str(transcript): {"last_line": 0, "last_uuid": "",
                                       "updated_at": kit.utc_now_iso()}}
    cfg.state_path.write_text(json.dumps(state_rewound), encoding="utf-8")
    marker_path.write_text(captured_marker, encoding="utf-8")
    report3 = processor.run_once(cfg)

    assert actions(report3) == ["duplicate"], f"outcomes={report3.outcomes}"
    assert len(seen) == 2, "the delta was re-sent exactly once"
    state_after_3 = json.loads(cfg.state_path.read_text(encoding="utf-8"))
    assert state_after_3[str(transcript)]["last_line"] == pointer_1, (
        "a 409 advances the pointer to the same EOF as the 201 did — it must not regress")


# =========================================================================== #
# AC8 — no credential ever reaches disk                                       #
# =========================================================================== #
def test_tokenized_remote_never_reaches_disk(tmp_path, httpserver):
    """AC8 — adversarial. Agent worktrees are routinely cloned with a tokenized remote.
    Writing it verbatim puts a live credential at rest in `queue/`, and indefinitely in
    `failed/` after a park (the tombstone is only cleared when the session later resolves
    cleanly, which a parked one by definition never does).

    Asserted at BOTH resting places, plus the property that makes the redaction safe:
    the processor still derives the correct project from the redacted url."""
    sid = "wave-n-ac8"
    cfg = mk_cfg(tmp_path, httpserver.url_for(""))
    wire_server(httpserver, TOKEN_CANONICAL)

    repo = make_repo(tmp_path / "tokenrepo", TOKEN_URL)
    wt = add_worktree(repo, tmp_path / "wt-token")
    transcript = transcript_for(tmp_path, sid, wt)
    marker_path = cfg.queue_dir / f"{sid}.json"

    assert run_hook(cfg.loom_home, payload_for(sid, transcript, wt)).returncode == 0
    marker_text = marker_path.read_text(encoding="utf-8")
    assert TOKEN_USERINFO not in marker_text, (
        f"the credential must never be written to disk; marker was: {marker_text}")
    assert "redact-me-0000" not in marker_text
    assert json.loads(marker_text)["cwd_remote_url"] == TOKEN_REDACTED, (
        "redaction strips the userinfo span and leaves the url otherwise intact")

    # The redacted url must still resolve to the SAME project — redaction is lossless
    # for identity (normalize() discards userinfo anyway).
    delete_worktree(wt)
    report = processor.run_once(cfg)
    entries = posted(httpserver)
    assert len(entries) == 1, f"outcomes={report.outcomes}"
    assert entries[0]["project_remote"] == TOKEN_CANONICAL

    # And the same must hold for a marker that gets PARKED rather than resolved.
    park_sid = "wave-n-ac8-parked"
    repo2 = make_repo(tmp_path / "tokenrepo2", TOKEN_URL)
    wt2 = add_worktree(repo2, tmp_path / "wt-token-2")
    park_transcript = transcript_for(tmp_path, park_sid, wt2)
    assert run_hook(cfg.loom_home, payload_for(park_sid, park_transcript, wt2)).returncode == 0
    park_marker_path = cfg.queue_dir / f"{park_sid}.json"
    parked = json.loads(park_marker_path.read_text(encoding="utf-8"))
    # One attempt short of the bound, so a single failing pass parks it.
    parked["attempts"] = processor.MAX_ATTEMPTS - 1
    park_marker_path.write_text(json.dumps(parked), encoding="utf-8")
    cfg.claude_bin = kit.make_claude_stub(tmp_path / "claude_fail.py", stdout="",
                                          exit_code=1)

    park_report = processor.run_once(cfg)

    assert "failed_parked" in actions(park_report), f"outcomes={park_report.outcomes}"
    failed_file = cfg.failed_dir / f"{park_sid}.json"
    assert failed_file.exists(), "precondition: the marker must really be parked"
    failed_text = failed_file.read_text(encoding="utf-8")
    assert TOKEN_USERINFO not in failed_text, (
        "a parked marker rests in failed/ until a human deletes it — a credential there "
        "is the longest-lived leak this feature could introduce")


@pytest.mark.parametrize("url, redacted, secret, label", [
    (COMPOUND_URL, COMPOUND_REDACTED, COMPOUND_USERINFO, "compound scheme"),
    (MULTI_AT_URL, MULTI_AT_REDACTED, MULTI_AT_USERINFO, "multi-@ userinfo"),
])
def test_redaction_holds_for_every_credentialed_url_form(tmp_path, httpserver,
                                                         url, redacted, secret, label):
    """AC8, the adversarial forms. Both were demonstrated against the first
    implementation of the redaction and are regression cases, not hypotheticals.

    The assertion is deliberately on the SECRET's absence rather than only on the
    expected output string: a future redaction that produced some other correct-looking
    url while leaving a fragment of the credential behind would satisfy an
    equality-only check on the happy form and still put a secret on disk."""
    sid = f"wave-n-ac8-{label.split()[0]}"
    cfg = mk_cfg(tmp_path, httpserver.url_for(""))
    wire_server(httpserver, TOKEN_CANONICAL)

    repo = make_repo(tmp_path / f"repo-{label.split()[0]}", url)
    wt = add_worktree(repo, tmp_path / f"wt-{label.split()[0]}")
    transcript = transcript_for(tmp_path, sid, wt)

    assert run_hook(cfg.loom_home, payload_for(sid, transcript, wt)).returncode == 0
    marker_text = (cfg.queue_dir / f"{sid}.json").read_text(encoding="utf-8")
    assert secret not in marker_text, (
        f"{label}: the credential reached disk; marker was: {marker_text}")
    # Every individual span of the secret, not just the whole: a first-`@` stop leaves
    # the tail behind, which a whole-string check would not notice.
    for fragment in secret.replace(":", "@").split("@"):
        assert fragment not in marker_text, (
            f"{label}: the fragment {fragment!r} of the credential survived redaction")
    assert json.loads(marker_text)["cwd_remote_url"] == redacted

    # And the project is still resolved correctly from the redacted form.
    delete_worktree(wt)
    report = processor.run_once(cfg)
    entries = posted(httpserver)
    assert len(entries) == 1, f"{label}: outcomes={report.outcomes}"
    assert entries[0]["project_remote"] == TOKEN_CANONICAL


def test_scp_like_remote_survives_redaction_unchanged(tmp_path, httpserver):
    """AC8, second half. `git@github.com:org/repo.git` has no `://`, and its `git@` is a
    USERNAME, not a secret. A redaction that strips it would mangle every SSH remote in
    existence — the overwhelmingly common case."""
    sid = "wave-n-ac8-scp"
    cfg = mk_cfg(tmp_path, httpserver.url_for(""))
    wire_server(httpserver, ALPHA_CANONICAL)

    repo = make_repo(tmp_path / "alpha", ALPHA_URL)
    wt = add_worktree(repo, tmp_path / "wt-alpha")
    transcript = transcript_for(tmp_path, sid, wt)

    assert run_hook(cfg.loom_home, payload_for(sid, transcript, wt)).returncode == 0
    assert marker_of(cfg, sid)["cwd_remote_url"] == ALPHA_URL, (
        "the scp-like form must survive byte for byte")

    delete_worktree(wt)
    processor.run_once(cfg)
    assert posted(httpserver)[0]["project_remote"] == ALPHA_CANONICAL


# =========================================================================== #
# AC9 — a second SessionEnd fire may not undo the first capture               #
# =========================================================================== #
# SessionEnd fires MORE THAN ONCE per session by design: `reason` = clear/resume are
# "extra capture points for free" (DESIGN.md §4.1), and the hook rebuilds the whole
# marker from stdin on every fire. That is idempotent for the five original fields —
# each is re-derivable from the payload on any invocation — and it is NOT idempotent for
# `cwd_remote_url`, which is derivable ONLY while the worktree still exists. Both tests
# below were demonstrated against the first implementation, which re-derived blindly.

def test_a_second_fire_after_deletion_keeps_the_first_capture(tmp_path, httpserver):
    """AC9a. Fire #1 captures while the worktree lives; the worktree is deleted; fire #2
    arrives for the SAME session and can resolve nothing.

    Re-deriving blindly erases the field and the session drops as `dropped_no_remote` —
    which is exactly the data loss this whole wave exists to close, reintroduced by the
    feature's own second write. FIRST CAPTURE WINS."""
    sid = "wave-n-ac9a"
    cfg = mk_cfg(tmp_path, httpserver.url_for(""))
    wire_server(httpserver, ALPHA_CANONICAL)

    repo = make_repo(tmp_path / "alpha", ALPHA_URL)
    wt = add_worktree(repo, tmp_path / "wt-alpha")
    transcript = transcript_for(tmp_path, sid, wt)

    # Fire #1 — a mid-session /clear, while the worktree is alive.
    first = payload_for(sid, transcript, wt)
    first["reason"] = "clear"
    assert run_hook(cfg.loom_home, first).returncode == 0
    assert marker_of(cfg, sid)["cwd_remote_url"] == ALPHA_URL

    # The worktree dies, then the session closes for good.
    delete_worktree(wt)
    assert run_hook(cfg.loom_home, payload_for(sid, transcript, wt)).returncode == 0

    marker = marker_of(cfg, sid)
    assert marker["cwd_remote_url"] == ALPHA_URL, (
        "the second fire must not erase what the first one captured — it is the only "
        "invocation at which the answer still existed")
    assert marker["reason"] == RECORDED_PAYLOAD["reason"], (
        "every OTHER field still comes from the latest fire; only the capture is pinned")

    report = processor.run_once(cfg)
    entries = posted(httpserver)
    assert len(entries) == 1, f"outcomes={report.outcomes}"
    assert entries[0]["project_remote"] == ALPHA_CANONICAL


def test_a_second_fire_on_a_reused_path_does_not_steal_the_capture(tmp_path, httpserver):
    """AC9b — the silent one. Between fire #1 and fire #2 the ephemeral path is torn down
    and REUSED for a different repo, so fire #2's `cwd` resolves live to BETA.

    Re-deriving would overwrite ALPHA's marker with BETA's remote. At processing time the
    path is gone again, so R4 would file ALPHA's session under BETA and log
    `attribution_from_marker` — indistinguishable from a correct resolution. That is
    strictly worse than L-2, which at least emits `attribution_superseded`.

    Both projects are registered, so a wrong choice still POSTs; it fails on WHICH."""
    sid = "wave-n-ac9b"
    cfg = mk_cfg(tmp_path, httpserver.url_for(""))
    wire_server(httpserver, ALPHA_CANONICAL, BETA_CANONICAL)

    shared = tmp_path / "wt-pooled"
    alpha = make_repo(tmp_path / "alpha", ALPHA_URL)
    wt = add_worktree(alpha, shared)
    transcript = transcript_for(tmp_path, sid, wt)

    first = payload_for(sid, transcript, wt)
    first["reason"] = "clear"
    assert run_hook(cfg.loom_home, first).returncode == 0
    assert marker_of(cfg, sid)["cwd_remote_url"] == ALPHA_URL

    # The pool recycles the path for an unrelated repo before this session's final fire.
    delete_worktree(shared)
    beta = make_repo(tmp_path / "beta", BETA_URL)
    add_worktree(beta, shared)
    assert processor._git_remote(str(shared)) == BETA_URL

    assert run_hook(cfg.loom_home, payload_for(sid, transcript, wt)).returncode == 0
    assert marker_of(cfg, sid)["cwd_remote_url"] == ALPHA_URL, (
        "a later fire may not replace this session's captured remote with whatever repo "
        "happens to occupy its path now")

    # Path gone again by the time the processor runs → R4 on the PINNED value.
    delete_worktree(shared)
    report = processor.run_once(cfg)
    entries = posted(httpserver)
    assert len(entries) == 1, f"outcomes={report.outcomes}"
    assert entries[0]["project_remote"] == ALPHA_CANONICAL, (
        "the session belongs to the repo it actually ran in")
    assert entries[0]["project_remote"] != BETA_CANONICAL


# =========================================================================== #
# AC7 — the hook is still unbreakable                                         #
# =========================================================================== #
@pytest.mark.parametrize("scenario", ["git_absent", "cwd_not_a_repo", "cwd_missing"])
def test_hook_still_writes_a_valid_marker_and_exits_zero(tmp_path, scenario):
    """AC7. `git` missing from PATH, a `cwd` that is not a repo, and a `cwd` that no
    longer exists. In every case the hook writes a VALID marker, omits the field rather
    than writing a null, and exits 0.

    The operational invariant is absolute: a session's shutdown may never be blocked or
    failed by this feature. `cwd_missing` is the case that would hang a naive
    implementation — `git -C <gone>` against a credential helper or a network remote —
    so the elapsed time is asserted, not just the exit code."""
    sid = f"wave-n-ac7-{scenario}"
    loom_home = kit.loom_home(tmp_path)
    path = None

    if scenario == "git_absent":
        cwd = add_worktree(make_repo(tmp_path / "alpha", ALPHA_URL), tmp_path / "wt")
        path = str(fakebin(tmp_path, omit=("git",)))
    elif scenario == "cwd_not_a_repo":
        cwd = tmp_path / "plain_dir"
        cwd.mkdir()
    else:
        cwd = add_worktree(make_repo(tmp_path / "alpha", ALPHA_URL), tmp_path / "wt")
        delete_worktree(cwd)

    transcript = kit.write_transcript(tmp_path / "t.jsonl",
                                      kit.transcript_lines(str(cwd), sid))

    started = time.monotonic()
    proc = run_hook(loom_home, payload_for(sid, transcript, cwd), path=path)
    elapsed = time.monotonic() - started

    assert proc.returncode == 0, f"{scenario}: {proc.stderr}"
    marker_path = loom_home / "queue" / f"{sid}.json"
    assert marker_path.exists(), f"{scenario}: the marker is NOT optional"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    # The five original fields are intact and unconditional.
    assert marker["session_id"] == sid
    assert marker["transcript_path"] == str(transcript)
    assert marker["cwd"] == str(cwd)
    assert marker["reason"] == RECORDED_PAYLOAD["reason"]
    assert marker["marked_at"].endswith("Z") and "T" in marker["marked_at"]
    # ABSENT, never null/empty: "could not resolve" and "old hook" must be ONE state.
    assert "cwd_remote_url" not in marker, (
        f"{scenario}: an unresolvable remote omits the key; a null or an empty string "
        f"would make the processor carry two code paths for one condition")
    assert elapsed < 10.0, (
        f"{scenario}: the hook took {elapsed:.1f}s — it is on the user's exit path and "
        f"must never wait on git")


def test_hook_without_jq_is_still_a_noop(tmp_path):
    """AC7, floor case. `jq` absent is the hook's oldest degradation: log and exit 0.
    This feature must not have introduced a `git` call that runs BEFORE that check and
    changes the outcome."""
    sid = "wave-n-ac7-nojq"
    loom_home = kit.loom_home(tmp_path)
    cwd = add_worktree(make_repo(tmp_path / "alpha", ALPHA_URL), tmp_path / "wt")
    transcript = kit.write_transcript(tmp_path / "t.jsonl",
                                      kit.transcript_lines(str(cwd), sid))

    proc = run_hook(loom_home, payload_for(sid, transcript, cwd),
                    path=str(fakebin(tmp_path, omit=("jq",))))

    assert proc.returncode == 0, proc.stderr
    assert not (loom_home / "queue" / f"{sid}.json").exists()
