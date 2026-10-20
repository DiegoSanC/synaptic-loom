# ATTRIBUTION.md — How a captured session is assigned to a project

Project identity (D1) is `normalize(<git remote origin>)` — lowercase `host/org/repo`.
This document is the contract for **where that remote comes from** when the session's
own working directory no longer exists by the time the processor runs.

It exists because agent sessions run in **ephemeral git worktrees** that are deleted as
soon as the agent finishes — often seconds after `SessionEnd`, always before the
15-minute periodic pass. `_process_marker` resolves identity at processing time from
`marker.cwd` (`client/loom/processor.py`, step 2), so a deleted worktree makes
`_git_remote` return `None` and an intact transcript is discarded as
`dropped_no_remote`.

The fix moves the **resolution** earlier without moving the **decision**: the SessionEnd
hook captures the raw `origin` URL while the worktree still exists; the processor
consumes it **only as a constrained fallback**. `normalize()` remains the single
identity authority, and the registered-project gate is unchanged.

Issue: #4 of the original development repository.

---

## 1. Marker schema (additive)

The queue marker `$LOOM_HOME/queue/<session_id>.json` gains **one optional field**:

```jsonc
{
  "session_id":     "733240a1-…",             // unchanged
  "transcript_path":"/home/…/<sid>.jsonl",    // unchanged
  "cwd":            "/tmp/agent-wt-1",        // unchanged
  "reason":         "prompt_input_exit",      // unchanged
  "marked_at":      "2026-09-13T10:00:00Z",   // unchanged
  "cwd_remote_url": "git@github.com:org/repo.git"  // NEW — optional, raw, may be absent
}
```

**Additive and optional.** A marker without the field behaves exactly as it does today.
That is what makes the change safe for markers already sitting in `queue/` across an
upgrade, for a `~/.loom/hooks/sessionend.sh` that is older than the repo's, and for the
sweep's synthetic markers (§5).

**Raw, not normalized.** `normalize()` (`client/loom/normalize.py`) is the identity
authority (D1) and the server copies its test table character for character
(`DESIGN.md` §3). Canonicalizing in the hook would mean a **second implementation in
`jq`/shell** that can drift from the Python one — and a drift here does not fail loudly,
it files sessions under a `host/org/repo` key nobody else computes. The hook captures a
string and makes no judgement; every semantic decision stays in Python, on the existing
code path.

**Absent, never `null` or empty.** `jq` omits the key when `git` produced nothing, so
"the hook could not resolve it" and "the hook is an old version" are one state with one
code path, not two.

**First capture wins.** `SessionEnd` fires more than once per session by design —
`reason` = `clear`/`resume` are "extra capture points for free" (`DESIGN.md` §4.1) — and
the hook rebuilds the whole marker from stdin on every fire. That is idempotent for the
five original fields, because each is re-derivable from the payload on *any* invocation.
It is **not** idempotent for `cwd_remote_url`, which is derivable only while the worktree
exists — and a session's worktree is routinely gone by its final fire. So a marker that
already carries a remote keeps it, and a later fire neither re-derives nor overwrites it.

Without this rule the feature undoes itself in two ways, both demonstrated:

- the later fire finds no directory, omits the field, and the session drops as
  `dropped_no_remote` — the exact loss this contract exists to prevent, reintroduced by
  the feature's own second write;
- worse, if a pooled ephemeral path has meanwhile been recycled for a **different** repo,
  the later fire captures *that* repo's remote into this session's marker. At processing
  time the path is gone again, so R4 proceeds on the stolen value and logs
  `attribution_from_marker` — indistinguishable from a correct resolution. That is
  strictly worse than L-2, which at least emits `attribution_superseded`.

Pinning cannot outrank a remote that is genuinely current: live truth still wins whenever
`cwd` resolves (§2, R3). A session's origin does not change across re-fires of the same
session, so the first fire that can answer is the one whose answer is about this session.

This is the hook's only read-modify-write. It is safe because the write remains a single
atomic `tmp` + `mv -f`, `sweep.py` never overwrites an existing marker, and a marker the
processor removes mid-hook simply yields an empty prior and a fresh capture.

There is a landed precedent for extending this schema: `client/loom/sweep.py` already
writes a sixth key, `"origin": "sweep"`, onto the hook's five.

### 1.1 The captured URL is credential-redacted before it is written

Agent worktrees are routinely cloned with a tokenized remote:

```
https://x-access-token:ghp_LIVE_TOKEN@github.com/org/repo.git
```

Writing that verbatim into `~/.loom/queue/<sid>.json` puts a **live credential at rest on
disk**, and not briefly: if the marker is later parked, `_park_marker` moves the *whole
marker* into `failed/`, where the tombstone is only cleared if that session later
resolves cleanly — which a parked marker by definition does not. The credential stays
until a human deletes it. That runs against I5/I6, and CI gates on `gitleaks` 8.30.1.

Nothing downstream needs it: `normalize()` discards userinfo (`normalize.py:33,37`,
`rpartition("@")[2]`). The credential is pure liability.

