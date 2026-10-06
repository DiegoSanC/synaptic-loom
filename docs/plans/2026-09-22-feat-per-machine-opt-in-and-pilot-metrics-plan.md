---
title: "feat: Per-machine capture opt-in, task-start check, recall telemetry and feedback"
type: feat
date: 2026-09-22
complexity: high
github_issue: 15  # rewrites #15 around this protocol; thin forms of #13 and #14 ride along
baseline_in_issue: bb52ec1
branch_from: main (bb52ec1)
branches: feat/capture-opt-in · feat/loom-check · feat/recall-events · feat/recall-feedback · ops/deploy-v5
---

# feat: Per-machine capture opt-in, task-start check, recall telemetry and feedback

## Overview

Written on 2026-09-22 after the owner's review of the pilot's readiness.
Four owner decisions of the same day frame everything below:

| Decision | Value | Why it matters here |
|---|---|---|
| Who consents to capture | **the person, per machine and per repository** — never the teammate who registered the project | Entries are append-only (D7): a capture that should not have happened is permanent, a capture that was missed costs one command. The gate must sit on the side whose mistake is cheap. |
| How the agent learns Loom exists | **one instruction in the user's `~/.claude/CLAUDE.md`, installed with consent**, that runs a local `loom check` at the start of a task and acts on its output | Project-level `CLAUDE.md` would tell every clone — pilot or not — that the repo is in Loom; `CLAUDE.local.md` would need one file per repo per machine. A user-level instruction whose first step is a *local, silent* check leaks nothing and asks nothing in repos that are not registered. |
| What the pilot measures | **recall events and per-entry feedback, aggregated by team, never by person** | The pilot scope needs at least one verified cross-developer reuse and at least one stale/not-applicable result; today there is no counter and nowhere to write either. |
| Claude Code version | **not pinned on any machine**; re-pin `contract/VERSION` to the latest verified version instead | The CLI version is a per-machine availability risk, not a comparability risk: extraction comparability comes from the summarizer prompt and model, which are already fixed. |

And the four owner answers to the plan's open questions (taken through the plan review
of the same date): **(A)** `recall_events.query` is stored in clear; **(B)** `loom register`
also enables the project locally for the person who registers it; **(C)** a human can
read `loom log` in any registered repo — only the *agent's instruction* is limited to
enabled repos; **(D)** `loom stats` is available to every member, team totals only.

## What exists already (verified on `main` at `bb52ec1`)

- **The capture gate is per project, on the server.** `processor._process_marker` step 3
  compares the canonical remote with the hourly cache of `GET /api/projects`
  (`~/.loom/projects_cache.json`, `cfg.cache_ttl_s`) and drops with
  `dropped_not_registered` (`client/loom/processor.py:1123-1131`, DESIGN §4.2). Comparing
  against the cache never sends the remote to the server.
- **There is no local per-project state.** `Config` (`client/loom/config.py:250-280`)
  carries server, token, TTL, summarizer, sweep, scanner and curation settings — nothing
  keyed by repository.
- **`loom log` resolves the cwd's project by asking the server for the remote**
  (`cli._resolve_project_by_cwd` → `GET /api/projects?remote=`). In a private repo that
  request carries the private remote. The processor already has the cache-first pattern
  that avoids this.
- **`install.sh` has five steps and never touches a `CLAUDE.md`.** It already knows how to
  merge without overwriting (settings.json via `jq`, the crontab block between markers).
- **The `/loom` skill's trigger** is "when reviewing PRs or inheriting a project"
  (`client/skills/loom/SKILL.md`); nothing tells the agent to consult before planning,
  diagnosis or implementation — issue #13's gap, unchanged.
- **The server has four base tables** (`users`, `projects`, `project_paths`, `entries`)
  plus the two additive ones of #20. Nothing records reads or feedback.
- **The disposition vocabulary is pinned by tests** — `client/tests/integration/test_processor.py`
  (the phase-1 matrix) and `test/waves/n` (attribution) assert exact outcome lists.
- **Six frozen waves (a, d, e, g, i, n) serve a fake `/api/projects` registry and then
  drive the processor expecting `posted`**; they write `config.json` by hand (a, d, e, g,
  i) or through `loom init` (c, n). None of them runs `loom enroll` or `loom register`.
  This is the constraint that shapes the capture-mode design below.

## Design

### Capture opt-in (D9)

**Two gates instead of one.** A session leaves the machine only if its remote is
registered on the server *and* enabled on this machine. The second gate is evaluated after
the first and only locally: an unregistered project still produces nothing, not even the
new drop.

