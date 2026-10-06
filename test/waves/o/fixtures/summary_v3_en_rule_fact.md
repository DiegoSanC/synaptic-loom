## Context
Debugging an e2e gate failure on macOS caused by a missing `setsid` command, and establishing a testing rule for hooks.

## Decisions
- [fact] **`setsid` is unavailable on macOS, so the e2e gate fails there** — the pilot host is WSL2, where `setsid` exists and the hook works; on darwin this is treated as the one sanctioned known failure. Evidence: "macOS has no setsid... on darwin this is the one sanctioned known failure."
- [method] **Every new hook test must pass under `LOOM_GATE=1` before it is merged** — standing rule, written into AGENTS.md. Evidence: "every new hook test must pass under LOOM_GATE=1 before it is merged."
