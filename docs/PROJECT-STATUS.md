# Synaptic Loom — Project status & handoff

> Historical snapshot: this file records the project's state as of its last update and is no longer maintained.

Last updated: **2026-07-30**. Purpose: single entry point to continue this project from
any session (human or agent) without prior context. Read this first; every section
links to the authoritative document.

---

## 1. What was asked (the original brief and how it evolved)

**Original problem (2026-07-24).** A small AI team whose members review each other's
projects develops with agentic AI at high velocity. The reviewer's cognitive load — understanding *what*
a peer did and *why* — is the bottleneck. Requested: a centralized service that
auto-captures per-project "logbooks" (decisions + rationale, NOT full transcripts)
from each dev's Claude Code sessions, opt-in per project, consumable by the reviewer's
own Claude session.

**Key evolutions during design/build:**
- Multi-agent evaluation first (business/architecture/security/data/feasibility +
  platform-facts verification) → verdict NEEDS_WORK → redesign agreed with the user.
- Project identity moved from filesystem paths to **normalized git remote URL**
  (killed the path-merge UI as a core feature; it survives only as a fallback).
- Capture moved from "hook runs the summarizer" to **hook-marks + decoupled processor**
  (write-ahead queue + worker).
- The user explicitly chose a **central server + SQLite + per-dev tokens** over the
  evaluators' git-repo-as-store suggestion (product decision, not to be reopened).
