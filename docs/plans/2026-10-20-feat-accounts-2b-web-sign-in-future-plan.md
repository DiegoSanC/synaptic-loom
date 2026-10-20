---
title: "feat (future): web sign-in without an enrolled machine — accounts v2 phase 2b"
type: feat
date: 2026-10-20
complexity: high
baseline: main after D20 phase 2a and the 7-day sliding UI session
decisions: D21 (proposed, number provisional); amends D14, D17, D8/D18
status: FUTURE — not scheduled. Written down so the next attempt starts from what phase 2a taught us. Nothing here is approved.
---

# feat (future): web sign-in without an enrolled machine (phase 2b)

## Why this exists

Phase 2a (D20) gave the web UI roles and writes, but the only way in is still `loom ui`
from an enrolled machine. The owner then accepted the cheap fix for the day-to-day friction
(a 7-day session that slides as it is used, 2026-10-20) and asked for the rest to be kept as
a plan. **2b is worth doing on the day someone needs the UI without an enrolled machine**
(a reviewer or manager who only reads, a phone, a borrowed computer). If everyone who uses
the UI has `loom` installed, the sliding session already covers it.

## Options to weigh first (owner decision 0)

| Option | What it gives | What it costs |
|---|---|---|
| **Passwords** (this plan's default shape, inherited from the 2026-10-19 plan) | `/ui/signin` with email + password | A verifier store, a KDF on a 256 MB VM, an online-guessing surface, a reset flow run by an admin, backups that become secret |
| **Passkeys (WebAuthn)** | Phishing-resistant, no shared secret stored on the server | More protocol code (or a dependency, against PLAN P6), enrolment of a passkey still needs a first trusted session (`loom ui` or an admin link), device-loss recovery |
| **Magic link by email** | No password at all | A mail credential on the server (weighed against D8/D18), deliverability, the link is as strong as the inbox |
| **Do nothing more** | — | People without an enrolled machine stay out |

Recommendation when this is picked up: compare **passwords vs passkeys** with a short spike
before writing the contract. Passkeys remove the hardest part of the password plan (the
throttle and the verifier in backups), at the price of more protocol.

## What phase 2a already provides (reuse, do not rebuild)

- The write framework: POST-only routes registered in `ui_pages.WRITE_ROUTES`, the
  `Origin`/`Sec-Fetch-Site` check against the public URL, the CSRF token
  (`HMAC(cookie, "csrf")`), and `ui_pages.write_transaction` re-reading session, device,
  `active` and role under the write lock.
- `ui_sessions.kind` (`cli` today; `password` was reserved for 2b) and `ui_auth.WRITABLE_KINDS`.
- `auth_events` (+ retention), `GET /api/me`, the `loom status`/`doctor` account rows.
- Roles, the last-admin guard, `admin.py promote/demote`, the web invite (an `invite`-kind
  enrolment code shown once) and the `db.ensure_column` migration rule.
- The 7-day sliding session (`ui_auth.renew_session`).

## Scope if passwords are chosen (from the 2026-10-19 plan, unchanged in substance)

- **Sign-in:** `GET/POST /ui/signin` (email + password); one error message for every
  failure; a dummy-hash path when the email is unknown; the session row has
  `kind='password'`, is rotated on sign-in and on any privilege change.
- **Case-folded emails:** a case-insensitive unique index, a startup collision check
  (refuse to start, naming the colliding rows, never merging them), folded lookups. This
  is the second migration of the codebase: follow `db.ensure_column`'s rule.
- **KDF:** stdlib `hashlib.scrypt` (n=2^14, r=8, p=1, ≈16 MiB), no new dependency;
  verification behind a semaphore of 1–2, outside any write transaction.
- **Throttling (amends D17):** a soft per-account throttle (never a hard lock, a throttled
  account answers exactly like a wrong password); counters only for existing accounts;
  a 429 before hashing when the per-IP window is full; **persisted** (Fly's idle stop
  empties in-memory counters): one row per existing user, an atomic upsert that counts the
  attempt before the check.
- **Account links** (`purpose` = `invite` | `reset`): 256-bit, sha256 at rest, single use
  under `BEGIN IMMEDIATE`, 24 h, carried in the fragment. Using a reset link ends the user's
  sessions. A password re-prompt before "Add a machine".
- **Password policy:** ≥ 12 characters, no composition rules, a small bundled list of
  common passwords, no forced rotation.
- **Backups (amends D8/D18):** backups now hold password verifiers and are secret; the
  RUNBOOK restore adds "re-apply post-backup revocations" and `admin.py end-all-sessions`.
- **Reworded property:** an admin reset is audited, and the user learns of it through
  `/api/me` → `loom status`.

## Lessons from phase 2a that this plan must carry (the adversarial review of 2026-10-19)

Each of these was a real defect in 2a, found only by review or by a real browser. Turn each
into an acceptance criterion of 2b before implementing.

1. **Test writes in a real browser, not only with a test client.** Under
   `Referrer-Policy: no-referrer` Chrome sends `Origin: null` on every form POST (the Fetch
   standard says so), and every write was refused with 403 while the whole suite was green,
   because the tests set `Origin` by hand. `/ui` now sends `same-origin`; the sign-in form
   must be proved in a headless Chrome against the real server (as the 2a Fly check did).
2. **Re-check inside the write's transaction.** The gate authorizes before the body is read,
   and a client can hold the body back. Sign-in, reset, "change password" and every admin
   action must go through `write_transaction` (or its sign-in equivalent) so a revocation,
   deactivation or demotion that commits in between changes nothing.
3. **Read the clock under the lock** for anything single-use (account links, like
   enrolment codes): a timestamp taken before `BEGIN IMMEDIATE` let a code redeem after a
   revocation had already expired it.
4. **An event commits with its change**, and is written only when something changed
   (sign-in, sign-out, reset, password change).
5. **Every `/ui` refusal is an HTML page naming its code** — the sign-in page's errors too,
   with the single generic message for credential failures.
6. **Ids in paths go through `db.is_row_id`** (an oversized id was a 500).
7. **The cascade has a sibling, Retire.** Decide early how password sessions relate to
   machine revocation: a password session has no device token, so "revoke my laptop" no
   longer ends a browser session opened by password. Name what ends it (sign-out, reset,
   deactivate, `end-all-sessions`) in the contract.
8. **The frozen oracle freezes response shapes.** Wave `t` pins `/api/me`'s keys exactly, so
   `loom doctor` still tells "someone else" apart by display name (two users with the same
   name are not told apart). If 2b needs an actor id in `/api/me`, write it into the new
   wave and amend D20 explicitly rather than editing wave `t`.

## Process (as phase 2a, which worked)

0. Owner decision 0 (above) and a D21 contract in DESIGN (§6.5), written before any code.
1. A frozen goal wave `u`, written independently from the contract, red, driving the real
   image with `docker run` (not Compose) and, new for 2b, a headless Chrome for the sign-in
   and write forms.
2. Parallel slices in worktrees (sign-in + KDF; throttle; account links + reset; case-folded
   emails migration; docs), integrated by the coordinator, full suite + contract.
3. A three-reviewer adversarial pass (security, data integrity, architecture/fidelity),
   then fixes, then a real Fly test app driven by a real browser, destroyed afterwards.

## Effort and risk (estimate)

About the size of phase 2a: one wave of 4–5 workers, one review round, one fix round.
Higher risk than 2a: the throttle and the reset flow are where races and account
enumeration hide, and the UI becomes guessable from the internet (today it is not,
because nothing opens it without an enrolled machine).
