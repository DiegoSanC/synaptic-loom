# Synaptic Loom — central server

HTTP API (FastAPI) + SQLite (stdlib `sqlite3`, no ORM) + per-developer Bearer auth.
Normative contracts in `PLAN.md §3`; schema in `DESIGN.md §6.1` / `server/schema.sql`.

## Components

| File | Role |
|---|---|
| `app.py` | API routes + Bearer auth middleware + uniform error shape |
| `rate_limit.py` | Bounded in-process sliding windows for failed auth and public health traffic |
| `client_ip.py` | `LOOM_CLIENT_IP_SOURCE`: which address keys those windows and the audit line (D17) |
| `db.py` | `sqlite3` connection (WAL, `foreign_keys=ON`, `busy_timeout`), idempotent schema, `hash_token` / `iso_now` helpers |
| `normalize.py` | Normalization of the git remote into a canonical identity (server-side authority) |
| `admin.py` | Admin CLI: `invite` / `listdevices` / `revokedevice` (machines) · `listusers` / `deactivate` (people) · `adduser` (break-glass, see below) |
| `schema.sql` | Literal schema (never rewritten) |

## Configuration

- **DB:** `LOOM_DB` environment variable (default `./loom.db`). Both `app.py` and
  `admin.py` read the same variable.
- The schema is applied at startup only if it does not exist yet (idempotent).
- **Client address:** `LOOM_CLIENT_IP_SOURCE` (D17), read once at startup — `peer`
  (default: the TCP peer; behind a reverse proxy, set uvicorn's `FORWARDED_ALLOW_IPS` to
  the proxy's address, never `*`), `fly-client-ip` (`fly.toml` sets it) or
  `header:<name>`. Any other value and the server refuses to start; the startup log line
  `client_ip_source mode=…` names what it resolved.

## Getting started

```bash
# deps already declared in pyproject: fastapi, uvicorn (extra "server"); pytest, httpx (extra "dev")
python3 -m venv .venv && .venv/bin/pip install -e ".[server,dev]"
export LOOM_DB=./loom.db

# 1. invite a person and their first machine — prints a single-use CODE, never a token
.venv/bin/python -m server.admin invite --name dev2 --email dev2@example.test  # --ttl 24h by default
.venv/bin/python -m server.admin invite --email dev2@example.test              # their second machine

# 2. start it
.venv/bin/python -m uvicorn server.app:app --port 8765     # or `make serve` (with --reload)

# 3. on the machine being enrolled — NOT here
~/.loom/venv/bin/loom enroll --server-url=http://127.0.0.1:8765   # reads the code at a hidden prompt
```

Send the enrolment code over the team's channel: it is single-use, dies with its TTL, and
is worthless once redeemed. The machine mints its own **device token** from it — 90 days,
self-renewing while that machine keeps running passes — and the device token itself never
travels (issue #20).

Day to day:

```bash
.venv/bin/python -m server.admin listusers
.venv/bin/python -m server.admin listdevices --email dev2@example.test   # --email optional; never prints a token
.venv/bin/python -m server.admin revokedevice --id 3                     # ONE machine
.venv/bin/python -m server.admin deactivate --email dev2@example.test    # the person: every token of theirs
```

> **`adduser` is break-glass.** It still works and still prints a token once, but that
> token is **permanent**: it never expires and never rotates, which is the thing the
> enrolment flow exists to stop handing around. `loom doctor` flags a client holding one
> and tells its owner to re-enrol. Reach for it only when enrolment itself is unavailable.
>
> ```bash
> .venv/bin/python -m server.admin adduser --name dev2 --email dev2@example.test
> ```

**On the hosted deployment every one of these runs inside the Fly Machine**, against
`/data/loom.db` — not from a laptop. `docs/RUNBOOK.md` §4, with §2 → "Administering
users", is the authority there; the block above is the local-`LOOM_DB` form.

Auth: `Authorization: Bearer <token>` on every route except `/api/health`.
`sha256(token)` is matched against `users.token_hash` first and then against
`device_tokens.token_hash`, so a break-glass token and a device token authenticate the
same caller by the same rule. Missing, empty or unknown token → `401`; a device token
that has been revoked or has expired → `401` (`revoked_token` / `expired_token`, each
audited by name); a valid token whose user is deactivated → `403`. Failed authentication is audited without credentials
and limited per client IP (`LOOM_CLIENT_IP_SOURCE`); only failures count, and a valid
credential is accepted even while its address is throttled. `/api/health` has a separate,
higher limit. TLS is terminated in front of the server — by Fly, or by Caddy in the
Compose topology (`deploy/compose/`) — and every response carries HSTS. The exact policy and
single-process constraint live in `DESIGN.md §6.2`; operations live in `docs/RUNBOOK.md §2`.

Errors: always JSON `{"error": "<code>", "detail": "..."}`.

## Endpoints

| Method | Path | Codes |
|---|---|---|
| GET | `/api/health` | 200 (no auth) |
| POST | `/api/projects` | 201 · 409 `already_registered` · 422 |
| GET | `/api/projects?remote=<url>` | 200 · 404 `project_not_found` (the server re-normalizes) |
| GET | `/api/projects` | 200 (listing, cache for the client gate) |
| POST | `/api/entries` | 201 · 409 `duplicate_entry` · 404 `project_not_found` · 422 |
| GET | `/api/projects/{id}/entries?since=<ISO>&limit=30` | 200 · 404 (`created_at` DESC order, `has_more`). Request header `X-Loom-Source: skill\|cli` records one `recall_events` row; no header, no row |

`created_at` and `user_id` are ALWAYS server-side (any client value is ignored).

## Smoke curls (PLAN.md §3)

```bash
T="<device token>"              # what `loom enroll` minted; it lives in ~/.loom/config.json
H="http://127.0.0.1:8765"        # in prod: https://HOST

# health (no auth)
curl -sf $H/api/health
# -> {"status":"ok","version":"0.1.0"}

# listing (cache for the gate)
curl -sf -H "Authorization: Bearer $T" $H/api/projects
# -> {"projects":[...]}

# register by remote (name optional, derived from the repo)
curl -sf -H "Authorization: Bearer $T" -X POST $H/api/projects \
     -H 'Content-Type: application/json' \
     -d '{"remote_url":"git@github.com:org/b.git"}'
# 201 -> {"id":1,"canonical_remote":"github.com/org/b","name":"b"}

# register the SAME remote again -> 409 with who and when
curl -s -H "Authorization: Bearer $T" -X POST $H/api/projects \
     -H 'Content-Type: application/json' \
     -d '{"remote_url":"https://github.com/ORG/B.git"}'
# 409 -> {"error":"already_registered",
#         "project":{"id":1,"canonical_remote":"github.com/org/b","name":"b"},
#         "registered_by":"dev2","registered_at":"2026-07-25T09:00:00Z"}

# resolve by remote (the raw URL received is re-normalized)
curl -sf -H "Authorization: Bearer $T" "$H/api/projects?remote=git@github.com:Org/B.git"
# 200 -> {"project":{"id":1,"canonical_remote":"github.com/org/b",...}}

# upload an entry (idempotent content_hash)
curl -sf -H "Authorization: Bearer $T" -X POST $H/api/entries \
     -H 'Content-Type: application/json' \
     -d '{"project_remote":"github.com/org/b","session_id":"s1",
          "covered_from":"uuid-a","covered_to":"uuid-b",
          "content_md":"## Contexto\n...","content_hash":"sha256-hex",
          "model":"sonnet","prompt_version":"v1"}'
# 201 -> {"id":42}   (re-POSTing the same content_hash -> 409 duplicate_entry)

# project logbook (DESC order, paginable)
curl -sf -H "Authorization: Bearer $T" "$H/api/projects/1/entries?limit=30"
# 200 -> {"entries":[{"id":42,"author":"dev2",...}],"has_more":false}
```

## Registration by path (fallback without a remote)

Repos with no git remote are registered by path (`DESIGN.md §3`, this is the exception):

```bash
curl -sf -H "Authorization: Bearer $T" -X POST $H/api/projects \
     -H 'Content-Type: application/json' \
     -d '{"path":"/home/dev/x","hostname":"wsl-dev2"}'
# 201 -> {"id":7,"canonical_remote":null,"name":"x"}   (creates projects + project_paths)
```

Duplicates are detected through `UNIQUE(user_id, hostname, path)` and also answer
`409 already_registered`. Merging paths into one logical project is phase 3 (web UI).

## Tests

```bash
make test                       # server/tests + client/tests
.venv/bin/python -m pytest server/tests -q
```

They cover: auth 401/403, health without auth, registration 201 / 409 (with who + when) / 422,
`GET ?remote=` 200/404, entries 201 / 409 duplicate hash / 404 nonexistent project / 422,
entry listing (DESC order, `limit`, `since`, `has_more`), registration by path and its 409,
the normalization table, `user_id`/`created_at` forced server-side, the `admin.py` CLI,
concurrent writes (WAL, no corruption and no exception), and recall telemetry (one
`recall_events` row per read that declares its source; none without the header or on a
401/404/422; no credential in any row; a failed write never costs the reader).
