# SCHEMA.md — Real structure of the JSONL transcript

Verified empirically against `contract/fixtures/real_session.jsonl` with
Claude Code **2.1.218** (see `VERSION`). Everything written here comes from a real run,
not from documentation. This document is the source for implementing `transcript_adapter.py`.

The fixture and `sessionend_payload.json` were anonymized for publication (2026-10-06):
paths, the recording machine's skill/agent listings, hook commands and MCP server names
were replaced; record count, order, types and every conversational message are unchanged.

## 1. Location and general shape

- Path: `~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`.
  The `<encoded-cwd>` replaces `/` and `.` with `-` (e.g.
  `/home/dev/loom-t0/repo` → `-home-dev-loom-t0-repo`).
  **Do not rebuild this path**: the SessionEnd payload already carries `transcript_path`
  absolute and correct (see `FLAGS.md` §payload). Use it directly.
- One JSON object per line. Every non-empty line parses as JSON (verified).
- The discriminator field is the top-level `type`.

## 2. Observed line types (`type`)

| `type` | Conversational? | Adapter action |
|--------|------------------|--------------------|
| `user` | **Yes** | Extract (text or tool_result) |
| `assistant` | **Yes** | Extract (text / thinking / tool_use) |
| `attachment` | No (noise) | Skip |
| `system` | No (noise) | Skip |
| `queue-operation` | No (noise) | Skip |
| `last-prompt` | No (noise) | Skip |
| `mode` | No (noise) | Skip |
| `permission-mode` | No (noise) | Skip |
| `file-history-snapshot` | No (noise) | Skip |
| `ai-title` | No (noise) | Skip |

> **IMPORTANT — the set of noise types is OPEN.** The headless and the interactive lists
> differ (the interactive one adds `mode`, `permission-mode`,
> `file-history-snapshot`, `ai-title`, `system`). Treat parsing as an **allowlist**:
> process only `user`/`assistant` with a valid `message`; **everything else is skipped
> silently** and does NOT count as an "unclassifiable line". See discrepancy **D-4** in
> `FLAGS.md`: the ">50% unclassifiable → AdapterError" rule of the phase-1 adapter would produce a
> false positive, because in a short session most lines are legitimate metadata
> (in the interactive fixture, only 2 of 21 lines are conversational).

## 3. Fields of a conversational line

`user`/`assistant` lines share (among others):

| Field | Meaning |
|-------|-------------|
| `uuid` | **Unique message ID** (the anchor of the D3 pointer) |
| `parentUuid` | uuid of the parent message; chains the conversation. May be `null` at the root. Noise lines do **not** have a `uuid`, so a message's parent may point to a line the adapter skips |
| `type` | `"user"` or `"assistant"` |
| `message` | object with `role` and `content` (see §4) |
| `timestamp` | ISO-8601 (`2026-07-24T12:20:15.639Z`) |
| `sessionId`, `cwd`, `version`, `gitBranch`, `isSidechain`, `userType` | metadata |
| `requestId` | only on `assistant` |
| `promptId` | on `user`; correlates with the payload's `prompt_id` |

- **message uuid** → `line["uuid"]`
- **parent uuid** → `line["parentUuid"]`
- **role** → `line["message"]["role"]`

> For the pointer (D3): the robust anchor is `new_last_line` (line number). `new_last_uuid`
> must be taken from the **last conversational message** (noise lines have no uuid).

## 4. `message.content`: two shapes depending on the case

`content` can be a **string** or a **list of blocks**. The adapter must branch:

### 4.1 Text `user` message → `content` is a STRING
```json
{"type":"user","uuid":"8864...","parentUuid":"68d5...",
 "message":{"role":"user","content":"Use the Bash tool to run exactly: echo ..."}}
```
The user's text = `message.content` (plain string).

### 4.2 `user` message with a tool result → `content` is a LIST
```json
{"type":"user","uuid":"81eb...","parentUuid":"4618...",
 "message":{"role":"user","content":[
    {"type":"tool_result","tool_use_id":"toolu_01Uw...","content":"hello-from-loom-fixture","is_error":false}]},
 "toolUseResult":{"stdout":"hello-from-loom-fixture","stderr":"","interrupted":false,"isImage":false}}
```
- The `tool_result` block carries `tool_use_id`, `content` (string or list) and `is_error`.
- There is also a top-level `toolUseResult` object with raw `stdout`/`stderr`.
- The adapter must **truncate** this content (DESIGN: ~500 chars) and emit
  `[tool:<name>]` (the name is resolved by `tool_use_id` against the preceding `tool_use`,
  or labeled generically as `[tool_result]`).

### 4.3 `assistant` message → `content` is a LIST of blocks
Observed blocks and where their text lives:

| `block.type` | Text in | Notes |
|--------------|----------|-------|
| `text` | `block.text` | The assistant's visible text |
| `thinking` | `block.thinking` | Reasoning; came through empty in the fixture. v1: optional/ignorable |
| `tool_use` | — | `block.name` (e.g. `"Bash"`), `block.id` (`toolu_...`), `block.input` (object, e.g. `{"command":"echo ...","description":"..."}`) |

`assistant` also carries `message.model` (e.g. `"claude-fable-5"`); `user` does not.

## 5. Minimum extractor contract (for `read_delta`)

For every new line (index > `last_line`):
1. `obj = json.loads(line)`. If parsing fails → counts as a hard error.
2. If `obj["type"]` is not `user`/`assistant` → **skip** (known or unknown noise; not an error).
3. If it is conversational but `uuid` or `message` is missing → shape error (candidate for `AdapterError`).
4. Extract per §4:
   - `user` + str content → `[user]: <text>`
   - `user` + list content with `tool_result` → `[tool:<n>]: <stdout|content truncated to 500>`
   - `assistant` → concatenate `text` blocks; each `tool_use` → `[tool:<name>]: <truncated input>`
5. `new_last_line = total number of lines read`; `new_last_uuid = uuid of the last conversational one`.

`AdapterError` (fail-loud, I4/P5) **only** if: some line does not parse as JSON, or a
`user`/`assistant` line lacks `uuid`/`message`, or **no** conversational message shows up
where content was expected. **Never** because of an abundance of noise lines.

## 6. Field-name summary (direct answer to the preflight's questions)

| Concept | Real field |
|----------|-----------|
| message uuid | `uuid` |
| parent uuid | `parentUuid` |
| role | `message.role` (`"user"`/`"assistant"`) |
| line type | `type` (top level) |
| user text | `message.content` (string) |
| assistant text | `message.content[*].text` where `type=="text"` |
| tool_use | `message.content[*]` with `type=="tool_use"` → `.name`, `.id`, `.input` |
| tool_result | `message.content[*]` with `type=="tool_result"` → `.tool_use_id`, `.content`; + `toolUseResult.stdout/stderr` |
| model | `message.model` (assistant only) |
