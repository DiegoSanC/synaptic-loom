---
title: "feat: Invitation links and revocation fixes now; accounts v2 (roles, writable UI, self-service machines) next"
type: feat
date: 2026-10-19
complexity: high
baseline: main (a051005)
branch_from: main (a051005)
branches:
  phase_1: feat/invite-links-and-revocation-fixes
  phase_2: feat/accounts-v2
decisions: D19 (phase 1, proposed), D20 (phase 2, proposed) — numbers provisional
status: approved for phase 1 (owner accepted every recommended default, 2026-10-19); revised after a four-reviewer, four-round review (security, architecture, simplicity, data)
---

# feat: Invitation links now; accounts v2 next

## Owner decisions (everything else below is a default you may override)

**Taken 2026-10-19:** the owner accepted the recommended default of every row below.

| # | Decision | Recommended default |
|---|---|---|
| 0 | **How the CLI gets its credential.** (a) a single-use **setup code** (10 min) issued from the web, redeemed by `loom enroll` on the machine, which mints the device token there; or (b) a **long-lived API key** created in the web and pasted into the CLI, OpenAI-style. Both start in the dashboard and neither needs to pass through a chat. The real difference is **how long a leaked copy stays valid**: minutes and one use for (a), the key's whole life for (b). (b) reopens #20 ("the secret that persists never travels"); (a) keeps it. | **(a) setup code** |
| 1 | Go for phase 1 as specified below. | Yes |
| 2 | TTL of an enrolment code issued as an invitation link. | Keep 24 h (`--ttl` already overrides) |
| 3 | **Token-usage tracking**: reuse detection on renew **and** B1c ("an old token dies once its successor has been used"). Both need the same usage column and the same client retry, and B1c reverses decision 2 of the 2026-09-22 enrolment plan (DESIGN §6.2, `server/tests/test_enrol.py:314-317`). | **Defer.** Phase 1 does not stop a stolen token from renewing, but it makes every renewal revocable as one chain and makes a thief's renewal visible as a loud 401 on the real machine |
| 4 | Phase 2 shape: **2a** (roles + writable UI reached through `loom ui`, no passwords) first, **2b** (passwords) only if web access without an enrolled machine is needed; or straight to 2b. | **2a first** |
| 5 | D14 amendment wording (the UI becomes writable for the actions listed in phase 2). | As drafted in phase 2 |
| 6 | Roles: global `admin` / `member`; only admins invite. | Yes |

---

## Overview

Onboarding today works and is secure, but it is operator-heavy and hard to explain to a team
that has never seen Loom:

1. The operator needs a shell on the server (`fly ssh console -u app` / `docker compose exec
   -u app loom`) to run `python -m server.admin invite`, once **per person per machine**.
2. Three things travel by hand: a 43-character enrolment code, the server URL and the repo.
3. Nobody but the operator can see or revoke a machine; there are no roles; there is no
   self-service for a second machine.

The owner asked for the shape most teams expect: **an administrator invites people from the
UI, people sign in, and they set up the CLI themselves**, the way an OpenAI or GitHub key is
created in a dashboard and pasted into a client. Decision 0 records the one place where this
plan recommends something different from that literal request, and why.

| Phase | What | Reopens a decision? | Effort |
|---|---|---|---|
| **1 — Invitation links + revocation fixes (D19)** | `invite` also prints a link; `loom enroll` accepts it; renewal chains revoked as a whole; renew re-issues instead of forking; `deactivate` does what it says; B2 as a cuttable last slice | No owner decision; it **does** change the API contract (listed below) | ≈ 3–4 days of worker time |
| **2a — Accounts v2 without passwords (D20)** | Roles; the `loom ui` session becomes writable; *Admin → Users* (invite = phase-1 link); *My machines* (setup codes, revoke) | Yes: D14, §6.3 | M (≈ 1 week) |
| **2b — Passwords (D20, optional)** | Web sign-in, password reset links, throttling | Yes: D14, D17, D8/D18 note | L |

Phase 1 is useful alone. 2a builds on it: an admin's "Invite" issues exactly the phase-1 link,
and a setup code is redeemed by the same `loom enroll`. 2b is a strict superset of 2a, so
starting with 2a loses nothing.

---

