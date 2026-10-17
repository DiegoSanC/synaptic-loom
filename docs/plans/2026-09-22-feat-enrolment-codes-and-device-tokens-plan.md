---
title: "feat: Single-use enrolment codes and expiring, silently renewed device tokens"
type: feat
date: 2026-09-22
complexity: high
github_issue: 20
closes_gaps_of: 19  # gaps 5 (tokens never expire) and 8 (token in shell history)
baseline_in_issue: b2c9e3c
branch_from: main (b2c9e3c)
branch: feat/enrolment-device-tokens
---

# feat: Single-use enrolment codes and expiring, silently renewed device tokens

## Overview

Issue #20 (decision of 2026-09-22 recorded in the pilot's private operations notes). The design is the issue's own, with its four open
owner decisions **taken on 2026-09-22 by the maintainer**:

| Decision | Value | Why it matters here |
|---|---|---|
| Token scope | **one device token per machine** | A maintainer with two machines (one with a legacy `adduser` token, one with nothing) is the common case. A per-person token would kill the first machine's client the moment the second enrolled. Consequence: a `device_tokens` table (many per user), and an admin surface to list and revoke one machine. |
| Device token lifetime | **90 days** | Renewal fires at 75 % of life (≈ day 67). A machine idle for more than 90 days re-enrols. |
| Enrolment code TTL | **24 h** (default of `invite --ttl`) | Configurable per invitation; the e2e uses seconds. |
| Renewal | **silent** | The client renews inside a pass it was already running. No human, no prompt, no logout. `loom doctor` shows the days left. |

Two things the issue fixes that this plan does not revisit: `adduser` stays byte-for-byte
as it is (five frozen waves parse its stdout; it is the break-glass path), and the
enrolment code is a high-entropy random token stored as `sha256`, so no new dependency.

## What exists already (verified on `main` at `b2c9e3c`)

The issue's two server-side prerequisites landed in PR #30 and are asserted in
`server/tests/test_server.py`:

- **#19 gap 1** — `AUTH_FAILURES`, a per-`Fly-Client-IP` sliding window (10 failures /
  60 s → `429` + `Retry-After`), checked in the middleware before any credential is read.
- **#19 gap 2** — `SECURITY_LOG` (`uvicorn.error.loom.security`) receives one structured
  line per rejected authentication: outcome, UTC timestamp, validated client IP, never a
  credential.

