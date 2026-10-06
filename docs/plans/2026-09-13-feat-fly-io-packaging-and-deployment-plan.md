---
title: "feat: Package and deploy the Loom API on Fly.io with persistent SQLite"
type: feat
date: 2026-09-13
complexity: large
github_issue: 2
branch: fm/fly-packaging-deploy
---

# feat: Package and deploy the Loom API on Fly.io with persistent SQLite

## Overview

Move the pilot Loom API off `127.0.0.1:8787` on a single developer machine and onto Fly.io as
**exactly one Machine in `cdg`**, with the SQLite database on a persistent 1GB volume,
carrying the existing pilot data across.

This is mostly packaging, configuration, migration and documentation. **Exactly one line of
product code changes** — the auth middleware's path check, which is safe on loopback and
unsafe on the public internet (see Problem Statement). Everything else is artifacts the repo
does not have yet (`Dockerfile`, `fly.toml`, `.dockerignore`, `uv.lock`, an entrypoint), a
data migration that is easy to do wrong, and retracting three documents that currently assert
a different deployment.

Work happens on `fm/fly-packaging-deploy`, branched from **`origin/main`** — not from the
local checkout, which is 7 commits behind (see §Technical Approach → Baseline).

## Problem Statement

The pilot server runs as a bare `uvicorn` process bound to loopback on a single developer's
machine (`docs/RUNBOOK.md:16`). Only that machine can reach it. The team is meant to
share one logbook (issue #15), and the reviewer flow (`/loom`) assumes a server reachable
from a second machine. Until the API is hosted, the pilot cannot demonstrate the thing it
exists to demonstrate: cross-developer reuse.

Two facts make this narrower than it sounds, and one makes it wider.

**Narrower.** The server needs no code change to be containerised:

- `server/db.py:27` reads `LOOM_DB` from the environment — pointing it at a volume is one
  env var.
- `server/app.py:25-28` applies the schema at startup inside the FastAPI `lifespan`, and it
  is idempotent and additive, so a fresh volume and a migrated volume both boot correctly.
- `server/` imports only stdlib, `fastapi`, `pydantic` and `starlette` — **no coupling to
  the `loom` client package** (verified across all of `server/*.py`).

**One code change, and it is a direct consequence of going public.** Issue #2 states the auth
middleware "exempts only `/api/health`". It does not. `server/app.py:92` reads:

```python
if not path.startswith("/api/") or path == "/api/health":
    return await call_next(request)
```

That exempts `/api/health` *and everything not shaped like `/api/*`*. `server/app.py:33`
constructs `FastAPI(title=..., version=VERSION, lifespan=lifespan)` with no `docs_url`,
`redoc_url` or `openapi_url` override, so FastAPI's defaults are live: **`/docs`, `/redoc`
and `/openapi.json` are unauthenticated.** On loopback that was unreachable by anyone but the
developer. On `*.fly.dev` it publishes the full API schema — every route, field, validator,
the `item_types` and `link.kind` vocabularies — to any anonymous visitor, plus a working
"Try it out" console.

The fix is to invert the condition into a strict allow-list rather than to null the three doc
URLs:

```python
if path == "/api/health":
    return await call_next(request)
```

Nulling `docs_url` fixes today's symptom; inverting the check fixes the class, so the next
non-`/api/` route anyone adds (a favicon, a static mount, a websocket) is protected by
default instead of exposed by default. This is a behaviour change to a documented contract
and belongs in the issue's record, not in a silent commit.

**Wider.** Three committed documents state a *different* deployment:

| Where | What it currently says |
|---|---|
| `DESIGN.md` §6.2 (`:303`) | "TLS mandatory (Caddy with an internal certificate, or everything inside Tailscale/VPN)." |
| `docs/RUNBOOK.md` §9 | Phase-3 TODO: "get the server off `127.0.0.1`. Options: an internal VM with Caddy… or `tailscale serve`." |
| `docs/PROJECT-STATUS.md` §5 | Same two options as next steps; Fly.io is not anticipated anywhere. |
`AGENTS.md` §7 and §13 are explicit that deviating from `DESIGN.md` is "a raised decision,
not a local choice", and that `DESIGN.md` §6.2 is a read-only shared contract. So amending
these three is in scope for this issue, not a tidy-up afterwards.

`test/phase_gates/phase_N_e2e_config.json` also carries `"container_parity": false`
with the reason *"CLI tool + localhost pilot service… no Docker"*. That one is **not** in scope
and must not be touched — see Phase 1 below for why it only looks like a contradiction.

## Proposed Solution

One Fly app, one Machine, one volume, one region.

```
  loom CLI (the team)                       [issue #17 — blocked by this]
        │ HTTPS, Bearer token
        ▼
  Fly proxy  ── shared IPv4, force_https, *.fly.dev ──┐
                                                      │ auto_start on request
                                          ┌───────────▼────────────┐
                                          │ 1 Machine · cdg        │
                                          │ shared-cpu-1x · 256MB  │
                                          │ uvicorn server.app:app │
                                          │ 0.0.0.0:8080 · 1 worker│
                                          └───────────┬────────────┘
                                                      │ /data  (LOOM_DB=/data/loom.db)
                                          ┌───────────▼────────────┐
                                          │ Volume 1GB · cdg       │
                                          │ loom.db + -wal + -shm  │
                                          └────────────────────────┘
```

Cold start is accepted (owner decision). `min_machines_running = 0`, so between sessions the
Machine is stopped and only the volume bills.

## Technical Approach

### Baseline

Branch from `origin/main` (`3b43aa6`), **not** from the local working tree (`971ea6c`,
7 commits behind). The 7 commits matter: they added `AGENTS.md` (binding doctrine),
`CLAUDE.md` (which just imports it), and `.github/workflows/ci.yml`. Any plan written
against the local checkout would miss all three.

```bash
git fetch origin
git switch -c fm/fly-packaging-deploy origin/main
```

### Doctrine that shapes this work

From `AGENTS.md`, three rules bind directly:

- **§9** — "Turn every acceptance criterion into executable assertions before implementing,
  and leave no acceptance criterion orphaned." The six ACs of issue #2 become assertions
  *first*.
- **§10** — "Validate the real boundary: **Docker** for containerizable infrastructure."
  The image is the boundary. Build it and run it; do not simulate it.
- **§12** — "Never weaken or delete acceptance assertions to get green."

### Where the assertions live

The repo already has the right two-tier pattern, and the six ACs split cleanly across it.

**Tier 1 — a frozen goal wave, real Docker boundary.** New `test/waves/m/test_goal_fly_packaging.py`,
following the existing wave convention (`test/waves/README.md`) and requiring a real
`docker build` + `docker run`, the way wave `a` requires a real gitleaks. Covers everything
that is a property of the *image and its configuration* and therefore testable without
spending money:

| AC | Executable assertion (local, no Fly resources) |
|---|---|
| AC1 build + FTS5 | **"Reproducible" is defined as:** base image pinned by digest (`python:3.12-slim@sha256:…`, not a floating tag) + committed `uv.lock` + `uv sync --locked`. Assertion: the build succeeds, and a deliberately stale `uv.lock` **fails** it — which is precisely what `--locked` buys over `--frozen`, and is a sharper check than diffing two builds (two builds of a digest-pinned base with a locked resolver are reproducible *by construction*; comparing them tests the premise rather than the artifact, and needs an invented comparison mechanism the plan would otherwise have to specify). Plus `docker run --rm IMG python -c "…pragma compile_options…"` lists `ENABLE_FTS5`; and a container on an empty mount reaches `/api/health` 200, proving `lifespan` schema init ran. |
| AC2 auth shape | Against the running container: `GET /api/health` → 200 with no header; `GET /api/projects` with no header → 401; with `Bearer garbage` → 401; **with a valid token → 200**; **and `GET /docs`, `/redoc`, `/openapi.json` → 401**, which is the assertion that pins the middleware fix and stops it regressing. |
| AC3 persistence | Write an entry; `docker rm -f` the container; start a **new** container on the **same** mount; read the entry back. Assert `loom.db`, `loom.db-wal`, `loom.db-shm` are all inside the mount and that **no** `loom.db` exists outside it. Also assert the container's `LOOM_DB` resolves under the mount — it is a plain env var, not a secret, and is the easiest thing in this whole change to leave unset. |
| AC5 no secrets | `docker save IMG \| tar -x` into a scratch dir and grep the **extracted layers** — not the running container's live filesystem, which cannot show what a discarded builder stage left behind. Assert no `*.db`, `.env`, token file or credential in any layer; `docker history --no-trunc` for command visibility; `gitleaks` over the build context. |
| AC4 port agreement | The Dockerfile's `--port`, `fly.toml`'s `internal_port` and the healthcheck path are three independently-edited values that must agree. Assert the container actually serves on the port `fly.toml` names. |

**How the wave actually reaches CI, and the two things that must move with it.**
`make test` runs `pytest server/tests client/tests` and does not name `test/waves/` at all.
The waves reach CI through `client/tests/e2e/test_blockt_e2e.py`, which *is* under
`client/tests`. Its `test_blockt_scenario_d_regression_goals` (`:1583`) runs:

```python
result = _run_suite([GOAL_WAVES_ROOT, "-rs"], timeout=7200)   # GOAL_WAVES_ROOT = "test/waves"
```

That is a **directory sweep**, and `test/waves/` has no `conftest.py` and no registry file.
So dropping `test/waves/m/` in place is collected automatically — the wave is not dead code,
and the README table edit registers nothing but documentation. Two things must still move
with it, and neither is cosmetic:

1. **`GOAL_WAVE_DIRS`** (`:1562`, `tuple(... for letter in "abcdefghijkl")`) backs
   `assert (REPO_ROOT / wave).is_dir()`. Its docstring says the waves are listed one by one
   "rather than swept up by a glob — so that a wave which stops being collected (renamed,
   emptied, moved) fails HERE instead of quietly not running." A wave `m` absent from that
   tuple runs but is exempt from that discipline. Add `"m"`.
