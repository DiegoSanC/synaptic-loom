---
title: "fix: Make the SessionEnd processor kick portable (macOS has no setsid)"
type: fix
date: 2026-09-13
complexity: medium
github_issue: 3
branch: fm/portable-sessionend-kick
---

# fix: Make the SessionEnd processor kick portable (macOS has no setsid)

## Overview

`client/hooks/sessionend.sh:84` launches the processor with `setsid`, a **util-linux**
binary that macOS does not ship. On darwin the kick never fires: the marker is written,
the hook still exits 0, and the session is only processed up to 15 minutes later by the
periodic trigger. One integration test has been failing on darwin since phase 1 and is
carried as a *sanctioned known failure* in four `phase_e2e_config.json` files and two e2e
gates.

The fix is small — **one launch line in a 89-line shell script** — but it sits inside an
unusually dense web of frozen contracts, and the two obvious implementations both break
something. This plan's value is mostly in §Constraints and §Evidence: the traps are
non-obvious and each one has been reproduced on this machine rather than reasoned about.

**Scope:** the hook's launch line, a `loom doctor` row, tests, and two documentation
updates. No change to extraction semantics, the marker protocol, the processor, or any
shared contract.

## Problem Statement

```
client/hooks/sessionend.sh:82-87
PY="$LOOM_HOME/venv/bin/python"
if [ -x "$PY" ]; then
  setsid "$PY" -m loom.processor --quiet >>"$LOG_DIR/processor.log" 2>&1 </dev/null &
else
  log "venv python not executable at $PY; kick skipped (the marker will be processed later)"
fi
```

The comment directly above that line states the design intent, and it is the single most
important sentence in this issue:

```
sessionend.sh:74-76
# --- 6. Kick (P1): launch the processor FULLY decoupled; it outlives the hook.
#        setsid + `&` + `< /dev/null` -> new session group, independent of the hook's own
#        process (the known killed-async-hooks bug does not apply: it is no longer its child).
```

`setsid` was chosen **for the new-session property specifically**, to dodge a known Claude
Code bug that kills async hook children. Any replacement that does not create a new session
reintroduces exactly the bug this line exists to avoid. That rules out the two fixes an
implementer reaches for first (see §Evidence).

`setsid` is invoked unconditionally whenever the venv Python exists. On darwin it is not on
PATH, so bash reports `setsid: command not found` **into the background job's stderr** and
the kick silently does not happen.

Three distinct defects, only the first of which the issue title names:

1. [x] **Portability.** No new session on darwin, so no immediate processing at all.
2. [x] **Observability.** The failure goes to `processor.log` (the redirect target), never to
   `hook.log`. `loom doctor` has no row for it. Reproduced below: after a failed kick,
   `hook.log` contains only `marked session <id>` — an operator reading the hook's own log
   sees a *successful* session end.
3. [x] **No graceful degradation.** There is no fallback path, so the only recovery is the
   15-minute periodic trigger — which the installer already acknowledges in a comment
   (`client/install.sh:676-677`) that this change makes stale.

What is **already correct** and must stay correct: the marker is written atomically before
the kick is attempted, the hook exits 0 on every path, and the `LOOM_SUMMARIZER=1`
anti-recursion guard returns before anything is created (`sessionend.sh:23`).

## Evidence — reproduced on this machine (WSL2/Linux, 2026-09-13)

No macOS machine was needed. The darwin condition is **deterministically reproducible on
Linux** by building a PATH that omits `setsid`, which is what makes AC1 and AC5 testable in
CI. Running the real hook against the real `contract/sessionend_payload.json` under
`PATH=<shim without setsid>`:

```
hook exit=0
marker            : 733240a1-....json          <- written
kick_argv.txt     : ABSENT                     <- kick did NOT fire
hook.log          : "marked session 733240a1-..."   <- no trace of the failure
```

### Launch-strategy matrix (measured, not assumed)

