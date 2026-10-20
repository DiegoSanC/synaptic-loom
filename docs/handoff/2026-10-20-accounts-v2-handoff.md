# Handoff — accounts v2 (D19, D20 phase 2a), 2026-10-20

For whoever picks this work up next, on any machine. It covers the state of the code
only. The records of a particular deployment (app names, snapshot and image ids, who the
users are, who to promote) are **not** here, by AGENTS.md rule 15. They live in the
owner's private operations notes and in the private mailbox between the owner's machines.

## State of `main`

| What | Where |
|---|---|
| D19: invitation links, renewal chains revoked as one, `deactivate`, B2 re-enrolment | DESIGN.md §6.2; plan `docs/plans/2026-10-19-feat-invite-links-and-accounts-v2-plan.md` (phase 1) |
| D20 phase 2a: global roles, a writable `/ui` reached through `loom ui`, setup codes, issuer cascade, Retire vs Revoke, `auth_events`, `GET /api/me`, the account rows of `loom status`/`doctor` | DESIGN.md D20 row and §6.4 (the contract, amended after the adversarial review); frozen goal wave `t` |
| The UI session lasts 7 days and slides as it is used (owner, 2026-10-20) | DESIGN.md D14 row and §6.3 |
| Two full suites can share one Docker host (the Compose waves take turns on a host-wide lock) | `test/waves/README.md`, AGENTS.md "Environment" |
| Phase 2b (web sign-in without an enrolled machine): **future, not scheduled** | `docs/plans/2026-10-20-feat-accounts-2b-web-sign-in-future-plan.md` |

Evidence at `b359c78`: `make test` 1958 passed, 0 failed; `LOOM_SKIP_LIVE=1 make contract`
OK; wave `t` 43/43. Before the merge, the 2a branch was also driven end to end on a
throwaway Fly app through a real headless Chrome (18/18 checks), and that app was
destroyed afterwards.

## Next steps

1. **Bootstrap the first admin on a deployed server**, which every server upgraded to D20
   needs, because every existing user starts as `member`:
   `python -m server.admin promote --email <email>` (on Fly: `fly ssh console -a <app> -C "…"`).
   Promote two people, so one can demote or deactivate the other without hitting the
   last-admin guard. Then each user runs `loom ui` once; the session lasts 7 days.
2. **After any upgrade**, read the startup log: `device_token_orphans count=N` (if N > 0,
   run `python -m server.admin revoke-orphans`, RUNBOOK §4), `client_ip_source mode=…`
   (`fly-client-ip` on Fly), and no `public_url_unset` WARN (on Fly the public URL comes
   from `FLY_APP_NAME`; elsewhere set `LOOM_PUBLIC_URL`).
3. **Dependabot alerts**: parked by the owner, still open.
4. **Known limitation:** `loom doctor` tells "someone else changed your account" apart
   by display name (`/api/me` has no actor id, and wave `t` freezes its keys). Two users
   with the same name are not told apart. The 2b plan says how to change it: a new wave
   plus an explicit D20 amendment, never an edit to wave `t`.

## Continuing on Windows

- **Run the suite in WSL2, not in Git Bash.** The suite drives `install.sh`, systemd-user
  timers, `flock`, Docker and gitleaks 8.30.1. RUNBOOK and USER-GUIDE already cover WSL2
  for the client.
- **Prerequisites** (AGENTS.md "Environment"): a running Docker daemon and gitleaks 8.30.1.
  Then a fresh checkout needs its own venv:
  `uv venv .venv && uv pip install --python .venv/bin/python -e '.[server,client,dev]'`.
  Without it, the suite fails with 72 collection errors.
- **Commits:** use `DiegoSanC <63015938+DiegoSanC@users.noreply.github.com>`, never a
  personal or work address. Check `git config user.email` first.
- **Docs are pinned by tests,** and frozen waves `a`–`t` are read-only. Run `make test`
  and `LOOM_SKIP_LIVE=1 make contract` after any change, docs included.
- **No deployment records in this repo** (rule 15). They go in the private notes or the
  mailbox.