**The hook therefore strips the `userinfo@` span before writing**, with `jq` (already a
hard dependency — no `sed` BSD/GNU portability question):

```
.u | sub("^(?<h>[^/]*://)[^/]*@"; .h)
```

Each part of that pattern is a bug that was demonstrated against the obvious version of
it, so none of the three is incidental:

| Part | Why not the obvious thing |
|---|---|
| `[^/]*://` | A scheme class that excludes `:` (e.g. `[a-zA-Z0-9+.-]+`) never matches a **compound** scheme. `git remote get-url` returns whatever `.git/config` holds with no scheme validation, so a real `origin` can be `git::https://user:tok@host/…` — and `url.<base>.insteadOf` rewriting can produce one at get-url time from a config that looks ordinary. The strict version left that token fully intact. |
| `[^/]*@` | Greedy to the **last** `@` before the first `/`, which is what `normalize()` itself does (`authority.rpartition("@")`). Stopping at the first `@` strips one span of `user:a@b` and leaves the rest of the secret behind. |
| `^` (kept) | Without the anchor, a `://…@` occurring later — in a path or query — would be rewritten instead, mangling a URL that carries no credential at all. |

The scp-like form (`git@github.com:org/repo.git`) has no `://`, does not match, and is
correctly left untouched — its `git@` is a username, not a secret.

**Known limit: a credential outside the userinfo span is not redacted.** A token carried
as a query parameter (`https://host/org/repo.git?token=…`) survives, because stripping a
query would change the URL rather than de-credential it, and the query is part of the path
`normalize()` keys on. This is accepted rather than fixed: git has no native support for
query-string auth, the value never leaves the machine (§3), and CI's `gitleaks` gate
catches such a string if one ever enters a fixture. If a self-hosted host in the pilot is
found to use one, this is the line to revisit.

**Why this is not the shell-normalization mistake §1 forbids.** The two fail in opposite
directions. A normalization bug in shell produces a *plausible-looking wrong key* and
files sessions silently under it. A redaction bug either leaves the token (caught by an
assertion) or mangles the URL so `normalize()` raises `ValueError` → row **R5** below →
`dropped_no_remote`, which is loud and safe. Redaction may fail; it cannot fail quietly
into a wrong project. That asymmetry is the justification.

---

## 2. Authority rule

`cwd` is authoritative. `cwd_remote_url` is a fallback, used **only** when `cwd` no
longer resolves to a remote.

