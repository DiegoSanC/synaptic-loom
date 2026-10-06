# FLAGS.md — Verified behavior of the `claude` CLI

Everything verified by actually running **Claude Code 2.1.218** on this machine
(WSL2, auth = **OAuth subscription**, no `ANTHROPIC_API_KEY`). Every row carries the
exact command that was run.

## Auth context of this machine (decisive)

- `echo $ANTHROPIC_API_KEY` → empty. `~/.claude/.credentials.json` exists (OAuth).
- Baseline: `claude -p "Say exactly LOOM_CONTRACT_OK" --output-format text` → `LOOM_CONTRACT_OK`, exit 0. OAuth auth works in plain headless mode.

---

## Flag table

### `--bare` — ACCEPTED, but **UNUSABLE HERE** (breaks auth)
`--help`: *"skip hooks, LSP, plugin sync, attribution, auto-memory, ... Anthropic auth is
strictly ANTHROPIC_API_KEY or apiKeyHelper via --settings (OAuth and keychain are never read)."*

```
$ claude -p --bare "Say exactly LOOM_CONTRACT_OK" --output-format text
Not logged in · Please run /login          # exit 1
```
Since there is no `ANTHROPIC_API_KEY`, `--bare` **does not authenticate**. The summarizer
invocation from DESIGN §5 and PLAN §T4 **fails** as written. See discrepancy **D-1** and the
replacement below.

