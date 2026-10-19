# Synaptic Loom — Phase 2 & Phase 3 specification (draft for review)

Status: **DRAFT v1 — not approved for build.** This document consolidates the phase 2/3
scope that was previously scattered across `DESIGN.md` (§4.1, §5.1, §6.3, §10, §11),
`docs/RUNBOOK.md` (§9) and `PLAN.md` (P1, P3), so it can be gap-analyzed as a whole
(document review + flow analysis) before any build planning starts.

Baseline: phase 1 is deployed and operating (see `docs/RUNBOOK.md`). Pilot gate runs
until ~**2026-08-14** (PLAN §5); its outcome CONDITIONS phase 3 (see §6).

---

## 1. Context and goals

Phase 1 delivered the full capture pipeline (SessionEnd hook → queue → decoupled
processor → tool-less summarizer → secret scan → central server) plus CLI consumption
(`loom log`, skills) for a single-machine pilot.

- **Phase 2 — Robustness.** Close the known capture/operability holes of the pilot.
  Value: fewer silently lost sessions, stronger secret scanning, less manual operation.
  NOT gated on the pilot outcome — it hardens the pilot itself while it runs.
- **Phase 3 — Full team.** Take the system from "one machine, one reviewer" to the
  whole team: server reachable from all machines, browse/manage without the CLI,
  curation so the logbook stays trustworthy over time. GATED on the pilot verdict.

### Non-goals (unchanged from DESIGN §11)

- MCP consumption interface (the HTTP skill covers the use case; revisit only if
  semantic search becomes a requirement).
- Semantic search / embeddings.
- Capturing subagent-internal reasoning (final reports already appear in the main
  transcript; sufficient for decision-level summaries).
- Multi-team / multi-org tenancy.

---

## 2. Frozen phase-1 contracts (read-only for phases 2–3)

Any phase 2/3 design that would mutate one of these must be flagged and re-decided
explicitly:

| Contract | Where defined |
|---|---|
| Project identity = normalized git remote URL | DESIGN §3 |
| Marker/queue format + hook behavior (mark + kick, always exit 0) | DESIGN §4.1 |
| Processor error matrix + pointer semantics (advance only after confirmed push) | PLAN §T4 |
| API endpoints, bodies and error codes | PLAN §3 |
| DB schema: `users`, `projects`, `project_paths`, `entries` (append-only, unique `content_hash`) | DESIGN §6.1 |
| Entries are self-contained OKF concepts (frontmatter built by the processor, D7) | DESIGN §5.1 |
| Summarizer invocation: `claude -p --safe-mode --tools ""`, delta via stdin | contract/FLAGS.md |
| Security invariants I1–I6 | DESIGN §1 |
| `~/.loom` layout + `config.json` keys | PLAN §T5 |

---

## 3. Phase 2 — Robustness

### R2.1 Orphan-transcript sweep

Sessions that die without a clean SessionEnd (crash, SIGKILL, WSL shutdown) never get
a marker and are currently lost (accepted in phase 1, DESIGN §4.1).

- **R2.1.1** A sweep component compares transcripts on disk (`~/.claude/projects/**/*.jsonl`)
  against `state.json` pointers and the queue, and enqueues a synthetic marker for any
  transcript with unprocessed content older than a settle threshold (default: mtime > 30 min).
- **R2.1.2** The sweep derives the session `cwd` from the transcript content (JSONL lines
  carry `cwd`), NOT from the encoded directory name. Transcripts whose cwd no longer
  exists or has no registered remote follow the normal gate (dropped).
- **R2.1.3** The sweep is idempotent and respects the existing lock; it never races the
  kick path. Synthetic markers are distinguishable (`"origin": "sweep"`) for logging.
- **AC:** kill a session mid-work (no SessionEnd) → after the sweep runs, its entry
  reaches the server with the same quality as a hook-captured one; re-running the sweep
  produces no duplicates (pointer + content_hash).

### R2.2 Periodic trigger (timer)

Phase 1 relies on hook kicks + manual `loom process` (P1). Retries of kept markers wait
for the next session close.