**Local state** — `~/.loom/projects_local.json`:

```json
{"github.com/org/repo": {"state": "enabled", "since": "2026-09-22T15:40:00Z"},
 "github.com/org/other": {"state": "disabled", "since": "2026-09-22T15:41:00Z"}}
```

Keyed by canonical remote (D1: stable across clones and paths), written atomically like
every other state file, kept *outside* any repository so it can never be committed and
survives a re-clone. `Config` gains `projects_local_path`; `config.json` is untouched.

**Capture mode** — one new optional `config.json` key, `capture_mode`, with two values:

| Value | Meaning |
|---|---|
| `"opt_in"` | D9: the local gate is enforced; a registered project is captured only when `projects_local.json` says `enabled`. |
| `"all_registered"` | The pre-D9 behaviour: every registered project is captured on this machine. **This is what an absent key means**, so every existing `config.json` keeps loading and behaving unchanged (the #20 precedent). |

Who writes `opt_in`: `loom enroll` (the documented onboarding) on a fresh config, and the
first `loom enable`/`loom disable` run on a machine (an explicit per-repo choice, whose
output says the machine is now in opt-in mode and that other registered projects need
`loom enable` too). `loom init` — the break-glass form — and a hand-written config stay in
`all_registered`. `loom doctor` shows the mode on every machine: `ok` with the count of
enabled projects in opt-in mode, `warn` in legacy mode with the one-line remedy.

**Processor** — step 3b in `_process_marker`, right after the registry gate: in `opt_in`
mode, a canonical remote whose local state is not `enabled` → `_resolve_marker` +
disposition `dropped_not_enabled` (class `_SKIPPED`). The pointer is untouched. The
`RunReport` counts it as skipped, like the other legitimate drops.

**CLI** —

- `loom enable` / `loom disable`, run inside a repo: resolve the remote locally, force a
  registry refresh (the person is at the keyboard and waiting), require the project to be
  registered for `enable` (otherwise: "not registered; run `loom register`"), write the
  state, switch the machine to `opt_in` if it was not, print one line. Idempotent.
  `disable` also works for an unregistered project: the "no" is recorded either way.
- `loom register` (decision B): after a successful registration, or on "already
  registered", it also enables the project locally **when the machine is in `opt_in`
  mode**. In legacy mode the project is captured anyway, so there is nothing to enable and
  the output says nothing new.
- `loom status`: a new block, printed only when the run log carries any, "N session(s) in
  <project> skipped: not enabled on this machine (run `loom enable` there)", counted from
  `processor.log` exactly like the label-fallback row (tab-separated, action in the 3rd
  field, never a byte of content).
- `loom doctor`: the capture-mode row described above.

### Task-start check (D10)

**`loom check`**, run inside a repo, prints exactly one of five stable tokens on stdout,
exit 0 (usage errors excepted), with `--json` for scripts:

| Output | Condition | What the agent does |
|---|---|---|
| `not_registered` | remote absent, not normalizable, or not in the registry | says nothing |
| `registered_undecided` | registered; `opt_in` mode; no local state | asks **once**: enabling here does two things — this repo's sessions are summarized and uploaded, and the teammates' findings become consultable |
| `enabled` | registered and enabled here (or registered in legacy mode: it *is* captured) | consults with a fixed budget; offers feedback at the end |
| `disabled` | registered; local state `disabled` | says nothing; the "no" is remembered |
| `unknown` | no token, or cache absent *and* server unreachable | continues the task and never claims there is no prior knowledge (#13) |

The check is **local**: it reads the cache; when the cache is older than `cache_ttl_s` it
tries one `GET /api/projects` with a two-second budget and, on any failure, uses the stale
cache. It never issues the by-remote lookup. Only the `enabled` outcome ever leads to a
request carrying search terms, and only from a repo the person enabled.

**`loom log` resolves by the cache first** (the processor's own pattern) and issues the
by-remote lookup only when the cache says the project is registered — so a private repo's
remote no longer travels.

**`install.sh` step 6** — a block in `~/.claude/CLAUDE.md` between `<!-- loom:start -->`
and `<!-- loom:end -->`: asks for confirmation (honours `--yes`), creates the file when
absent, never touches a byte outside the markers, and `--update` replaces only the inside.
Same discipline as the crontab block. The block's text (final wording in PR B):

```markdown
<!-- loom:start -->
## Synaptic Loom (team shared memory)
At the start of a planning, diagnosis or implementation task, run
`~/.loom/venv/bin/loom check` once and act on its output:
- `not_registered` or `disabled`: do not mention Loom.
- `registered_undecided`: tell the user this project is registered in Loom and that
  enabling it here (`loom enable`) does two things: it summarizes and uploads their
  sessions in this repo, and it lets them consult what teammates found. Ask once; do
  not insist.
- `enabled`: use the /loom skill with `--grep` and the task's terms before proposing
  an approach; treat the result as unverified reference. If something retrieved changed
  your approach, or turned out stale, use /loom-feedback when you finish.
- `unknown`: carry on; never state that there is no prior knowledge.
<!-- loom:end -->
```

**Skills** — `/loom`'s description is rewritten around "before planning, diagnosis or
implementation in an enabled project" and its command passes `--source skill`;
`/loom-register` mentions that registering enables locally and that teammates will see
the question; a new `/loom-feedback` prepares `loom feedback` for the entries the agent
used and asks for the one-line note.

### Recall telemetry and feedback

> **Amended 2026-09-23 (D13):** feedback is born item-grained — `recall_feedback`
> references an item (`<entry>.<n>`), not an entry, and `loom feedback` takes `--item`.
> Build branch D from slice F of
> `docs/plans/2026-09-23-fix-label-grammar-fidelity-plan.md`, and never before its slice C.

**Schema** — additive, the #20 mechanism (`ensure_*_schema` at startup; `schema.sql`
untouched; a rolled-back image leaves two tables nobody reads):

```sql
CREATE TABLE IF NOT EXISTS recall_events (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  project_id INTEGER NOT NULL REFERENCES projects(id),
  source TEXT NOT NULL CHECK (source IN ('skill','cli')),
  query TEXT,                              -- decision A: stored in clear
  result_count INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recall_feedback (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  project_id INTEGER NOT NULL REFERENCES projects(id),
  entry_id INTEGER NOT NULL REFERENCES entries(id),
  verdict TEXT NOT NULL CHECK (verdict IN ('useful','not_applicable','stale')),
  note TEXT NOT NULL,
  feedback_hash TEXT UNIQUE NOT NULL,      -- sha256(user_id, entry_id, verdict, note)
  created_at TEXT NOT NULL
);
```

**Routes** (DESIGN §6.2, under a dated decision):

- `GET /api/projects/{id}/entries` — unchanged contract. When the request carries
  `X-Loom-Source: skill|cli`, one `recall_events` row is inserted after the query is
  answered. No header (an older client) → no row. A 401 never creates a row.
- `POST /api/feedback` (new, authenticated): `{project_id, entry_id, verdict, note}` →
  `201`, `409 duplicate` when the hash exists, `422` when the entry does not belong to the
  project or the verdict is outside the vocabulary. Rate limit and audit are inherited from
  the middleware.
- `GET /api/stats?since=<ISO>` (new, authenticated): recalls by source, feedback by
  verdict, top 10 entries by `useful` with their project. **No per-user field** — a
  non-goal of #14 and #15.

**Client** — `loom log --source skill|cli` sets the header (the skill passes `skill`);
`loom feedback --entry <id> --verdict useful|not_applicable|stale --note "<one line>"`
inside the repo; `loom stats [--since 7d]`. Against an older server `loom feedback`
receives `404` and says the server does not support feedback yet; everything else works.

### Docs and contracts

- `DESIGN.md` §1: **D9** (two gates; append-only makes the two errors asymmetric) and
  **D10** (task-start local check in the user's `CLAUDE.md`; not ambient injection because
  it transports no Loom content). §3 registration; §4.2 step 3b; §6.1 tables; §6.2 routes
  with the dated decision paragraph; §7 `loom status`.
- `PLAN.md` §T5: the `capture_mode` key and `projects_local.json`; §3: the two routes.
- `docs/RUNBOOK.md` §0: each machine's mode; §5: `loom enable` after step 5, **without
  touching the strings `client/tests/integration/test_cli_init.py` pins**; §8: how to stop.
- The teammate-facing page can now say "installing captures
  nothing until you enable a repo".
- Issue #15: rewritten with this as its protocol, replacing the 13 dependencies; the
  window stays open with a first *review* date, not a close date.
- `contract/`: untouched. Nothing here touches the transcript or the `claude` flags.

## Decisions raised, not improvised

1. **`all_registered` is what an absent `capture_mode` means.** The alternative — every
   machine is opt-in, full stop — breaks six frozen waves (a, d, e, g, i, n) that register
   through a fake registry and expect captures, and would require editing their fixtures
   from the implementer's side, which the wave doctrine forbids. The legacy value keeps
   them untouched and green, keeps every existing config loading unchanged, and the
   machines that matter — anything onboarded through `install.sh` + `loom enroll` — land
   in `opt_in`. `doctor` warns on every legacy machine, so the two current dev machines
   are migrated by running `loom enable` once each, not by silent behaviour change.
2. **`loom init` does not write `opt_in`.** It is the break-glass form and two frozen waves
   (c, n) use it before a fake registration; the documented path is `enroll`.
3. **`loom check` refreshes the cache only when stale, never in the background.** A
   detached refresher would inherit the macOS consent problem seen on 2026-09-22 and the
   15-minute pass already refreshes hourly. The cost of the rule is that a teammate may see
   the "enable?" question up to an hour late, once.
4. **No new frozen goal wave** for the same reason as #20: one author, so the criteria are
   asserted in the suites that own each boundary and the gate drives them.
5. **The check instruction is close to the line the pilot scope draws** ("ambient
   SessionStart injection … outside this pilot") without crossing it: it injects no Loom
   content, runs once per task, and can only ask a question. It is written down as D10
   precisely because it is close.

## Acceptance criteria → assertions

| Criterion | Where asserted |
|---|---|
| A registered project on a machine in `opt_in` mode is skipped with `dropped_not_enabled`, the marker is resolved, the pointer untouched | `client/tests/integration/test_processor.py` (new matrix row) |
| The same project, enabled locally → `posted`; a machine with no `capture_mode` → `posted` (legacy unchanged) | `test_processor.py` |
| `loom enable` refuses an unregistered project; `loom disable` records the "no"; both idempotent; both switch the machine to `opt_in` and say so | `client/tests/integration/test_cli_enable.py` (new) |
| `loom register` enables locally in `opt_in` mode and not in legacy mode | `test_cli_register.py` |
| `loom enroll` writes `capture_mode: opt_in` on a fresh config and preserves an existing value | `test_cli_enroll.py` |
| `loom status` names the skipped sessions and the project; `loom doctor` shows the mode | `test_cli_status.py` · `test_cli_doctor.py` |
| Real capture with one enabled and one only-registered repo, both unattended entry points | `client/tests/e2e/test_enrolment_e2e.py` (extended: enrol → `dropped_not_enabled` → `loom enable` → posted) |
| `loom check` prints exactly one of five tokens in each state; a stale cache with a dead server answers from the stale cache in under two seconds; in an unregistered repo no request carries the remote | `client/tests/integration/test_cli_check.py` (new; asserts on the mock server's request log) |
| `install.sh` run twice leaves one block and no byte changed outside it; `--update` replaces only the inside | `test_install_update.py` |
| A read with the source header creates one `recall_events` row with the right count; no header → no row; a 401 → no row; no token in any row | `server/tests/test_recall.py` (new) |
| The same feedback twice → `201` then `409`, one row; another project's entry → `422`; the three verdicts accepted, a fourth refused; `stats` carries no user field | `server/tests/test_feedback.py` (new) |
| `DESIGN.md` D9/D10 and §6.2 under dated decisions; `RUNBOOK.md` §5 still satisfies its pins | the docs PRs; `test_cli_init.py` |

## Slices (TDD, each red → green, one PR each)

- **A — `feat/capture-opt-in`:** `capture_mode` + `projects_local.json`; processor step 3b;
  `enable`/`disable`; `register` enabling; `enroll` writing the mode; `status` and
  `doctor` rows; enrolment e2e extended; D9, §4.2, §T5, RUNBOOK §0/§5. Prerequisite for
  onboarding engineer #2.
- **B — `feat/loom-check`:** `loom check`; cache-first `loom log`; `install.sh` step 6;
  the three skills; D10. Prerequisite for onboarding engineer #2.
- **C — `feat/recall-events`:** `recall_events`; the source header; `loom log --source`;
  the skill passing it. Before the measurement window opens.
- **D — `feat/recall-feedback`:** `recall_feedback`; `POST /api/feedback`;
  `GET /api/stats`; `loom feedback`; `loom stats`; `/loom-feedback`. Before the window.
- **E — `ops/deploy-v5`:** snapshot; `fly deploy` (a production action — asked for
  explicitly); `install.sh --update` and `loom enable` on both dev machines; one real
  capture from each as evidence; RUNBOOK; the teammate page; the #15 rewrite.

Each PR: full `make test`, `LOOM_SKIP_LIVE=1 make contract`, gitleaks, self-review against
this plan.
