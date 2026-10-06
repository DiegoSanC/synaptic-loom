# Synaptic Loom — Imported improvements specification (draft for review)

Improvements adapted from the comparative analysis (2026-08-12) of two external memory
projects: [TencentCloud/TencentDB-Agent-Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory)
and [mem0-local](https://github.com/oriolgual/mem0-local) (+ the adversarial audit run on
the latter). Every requirement is **marked with its origin project**; nothing here is
original design — the adaptation to Loom's invariants is.

**Status:** DRAFT. Block T (Tencent-inspired) is ready to route to a build run.
Block M (mem0-inspired) is **deliberately deferred**: the owner wants to think it over
before committing — do NOT route Block M until it is explicitly unblocked.

**Execution model:** each block is one planned build run (route → plan → review →
build), same as phases 2–3. Later runs must respect the seams frozen by prior runs.

---

## 1. Frozen constraints (read-only for every requirement here)

Inherited from DESIGN.md and PHASE-2-3-SPEC.md §2; any requirement that cannot be met
within these is raised, not improvised:

- **I1–I6 stand.** In particular I3 (any new LLM pass is a tool-less, network-less
  `claude -p --safe-mode --tools ""` fed via stdin, output secret-scanned fail-closed)
  and I5 (any new consumption surface carries the untrusted-data framing).
- **`entries.content_md` is immutable** (D7). Curation never rewrites history; current
  state is expressed through *new* rows (links, digests), never mutation of old ones.
- **No embeddings / no semantic search** (DESIGN §11 non-goal, unchanged). RT.1 is
  lexical BM25 — that is the point, not a compromise.
- **Stack stays P6:** Python 3.12, FastAPI, `sqlite3` stdlib (FTS5 is compiled into the
  stdlib build — verify with `sqlite3.connect(':memory:').execute("pragma compile_options")`
  during T0-style preflight; if absent, RT.1 blocks loudly).
- **The server never runs an LLM.** Anything needing `claude` runs client-side on a dev
  machine (the curation pass is a `loom` subcommand, not a server job).
- The pilot-gate rule (PLAN §5) still governs: Block T should be routed **after** the
  2026-08-14 gate verdict, since RT.3/RT.4 implement spec-R3.4 whose scope the gate
  conditions (PHASE-2-3-SPEC §6).

### Relationship to PHASE-2-3-SPEC.md

- **RT.3 + RT.4 ARE the implementation of R3.4** (curation digest) and close open
  question **Q-3.5** (supersession mechanism) with the link-table option. When Block T
  is routed, R3.4 must be struck from any future phase-3 feature run — one owner, no
  double scope.
- RT.1 feeds R3.2 (UI search box becomes a thin view over the same endpoint) and RT.4
  gives R3.5 its bundle `index.md` for free.
- Block M is net-new scope, in tension with D6 ("advisory") — hence the deferral.

---

## 2. Block T — Tencent-inspired improvements (route first)

### RT.1 — Lexical search over the logbook (BM25 / FTS5)
**[Origin: TencentDB-Agent-Memory — its default retrieval posture is BM25 via SQLite
FTS5 with embeddings disabled; hybrid/vector is opt-in. Adopted verbatim minus the
vector half.]**

Today the logbook is only consumable as "latest N entries". A reviewer asking "what did
we decide about auth?" has no way to cut through 200 entries.

- **RT.1.1** FTS5 external-content table over `entries.content_md`
  (`content='entries'`), populated by insert trigger (entries are append-only, so no
  update/delete sync needed in v1). One-shot backfill migration for existing rows.
- **RT.1.2** `GET /api/projects/{id}/entries?q=<terms>` returns BM25-ranked entries
  with an FTS5 `snippet()` per hit; same auth, same response envelope plus
  `snippet`/`rank` fields. Malformed FTS5 query syntax → 422, never 500.
- **RT.1.3** `loom log <project> --grep "<terms>"` and the `/loom` skill accept an
  optional query; output keeps the I5 framing.
