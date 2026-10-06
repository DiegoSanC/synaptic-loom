---
title: "feat: Label grammar fidelity and addressable items — summarizer v3, item model, item links, curation previews"
type: feat
date: 2026-09-23
complexity: high
github_issue: none  # problems 3 and 4 have no issue; pulls in the identity part of #9, the link part of #12 and #6's partial supersede
baseline_in_issue: main at 3efe1a5 (D9 #42, D10 #43, recall events #44 merged)
branch_from: main (3efe1a5)
branches: fix/curation-preview-body · feat/summarizer-v3 · feat/entry-items · feat/log-items · feat/item-links · (09-22 plan) feat/recall-feedback, item-first
---

# feat: Label grammar fidelity and addressable items

## Overview

Prompt v2 (`prompts/summarizer_v2.md`) and the database describe the same knowledge at
different grains and with different vocabularies. Four mismatches follow:

| # | Problem | Example of what a reader gets today |
|---|---|---|
| 1 | **Granularity.** Labels are per bullet; the DB stores a per-entry label set (`entry_item_types`) | `loom log --type method` returns whole entries: one rule plus five unrelated decisions |
| 2 | **Status.** Per-item in-session `Status: current\|reverted\|superseded by X` exists only as text; the served entry status (`current\|superseded\|duplicate`) is derived from `entry_links`; the OKF frontmatter always says `status: current` | A decision dropped an hour later is still an item, can be the entry's title (first bold bullet), and answers `--type decision`; a superseded entry printed under `--all` says `status: current` |
| 3 | **`[task]` vs `## Open items`.** Pending work fits both definitions; only `[task]` is typed | `--type task` and the digest's "Open tasks" miss work left in *Open items*. The v2 golden itself lists the same RUNBOOK task in both places (`test/waves/i/fixtures/summary_v2_golden.md:7,11`) |
| 4 | **`decision` as the catch-all.** "If unsure, use [decision]"; the processor types every unlabelled or unknown-label bullet `decision` | `[método] **No editar schema.sql**` (a translated label) is stored as a decision and never answers `--type method` |

Research for this plan found a fifth defect, in the same family and the most concrete:

| F-1 | **Curation candidates are previewed as frontmatter.** `build_payload` shows each candidate as `_flatten(content_md, 400)` (`client/loom/curate.py:103,510-511`); a real OKF frontmatter is ~430 characters (measured: 428 with `okf.wrap_entry` on a one-item entry) | The curation LLM sees `--- type: decision-log-entry title: "…" timestamp: … status: current … sources: - session: …` and **nothing of the decisions** beyond the title. `pool_query` (`curate.py:368`) also draws candidate-search terms from that frontmatter. The gate never noticed because its fixture frontmatter is ~110 characters (`client/tests/e2e/test_blockt_e2e.py:770-784`) |

### Owner decisions of 2026-09-23

The first version of this plan (same date) raised five questions. The owner accepted
four and overrode the fifth:

| Question | Answer |
|---|---|
| (A) Summarizer v3 with five rule changes | **Accepted** → D11 |
| (B) How v3 reaches the pilot | **`loom enroll` writes `v3`; `Config` default stays `v2`** (the D9 pattern) |
| (C) `--type` semantics | **Items by default**, `--whole-entries` as the escape |
| (D) Served status is authoritative over frontmatter `status` | **Accepted** → D12 |
| (E) Defer per-item storage (#9/#12) with re-open triggers | **Overridden: build the item model now**, while the hosted corpus is 6 entries, 0 links, 0 feedback, 0 digests → D13. This overrides the #9–#12 deferral in the pilot scope for the parts listed in "Scope of the item model". Its eight sub-questions (E1–E8, under "Decisions raised") were answered as recommended; E7 = manual curation until the owner has checked link quality |

The owner's reason for (E): migrating a data model is cheapest when there is no data.
Research adds a sharper version of it: **item rows can be re-derived later from the
immutable `content_md` of labelled entries, but feedback rows and curation links written
per entry can never be re-assigned to an item without ambiguity.** The real deadline is
therefore not "now" but *before the first feedback row and before the first curation link
is written* — and neither exists yet.

### What the evidence says (measured 2026-09-23, read-only)

The hosted logbook holds **6 entries in 1 project** (`loom-pilot`), all by the maintainer,
at least one synthetic: **1 × v2**, 2 × v1, 3 × pre-OKF. `label_fallback` total on this
machine: 0 over 1 v2 post. No digest exists; curation has never linked anything; no
feedback table exists. None of the four problems can be *rated* on this corpus — only
shown to be mechanically real, which the code and the v2 golden already do.

- **What is decided at capture is permanent** (D7). Every pilot entry captured under v2
  keeps problems 2–4 forever: the prompt must change **before the pilot window opens**.
- **Per-entry feedback and links are permanent too.** Feedback must be born item-grained
  (the 09-22 plan's branch D is not built yet), and nobody runs `loom curate` against the
  hosted server until item links exist.
- **What is decided at reading is reversible** (`loom log`, curation previews).

## What exists already (verified at c48081a, re-checked on main 3efe1a5)

- **Frozen seams** (the build plan's seam record): `t-label-grammar`
  (strict `^- \[(decision|method|task|fact)\] ` under `## Decisions` only, fallback
  `decision`, the PROCESSOR parses, `DEFAULT_PROMPT_VERSION='v2'`, prompt edits re-record
  the golden), `t-entry-item-types` (:446 — closed vocabulary, per-entry set, the entry
  response keeps its frozen fields), `t-entry-links` (:505 — status derived, never
  stored; a rebuild is sanctioned only for a new link KIND), `t-curation-watermark`
  (:520 — verdict grammar "exact"), `t-digest-entry` (:548), `t-prompt-shipping` (:492).
  Phase-1 seams are additive-only (the phase-4 build manifest).
- **A new prompt version is an anticipated extension point.** Versions ship *alongside*
  earlier ones (`client/install.sh:606-612`); `normalize_prompt_version` accepts any
  token-shaped version (`client/loom/config.py:208-233`); the label parse is gated by an
  allowlist where "a future labeled version is one literal added here"
  (`processor.py:87-92`); `prompt_version` is part of `content_hash`
  (`processor.py:808-822`); the server stores it as free text (`server/app.py:313,541-545`).
- **The v2 default is frozen at the `Config` constructor**, not at onboarding
  (`test/waves/i/test_goal_e14_typed_items.py:295-300`; the no-config `loom doctor`
  fallback, `test/waves/k/test_goal_e16_digest.py:317-326`). What `loom enroll` writes is
  pinned only by a non-frozen test (`client/tests/integration/test_cli_enroll.py:79`).
- **The v2 prompt was validated once**, on one synthetic English conversation
  (`test/waves/i/fixtures/summary_v2_golden.md`). No automated check has ever run a
  summarizer prompt live; pilot sessions are mostly Spanish.
- **Entries are the only unit on the server.** `EntryCreate` ignores unknown fields
  (`server/app.py:304-305`: an old server silently drops a new payload field); labels go
  to `entry_item_types` in the entry's transaction (`app.py:549-557`); **any**
  `IntegrityError` on that insert answers `409 duplicate_entry` (`app.py:558-560`,
  follow-up F5 of the wave-i build review), which a new CHECK-constrained child table would
  turn into a permanently dropped entry. The entry response key sets are pinned exactly
  (`server/tests/test_links.py:392,396,405,416`; `server/tests/test_search.py:341`).
- **The server image ships only `server/`** (`Dockerfile:58`; wave m
  `test_goal_fly_packaging.py:696-714`): the server cannot import the client's parser.
- **Links are per entry, anonymous and permanent.** `POST /api/links` checks only
  structure (existing entries, same project, newer→older, no contradictory pair —
  `app.py:575-655`) and records no actor; there is no revocation route. Status is derived
  at read time (`server/links.py:159-171`).
- **`loom curate` is scheduled nowhere** — not in `install.sh`, `client/timer/`,
  `client/hooks/` or the RUNBOOK. Links and digests exist only when someone runs it by
  hand; the frozen trigger bytes (waves b, d, g) pin `loom process` as the only scheduled
  command.
- **The frozen fixtures post entries without items** and often without frontmatter
  (`test_goal_e13_search.py:34-46,253-273`; gate `test_blockt_e2e.py:523-541,770-784`),
  and read `/entries?type=`, `/entries?q=` and `loom log` expecting entry-grained results
  (E13 :115-126, :253-273, :317-324; gate a :974-982, :1007-1013, :1102; gate c
  :1397-1437; wave j :258, :275-281; wave k :310). The curation replay stubs parse the
  literal markers `### ENTRY <id>` and `- CANDIDATE <id>: ` (gate :636-667; wave j
  :120-122; wave k :122-123).
- **Nothing reads the OKF `status:` field** (`curate._frontmatter_type` reads only
  `type:`, `curate.py:302-316`); consumers use the served `status`
  (`curate.entry_status`, aliased at `cli.py:776`).
- **No rendering prints an entry id today** except the digest header and collapse notes
  (`cli._entry_header`, `cli.py:676-680`): even the 09-22 plan's `loom feedback --entry`
  has nothing for an agent to cite.

## Design

Six slices. Slices A, B and D are client-only; C, E and F add additive server tables and
routes; no existing table, route or response shape changes.

### Slice A — curation previews read the body (F-1)

- `okf.body(content_md) -> str`: the document without its leading `---…---` frontmatter
  block (unchanged when there is none).
- Candidate preview = `_flatten(preview_source, CANDIDATE_PREVIEW_CHARS)` where
  `preview_source` is the body's `## Decisions` section when one exists, otherwise the
  whole body. `pool_query` extracts its terms from `okf.body(content_md)`.
- Still needed after slice E: un-itemized entries keep entry-level curation.

### Slice B — summarizer v3 (problems 2, 3, 4 at capture) — D11

**The prompt** (`prompts/summarizer_v3.md`, full draft in the appendix):

| Rule | v2 | v3 | Fixes |
|---|---|---|---|
| What an item is | any decision, with its in-session status | **only what still holds at the end of the excerpt**; a reversed choice is not an item, the discarded option is named inside the final item's rationale | 2 |
| Per-item `Status:` | required | **removed** | 2 |
| Pending work | `[task]` = "agreed future work"; *Open items* = "open questions or deferred work" | **all pending work is a `[task]`**; `## Open questions` holds only undecided questions, omitted when empty | 3 |
| When unsure | "If unsure, use [decision]" | **`[decision]` only for a choice among alternatives; otherwise `[fact]`** | 4 |
| Language | prose in the session's language; sentinel and labels literal English | **also the section headings** literal English | 4 |

Unchanged on purpose: the four labels, `## Decisions` as the only typed section, the
`NO_ENTRY` rule, the evidence and redaction rules, `FALLBACK_ITEM_TYPE = "decision"`
(frozen), `extract_title` (under v3 the first bold bullet is final by construction),
`digest_v1.md` (its "Open tasks" come from `[task]`, which under v3 is all pending work).

**Product changes.** `LABELLED_PROMPT_VERSIONS = {"v2", "v3"}`; `install.sh` step 3
ships `summarizer_v3.md` beside v1 and v2; `loom enroll` writes `"prompt_version": "v3"`
on a fresh config and preserves an existing value; `Config`'s default and `loom init`
stay on `v2`; a RUNBOOK upgrade note moves the maintainer's two enrolled machines.

**Validation — an evaluation harness outside the product** (doctrine rules 4, 10, 12):
`test/prompt_eval/` (not shipped, not collected by pytest):
- `cases/<name>.txt`: ~10 **synthetic** deltas in the `render_for_summarizer` format,
  half Spanish, half English: in-session reversal; rejected alternative; agreed task;
  deferred work phrased as an aside; open question; standing rule; one-off fact; ambiguous
  item; no reviewable content (→ `NO_ENTRY`); a session quoting the output template.
- `cases/<name>.expect.json`, **authored from the case design before any run**: required
  labels, forbidden bold heads, strings that must sit inside a `[task]` item and not under
  `## Open questions`, `NO_ENTRY` exactness, `label_fallback == 0`, and — using slice C's
  splitter — the expected item count and labels.
- `run_eval.py --prompts v2 v3 --runs 3`: the product's invocation shape, the product's
  parser for scoring, a table per case × prompt, `claude --version` and the model recorded.
- **Pass rule, fixed now: every case passes in all 3 runs.** A flaky case revises the
  prompt, never the threshold. v2 runs as a baseline; its failures are the evidence for
  the change, not a gate.

**Frozen goal wave `o`**, authored before implementation: two v3 goldens recorded once with
the real CLI (one Spanish with a reversal, deferred work and an open question; one English
with a rule and a fact), replayed through the real `run_once` (the E14 pattern).

### Slice C — addressable items (D13)

**Identity.** An item is one bullet of `## Decisions` in an entry captured under a
labelled prompt version (v2, v3), with its continuation lines and sub-bullets. Its public
id is **`<entry_id>.<ordinal>`** (e.g. `17.2`), ordinal 1..n in document order. Entries
stay what they are — the immutable session record and the item's provenance (D7).

**Who produces items: the client, validated by the server.** The processor already owns
the grammar (t-label-grammar) and the server image cannot import it (`Dockerfile:58`), so
the client sends items and the server checks them without understanding markdown:

- `okf.split_items(summary_md, prompt_version) -> list[Item]` (`Item = item_type,
  labelled, text_md`): `text_md` is the item's lines **verbatim**, so it is a substring of
  the summary and therefore of `content_md`. Labelled versions only; `[]` otherwise.
  `processor.parse_item_types` is re-expressed on top of it (refactor, suite green before
  and after), so storage-time typing and item rows cannot drift. A golden manifest pins
  `split_items` over the v2 and v3 goldens, with the expected ordinals, labels and texts
  written by hand from the golden files.
- `POST /api/entries` gains an optional `items: [{item_type, labelled, text_md}]`
  (additive, the way `item_types` was added). Validation → `422 invalid_items`, nothing
  stored: at most 50 items; `item_type` in the closed vocabulary; `text_md` non-empty and
  **a substring of `content_md`**; when `item_types` is also sent it must equal the sorted
  set of the items' types (when absent it is derived from them). The server assigns
  ordinals 1..n in payload order and inserts the items in the entry's transaction.
- The substring rule keeps every item inside bytes the client already secret-scanned. It
  also guards quarantine: after a hand redaction of `content_md`, a stale item that still
  holds the secret fails with 422. `loom push-quarantine` re-derives items from the edited
  `content_md` before posting, so the 422 is a second line of defence, not the path.
- **F5 fixed in the same change**: only `SQLITE_CONSTRAINT_UNIQUE` on `content_hash`
  answers `409 duplicate_entry`; any other `IntegrityError` is a 500 (the discrimination
  `app.py:636-645` already does for links).
- A re-POST of the same `content_hash` is a 409 and writes nothing: the stored entry keeps
  the items of the POST that created it.
- `item_types` keeps being sent and stored exactly as today (frozen payload and
  frontmatter).

**Schema** (`server/items.py`, `ensure_items_schema` from `init_db`, the #20 pattern;
`schema.sql` untouched; every object is a new table, so no CHECK is ever altered):

```sql
CREATE TABLE IF NOT EXISTS entry_items (
  id INTEGER PRIMARY KEY,
  entry_id INTEGER NOT NULL REFERENCES entries(id),
  ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
  item_type TEXT NOT NULL CHECK (item_type IN ('decision','method','task','fact')),
  labelled INTEGER NOT NULL CHECK (labelled IN (0, 1)),   -- 0 = processor fallback
  text_md TEXT NOT NULL,
  UNIQUE (entry_id, ordinal)
);
-- + entry_items_fts: FTS5 external content over text_md, AFTER INSERT trigger on
--   entry_items (never on entries), backfilled with 'rebuild' like entries_fts.
CREATE TABLE IF NOT EXISTS item_links (            -- written from slice E
  id INTEGER PRIMARY KEY,
  from_item INTEGER NOT NULL REFERENCES entry_items(id),
  to_item INTEGER NOT NULL REFERENCES entry_items(id),
  kind TEXT NOT NULL CHECK (kind IN ('supersedes','duplicates')),
  rationale TEXT,
  created_by INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL,
  revoked_at TEXT, revoked_by INTEGER REFERENCES users(id), revoke_reason TEXT
);
-- + a partial UNIQUE (from_item, to_item, kind) WHERE revoked_at IS NULL,
--   and an index on to_item.
```

**Read route** (new; entry responses are untouched):
`GET /api/projects/{id}/items?entry_ids=<csv, ≤ 500>[&type=X][&q=terms]` →
`{items: [{entry_id, ordinal, item_type, labelled, text_md, match, snippet?, status,
links_in, links_out}]}`. It returns **all** items of the listed entries; `match` is true
when the item satisfies every filter given (`type` = its label, `q` = an
`entry_items_fts` match, with `snippet`). Returning non-matching items too is what lets a
reader tell "itemized, nothing matched" from "not itemized". `status` and the link lists
are derived from non-revoked `item_links` exactly as `links.derive_status` does for
entries; before slice E every item is `current` with empty lists. Errors mirror
`/entries`: `422 invalid_query`, `422 invalid_type`.

**Existing entries.** The 5 unlabelled entries cannot be itemized and stay entry-only.
The one v2 entry is the maintainer's capture-test data: no backfill (decision E6). Every
reader treats an entry with no items as a whole, as today.

**Rollout order.** An old server drops `items` silently (`extra="ignore"`): the server
deploy comes first, then clients. The migration runs at boot on the one worker; a Fly
snapshot precedes it (RUNBOOK §0 restore note: a rolled-back image ignores the new tables,
and entries it accepts in that window have no items).

### Slice D — `loom log` reads items (problem 1; problem 2 on the read side)

**Retrieval stays entry-first.** The page of entries comes from `/entries` exactly as
today — same `type`, `q`, `limit`, `since`, pagination and ranking — so every frozen
`loom log` assertion reads the same entries. One extra call,
`GET …/items?entry_ids=<page>` with the same `type`/`q`, returns the page's items.

Rendering per entry (block count and budget accounting unchanged: one block per entry,
built by `_entry_block`):

- **Un-itemized entry** (no items returned): rendered exactly as today.
- **Default view, itemized entry:** today's block, with each item's id printed in front
  of its text (`17.2  - [method] **…**`), located by the item's `text_md`. A superseded
  item's text is replaced by one line `17.2 → replaced by 21.1 on <date>`; a duplicate by
  `17.2 → duplicate of 12.1`. The notes never use the `superseded by #<n>` prefix that the
  entry collapse uses (gate a checks it by substring). An itemized entry whose items are
  all superseded or duplicate collapses to one line like a superseded entry.
- **`--type X` or `--grep`, itemized entry:** header, one line
  `"<title>"  ·  <k> of <n> items`, then only the items with `match: true`, each with its
  id (and snippet under `--grep`). An itemized entry with no matching item is not printed.
- **`--whole-entries`:** today's rendering, byte-for-byte.
- **Served status overlays the frontmatter** (D12): when an entry is rendered whole and
  its served status is not `current`, its `status: current` line prints as
  `status: superseded (by #N)` / `status: duplicate (of #N)`.
- `SKILL.md`: item ids (`<entry>.<n>`), `--type` narrowing to items, and (from slice F)
  `/loom-feedback` citing those ids; the phrases pinned by E13 :352-354, E16 :329-333 and
  `test_cli_skills.py` stay.

### Slice E — item links and item-level curation

**Server.**
- `POST /api/item-links {from, to, kind, rationale}` with `from`/`to` as `"<entry>.<n>"`:
  `404 item_not_found`; `422` unless both items are in the same project; `422` unless
  `entry(from) > entry(to)` strictly (no link inside one entry, no cycles — the argument
  of `app.py:579-585`); `409 contradictory_link` / `409 duplicate_link` among active links,
  checked under `BEGIN IMMEDIATE`; `201 {id}`. `created_by` is the caller.
- `POST /api/item-links/{id}/revoke {reason}` → `200`; `409 already_revoked`; `404`.
  A revoked link stays as history and stops counting; the same link can be asserted again.
- Item `links_in`/`links_out` include the author's name and, for revoked links, who
  revoked and why (#12). **Entry status is untouched** — still derived from `entry_links`
  only (t-entry-links).
- `/api/links` is never extended: an old server would silently turn an item-qualified body
  into a permanent whole-entry link.

**Client curation.**
- For itemized entries the payload keeps the frozen markers and lists items inside them:
  `### ENTRY 21` followed by the entry's items as `[21.1] [decision] …`, and candidates as
  `- CANDIDATE 17: [17.1] [method] … [17.2] [decision] …` (item text, not frontmatter).
- The verdict grammar gains an **additive** qualified form, one line per new item:
  `21.1: keep` · `21.1: supersedes:17.2[,12.1]` · `21.1: duplicates:17.2`. Entry-form lines
  (`21: …`) stay valid and are what un-itemized entries receive. "Every presented unit
  gets exactly one verdict": an item for itemized entries, the entry otherwise. Every id
  after the colon must be an item of a candidate of the same block.
- Item verdicts post to `/api/item-links`, entry verdicts to `/api/links` as today. The
  watermark stays an entry id (items are born with their entry); `pending_batch` gains the
  item-link body shape.
- `CURATION_PROMPT` (inline in `curate.py`) is rewritten for items and keeps the phrase the
  replay stub keys on ("curating a team decision logbook"). Candidate pools draw
  `pool_query` terms from item text; a candidate entry qualifies while it has at least one
  current item.
- **Digest:** the input keeps `### ENTRY` blocks and `digest_v1`; superseded and duplicate
  items' `text_md` are removed from each current entry's text before it is sent.
- **`loom unlink <link-id> --reason "<one line>"`** revokes an item link.
- **Scheduling stays manual** (decision E7): a RUNBOOK section "Curating" — one machine
  only (the watermark is local, so a second machine re-curates from zero and may disagree),
  weekly, `loom curate <project>`, then read the new `→ replaced by` notes and
  `loom unlink` any wrong one — and **no `loom curate` against the hosted server until
  this slice is deployed**. Re-open automatic curation (inside `loom process`, behind a
  config flag, on one machine, at most daily, only with new entries — the trigger bytes
  are frozen) once the owner has judged link quality on real pilot entries.

### Slice F — feedback born item-grained (amends the 09-22 plan, branch D)

- `recall_feedback.entry_id` becomes `item_id INTEGER NOT NULL REFERENCES
  entry_items(id)`; `feedback_hash = sha256(user_id, item_id, verdict, note)`.
- `POST /api/feedback {project_id, item: "<entry>.<n>", verdict, note}` → `201`,
  `409 duplicate`, `404 item_not_found`, `422` when the item is not in the project or the
  verdict is outside the vocabulary. Un-itemized entries cannot receive feedback (they are
  the maintainer's pre-v2 test captures).
- `GET /api/stats`: top 10 **items** by `useful`; still no per-user field.
- `loom feedback --item 17.2 --verdict useful|not_applicable|stale --note "<one line>"`;
  `/loom-feedback` tells the agent to cite the ids `loom log` printed; the CLAUDE.md block
  regains the plan's sentence pointing at `/loom-feedback` (dropped from PR #43).
- `recall_events` (already on main, #44) is unchanged: `X-Loom-Source` rides the FIRST
  `/entries` request of a `loom log` only (`RECALL_SOURCE_HEADER`, `client/loom/cli.py`);
  slice D's items call sends no source header; `result_count` stays the number of entries
  on the page.

### Scope of the item model (what D13 pulls in, and what stays deferred)

| From | Pulled in now | Still deferred |
|---|---|---|
| #9 addressable learnings | stable item identity, provenance to the entry | revisions, problem/solution/applicability fields, evidence records |
| #12 correction | actor on links, revocation with reason and history | "retire" with no replacement (a new link kind), restore workflow, concurrent-edit conflicts |
| #6 multi-item curation | partial supersede: a link hides one item, never its siblings; body previews | — |
| #10, #11, #14, promotion to agreements | — | all |

The pilot scope's "Deferred work" section is amended accordingly in slice C's branch.

## Problem → fix map

| Problem | Fixed by | What remains |
|---|---|---|
| 1 Granularity | C (items addressable), D (`--type`/`--grep` show items), E (links per item), F (feedback per item) | revisions and richer learning fields (#9 rest) |
| 2 Status / title | B (no reverted items, no per-item status), D (served status overlaid; item status shown), E (partial supersede per item), A (curator sees decisions) | the one v2 entry and the v1 entries keep their text |
| 3 Task vs Open items | B | v2/v1 entries keep the ambiguity |
| 4 Decision catch-all | B; `labelled = 0` on each fallback item makes misfiling visible per item | processor fallback stays `decision` (frozen) |
| F-1 Curation previews | A (entries), E (items) | — |

## Decisions raised

Recorded answers (see "Owner decisions of 2026-09-23"): A → **D11**, B, C, D → **D12**.
The item model is **D13**. **The owner accepted the recommendation on E1–E8 the same
day**; on E7 specifically: curation stays manual *for a while*, until the owner has
checked how well item-level links work, and no auto-curation flag is built in this plan.
The questions, kept for the record:

- **E1 — Who produces items.** Client splits and sends; server validates by substring
  (recommended — one grammar owner, the server stays markdown-blind, quarantine is
  guarded), or the server re-parses `content_md` (a second copy of the grammar inside
  `server/`, contradicting t-label-grammar's "the PROCESSOR parses").
- **E2 — Public item id `<entry>.<ordinal>`** (recommended — readable, stable because
  entries are immutable, derivable by eye from `loom log`), or opaque integer ids.
- **E3 — Reading is entry-first, rendering is item-level** (recommended — ranking,
  pagination and every frozen `loom log`/`/entries` assertion unchanged), or item-ranked
  search on its own route (better ranking; amends E13, gate b and gate c assertions).
- **E4 — `item_links` with actor and revocation, exposed on reads.** The 09-22 rule
  "aggregated by team, never by person" covers metrics; a correction shows who made it
  (#12). Recommended: accept.
- **E5 — Curation verdict grammar gains the qualified form** `<e>.<n>: …` inside the frozen
  markers — an amendment of the t-curation-watermark seam ("grammar exact") that changes
  no frozen test. Recommended: accept.
- **E6 — No backfill** of the 6 existing entries (recommended — maintainer's test
  captures), or a one-shot `server.admin import-items` for the v2 one.
- **E7 — Curation stays manual** for the pilot, with the "no curate until slice E" rule
  (recommended), or scheduled inside `loom process` behind a config flag on one machine
  (the frozen trigger bytes rule out a new timer line).
- **E8 — amendment to the pilot scope** as in "Scope of the item model". Recommended: accept.

## Frozen surfaces

**Untouched:** waves a–n and their fixtures; `summarizer_v1.md`, `summarizer_v2.md`,
`digest_v1.md`; `schema.sql`; `entries`, `entry_item_types`, `entry_links` and their
DDL; `/entries` query semantics and response key sets; `/api/links`; the `### ENTRY` /
`- CANDIDATE` markers and the entry-form verdicts; `extract_title`; `FALLBACK_ITEM_TYPE`;
`DEFAULT_PROMPT_VERSION`; the stored `status: current` literal; `contract/*.md`.

**Seam amendments (no frozen test changes):** t-curation-watermark (additive qualified
verdicts, E5); new seams t-entry-items, t-items-read, t-item-links, t-item-feedback in
the build plan's seam record, written when each slice's goal wave is frozen.

**API additions (AGENTS.md rule 13, recorded in DESIGN §6.2 under D13):** `items` in
`POST /api/entries`; `GET /api/projects/{id}/items`; `POST /api/item-links`;
`POST /api/item-links/{id}/revoke`; `POST /api/feedback` item-first (with the 09-22
plan's `/api/stats`).

**Non-frozen tests that change, and why** (each named in its commit message):
- `client/tests/test_typed_items.py:270` — `"v3"` as the example of an *unknown* version
  becomes `"v9"`; same for the prose at `docs/RUNBOOK.md:836`.
- `client/tests/integration/test_cli_enroll.py:79` — a fresh enrol writes `v3` (B).
- `client/tests/integration/test_install_update.py:33-45` — `CHECKOUT_FILES` lists
  `summarizer_v3.md`.
- `client/tests/test_curate.py` — `pending_batch` and verdict-validation tests gain the
  item shapes; the entry shapes stay asserted.
- `client/tests/e2e/test_blockt_e2e.py:130,1614` — `GOAL_WAVE_DIRS` gains each new wave
  (`o`, `p`, `q`); the `>= 288` floor rises by exactly the tests added, in the same commit.

## Acceptance criteria → assertions

| # | Criterion | Asserted in |
|---|---|---|
| A1 | A candidate built from a real `okf.wrap_entry` output (frontmatter > 400 chars) previews its first Decisions item and no frontmatter key | `client/tests/test_curate.py` (new) |
| A2 | `pool_query` on a wrapped entry yields body terms and none of: timestamp digits, UUID fragments, `sonnet`, `github` | `client/tests/test_curate.py` (new) |
| A3 | Through the real server: a new entry reversing a candidate whose decision sits past character 400 of its raw `content_md` gets `supersedes` from a replay stub that links only what it sees; before the fix, `keep` | `test_blockt_e2e.py` (new leg, real-size frontmatter) |
| B1 | `summarizer_v3.md` carries the four labels, the literal-English rule for headings and labels, `## Open questions`, no `Status:` in its format, and none of the stub-classifier phrases | wave `o` |
| B2 | The two v3 goldens replayed through `run_once`: posted `item_types` and `items` equal the authored literals; `label_fallback` 0; the title is the final decision; no `Status:`; frontmatter `prompt: "v3"`; pending work only in `[task]` items | wave `o` (frozen before implementation) |
| B3 | `parse_item_types(s, "v3")` types like v2; `"v9"` stays untyped | `test_typed_items.py` |
| B4 | `install.sh` (fresh and `--update`) ships `summarizer_v3.md` beside v1 and v2 | `test_install_update.py`; waves b, g green |
| B5 | Fresh `loom enroll` writes `v3`; an existing value is preserved; `Config()` still defaults to `v2`; `loom init` unchanged | `test_cli_enroll.py`; wave i green |
| B6 | Eval: v3 passes every case in 3 of 3 runs; v2 baseline attached | `test/prompt_eval/RESULTS.md`, linked from the PR |
| C1 | `split_items` over the v2 and v3 goldens equals the hand-written manifest (ordinals, labels, `labelled`, verbatim text); `parse_item_types` equals the label set of `split_items` on every existing typed-items case | `client/tests/test_typed_items.py` + golden manifest |
| C2 | POST with items → 201; `GET …/items` returns them with ordinals 1..n in order and `text_md` byte-equal | wave `p` (real HTTP boundary) |
| C3 | An item whose text is not in `content_md`, an unknown type, more than 50 items, or `item_types` inconsistent with the items → 422 `invalid_items`, and no entry row exists afterwards | wave `p` |
| C4 | A re-POST of the same `content_hash` with other items → 409; stored items unchanged | wave `p` |
| C5 | A non-UNIQUE `IntegrityError` on entry insert answers 500, never `409 duplicate_entry` | `server/tests/test_server.py` (new) |
| C6 | `push-quarantine` after a redaction posts items re-derived from the edited `content_md`; no posted item contains the removed string | `client/tests/integration` (new) |
| C7 | An entry posted without items: `/entries` responses (key sets, `?type=`, `?q=`) are byte-identical to today; `…/items` returns nothing for it | wave `p`; existing server tests green |
| C8 | `ensure_items_schema` twice on a populated database is a no-op; `entry_items_fts` agrees with `entry_items` | `server/tests/test_items.py` (new) |
| D1 | Default `loom log`: itemized entries print item ids in place; one block per entry; gate a :1102 and wave k :310 green | `test_cli_log.py` (new); gate, wave k |
| D2 | `--type method` on an itemized entry with two decisions and one method prints only the method item with its id and `1 of 3 items`; an un-itemized entry carrying `method` prints whole (E13 :317-324 green) | `test_cli_log.py`; wave i |
| D3 | `--grep` on itemized entries prints only matching items with a snippet each | `test_cli_log.py` |
| D4 | A superseded item prints `→ replaced by`; no item note contains `superseded by #`; an entry whose items are all superseded collapses to one line | `test_cli_log.py` |
| D5 | `--whole-entries` output is byte-identical to today's, captured as a golden **before** the change | `test_cli_log.py` (golden manifest) |
| D6 | Under `--all`, a superseded entry renders `status: superseded (by #N)`; a current entry renders byte-identically | `test_cli_log.py` |
| E1 | Item link: 201 with `created_by`; 404 unknown item; 422 cross-project, same entry, older→newer; 409 duplicate and contradictory among active links | wave `q` |
| E2 | Revoke: 200, then 409; a revoked link no longer affects status; re-asserting it → 201 | wave `q` |
| E3 | Item links never change any entry's served status | wave `q` |
| E4 | Replay curation through the real server: a new entry reversing one of two items of an older entry links that item only; the sibling stays current and visible in `loom log` (#6) | wave `q` (record-replay stub) |
| E5 | Entry-form verdicts for un-itemized entries post `/api/links` exactly as waves j and gate a expect | waves j, gate a green |
| E6 | The digest payload contains no text of a superseded item | `client/tests/test_digest.py` (new) |
| F1 | Feedback per item: 201, then 409 for the same; an item of another project 422; `/api/stats` carries no user field | 09-22 plan's `server/tests/test_feedback.py`, item-first |
| F2 | `loom feedback --item 17.2 …` posts; an unknown id exits 1 naming it | `client/tests/integration` (new) |
| All | `make test` and `LOOM_SKIP_LIVE=1 make contract` green on each branch | CI + local (Docker required) |

## Branches and sequencing

| Order | Branch | Slices | Size | Hard constraint |
|---|---|---|---|---|
| 1 | `fix/curation-preview-body` | A | S (≈ ½ d) | — |
| 2 | `feat/summarizer-v3` | B | M (≈ 2 d) | **before the pilot window opens** |
| 3 | `feat/entry-items` | C (+ F5, + pilot scope, DESIGN D13, §6.2) | M–L (≈ 3 d) | server deployed before any client sends items |
| 4 | `feat/log-items` | D | M (≈ 1½ d) | — |
| 5 | 09-22 `feat/recall-feedback` | F | S on top of the 09-22 estimate (≈ 1 d extra) | **never before C**: feedback is born item-grained |
| 6 | `feat/item-links` | E | L (≈ 3–4 d) | **no `loom curate` against the hosted server before it deploys** |

Roughly **11–12 working days**, against 3½ for the version without the item model. Slices
3–5 are what the pilot needs before its window (items visible and citable, feedback per
item). Slice 6 may follow the window's opening, because nothing writes links until someone
runs `loom curate`.

Waves: `o` (B), `p` (C and D at the HTTP boundary), `q` (E), each frozen before its
implementation starts. Every branch runs the full `make test` (the docs are pinned by
tests).

## Risks

- **Size.** The item model roughly triples the plan for one maintainer. The cut line above
  keeps what cannot be done later (v3, items, item-first feedback) ahead of the window.
- **The model ignores the literal-English rule on Spanish sessions.** The eval measures it;
  the fallback still types the item, now visibly (`labelled = 0`).
- **The splitter and the model disagree on what an item is** (sub-bullets, fences,
  continuation lines). The golden manifest pins the splitter; the eval checks item counts
  per case.
- **Folding reversals loses history.** The discarded option stays inside the final item's
  rationale; the eval's reversal cases assert it.
- **An old client or server in the loop.** An old server drops `items` silently; an old
  client sends none. Both leave entries un-itemized, which every reader handles. The
  deploy order and `loom doctor`'s client/server version line make it visible.
- **Curation quality at item grain is unmeasured.** Manual curation, the `→ replaced by`
  notes and `loom unlink` keep a wrong link visible and reversible.
- **CLI drift** (golden recorded with 2.1.228, `contract/VERSION` 2.1.218): the eval
  records its version; re-pinning belongs to the 09-22 plan.

## Appendix — `prompts/summarizer_v3.md` (draft)

The rules are what D11 approves; the eval may still change wording. Do not reuse this
text as a test oracle: wave o's expectations are authored from the cases.

```
You are a technical analyst. You receive an excerpt of a conversation between a
developer and their coding agent. Extract EXCLUSIVELY what is useful to a
reviewer who will assess these changes with no prior context.

Record the FINAL state of the excerpt, not its history:

1. Every item under "## Decisions" is something that still holds at the END of
   the excerpt. If a choice was made and later reversed or replaced within the
   excerpt, record only the final choice and name the discarded option inside its
   rationale ("X was tried and dropped because ..."). Never list a discarded,
   reverted or replaced option as an item of its own.
2. Every item starts with EXACTLY ONE label from this closed vocabulary, chosen
   by what the item IS:
   - [decision] — a choice among alternatives ("use X instead of Y").
   - [method]   — a standing rule, SOP or taboo ("always run make contract after
     CLI updates", "never edit schema.sql in place").
   - [task]     — pending work of any kind: agreed, deferred or left to do, with
     owner/deadline if stated. ALL pending work is a [task] item.
   - [fact]     — a finding or constraint that is none of the above.
   Use [decision] ONLY for a choice among alternatives; anything else that is
   neither a rule nor pending work is a [fact]. Never invent other labels.
3. Rationale ONLY if it is stated in the text. If it is not explicit, write
   "(rationale not stated)" — never invent it.
4. Minimum evidence per item: a short verbatim quote, a file or a commit mentioned.
5. "## Open questions" holds only questions the excerpt left UNDECIDED. Never put
   pending work there and never repeat an item from "## Decisions". Omit the
   section when there are none.
6. Ignore: tool errors, retries, explorations that reached no conclusion, and
   implementation details already visible in a diff.
7. Never include credentials, tokens, connection strings, PII or customer
   identifiers; refer to those values in the abstract.
8. If there is no reviewable decision, respond exactly: NO_ENTRY
9. Write the prose in the same language as the conversation. The section
   headings, the four labels and the NO_ENTRY sentinel are ALWAYS literal
   English, exactly as written in the format below, whatever the language of
   the conversation.

Output format (markdown):
## Context
<1-2 sentences: what was worked on>
## Decisions
- [decision] **<what was decided>** — <rationale, including any discarded option, or "(rationale not stated)">. Evidence: <quote/file/commit>.
- [method] **<the standing rule>** — <why, if stated>. Evidence: <quote/file/commit>.
- [task] **<the pending work>** — <owner/deadline if stated>. Evidence: <quote/file/commit>.
- [fact] **<the finding>** — <why it matters, if stated>. Evidence: <quote/file/commit>.
## Open questions
- <a question the excerpt left undecided>
```
