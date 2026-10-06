---
title: "fix: Preserve project attribution after an ephemeral worktree is deleted"
type: fix
date: 2026-09-13
complexity: large
github_issue: 4
baseline_in_issue: 971ea6c8202cf8068bea69314f4b967e4295c17f
branch_from: main (ec2ee05 or later — issue #3 has landed and rewrote the hook)
branch: fm/marker-attribution-fallback
---

# fix: Preserve project attribution after an ephemeral worktree is deleted

## Overview

Project identity (D1) is resolved **at processing time** from `marker.cwd`. Agent
sessions run in ephemeral git worktrees that are deleted as soon as the agent finishes
— often seconds after `SessionEnd`, always before the 15-minute periodic pass. By the
time `_process_marker` runs, `cwd` is gone, `_git_remote` returns `None`, and a
perfectly intact transcript is thrown away as `dropped_no_remote`.

The fix moves the **resolution** earlier without moving the **decision**: the SessionEnd
hook captures the raw `origin` URL while the worktree still exists and writes it into
the marker it already writes atomically; the processor consumes it as a *constrained
fallback*, used only when `cwd` no longer resolves. Normalization, the registered-project
gate, the pointer and `content_hash` idempotency are all untouched.

**Scope:** one `git` call in a 143-line shell script, one additive marker field, one
branch in `_process_marker`'s step 2, an additive contract document, tests, and a new
frozen goal wave. **No change** to extraction semantics, the summarizer, the secret
scan, the API payload, `state.json`, or any existing disposition constant.

The work is small. Its risk is not: the marker is a schema two unlocked concurrent
writers share (`sessionend.sh` and `sweep.py`), attribution is a trust boundary, and
getting the authority rule backwards silently files one project's sessions under
another. Per the owner's 2026-09-12 coordination note, **the contract in §4 must be
reviewed and accepted before any implementation starts.**

## Problem Statement

### The failure, precisely

`client/loom/processor.py:1007-1027`, step 2 of the nine-step pipeline:

```python
cwd = marker.get("cwd", "")
...
remote_url = _git_remote(cwd)          # line 1016
if remote_url is None:
    _resolve_marker(cfg, marker_path, identity, session_id)
    _record(report, cfg, session_id, "dropped_no_remote", "no git remote origin")
    return
```

and `_git_remote` (`client/loom/processor.py:621-637`):

```python
def _git_remote(cwd: str) -> str | None:
    if not cwd or not Path(cwd).is_dir():
        return None
    ...
```

`Path(cwd).is_dir()` is `False` the moment the worktree directory is unlinked. The
marker is then **deleted** by `_resolve_marker` and the outcome is categorised as
`_SKIPPED` (`processor.py:109`) — it does not even count as an error in `loom status`.

The transcript itself survives: it lives at
`~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`, outside the worktree. The pointer
is **not** advanced on this path, so the content is still formally unprocessed — but
nothing will ever pick it up again, because the sweep's eligibility precheck
(`sweep.py:524-535`) asks the same dead `cwd` the same question and silently withholds
the transcript on every pass, forever.

So the capture is lost **twice over**, and neither loss is loud.

### Verified evidence (this machine, 2026-09-13)

A linked worktree shares `origin` with its main repo, and resolving it is free:

```
$ git -C /tmp/wtcheck/wt remote get-url origin
git@github.com:example-org/synaptic-loom.git
$ time git -C /tmp/wtcheck/wt remote get-url origin
0.001 total                      # reads .git/config; never touches the network

$ rm -rf /tmp/wtcheck/wt
$ git -C /tmp/wtcheck/wt remote get-url origin
fatal: cannot change to '/tmp/wtcheck/wt': No such file or directory  (exit 128)
```

Both halves of the bug are reproduced by those four lines: the information the processor
needs **is available at SessionEnd, for one millisecond of cost**, and is **gone**
by the time the processor asks for it.

### The second bug this exposes

Today `_git_remote(cwd)` is trusted unconditionally. If an ephemeral worktree path is
**reused** — `/tmp/agent-wt-1` deleted for repo A, recreated for repo B — a surviving
marker from the repo-A session resolves `cwd` to **repo B** and, if B is registered, the
A session's summary is posted under project B. That is a silent cross-project
misattribution that exists in the code today. This plan does not fix it (see
§Known limits, L-2); it is recorded here because it is the reason §4's authority rule
needs a written decision rather than an obvious default.

## Proposed Solution

### One sentence

`sessionend.sh` adds `cwd_remote_url` — the **raw** `origin` URL, captured while the
worktree exists — to the marker; `_process_marker` normalizes it with the same
`normalize()` and uses it **only when `_git_remote(cwd)` returns `None`**.

### Why the raw URL and not the canonical form

`normalize()` (`client/loom/normalize.py`) is the project-identity authority (D1), and
the server copies its test table character for character (PLAN.md §T2). Canonicalizing
in the hook would mean a **second implementation in `jq`/shell** that can drift from the
Python one — and a drift here does not fail loudly, it files sessions under a
`host/org/repo` key nobody else computes. The hook therefore captures a string and makes
no judgement; every semantic decision stays in Python, on the existing code path.

### Why the marker, and not a path→remote ledger

The issue asks for a *"transcript/session-to-canonical-remote association"* — keyed by
**session**, not by path. The marker already is exactly that: an atomically published
(`sessionend.sh:59-66`, tmp + `mv -f`) record that binds `session_id`,
`transcript_path` and `cwd` together. Adding the remote to it is additive, needs no new
file, no new retention policy and no new atomicity argument, and it cannot be
invalidated by anything that happens to the path afterwards.

A `~/.loom/paths.json` ledger was considered and rejected — see §Alternatives, A-1.

### Why the hook is the right capture point

The issue requires an *early* capture point, explicitly rejecting "cache it after the
first processing pass". `SessionEnd` is the last moment at which the session's own
working directory is guaranteed to exist, and it is unconditionally **before** any
processor pass — including the kick the hook itself fires. It is the earliest point the
client controls that does not require installing a new hook type.

## Technical Approach

### D4.1 — Marker schema (additive)

```jsonc
{
  "session_id":     "733240a1-…",     // unchanged
  "transcript_path":"/home/…/<sid>.jsonl",  // unchanged
  "cwd":            "/tmp/agent-wt-1",      // unchanged
  "reason":         "prompt_input_exit",    // unchanged
  "marked_at":      "2026-09-13T10:00:00Z", // unchanged
  "cwd_remote_url": "git@github.com:org/repo.git"  // NEW, optional, raw, may be absent
}
```

- **Additive and optional.** A marker without the field behaves exactly as today. This
  is what makes the change safe for markers already sitting in `queue/` across an
  upgrade, and for `sweep.py`'s synthetic markers.
- **Raw, not normalized.** See above.
- **Absent, never `null`/empty.** `jq` omits the key when `git` produced nothing, so
  "the hook could not resolve it" and "the hook is an old version" are the same state and
  need one code path, not two.

**There is a landed precedent for this exact move.** `sweep.py:441-448` already writes a
sixth key, `"origin": "sweep"`, onto the hook's five — the marker schema is extensible by
design and by practice, and `contract/verify.py` never reads the queue.

**But there is exactly one exact-key-set assertion, and it is load-bearing.**
`client/tests/unit/test_sweep.py:154-155`:

```python
# Hook-marker schema + origin, and nothing else (AC-2.1d).
assert set(marker) == {"session_id", "transcript_path", "cwd", "reason",
                       "marked_at", "origin"}
```

It constrains the **sweep's** synthetic marker, not the hook's. D4.6 says the sweep must
*not* carry `cwd_remote_url` — so this assertion is not an obstacle, it is the **check
that turns that decision from a convention into a property**. Leave it exactly as it is:
if a future implementer adds the field to `_enqueue_marker`, this test goes red, which is
the correct outcome. Do not "fix" it by widening the set.

No other exact-key-set assertion exists (`client/tests/integration/test_hook_install.py:249-254,399-402`
and `test/waves/a/test_goal_e1_sweep.py:96-102` assert per-field, which is additive-safe).
So: the hook's field trips nothing; the sweep's absence of it is guarded. What still needs
review is the *semantics* in D4.2.

### D4.1b — The raw URL must be credential-redacted before it is written

**This is a defect in the naive version of this plan, found in flow analysis and verified
against the code.** Agent worktrees are routinely cloned with a tokenized remote:

```
https://x-access-token:ghp_LIVE_TOKEN@github.com/org/repo.git
```

Writing that string verbatim into `~/.loom/queue/<sid>.json` puts a **live credential at
rest on disk** — and not briefly. If the session's marker is later parked,
`processor.py:1338` does `shutil.move` of the *whole marker* into `failed/`, where
`_clear_failed_tombstone` only ever removes it if that session later resolves cleanly,
which a parked marker by definition does not. The credential stays until a human deletes
it. This runs directly against I5/I6 (content never leaks), and the repo gates CI on
`gitleaks` 8.30.1 — so a fixture built from a tokenized remote would trip the suite too.

Nothing downstream needs it: `normalize()` discards userinfo at
`client/loom/normalize.py:32,36` (`rpartition("@")[2]`). The credential is pure liability.

**The hook therefore strips the `userinfo@` span before writing.** Verified on this
machine, using `jq` (already a hard dependency — no `sed` BSD/GNU portability question):

```
$ echo '{"u":"https://x-access-token:ghp_SECRET123@github.com/org/repo.git"}' \
    | jq -r '.u | sub("^(?<s>[a-zA-Z0-9+.-]+://)[^/@]*@"; .s)'
https://github.com/org/repo.git
```

The scp-like form (`git@github.com:org/repo.git`) has no `://` and is left untouched,
correctly — its `git@` is a username, not a secret.

**Why this is not the shell-normalization mistake D4.2 forbids.** The two fail in opposite
directions. A normalization bug in shell produces a *plausible-looking wrong key* and
files sessions silently under it. A redaction bug either leaves the token (caught by the
assertion below) or mangles the URL so `normalize()` raises `ValueError` → **R5** →
`dropped_no_remote`, which is loud and safe. Redaction may fail; it cannot fail quietly
into a wrong project. That asymmetry is the whole justification, and it belongs in the
contract.

### D4.2 — Authority rule (the decision this plan exists to pin)

| # | `cwd` resolves? | `cwd_remote_url` | Identity used | Disposition | Diagnostic |
|---|---|---|---|---|---|
| R1 | yes | absent | live | unchanged | — |
| R2 | yes | present, equal | live | unchanged | — |
| R3 | yes | present, **differs** | **live** | unchanged | `attribution_superseded` |
| R4 | **no** | present, normalizable | **captured** | **now proceeds** | `attribution_from_marker` |
| R5 | no | present, not normalizable / not a `str` | none | `dropped_no_remote` | detail `fallback not normalizable` |
| R6 | no | absent | none | `dropped_no_remote` | detail `no git remote origin` (today's string) |

**R3 is the rule that satisfies acceptance criterion 3.** The issue specifies the
captured value as *"a constrained fallback when cwd no longer resolves"* — so a live
remote always wins, and the captured value can never pull a transcript back onto an old
project. The `attribution_superseded` line makes the divergence visible instead of
silent, which is the other half of that criterion. A genuinely renamed repository
therefore keeps working with no intervention.

**R4 is the fix.** Nothing else changes.

**R5/R6 satisfy acceptance criterion 4:** absent or unusable identity is recorded with a
distinguishing detail and the marker is dropped — never attributed to a guess. No new
disposition constant is introduced, so `_CATEGORY` (`processor.py:103-119`), `loom
status`'s counters and every existing report assertion are untouched.

### D4.3 — What "verifiable" means here

The association is trustworthy because of three properties that already hold, not
because of anything new:

1. **Session-bound.** `cwd_remote_url` is published in the same atomic write as the
   `session_id` and `transcript_path` it describes (`sessionend.sh:59-66`). There is no
   window in which a marker carries one session's id and another's remote.
2. **Never authoritative over live truth.** R3.
3. **Still gated.** The fallback produces a candidate `canonical`, which then goes
   through the *unchanged* registry gate (`processor.py:1030-1037`). An unregistered
   remote is still dropped. The fallback widens *which sessions reach the gate*, never
   *what the gate allows*.

### D4.4 — The hook change

```sh
# after the marker's DST is published, before the kick
REMOTE=""
if [ -n "$CWD" ] && [ -d "$CWD" ]; then
  REMOTE=$(_loom_git_remote "$CWD")
fi
```

Two shell constraints, both non-obvious:

- **`timeout` is not portable.** It is coreutils; macOS does not ship it (`gtimeout`
  only with Homebrew). This is the *same trap* as `setsid` in issue #3, whose fix landed
  in `ec2ee05` four commits ago. Use the **same tiered pattern the hook already
  established at `sessionend.sh:127-141`**: try `timeout`, then `gtimeout`, then run
  `git` bare. `git remote get-url` reads `.git/config` and never opens a socket
  (verified above, 1 ms), so the bare branch is an acceptable floor rather than a
  hazard — but it must be a deliberate, commented branch, not an accident.
- **The marker must be published before the remote is resolved, and the field merged
  into it afterwards** — or the resolution must be done before the `jq -n` that builds
  the marker. Prefer the latter (one atomic write, no read-modify-write on a file two
  unlocked writers share). The `git` call goes *above* line 61.

`git` may be missing entirely. The hook already treats a missing `jq` as "do nothing and
exit 0" (`sessionend.sh:36-39`); a missing `git` must degrade the same way — omit the
field, log one line, still write the marker. **A session's shutdown may never be blocked
or failed by this feature.**

### D4.5 — The processor change

One branch inside step 2 of `_process_marker`, replacing lines 1016-1026. Shape:

```python
remote_url = _git_remote(cwd)
canonical = _canonical_from_marker(cfg, session_id, marker, remote_url)
if canonical is None:
    _resolve_marker(cfg, marker_path, identity, session_id)
    _record(report, cfg, session_id, "dropped_no_remote", <detail per R5/R6>)
    return
```

`_canonical_from_marker` is a new module-level helper holding the whole of D4.2 — one
function, one table, testable in isolation, and the single place a reviewer has to read
to audit the trust boundary. It must be **total**: it is called inside a per-marker loop
and an escaping exception would abort the pass (the argument `sweep._canonical_remote`
already makes at `sweep.py:545-560` applies verbatim, including the two confirmed
`_git_remote` escapes — `EACCES`/`EPERM` from `Path.is_dir()` on macOS TCC, and
`UnicodeDecodeError` from a non-UTF-8 `origin` URL).

### D4.6 — What deliberately does NOT change

- `sweep.py` synthetic markers carry no `cwd_remote_url`; they are produced at
  processing time, when the sweep knows nothing the processor does not. Falling through
  to R6 is correct, and the sweep's eligibility precheck keeps its current semantics.
- `normalize()`, `state.json`, `content_hash`, the POST payload, `okf.wrap_entry`,
  `contract/SCHEMA.md`, `contract/FLAGS.md` and `prompts/` — all untouched
  (`AGENTS.md` §13).
- Idempotency: the fallback changes only *which canonical string* step 2 produces.
  Steps 4-9, the pointer and the server-side `content_hash` uniqueness are reached
  exactly as before, so reprocessing still yields `409 duplicate` (I6).

## Implementation Phases

### Phase 1 — Contract (no code)

**Deliverable:** `contract/ATTRIBUTION.md` (new) containing D4.1 (the schema), D4.2 (the
six-row authority table), D4.3 (the verifiability argument) and D4.6 (the frozen
surfaces this does not touch), plus a pointer from `DESIGN.md` §4.2 step 2 and the
`AGENTS.md` note if one is warranted.

**Gate:** owner review and explicit acceptance, per the 2026-09-12 coordination comment.
**Nothing in phases 2-4 may start before this is accepted.**

### Phase 2 — Goal wave `n` (frozen oracle, authored before implementation)

Per `AGENTS.md` §9: every acceptance criterion becomes an
executable assertion **before** any implementation.

**Deliverable:** `test/waves/n/test_goal_issue4_attribution.py`, exercising the real
boundary — a real git repo, a real linked worktree, a **real deletion**, the real
`sessionend.sh` run against a recorded SessionEnd payload, the real processor, a capture
HTTP server, a stub `claude`.

Registration, in the same commit (this is a checked property, not a convention —
`client/tests/e2e/test_blockt_e2e.py:1602-1610` explains why the floor moves with the
count):

- `test_blockt_e2e.py:129` — `"abcdefghijklm"` → `"abcdefghijklmn"`
- `test_blockt_e2e.py:1609` — the `>= 267` floor moves up by exactly the number of
  tests wave `n` adds
- `test/waves/README.md` — a row for wave `n`, naming its boundary
- the relevant `test/phase_gates/phase_N_e2e_config.json`, if wave `n` is to be
  part of a phase gate

Expected identities are **independent literals** (`github.com/org/b`-style constants as
in `client/tests/integration/conftest.py:26-27`), never recomputed by calling
`normalize()` — the coordination comment requires this explicitly.

### Phase 3 — Implementation

1. `client/hooks/sessionend.sh` — D4.4. Resolve above the `jq -n`, portable timeout
   tier, degrade silently.
2. `client/loom/processor.py` — D4.5. `_canonical_from_marker` + the step-2 branch.
3. `client/tests/integration/test_processor.py` — the R1-R6 table as unit-level
   integration cases. `Env.drop_marker(**extra)`
   (`client/tests/integration/conftest.py:98`) already forwards arbitrary marker fields,
   so no fixture change is needed.
4. `client/tests/integration/test_hook_install.py` — the hook actually writes the field
   for a real repo, omits it for a non-repo `cwd`, and redacts a tokenized one (AC8).
   Per-field assertions at `:249-254,399-402`; additive-safe as they stand.
5. `DESIGN.md` §4.1 (`:104-125`) — the design's own hook snippet duplicates the marker
   field list and would otherwise drift out of date the moment the hook changes.
6. The build plan's record of frozen seams — record the marker-schema amendment, the
   way the earlier `p2-marker-origin` and `p4-read-cwd-strict` seams were recorded.
7. `client/loom/cli.py:925-933` (`_marker_tags`) — **only** if the field should surface in
   `loom status`; if it should, `client/tests/integration/test_cli_status.py` covers it.
   Default: leave it out, the `hook.log` diagnostics are enough.

Explicitly **not** touched: `client/install.sh` copies the hook verbatim (`:80-81`) and
creates the state tree, so it needs no change; and `client/tests/unit/test_sweep.py:154`
stays exactly as written (see D4.1).

### Phase 4 — Surfacing and documentation

1. `loom doctor` row: does the installed hook capture attribution? The hook in
   `~/.loom/hooks/` can be older than the repo's after an upgrade, and the symptom of
   the stale one is a *silent* loss. There is a precedent row for exactly this shape of
   question at `client/loom/cli.py:1387-1420`.
2. `docs/RUNBOOK.md` — how to read `attribution_from_marker` / `attribution_superseded`
   in `hook.log` / `processor.log`, and what to do about L-1. The troubleshooting section
   already cites marker fields (`:704-705` queue paths, `:849` `adapter_error`).
3. `sweep.py` — the per-session diagnostic for an unresolvable `cwd` (see L-1). Small,
   and it closes acceptance criterion 4 for the sweep path as well.
4. `README.md` / `DESIGN.md` §4.2 — the one-line behavioral note.

**Full-suite gate (`AGENTS.md` §5):** `make test` plus `LOOM_SKIP_LIVE=1 make contract`,
and the wave sweep under `LOOM_GATE=1` (Docker daemon and `gitleaks` 8.30.1 required —
`AGENTS.md` §Environment).

### Sequencing against the other open issues

The coordination comment pins this: **#4 has no semantic dependency on #3, but both
touch the SessionEnd hook. Integrate #3 first, then rebase and run the combined
hook/processor/sweep acceptance suite before integrating #4.** **Verified: issue #3 has landed on `main`** (`aaae4ec`, merged as `ec2ee05`, plus the
`4f4a506`/`02e0085`/`4bb6807` follow-ups). The hook on disk is already the post-#3,
143-line version with the launcher tier at `sessionend.sh:127-141`. So there is nothing
to wait for — but branch from current `main`, not from `971ea6c`, or §D4.4's tiered
timeout will be written against a hook that no longer exists and will conflict.

## Acceptance Criteria

Each maps to at least one assertion in `test/waves/n/`; none may be left unasserted
(`AGENTS.md` §9), and none may be weakened to get green (§12).

- [x] **AC1 — the fix.** A session whose worktree is created, used, closed via the real
      `sessionend.sh`, then **deleted**, is processed into the correct registered project
      on the next pass. Asserted on the captured `POST /api/entries` body's
      `project_remote`, compared against a **literal** canonical string.
- [x] **AC2 — before the first pass.** The deletion happens between the hook's marker
      write and the first `run_once`. Asserted end to end, using the hook itself as the
      capture mechanism (not a hand-written marker).
- [x] **AC3a — reused directory.** Worktree for repo A deleted; a repo-B worktree
      created at the **same path**; the surviving A marker is processed. Assert the entry
      is **not** attributed to A, and that R3's `attribution_superseded` diagnostic is
      emitted.
- [x] **AC3b — changed remote.** `origin` is rewritten between SessionEnd and the pass.
      Assert the **live** remote decides and the session is not filed under the captured
      one.
- [x] **AC4a — absent identity.** Worktree deleted, marker written by a pre-fix hook (no
      `cwd_remote_url`). Assert `dropped_no_remote`, **zero** `POST /api/entries`, and a
      log line naming the session.
- [x] **AC4b — unusable identity.** `cwd_remote_url` present but not normalizable (and,
      separately, not a `str`). Assert `dropped_no_remote` with the R5 detail, zero
      POSTs, and **no exception escaping the pass** — the marker after it must still be
      processed in the same `run_once`.
- [x] **AC5a — still fail-closed.** A fallback-resolved remote that is **not registered**
      is dropped as `dropped_not_registered`, zero POSTs.
- [x] **AC5b — still idempotent.** Processing the same fallback-attributed session twice
      yields one stored entry: second pass records `duplicate` (409) and the pointer does
      not regress.
- [x] **AC6 — no regression.** Every existing disposition in
      `client/tests/integration/test_processor.py` is unchanged, and the full suite plus
      waves `a`-`m` stay green.
- [x] **AC8 — no credential reaches disk.** The hook runs in a repo whose `origin` is
      `https://user:tok@host/org/repo.git`. Assert the marker file contains no `tok`
      substring, that `failed/` never does either after a forced park, and that the
      processor still derives the literal `host/org/repo`. Also assert the scp-like form
      survives redaction unchanged.
- [x] **AC7 — the hook is still unbreakable.** With `git` absent from `PATH`, with a
      `cwd` that is not a repo, and with a `cwd` that no longer exists, the hook still
      writes a valid marker, still exits 0, and adds no measurable latency to session
      shutdown.

## Alternatives Considered

**A-1 — persistent `~/.loom/paths.json` ledger (path → canonical remote).**
Rejected. It is keyed by **path**, which is precisely the key that ephemeral worktrees
recycle; a reused `/tmp/agent-wt-1` would resolve to whatever repo used it last. The
issue asks for a *session*-keyed association for this reason. It also adds a file with
its own retention, atomicity and concurrent-writer story next to a queue that already
has one.

**A-2 — install a `SessionStart` hook and record the association at session start.**
This is the only option that would also cover a session that **crashes** in a worktree
that is then deleted (the sweep path, L-1). Rejected for now on cost/benefit: it
requires a second hook type in `~/.claude/settings.json`, a second install/merge path in
`install.sh`, a second doctor row and a second uninstall path, and `FLAGS.md` has
verified payloads only for `SessionEnd`. Worth reopening if L-1 is observed in the pilot.

**A-3 — have the processor cache `cwd → remote` on first successful resolution.**
Rejected by the issue itself: *"caching only after first processing is not complete
coverage"*. In the exact scenario this issue is about, there is no first successful
resolution.

**A-4 — infer the project from the transcript's content or from the encoded transcript
directory name.** Rejected: an explicit non-goal in the issue, and the encoded directory
name is a dead path by construction (the same dead path).

## Risk Analysis & Mitigation

| Risk | Why it matters | Mitigation |
|---|---|---|
| Hook slows or blocks session shutdown | The hook is on the user's exit path; D2 exists so it can never fail on timeout | Bare `git remote get-url` measured at 1 ms; tiered `timeout`/`gtimeout`; `-d "$CWD"` precondition; AC7 asserts it |
| `timeout` missing on macOS | Silent no-op on darwin — the exact shape of issue #3 | Tier it like `sessionend.sh:127-141`; assert both branches, as issue #3's tests do |
| Shell/`jq` re-implementation of `normalize()` drifts from Python | Silent misfiling under a key nobody computes | Capture the **raw** URL; normalize only in Python (D4.2) |
| Authority rule inverted (captured beats live) | Silent cross-project attribution — the worst outcome in the issue | R3 pinned in `contract/ATTRIBUTION.md`, asserted by AC3a **and** AC3b |
| New helper raises inside the per-marker loop | Aborts the pass; every later marker starved, forever | `_canonical_from_marker` total by construction; AC4b asserts the next marker still processes |
| Marker-field addition trips a frozen gate | Red suite, or worse, a weakened assertion to get green | Phase 2 registers wave `n` and moves the floor in the same commit; `AGENTS.md` §12 |
| Raw `origin` carries a live token | Credential at rest in `queue/`, and indefinitely in `failed/` after a park (`processor.py:1338`); violates I5/I6; trips `gitleaks` | D4.1b redaction in the hook via `jq sub()`; AC8 asserts it, including after a forced park |
| A wrong attribution is unrecoverable | `content_hash` is globally UNIQUE (`server/schema.sql:25`); a re-post under the right project returns 409 and is read as success | Never guess: R5/R6 drop rather than fall back, and a drop leaves the pointer and transcript recoverable (L-2) |
| Hook in `~/.loom/hooks/` stale after upgrade | Feature silently absent on a pilot machine | Phase 4 `loom doctor` row |

## Known Limits (explicit, to be recorded in the contract)

- **L-1 — the sweep path stays uncovered.** A session that dies *without* `SessionEnd`
  (crash, SIGKILL, WSL shutdown) in a worktree that is then deleted has no marker and no
  captured remote. `sweep._is_eligible` (`sweep.py:524-535`) resolves the dead `cwd`,
  returns ineligible, and withholds the transcript on every pass — silently. This plan
  does not close it; A-2 is the option that would. It must be **named in the contract**
  so it is a known residual and not a surprise.

  **"Silently" is literal and is worth fixing cheaply even though the gap stays open.**
  The withhold only increments `ps.counters["ineligible"]`; there is no per-session
  `_log`, unlike the `sweep_enqueued` line at `sweep.py:453`. So there is no log line, no
  `loom status` row and no counter a human would read as data loss. It also silently
  removes a property the sweep advertises at `sweep.py:527-532` — that registering a
  project later makes historical orphans deliverable with no manual intervention — for
  every worktree session. **Phase 4 should add a per-session diagnostic naming the
  unresolvable `cwd`.** That is a few lines, it closes acceptance criterion 4 for the
  sweep path too, and it converts an invisible loss into a visible one without expanding
  this issue's scope.
- **L-2 — reused-path misattribution under R3.** When a path is reused and the live
  remote resolves to the *new* repo, R3 files the old session under the new project (the
  pre-existing behavior described in §Problem Statement). R3 makes it *visible*
  (`attribution_superseded`) rather than fixing it; fixing it would mean letting the
  captured value beat live, which acceptance criterion 3 forbids. Recorded as a known
  limit, not as this issue's scope.

  **A misattribution is terminal, and that raises the stakes on every rule in D4.2.**
  `server/schema.sql:25` declares `content_hash TEXT UNIQUE NOT NULL` — unique
  **globally**, not per project — and `compute_content_hash` (`processor.py:784`)
  deliberately excludes `project_remote` from its material. So once a delta is stored
  under project A, re-posting the identical delta under B returns `409`, which the client
  treats as **success**: `duplicate`, pointer advanced, marker deleted
  (`processor.py:1216-1223`). There is no "post it to the right project instead" recovery,
  not even manual.

  Two consequences the contract must state. First, the good news: attribution is not in
  the hash, so **changing how a project is resolved can never create duplicates** — AC5b
  is safe by construction. Second, the governing one: **any branch that might guess is
  strictly worse than dropping**, because a drop leaves the pointer untouched and the
  transcript on disk (recoverable forever), while a wrong post is permanent. This is the
  reason R5 and R6 drop rather than fall back to anything, and it is the strongest
  argument for R3 as written.

## References

**Repo (baseline `971ea6c`)**
- `client/loom/processor.py:1007-1037` — step 2, the drop this plan removes
- `client/loom/processor.py:621-637` — `_git_remote`
- `client/loom/processor.py:103-119` — `_CATEGORY`, untouched
- `client/loom/processor.py:927-955` — `_resolve_marker` / `_marker_is_still_ours`
- `client/hooks/sessionend.sh:59-66` — the atomic marker write
- `client/hooks/sessionend.sh:127-141` — the tiered launcher pattern to mirror (issue #3)
- `client/loom/normalize.py` — `normalize()`, identity authority (D1)
- `client/loom/sweep.py:524-560` — eligibility precheck and the totality argument
- `client/tests/integration/conftest.py:26-30,98-114` — `TEST_REMOTE_*`, `_make_git_repo`, `drop_marker(**extra)`
- `client/tests/e2e/test_blockt_e2e.py:129,1602-1610` — wave registration and the collection floor
- `test/waves/README.md` — the frozen-oracle doctrine and the wave table
- `DESIGN.md` §1 (D1-D7, I1-I6), §4.2 (the pipeline), §6.2 (frozen API)
- `AGENTS.md` §5, §9, §10, §12, §13 — verification, unasserted criteria, real boundaries, no weakening, frozen contracts

**Issue**
- Issue #4
- Coordination comment, 2026-09-12 — contract-first, #3-before-#4, independent literals
- Precedent plan: `docs/plans/2026-09-13-fix-sessionend-kick-portable-without-setsid-plan.md` (issue #3, same file, same class of portability trap)
