# Wave goals — FROZEN ORACLES (build phase 1 = Loom phase 2)

The `test_goal_*.py` files under `a/`, `b/`, `c/` are the **frozen per-epic oracles**
authored independently at plan time (2026-07-30, strong
model, adversarial mandate), validated against **real boundaries**, and **read-only
to implementers** — extend `_goalkit.py` (accessory) or add cases, never weaken an
existing assertion. The build runs them verbatim as each epic's stop-condition
and as the post-fix regression gate.

| Wave | Epic | Goal | Boundary |
|---|---|---|---|
| a | E1 orphan-sweep | `a/test_goal_e1_sweep.py` | real FS + flock; stub claude; httpserver |
| a | E3 gitleaks-engine | `a/test_goal_e3_scan_engine.py` | REAL gitleaks 8.30.1; PATH shims |
| b | E2 timer-packaging | `b/test_goal_e2_install_doctor.py` | REAL install.sh; shimmed schedulers; file:// clone |
| c | E4 phase-e2e-gate | `c/test_goal_e4_e2e.py` | REAL uvicorn + admin CLI + gitleaks + install.sh |
| i | E13 logbook-query-surface | `i/test_goal_e13_search.py` | REAL uvicorn + temp LOOM_DB (FTS5/WAL); real loom CLI |
| i | E14 typed-capture | `i/test_goal_e14_typed_items.py` | REAL processor + git repo; GOLDEN claude fixture (recorded 2026-08-12, claude 2.1.228); capture httpd; REAL install.sh |
| j | E15 curation-links | `j/test_goal_e15_links.py` | REAL uvicorn + real curate; smart verdict replay stub (frozen stdin/verdict grammars) |
| k | E16 digest | `k/test_goal_e16_digest.py` | REAL uvicorn + real curate/log; recording digest stub |
| l | E17 blockt-e2e-gate | `l/test_goal_e17_e2e.py` | gate-of-the-gate + runs gate scenarios under LOOM_GATE=1 |
| m | issue #2 fly-packaging | `m/test_goal_fly_packaging.py` | REAL docker build + docker run; bind mount standing in for the Fly volume |
| n | issue #4 attribution-fallback | `n/test_goal_issue4_attribution.py` | REAL git repo + REAL linked worktree + REAL deletion; REAL `sessionend.sh` against the recorded payload; real processor; stub claude; httpserver |
| o | D11 summarizer v3 | `o/test_goal_d11_summarizer_v3.py` | REAL processor + git repo; two GOLDEN v3 claude fixtures (recorded 2026-09-23, claude 2.1.280); capture httpd; REAL install.sh |
| p | D13 entry items (slice C) | `p/test_goal_d13_entry_items.py` | REAL uvicorn + temp LOOM_DB + admin CLI; real processor replaying wave o's goldens against it |
| q | D13 item links (slice E) | `q/test_goal_d13_item_links.py` | REAL uvicorn + temp LOOM_DB + admin CLI (two users); real `loom curate`/`log`/`unlink`; smart verdict replay stub (item grammars) |
| r | D17 compose-caddy | `r/test_goal_d17_compose_caddy.py` | REAL `docker compose up --build` of `deploy/compose` (repo Dockerfile + pinned Caddy + named volume); client containers at literal addresses; Caddy's internal CA for `loom.localhost` |
| s | D18 compose-backups | `s/test_goal_d18_compose_backups.py` | REAL `docker compose up --build` of `deploy/compose` (`loom` + `backup`, real named volumes); REAL `backup.sh` in the service and in one-off containers; REAL `deploy/compose/restore.sh` run on the host |
| t | D20 roles-and-writes (phase 2a) | `t/test_goal_d20_roles_and_writes.py` | REAL `docker build` of the repo Dockerfile + `docker run` (named volumes, loopback ports, no Compose); `docker exec … server.admin`; the SQLite file read in the container; REAL `loom enroll`/`ui`/`status`/`doctor`; a FROZEN pre-D20 schema fixture |

Waves `o`, `p` and `q` belong to the plan of 2026-09-23
(`docs/plans/2026-09-23-fix-label-grammar-fidelity-plan.md`): `o` is slice B (decision
D11), `p` is slice C (decision D13, the HTTP boundary of addressable items; slice D
extends it by adding cases) and `q` is slice E (item links, their revocation and
item-level curation). Wave `o` follows the wave-i E14 pattern: its goldens are re-recorded only with the recipe in its
docstring, and the prompt's behaviour on a wider set of cases is measured by the harness in
`test/prompt_eval/` (real model calls, not collected by pytest).

