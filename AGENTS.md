# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.

## Development doctrine

Canonical working rules for any agent (or human) changing this codebase. `DESIGN.md` is the
specification; an implementer should not need to make a
design decision that is not written there — if one comes up, note it and raise it, don't
improvise.

1. **Find the minimal public interface first.** Identify the narrowest public surface
   (CLI subcommand, HTTP route, function boundary) where the behavior under change can be
   observed before writing anything.
2. **TDD in vertical slices:** one red test → minimal green implementation → repeat.
3. **Test observable behavior**, not internals; do not mock internal collaborators.
   Simulate only what the change does not own.
4. **Derive expected values from literals, worked examples, or independent
   specifications** — never from the same algorithm under test.
5. **Verify continuously what you touch, and run the full suite at the end**
   (`make test`, plus `LOOM_SKIP_LIVE=1 make contract` for static contract assertions).
6. **Refactor outside the red-green loop**, with the suite green before and after.
7. **Respect the vocabulary and decisions of `DESIGN.md`** (D-x, I-x, P-x): reuse their terms; a deviation is a raised decision, not a local choice.
8. **Self-review** your changes against repo standards and against the fidelity of the
   original request before declaring work done.
9. **Turn every acceptance criterion into executable assertions before implementing,**
   and leave no acceptance criterion orphaned (unasserted).
10. **Validate the real boundary:** Docker for containerizable infrastructure; external
    record-replay scripts or simulators (outside the product) for non-containerizable
    services. Simulate only what the change does not own.
11. **Use golden manifests for deterministic transformations** (e.g. transcript
    adaptation, OKF frontmatter emission).
12. **Never weaken or delete acceptance assertions to get green.**
13. **Treat shared contracts as read-only** unless there is an explicit decision to
    change them (`contract/SCHEMA.md`, `contract/FLAGS.md`, `contract/ATTRIBUTION.md`,
    `prompts/`, the API in `DESIGN.md` §6.2, the plugin contract of D21: code outside this
    repo builds on it). `ATTRIBUTION.md` is in that list because
    its authority table governs a trust boundary: inverting one row misfiles one
    project's sessions under another, permanently and silently.
14. **Stop and report if an acceptance criterion stays red after honest attempts** —
    no retry loops, no oracle downgrade.
15. **Operations records stay out of this repo.** Records of one deployment — machine
    names, infrastructure IDs, tokens, user/device inventories, real project names, dated
    evidence — go to the private operations notes, never into this repository, its PRs or
    its issues.

## Environment

**A working Docker daemon is required to run the test suite**, not only in CI. `make test`
drives `client/tests/e2e/test_blockt_e2e.py`, whose regression leg sweeps `test/waves` with
`LOOM_GATE=1`, and `test/waves/m` (issue #2, Fly.io packaging) validates the real image
boundary per §10 — under that flag a missing `docker` is a FAILURE, not a skip, by design.
The dependency is introduced two subprocess levels below the Makefile, so it is recorded
here rather than left to be discovered from a stack trace. `gitleaks` (pinned 8.30.1) is
required for the same reason and has been since phase 2; since D16 every processor/doctor
test needs it too. Any test that runs `client/install.sh` must set `LOOM_SKIP_GITLEAKS=1`
(or supply `LOOM_GITLEAKS_TARBALL`), or the installer's step 1b tries to download gitleaks.

**Two suites may share one Docker daemon** (e.g. from two worktrees). Waves `r` and `s` fix
their stacks' subnets, so a second suite does not fail with "Pool overlaps"; it waits for
a host-wide lock and prints one `waiting for the Compose-wave host lock … (held by pid …)`
line. Mechanism and lock file: `test/waves/README.md`.

**A git worktree needs its own `.venv` before the suite will run.** The doctrine above
says to work in a worktree, and `make test` resolves `PYTHON` as
`[ -x .venv/bin/python ] && echo .venv/bin/python || echo python3` — so a fresh worktree
silently falls back to the system `python3` (`No module named pytest`), and pointing
`PYTHON=` at another checkout's venv does not help either: the wave goals spawn the server
as `<REPO_ROOT>/.venv/bin/python`, where `REPO_ROOT` is `Path(__file__).parents[3]`, i.e.
the worktree. The symptom is not a missing-venv message but **72 collection errors and 3
failures** whose only evidence is a `FileNotFoundError` buried in `subprocess.py`. Create
it first:

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e '.[server,client,dev]'
```

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
