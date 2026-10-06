# Synaptic Loom

Centralized knowledge base of Claude Code sessions: it automatically captures the
decisions (and the reasoning behind them) from each developer's sessions and exposes them
as an advisory "logbook" for whoever reviews or inherits the project.

## Repo structure

```
server/            # central FastAPI API + SQLite + token auth (T2)
  app.py           # routes + Bearer auth middleware
  db.py            # sqlite connection (WAL) + idempotent schema.sql
  schema.sql       # literal schema (DESIGN.md §6.1)
  admin.py         # CLI: adduser / deactivate / listusers
  tests/
client/            # per-developer-machine components
  loom/            # installable package: cli, processor, transcript_adapter,
                   # normalize, secret_scan, api_client, config (T3-T6)
  hooks/           # SessionEnd hook (T5)
  skills/          # Claude Code skills: loom, loom-register (T6)
  install.sh       # installer: venv + hook + ~/.loom + config (T5)
  tests/           # unit/ integration/ contract/
prompts/           # summarizer_v1.md (versioned prompt, DESIGN.md §5)
contract/          # T0 output: real fixtures, SCHEMA.md, FLAGS.md, verify.py
                   # + ATTRIBUTION.md: how a session is assigned to a project
```

Sessions that run in an **ephemeral git worktree** (the usual agent-harness setup) keep
their project attribution: the SessionEnd hook captures the repo's remote while the
worktree still exists, and the processor falls back to it only when the directory is
already gone. A live remote always wins, and an identity that cannot be established is
dropped rather than guessed — `contract/ATTRIBUTION.md` is the authority.

## make targets

- `make test` — pytest over `server/tests` and `client/tests`.
- `make contract` — runs `contract/verify.py` (requires a live `claude` CLI).
- `make serve` — `uvicorn server.app:app --reload`.
- `make install` — `bash client/install.sh`.

## Continuous integration

CI (`.github/workflows/ci.yml`) runs the full test suite (`make test`) plus the static
contract assertions (`LOOM_SKIP_LIVE=1 make contract`) on every PR and push to `main`.
The full `make contract` — which invokes the real `claude` CLI — stays a manual check,
reserved for changes to session capture, transcript adaptation, or Claude integration.

## Getting started (dev)

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[server,client,dev]"
make test
```

## Documentation

- `DESIGN.md` — technical design (architecture, security, schema, invariants).
- `PLAN.md` — phase-1 implementation plan (tasks, ACs, API contracts).
- `contract/ATTRIBUTION.md` — the project-attribution authority rule (issue #4).
- `docs/RUNBOOK.md` — pilot operations, including how to read the attribution
  diagnostics in `~/.loom/log/processor.log`.

Issue, PR and commit references cited in the docs and code (`#N`, e.g. "issue #4", or a
commit hash) refer to the original development repository; they do not resolve in this one.
