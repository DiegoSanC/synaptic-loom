---
title: "fix: Bound what the processor cannot process, and accept a token of any shape"
type: fix
date: 2026-09-19
complexity: medium
github_issue: 22
also_closes: [23, 25]
baseline_in_issue: 23bd1ff
branch_from: main (23bd1ff — #27 has landed)
branch: fm/bounded-failures-and-token-shape
---

# fix: Bound what the processor cannot process, and accept a token of any shape

## Overview

Three open issues, all **pre-existing and latent** rather than regressions, all filed out
of the review of PR #24 and the wave-c token repair (`e67c0e0`). They are one PR because
they are one theme:

> **an input the code did not narrow, or a failure the code did not bound, silently
> disables something the pilot depends on.**

- **#22** — a valid-JSON non-object file in `queue/` aborts every pass, permanently.
- **#23** — a marker whose processing raises is retried forever, uncapped and unparked.
- **#25** — ~1 token in 64 starts with `-`, and the RUNBOOK's onboarding command then
  fails in argparse before `loom init` runs at all.

**Scope:** input narrowing at four JSON reads, one new bounded failure handler, one new
disposition constant, one argv normalization in the CLI, a normative row in `PLAN.md` §T4,
operator rows in `docs/RUNBOOK.md`, and regression tests in the suites that already own
this behavior. **No change** to extraction semantics, the summarizer, the secret scan, the
API payload, `state.json`'s schema, the marker schema, attribution, or any frozen contract.

**No new goal wave.** #2 and #4 got frozen oracles because they were features with
acceptance criteria authored before implementation. These are repairs to behavior the
existing suites already assert, so they belong in those suites; `GOAL_WAVE_DIRS` and the
`>= 288` collection floor are untouched.

## Problem Statement

### #22 — one file, and capture stops on that machine

`_read_marker` (`client/loom/processor.py:365-369`) is annotated `dict | None` and never
checks:

```python
def _read_marker(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
```

`_iter_markers` (`:435-443`) sorts with it:

```python
    def sort_key(p: Path):
        marker = _read_marker(p) or {}
        return (marker.get("marked_at") or "", p.name)

    return sorted(markers, key=sort_key)
```

`[1, 2]` is **truthy**, so `or {}` never fires and `.get` raises `AttributeError`. That
call is the **loop expression** of `run_once` (`:1445`), evaluated before the first `try:`
at `:1446`, so it escapes the per-marker isolation entirely and aborts the pass before any
marker is processed. The file is never resolved or removed, so every later pass dies
identically. Verified in the issue:

```python
(cfg.queue_dir / "bad.json").write_text("[1, 2]")
processor.run_once(cfg)
# AttributeError: 'list' object has no attribute 'get'
```

Truncated or torn files are **not** a vector — `JSONDecodeError` is already caught. The
vector is well-formed JSON that is not an object: a foreign writer, a manual edit, or
filesystem corruption.

### The blast radius is wider than the issue states

Two further doors, found while planning, both outside the per-marker `try`:

1. **The registry cache.** `_load_registry` (`:725-733`) calls the same `_read_marker` on
   `projects_cache.json` and then `cache.get("projects")`. That file is written from the
   **server's** `resp.json()` — it is remote-controlled. `sweep._registry_remotes`
   (`sweep.py:481-531`) already guards exactly this file and says why at length; the
   processor's own read does not.
2. **A non-string `marked_at`.** `{"marked_at": 5}` yields the tuple `(5, "x.json")`,
   which raises `TypeError` when `sorted()` compares it against `("2026-…", "y.json")` —
   same function, same blast radius, different exception.

And `_read_marker_with_identity` (`:398-421`) has the same gap on the `_process_marker`
path: `json.loads(b"[1,2]")` returns a `list`, the `if marker is None` guard at `:1067`
passes, and `:1073` raises. That one *is* contained by the loop's handler — which is #23.

### `loom status` crashes on the same file

`cmd_status` reads each marker with the same unnarrowed idiom, at three sites
(`client/loom/cli.py:1094`, `:1104`, `:1111`):

```python
        data = _read_json(path) or {}
        print(f"  {data.get('session_id', path.stem)}  …")
```

`_read_json` is annotated `object | None` precisely so callers narrow — and the **other
six** call sites in that file all do (`:1001`, `:1033`, `:1139`, `:1197`, `:1328`,
`:1550`). These three do not. So the command `docs/RUNBOOK.md` §6 prescribes for
diagnosing a stuck marker raises `AttributeError` on the very file that wedged the
processor. `DESIGN.md` §7 calls `loom status` "the cheap answer to silent failure"; it
fails at the exact moment it is needed.

This also constrains the fix: parking a non-object marker into `failed/` without repairing
`cmd_status` would move the crash rather than remove it.

### #23 — the one failure class that is not bounded

`run_once`'s per-marker handler (`:1445-1455`) records the failure and does nothing else:

```python
        for marker_path in _iter_markers(cfg):
            try:
                _process_marker(cfg, api, registry, state, report, marker_path)
            except Exception as exc:  # noqa: BLE001 — isolate the marker, don't abort the run.
                _record(report, cfg, marker_path.stem, "error",
                        f"{type(exc).__name__}: {exc}"[:160])
```

It never calls `_resolve_marker`/`_drop_marker` and never routes through `_bound_attempts`
(`:1371-1407`) — the attempts counter, `MAX_ATTEMPTS = 3`, park into `failed/` — which is
wired only from `_handle_summarizer_failure` and `_handle_adapter_failure`. So such a
marker gets **no attempt counter**, is **never parked**, is retried unchanged on **every**
future pass for as long as the condition persists, and surfaces only as a generic `error`
indistinguishable from any other.

Every other bounded failure class parks at 3. This one does not, and the asymmetry is
written down nowhere: `PLAN.md` §T4's error matrix is declared *normative* and has **no
row** for "`_process_marker` raised".

**A concrete way in.** `_log` (`:449-455`) does `cfg.log_dir.mkdir(...)` plus an unguarded
`open(..., "a")`, both of which raise `OSError` on a full or read-only `LOOM_HOME`.
`_canonical_from_marker` calls `_log` directly on its R3/R4 paths (`:694`, `:704`) — which
are reached when attribution **succeeded** — so a disk-full condition converts a good
capture into an escaping exception. Its own docstring (`:671-674`) already admits this:

> The `_log` calls are not bounded here and can still raise `OSError` on a full or
> unwritable disk; the loop's handler is what covers that […]

Secondary: the handler's `_record` calls `_log` again under the same failing condition, so
the reporting path can raise too — and *that* escapes the loop and aborts the remaining
markers in the pass.

**Escape routes that exist today**, beyond the two above: `_run_summarizer` (`:1193`) is
wrapped only in `except subprocess.TimeoutExpired`, so a missing `claude_bin` or prompt
file raises straight past the attempts discipline; `_advance_pointer` and `_quarantine`
raise `OSError` from `_atomic_write_json`; `_drop_marker`'s `unlink` is unguarded.

### #25 — it lands during onboarding, on a new machine

`server/admin.py:21` mints with `secrets.token_urlsafe(32)`, whose alphabet is
`[A-Za-z0-9_-]`, so **~1 token in 64 starts with `-`** (measured at 1.54 %, ≈1 in 65, over
200 000 draws during the phase-3 wave h review).
`docs/RUNBOOK.md:603`, onboarding step 3, tells the operator to run:

```bash
~/.loom/venv/bin/loom init --server-url http://<HOST>:8787 --token <TOKEN>
```

argparse reads a value beginning with `-` as the next *option*:

```
loom init: error: argument --token: expected one argument
```

The error names `--token` but not the cause, and the obvious operator reaction — "the
token got mangled in copy-paste" — sends them to re-issue, which has the same 1-in-64
chance again. The token is printed **once** and stored only as a sha256
(`server/db.py:68-70`), so re-issuing means `deactivate` + `adduser`.

It already fired twice: as a flaky CI failure (`test/waves/c/test_goal_e4_e2e.py`, why
`main` was red at `ec2ee05`), repaired test-side in `e67c0e0`, and live on the phase-4 exit
run. This is the product-side half that commit deliberately left.

## Proposed Solution

### Verified while planning: argparse cannot be configured out of this

Not `allow_abbrev=False`, not a `type=`, not `nargs`, not the `--` separator. `--token=<v>`
is the **only** form argparse accepts for a `-`-leading value:

```
space form, dash token     | default            -> ArgumentError: expected one argument
space form, dash token     | allow_abbrev=False -> ArgumentError: expected one argument
equals form, dash token    | default            -> OK token='-Xy9abc'
double-dash separator      | default            -> ArgumentError: expected one argument
```

So issue #25's option 1 — "accept any token shape at the CLI" — necessarily means
normalizing `argv` before `parse_args`. Two negatives keep that change small, both
verified:

- `--` **already** rescues positionals: `loom log -- -weird-project` parses today. The
  `proyecto` and `target` positionals need documentation, not code.
- `--limit -5` is **already** accepted, via argparse's negative-number matcher. The
  `type=int` options need nothing, and including them would silently turn a rejected
  negative limit into an accepted one.

That leaves exactly the **string-valued optionals**: `--token`, `--server-url`, `--grep`,
`--type`.

### Decisions this plan pins

Raised rather than improvised (`AGENTS.md` §7), and accepted before implementation.

#### D22.1 — Narrow at the read, not at the call site

`_read_marker` returns `None` unless the parsed value `isinstance(..., dict)`. This is the
idiom `sweep._registry_remotes` and `processor._load_attention` (`:479-491`) already use,
and it fixes `_iter_markers` and `_load_registry` in one place. The alternative — guarding
each sort key and each `.get` — leaves the next caller to rediscover the hazard.

`_read_marker_with_identity`, `_load_state` and the `marked_at` coercion get the same
treatment, for the same reason.

#### D22.2 — A non-object marker is parked, not retried forever

Narrowing alone routes such a file to `_process_marker:1067`'s "corrupt/vanished marker"
path, which **keeps** it and records `error` on every pass — which is #23's complaint in
another guise. The hook writes atomically (tmp+mv), so a non-object marker in `queue/` is
**durable corruption, not a torn write**: it can never become processable, and retrying it
every 15 minutes forever is not "harmless", it is a permanent error line no operator action
clears.

So it is parked once, into `failed/`, under the **existing** `failed_parked` disposition —
no second new constant — with detail `marker unreadable`.

**Only when it was actually read and failed to parse.** `_read_marker_with_identity`
returns `(None, None)` for `OSError`/vanished and `(None, identity)` for a parse failure.
The first is transient or already gone: record and return, never park. And the park asks
`_marker_is_still_ours` first, like every other disposition that touches the queue path
(P2.F3).

#### D23.1 — The generic handler is bounded, and distinguishable

Issue #23 offers two defensible endings and asks for one to be written down. **Chosen:
bounded.**

A new `kept_pipeline_error` disposition below the bound; at `MAX_ATTEMPTS` the marker parks
via `_bound_attempts`, exactly the shape `_handle_adapter_failure` already has. Reasons:

- **Symmetry.** P4/P5 bound every other failure class. An unbounded one beside them is the
  asymmetry the issue objects to, whichever way it is resolved.
- **Parking does not lose the capture.** It moves to `failed/`, shows in `loom status`, and
  `docs/RUNBOOK.md:1047` already documents clearing a false park — the sweep then
  re-enqueues the session.
- **Unbounded retry only helps a fault that clears itself.** A deterministic defect (an
  `AttributeError` from a code bug) never self-heals, and hides forever behind a generic
  `error` string.
- **The environmental case is handled better elsewhere.** On a full disk the *reporting*
  path fails too, so retrying indefinitely does not help; D23.2 is what actually addresses
  it.

This adds one key to `_CATEGORY` (`:103-119`). `contract/ATTRIBUTION.md` §2 deliberately
boasts of *not* introducing one, which is why this is a raised decision. Nothing pins the
dict's contents — `cli._ERROR_ACTIONS` (`cli.py:919`) derives from it, and neither the
suites nor `contract/verify.py` assert its membership — so no counter or existing assertion
moves.

#### D23.2 — A diagnostic may never fail a capture that otherwise succeeded

`_log` swallows `OSError`. This is the issue's "related, cheap regardless", and it is what
makes the R3/R4 case correct rather than merely bounded: a successful attribution must not
become a failed capture because the disk is full.

The disposition survives a lost log line — `_record` (`:936-938`) appends to
`report.outcomes` **before** calling `_log`, so `loom process` still reports it.

#### D23.3 — The attempt must land on the marker that failed

`_bound_attempts` guards every write with `_marker_is_still_ours(marker_path, identity)`,
because `sessionend.sh` writes the queue **without the lock** and a fresh capture can land
on the path during the summarizer window (minutes, in practice). But
`_marker_is_still_ours(path, None)` **degrades to `True`** (`:1032`), so a handler that
re-read its own identity would satisfy the guard that exists to stop exactly this, and
stamp a fresh session with an old payload's attempt.

`_process_marker` returns `None` on all 13 paths and cannot hand its identity back. So
`run_once` captures `identity = _identify_marker(marker_path)` immediately **before** the
`try` and passes it to the handler. Cost: one extra small read per marker per pass. The
conservative outcome is already implemented inside `_bound_attempts` — it reports
`attempts=N not persisted (marker replaced by the hook)` and parks nothing.

#### D23.4 — The diagnostic is sanitized

The current handler emits raw `f"{type(exc).__name__}: {exc}"`, which can echo paths and
values into `processor.log` and onto the parked marker. A `_pipeline_detail(exc)` modelled
on `_adapter_detail` (`:1324-1343`) keeps it to a single log-safe line. The unit suite
already enforces this class of discipline
(`test_adapter_failure_detail_is_a_single_log_safe_line`,
`test_unicode_decode_error_is_classified_without_echoing_bytes`).

#### D25.1 — Accept any token shape at the CLI, and document the `=` form

Issue #25's options 1 + 3, its own recommendation.

**Option 2 is deliberately rejected.** Minting dash-free tokens would help only tokens
issued after the change, and a raw token is unrecoverable, so every operator already
holding one would still hit the same opaque error. `server/admin.py` is not touched.

The normalization binds the next token to a single-value option, which is sound **because
these options take exactly one value**: whatever follows the flag *is* that value, by the
grammar's own rule. Verified against the real parser:

| argv | result |
|---|---|
| `["--token", "-Xy9abc"]` | parses, `token='-Xy9abc'` |
| `["--token", "plain"]` | unchanged behavior |
| `["--token=-Xy9abc"]` | unchanged behavior |
| `["--token"]` | **still** errors, same message |
| `["--token", "--force"]` | binds `--force` as the token — the one behavior change |

The last row is the whole cost: today it errors. It gets a comment at the call site.

`cmd_init`'s `interactive = args.server_url is None or args.token is None` predicate is
unaffected — normalization never turns a present value into `None`.

`docs/RUNBOOK.md:603` moves to the `=` form regardless, since it is immune to token shape
with or without the CLI change, and operators paste that line verbatim.

## Technical Approach

### Commit 1 — `docs:` write the decision down before implementing it

- This file.
- `PLAN.md` §T4 error matrix — one new row, the artifact #23 asks for, in P4's existing
  vocabulary:

  | `_process_marker` raised (bug or environment) | keep the marker, `attempts+1`, flag `pipeline_error`; ≥3 → `failed/` | `loom status` |

- `docs/RUNBOOK.md` §6 — `kept_pipeline_error` in the stuck-marker list beside
  `kept_summarizer_failed`, and a row in the log-line→action table (`:846-851`).

`PLAN.md` and `docs/` are not in `AGENTS.md` §13's frozen set. `contract/ATTRIBUTION.md`,
`contract/SCHEMA.md` and `contract/FLAGS.md` are **not** touched.

### Commit 2 — `fix:` #22

`client/loom/processor.py`

| Site | Change |
|---|---|
| `_read_marker` `:365` | `return parsed if isinstance(parsed, dict) else None` |
| `_read_marker_with_identity` `:419-421` | same narrowing, so `:1073` cannot raise |
| `_iter_markers` sort key `:440-441` | coerce `marked_at` to `str` |
| `_load_state` `:1270-1278` | narrow to `dict` on return |
| new `_park_marker(cfg, marker_path)` | `failed_dir.mkdir` + `shutil.move`, factored out of `_bound_attempts` |
| `_process_marker` `:1067-1071` | park when `identity is not None` and `_marker_is_still_ours`; record `failed_parked` / `marker unreadable` |

Factoring `_park_marker` out also makes `contract/ATTRIBUTION.md:93-97`'s reference to a
`_park_marker` true again — it is currently a stale name — **without editing the frozen
contract**.

`client/loom/cli.py` — `cmd_status` `:1094`, `:1104`, `:1111`: narrow with
`isinstance(data, dict)`, matching the six sites in the same file that already do.

### Commit 3 — `fix:` #23

`client/loom/processor.py`

| Site | Change |
|---|---|
| `_log` `:449-455` | wrap in `try/except OSError` (D23.2) |
| new `_pipeline_detail(exc)` | sanitizer modelled on `_adapter_detail` (D23.4) |
| new `_handle_pipeline_failure(...)` | third sibling of the two existing handlers; sets `marker["pipeline_error"]`, delegates to `_bound_attempts(..., "kept_pipeline_error", detail, identity)`; parks as unprocessable if the re-read yields no dict; body wrapped so the handler can never abort the pass |
| `run_once` `:1445-1455` | `identity = _identify_marker(marker_path)` before the `try`, passed to the handler (D23.3) |
| `_CATEGORY` `:103-119` | `+ "kept_pipeline_error": _ERRORS` |

`client/loom/cli.py` — `_marker_tags` `:924-932`: render `pipeline_error`, as it already
does for `adapter_error` and `registry_error`.

### Commit 4 — `fix:` #25

`client/loom/cli.py` — new `_bind_option_values(argv)`, applied in `main()` (`:2065-2075`)
before `parse_args`, over `{--token, --server-url, --grep, --type}`.

`docs/RUNBOOK.md:603`:

```bash
~/.loom/venv/bin/loom init --server-url=http://<HOST>:8787 --token=<TOKEN>
```

`test/waves/c/test_goal_e4_e2e.py:259` and `client/tests/e2e/test_phase2_e2e.py:671` keep
their `=` form: it is correct, it hardens against future shapes, and the wave is frozen
(`AGENTS.md` §12, §14).

### What deliberately does NOT change

- `server/admin.py` token minting (D25.1).
- The `push-quarantine <target>` and `log`/`curate <project>` positionals — `--` already
  handles them natively.
- Any frozen contract, the marker schema, attribution, the API payload, the summarizer,
  the secret scan, `content_hash` idempotency, the pointer discipline (I2), or any existing
  disposition constant.
- `GOAL_WAVE_DIRS` and the `>= 288` goal-wave collection floor.

## Acceptance Criteria

Each maps to at least one executable assertion, written before the implementation it
covers; none may be left unasserted (`AGENTS.md` §9) and none weakened to get green (§12).

- [ ] **AC1 — #22, the fix.** A `queue/` file containing `[1, 2]` does not prevent a
      healthy marker beside it from being processed **in the same pass**. Asserted on the
      healthy session's disposition, following
      `test_unusable_fallback_does_not_starve_the_next_marker`, which already depends on
      `marked_at` ordering. Red today: it raises in `sorted()` before any outcome exists.
- [ ] **AC2 — #22, the registry door.** A `projects_cache.json` whose top-level JSON is not
      an object degrades to "registry absent" and the pass completes. Red today.
- [ ] **AC3 — #22, non-string `marked_at`.** `{"marked_at": 5}` does not abort the pass.
      Red today (`TypeError` in `sorted()`).
- [ ] **AC4 — D22.2.** A non-object marker is parked into `failed/` once, recorded
      `failed_parked` with detail `marker unreadable`, and the next pass produces no
      outcome for it. A marker that vanished (`OSError`) is **not** parked.
- [ ] **AC5 — `loom status` survives.** `cmd_status` renders a non-object file in `queue/`
      and in `failed/` without raising. Red today.
- [ ] **AC6 — #23, bounded.** A marker whose processing raises is recorded
      `kept_pipeline_error` with `attempts=1`, then `2`, then `failed_parked` at
      `MAX_ATTEMPTS`, with the marker present in `failed/` and absent from `queue/`.
      Asserted on the persisted marker, following
      `test_summarizer_fails_three_times_parks_in_failed`.
- [ ] **AC7 — D23.2.** With an unwritable `log_dir`, a healthy marker still posts (the
      R3/R4 diagnostic path) **and** the remaining markers in the pass are still processed.
      Red today: the capture fails and the pass aborts.
- [ ] **AC8 — D23.4.** The persisted `pipeline_error` is a single line and does not echo
      the exception's payload.
- [ ] **AC9 — D23.3.** A marker replaced by the hook mid-pass does not inherit the failing
      pass's attempt; the outcome states the count was not persisted.
- [ ] **AC10 — #25, the fix.** `["init", "--server-url", url, "--token", "-Xy9abc"]` — the
      **space** form with a dash-leading token — reaches `cmd_init` and writes the config.
      Red today with `SystemExit: 2`.
- [ ] **AC11 — #25, no collateral.** The `=` form keeps working; a missing value still
      errors; `--grep` accepts a dash-leading term.
- [ ] **AC12 — the RUNBOOK line is the `=` form**, asserted by reading `docs/RUNBOOK.md` in
      the docs test if one exists, otherwise verified by inspection in review.

## Alternatives Considered

- **#22: guard each call site instead of the read.** Rejected — it leaves the next caller
  of `_read_marker` to rediscover the hazard, and there are already three.
- **#22: keep the non-object marker in `queue/` (today's "kept and reported").** Rejected
  as D22.2: the hook writes atomically, so it can never become processable, and an error
  line every 15 minutes forever is not a disposition.
- **#23: unbounded with a distinguishable disposition** (the issue's option 2). Defensible,
  and rejected as D23.1 — a deterministic defect never self-heals, and the environmental
  case is better served by D23.2.
- **#23: bounded but reusing the generic `error` string.** Rejected — it adds no
  `_CATEGORY` key, but `loom status` still cannot tell a pipeline fault from any other
  error until it parks, which was half the complaint.
- **#25: mint dash-free tokens** (option 2). Rejected as D25.1 — leaves every issued token
  broken and they cannot be recovered.
- **#25: docs only** (option 3 alone). Rejected — operators who paste the space form from
  memory or an older runbook still hit it, and it does nothing for a token already in hand.
- **A frozen goal wave for all three.** Rejected — these repair behavior the existing
  suites already assert; a wave would move the `>= 288` floor for no added oracle strength.

## Risk Analysis & Mitigation

| Risk | Mitigation |
|---|---|
| Parking a non-object marker routes it into `loom status`, which has the same bug | Commit 2 fixes `cmd_status`'s three sites **in the same commit** as the park (AC5) |
| The new disposition silently counts as nothing if `_CATEGORY` is missed | It is added in the same commit; `_count` uses `_CATEGORY.get(...)`, and AC6 asserts the counter |
| Bounding the generic handler parks a marker that a later pass would have processed | Parking is recoverable and documented (`RUNBOOK:1047`); `attempts` is visible in `loom status` before the park |
| The attempt lands on a fresh capture written by the hook mid-pass | D23.3 — identity captured before the `try`; `_bound_attempts`' existing guard then refuses the write |
| `_log` swallowing `OSError` hides a real disk problem | The disposition still reaches `report.outcomes` and `loom process`'s summary; `loom doctor` is the surface for disk health |
| argv normalization changes an existing invocation's meaning | Only `--token --force`-shaped input, which errors today; commented at the call site and covered by AC11 |
| The wave-c `=` form looks redundant after the CLI fix and gets "cleaned up" | Explicitly out of scope above; the wave is frozen (`AGENTS.md` §12, §14) |

## Known Limits (explicit)

- The normalization covers **string-valued optionals only**. A dash-leading value for a
  positional still needs `--`, which argparse already supports; this is documented, not
  coded.
- `_log` swallowing `OSError` means a full disk loses diagnostics silently. The dispositions
  survive, but `processor.log` will have gaps.
- Parking a non-object marker preserves the bytes for forensics but does not repair them; a
  human still clears `failed/`.
- A marker that raises **inside** `_bound_attempts` itself (e.g. the `shutil.move` fails on
  a read-only volume) is caught by the handler's own wrapper and reported, but nothing can
  be persisted in that state.

## References

**Repo (baseline `23bd1ff`)**
- `client/loom/processor.py:365-369` — `_read_marker`, the unnarrowed read (#22)
- `client/loom/processor.py:398-421` — `_read_marker_with_identity`, the same gap
- `client/loom/processor.py:435-443` — `_iter_markers`, the sort key that raises
- `client/loom/processor.py:449-455` — `_log`, unguarded `mkdir` + `open` (#23)
- `client/loom/processor.py:649-705` — `_canonical_from_marker`, the R3/R4 `_log` calls
- `client/loom/processor.py:725-746` — `_load_registry`, the second unnarrowed door
- `client/loom/processor.py:936-938` — `_record`, appends before logging
- `client/loom/processor.py:960-1032` — `_drop_marker` / `_resolve_marker` / `_marker_is_still_ours`
- `client/loom/processor.py:1056-1073` — `_process_marker`, the unreadable-marker path
- `client/loom/processor.py:1371-1407` — `_bound_attempts`, the discipline to join
- `client/loom/processor.py:1413-1463` — `run_once`, the loop expression and the handler
- `client/loom/processor.py:103-119` — `_CATEGORY`, gains one key
- `client/loom/cli.py:918-932` — `_ERROR_ACTIONS`, `_marker_tags`
- `client/loom/cli.py:1094,1104,1111` — the three unnarrowed `_read_json` reads
- `client/loom/cli.py:2052-2075` — the `init` parser and `main()`
- `client/loom/sweep.py:481-531` — `_registry_remotes`, the narrowing idiom to match
- `server/admin.py:18-39` — token minting, deliberately untouched
- `docs/RUNBOOK.md:603` — the onboarding line; `:807`, `:846-851`, `:1047` — the operator contract for `failed/`
- `PLAN.md` §T4 — the normative error matrix, which gains a row; P4, P5
- `DESIGN.md` §4.2 (the pipeline), §7 (`loom status`), I2/I4/I6
- `AGENTS.md` §2, §5, §7, §9, §12, §13, §14
- `e67c0e0` — the test-side half of #25, and its rationale

**Issues**
- #22
- #23
- #25