2. **The collection floor** (`:1599`, `assert counts["passed"] >= 240`). Its comment records
   that 248 tests collect today and that the floor was set close to that on purpose — "A
   floor with 48 tests of slack is not a regression leg, it is a slogan." Adding a wave
   raises the collected count without raising the floor, so the slack grows and the floor
   silently loses the property it was chosen for. Raise it by the number of tests wave `m`
   adds, in the same commit.

Both edits touch a completed phase's gate file. That is additive — it adds an oracle and
weakens none — but it is a deliberate act on frozen machinery and belongs in the commit
message, not in a silent diff.

**Cost this adds to every run.** A real `docker build` + `docker run` now rides inside a
nested-subprocess regression leg already budgeted at 7200s, on every `make test` and every CI
run to `main`, and it makes a Docker daemon a hard CI dependency (GitHub's `ubuntu-latest`
ships one; this host has 29.1.5). Layer caching keeps the steady-state cost small, but this
is a real change to the project's test economics and `AGENTS.md`'s "minimal impact" priority
means saying so rather than inheriting it silently. The alternative — an acceptance
assertion CI never executes — is worse.

**Tier 2 — a deployment rehearsal record in `docs/RUNBOOK.md`**, following the existing
"Deployment rehearsal record" sections (`docs/RUNBOOK.md:657`, `:691`). Covers what only a
real Fly app can show: AC2's HTTPS leg, AC3's Machine-destroy leg, AC4 (exactly one Machine),
AC6 (recorded resources and month-to-date bill). Evidence is pasted command output with
dates, exactly as the existing records do it.