- **R2.2.1** A periodic trigger runs `loom process` (which also runs the R2.1 sweep):
  systemd **user** timer where available, cron fallback, both documented; WSL2 caveats
  (VM lifetime) documented in the RUNBOOK.
- **R2.2.2** Default cadence: 15 min. Timer installation is part of `install.sh`
  (idempotent, `--yes` respected) and checked by `loom doctor`.
- **AC:** with the server down, close a session → marker kept; restore the server → the
  entry lands within one timer period with no human action.

### R2.3 Full secret scanning (gitleaks)

Phase 1 ships a curated regex set (P3). Known gap: entropy-based and lesser-known
credential formats.

- **R2.3.1** Integrate gitleaks as the primary scanner over the final wrapped
  `content_md`, keeping the current regex set as a built-in fallback when gitleaks is
  not installed (doctor reports which engine is active). *Superseded by DESIGN.md D16
  (2026-10-15): there is no fallback any more — gitleaks is mandatory, installed by
  install.sh, and capture pauses without it.*
- **R2.3.2** Fail-closed semantics preserved exactly (I4): scanner error == positive.
  Findings map to the existing quarantine flow unchanged.
- **R2.3.3** gitleaks version pinned in config; contract-test-style check that the
  binary answers as expected after machine updates.
- **AC:** a planted high-entropy token that the regex set misses is quarantined when
  gitleaks is active; `push-quarantine` re-scan uses the same engine selection.

### R2.4 Installer/packaging polish

- **R2.4.1** `install.sh` gains the timer installation (R2.2.2) and a `--update` path
  (git pull + re-copy hook/prompt/skills + doctor) so client updates are one command.
- **R2.4.2** `loom doctor` extended: timer present/active, sweep last-run age, scanner
  engine + version.
- **AC:** fresh-machine install and update-in-place both leave doctor fully green.

---

## 4. Phase 3 — Full team

### R3.1 Server exposure (TLS / move off localhost)

- **R3.1.1** The server becomes reachable from the team's machines. Two candidate
  mechanisms (DECISION PENDING, see Q-3.1): internal VM + Caddy reverse proxy with an
  internal cert, or `tailscale serve` inside the team VPN.
- **R3.1.2** TLS mandatory for any non-localhost binding (DESIGN §8). Plain HTTP
  remains allowed only on 127.0.0.1. R3.1 concerns **transport only**: the
  authentication scheme is unchanged (opaque bearer tokens — see Q-3.2 for the JWT
  analysis and Q-3.3 for UI sessions).
- **R3.1.3** Migration from the pilot host: documented runbook step (stop → copy
  SQLite + backups → start on VM → clients re-`init` with the new URL). Token rotation
  on migration (DECISION PENDING, Q-3.2).
- **AC:** `loom log` works from a second machine against the exposed server over TLS;
  the pilot's data survives the migration intact.

### R3.2 Minimal web UI

Scope (DESIGN §6.3): login → project list → browsable logbook (by project/date/author)
→ manual project registration (same endpoint + duplicate warning as the CLI) → merge
of path-fallback registrations into a logical project → token management.

- **R3.2.1** Login mechanism: **resolved by DESIGN D14 (2026-09-29)** — `loom ui` trades
  the machine's bearer for a single-use login code; the browser gets a short, server-side
  session. The read-only part of R3.2 shipped with it (DESIGN §6.3); registration, merge
  and token management remain (token management: DESIGN D20, R3.2.2).
- **R3.2.2** Token management: create/revoke per-dev tokens from the UI, replacing the
  `admin.py` CLI for day-to-day use (CLI remains for bootstrap). Requires an admin
  concept (Q-3.4). *(Delivered 2026-10-19 by DESIGN D20, phase 2a, §6.4: in `/ui` a
  member adds and revokes their own machines and an admin invites, deactivates, changes
  roles and revokes any machine; `admin.py` stays for bootstrap.)*
- **R3.2.3** The read-only browsing need may be partially covered by R3.5 (OKF
  visualizer); the UI build should start AFTER R3.5 lands so its scope can shrink to
  what the visualizer does not cover (login, registration, merge, tokens).
- **AC:** each UI flow usable end-to-end by a non-CLI user; registration duplicate
  warning matches the API contract; revoking a token cuts access immediately.