Each variant was run from a non-interactive bash script with `setsid` absent, launching a
stub at `$LOOM_HOME/venv/bin/python` that records its own argv, pid/pgid/sid, and whether
it outlived its parent.

| # | Variant | argv preserved | New session (`pid==pgid==sid`) | Outlives parent | Nonblocking |
|---|---|---|---|---|---|
| A | `( cmd & ) &` double-fork | ✅ | ❌ `sid` = launcher's | ✅ | ✅ 17ms |
| B | `( nohup cmd & ) &` | ✅ | ❌ `sid` = launcher's | ✅ | ✅ 18ms |
| C | `perl -MPOSIX -e 'POSIX::setsid(); exec @ARGV' --` | ✅ | ✅ | ✅ | ✅ 4ms |
| D | `python3 -c '<setsid+execv>'` | ✅ | ✅ | ✅ | ✅ |
| E | `"$PY" -c '<setsid+execv>'` (venv python as its own launcher) | ❌ **breaks the frozen argv assertion** | ✅ | ✅ | ✅ |

**A and B are the intuitive fixes and both are wrong for AC2**, and `sessionend.sh:74-76`
says why in the repo's own words. They reparent to init and
survive a parent *exit*, but the child stays in the hook's **process group and session**.
`setsid`'s actual guarantee — immunity to a process-group-directed signal and to the
controlling terminal going away — is exactly what they do not provide. A plan that shipped
`nohup` would look green on every test that only checks "did it survive the hook returning".

**E breaks the test contract.** Using the venv Python as its own detacher makes the
recorded argv `-c import os,sys; os.setsid(); ... -m loom.processor --quiet` instead of
`-m loom.processor --quiet`, because the test's stub is a fake `python` that records and
exits without exec'ing. Making the stub emulate `exec` would be simulating the seam this
change owns (AGENTS.md §3). **The detacher must not be `$PY` itself.**

### The EPERM trap (this is the one that would ship latent)

`os.setsid()` fails with `EPERM` when the calling process is **already a process-group
leader**. In a non-interactive shell that is not the case — but with job control on
(`set -m`), every background job *is* its own group leader. Measured:

```
naive setsid(), job control OFF  -> argv recorded, pid==pgid==sid   OK
naive setsid(), job control ON   -> PermissionError: [Errno 1]      LAUNCH SILENTLY FAILS
fork-first,     job control ON   -> argv recorded, pid==pgid==sid   OK
```

The launcher **must fork before calling `setsid()`**. The naive one-liner passes every
test we would naturally write and then fails on a host whose shell enables job control.

## Proposed Solution

Replace the single `setsid` call with a **tiered launcher**, tried in order, with the tier
actually used recorded in `hook.log`:

1. **`setsid`** when on PATH — unchanged behavior on Linux/WSL2, zero new dependency.
2. **`python3 -c '<fork; setsid; execv>'`** — fork-first, preserves argv verbatim, yields a
   true new session.
3. **Neither available** — do not pretend. Log `kick skipped (no session-detach launcher);
   the marker will be processed by the periodic pass` and exit 0.

**Why `python3` and not `perl` for tier 2** — both work (variants C and D), but `python3`
costs nothing extra:

- It is already a hard prerequisite of the product: `install.sh` builds the venv from a
  python3/uv (`install.sh:545`).
- It is already in the goal waves' binary allowlist (`test/waves/_goalkit.py:183`
  `SAFE_BIN_NAMES`); `perl` is not, so choosing perl means editing that shared accessory.
- `docs/RUNBOOK.md:50` already records python3.12 as a verified host tool.

Tier 3 is deliberately *refusal plus a log line*, not variant B. A `nohup` tier would fire
a process that is not in a new session while logging "kick fired" — it would convert a
loud, diagnosable gap into a quiet, conditional one. **Owner-confirmed 2026-09-13**: no
`nohup` tier (§Open Decisions item 1).

Plus the observability half:

- `hook.log` records which tier launched, or that none did.
- A new `loom doctor` row names the available launcher and warns when none is.
- `client/install.sh:676-677` and `docs/RUNBOOK.md` §1/§5 updated.