This split is what keeps AC4 and AC6 from being orphaned: they are not unit-testable, but
they are not hand-waved either — they have a named artifact and a named form of evidence.

### Artifacts to create

**`Dockerfile`** — multi-stage, `python:3.12-slim` (Debian bookworm's libsqlite3 ships FTS5;
Alpine/musl is rejected for wheel and runtime friction, distroless for lack of FTS5
provenance and no shell to verify it with).

The important structural decision: **do not install the project at all.** `pyproject.toml:19-24`
declares `packages = ["loom", "server"]` with `package-dir` mapping `loom → client/loom`, so
any `pip install .` / `uv sync` that builds the project drags `client/` into the build
context. Since `server/` imports nothing from `loom`, the clean answer is
`uv sync --locked --extra server --no-install-project` in **both** stages plus
`COPY server/ /app/server/` with `WORKDIR /app`. `server/__init__.py` exists, so the copied
tree is a real importable package, and `python -m uvicorn` inserts the CWD on `sys.path` —
no `PYTHONPATH` needed. The image then carries the dependencies and the `server` package and
nothing else.

The cost of this choice is one real drift risk, worth naming so nobody is surprised by it: it
is **dependencies, not files**. Adding an `import redis` to `server/*.py` without adding
`redis` to the `server` extra means `uv sync --extra server` never installs it. That failure
is loud and immediate, not silent — the lifespan import chain raises at process start and
AC1's "container reaches `/api/health` 200" assertion catches it on the very next build. A
bounded, self-alarming failure mode is an acceptable price for keeping the client out of the
server image.