### R3.3 Path-fallback registration + merge

For repos without a git remote (edge case; DESIGN §3 fallback).

- **R3.3.1** `POST /api/paths` (deferred from phase 1) or equivalent: associate a
  local path with an existing logical project; `loom register` in a remote-less repo
  offers it instead of exiting 1.
- **R3.3.2** UI merge flow: map one or more `project_paths` rows to a logical project;
  processor gate consults path registrations when no remote resolves.
- **AC:** a remote-less repo registered by path captures sessions into the merged
  logical project from two different machines/paths.

### R3.4 Curation digest (logbook staleness)

The logbook is append-only; decisions get superseded and the entry `status` currently
lives ONLY inside the OKF frontmatter of immutable `content_md` (D7). **Definition gap
acknowledged:** marking `superseded` requires a mechanism the schema does not have
(Q-3.5 — options: mutable `status` column + PATCH endpoint; supersession-link table;
or periodic digest entries that declare current state without mutating history).

- **R3.4.1** A periodic curation pass (scheduled `claude -p`, same safety profile as
  the summarizer: tool-less, stdin, fail-closed scan) reads a project's recent entries
  and produces a "current state of decisions" digest.
- **R3.4.2** Superseded decisions become discoverable: consumers (`loom log`, UI,
  OKF export) can distinguish current vs superseded without reading the full history.
- **AC:** after a session reverses an earlier decision, the digest marks the old one
  superseded and links its replacement; `loom log` surfaces the distinction.

### R3.5 OKF export + static visualizer

Entries are already OKF concepts (D7); this exposes them as bundles.

- **R3.5.1** `GET /api/projects/{id}/okf` emits the project bundle: one file per
  entry + a generated `index.md`; delivery format DECISION PENDING (Q-3.6: zip vs
  per-file routes vs both).
- **R3.5.2** Google's static HTML visualizer is served (or generated) for read-only
  browsing of the bundle — no new backend.
- **R3.5.3** Bundle access enforces the same auth as the API.
- **AC:** an agent with no loom tooling ingests the bundle markdown directly; the
  visualizer renders the project graph; unauthorized access is rejected.

### R3.6 Team onboarding

- **R3.6.1** The other 2 devs onboarded: users + tokens, client install, ≥1 real
  project each registered (subject to the no-client-data policy, DESIGN §8).
