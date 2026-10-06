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