## Constraints — what the surrounding contracts forbid

These are the traps. Each was verified by reading the asserting file.

| # | Constraint | Why | Source |
|---|---|---|---|
| C1 | The launched argv must remain **exactly** `-m loom.processor --quiet` | asserted verbatim; rules out variant E | `test_hook_install.py:117` |
| C2 | **Do not add any `skip`** to `client/tests` or `server/tests` | the blockt gate asserts `counts["skipped"] == 0` over the driven suites | `test_blockt_e2e.py:1689` |
| C3 | The new `doctor` row must be `ok`/`warn`, **never `fail`** | `assert "✗" not in out` and `"Environment OK."`; the doctor fixture's PATH is a fakebin holding only `claude/jq/systemctl/crontab` prepended to the real PATH | `test_cli_doctor.py:87`, `test_phase2_e2e.py:696` |
| C4 | The new `doctor` row's keywords must not collide with existing rows, and must carry **no absolute tmp path** | frozen doctor oracles match rows by keyword | `cli.py:_unit_integrity_check` docstring, `test/waves/d:862` |
| C5 | **Do not remove** the `setsid`/`darwin`/`test_hook_install` literals from the two e2e gates | frozen goals assert those literals are present *in the gate source* | `test/waves/h:535`, `test/waves/l:882` |
| C6 | Adding a `test/waves/n` forces editing frozen gate internals | `GOAL_WAVE_DIRS` is `"abcdefghijklm"` and the floor `>= 267` must move by the exact new count, in the same commit | `test_blockt_e2e.py:129,1609` |

**C5 is counter-intuitive and is the most likely thing to get wrong.** The instinct after
fixing the bug is to delete the now-obsolete known-failure tolerance. Do not. Two frozen
goal files assert those literals exist in the gate source, and the tolerance is
**permissive** — it reads `unexpected = [n for n in failed if n != SANCTIONED_FAILURE]` and
then `if failed:`. A suite where nothing fails satisfies it trivially. Leaving it is both
required and harmless. Retiring it is a separate, later decision once darwin is verified
green (see §Open Decisions).

**C6 argues against a new goal wave.** The minimal public interface for this behavior is
already `client/tests/integration/test_hook_install.py`, which both e2e gates already drive
as part of the client suite, and whose floors are `>=` so added tests are free. Recommend
**no `test/waves/n`** — it would buy nothing and force touching two frozen gates.

## Where the assertions live

| Assertion | Home | New? |
|---|---|---|
| Kick fires with `setsid` absent from PATH (AC1) | `test_hook_install.py` | new test, PATH-shim pattern already in `test_hook_missing_jq_exits_zero:170` |
| Kick fires with `setsid` present (Linux, no regression) (AC2) | `test_hook_install.py:98` | existing, unchanged |
| Launched process is in a **new session** (`pid==pgid==sid`) (AC2) | `test_hook_install.py` | new — the stub records `os.getsid(0)`; this is the assertion that rejects `nohup` |
| Hook returns before the child finishes (nonblocking) (AC2) | `test_hook_install.py` | new — stub sleeps, hook's wall time is asserted below it |
| Marker survives a failed launch; periodic pass recovers it (AC3) | `test_hook_install.py` | new — PATH with neither `setsid` nor `python3` |
| `LOOM_SUMMARIZER=1` still no-ops; user hooks preserved (AC4) | `test_hook_install.py:139`, `test_install_update.py` | existing, unchanged |
| `doctor` names the launcher / warns when absent (AC5) | `test_cli_doctor.py` | new, `warn`-max per C3 |
| Platform prerequisites documented (AC5) | `docs/RUNBOOK.md` §1, §5 | doc change |

### How to assert "outlived the hook" without a flaky sleep