- **R3.6.2** RUNBOOK updated from single-machine pilot to team deployment.
- **AC:** a review in the real ring (Dev N reviewing Dev M's project) uses `/loom`
  against the shared server.

---

## 5. Cross-phase constraints

- Security invariants I1–I6 apply unchanged to every new component (sweep, timer,
  curation pass, UI, export).
- The curation pass and the sweep are new automated `claude -p` consumers → same
  guards as the summarizer: `LOOM_SUMMARIZER=1`, neutral cwd, `--safe-mode`,
  `--tools ""`, fail-closed scan before anything leaves the machine.
- LLM cost: sweep and curation add scheduled Sonnet runs; stay within the OAuth
  subscription assumption unless the team provisions an API key (open, Q-2.1).
- Every phase-2/3 change lands with the same test discipline as phase 1 (unit +
  integration + contract; error-matrix rows for new failure modes).

---

## 6. Gate dependency (what the 2026-08-14 verdict changes)

| Gate outcome | Effect on this spec |
|---|---|
| Logbook consulted + trusted | Build phase 3 as specified |
| Consulted but noisy/vague | Iterate summarizer prompt first; R3.4 (curation) gains priority over R3.2 (UI) |
| Not consulted | STOP: phase 3 does not build; phase 2 items already built keep the pilot cheap to leave running |
| OKF visualizer (R3.5) proves sufficient for browsing | R3.2 shrinks to login/registration/merge/tokens |

Phase 2 proceeds regardless (it hardens the running pilot).

---

## 7. Open questions (known — the gap analysis should extend this list)

- **Q-2.1** Summarizer/sweep/curation LLM auth at team scale: stay on each dev's OAuth
  subscription, or provision a shared `ANTHROPIC_API_KEY` for background runs?
- **Q-2.2** Sweep threshold and scope: is 30-min mtime settle right? Should the sweep
  also cover transcripts from before the sweep feature existed?
  *Analysis (2026-07-29):* the settle threshold is a **heuristic**, not a liveness
  check — an active session appends to its transcript constantly, so "no writes in
  30 min" ≈ "no session behind it". Known false positive: a session left open but idle
  past the threshold gets its partial content summarized early; this is harmless by
  design (the pointer captures the remainder at the real close; `NO_ENTRY` suppresses
  decision-less partials; worst case is one conversation split across two entries,
  never duplicates or loss). Stronger liveness signals (`lsof` on the transcript, a
  SessionStart sentinel cleared by SessionEnd) were considered and deliberately
  rejected for v1: platform coupling to eliminate a failure mode that is already
  cheap. The open part of this question is therefore only the threshold VALUE and
  whether the first sweep backfills pre-existing transcripts.
- **Q-3.1** Server exposure mechanism: VM+Caddy vs Tailscale — depends on team infra;
  who owns the VM, certificates, and uptime?
- **Q-3.2** Token rotation policy on server migration and on a schedule; are tokens
  per-dev or per-machine (a dev with two machines today shares one token)?
  *Analysis (2026-07-29):* **JWT was considered and rejected as the machine/CLI API
  credential.** Reasons: (a) a signed JWT is valid until expiry — instant revocation
  (the security requirement for ex-employees) would need a server-side denylist,
  reintroducing exactly the state lookup JWT exists to avoid; (b) the API's main
  client is the UNATTENDED processor (kick at night, timer) — expiring tokens break
  background pushes unless refresh-token machinery is added; (c) stateless validation
  solves a scale problem a 3-user SQLite service does not have. Opaque long-lived
  bearer tokens (hashed server-side, chmod-600 config) remain the machine credential.
- **Q-3.3** *(Resolved 2026-09-29 by DESIGN D14: neither paste-token nor passwords — the
  CLI mints a single-use login code with its bearer, and the browser holds a 12 h
  server-side session that dies with the device.)* UI login: reuse bearer tokens (paste-token login), or add real sessions/
  passwords/SSO? Password infra does not exist and may be out of scope.
  *Candidate (2026-07-29), hybrid:* the UI login validates the dev's opaque token
  (paste-token or a later mechanism) and issues a **short-lived signed session** for
  the browser — a signed cookie (preferred for a single-server app) or a short-lived
  JWT, where non-revocability stops mattering because of the short TTL. The CLI/
  processor keeps its opaque token untouched (see Q-3.2). This keeps machine and
  human credentials separate, each with the right lifecycle.
- **Q-3.4** *(Resolved in part 2026-10-19 by DESIGN D20: global roles, `admin` or
  `member` in `users.role`; admins invite, deactivate, change roles and revoke any
  machine, members manage their own machines. Per-project permissions remain open.)*
  Admin model: who can create/revoke users and merge paths — any team member, or an
  admin role (schema has no roles)?
- **Q-3.5** Supersession mechanism (see R3.4): mutable status column vs link table vs
  digest-only. Entries are append-only by contract — which option preserves that?
- **Q-3.6** OKF bundle delivery: zip, per-file routes, or both; and how the visualizer
  is hosted (static file from the API server?).
- **Q-3.7** Concurrency/scale: a small team × sessions/day is trivial for SQLite, but the
  exposed server now takes writes from several machines — is WAL + busy_timeout enough
  (assumed yes; validate)?
- **Q-3.8** Rate limiting / payload caps on the exposed API (absent today; localhost
  made them unnecessary)?

---

## 8. Suggested build cut (input for the build plan, NOT a plan)

- **Phase 2 run (one planned build run, now):** epics ≈ {R2.1 sweep, R2.2 timer + R2.4 packaging,
  R2.3 gitleaks}. Independent, parallelizable, each with a real-boundary goal.
- **Phase 3 run (one planned build run, after the gate):** epics ≈ {R3.1 exposure+
  migration, R3.5 OKF export+visualizer, R3.2 UI (scope per gate + R3.5), R3.3 path
  fallback, R3.4 curation, R3.6 onboarding}. R3.5 before R3.2 (see R3.2.3).