| # | `cwd` resolves? | `cwd_remote_url` | Identity used | Disposition | Diagnostic |
|---|---|---|---|---|---|
| R1 | yes | absent | live | unchanged | — |
| R2 | yes | present, equal | live | unchanged | — |
| R3 | yes | present, **differs** | **live** | unchanged | `attribution_superseded` |
| R4 | **no** | present, normalizable | **captured** | **now proceeds** | `attribution_from_marker` |
| R5 | no | present, not normalizable / not a `str` | none | `dropped_no_remote` | detail `fallback not normalizable` |
| R6 | no | absent | none | `dropped_no_remote` | detail `no git remote origin` (today's string) |

**R3 is the rule this document exists to pin.** A live remote always wins, so the
captured value can never pull a transcript back onto an old project. The
`attribution_superseded` line makes the divergence visible instead of silent. A
genuinely renamed repository therefore keeps working with no intervention.

**R4 is the fix.** Nothing else changes.

**R5 and R6 fail closed.** An absent or unusable identity is recorded with a
distinguishing detail and the marker is dropped — never attributed to a guess. No new
disposition constant is introduced, so `_CATEGORY` (`processor.py:103-119`), `loom
status`'s counters and every existing report assertion are untouched.

**"Equal" and "differs" mean *after* normalization.** R2 and R3 are separated by
`normalize(live) == normalize(captured)`, not by raw string equality — a repo cloned over
HTTPS and captured over SSH spells the same project two ways, and comparing raw would
emit `attribution_superseded` on every such session, which is noise that trains an
operator to ignore the one line that matters. A captured value that does not normalize
counts as **differing** (it cannot be shown equal), so it takes the R3 path: live decides,
one diagnostic, no drop. R5 applies only when there is no live remote to fall back from.

`attribution_from_marker` and `attribution_superseded` are **diagnostics**, not
dispositions: they are `_log` lines, they do not appear in `_CATEGORY`, and they do not
change any counter.

---

## 3. Why the association is trustworthy

Three properties, all of which already hold:

1. **Session-bound.** `cwd_remote_url` is published in the same atomic write (tmp +
   `mv -f`) as the `session_id` and `transcript_path` it describes. There is no window in
   which a marker carries one session's id and another session's remote. This is why the
   association is keyed by **session** and not by path — see §6, A-1.
2. **Never authoritative over live truth.** R3.
3. **Still gated.** The fallback produces a *candidate* `canonical`, which then goes
   through the **unchanged** registry gate (`processor.py`, step 3). An unregistered
   remote is still dropped. The fallback widens *which sessions reach the gate*, never
   *what the gate allows*.

**The capture point is `SessionEnd`** because it is the last moment at which the
session's own working directory is guaranteed to exist, and it is unconditionally before
any processor pass — including the kick the hook itself fires. It is the earliest point
the client controls that does not require installing a new hook type.

---

## 4. Why a wrong attribution is worse than a drop

`server/schema.sql` declares `content_hash TEXT UNIQUE NOT NULL` — unique **globally**,
not per project — and `compute_content_hash` deliberately excludes `project_remote` from
its material. Two consequences:

- **Changing how a project is resolved can never create duplicates.** Attribution is not
  in the hash, so re-processing a fallback-attributed session still yields `409`.
- **A misattribution is terminal.** Once a delta is stored under project A, re-posting
  the identical delta under B returns `409`, which the client treats as success:
  `duplicate`, pointer advanced, marker deleted. There is no "post it to the right
  project instead" recovery, not even manual.

Therefore **any branch that might guess is strictly worse than dropping**: a drop leaves
the pointer untouched and the transcript on disk (recoverable indefinitely), while a
wrong post is permanent. This is why R5 and R6 drop rather than fall back to anything,
and it is the strongest argument for R3 as written.

---

## 5. What this deliberately does NOT change

- **Sweep-synthesized markers carry no `cwd_remote_url`.** They are produced at
  processing time, when the sweep knows nothing the processor does not. Falling through
  to R6 is correct, and the sweep's eligibility precheck keeps its current semantics.
  This is a **checked** property, not a convention: `client/tests/unit/test_sweep.py`
  asserts the sweep's marker key set exactly. If a future change adds the field to
  `_enqueue_marker`, that test goes red — which is the correct outcome. Do not widen the
  set to make it green.
- **`normalize()`** — untouched. It remains the sole identity authority (D1).
- **`state.json`, the pointer, `content_hash`, the `POST /api/entries` payload,
  `okf.wrap_entry`, `contract/SCHEMA.md`, `contract/FLAGS.md`, `prompts/`** — untouched
  (`AGENTS.md` §13).
- **Idempotency.** The fallback changes only *which canonical string* step 2 produces.
  Steps 4-9 are reached exactly as before, so reprocessing still yields `409 duplicate`
  (I6).
- **Session shutdown.** The hook must never be blocked or failed by this feature. A
  missing `git`, a `cwd` that is not a repo, and a `cwd` that no longer exists all
  degrade to "omit the field, log one line, still write the marker, still `exit 0`" —
  the same way a missing `jq` already degrades.

---

## 6. Known limits

These are residuals of this contract, recorded so they are known rather than
discovered.

**L-1 — the sweep path stays uncovered.** A session that dies *without* `SessionEnd`
(crash, SIGKILL, WSL shutdown) in a worktree that is then deleted has no marker and no
captured remote. `sweep._is_eligible` resolves the dead `cwd`, returns ineligible, and
withholds the transcript on every pass. It also silently removes a property the sweep
advertises — that registering a project later makes historical orphans deliverable with
no manual intervention — for every worktree session. The withhold only increments
`ps.counters["ineligible"]`; there is no per-session log line, so there is no `loom
status` row a human would read as data loss. **A per-session diagnostic naming the
unresolvable `cwd` converts that invisible loss into a visible one** without closing the
gap; closing it would require a `SessionStart` hook (§7, A-2).

**L-2 — reused-path misattribution under R3.** When an ephemeral path is reused
(`/tmp/agent-wt-1` deleted for repo A, recreated for repo B) and the live remote resolves
to the *new* repo, R3 files the old session under the new project. This is **pre-existing
behavior**: `_git_remote(cwd)` is trusted unconditionally today. R3 makes it *visible*
(`attribution_superseded`) rather than fixing it. Fixing it would mean letting the
captured value beat live, which §2 forbids for the reasons in §3 and §4.

---

## 7. Alternatives rejected

- **A-1 — persistent `~/.loom/paths.json` ledger (path → canonical remote).** Keyed by
  **path**, which is precisely the key ephemeral worktrees recycle; a reused
  `/tmp/agent-wt-1` would resolve to whatever repo used it last (L-2, but unconditionally
  and invisibly). It also adds a file with its own retention, atomicity and
  concurrent-writer story next to a queue that already has one.
- **A-2 — a `SessionStart` hook.** The only option that would also cover L-1. Rejected on
  cost/benefit for now: a second hook type in `~/.claude/settings.json`, a second
  install/merge path in `install.sh`, a second doctor row, a second uninstall path, and
  `FLAGS.md` has verified payloads only for `SessionEnd`. Worth reopening if L-1 is
  observed in the pilot.
- **A-3 — cache `cwd → remote` in the processor on first successful resolution.** In the
  exact scenario this contract is about, there is no first successful resolution.
- **A-4 — infer the project from transcript content or from the encoded transcript
  directory name.** An explicit non-goal; the encoded directory name is the same dead
  path by construction.
