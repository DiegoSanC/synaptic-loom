## Context
The conversation addresses recurring drift between CLI flags and the contract, and touches on a related implementation choice and follow-up documentation work.

## Decisions
- [method] **Always run `make contract` after CLI updates** — added to the Makefile docs as a standing rule to prevent CLI flags from drifting from the contract again. Evidence: "always run make contract after CLI updates - that's our standing rule from now on, added to the Makefile docs." Status: current.
- [decision] **Use argparse instead of click for the new subcommand** — to avoid adding a dependency. Evidence: "we chose argparse over click for the new subcommand to avoid a dependency; see client/loom/cli.py." Status: current.
- [task] **Diego to update `docs/RUNBOOK.md` section 7 by Friday** — (rationale not stated). Evidence: "Diego will update docs/RUNBOOK.md section 7 by Friday." Status: current.
- [fact] **The pilot host is WSL2, where `setsid` is available** — (rationale not stated). Evidence: "the pilot host is WSL2, so setsid is available there." Status: current.

## Open items
- Runbook update (docs/RUNBOOK.md §7) pending completion by Friday.