```dockerfile
# Both stages pin the SAME digest — a floating :3.12-slim tag would make
# "builds reproducibly" untrue on the day Debian rebuilds the image.
FROM python:3.12-slim@sha256:<pin at implementation time> AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /bin/uv
ENV UV_PYTHON_DOWNLOADS=0 UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --extra server --no-install-project --no-editable

FROM python:3.12-slim@sha256:<same pin>
RUN useradd --system --create-home --home-dir /app app \
 && apt-get update && apt-get install -y --no-install-recommends gosu \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app server/ /app/server/
COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
ENV PATH="/app/.venv/bin:$PATH" LOOM_DB=/data/loom.db
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "server.app:app", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--timeout-graceful-shutdown", "30"]
```

Two details that are load-bearing rather than stylistic:

- **Exec-form `CMD`.** Shell form puts `/bin/sh -c` at PID 1, which does not forward SIGTERM;
  uvicorn would be hard-killed instead of running its graceful shutdown and the FastAPI
  `lifespan` exit. With `auto_stop_machines` this happens on *every* idle cycle, not rarely.
- **`kill_timeout` in `fly.toml` must be ≥ `--timeout-graceful-shutdown`**, or Fly SIGKILLs
  before shutdown completes and the point of the exec form is lost.

**`docker-entrypoint.sh`** — the non-root/volume-ownership fix. Fly creates a volume as
`root:root`; a `USER app` process gets `Permission denied` on its first write, and
`COPY --chown` cannot help because the volume is attached at *runtime*. Entrypoint runs as
root only long enough to `mkdir -p /data && chown app:app /data`, then `exec gosu app "$@"`.

**`fly.toml`** — the single-Machine contract, in config rather than in a human's memory:

```toml
app = "your-loom-app"            # exact name TBD at `fly apps create`
primary_region = "cdg"
kill_timeout = "30s"

[build]

[env]
  LOOM_DB = "/data/loom.db"

[http_service]
  internal_port = 8080
  force_https = true
  auto_stop_machines = "suspend"
  auto_start_machines = true
  min_machines_running = 0

[[vm]]
  size = "shared-cpu-1x"
  memory = "256mb"

[mounts]
  source = "loom_data"
  destination = "/data"
  initial_size = "1gb"
```

`auto_stop_machines` is a **three-way string enum** in current Fly config
(`"off"` / `"stop"` / `"suspend"`), not the boolean older examples use. `"suspend"` resumes
from a memory snapshot in ~hundreds of ms against ~2+ s for a cold `"stop"`, and requires
memory ≤ 2GB — which 256MB trivially satisfies. Given the cold-start concern against the
client's 3.05 s connect budget, `"suspend"` is the better default; the rehearsal measures
both and the choice is recorded.

**No `[[http_service.checks]]` block initially** — deliberately. Fly's documentation does not
state whether proxy health probes count as traffic for the auto-stop idle determination
(verified today: neither `fly.io/docs/launch/autostop-autostart/` nor
`fly.io/docs/reference/fly-proxy-autostop-autostart/` addresses it). If they do, a 30-second
check keeps the Machine awake permanently and quietly converts the cost model from ~$0.65/mo
to always-on. Deploy without checks, confirm the Machine actually stops, then add checks and
confirm it *still* stops. Don't assume in either direction.

**`.dockerignore`** — `.git/`, `client/`, `test/`, `docs/`, `prompts/`,
`contract/`, `.venv/`, `.env`, `*.db`, `*.db-wal`, `*.db-shm`, `__pycache__/`, caches, logs.
This is AC5's first line of defence: the pilot's token files and its
`loom.db` live outside the repo, but `*.db` and `.env` exclusions ensure a stray copy inside
the tree can never reach a layer.

**`uv.lock`** — does not exist yet; `uv lock` generates it (uv 0.9.26 is available locally).
This is what makes AC1's "builds reproducibly" true rather than aspirational: today
`pyproject.toml` pins nothing (`fastapi`, `uvicorn`, `requests`, `pytest` are all unbounded),
so two builds a week apart can differ. Use `uv sync --locked` (not `--frozen`): `--locked`
fails the build when `uv.lock` is stale against `pyproject.toml`, which is the error worth
catching in CI.