- **AC:**
  - [ ] Seeded corpus: query ranks the on-topic entry above off-topic ones; empty
    result set is a clean 200, not an error.
  - [ ] FTS5-reserved characters in the query (`"`, `*`, `NEAR`) cannot crash the
    endpoint (unit test with hostile queries).
  - [ ] Backfill migration is idempotent (rerun → same row count) and covered by a test
    against a copy of a real DB fixture.
  - [ ] WAL concurrency test from PLAN-T2 extended to cover concurrent insert + search.

### RT.2 — Typed logbook items with priority
**[Origin: TencentDB-Agent-Memory — L1 atom taxonomy for its `code` mode
(`work_fact` / `work_task` / `work_method` / `work_artifact`) plus a 0–100 priority
field on every atom. Adapted to per-item labels inside Loom's entry markdown.]**

Loom entries only know "decision". Tencent's taxonomy shows reviewers also need the
project's *methods* (SOPs, taboos) and *tasks* (owner/deadline) distinguished from
one-off facts.

- **RT.2.1** Summarizer prompt **v2**: each `## Decisions` bullet gains a leading
  constrained label — `[decision]`, `[method]`, `[task]`, `[fact]` — chosen by the
  summarizer from that closed vocabulary (prompt rule, with examples). `prompt_version`
  bumps to `v2`; `content_hash` already covers it, so no dedup collision with v1 entries.
- **RT.2.2** The **processor** (not the LLM — D7 discipline) parses the labels with a
  strict regex; unknown/missing label → `[decision]` fallback, counted and visible in
  `loom status`. The OKF frontmatter gains `item_types: [...]` (aggregated, deduped).
- **RT.2.3** `loom log --type method` filters server-side (needs the aggregate stored
  queryably: either a JSON1-queried frontmatter or a small `entry_item_types` table —
  implementer's choice, decided at plan time, not improvised mid-build).
- **AC:**
  - [ ] Fixture transcript containing an explicit SOP ("always run make contract after
    CLI updates") yields at least one `[method]` item under prompt v2.
  - [ ] A v2 summary with a garbage label round-trips to `[decision]` fallback without
    failing the entry, and `loom status` reports the fallback count.
  - [ ] v1 entries keep rendering untouched (no retroactive re-typing).
- **Deliberately dropped from the origin:** Tencent's numeric `priority` 0–100. Loom's
  summaries are per-session and short; a priority score without a consumer is dead
  weight. Revisit only if RT.4 digests need tie-breaking.

### RT.3 — Batch supersession/duplicate detection (implements spec-R3.4, closes Q-3.5)
**[Origin: TencentDB-Agent-Memory — its `l1-dedup` pipeline: new memories are judged in
batch against a pool of similar existing candidates and receive one of
store/skip/update/merge, with N→1 merge targets. Adapted: append-only Loom cannot
update/merge in place, so the verdict vocabulary maps to link rows.]**

Loom's only dedup today is the exact `content_hash`. The real problem — the same
decision reversed or restated across sessions — is untouched (mem0-local's audit
measured ~35% near-duplicates in a comparable corpus left uncurated).

- **RT.3.1** Schema: `entry_links(id, from_entry REFERENCES entries(id), to_entry
  REFERENCES entries(id), kind TEXT CHECK(kind IN ('supersedes','duplicates')),
  rationale TEXT, created_at)` + `POST /api/links` (auth'd, 409 on exact duplicate
  link) + links included in entry GET responses. **`status` is derived** (an entry with
  an incoming `supersedes`/`duplicates` link is superseded/duplicate), never a mutable
  column — this is the Q-3.5 resolution.
- **RT.3.2** `loom curate <project>`: fetches entries newer than the last curation
  watermark (stored in `~/.loom/state.json`, same I2 advance-only discipline as the
  transcript pointer), builds the candidate pool for each via RT.1's FTS5 (top-K
  lexical neighbours among *current* entries), and runs **one batched tool-less
  summarizer-profile LLM pass** (I3) whose output is a strict verdict list:
  `keep | supersedes:<id>[,<id>…] | duplicates:<id>`. Unparseable output → fail closed,
  watermark stays, visible in `loom status` (P4-style attempts).
- **RT.3.3** The deterministic script (never the LLM) posts the links. Re-running
  `curate` is idempotent (409-tolerant, watermark-guarded).
- **RT.3.4** Consumers distinguish state: `loom log` renders superseded entries
  collapsed/annotated ("superseded by #42 on <date>"), and skips duplicates by default
  (`--all` shows everything).
- **AC (includes spec-R3.4's AC verbatim):**
  - [ ] After a session reverses an earlier decision, `curate` links old→new and
    `loom log` surfaces the distinction without reading full history.
  - [ ] The curation LLM pass receives entry text only via stdin, has no tools, and its
    verdicts are schema-validated before any POST (hostile fixture: verdict list
    containing an id outside the candidate pool → rejected, nothing posted).
  - [ ] Curation never mutates `entries` rows (assert row-identical before/after on the
    fixture DB except for new `entry_links` rows).
  - [ ] Two concurrent `curate` runs cannot double-link (flock, same as the processor).

### RT.4 — Per-project consolidated digest ("current state of decisions")
**[Origin: TencentDB-Agent-Memory — the L1→L2→L3 consolidation ladder: atoms are
periodically compacted into human-readable scene blocks and a stable per-scope profile
document that is cheap to inject. Adapted as a single digest entry per project,
regenerated on a threshold.]**

- **RT.4.1** `loom curate` (after RT.3 linking): if ≥N new *current* entries since the
  last digest (N configurable, default 15), generate a digest via the same I3-safe LLM
  profile: input = current (non-superseded) entries only; output = a "state of the
  project's decisions" markdown. Stored as a normal entry with frontmatter
  `type: digest` (secret-scanned like everything else, D7 frontmatter built by the
  script).
- **RT.4.2** `loom log` and `/loom` print the latest digest first, then the most recent
  non-digest entries, under an explicit **character budget** (default ~8k chars)
  **[Origin: TencentDB-Agent-Memory — retrieval is budgeted (max results, score
  threshold, char caps) so "memory never eats the context"]**. `--no-digest` and
  `--limit` keep the old behaviour reachable.
- **RT.4.3** The digest doubles as `index.md` when spec-R3.5 (OKF export) is built.
- **AC:**
  - [ ] Below threshold → no digest regenerated (watermark test).
  - [ ] Digest content references only current entries (fixture with a superseded pair:
    the superseded text does not appear as current in the digest).
  - [ ] `/loom` output on a 200-entry project fits the budget and still carries the I5
    framing.

---

## 3. Block M — mem0-local-inspired improvements (route LAST — deferred by owner decision)

> **HOLD.** The owner explicitly wants to deliberate on this block before building.
> It is specified here so the deliberation has something concrete to accept, trim or
> reject — not as a commitment. Do not include it in the Block T build run.

### RM.1 — Ambient recall at SessionStart (opt-in)
**[Origin: mem0-local — its core thesis: "the agent doesn't know what it doesn't know",
so recall must be ambient (lifecycle hooks), not a tool the model must remember to
call. Its `SessionStart` hook injects project memories when a session starts. Adopted
with every defect its adversarial audit verified, fixed by construction — and its
per-prompt `UserPromptSubmit` recall deliberately NOT adopted (noise + prompt-cache
invalidation; TencentDB reached the same conclusion: inject only stable summaries,
expose fine-grained search as a tool).]**

- **RM.1.1** A `SessionStart` hook (matcher `startup|resume`, **opt-in at install**,
  default off): resolve the cwd's remote (same normalize), and if the project is
  registered, print the latest digest (RT.4) + the K most recent current entries —
  recency-ordered with visible dates, budgeted, wrapped in the I5 framing.
- **RM.1.2** Fail-open and fast: server unreachable / no remote / unregistered → exit 0
  silently within a hard sub-second budget (local registry cache; never a blocking
  network wait — the hook must be unnoticeable when Loom is down).
- **Anti-requirements (each one is a verified mem0-local defect, kept as a test):**
  - No per-prompt hook (`UserPromptSubmit`) — ever, in this requirement.
  - Never unordered/heap-order output; recency is asserted in tests.
  - Never inject without the I5 untrusted framing (second-order injection surface).
  - Never block or delay session start on failure (mem0-local hung up to 8 s per
    prompt when its server was warming up).
- **AC:**
  - [ ] In a registered repo: session starts with digest + dated entries under budget.
  - [ ] In an unregistered repo / server down: hook exits 0, <1 s, zero output.
  - [ ] Installer merge keeps any pre-existing user `SessionStart` hooks (mem0-local's
    installer clobbered whole hook events; ours must extend the T5 merge test to this
    event).
- **Open questions for the owner's deliberation:**
  - OQ-M1: Does ambient injection erode D6 (advisory logbook) — does content pushed
    into every session start carry more implied authority than content the reviewer
    asked for via `/loom`?
  - OQ-M2: Author vs reviewer: the hook fires for the project *author* too. Useful
    (continuity) or noise (they already know)? Per-role or per-repo opt-in?
  - OQ-M3: Is the pilot-gate signal ("did reviewers consult /loom?") invalidated if
    recall becomes ambient? (Metric changes meaning.)

### RM.2 — Delta-composition instrumentation
**[Origin: mem0-local — its audit measured that 55% of the messages its capture hook
sent to the extractor were empty `[tool_use: X]` markers, silently degrading extraction
quality. Loom's adapter is better (500-char tool excerpts) but nothing measures it.]**

- **RM.2.1** `read_delta` reports composition (user/assistant/tool chars, marker-only
  message count); `loom status` and the contract test surface it; a threshold (e.g.
  >60% tool noise) flags the delta in `loom status` as low-signal.
- **AC:** the real T0 fixture yields stable composition numbers in `make contract`;
  a synthetic tool-storm fixture trips the flag.

### RM.3 — Retrieval-quality benchmark harness (backlog)
**[Origin: mem0-local — `benchmark_embeddings.py`: retrieval benchmark over the user's
real corpus with LLM-generated indirect queries, 4-gram leak control between query and
target, paired confidence intervals, checkpointing. The only rigorously evaluated
component of that project.]**

- Only worth building if RT.1's ranking quality is ever questioned or semantic search
  is ever reconsidered (DESIGN §11 change — a design decision, not an implementation
  one). Kept here as methodology pointer, not scheduled scope.

---

## 4. Suggested build cut (input for the build plan, NOT a plan)

**Run T** (after the 2026-08-14 gate verdict; supersedes spec-R3.4's slot):

| Wave | Epics | Notes |
|---|---|---|
| a | `logbook-search` (RT.1) ∥ `typed-items` (RT.2) | Disjoint files: RT.1 = server schema/app + cli/api_client read path; RT.2 = prompts/, okf.py, processor parse. Prompt v2 fixture recorded before build (real-boundary oracle = summarize a fixture transcript with the real CLI). |
| b | `curation-links` (RT.3) | Depends on RT.1 (candidate pool) and RT.2 (typed inputs improve verdicts, soft dep). |
| c | `digest` (RT.4) + phase E2E gate | Gate scenario: 20-entry fixture project with one reversal → curate → search → budgeted `/loom` output shows digest + supersession. |

**Run M** (separate, later, only after the owner unblocks Block M): RM.1 + RM.2 as one
small single-phase run; RM.3 stays backlog.

Epic IDs continue the initiative-wide sequence (next free: E13).

---

## 5. Provenance summary

| Requirement | Origin project | Origin mechanism |
|---|---|---|
| RT.1 | TencentDB-Agent-Memory | BM25-first retrieval on SQLite FTS5, embeddings off by default |
| RT.2 | TencentDB-Agent-Memory | L1 atom taxonomy (`work_fact/task/method/artifact`) |
| RT.3 | TencentDB-Agent-Memory | Batch conflict detection with store/skip/update/merge verdicts |
| RT.4 | TencentDB-Agent-Memory | L1→L2→L3 consolidation + budgeted injection |
| RM.1 | mem0-local | Lifecycle-hook ambient recall (SessionStart), minus its verified defects |
| RM.2 | mem0-local | Measured transcript-noise finding (55% marker-only messages) |
| RM.3 | mem0-local | Retrieval benchmark methodology (leak control, paired CIs) |

Explicitly **not** imported: Tencent's LLM proxy (`ANTHROPIC_BASE_URL` interception —
violates I3/D5 and forces API-token billing), Tencent's 4-service topology, Tencent's
numeric priority field (no consumer), mem0-local's per-prompt recall hook (noise +
cache invalidation), mem0-local's vector/embedding stack (DESIGN §11).