- Entries became **self-contained OKF concepts** (Google's Open Knowledge Format) after
  the user asked to evaluate OKF fit (2026-07-28).
- The project is headed for **open source** → entire repo (code, comments, strings,
  docs, prompt) anglicized (2026-07-28).

## 2. What is built and deployed (phase 1 — COMPLETE)

Built by Opus subagents in tasks T0–T7 (PLAN.md), 169 tests green. Deployed and
operating on a single developer machine since 2026-07-24 (see `docs/RUNBOOK.md`):

- **Capture pipeline:** SessionEnd hook (marks + kicks, always exit 0) → queue markers
  → decoupled idempotent processor (remote gate → delta by pointer → tool-less
  `claude -p --safe-mode --tools ""` summarizer via stdin → fail-closed secret scan →
  POST). Error matrix in PLAN §T4 is normative.
- **Server:** FastAPI + SQLite (WAL), bearer-token auth, admin CLI; **hosted on Fly.io**
  at https://<app>.fly.dev since the cutover of 2026-09-21 (platform TLS, one Machine
  in `cdg`, SQLite on a persistent volume, daily volume snapshots kept 14 days). The
  phase-1 systemd user unit on 127.0.0.1:8787 with cron backups is **retired** (issue #17).
- **Client:** `loom` CLI (process/register/log/status/push-quarantine/init/doctor),
  installer (idempotent, settings.json merge with backup), skills `/loom` and
  `/loom-register`.
- **OKF (D7):** every entry ships YAML frontmatter (type/title/timestamp/generated/
  status/project/tags/sources) built deterministically by the processor.
- **Pilot:** a synthetic repo (`loom-pilot`); E2E verified including
  incremental resume linkage, cross-user reviewer view, quarantine flow.
- **T0 platform contract** (`contract/`): verified CLI facts (v2.1.218) — `--bare`
  breaks OAuth (use `--safe-mode`), `--allowedTools ""` does NOT block tools (use
  `--tools ""`), `-p` fires SessionEnd, transcript JSONL schema. Re-verify with
  `make contract` after any Claude Code update.

## 3. Decisions taken (with pointers)

| Decision | Where recorded |
|---|---|
| D1–D7 design decisions (identity, capture, pointer, store, summarizer, advisory logbook, OKF entries) | DESIGN.md §1 + §5.1 |
| P1–P6 plan decisions (kick not cron, logic-in-CLI, regex scan v1, retry policy, adapter fail-loud, stack) | PLAN.md §0 |
| Security invariants I1–I6 (incl. summarizer isolation, fail-closed scan, logbook-as-untrusted-data) | DESIGN.md §1 |
| Server on a single WSL2 developer machine + synthetic pilot + OAuth summarizer auth | User choices, RUNBOOK §0–1 |
| Open source → full-English repo; generated summaries follow the conversation's language (prompt rule 7) | prompts/summarizer_v1.md; commits 90d920d + pass 2 |
| JWT rejected as API machine credential; short-lived signed session only as UI candidate | PHASE-2-3-SPEC.md Q-3.2/Q-3.3 |
| Sweep liveness = mtime settle heuristic; lsof/sentinel rejected for v1 | PHASE-2-3-SPEC.md Q-2.2 |
| Phases 2/3 via **two separate planned build runs** (phase 3 planned only AFTER the pilot gate; NOT one multi-phase plan — the gate is a deliberate learn point) | Conversation 2026-07-28; this doc §5 |
| Gap analysis BEFORE build: a multi-lens document review + a spec flow analysis over the consolidated spec | Conversation 2026-07-28; this doc §5 |

## 4. Where things stand right now

- **Pilot gate running until ~2026-08-14.** After each real review: did the reviewer
  consult `/loom`? time saved? noise/wrong entries? Decision rule in PLAN §5 /
  RUNBOOK §7. Gate outcome CONDITIONS phase 3 (spec §6).
- **Phase 2/3 spec consolidated for review:** `docs/PHASE-2-3-SPEC.md` (DRAFT v1,
  requirements R2.1–R2.4 / R3.1–R3.6 with ACs, frozen phase-1 contracts, open
  questions Q-2.1…Q-3.8). Companion with worked examples:
  `docs/PHASE-2-3-EXPLAINED.md`.
- **Assessed buildability (2026-07-30):** phase 2 is implementable as specced.
  Phase 3 is NOT yet: blocked on Q-3.1 (exposure mechanism / who owns the VM),
  Q-3.4 (admin model → schema change), Q-3.5 (supersession mechanism), the gate
  outcome, plus two definition gaps — API contracts for the new endpoints (OKF
  export, /api/paths, token/session management) at PLAN-§3 rigor, and UI definition
  (screens, stack, missing endpoints).
- **Open-source items:** published without a LICENSE file for now (owner decision,
  2026-10-06); the internal-data sweep before publishing is done.

## 5. Next steps (in order)

1. **Gap analysis on the spec** (agreed, not yet run): a multi-lens document review
   over `docs/PHASE-2-3-SPEC.md` (expect adversarial / scope-guardian / security /
   design lenses to trigger) + a spec flow analysis focused on the phase-3 flows (UI, onboarding, tokens, merge). Fix spec → iterate to
   "Ready".
2. **Resolve blocking questions with the user:** Q-3.1, Q-3.4, Q-3.5 (+ Q-2.1 API key
   vs OAuth at team scale; Q-3.6 bundle delivery).
3. **Phase 2 build (can start NOW, parallel to the gate):** one planned build run on
   this repo. Suggested epic cut in spec §8.
4. **Pilot gate review (~2026-08-14):** apply the decision rule; adjust phase-3 scope
   per spec §6 (note: if the OKF visualizer covers browsing, R3.2 shrinks).
5. **Phase 3:** update spec with gate learnings + resolved Qs → write API contracts at
   PLAN-§3 rigor for the new endpoints → second planned build run, which must respect
   the seams the first run froze.
6. **Imported improvements (2026-08-12):** `docs/IMPORTED-IMPROVEMENTS-SPEC.md` — Block T
   (Tencent-inspired: RT.1–RT.4) goes to a planned build run after the gate verdict and
   replaces spec-R3.4's slot; Block M (mem0-inspired: RM.1–RM.3) is DEFERRED until the
   owner deliberates on OQ-M1…M3 — do not route it before that.

## 6. Map of authoritative documents

| Document | What it is |
|---|---|
| `DESIGN.md` | Phase-1 technical design: decisions D1–D7, invariants, architecture, schema, API, prompt |
| `PLAN.md` | Phase-1 executed plan: T0–T7, normative API contracts (§3), error matrix (§T4), pilot gate rule (§5) |
| `contract/` | Empirically verified Claude Code platform facts + fixtures + `verify.py` (`make contract`) |
| `docs/RUNBOOK.md` | Operating the deployed pilot: start/stop/backup/users/onboarding/troubleshooting |
| `docs/PHASE-2-3-SPEC.md` | Phase 2/3 requirements under review (the gap-analysis input) |
| `docs/IMPORTED-IMPROVEMENTS-SPEC.md` | Improvements adapted from TencentDB-Agent-Memory (Block T: FTS5 search, typed items, supersession links, digest — implements R3.4/Q-3.5) and mem0-local (Block M: ambient recall — DEFERRED pending owner deliberation). Input for two future build runs |
| `docs/PHASE-2-3-EXPLAINED.md` | Companion to the spec: each requirement explained with worked examples |
| `docs/PROJECT-STATUS.md` | This file |