## What exists already (verified on `main` at `a051005`)

| Piece | Where | Notes |
|---|---|---|
| Users | `server/schema.sql` (`users`: name, email UNIQUE, token_hash, active) | email unique **case-sensitively**, unverified |
| Enrolment codes | `server/auth.py` (`enrolment_codes`, `create_invitation`, `redeem`) | `secrets.token_urlsafe(32)`, sha256 at rest, single-use under `BEGIN IMMEDIATE`, 24 h; `redeem` refuses codes of inactive users (`auth.py:194`) |
| Device tokens | `server/auth.py` (`device_tokens`: 90 days, `renew`, `revoke`) | one per machine; silent renewal at 75 % of life; the predecessor stays valid until its own expiry (DESIGN §6.2, pinned by `test_enrol.py:314-317`) |
| Operator CLI | `server/admin.py` (`invite`, `adduser`, `listusers`, `listdevices`, `revokedevice`, `deactivate`) | needs a shell on the server; has no logging (its output goes to the operator's terminal) |
| Client | `client/loom/cli.py` (`cmd_enroll` reads the code with `getpass`; `--server-url` or a prompt) | `config.json` stores the token and its two dates — **not** its id (the server strips the id from `/api/enroll` and `/renew` responses) |
| Web UI | `server/ui_auth.py`, `server/ui_pages.py` (D14) | `loom ui` → 120 s single-use code → 12 h `loom_ui` cookie (`HttpOnly; Secure; SameSite=Strict; Path=/ui`), **GET only** |
| Public paths | `server/app.py:143` `PUBLIC_PATHS` (exact-path allow-list) | public paths get the health rate window, except the two redemption paths (`/api/enroll`, `/ui/session`), which count against the auth-failure window |
| Security headers | `server/app.py` `security_headers_middleware` | applies `ui_pages.SECURITY_HEADERS` (already `default-src 'none'`, `no-referrer`, `no-store`) to every `/ui` path, overwriting any route-set header |
| Rate limiting | `server/app.py`, `server/rate_limit.py` (D17) | per client IP, failures only, **in process** (reset at start; Fly stops the idle machine: `auto_stop_machines = "stop"`, `min_machines_running = 0`) |

**Bugs, fixed in phase 1:**

- **B1 — revocation and renewal chains.**
  - `auth.revoke` marks one row, so a predecessor keeps authenticating until its own expiry.
  - `renew` checks nothing inside its transaction and **overwrites** `replaced_by`
    (`auth.py:258`). Renewing the same token twice therefore forks the chain and leaves the
    first successor **orphaned**: live for 90 days and unreachable from the chain. A lost
    renew response produces exactly this today, and so does a thief renewing a stolen token.
- **B2 — re-enrolling a machine does not revoke its previous token**, which stays valid for
  up to 90 days.
- **B3 — `deactivate` lies.** It prints "their token is now revoked" (`admin.py:57`), but it only
  sets `active = 0`. Device tokens are blocked only indirectly, and they come back if the user is
  ever reactivated.

---

## Phase 1 — Invitation links and revocation fixes (D19)

### Server

**B1a — revoke the whole chain.** `revoke` revokes every token of the chain in one
`WITH RECURSIVE` update over `replaced_by`, inside `BEGIN IMMEDIATE`. The CTE computes the
**connected component**: from every row already found it follows `replaced_by` both ways. It
is not two one-way walks from the starting id.
Add `CREATE INDEX IF NOT EXISTS … ON device_tokens(replaced_by)`. **No new column.** This is
sound because of B1b: once renew never forks, the live tokens of a machine form a single line.

**B1b — renew re-issues and never forks.** In one `BEGIN IMMEDIATE`, `renew`:
- re-checks that the caller is not revoked or expired and that its user is active (this closes
  the revoke/renew race);
- if the caller already has a successor, revokes **every token after the caller**, walking
  forward along `replaced_by`. It then points the **last** of them (the one whose
  `replaced_by` is NULL) at the new token and mints it. The caller **keeps pointing at its first
  successor**, and every other pointer is also left alone. The chain therefore stays a strict
  line, caller → S1 → … → S_last → new token and a walk from any id
  still reaches the live token. With one successor this reduces to "revoke the successor and
  append". With several, which only a thief or a copied `config.json` can produce, it is what
  keeps a live token from being cut off the chain.

The predecessor stays valid until its own expiry, as today, so the 2026-09-22 decision and its
pinned test are unchanged. **Known property:** a thief who renews a stolen old token kills the
legitimate successor, so the real machine gets a **loud 401**. Revoking the machine then
revokes the whole chain, the thief's token included. Reuse detection is owner decision 3.

**Orphans already in a database.** At server startup (`lifespan`, not `init_db`, which also runs
on every `admin.py` command and would pollute `invite`'s stdout), the server logs a **count** of live device
tokens that no `replaced_by` and no `enrolment_codes.device_token_id` reference. It never
revokes at boot. A new `admin.py revoke-orphans` revokes them in one transaction and prints the
ids it revoked. RUNBOOK's upgrade step makes it **required**: run a read-only count first, then
the command.

**`listdevices`** keeps one row per token (pinned by `test_enrolment_e2e.py:251-254`) and appends
a `chain` column (the root id). `revokedevice --id` accepts any id of a chain.

**B3 — `deactivate`.** In one transaction it revokes all the user's chains and expires their
pending enrolment codes. Its message becomes true. UI sessions are **not** deleted: the
existing `active` join already ends them.

To keep the pinned outcomes, B3 **moves the `active` check ahead of the revocation check**, in
both `auth_middleware` (`app.py:319-327`) and `_ui_session_gate` (`app.py:250-253`). Today
both gates check revocation first. The reorder is safe because both branches reject; only the
outcome name changes, and only for a revoked or expired token that belongs to a deactivated user. A deactivated user's token then
still answers **403 `deactivated_user`** (`test_enrol.py:284-288`), and a UI session still gets
`ui_deactivated_user` (`test_ui_auth.py:256-267`), rather than 401 `revoked_token` or
`ui_revoked_device`.

`reactivate` is **not** added, so a deactivated user is re-created with a new email or by SQL;
RUNBOOK says so.

**Invitation link.**
- `admin.py invite` keeps its stdout byte-identical and appends one line,
  `<public-url>/ui/join#code=<code>`. The waves parse `adduser`, not `invite`, but other
  callers may parse `invite`, so append-only is still the rule.
- The public URL comes from `LOOM_PUBLIC_URL`, which defaults to `https://$FLY_APP_NAME.fly.dev`
  on Fly. In Compose, `docker-compose.yml` sets `LOOM_PUBLIC_URL: https://${LOOM_DOMAIN}` in
  the `loom` service, because today `LOOM_DOMAIN` reaches only `caddy`. `.env.example` gains no
  key; wave r pins it. `invite --public-url` overrides both.
  Check once that the variable is visible inside `fly ssh -C`.
- Without a public URL, the "how to set it" hint goes to **stderr**.

**`GET /ui/join`** is a **static page with no script**:
- it inherits `ui_pages.SECURITY_HEADERS`, which already sends `default-src 'none'`,
  `no-referrer` and `no-store`. For this page only, `security_headers_middleware` applies a
  per-path CSP override that drops `script-src` and `connect-src` and adds
  `form-action 'none'`, since the page has no script and no form. The middleware overwrites any
  CSP a route sets, so the override has to live there. `test_ui_views.py`'s
  `PUBLIC_UI_PATHS` (line 35) gains `/ui/join`. That adds a public path; it removes no
  assertion.
- it never redeems, looks up or renders a code; the code stays in the fragment, which is never
  sent to the server;
- it explains the install steps and says "paste this whole link into `loom enroll`";
- it is added to `PUBLIC_PATHS` explicitly, and an AC asserts that the allow-list equals the
  expected set.

The fragment stays in the browser history; decision 2's TTL bounds that.

**Structural rule (asserted):** no secret ever travels in a path or a query string. This is
enforced by a test over the route table; the access-log filter is only a second belt.

**Phase 1 is not audited.** The operator is the only actor and has a shell, so an audit row
would not constrain them. Log lines written by `admin.py` would land in the operator's terminal,
not in the server logs, so the draft's `caplog` AC is deleted. D19 says this plainly. Auditing
arrives with web admins in phase 2.

**Migration rule:** written down even though phase 1 adds no column (only an index), because
phase 2 adds the codebase's first `ALTER TABLE`.
- Re-check `PRAGMA table_info` **inside** `BEGIN IMMEDIATE`, because the server and an
  `admin.py` run can start together.
- Columns are nullable or have a constant DEFAULT.
- Backfills can be re-run.
- Tests cover a pre-migration database copy and two concurrent `init_db` calls.

### Client

- **`loom enroll` takes the link** at its existing hidden prompt (`getpass`; the code is still
  never accepted as an argument). Parsing is strict:
  - `https` only (loopback `http` excepted), path `/ui/join`, fragment `#code=`, nothing else;
  - the pasted value is never echoed;
  - the server URL comes from the link.
- **Phishing guard.** When the URL came from a link, the CLI shows the host prominently and asks
  **y/N** on the first enrolment of the machine, and whenever the host differs from the
  configured `server_url`. It refuses a silent repoint. If `--server-url` is given and the
  pasted link's origin differs from it, enrolment is **refused**. An explicit, matching
  `--server-url` skips the question, so the existing "no `input()` with `--server-url`" pin
  (`test_cli_enroll.py:41-51`) holds. RUNBOOK §5's pinned literals and headings stay
  (`test_cli_init.py:116-135`).
- **The processor re-reads `config.json` after taking its lock** and before renewing. Today `cfg`
  is loaded (`cli.py:361`, and `processor.py:1899` for `python -m loom.processor`) before the non-blocking lock (`processor.py:1838`). A pass that loaded
  it just before another pass renewed and released the lock would otherwise re-issue over a
  fresh successor. That is churn, not loss, but a cheap guard closes the gap.

### B2 — last slice, cuttable

`loom enroll` on a machine that already has a device token for **the same `server_url`** sends
that token as `Authorization: Bearer <old>` on `POST /api/enroll`. This is proof of possession,
never an id and never a body field: `EnrolRequest` is `extra="forbid"`, so a body field would
422 on older servers, while older servers ignore this header (`app.py:280-284`).

The server, inside `redeem`'s transaction, revokes the chain of that token **only if** it is a
device token (never a legacy `users.token_hash`) of the **same user** as the code. Unknown,
revoked, expired or foreign tokens are a silent no-op and are not counted by the limiter. The
response is unchanged.

Until B2 ships, the docs say "to re-enrol a machine, revoke it, then enrol".

### Contract changes (D19)

- DESIGN §6.2:
  - add `GET /ui/join`;
  - add the renew re-issue rule and the chain revocation rule;
  - add, if shipped, the optional `Authorization` header on `/api/enroll`;
  - amend the Credentials paragraph.
- Add `PUBLIC_PATHS` as an exact set.
- Add `admin.py revoke-orphans` and the `chain` column of `listdevices`.
- RUNBOOK §2, §4 and §5: onboarding with a link, the upgrade step, recovering a mis-sent link.
- USER-GUIDE: the invitee's steps. Also fix its stale "gitleaks is recommended" line, which
  contradicts D16.
- SERVER-GUIDE: `LOOM_PUBLIC_URL`.

**A mis-sent link** (no new command): the operator runs one documented one-liner that expires
that user's unredeemed codes, through `python -m sqlite3` (the image ships no `sqlite3` binary),
then issues a fresh `invite`. `deactivate` is **not** the remedy: it revokes the user's working
machines, and nothing reactivates them.

### Acceptance criteria → assertions (phase 1)

| AC | Where asserted |
|---|---|
| Revoking any id of a chain makes every token of the chain 401 | `server/tests/test_enrol.py` |
| A renew retried after an unpersisted successor succeeds, the earlier successor is revoked, and capture continues (real client, dropped 201) | `server/tests/test_enrol.py` + `client/tests/e2e` through the real `_renew_device_token` |
| A fork fixture (pre-fix database): startup logs the orphan count without revoking; `revoke-orphans` revokes exactly the orphans | `server/tests/test_enrol.py` / `test_admin.py` |
| A revoke interleaved with a renew leaves no live token | `server/tests/test_enrol.py` (interleaving test) |
| Renew O → S1, renew S1 → S2, renew O again: every token after O is revoked, the chain stays one line, and **for each id in {O, S1, S2, new}**, revoking from that id leaves no live token | `server/tests/test_enrol.py` |
| `test_enrol.py:303-325` (the predecessor stays valid) still passes unmodified | `make test` |
| `listdevices` keeps per-token rows plus a `chain` column | `server/tests/test_admin.py` + `test_enrolment_e2e.py:251-254` unchanged |
| `deactivate` revokes the chains and expires the codes, and its message is true | `server/tests/test_admin.py` |
| A deactivated user still gets 403 `deactivated_user` and `ui_deactivated_user` | `test_enrol.py:284-288` and `test_ui_auth.py:256-267` pass unmodified |
| `invite` stdout keeps its lines byte-identical and appends exactly one link line when a public URL is known; the hint goes to stderr otherwise | `server/tests/test_admin.py` |
| `/ui/join` is public, carries the headers (with its CSP override), has no script and never touches a code | `server/tests/test_join.py` (plain HTTP GET) + `test_ui_views.py` with `/ui/join` in `PUBLIC_UI_PATHS` |
| The allow-list equals the expected set; no route takes a secret in a path or query | `server/tests/test_app.py` (route-table test) |
| `loom enroll` accepts the bare code or the link, parses strictly, confirms the host when it came from a link, refuses a link that contradicts `--server-url`, never repoints silently, never echoes | `client/tests/integration/test_cli_enroll.py` |
| B2 (if shipped): same user and device token → old chain revoked; a foreign, legacy or unknown token → no-op; an older server ignores the header | `server/tests/test_enrol.py` + `client/tests/integration/test_cli_enroll.py` |
| Full flow: invite → GET `/ui/join` → paste the link into `loom enroll` → register a project | `client/tests/e2e/test_enrolment_e2e.py` (real uvicorn, real CLI subprocess) |
| `make test` and `LOOM_SKIP_LIVE=1 make contract` green | CI + local |

There is **no new frozen goal wave** for phase 1: the assertions live in the suites that own each
boundary.

### Slices (phase 1, each red-first)

1. B1a + B1b + race re-check, plus the processor re-reading config after its lock.
2. Orphan count, `revoke-orphans`, the `listdevices` chain column, the B3 `deactivate`.
3. `LOOM_PUBLIC_URL`, the `invite` link line and `/ui/join` with its headers; the allow-list and
   route-table tests.
4. Client: `loom enroll` takes the link, with the phishing guard.
5. E2E, docs and the contract list; full suite + contract; self-review.
6. **B2** (cuttable).

---

## Phase 2 — Accounts v2 (D20)

### Common to 2a and 2b

- **`users.role`** (`admin` | `member`, default `member`): the first `ALTER TABLE`, following the
  migration rule above. `admin.py promote/demote --email` bootstraps the first admin.
  **Last-admin guard** inside `BEGIN IMMEDIATE`.
- **One session table:** `ui_sessions` gains a `kind` column. There is no `web_sessions`, no
  `last_seen_at` and one `end_sessions()` helper. Role and `active` are read on every request.
  The cookie stays `loom_ui` at `Path=/ui`.
- **Writes (D14 amended):**
  - POST only, under `/ui`, server-rendered forms, no state-changing GET.
  - A strict `Origin` check against `LOOM_PUBLIC_URL`, with `Sec-Fetch-Site: same-origin` as
    the fallback; a request with **neither** header is rejected.
  - `SameSite=Strict` stays.
  - A **CSRF token**, derived as `HMAC(session_secret, "csrf")` and sent in a hidden form
    field, sits on top of the Origin check. It is never stored, so it needs no column.
  - **Every write is its own POST-only route; no GET page accepts a write method.** The D14
    crawl in `test_ui_views.py:138-141`, which expects a 405 for POST/PUT/PATCH/DELETE on every
    GET page, skips non-GET routes, so it holds unmodified. The code change is the gate's
    blanket "The UI is read-only" refusal (`app.py:240-241`), which becomes "read-only except
    the listed POST routes".
  - `/api/*` stays bearer-only.
- **Setup codes** reuse `enrolment_codes` with `kind ('invite','setup')`, `created_by_user_id`
  and the issuing session/chain id. They have a 10-minute TTL, are single-use, are shown once and
  are redeemed by the existing `loom enroll`.
- **Issuer cascade:** revoking a chain also revokes the chains minted through setup codes that
  its sessions issued. It applies **transitively**: chains minted from codes issued by sessions
  of chains already in the cascade are revoked too. All of it happens in the same
  `BEGIN IMMEDIATE` as the chain revoke, so a two-level chain cannot survive.
- **`auth_events`** (`ts`, `actor_user_id`, `event`, `target_type`, `target_id`, `client_ip`)
  holds **state-changing** events and successful sign-ins only. Failures stay log lines.
  Retention is owner-set and documented as personal data (`client_ip`).
- **`GET /api/me`** (bearer-only) returns the name, role, device id and recent security events.
  `loom status` and `loom doctor` show them. This is how a user learns that an admin reset or
  revoked something.
- **Pages:**
  - *My machines*: chains with name, created, last renewed and expiry; "Add a machine" → setup
    code; "Revoke".
  - *Admin → Users* (admins only): invite (email, name, role) → the phase-1 link, shown once;
    the list with status; deactivate; change role; revoke any machine.
  - Sign-out.
- **ACs common to 2a and 2b:**
  - the last-admin guard;
  - IDOR → 404 (a member touching another member's device);
  - role read on every request;
  - members get 403 on admin pages;
  - a hostile `device_name` is escaped;
  - a cross-origin or header-less POST is rejected;
  - a CLI session has no admin write unless it belongs to an admin;
  - no secret in any log line;
  - every frozen wave still green.
- **Frozen goal wave `t`**, written independently **before** implementation, drives the real
  server in Docker. It is registered in `GOAL_WAVE_DIRS`, with the floor moved, in the same
  commit.

### 2a — no passwords (recommended first)

- The session opened by `loom ui` becomes **writable** for its user, within that user's role.
- Two costs must appear in D20:
  - nobody gets web access without an enrolled machine;
  - an admin's laptop token becomes a web-admin credential. Machine revocation and the
    issuer cascade bound this.
- Invitee flow: the admin clicks "Invite", sends the link, and the invitee runs the phase-1
  `/ui/join` → `loom enroll` flow. A second machine comes from *My machines* → "Add a machine".

### 2b — passwords (only if decision 4 says so)

- **Sign-in:** `GET/POST /ui/signin` (email + password) with one error message for every failure
  and a dummy-hash path when the email is unknown. A session row with `kind='password'` is
  rotated on sign-in and on any privilege change. Emails are **case-folded**: a case-insensitive
  unique index, a startup collision check and folded lookups.
- **KDF:** stdlib `hashlib.scrypt` (n=2^14, r=8, p=1, ≈ 16 MiB), with no new dependency (PLAN P6)
  and sized for the 256 MB VM. Verification is behind a **semaphore of 1–2** and runs outside any
  write transaction.
- **Throttling (D17 amended).** Requirements:
  - a **soft** per-account throttle, never a hard lock, and a throttled account answers exactly
    like a wrong password;
  - counters **only for existing accounts** (`rate_limit.py:27-28` evicts its oldest key at
    10 000, so fake emails must not get keys);
  - when the per-IP window is full, a **429 before hashing**;
  - the effect of an idle-stop reset is documented.

  The store is chosen when 2b is designed. It leans **persisted** (one row per existing user,
  an atomic upsert that counts the attempt before the check), because Fly's idle stop empties
  in-memory counters.
- **Account links** (`purpose` = `invite` | `reset`): 256-bit, sha256 at rest, single-use under
  `BEGIN IMMEDIATE`, 24 h, carried in the fragment. Using a reset link revokes the user's
  sessions. A password re-prompt is required before "Add a machine".
- **Password policy:** minimum 12 characters, no composition rules, a small bundled list of
  common passwords, no forced rotation.
- **D8/D18 note:** backups now hold password verifiers and are secret. The RUNBOOK restore adds
  "re-apply post-backup revocations" and `admin.py end-all-sessions`.
- **Reworded property:** an admin reset is audited, and the user is told through `/api/me` →
  `loom status`. The draft's "shown at next sign-in" is dropped, because after a reset the user's
  own password no longer works.

### Reopened items (D20 lists all of them)

- **D14:** writes are allowed on the listed POST routes, and revocation now has to be rebuilt
  rather than inherited. The 405 crawl holds; the gate's blanket refusal changes.
- **§6.3:** the out-of-scope list and the "no logout" line.
- **Q-3.3's resolution.**
- **Q-3.4**, resolved here only as global roles.
- In 2b also:
  - **D17:** the password exception and the persisted throttle;
  - **D8/D18:** the crackable-verifier note.

### Slices (phase 2)

0. Wave `t` authored independently and frozen (red).
1. The migration idiom, `users.role`, `promote/demote`, the last-admin guard, `ui_sessions.kind`.
2. The writable-UI foundation (Origin check, POST-only forms), the D14 amendment, `auth_events`.
3. *My machines* (chains, setup codes, revoke, cascade).
4. *Admin → Users*.
5. `GET /api/me` + the `loom status`/`doctor` rows.
6. Docs; full suite + contract; adversarial review; real-VPS validation (Compose), as for D17.
7. *(2b only)* sign-in/sign-out, scrypt, throttle, account links, reset, case-folded emails,
   the backup note.

---

## Later (explicitly out of scope)

- Token-usage tracking (decision 3): reuse detection and B1c, with `last_used_at` and the client
  "re-read config and retry once" path.
- Passkeys (WebAuthn); TOTP for admins.
- Real API keys for non-interactive clients (CI), prefixed (`loom_live_…`) so gitleaks and
  secret scanners catch a leak, hashed, scoped, expiring and revocable.
- Per-project permissions (Q-3.4 in full).
- Email delivery of invitations and resets (a mail credential on the server, weighed against
  D8/D18).
- `listinvites`, `revokeinvite`, `reactivate`, `make invite*`: cut from phase 1, and to be revisited
  only if operators ask.

## Risks

| Risk | Mitigation |
|---|---|
| A thief's renew kills the real machine's successor | Loud 401 on the real machine; chain revocation ends the thief's branch; decision 3 can add reuse detection |
| Orphans from before the fix stay live until the operator acts | Startup count in the log; `revoke-orphans` required in the RUNBOOK upgrade step |
| A crafted link points a victim's CLI at an attacker's server | Strict parse, https only, host confirmation, no silent repoint |
| The writable UI widens the attack surface of a public repo's default deployment | Strict Origin + `SameSite=Strict` + POST-only; IDOR and last-admin ACs; adversarial review; wave `t` |
| 2a: an admin's laptop token is a web-admin credential | Revoke the machine; the issuer cascade; 2b if this is unacceptable |
| 2b: password guessing, or hashing exhausting the 256 MB VM | Soft per-account throttle, 429 before hashing, a scrypt semaphore |
| Scope creep | The "Later" list; each item needs its own decision |
| The pilot is three people | Phase 1 alone may be enough; phase 2 is justified by adoption beyond the pilot |

## What the review changed (from the first draft)

- **B1:**
  - "renew refuses a replaced token" became **re-issue**, because refusing would break
    crash-safe renewal. Re-issue revokes every token after the caller and appends, so a
    thief renewing an old token twice cannot cut a live token off the chain.
  - The "used successor" rule moved to decision 3.
  - Chains are revoked by a recursive walk, sound only once renew stops forking.
  - Existing orphans get a count plus an explicit command.
- **B2:** the client never had a token id, and a body field would 422 on older servers. B2 is now
  proof of possession in a header, the last cuttable slice.
- **Wrong rationale:** the frozen waves parse `adduser`, not `invite`.
- **Contract changes:** listed instead of "one route".
- **Audit:** the phase-1 operator audit is cut, because `admin.py` logs never reach the server
  logs.
- **Phase-1 scope:** `listinvites`, `revokeinvite`, `reactivate` and `make invite*` are cut;
  `deactivate` made truthful.
- **`/join` → `/ui/join`:** static, no script.
- **Phishing guard** added to `loom enroll`.
- **Phase 2:**
  - split into 2a and 2b;
  - one session table instead of two;
  - a derived CSRF token (no column) on top of a strict Origin check;
  - scrypt instead of argon2-cffi;
  - a soft throttle instead of a hard lockout (the lockout let anyone lock a user out and
    revealed which emails exist).
- **Decision 0** made explicit, with an honest comparison.
- **Decisions:** 15 shrunk to the 7 in the owner box.
