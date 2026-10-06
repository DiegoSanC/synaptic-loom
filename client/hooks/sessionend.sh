#!/usr/bin/env bash
# ~/.loom/hooks/sessionend.sh — Synaptic Loom SessionEnd hook (DESIGN.md §4.1, PLAN.md §T5).
#
# It does ONE thing: marks the transcript of the session that just closed as "pending
# review" (atomic JSON marker in $LOOM_HOME/queue/<session_id>.json) and, right after,
# launches the fully decoupled processor (the "kick" from decision P1).
#
# OPERATIONAL INVARIANT: the hook must NEVER break the user's session shutdown. That is why
# it uses neither `set -e` nor `set -u`: any failure (jq missing, $LOOM_HOME not writable,
# malformed stdin, kick impossible) is logged to $LOOM_HOME/log/hook.log when possible and
# the script ALWAYS ends in `exit 0`. It is fast: no network, no waiting on the kick.
#
# Shape of the hook in ~/.claude/settings.json (checked against the official Claude Code
# hooks docs, https://code.claude.com/docs/en/hooks.md, SessionEnd section):
#
#   {"hooks":{"SessionEnd":[{"hooks":[{"type":"command","command":"<path to this script>"}]}]}}
#
# The "matcher" is optional for SessionEnd; omitting it makes the hook fire on ANY
# `reason` (prompt_input_exit, clear, resume, logout, other), which is exactly what DESIGN
# §4.1 wants ("extra capture points for free"; the delta is always governed by the pointer).

# --- 0. Anti-recursion belt: never mark the summarizer's own session. ----------------------
[ "${LOOM_SUMMARIZER:-}" = "1" ] && exit 0

LOOM_HOME="${LOOM_HOME:-$HOME/.loom}"
LOG_DIR="$LOOM_HOME/log"
QUEUE_DIR="$LOOM_HOME/queue"

# Best-effort log: it never fails the hook (everything falls back to a no-op via `|| ...`).
log() {
  mkdir -p "$LOG_DIR" 2>/dev/null || return 0
  printf '%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG_DIR/hook.log" 2>/dev/null || true
}

# --- 1. Without jq the payload cannot be parsed → log and exit 0. --------------------------
if ! command -v jq >/dev/null 2>&1; then
  log "jq not found in PATH; SessionEnd ignored"
  exit 0
fi

# --- 2. Read the payload from stdin. -------------------------------------------------------
IN=$(cat)

# --- 3. Extract session_id. Non-JSON stdin or a missing session_id → exit 0. ---------------
SID=$(jq -r '.session_id // empty' <<<"$IN" 2>/dev/null)
if [ -z "$SID" ]; then
  log "payload without session_id or non-JSON stdin; ignored"
  exit 0
fi

# --- 4. Ensure the queue directory. If it cannot be created → log and exit 0. --------------
if ! mkdir -p "$QUEUE_DIR" 2>/dev/null; then
  log "could not create $QUEUE_DIR; session $SID not marked"
  exit 0
fi

TMP="$QUEUE_DIR/$SID.json.tmp"
DST="$QUEUE_DIR/$SID.json"

# --- 4a. FIRST CAPTURE WINS. If a marker for this session already carries a remote,
#         that one is kept and this fire does not re-derive it.
#
#         SessionEnd fires MORE THAN ONCE per session by design — `reason` = clear/resume
#         are "extra capture points for free" (DESIGN §4.1) — and this hook rebuilds the
#         whole marker from stdin every time. That is idempotent for the five original
#         fields, because each is re-derivable from the payload on ANY invocation. It is
#         NOT idempotent for `cwd_remote_url`, which is derivable only while the worktree
#         still exists, and a session's worktree is routinely gone by its final fire.
#         Re-deriving blindly therefore erases the capture and reintroduces the exact
#         drop this feature exists to prevent — and, if the pooled path has meanwhile
#         been recycled for another repo, silently replaces it with THAT repo's remote,
#         which the processor would then use with no divergence to report.
#
#         So the field is pinned to the first fire that could answer. A session's origin
#         does not change across re-fires of the same session, and live truth still wins
#         whenever `cwd` resolves at processing time (contract/ATTRIBUTION.md §2 R3), so
#         pinning cannot outrank a remote that is genuinely current.
#
#         This is the hook's only read-modify-write, and it is a safe one: the write below
#         is still a single atomic tmp+mv, `sweep.py` never overwrites an existing marker,
#         and a marker the processor removes mid-hook simply yields an empty prior and a
#         fresh capture.
PRIOR=""
if [ -f "$DST" ]; then
  PRIOR=$(jq -r '.cwd_remote_url // empty' "$DST" 2>/dev/null)
fi