### Implementation Phases

#### Phase 1 — Assertions first (no Fly resources, no spend)

1. Branch from `origin/main`; create the dev venv the repo documents
   (`python3 -m venv .venv && .venv/bin/pip install -e ".[server,client,dev]"`).
2. `uv lock`, committed.
3. Write `test/waves/m/test_goal_fly_packaging.py` with the Tier-1 assertions above — red.
4. Add the wave's row to `test/waves/README.md`'s table (documentation), add `"m"` to
   `GOAL_WAVE_DIRS` and raise the `>= 240` collection floor in
   `client/tests/e2e/test_blockt_e2e.py` (the parts that actually bind).

**Do not touch `test/phase_gates/*`.** An earlier draft of this plan proposed flipping
`container_parity` to `true` in a new phase config. That was a category error. `container_parity`
is not a global "this repo uses Docker" flag — it is read by exactly two frozen oracles, each
pinned to one specific phase's own config (`test/waves/f/test_goal_e8_e2e.py:251` asserts
phase-2's is `False`; `test/waves/h/test_goal_e12_e2e.py:354` asserts phase-3's is). Editing an
existing one would break those oracles, which `AGENTS.md` §12 forbids. And each phase config
belongs to one already-built phase of the build plan; a fabricated phase-5 config would be
read by nothing. Those `"no Docker"` entries are **historically true self-descriptions** of how
already-shipped phases validated themselves, not a standing prohibition — there is nothing to
supersede. If the repo wants a record that Docker now exists here, it belongs in `AGENTS.md`,
whose stated purpose is exactly project-intrinsic knowledge that should travel with the code.

**Deliverable:** a failing test that describes the image we intend to build.
**Success:** every one of AC1, AC2, AC3, AC5 has at least one named assertion; AC4 and AC6
have a named rehearsal section. No AC orphaned.

#### Phase 2 — Image and config (still no Fly resources, no spend)

5. `Dockerfile`, `docker-entrypoint.sh`, `.dockerignore`, `fly.toml`.
6. Invert the auth middleware to a strict allow-list (`server/app.py:92`), the one product
   code change. Existing server tests must stay green.
7. Drive wave `m` to green against a real local `docker build` / `docker run` with a
   bind-mounted directory standing in for the Fly volume. Docker 29.1.5 is present on this
   host, so this is verifiable end to end before a single billable resource exists.
8. `make test` and `LOOM_SKIP_LIVE=1 make contract` green (what CI runs).

**Phase 2 is the dry run, and that is deliberate.** The first time this packaging meets a
real Docker boundary must not be the time it meets the pilot's only copy of real tokens and
transcripts. Every failure mode that would otherwise surface during migration — a base image
without FTS5, a root-owned mount a non-root process cannot write, a `LOOM_DB` left unset, a
port mismatch — surfaces here against throwaway data, at zero cost.

**Deliverable:** an image proven to satisfy four of the six ACs, with nothing deployed.
**Success:** `docker run` of the built image serves `/api/health`, enforces Bearer on
everything else *including `/docs`*, keeps its DB plus WAL sidecars on the mount, and
contains no `*.db` in any layer.

#### Phase 3 — Provision and migrate (**first spend; requires explicit owner go-ahead**)

Order is not negotiable here; steps 8–12 are where data can be lost silently.

8. `fly apps create` (no machines yet); `fly volumes create loom_data -r cdg -s 1 --yes`.
9. `fly deploy --ha=false`; `fly status` / `fly machines list` to assert exactly one Machine.
10. **Stop the local server first** (`pid 429` on `127.0.0.1:8787` right now — note the
    pidfile is missing, so `server/stop.sh` will report "nothing to stop"; kill by pid, and
    confirm the port is free before continuing).
11. Produce the migration copy with `server/backup.sh` — i.e. `sqlite3 .backup`, which is
    WAL-aware — **never** `cp loom.db`. A plain copy of a WAL database taken while a writer
    is live yields a torn snapshot: the newest entries, or a just-created user row, silently
    absent.
12. `fly sftp put` the snapshot to `/data/loom.db` with the Machine started, then restart it
    and verify entry count and user count match the source.

**Deliverable:** a deployed API serving the pilot's existing data.
**Success:** entry and user counts on Fly equal the counts in the pre-migration snapshot,
asserted by an actual query, not by "it looks right".