`loom init` already reads the token through `getpass` (gap 8's code half). The client-side
prerequisite — *a 401 on an unattended path must become loud* — does **not** exist:
`processor._process_marker` files a 401 under a generic `error` disposition and keeps the
marker; nothing else notices.

## Design

### Server

**Schema** — additive, via a new `server/auth.py: ensure_auth_schema(conn)` called from
`db.init_db`, exactly like `links.ensure_links_schema`. `schema.sql` is not touched.

```sql
CREATE TABLE IF NOT EXISTS enrolment_codes (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  code_hash TEXT UNIQUE NOT NULL,          -- sha256 of the code; the code is printed once
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  redeemed_at TEXT, device_token_id INTEGER REFERENCES device_tokens(id)
);
CREATE TABLE IF NOT EXISTS device_tokens (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  token_hash TEXT UNIQUE NOT NULL,         -- sha256; the token exists only on the device
  device_name TEXT NOT NULL,
  issued_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  revoked_at TEXT, replaced_by INTEGER REFERENCES device_tokens(id)
);
CREATE INDEX IF NOT EXISTS idx_device_tokens_user ON device_tokens(user_id);
```

**Credential lookup** (middleware) tries `users.token_hash` first (legacy, permanent,
break-glass), then `device_tokens`. A device token that is revoked or past `expires_at`
is rejected `401` with its own audit outcome (`revoked_token` / `expired_token`), so an
operator can tell an expiry from a typo in Fly Logs. The middleware records the matched
device token id on `request.state` for the renewal route.

**`POST /api/enroll`** — anonymous, the second entry of `PUBLIC_PATHS`. Body
`{"code": str, "device_name": str}`. Redemption is one `UPDATE … WHERE code_hash = ? AND
redeemed_at IS NULL AND expires_at > now` inside one transaction, so two concurrent
redemptions cannot both win. Success `201 {"token", "issued_at", "expires_at", "user":
{"id","name"}}`. Failure is `401 invalid_code` for unknown, already-redeemed **and**
expired codes alike (an anonymous caller learns nothing), **counts against
`AUTH_FAILURES`** (a failed redemption is a failed authentication) and is audited as
`enrol_invalid_code`; a success is audited as `enrol_redeemed user_id=<id>
device_token_id=<id>` — the trace the issue asks for, with no secret in it.

**`POST /api/device-tokens/renew`** — authenticated with a device token only (a legacy
token gets `403 not_a_device_token`: there is nothing to renew). Mints a successor with a
fresh 90-day life, links `replaced_by`, and **leaves the old token valid until its own
`expires_at`**. That is deliberate: if the client crashes between receiving the successor
and persisting it, the machine keeps capturing on the old token and simply renews again on
the next pass. The window in which two tokens are valid is bounded by the old expiry.

**Lifetimes** — `LOOM_DEVICE_TOKEN_TTL_S` (default `7776000`, 90 days) is read by the
server so the e2e can prove renewal across a *real* expiry in seconds. The enrolment code
TTL is per invitation (`invite --ttl`, default `24h`; accepts `s`/`m`/`h`/`d`).

**`server/admin.py`** gains, additively:

- `invite --email <e> [--name <n>] [--ttl 24h]` — for an existing user, or creates one
  when `--name` is given. A user created here gets a **discarded** random `token_hash`
  (the column is `NOT NULL`; nobody holds the preimage), so the only way onto the API is
  enrolment. Prints the code once, like `adduser`.
- `listdevices [--email <e>]` and `revokedevice --id <n>` — the admin surface the
  per-machine decision requires; without them "revoke a stolen laptop" is not possible.

### Client

**`config.json`** gains two optional keys, `token_issued_at` and `token_expires_at`
(ISO-8601 Z). Absent keys mean a legacy permanent token; every existing config keeps
loading unchanged.

**`loom enroll [--server-url URL] [--device-name NAME]`** — prompts for the URL if absent,
reads the code via `getpass` (never echoed, never an argv), validates `/api/health`,
redeems, and writes `config.json` (0600) **merging** into an existing config so
`cache_ttl_s`, `summarizer`, `sweep` and `scanner` survive. `device_name` defaults to the
hostname (since 2026-10-17 the short host name — its first label, `device` if empty — so the
network domain is not sent; existing enrolments keep their name). Creates the work
directories like `init`.

**Silent renewal** — `loom/auth.py: renew_if_due(cfg, api)` runs at the top of
`processor.run_once`, inside the lock, before the registry refresh: the pass was already
going to talk to the server. Due when the remaining life is below 25 % of the issued
lifetime. On `201` it rewrites `config.json` atomically (tmp + `os.replace`, 0600) and
swaps the token on the live `ApiClient`. A network error or 5xx is logged and the pass
continues on the current token; a `401` goes to the loud path below. Both unattended
entry points (`loom process --quiet` from the trigger, `python -m loom.processor --quiet`
from the hook's kick) call `run_once`, so both renew.

**Loud 401** — a new disposition `kept_auth_rejected` replaces the generic `error` for
`401`/`403` on `POST /api/entries`, and the same outcome is raised when the registry
refresh or the renewal is rejected. It writes `<LOOM_HOME>/auth_rejected.json`
(`{"status", "at", "detail"}`) atomically; the next authenticated `2xx` in a pass removes
it. Three surfaces read the file without anyone "running doctor by chance":

- `loom status` prints a `✗ AUTH REJECTED since <ts> … run 'loom enroll'` block first.
- `loom doctor` gains a `fail` row while the file exists.
- `loom log` (and therefore the `/loom` skill inside Claude Code, the one surface a
  developer sees daily) appends the same hint to its existing `Server error: HTTP 401`
  line.

**`loom doctor`** gains one credential row: `ok` with days remaining (and the renewal day)
for a device token, `warn` for a legacy permanent token (with the `loom enroll` hint),
`fail` when expired.

### Docs and contracts

- `DESIGN.md` §6.2 — an explicit, dated decision paragraph (rule 13), same mechanism as
  the hosting decision of 2026-09-13.
- `PLAN.md` §3 and §T5 — the second public route, the two new routes, the two config keys.
- `docs/RUNBOOK.md` §2 (admin inside the Machine: `invite`, `listdevices`, `revokedevice`),
  §4 (adding a user = invite), §5 (`loom enroll` is the default onboarding; `init` is the
  break-glass form).

## Decisions raised, not improvised

1. **No new frozen goal wave.** The acceptance criteria are asserted in the suites that
   already own each boundary — `server/tests` (TestClient), `client/tests/integration`
   (mock server) and `client/tests/e2e` (real uvicorn, real CLI subprocesses) — and the
   phase gate drives all of them. A wave under `test/waves` is the frozen-oracle
   doctrine (independent author, frozen before implementation) and this work has one
   author; registering it there would claim an independence it does not have.
2. **Old token survives renewal until its own expiry** (see above). A stricter
   "revoke-on-renew" trades a crash window for a dead machine; the pilot chooses the
   bounded overlap.
3. **`invite` may create the user.** The alternative — `adduser` first — prints a
   permanent token to the operator, which is what this feature exists to stop.

## Acceptance criteria → assertions

| AC (issue #20) | Where asserted |
|---|---|
| Code is single-use and expires; redeeming twice fails, redeeming an expired one fails | `server/tests/test_enrol.py` |
| Device token never transmitted to the operator; never in shell history | `test_enrol.py` (invite stdout carries the code, never a token) · `client/tests/integration/test_cli_enroll.py` (no `--code` flag exists; the code is read by `getpass`) |
| `loom enroll` prompts without echoing | `test_cli_enroll.py` (patches `getpass.getpass`; `input()` is never called for the code) |
| Expired device token rejected by the middleware | `test_enrol.py` (real expiry: `LOOM_DEVICE_TOKEN_TTL_S=1`, then `time.sleep`) |
| Silent renewal keeps both unattended paths working across an actual expiry | `client/tests/e2e/test_enrolment_e2e.py` (real uvicorn with a 6 s token life; one pass through `loom process --quiet`, one through `python -m loom.processor --quiet`; an entry lands after the original expiry) |
| A 401 on an unattended path is visible without running doctor by chance | `client/tests/integration/test_processor_auth.py` (file written, `kept_auth_rejected`) · `test_cli_status.py` · `test_cli_log.py` (hint on 401) |
| `/api/enroll` rate-limited and redemption attempts logged | `test_enrol.py` (11th bad code from one IP → 429; `caplog` carries `enrol_invalid_code` and `enrol_redeemed`) |
| `adduser` unchanged, five frozen waves pass | `test_admin.py` untouched · `make test` (the gate sweeps `test/waves`) |
| `DESIGN.md` §6.2 updated under a dated decision | this PR |
| `RUNBOOK.md` §5 documents the new onboarding as default | this PR |

## Slices (TDD, each red → green)

1. Server schema + `invite` + `POST /api/enroll` (single-use, expiry, rate limit, audit).
2. Middleware accepts device tokens; rejects expired/revoked; `renew`; `listdevices` /
   `revokedevice`.
3. Client config keys + `loom enroll` + `loom doctor` credential row.
4. Client silent renewal in `run_once` + loud 401 (`kept_auth_rejected`, status, doctor,
   log hint).
5. E2E across an actual expiry, both unattended entry points.
6. `DESIGN.md` / `PLAN.md` / `RUNBOOK.md`; full suite + contract; self-review.