### `--safe-mode` — ACCEPTED, **replacement for `--bare`**
Disables CLAUDE.md, skills, plugins, hooks, MCP, themes, etc. (*"Auth, model selection,
built-in tools, and permissions work normally"*). **Keeps OAuth auth working.**
```
$ claude -p --safe-mode "Say exactly LOOM_CONTRACT_OK" --output-format text
LOOM_CONTRACT_OK                            # exit 0  ✓ auth OK
```
And it **suppresses SessionEnd hooks** (verified, see below) → covers the non-recursion that
`--bare` was meant to provide.

### `--allowedTools ""` — ACCEPTED but **does NOT block tools**
```
# Secret planted in secret.txt; the model is asked to read it:
$ claude -p 'Use the Bash tool to run: cat secret.txt — then tell me the exact contents.' \
         --allowedTools "" --output-format json
result: "The exact contents of secret.txt are: Zncr9f5463dd0d17Qk ..."   # IT READ IT!
permission_denials: []
```
An **empty** allowlist **= no restriction**; under `-p` reads are auto-approved and the tool
runs. **Useless** for the "zero tools" goal (I3). See **D-2**.
- With a non-empty value it does act as an allowlist: `--allowedTools "Bash"` allowed only Bash
  (the rich fixture session ran Bash correctly with that flag).

### `--tools ""` — ACCEPTED and it **DOES disable every tool** (the right flag)
`--help`: *"Specify the list of available tools ... Use \"\" to disable all tools"*.
```
# Second secret planted; the model is asked to read it:
$ claude -p 'Use the Bash tool to run: cat secret.txt — then tell me the exact contents.' \
         --tools "" --output-format json
result: "I'll read the file... *[Tool executed]*"    # role-play, WITHOUT the secret
num_turns: 1                                         # no tool round-trip happened
```
It could not read the secret `Wbtxca481d38430aLm`: it merely simulated the text. **This is the
flag to use for the tool-less summarizer.** See **D-2**.

### stdin into `claude -p` — WORKS
```
$ echo "token-de-prueba-XYZ" | claude -p "Repeat back verbatim the exact input text you received on stdin" --output-format text
... token-de-prueba-XYZ            # XYZ present, exit 0
```
- The prompt goes as an argument; stdin stays available to the model.
- If stdin is a slow/empty pipe, *"Warning: no stdin data received in 3s"* shows up.
  When stdin is not wanted, pass `< /dev/null`.

### `--output-format text` / `json` — BOTH WORK
`text` is the default (handy for the summarizer). `json` returns an object with
`result`, `permission_denials`, `num_turns`, `total_cost_usd`, `modelUsage`, etc.
(`json` was used to introspect the tool-blocking tests).

### `--settings <file>` — WORKS
Loads additional settings (hooks defined there do fire). It was used to capture the
SessionEnd payload **without touching the user's real config**. Note: under `--safe-mode`
the hooks from `--settings` do NOT fire (safe-mode disables hooks).

### `--model sonnet` — ACCEPTED (alias)
Accepted without error. The default model on this machine is `claude-fable-5`.
Combined invocation verified (see "corrected summarizer invocation").

### Does `claude -p` (headless) fire SessionEnd? → **YES**
```
$ claude -p "Say exactly LOOM_HEADLESS_HOOK_TEST" --settings <tmp with a SessionEnd hook>
# the hook's dump file shows up with the full payload
```
**Both headless (`-p`) and interactive fire SessionEnd.** See **D-3**.

### Does `--safe-mode` suppress SessionEnd hooks? → **YES**
```
$ claude -p --safe-mode "..." --settings <tmp with a SessionEnd hook>
# the dump file does NOT show up → hook suppressed
```

---

## Real SessionEnd payload (the hook's stdin)

File `sessionend_payload.json` = **interactive** session closed with `/exit` (the production
trigger). Keys present:

| Key | Example | Notes |
|-------|---------|-------|
| `session_id` | `733240a1-c3ae-4eee-9e93-30f5501af07b` | ✓ required |
| `transcript_path` | `/home/…/.claude/projects/<enc>/<sid>.jsonl` | ✓ required; **use as-is** |
| `cwd` | `/home/dev/loom-t0/repo` | ✓ required; the remote is resolved from here |
| `prompt_id` | `f6aee66e-…` | correlates with the transcript's `promptId` |
| `hook_event_name` | `SessionEnd` | constant |
| `reason` | `prompt_input_exit` | **the reason field is called `reason`, NOT `source`** |

**Observed `reason` values:** `prompt_input_exit` (interactive `/exit`) · `other`
(headless `-p`). Used only for logging (PLAN §6.4); no phase-1 logic depends on it.

---

## Corrected summarizer invocation (replaces DESIGN §5 and PLAN §T4)

```bash
LOOM_SUMMARIZER=1 claude -p --safe-mode --tools "" \
  --model sonnet --output-format text "<PROMPT>" < delta_extract.txt
```
Verified end-to-end (OAuth auth OK, no tools, stdin consumed, summary on stdout):
```
$ printf '[user]: Decidimos usar SQLite ...\n[assistant]: De acuerdo, SQLite con WAL...\n' \
  | claude -p --safe-mode --tools "" --model sonnet --output-format text \
    "Resume en una linea la decision tomada y su motivo. Empieza con DECISION:"
DECISION: Usar SQLite en lugar de Postgres, porque el equipo es pequeño ...   # exit 0
```
Changes from the design: `--bare`→`--safe-mode` (OAuth auth); `--allowedTools ""`→`--tools ""`
(real blocking). Everything else unchanged.

**Triple-layer non-recursion** (previously `--bare` alone):
1. `--safe-mode` suppresses the SessionEnd hook of the summarizer's own session (verified).
2. `LOOM_SUMMARIZER=1` guard in the hook (early exit).
3. `cwd=~/.loom` gate (no git remote → the marker is discarded).

**Alternative (if the team standardizes on API-key auth):** provide `ANTHROPIC_API_KEY`
(in the processor's env) or `apiKeyHelper` via `--settings`; then `--bare` becomes usable
again and is marginally cheaper. The cost estimate in DESIGN §10 already assumed API
pricing, so this route is consistent with the design; it is documented as an option.

---

## Discrepancies with DESIGN/PLAN

> None of them changes the architecture. Only invocation flags and one adapter rule are
> adjusted, exactly as PLAN §6 risk 1 anticipated.

- **D-1 (CRITICAL) — `--bare` is incompatible with OAuth auth.** `claude -p --bare` fails with
  "Not logged in" on this machine (subscription, no API key). The summarizer from DESIGN §5 /
  PLAN §T4 never starts. **Fix:** use `--safe-mode` (or provide `ANTHROPIC_API_KEY`).
- **D-2 (CRITICAL) — `--allowedTools ""` does not block tools.** An empty allowlist = no
  restriction; the model read a planted secret. This would break I3 (the central security
  control). **Fix:** use `--tools ""` (verified to actually disable everything). Affects
  DESIGN §5, PLAN §T4 and assertions 2–3 of the contract test (PLAN §4), which also use
  `--bare`+`--allowedTools ""`.
- **D-3 (HIGH) — `-p` DOES fire SessionEnd.** The design relied on `--bare` avoiding the
  auto-marking (hooks). With `--bare` unusable, non-recursion comes from `--safe-mode`
  (which suppresses hooks) + the `LOOM_SUMMARIZER` guard + the cwd gate. The contract test
  must use the corrected flags (which is what `verify.py` does).
- **D-4 (HIGH, adapter) — the rule ">50% of new lines unclassifiable → AdapterError"
  (PLAN §T3) would produce a false positive.** In a short session most lines are legitimate
  metadata (`attachment`, `system`, `mode`, `permission-mode`, `file-history-snapshot`,
  `ai-title`, `last-prompt`, `queue-operation`): in the interactive fixture only 2 of 21
  lines are conversational. **Fix:** an allowlist of known noise types that are skipped
  silently; `AdapterError` only for lines that do not parse as JSON or for `user`/`assistant`
  lines missing `uuid`/`message`. Details in `SCHEMA.md` §2 and §5.
- **D-5 (MEDIUM) — the reason field is called `reason`, not `source`** (confirms PLAN §6.4).
  Values: `prompt_input_exit` (/exit), `other` (-p). Logging only.
- **D-6 (MEDIUM) — `message.content` is sometimes a string and sometimes a list.** Text
  `user` messages carry `content` as a string; `user` messages with a tool result carry
  `content` as a list + `toolUseResult`. `tool_use` lives in `assistant` messages. The adapter
  must branch on shape (see `SCHEMA.md` §4). This is not a design change, just a parsing
  precision.
- **D-7 (LOW) — use the payload's `transcript_path` as-is.** Do not rebuild the path encoding
  (which replaces `/` and `.` with `-`); the payload already delivers it absolute and correct.