#### Phase 4 — Prove and document

13. **AC3 for real:** destroy and recreate the Machine (not a restart, not an in-place
    redeploy) against the same volume, and read the entry back. See gap #2 — a restart proves
    nothing about the mount.
14. Measure: cold-start latency (`"suspend"` vs `"stop"`) against the client's
    `DEFAULT_TIMEOUT = (3.05, 15.0)` (`client/loom/api_client.py:14`); memory via
    `fly machine status` / `cat /sys/fs/cgroup/memory.current`; watch for exit code 137.
15. Add health checks, re-confirm the Machine still auto-stops.
16. Document: a new `docs/RUNBOOK.md` deployment section (deploy, rollback, `fly ssh console`
    for `server/admin.py`, cost check), update `§0`'s deployment table, and retire the §9
    TODO. **Amend `DESIGN.md` §6.2** (owner decision, 2026-09-13) so it describes the real
    deployment — Fly.io, platform TLS on `*.fly.dev`, synthetic data only — instead of
    Caddy/Tailscale, and update
    `docs/PROJECT-STATUS.md` §5 with it. This is a change to a read-only shared contract
    (`AGENTS.md` §13) made under an explicit owner decision, so the commit must say so.
17. Record provisioned resources now, and **set a recheck date**. AC6 compresses two
    timescales into one criterion: provisioned resources are checkable the day of the deploy,
    but a month-to-date bill carries no signal on day zero. Discharge it in two parts —
    resources recorded at deploy, first real bill recorded at a named date roughly a month
    later — otherwise the criterion is either ticked on meaningless evidence or left open
    forever. State in the same section that the worst case under `auto_start` is the
    always-on price (≈$2.17/mo), so a future reader seeing the bill move does not have to
    re-derive the bound.

### The window between this issue and #17