Waves `m` and `n` are the waves that are NOT per-epic oracles: they belong to
GitHub issues (#2 Fly.io packaging, #4 attribution after worktree deletion) and are
authored against those issues' acceptance criteria. They obey the same freeze — read-only
to implementers, extended by adding cases, never by weakening one — and they are
registered in `GOAL_WAVE_DIRS` and the collection floor of
`client/tests/e2e/test_blockt_e2e.py` like every other wave. Wave `m`'s boundary is Docker
(`AGENTS.md` §10), so it needs a working Docker daemon; under `LOOM_GATE=1` a missing
daemon is a FAILURE, not a skip. Wave `n` needs only `git` and `jq`, and it is the
executable form of `contract/ATTRIBUTION.md` — that document is the specification its
assertions are derived from, so the two change together or not at all.

Wave `r` (D17, owner decision of 2026-10-16) is, like `m`, a Docker-boundary wave that is
not a per-epic oracle. It drives the real `deploy/compose` stack; the one layer on top,
`r/compose.goal.yml`, moves Caddy's ports to a loopback port and fixes the `edge` subnet
so client containers get literal addresses. TLS is real: `LOOM_DOMAIN=loom.localhost` is a
name Caddy's automatic HTTPS issues from its internal CA, because a public ACME CA cannot
validate a name from a test host. The stack's subnets are fixed by design, so two runs of
wave `r` cannot share one Docker host at the same time.

Wave `s` (D18, owner decision of 2026-10-17) is the same kind of wave for that topology's
backups and restore. It drives the real `deploy/compose` stack (`loom` and `backup` only;
Caddy is wave `r`'s) and the real `deploy/compose/restore.sh`; its one layer,
`s/compose.goal.yml`, moves the `backend` subnet to `10.217.19.0/24`, so waves `r` and `s`
can run at the same time on one host, but two runs of wave `s` cannot. The interval (2 s)
and retention (3) it runs with come from its own env file; the defaults are asserted on the
production file. **Amendment to wave `r` (D18):** `test_only_caddy_is_published_and_the_server_has_no_host_port`
asserted the service set `{loom, caddy}`; it now asserts `{loom, caddy, backup}` exactly,
and that no service but Caddy has a host port.

Wave `t` (D20, owner decision of 2026-10-19) is the frozen oracle of phase 2a of
`docs/plans/2026-10-19-feat-invite-links-and-accounts-v2-plan.md`: roles, the writable
`/ui` reached through `loom ui`, setup codes, the issuer cascade, `auth_events` and
`GET /api/me`. It is derived from DESIGN.md's D20 row and §6.4 alone, and was written and
committed red, before the implementation. Its boundary is the real server in Docker, run
with `docker run` rather than Compose, so it shares no subnet with waves `r` and `s` and can
run next to them; every container and volume it starts has a unique name and is removed at
the end, failure or not. The server runs with `LOOM_PUBLIC_URL` set to the exact loopback
origin the tests use, and with `LOOM_CLIENT_IP_SOURCE=header:x-goal-client-ip` and a fresh
address per request, because the wave provokes far more refusals than the auth-failure
window's ten per minute. `t/fixtures/pre_d20_schema.sql` is the schema as it stood before
D20, for the migration-rule test; it is never regenerated from the code.

Seam ids such as `t-curation-watermark`, `p3-state-stat-fields` or `p4-coverage-cache`,
cited across the code and tests, name interfaces frozen by the original build plan, whose
seam record is not published with this repository. The frozen goals here and the tests
that cite each id are its authoritative statement: a seam changes only additively, and an
existing assertion about it is never weakened.

Run one: `.venv/bin/python -m pytest test/waves/<w>/test_goal_*.py -q`
Regression suite: `.venv/bin/python -m pytest -q` (darwin known-fail: see
`test/phase_gates/phase_1_e2e_config.json`).

Environment assumptions (validated on this machine at plan time): gitleaks 8.30.1 on
PATH; `.venv` built from `-e .[server,client,dev]`; E4's install scenario assumes a
host whose PATH has no usable `systemctl --user` (macOS) so the cron branch is the
executable boundary — the systemd branch is verified in wave b via the recorder shim
and manually on the WSL2 pilot host (AC-2.2c).

**D16 amendments (2026-10-15, owner decision; DESIGN.md D16).** D16 superseded the
phase-2 "``auto`` → regex fallback" behaviour these goals pinned: gitleaks is mandatory,
``auto`` is a legacy alias of ``gitleaks``, ``regex`` is refused, and capture PAUSES without
gitleaks. The goals were amended only to assert the new behaviour with equal or greater
strength, never to drop an assertion: in ``a/test_goal_e3_scan_engine.py`` the two fallback
tests now assert the refusal (and still that ``regex`` never executes the binary); in
``b/test_goal_e2_install_doctor.py`` the no-gitleaks doctor is a ✗ "capture paused", not a
⚠; in ``d/test_goal_e7_trigger.py`` the "doctor-green" environment includes gitleaks. The
environment of every goal that ran capture with ``scanner_engine="regex"`` now runs it with
the real ``gitleaks``, and every goal that runs ``install.sh`` sets ``LOOM_SKIP_GITLEAKS=1``
(the installer's new step 1b downloads gitleaks; tests never touch the network — that step
is covered by ``client/tests/integration/test_install_gitleaks.py``).

Planted-secret trap (E3/E4): validated empirically at plan time — gitleaks rule
`generic-api-key` (entropy 5.29) catches it; the phase-1 regex set returns `[]`.

### Re-recording the wave-i golden (E14)

Only when `prompts/summarizer_v2.md` changes, from the repo root, with the real CLI:

```bash
LOOM_SUMMARIZER=1 claude -p --safe-mode --tools "" --model sonnet --output-format text \
  "$(cat prompts/summarizer_v2.md)" \
  < test/waves/i/fixtures/delta_sop.txt > test/waves/i/fixtures/summary_v2_golden.md
```

The result must still carry all four labels, with `[method]` on the SOP (the goal guards
this). `client/tests/unit/fixtures/items_manifest.json` is written by hand from the golden,
so update it in the same change.
