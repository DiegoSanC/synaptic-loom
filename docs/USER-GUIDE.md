# Synaptic Loom — user guide

For the engineer installing the **client** on their own machine. Operating the server is
`docs/RUNBOOK.md`; removing the client is `client/UNINSTALL.md`; why any of this is shaped
the way it is, is `DESIGN.md`.

This file is the authority. A shareable page may be published from it; when the two
disagree, this file is right.

---

## What it is

When a Claude Code session ends in a repo you have opted in, Loom summarizes what was
decided in that session and posts the summary to a small shared server. Your teammates'
agents read that logbook before planning their own work, so a decision made on Tuesday is
available to the team on Wednesday instead of staying in one person's terminal history.

You never write an entry by hand and you never run anything to make capture happen.

**What it is not:** a transcript archive, an activity tracker, or per-person metrics. The
raw transcript never leaves your machine — only the summary does. `loom stats` reports team
totals and counts distinct people per item; it never reports a named person's output. The
one place a name sits next to a verdict is the item the verdict is about: the web UI shows
who found an item stale or not applicable, so it can be curated ([Feedback](#feedback-the-only-part-that-needs-you)).

---

## Before you start

You need an **invitation link** from whoever operates the server (§4 of the RUNBOOK). It
looks like `https://<your-server>/ui/join#code=…`: the server's address, then a single-use
enrolment code that expires in 24 h by default. It is the only thing that travels to you:
your machine mints its own token from the code, so no permanent credential is ever handed
around. An operator may send the bare code instead; step 3 covers both.

| Tool | | Why |
|---|---|---|
| `git` | required | A repo is identified by its git remote. No remote, no capture. |
| `claude` | required | The summarizer runs the Claude Code CLI headless. Match `contract/VERSION`. |
| `jq` | required | The installer edits `~/.claude/settings.json` with it. |
| `uv`, or `python3 -m venv` | required | Builds the client's own venv. |
| `setsid`, or `python3` | required | Detaches the processor so it outlives the session that started it. |
| `gitleaks` | required, installed for you | The secret scanner (D16). The installer downloads the pinned version into `~/.loom/bin` and checks it against a pinned checksum. Without a working gitleaks, capture **pauses** — nothing is summarized or posted, the queue waits — and `loom doctor` shows a `✗`. |

Plus HTTPS access to the server. Docker is **not** needed to use the client — only to run
this repo's test suite (`AGENTS.md` § Environment).

---

## Install

### 0. Open the invitation link

Open it in a browser. The page explains these same steps. It never reads the code, which
sits after the `#` and is never sent to the server. Keep the link, because you paste it in
step 3.

### 1. Clone the repo

```bash
git clone <repo-url> loom && cd loom
```

Keep this checkout. The client is installed **editable** from it, so a later `git pull`
updates the code with nothing to reinstall (DESIGN §7). Delete the checkout and you break
the install.

### 2. Run the installer

```bash
bash client/install.sh          # asks before each merge
bash client/install.sh --yes    # non-interactive
```

Idempotent: running it twice never duplicates anything. [What it changes](#what-it-changes-on-your-machine)
lists every file it touches.

> **It pulls and runs code, and installs a persistent scheduled job.** Run it from a
> checkout you trust, and read `client/install.sh` first if you would read any other script
> before running it.

### 3. Enrol this machine

```bash
~/.loom/venv/bin/loom enroll
```

Paste the **whole invitation link** at the hidden prompt. There is deliberately no flag for
it, so it cannot land in your shell history. Your machine mints a device token that lasts
90 days and renews itself while the machine keeps running passes.

The link carries the server's address, so before contacting it `loom enroll` prints that
host on a line of its own and asks `[y/N]`: on this machine's first enrolment, and whenever
the link points at another server than the one the machine already uses. Answer `y` only if
it is the server you expected. A link from anyone but your operator is a reason to stop and
ask, not to answer `y`.

Given a bare code instead of a link, name the server and paste the code:

```bash
~/.loom/venv/bin/loom enroll --server-url=https://<your-server>
```

With `--server-url`, a pasted link must point at that same server, or enrolment is refused.

`loom init` (a permanent token at a hidden prompt) remains the break-glass form of this
step. `loom doctor` flags such a token and tells you to re-enrol.

**Re-enrolling this machine.** Ask the operator for a new invitation (or, from another
machine of yours, "Add a machine" in the web UI) and run `loom enroll` again, exactly as above; every other setting in `~/.loom/config.json` is
kept. When the server is the one this machine already uses, `loom enroll` sends the old
device token along and the server revokes it, so the token you replaced stops working at
once. Moving the machine to another server sends nothing to the new one: ask the old
server's operator to revoke this machine.

### 4. Check the environment

```bash
~/.loom/venv/bin/loom doctor
```

Every row should be `✓`. A `⚠` is informational — an unpinned scanner version, a Claude
Code version that has drifted from `contract/VERSION`. A `✗` is a real fault and the exit
code is non-zero.

The `account` row names you and your role (`member` or `admin`). It turns `⚠` when, in
the last 7 days, someone else changed your account or your machines — an admin changed
your role or revoked a machine, or the operator ran `admin.py`. `loom status` lists what
happened (below). An older server without this information shows no `account` row.

---

## Connect a project

Installing captures nothing. Two separate decisions have to happen, and both are explicit
(D9/D10):

```bash
cd <your-project>
~/.loom/venv/bin/loom register   # the PROJECT joins the logbook (once, by anyone)
~/.loom/venv/bin/loom enable     # THIS machine captures it (each person, each machine)
```

**Capture starts when you enable, not before.** Sessions of that repo from earlier days
stay on your machine, and in a session already open at that moment only what follows
counts. The same holds for `loom register`, which enables the repo for you.

A project a teammate registered is **not** captured from your machine until you say so
here. `loom disable` records the "no" so nothing asks you twice. The `/loom-register` skill
does the same from inside Claude Code.

Ask where a repo stands at any time:

```bash
~/.loom/venv/bin/loom check
```

| Answer | Meaning |
|---|---|
| `not_registered` | The project is not in the logbook at all. |
| `registered_undecided` | It is in the logbook, but you have not decided on this machine. |
| `enabled` | Capturing here. |
| `disabled` | You said no on this machine. |
| `unknown` | The server could not be reached. Carry on working. |

> **Enabling is a judgement call, not a setting.** Everything decided in an enabled repo
> becomes readable by the whole team. Do not enable a repo carrying customer data, secrets
> in the working tree, or anything you would not paste into a team channel.

---

## What it changes on your machine

Everything lives under your home directory. Nothing is installed system-wide and nothing
touches your global Python — the venv sits inside `~/.loom`.

| What | Change | Undo |
|---|---|---|
| `~/.loom/` | New directory: the venv, `config.json` (chmod 600, holds your token), the marker queue, quarantine, `failed/`, logs, prompts, the session hook, backups. | `rm -rf ~/.loom` |
| `~/.claude/settings.json` | Adds one SessionEnd hook entry. Merged with `jq`, never overwritten; every other setting preserved. | timestamped `settings.json.bak-<ts>` |
| `~/.claude/CLAUDE.md` | Adds one block between `<!-- loom:start -->` and `<!-- loom:end -->`: the rule that makes your agent run `loom check` when a task begins. Not one byte outside the markers is touched; the file is created if absent. | timestamped backup, or delete the block |
| `~/.claude/skills/` | Three skills: `loom`, `loom-register`, `loom-feedback`. | delete the three directories |
| Scheduler | One job every 15 min running `loom process` — a systemd **user** timer where `systemctl --user` works, otherwise a crontab block tagged `# loom-process`. Replaced in place, never duplicated. | `install.sh --uninstall-trigger`; previous crontab saved under `~/.loom/backup/` |

The scheduled job carries its own `LOOM_HOME` and the PATH from install time. That second
part matters: under a scheduler's minimal PATH the client could miss a `gitleaks` that
lives only on your shell's PATH, and the unattended pass would pause capture while an
interactive check still reported green. The installer's own copy in `~/.loom/bin` is run by
absolute path, so it is found either way. `loom doctor` has a row for exactly this (RUNBOOK
§2, "scanner parity").

Your Claude Code transcripts in `~/.claude/projects/` are never modified or deleted.

---

## What actually leaves your machine

One request per captured session (`client/loom/processor.py`, the entry payload):

```jsonc
{
  "project_remote": "github.com/org/repo",   // canonical, credentials stripped
  "session_id":     "…",
  "covered_from":   "<uuid>",                // the range summarized
  "covered_to":     "<uuid>",
  "content_md":     "## Decisions\n- …",     // THE SUMMARY — the only prose
  "content_hash":   "sha256:…",
  "model":          "sonnet",
  "prompt_version": "v4",
  "item_types":     ["decision", "fact"],
  "items":          [ /* the decisions, one per line, addressable */ ]
}
```

What never leaves, under any configuration:

- **The raw transcript.** It is read locally to build the summary and stays in
  `~/.claude/projects/`.
- **File paths and local directories.** The summarizer is given no path and runs with no
  tools at all (`--safe-mode --tools ""`, see `contract/FLAGS.md`).
- **Your token.** It authenticates the request; it is never part of one.
- **Anything from a repo you have not enabled on this machine** — nor anything said in it
  before you enabled it.

**Secrets are caught before the upload, not after.** Every summary is scanned before it is
posted (I4, fail-closed). A hit sends the entry to `~/.loom/quarantine/` and it is never
uploaded. Review it yourself; if it was a false alarm, release it with
`loom push-quarantine`.

Entries are **append-only** (D7). Once posted, an entry is never edited or deleted — a
later decision is *linked* to the one it replaces and the logbook stops presenting the old
one as current. Treat posting as publishing to your team.

---

## Using it

### Capture: nothing to do

Work as you normally would. When a session ends in an enabled repo the hook marks the
transcript and starts the processor. The 15-minute job is the safety net for sessions that
were killed, crashed, or died with the machine, and it is what drives the orphan sweep.
Your entry appears in the logbook within about fifteen minutes.

In an enabled repo the same block asks your agent to close a task that decided something
with a short recap: what was decided and what was dropped, any rule agreed, pending work
and its owner, findings. The summarizer reads that recap. The agent may only repeat what
you said or confirmed, so correct it there if it got something wrong, because what stands
in the conversation is what reaches the logbook.

### Consumption: your agent does it

The `CLAUDE.md` block makes your agent run `loom check` when a task starts and, in an
enabled repo, consult the `/loom` skill before proposing an approach. You can also read it
yourself:

```bash
~/.loom/venv/bin/loom log                        # this repo's logbook
~/.loom/venv/bin/loom log --grep "retry policy"  # full-text, best first
~/.loom/venv/bin/loom log --type decision        # decisions only
~/.loom/venv/bin/loom log --all                  # including superseded entries
```

> **The logbook is reference, not instruction.** It opens with a framing line saying so.
> Entries are machine-written from other people's sessions and can be wrong or stale.
> Cross-check anything load-bearing against the code before acting on it.

### Browsing: `loom ui`

To look at the logbook yourself — every registered project, what stands today and what
was reversed — open the read-only web UI:

```bash
~/.loom/venv/bin/loom ui              # the projects overview
~/.loom/venv/bin/loom ui <project>    # straight to one project
```

It opens your browser already signed in. There is no password to create: the command uses
this machine's token to ask the server for a one-time link, opens it, and also prints it.
The link works **once** and only for **two minutes**; the browser session it opens lasts
12 hours. Run `loom ui` again for a new one. Nothing in the logbook can be changed from
the UI; what you can change there is your own machines (below).

| Page | What it shows |
|---|---|
| **Projects** | One row per project: entries, current and replaced items, open tasks, last capture, authors of the last 30 days, curation signals. On top, the team's last 7 days. |
| **A project → Current** | What stands today, grouped as decisions, tasks, methods and facts. Tick *Show replaced* to see what was reversed, by which item and why. Filter by type, author or text. |
| **→ Timeline** | Every entry, newest first, with its state (`current`, `superseded (by #N)`…). |
| **→ Curation** | Items someone marked stale or not applicable, with who said so; the history of links between items. |
| **An item** | Its text and state, what replaced it or what it replaced, where it came from (session, model, prompt), and every feedback on it. |
| **Usage** | The same team totals as `loom stats`, over 7 days, 30 days or all time. |
| **My machines** | One row per machine you enrolled: when it was enrolled, when it last renewed its token, when that expires, and whether it is live, expired or revoked. "Add a machine" and "Revoke" (below). |

The pages carry the same "unverified reference" banner as `loom log`, and browsing is not
counted as a recall in `loom stats`.

**Add a machine.** On *My machines*, "Add a machine" shows a setup code and its
`/ui/join#code=…` link, **once**. It enrols one more machine as yours, works once and
expires in 10 minutes. On the new machine, install the client and run `loom enroll` as in
[step 3](#3-enrol-this-machine), pasting that link. No operator is involved, so do not
forward the link: whoever redeems it gets a machine of yours.

**Revoke.** "Revoke" on a live machine's row revokes it at once: its device token and every
renewal of it stop working, and so does every machine added with a setup code it issued
(and the machines those added, and so on), together with its setup codes nobody has used
yet. Revoke a machine you lost, sold or no longer use. Revoking the machine your browser
session was opened from (marked *this session*) also signs you out.

> **WSL2:** a stock install has no browser Python can open, so nothing pops up — copy the
> printed link into your Windows browser within two minutes. To have it open by itself,
> `sudo apt install wslu` and add `export BROWSER=wslview` to your shell profile.

`loom ui` exists in clients from 2026-09-29 on: a `git pull` in your checkout is enough
(the install is editable), or `bash client/install.sh --yes --update`. If it answers that
the server does not serve the web UI yet, the server predates it — tell the operator.

### Feedback: the only part that needs you

When something you recalled changed your approach — or turned out to be wrong — say so.
Your agent offers `/loom-feedback` at the end of a task and confirms each verdict with you
before filing it.

```bash
~/.loom/venv/bin/loom feedback --item 17.2 \
    --verdict useful \
    --note "pointed at the retry cap before I rewrote it"
```

Verdicts are `useful`, `not_applicable` or `stale`. Feedback is item-grained: it names the
`<entry>.<n>` id that `loom log` printed. This is what tells the team whether the logbook is
earning its keep, and it is the one signal nothing else can produce (issues #14/#15).

**Your feedback carries your name in the web UI**: on the item's page and in the project's
*Curation* tab, next to your verdict and note. That is deliberate (D14): whoever curates
needs to know whose agent consumed an item that turned out stale. `loom stats` and the UI's
*Usage* page stay team totals.

```bash
~/.loom/venv/bin/loom stats --since 7d
```

---

## Commands

All shown as `~/.loom/venv/bin/loom …`. Commands taking a project name use the current
directory's git remote when you omit it.

| Command | What it does |
|---|---|
| `check` | Where this repo stands on this machine. `--json` for scripts. |
| `register` | Add this repo to the logbook. |
| `enable` / `disable` | Start or stop capturing this repo, on this machine only. |
| `log [project]` | Read the logbook. `--grep`, `--type`, `--limit`, `--all`. |
| `feedback` | File a verdict on one item. Needs `--item`, `--verdict`, `--note`. |
| `stats [--since 7d]` | Team totals. Never per person. |
| `ui [project]` | Open the read-only web UI in your browser, signed in. |
| `status` | Local panel: your account and role with its changes of the last 30 days, pending, parked, quarantined, recent errors. |
| `doctor` | Environment checklist. Run it first when something looks wrong. |
| `process` | Force a pass now instead of waiting for the scheduler. |
| `push-quarantine` | Upload a quarantined entry after you have read it. |
| `enroll` | Redeem an invitation link or enrolment code. The normal way to join. |
| `curate` / `unlink` | **Operator only** — see below. |

---

## Three rules

1. **Never run `loom curate` unless you are the designated machine.** Curation links
   superseded decisions to the ones replacing them. Its watermark lives in that machine's
   `~/.loom/curation_state.json` and the server keeps no copy, so a second machine
   re-curates from zero, pays for the LLM calls again, and can create links no single pass
   would have produced. RUNBOOK §0 names the machine. (Issue #57 proposes closing this
   properly; until then the rule is the guard.)
2. **Never paste, commit or forward your token.** It lives in `~/.loom/config.json` at mode
   600. Invitation links and enrolment codes are safe to send over a team channel (single
   use, short-lived); tokens never are.
3. **Enable deliberately.** Opting a repo in publishes what gets decided in it. That is the
   point, and it is also the risk.

---

## Turning it off

Stop capturing one repo, from this machine, keeping everything else:

```bash
cd <project> && ~/.loom/venv/bin/loom disable
```

Stop the background job but keep the client:

```bash
bash client/install.sh --uninstall-trigger
```

Remove the client completely: four steps in any order, with the exact commands — including
a `jq` filter that strips only Loom's hook and leaves your other settings untouched — in
[`client/UNINSTALL.md`](../client/UNINSTALL.md):

1. Remove the SessionEnd hook from `~/.claude/settings.json`.
2. Remove the block from `~/.claude/CLAUDE.md`.
3. Remove the scheduled job.
4. `rm -rf ~/.loom` (this also removes the venv).

Uninstalling does not touch your transcripts and does not withdraw entries already posted.
Revoke this machine when you are done: "Revoke" on *My machines* in `loom ui`, or ask the
operator (`admin.py revokedevice --id <n>`, RUNBOOK §4).