Do **not** assert on timing. Assert on **kernel-visible identity**: have the stub write its
`pid`, `os.getpgid(0)` and `os.getsid(0)`, then assert `pid == pgid == sid`. A process that
is its own session leader is by definition detached from the hook's session — that is a
deterministic property, not a race. `_wait_for()` (`test_hook_install.py:81`) already
exists for the file-appearance poll and needs no change.

A *weak* version of AC2 to avoid: asserting only that `kick_argv.txt` eventually appears.
Variants A and B both satisfy that and both fail the actual requirement.

## Implementation Phases

### Phase 1 — Assertions first (red)

1. Extend the kick stub to record `pid/pgid/sid` alongside argv.
2. Add the session-identity assertion to the **existing** happy-path test. On Linux with
   `setsid` this passes immediately — it pins current behavior before anything changes.
3. Add `test_hook_kicks_processor_without_setsid_on_path` — fakebin PATH with `jq`,
   `python3`, coreutils, and **no** `setsid`. Red today (reproduced above).
4. [x] Add `test_hook_marker_survives_when_no_launcher_is_available` — PATH with neither
   `setsid` nor `python3`: marker exists, exit 0, `hook.log` names the skip. Red today
   (no such log line exists).

Gate: the three new assertions fail for the stated reasons and nothing else regresses.

### Phase 2 — The launcher (green)

5. [x] Replace `sessionend.sh:82-87` with the tiered launcher. Keep the `[ -x "$PY" ]` guard and
   its existing log line untouched, and **update the `:74-76` comment** so it describes the
   tiers rather than `setsid` alone — it is the file's statement of intent.
6. [x] The tier-2 payload must **fork before `setsid()`** (§EPERM trap) and must exec `$PY` with
   argv `-m loom.processor --quiet` unchanged (C1).
7. [x] Log the tier used.

Gate: `pytest client/tests/integration/test_hook_install.py -q` green; the new
no-`setsid` test passes.

### Phase 3 — Diagnostics

8. [x] Add `_kick_launcher_check()` to `client/loom/cli.py` following the
   `-> tuple[str, str]` pattern of `_trigger_check`, prepended into `checks` in
   `cmd_doctor`. `ok` when a launcher is available, `warn` when not — **never `fail`** (C3).
   Keyword must be distinct and carry no absolute path (C4).
9. [x] Add the doctor test.

Gate: `test_doctor_all_green_exits_zero` still exits 0 with `"✗" not in out`.

### Phase 4 — Documentation and verification

10. [x] `client/install.sh:676-677` — the comment still says the kick is best-effort because
    "setsid is missing on darwin". Rewrite: the kick is best-effort because it does not
    survive a crash; the periodic pass remains the recovery path. **Do not** weaken the
    periodic trigger; it stays exactly as it is (issue Scope).
11. [x] `docs/RUNBOOK.md` §1 — add the launcher prerequisite to the verified-tools list.
    §5 (onboarding) — a short "supported platforms" note: Linux/WSL2 via `setsid`, macOS via
    `python3`, and what an operator sees if neither is present.
12. Run the full suite and `LOOM_SKIP_LIVE=1 make contract` (AGENTS.md §5).

### Phase 5 — AC5's honest limit

AC5 says "run the relevant hook/install/doctor tests on representative supported
environments". **There is no macOS machine and no macOS CI in this project.** Discharge it
as follows, and record the gap rather than implying coverage that does not exist:

- **Mechanically verified on Linux:** the `setsid`-absent path, via the PATH shim. This is
  the actual failure mechanism, so the *code path* darwin takes is covered in CI.
- **Not verified:** that macOS's process lifecycle at Claude Code session shutdown behaves
  as Linux's does. Record this explicitly in the PR and in RUNBOOK §5.
- **To close it:** one manual run of `pytest client/tests/integration/test_hook_install.py
  client/tests/integration/test_cli_doctor.py -q` on a Mac, pasted into the PR. Until that
  exists, the darwin sanction in the four `phase_e2e_config.json` files **stays** (C5).

## Alternative Approaches Considered