# --- 4b. Capture the project's remote WHILE the session's cwd still exists (issue #4).
#         Agent sessions run in ephemeral git worktrees that are deleted seconds after
#         this hook returns, and ALWAYS before the periodic pass. The processor resolves
#         project identity from `cwd` at PROCESSING time, so by then the directory is
#         gone, the remote is unresolvable and an intact transcript is dropped. This is
#         the last moment at which the answer still exists.
#
#         THE RAW URL IS CAPTURED, NEVER A CANONICAL ONE. `normalize()` (client/loom/
#         normalize.py) is the identity authority and the server copies its table
#         character for character; a second implementation in jq/shell could drift, and
#         a drift here does not fail loudly — it files sessions under a host/org/repo key
#         nobody else computes. The hook captures a string and makes no judgement.
#
#         IT RUNS BEFORE THE `jq -n` BELOW, deliberately: one atomic write, no
#         read-modify-write on a file two unlocked writers share (this hook and sweep.py).
#
#         `timeout` IS NOT PORTABLE — it is coreutils, and macOS does not ship it. Same
#         trap as `setsid` in issue #3 (see §6), so it gets the same tiered treatment:
#         timeout, then gtimeout, then bare. The bare branch is an acceptable FLOOR
#         rather than a hazard because `git remote get-url` reads .git/config and never
#         opens a socket (measured at 1 ms) — but it is a deliberate, commented branch,
#         not an accident.
#
#         EVERY failure degrades to "omit the field": no git, no repo, no remote, a cwd
#         that already vanished. A missing key and "the hook is an old version" are then
#         ONE state needing one code path downstream, which is why the key is omitted and
#         never written as null or "". The marker is still written, and the hook still
#         exits 0 — the operational invariant above outranks this feature entirely.
CWD=$(jq -r '.cwd // empty' <<<"$IN" 2>/dev/null)
REMOTE=""
if [ -n "$PRIOR" ]; then
  :   # 4a: already captured for this session; do not fork git, do not re-derive.
elif [ -n "$CWD" ] && [ -d "$CWD" ]; then
  if ! command -v git >/dev/null 2>&1; then
    log "git not found in PATH; session $SID marked without project attribution"
  elif command -v timeout >/dev/null 2>&1; then
    REMOTE=$(timeout 5 git -C "$CWD" remote get-url origin 2>/dev/null)
  elif command -v gtimeout >/dev/null 2>&1; then
    REMOTE=$(gtimeout 5 git -C "$CWD" remote get-url origin 2>/dev/null)
  else
    REMOTE=$(git -C "$CWD" remote get-url origin 2>/dev/null)
  fi
fi

# --- 5. Write the marker atomically (.tmp + mv). Overwriting = idempotent re-mark.
#        Fields per DESIGN §4.1; marked_at = now, in UTC ISO-8601.
#
#        THE CAPTURED URL IS CREDENTIAL-REDACTED HERE, and that is not optional. Agent
#        worktrees are routinely cloned with a tokenized remote
#        (https://x-access-token:<token>@github.com/org/repo.git); writing it verbatim
#        would put a LIVE credential at rest in queue/, and indefinitely in failed/ once
#        the marker is parked — the tombstone there is only cleared when the session
#        later resolves cleanly, which a parked marker by definition never does. Nothing
#        downstream wants it: normalize() discards userinfo anyway. jq does the stripping
#        because it is already a hard dependency — `sed` would reintroduce a BSD/GNU
#        portability question this hook does not need.
#
#        THE PATTERN IS `^(?<h>[^/]*://)[^/]*@`, and each of its three parts is a bug
#        that was demonstrated against the obvious version of it:
#          • `[^/]*://` rather than `[a-zA-Z0-9+.-]+://` — a scheme run that excludes `:`
#            never matches a COMPOUND scheme, and `git remote get-url` returns whatever
#            .git/config holds with no scheme validation, so a real origin can be
#            `git::https://user:tok@host/...`. The strict version left that token intact.
#            `url.<base>.insteadOf` rewriting can produce such a url at get-url time from
#            a config that looks perfectly ordinary.
#          • `[^/]*@` rather than `[^/@]*@` — greedy to the LAST `@` before the first `/`,
#            which is what `normalize()` itself does (`authority.rpartition("@")`).
#            Stopping at the FIRST `@` strips one span of `user:a@b` and leaves the rest.
#          • still ANCHORED at `^` — without the anchor a `://...@` appearing later, in a
#            path or query, would be rewritten instead, mangling a url that carries no
#            credential at all.
#        The scp-like `git@host:org/repo.git` has no `://`, does not match, and is
#        correctly left alone: there `git@` is a USERNAME, not a secret.
#
#        KNOWN LIMIT: a credential carried OUTSIDE the userinfo span — a query parameter,
#        say — is not redacted, because stripping a query would change the url rather
#        than de-credential it. Recorded in contract/ATTRIBUTION.md §1.1.
if jq -n --argjson in "$IN" --arg remote "$REMOTE" --arg prior "$PRIOR" \
     '{session_id: $in.session_id,
       transcript_path: $in.transcript_path,
       cwd: $in.cwd,
       reason: $in.reason,
       marked_at: (now | todate)}
      + (if $prior != "" then {cwd_remote_url: $prior}          # 4a: first capture wins
         elif $remote != "" then {cwd_remote_url:
                 ($remote | sub("^(?<h>[^/]*://)[^/]*@"; .h))}  # already redacted if kept
         else {} end)' >"$TMP" 2>/dev/null && mv -f "$TMP" "$DST" 2>/dev/null; then
  log "marked session $SID (reason=$(jq -r '.reason // "?"' <<<"$IN" 2>/dev/null), attribution=$([ -n "$PRIOR" ] && echo kept || { [ -n "$REMOTE" ] && echo captured || echo none; }))"