Repointing the clients is out of scope (#17), so the moment this issue is "done", the three
engineers are still writing to the **local** server. The Fly copy is frozen at migration time
and starts diverging immediately — "complete" would describe a state in which the hosted
database is already stale.

**Owner decision (2026-09-13): freeze local writes at migration.** The local server is stopped
for good in Phase 3 step 10 and never restarted. Capture pauses until #17 lands, and that cost
is accepted deliberately. The alternative considered and rejected was migrating twice (keep
capturing, re-migrate right before #17's cutover) — rejected because it runs the one genuinely
destructive step twice and leaves a window in which two live SQLite files both look
authoritative, which is exactly what AC4 exists to prevent.

Consequences that follow and must be honoured:

- Phase 3 step 10 stops the local server **permanently**; it is not a maintenance pause.
- The migrated Fly database is authoritative from that moment. No second migration is planned,
  so a mistake in Phase 3's copy is not something a later pass corrects.
- #17 becomes the thing that restores capture, which raises its priority from "follow-up" to
  "the pilot is degraded until this lands". Worth saying in #17.

## Alternative Approaches Considered

**Install the project in the image (`pip install .[server]`).** Rejected: it requires
`client/` in the build context purely to satisfy setuptools' `packages = ["loom","server"]`
discovery, putting the entire client — including its test fixtures — into the build context
for no runtime benefit. Copying `server/` alone is smaller, faster, and removes a whole class
of "why is the client in the server image" question.

**`auto_stop_machines = "off"` (always-on).** Rejected as the default because the owner
accepted cold start explicitly. Worth noting the cost argument is weak either way: always-on
256MB plus the volume is ≈ $2.17/mo, still inside the pilot's budget, so the real trade is latency against
nothing much. If cold start measures badly against the 3.05 s connect budget in Phase 4, this
is the cheapest remedy and should be reconsidered then.

**Seeding the volume by baking the DB into the image.** Explicitly rejected — it is the
obvious way to make "the pilot does not start empty" true, and it puts real transcripts and
real `users.token_hash` values into image layers and the Fly registry. Direct AC5 violation.
Migration goes over `fly sftp` into a running Machine, never through a layer.

**`bluegreen` deploy strategy.** Not available: it provisions parallel Machines, and a Fly
volume attaches to one Machine at a time. `rolling` (default) or `immediate` for a
single-Machine volume-backed app. *(Flagged UNVERIFIED against the primary flyctl deploy
reference — confirm before relying on it as a gate.)*

## Acceptance Criteria

Restating issue #2's six criteria with the concrete evidence each one requires.

- [ ] **AC1** — `uv.lock` committed; two clean `docker build`s produce the same dependency
      set; `ENABLE_FTS5` present in the image's `pragma compile_options`; container against
      an empty mount reaches `/api/health` 200 (proving `lifespan` schema init, including
      `search.ensure_search_schema`'s FTS5 probe at `server/search.py:122-163`, which
      **aborts startup** if FTS5 is missing — a crash-loop, not a degrade).
- [ ] **AC2** — HTTPS reachable on `*.fly.dev` with `force_https`; `/api/health` 200 with no
      header; protected route 401 with no header and 401 with a bad token; **200 with a valid
      token**; **and `/docs`, `/redoc`, `/openapi.json` 401**.
- [ ] **AC3** — an entry written before, read back after **Machine destroy + recreate**
      against the same volume, and after an application redeploy; `loom.db`, `-wal`, `-shm`
      all inside the mount; no DB file outside it.
- [ ] **AC4** — `fly status`, `fly machines list` and `fly scale show` each show exactly one
      Machine; `[mounts]` present (which by itself suppresses Fly's default two-Machine
      redundancy); deployed with `--ha=false`; deploy strategy is one that never runs two
      Machines at once.
- [ ] **AC5** — no `*.db`, `.env`, token file or Claude credential in any image layer or in
      the build context; `gitleaks` clean over the context; `git status` clean of DB files.
- [ ] **AC6** — RUNBOOK section covering deploy, rollback (`fly releases --image` +
      `fly deploy --image …`), `admin.py` over `fly ssh console`, and the cost check;
      §0's table updated with the real provisioned Machine size, volume size and region;
      month-to-date bill recorded, with the explicit statement that **Fly enforces no hard
      spend cap and offers no billing alerts**, so the budget is a sizing target verified by hand.

## Risk Analysis & Mitigation

Ranked by what it costs if missed.

**1. Torn migration copy.** Copying `~/.loom-server/loom.db` with `cp` while the local server
holds WAL writes yields a silently incomplete database — a missing entry or a missing user
row surfaces weeks later as "my token stopped working". *Mitigation:* stop the local server
first, then `server/backup.sh` (`sqlite3 .backup`, WAL-aware), then assert row counts match.
The DB is 40KB with no WAL sidecars currently present, so the cost of doing this correctly is
seconds.

**2. AC3 green on a misconfigured mount.** If `LOOM_DB` were left at its CWD-relative default
(`./loom.db`, `server/db.py:27`), the app still boots, still answers health, and an entry
still "survives" a restart and an in-place redeploy — on the container's ephemeral disk,
having never touched the volume. *Mitigation:* AC3's evidence is Machine **destroy and
recreate**, plus an explicit assertion that no `loom.db` exists outside the mount.

**3. AC2 green on a failed migration.** `apply_schema` only runs when `users` is absent, so a
fresh unmigrated volume boots with zero users and 401s *everything* — including valid
historical tokens. AC2 as worded ("reject missing/invalid tokens") is satisfied by that
broken deployment. *Mitigation:* the positive case is part of AC2 above — a valid token must
return 200.

**4. Divergent `users` tables.** Running `server/admin.py` locally against a stale copy of
the DB creates a second, divergent set of `token_hash` values with nothing to surface the
divergence. *Mitigation:* document `fly ssh console` as the **only** way to run `admin.py`
against the pilot. Note `fly ssh console` on an auto-stopped Machine is **UNVERIFIED** —
verify whether it wakes the Machine, and if not, document `fly machine start` first.

**5. Split brain with the local server.** The local server is running *right now* (pid 429).
If it keeps running after cutover, entries divide across two independent SQLite files.
*Mitigation:* Phase 3 step 10 stops it before migration; issue #17 owns the client-side half.

**6. Public schema disclosure.** `/docs`, `/redoc` and `/openapi.json` are unauthenticated
today and become internet-reachable the moment this deploys. Not a data leak — no route
returns entries without a token — but a free recon surface that did not exist on loopback.
*Mitigation:* the middleware inversion in Phase 2 step 6, pinned by an AC2 assertion so it
cannot regress. **Must be done before the first deploy, not after.**

**7. FTS5 absence becomes a cost problem, not just a functional one.**
`server/search.py:122-163` aborts startup when FTS5 is missing. Combined with
`auto_start_machines` and a restart-on-failure policy, a wrong base image does not fail
quietly — it produces a restart storm that burns compute against the pilot's budget while serving
nothing. *Mitigation:* the Phase 2 wave proves FTS5 in the image before any Machine exists;
`python:3.12-slim` (Debian bookworm libsqlite3) ships it, Alpine/musl is rejected partly for
this reason.

**8. Health checks defeating auto-stop.** Undocumented by Fly, verified today. If probes
count as traffic, the Machine never stops and the cost model silently changes.
*Mitigation:* deploy without checks, confirm stopping, add checks, re-confirm.

**9. 256MB too small.** Realistic idle RSS for single-worker FastAPI + uvicorn + sqlite3 is
~60-100MB, so 256MB should hold, but it is not generous. *Mitigation:* watch for exit code
137; `fly scale memory` is a one-command fix. The owner's two recorded decisions differed here
(2026-09-12 said 512MB, 2026-09-13 said 256MB); **confirmed 2026-09-13 as 256MB**, with the
trade understood — under `auto_stop` Fly bills run time, not provisioned RAM, so the smaller
size saves almost nothing and the reason to hold it is discipline about measuring real usage,
not cost.

**10. Rollback restores the image, not the data.** Fly's own wording: *"you're rolling back
the VM image, not the database"*, and config does not roll back either — an old image runs
against the current `fly.toml`, env and secrets. *Mitigation:* say this explicitly in the
RUNBOOK rollback section, because the natural assumption is the opposite. Data rollback is
issue #8's territory.

## Dependencies

- **Blocks #17** (repoint clients off localhost) — and #17's "tokens survive" criterion
  depends on the migration in Phase 3 actually carrying `users.token_hash`.
- **Semantic prerequisite for #8** (off-volume backups and proven restore).
- **Owner go-ahead required before Phase 3.** Phases 1-2 create no billable resources;
  Phase 3 creates an app, a Machine and a volume.
- `flyctl` is authenticated to the intended Fly org, and `cdg` is available (verified).
  The pilot's budget is org-wide, not app-scoped: anything else the org already bills
  counts against it. **Precondition on Phase 3:** the owner reads the current
  month-to-date org bill before provisioning, so the pilot's budget starts from the real
  baseline rather than from zero.

## References

**Internal**
- `server/db.py:27` (`LOOM_DB`), `:34-39` (WAL, foreign_keys, busy_timeout 5000ms)
- `server/search.py:122-163` (`_require_fts5`, hard startup abort)
- `server/app.py:25-28` (lifespan schema init), `:89-98` (Bearer middleware), `:230` (health)
- `server/admin.py` (user administration CLI), `server/backup.sh:17` (WAL-safe `.backup`)
- `pyproject.toml:19-24` (`packages = ["loom","server"]`, package-dir mapping)
- `client/loom/api_client.py:14` (`DEFAULT_TIMEOUT = (3.05, 15.0)`)
- `AGENTS.md` §7, §9, §10, §12, §13 · `test/waves/README.md` (wave convention)
- `docs/RUNBOOK.md:16` (current deployment), `:657`, `:691` (rehearsal-record format), §9 (TODO)
- `DESIGN.md` §6.2 (`:303`), `docs/PROJECT-STATUS.md:43`, §5

**External**
- fly.io/docs/reference/configuration/ — `fly.toml` schema
- fly.io/docs/launch/autostop-autostart/ — `"off"`/`"stop"`/`"suspend"`, `min_machines_running`
- fly.io/docs/reference/app-availability/ — a mounted volume limits the process group to one Machine
- fly.io/docs/reference/suspend-resume/ — suspend ~hundreds of ms vs stop ~2+ s; ≤2GB
- fly.io/docs/volumes/overview/ — region-pinned, no replication, cannot shrink
- fly.io/docs/blueprints/rollback-guide/ — rollback is image-only
- docs.astral.sh/uv/guides/integration/docker/ — `uv sync --locked`, builder-stage-only uv

**Open / unverified**
- Does Fly Proxy hold a request while a Machine boots, and with what timeout? *(undocumented)*
- Do health-check probes reset the auto-stop idle timer? *(undocumented — Phase 4 step 15)*
- Does `fly ssh console` wake a stopped Machine? *(undocumented — Phase 4)*
- Is `bluegreen` formally incompatible with volumes? *(believed yes, not quoted from the primary reference)*