| Approach | Verdict |
|---|---|
| `nohup` / `( cmd & ) &` | **Rejected.** Measured: no new session (variants A/B). Satisfies a naive reading of AC2 while failing its intent. `server/start.sh:37` uses `nohup`, but that is a foreground-managed server with a pidfile — a different problem. |
| `perl -MPOSIX` launcher | **Viable, not chosen.** Works identically (variant C) and perl is stock on macOS, but it is not in `SAFE_BIN_NAMES` and adds a dependency python3 already discharges. Keep as fallback if python3 proves unreliable on a pilot Mac. |
| Venv `$PY` as its own detacher | **Rejected.** Breaks C1; fixing it means teaching the test stub to `exec`, i.e. simulating the seam under test. |
| `--detach` flag on `loom.processor` | **Rejected.** Changes the launched argv (C1) and moves process-lifecycle logic into the product for one caller's benefit. |
| `launchctl submit` on macOS | **Rejected.** Registers persistent per-user state — the issue's non-goals forbid globally modifying the developer's environment. |
| Drop the kick, rely on the 15-min trigger | **Rejected.** The issue explicitly forbids it: "Keep periodic processing as recovery, not as an excuse to silently break the immediate kick." |
| Add a `test/waves/n` goal wave | **Rejected.** See C6. |

## Acceptance Criteria

Restated as falsifiable assertions; issue AC in brackets.

- [x] **[AC1]** With `setsid` absent from PATH, the hook launches the processor and the
      recorded argv is exactly `-m loom.processor --quiet`.
- [x] **[AC2a]** The launched process satisfies `pid == pgid == sid` — it is a session
      leader — on both the `setsid` and the `python3` tier.
- [x] **[AC2b]** The hook returns while the launched process is still running.
- [x] **[AC2c]** The existing Linux/`setsid` path is asserted, not merely left alone.
- [x] **[AC3]** With no launcher available: exit 0, marker present and well-formed, and
      `hook.log` names the skipped kick. A subsequent `loom process` consumes the marker.
- [x] **[AC4]** `LOOM_SUMMARIZER=1` still creates no marker, no queue and no kick; the
      installer still preserves unrelated hooks and keys.
- [x] **[AC5a]** `loom doctor` shows a launcher row; `warn` when none, and `loom doctor`
      still exits 0 with no `✗` in the all-green fixture.
- [x] **[AC5b]** `docs/RUNBOOK.md` states the per-platform prerequisite and what is *not*
      verified on darwin.
- [ ] **[regression]** `make test` green; `test/waves` 275 passed (unchanged — no new wave);
      client+server **>= 909 passed, 0 failed, 0 skipped** (909 collect today; this change
      adds tests, so the count rises and must never fall); `LOOM_SKIP_LIVE=1 make contract`
      green.
- [x] **[contracts]** No frozen goal file edited; the `setsid`/`darwin`/`test_hook_install`
      literals still present in both e2e gates; `phase_e2e_config.json` files unchanged.

## Risk Analysis & Mitigation

| Risk | Severity | Mitigation |
|---|---|---|
| Implementer ships `nohup` because it makes the test pass | **High** — silently fails AC2's intent | The `pid==pgid==sid` assertion (Phase 1, step 2) is written *before* the fix and rejects it |
| Naive `os.setsid()` ships and fails on a job-control host | **High** — silent, host-dependent | fork-first is mandated in Phase 2 step 6; add a `set -m` case to the test |
| Implementer "cleans up" the now-dead darwin sanction | **Medium** — breaks two frozen goals | C5, called out twice |
| New doctor row turns `doctor` red where a launcher is absent | Medium | C3: `warn` ceiling, asserted |
| A new test introduces a `skip` | Medium — breaks the blockt gate | C2; use `pytest.fail`-style hard assertions, never `skipif` |
| `python3` genuinely absent on a pilot Mac | Low | Tier 3 degrades loudly; perl fallback held in reserve |
| Merge conflict with issue #4 | Low | Known and sequenced: integrate #3 first, then rebase #4 (issue #3 comment, 2026-09-12) |