else
  rm -f "$TMP" 2>/dev/null || true
  log "could not write the marker for $SID"
  exit 0
fi

# --- 6. Kick (P1): launch the processor FULLY decoupled; it outlives the hook. -------------
#        The launcher must put the processor in a NEW SESSION, not merely in the
#        background. A new session is what makes it immune to a signal aimed at the hook's
#        process group and to the controlling terminal going away — the known
#        killed-async-hooks bug does not apply, because it is no longer the hook's child.
#        `nohup` and `( cmd & ) &` do NOT buy that: they survive the hook EXITING while
#        leaving the child inside the hook's own process group and session. They are
#        deliberately not used here.
#
#        TWO TIERS, because `setsid` is util-linux and macOS does not ship it (issue #3):
#          1. setsid   — Linux/WSL2. Unchanged behavior, no new dependency.
#          2. python3  — everywhere else. Already a hard prerequisite of the product
#                        (install.sh builds the venv with a python3/uv), so it costs no new
#                        dependency either. Two non-obvious requirements, both load-bearing:
#                        • it FORKS BEFORE setsid(), because setsid(2) fails with EPERM when
#                          the caller is already a process-group leader — which EVERY
#                          background job is on a host whose shell has job control on
#                          (`set -m`). Without the fork the launch fails silently, and only
#                          on such hosts.
#                        • the detacher is python3, never "$PY" itself: the launched argv
#                          (`-m loom.processor --quiet`) is a frozen contract, and re-exec'ing
#                          through the venv python would rewrite it.
#        With NEITHER available the kick is refused and LOGGED, never faked. A process
#        started in the hook's own session but logged as a successful kick would turn a
#        loud, diagnosable gap into a quiet, host-dependent one. The periodic trigger
#        (install.sh step 5) remains the recovery path, exactly as before.
#
#        WHAT THE LOG LINE BELOW CAN AND CANNOT CLAIM. It says "dispatched", not "fired",
#        and the distinction is the whole point of this change. Confirming that the
#        launcher actually reached the processor would mean WAITING on it, which is the one
#        thing this hook must never do. So the line records the tier the hook handed the
#        work to; whether that tier then succeeded shows up in processor.log, or in the
#        queue draining. Writing "fired" here would re-create, in miniature, the very
#        failure this change exists to remove: a hook.log that reads like a clean session
#        end while nothing ran. The known case is a macOS box with no Xcode Command Line
#        Tools, where /usr/bin/python3 is a stub that satisfies `command -v` and then
#        prompts for an install instead of running: stdin is /dev/null so it cannot block
#        the shutdown, but the kick does not happen. `loom doctor` cannot see that either
#        (it would have to run the stub to find out, which is what triggers the prompt) —
#        see docs/RUNBOOK.md §5.
#
#        NOTE: the PLAN §T5 snippet writes `loom process --quiet`; that CLI subcommand
#        arrives in T6. Here we use `python -m loom.processor --quiet`, which exists ALREADY
#        (T4) and is equivalent (one processor pass). Migrating to `loom process` once T6
#        lands is optional — both run the same run_once().

# Tier-2 detacher. argv[1:] is the command to run, handed to execv UNCHANGED.
LOOM_DETACH_PY='import os, sys
if os.fork():
    os._exit(0)
os.setsid()
os.execv(sys.argv[1], sys.argv[1:])'

PY="$LOOM_HOME/venv/bin/python"
if [ -x "$PY" ]; then
  if command -v setsid >/dev/null 2>&1; then
    setsid "$PY" -m loom.processor --quiet >>"$LOG_DIR/processor.log" 2>&1 </dev/null &
    log "kick dispatched (launcher=setsid); its own failures land in processor.log"
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c "$LOOM_DETACH_PY" "$PY" -m loom.processor --quiet \
      >>"$LOG_DIR/processor.log" 2>&1 </dev/null &
    log "kick dispatched (launcher=python3); its own failures land in processor.log"
  else
    log "kick skipped (no session-detach launcher); the marker will be processed by the periodic pass"
  fi
else
  log "venv python not executable at $PY; kick skipped (the marker will be processed later)"
fi

exit 0