## Open Decisions

Raised rather than decided locally (AGENTS.md §7). Item 1 was decided by the owner on
2026-09-13; items 2 and 3 remain open.

1. ~~Should there be a tier-3 `nohup` fallback?~~ **DECIDED 2026-09-13 (owner): no.** If
   neither `setsid` nor `python3` is available the hook logs `kick skipped (no
   session-detach launcher)` and returns 0; the periodic pass is the recovery. A
   same-session process logged as a successful kick is worse than a logged refusal.
   This is now a requirement, not a preference — **do not add a `nohup` tier**, and the
   AC3 test asserts the log line.
2. **When is the darwin sanction retired?** Not in this PR (C5 makes removal a frozen-goal
   change, and AC5 is not fully discharged without a Mac). Proposed: a follow-up once a
   macOS run is pasted into the PR.
3. **`python3` vs `perl` for tier 2.** Recommending python3 on dependency grounds; both are
   measured to work.

## Unverified — confirm before or during implementation

Everything in §Evidence was **measured on this machine** (WSL2/Linux, 2026-09-13). These two
questions were not settled and are deliberately not asserted anywhere above:

1. **Is `python3` on PATH on a stock pilot Mac?** On macOS `python3` is provided by the Xcode
   Command Line Tools; without them, invoking it triggers an install prompt rather than
   running. Mitigated rather than assumed: tier 3 degrades loudly, and `perl` is held in
   reserve (§Alternatives). **Cheapest check:** `command -v python3 perl setsid` on the pilot
   Mac — one line, settles tiers 2 and 3 outright. Worth doing before Phase 2.
2. **Does the Claude Code CLI kill the hook's process group at session end, and is there a
   hook timeout?** `sessionend.sh:74-76` asserts a "known killed-async-hooks bug", which is
   the repo's own prior, not a cited doc. This plan does not depend on resolving it — a new
   session is strictly safer than the status quo either way — but it is the reason the
   `pid==pgid==sid` assertion is worth its weight.

Neither blocks Phase 1, which is assertions only.

## Dependencies

None. Issue #3 is semantically independent of #2 and may proceed in parallel
(issue comment, 2026-09-12). It shares `client/hooks/sessionend.sh` with **#4** — an
integration conflict, not a semantic dependency: **integrate #3 first**, then rebase #4.

Note: the current branch is `fm/fly-packaging-deploy` (issue #2, PR #18 open). Branch this
work from `origin/main`, not from the current checkout.

## References

**Code**
- `client/hooks/sessionend.sh:82-87` — the defect
- `client/install.sh:676-677` — the stale comment; `:545` — python3/uv prerequisite
- `client/loom/cli.py:1756` `cmd_doctor`; `_unit_integrity_check` — the row pattern
- `client/loom/processor.py:1401` `main` — flock already makes concurrent kicks safe
- `server/start.sh:37` — the in-repo `nohup` precedent, and why it does not apply

**Tests / contracts**
- `client/tests/integration/test_hook_install.py:98,120,169` — the failing test, the argv
  assertion, the PATH-shim pattern
- `client/tests/integration/test_cli_doctor.py:19,87` — fakebin PATH, the `✗` assertion
- `client/tests/e2e/test_blockt_e2e.py:129,136,1609,1685,1689` — `GOAL_WAVE_DIRS`,
  `SANCTIONED_FAILURE`, the wave floor, the suite floor, the zero-skip assertion
- `client/tests/e2e/test_phase4_e2e.py:109,113,1259` — the second gate carrying the sanction
- `test/waves/h/test_goal_e12_e2e.py:535`, `test/waves/l/test_goal_e17_e2e.py:882` — frozen
  goals asserting the literals
- `test/waves/_goalkit.py:183` — `SAFE_BIN_NAMES`
- `test/phase_gates/phase_{1,2,3,4}_e2e_config.json` — the sanction records

**Issue**
- Issue #3 (+ coordination comment, 2026-09-12)
