# Phases 2 and 3 explained — a guide with examples

A detailed explanation of each requirement (R) in `PHASE-2-3-SPEC.md`, with step-by-step
examples. A companion document; the normative spec is `PHASE-2-3-SPEC.md`.

> **Settled since this was written** (read the rows in `DESIGN.md` §1 before acting on a
> "pending" below): D14, the web UI's login (Q-3.3); D16, gitleaks mandatory with no
> regex-only fallback (R2.3); D17, the client address the API's rate limits and audit
> read (Q-3.8); D18, backups for the Docker Compose topology (R3.1); D20, global roles
> and the web's account writes (Q-3.4 in part; per-project permissions stay open).

---

## 0. Mini-glossary (the terms used throughout this document)

- **Transcript**: the JSONL file where Claude Code records, line by line, everything that
  happens in a session (your messages, its replies, the tools it used). It lives at
  `~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`.
- **SessionEnd hook**: a script Claude Code runs automatically when a session ends. Ours
  does two things in <100 ms: it writes a *marker* and launches the *processor*
  ("kick").
- **Marker**: a small JSON file in `~/.loom/queue/` meaning "this session has content
  pending review". It is the processor's to-do list.
- **Processor**: the script (`loom process`) that consumes markers: it decides whether the
  session belongs to a registered project (the *gate*), extracts what is new in the
  transcript, calls the *summarizer*, scans for secrets and uploads the entry to the
  server. It is not a daemon: it runs and exits.
- **Pointer**: a local record (`~/.loom/state.json`) of the line up to which each
  transcript has already been processed. It is what prevents summarizing the same thing
  twice when you resume a session with `claude -r`.
- **Summarizer**: a fresh `claude -p` session (Sonnet model, no tools, no disk or network
  access) that receives the new text of the conversation and returns a summary of the
  decisions made and their rationale — or the word `NO_ENTRY` if there was no decision
  worth recording.
- **Entry**: a session's summary once uploaded to the server. Since D7 every entry is an
  "OKF concept": markdown with a YAML header (frontmatter) carrying its provenance
  (session, covered range, model, date, status).
- **Logbook**: the set of entries of a project. What the reviewer consults with
  `/loom <project>`.
- **Pilot gate**: the ~3-week evaluation (until ~2026-08-14) in which the reviewer notes
  whether the logbook saved them time. Its verdict decides whether phase 3 gets built and
  with what scope.
- **Quarantine**: a local folder where an entry goes if the scan detects a possible
  secret. Nothing is uploaded; it is reviewed by hand with `loom push-quarantine`.

---

## PHASE 2 — Robustness (does not depend on the gate)

### R2.1 — Orphan sweep

**The problem.** The SessionEnd hook only fires on clean exits (`/exit`, the end of a
`claude -p`…). If the session dies abruptly — you close the terminal window, WSL
crashes, a `kill -9` — there is no hook, no marker, and that session **is lost forever**
even though its transcript is intact on disk. In phase 1 we accepted this as a known
loss.

**The idea.** The transcript IS the source of truth and is still on disk. A periodic
sweep compares what is on disk with what the pointer says has already been processed; if
it finds a transcript with unprocessed content and no marker, it creates a **synthetic
marker** for it (tagged with `"origin": "sweep"` to tell it apart in the logs) and the
normal pipeline does the rest.

**Step-by-step example.**
1. Monday 10:00 — you work on project B (registered) and make the decision "we use
   Redis for the session cache".
2. Monday 10:40 — Windows restarts for an update and takes WSL down with it. There was
   no SessionEnd → there is no marker. Today, that decision would never reach the
   logbook.
3. Tuesday 09:00 — you turn the machine on. The sweep runs (launched by the R2.2 timer),
   walks `~/.claude/projects/`, and finds Monday's transcript: its last modification was
   >30 min ago (the "settle" threshold — it avoids processing sessions that are still
   open) and the pointer says "processed up to line 0".
4. The sweep reads the `cwd` field **from inside the transcript** (every line carries
   it) — it does not trust the encoded folder name — and creates the synthetic marker.
5. The processor runs its normal pipeline: gate (is B registered? yes) → delta →
   summarizer → scan → push. The Redis decision shows up in the logbook a day late, but
   it shows up.
6. If the sweep runs again, nothing happens: the pointer has already advanced
   (idempotency).

**Acceptance criterion in plain words:** kill a session ruthlessly → its entry still
ends up on the server, and repeating the sweep creates no duplicates.

**Does it tell an open session from a closed one?** Not with certainty — it uses a
heuristic: an active session writes to its transcript constantly (every message updates
the mtime), so "30 min without writes" ≈ "there is no session behind it". The false
positive (a session that is open but idle for >30 min, e.g. you went to lunch) is
harmless by design: the partial content is summarized, the pointer captures the rest at
the real close, `NO_ENTRY` filters out partials without decisions, and the worst case is
a conversation split into two entries — never duplicates or loss. Stronger signals
(`lsof` on the file, a SessionStart sentinel) were considered and discarded in v1:
coupling to the platform to eliminate a failure that is already cheap.

**Related open question (Q-2.2):** what remains open is only the VALUE of the threshold
(30 min?) and whether the first sweep should also "rescue" old sessions from before the
sweep existed.

---

### R2.2 — Periodic trigger (timer)

**The problem.** Today the processor only runs when (a) you close a session (kick) or
(b) you launch it by hand. Consequence: a marker left pending (e.g. the server was down
right at close) waits **until your next session close**, which may be the next day. And
the R2.1 sweep needs something to run it regularly.

**The idea.** A system timer runs `loom process` every 15 minutes (which in turn includes
the sweep). On machines with user-level systemd a *systemd user timer* is used (the
modern version of cron: you declare "run this every 15 min" and systemd takes care of
it); elsewhere, a classic *cron* (the Unix task scheduler). The installer sets it up and
`loom doctor` checks that it is active.

**Step-by-step example.**
1. Friday 18:00 — you close your last session. The kick runs, but the server is down
   (VM restart). The marker is kept (error matrix: network failure = cheap infinite
   retry, without consuming attempts).
2. Without a timer (today): that marker would wait until your next session close —
   perhaps on Monday.
3. With a timer: at 18:15 the timer launches `loom process`, the server is back, the
   entry is uploaded. Nobody did anything.

**WSL2 nuance** (why this was not in phase 1): in WSL the "computer" (the VM) shuts down
when you close all terminals, and a timer that is off does not fire. That is why the
pilot used the kick (P1). The timer is an *addition* to the kick, not a replacement — it
covers the gaps while the VM is on, and its limitations are documented in the RUNBOOK.

**Acceptance criterion:** with the server down, close a session; bring the server up;
the entry appears on its own within ≤15 min.

---

### R2.3 — Full secret scanning (gitleaks)

**The problem.** Before uploading anything, the processor scans the summary looking for
secrets (API keys, passwords…). Today it uses its own set of regular expressions that
recognize *known* formats: `AKIA…` (AWS), `sk-…` (OpenAI), `ghp_…` (GitHub), PEM
blocks, `password=`… Its blind spot: secrets **with no recognizable format** — an
internal token of your organization such as `x7Kp2mQv9…` matches no regex.

**The idea.** Integrate **gitleaks**, the industry-standard open-source scanner, which
on top of hundreds of rules uses **entropy detection**: it measures how "random" a string
is (a cryptographic token has a statistical randomness that ordinary words do not) and
flags the suspicious ones. Our own regexes stay as a fallback if gitleaks is not
installed, and `loom doctor` tells you which engine is active.

**Step-by-step example.**
1. While debugging project A you paste an internal token into the conversation:
   `INTERNAL_TOKEN=x7Kp2mQv9rT4wY8zB3nF6jH1`.
2. The summarizer, despite the prompt rule ("never include credentials"), reproduces it
   in the summary — LLMs sometimes fail; that is why the deterministic scan exists
   (defense in depth, invariant I4).
3. Today: the regexes do not recognize that format → the token **is uploaded to the
   server**. A leak.
4. With R2.3: gitleaks detects it by entropy → the entry goes to quarantine → you review
   it with `loom push-quarantine`, delete the token, upload the clean version.
5. Fail-closed intact: if gitleaks breaks (a bug, a broken binary), it is treated as if
   it had found something — to quarantine. Never "I couldn't scan, I'll upload it
   anyway".

**Acceptance criterion:** a high-entropy token that the regexes miss ends up in
quarantine when gitleaks is active.

---

### R2.4 — Installer polish

**The problem.** Two everyday operations are manual today: installing the R2.2 timer
does not exist, and **updating** the client on a machine requires knowing what to
re-copy (the code comes along through the editable install, but the hook, the prompt and
the skills are copies).

**The idea.** `install.sh` also installs the timer, and gains an `--update` mode:
`git pull` + re-copy hook/prompt/skills + a verifying `loom doctor`. One command.
`loom doctor` learns three new checks: is the timer active?, when did the last sweep
run?, which scan engine is present and in which version?

**Example.** We publish an improvement to the summarizer prompt. Today: every dev has to
remember to `git pull` **and** to copy the prompt to `~/.loom/prompts/` (otherwise they
keep using the old one without knowing it). With R2.4: `bash client/install.sh --update`
and doctor is green. Done.

**Acceptance criterion:** a from-scratch install and an in-place update both leave
`loom doctor` fully green.

---

## PHASE 3 — Full team (conditional on the gate)

### R3.1 — Server exposure (TLS / move off localhost)

**The problem.** The pilot server listens only on `127.0.0.1` — the "localhost" of YOUR
machine. Dev2 and Dev3 literally cannot connect. And as soon as the server is reachable
over the network, the traffic carries tokens and conversation content → **TLS is
mandatory** (the encryption in HTTPS; the policy in DESIGN §8 forbids plain HTTP off
localhost).

**The two options (pending decision, Q-3.1):**
- **Internal VM + Caddy**: the server moves to its own VM; Caddy is put in front of it (a
  web server acting as a *reverse proxy*: it receives the HTTPS, manages the
  certificate, and forwards to the internal uvicorn).
- **Tailscale serve**: if the team uses Tailscale (a mesh VPN), it exposes the service
  encrypted inside the private network by itself, with no certificates to manage.

It includes the **migration** from your WSL: stop → copy `loom.db` and backups → start on
the VM → each client re-runs `loom init` with the new URL. Whether the tokens are also
rotated during the move is Q-3.2.

**Example.** Dev3 has to review your project. Today: `loom log` from their machine →
connection refused. With R3.1: their config points to `https://loom.example.internal`,
the `loom log` travels encrypted, and your Monday entry shows up in their terminal.

**Acceptance criterion:** `loom log` works from a second machine over TLS and the pilot
data survives the migration.

---

### R3.2 — Minimal web UI

**The problem.** Everything goes through the CLI today. That works for us; it does not
work for comfortably browsing the history, nor for adding users without touching
`admin.py` on the server, nor for the future teammate who only wants to *read*.

**The scope** (deliberately minimal): login → list of projects → logbook browsable by
project/date/author → register a project by hand (the same duplicate check as the CLI:
if it was already there, it tells you by whom and when) → merge no-remote paths (see
R3.3) → token management (create/revoke).

**The three definition gaps ahead of it** (which is why the spec sends it to review
before building):
- **Q-3.3 — log in with what?** The system only knows *bearer tokens* (a secret string
  the CLI sends with every request: `Authorization: Bearer <token>`). There are no
  passwords and no sessions. Does the UI ask you to "paste your token" (simple, ugly), or
  do we build sessions/SSO (expensive)? *Hybrid candidate (2026-07-29):* the UI login
  validates your opaque token and issues a **short-lived signed session** for the browser
  (a signed cookie, or a short JWT — with a TTL of minutes/hours its non-revocability no
  longer matters). The CLI/processor is untouched. **JWT as the API credential was
  ruled out** (analysis in Q-3.2 of the spec): it cannot be revoked instantly without
  reintroducing state on the server, and its expiry would break the unattended processor
  (a nightly push getting 401 until a human "logs back in").
- **Q-3.4 — who is admin?** Revoking someone else's token or merging paths should not be
  something just anyone can do… but the DB schema has no roles.
- **R3.2.3 — build AFTER the OKF visualizer (R3.5):** if the visualizer already solves
  "browsing the logbook", the UI shrinks to login+registration+merge+tokens.

**Example.** A fourth dev joins. Without a UI: you SSH into the server, run
`admin.py adduser`, pass them the token over a secure channel, and they configure the
CLI. With a UI: you create their user in two clicks, and they browse the logbook of the
project they are inheriting from the browser before even installing the client.

---

### R3.3 — Path registration + merge (repos without a remote)

**The problem.** A project's identity is its normalized **git remote**
(`github.com/org/b`), which makes the same repo cloned on three machines ONE project
without configuring anything. But a repo **without a remote** (a local prototype that was
never pushed) has no such identity: today `loom register` tells you "there is no remote"
and exits with error 1. Its sessions are not captured.

**The idea.** The plan B designed in phase 1 and deferred: register by **path** (the
`project_paths` table already exists in the schema). Since the path differs on each
machine (`/home/ana/protos/x` vs `/home/dev3/experiments/x`), the UI offers the
**merge**: "these two paths are the same logical project". The processor's gate learns
to consult registered paths when it cannot resolve a remote.

**Step-by-step example.**
1. You prototype `~/protos/rules-engine` with no remote. `loom register` → offers to
   register it by path as project "rules-engine". Your sessions there start being
   captured.
2. Dev3 receives a copy and puts it in `~/experiments/rules-engine`. They register their
   path. Right now the server sees TWO projects.
3. In the UI: merge both paths → a single logical project. The entries from both of you
   end up in the same logbook, and `/loom rules-engine` from either machine sees
   everything.

**Important note:** this is the *fallback*, not the main mechanism. As soon as the repo
has a remote, the right identity is the remote's.

---

### R3.4 — Curation digest (logbook staleness)

**The problem — the subtlest one in phase 3.** The logbook is *append-only* (things are
only added, never deleted or modified — on purpose: it is a historical record with a
unique `content_hash`). But decisions expire: in March you decided "SQLite"; in June,
"we are migrating to Postgres". Both entries coexist, and a reviewer who reads the March
one may believe something is still in force when it no longer is. With months of
accumulation, the logbook loses exactly what makes it valuable: trust.

**The idea.** A periodic **curation** pass: another `claude -p` with the same security
profile as the summarizer (no tools, input via stdin, fail-closed scan) reads a project's
recent entries and produces a **digest**: "current state of the decisions", marking which
ones were **superseded** and by which.

**The definition gap (Q-3.5), with the three options on the table:** the
`status: current` state lives inside the frontmatter of an immutable `content_md`.
Marking "superseded" requires choosing a mechanism:
1. A mutable `status` column + a PATCH endpoint → simple, but breaks the append-only
   contract.
2. A table of supersession links (`entry 12 superseded_by entry 47`) → preserves
   immutability; the entries do not change, the relationship lives separately.
3. Digest-only: the digest is just another entry that declares the state; nothing is
   marked → maximum purity, but consumers have to read the digest to know what holds.

**Step-by-step example.**
1. March, entry #12: "**SQLite as the store** — no concurrency expected. Status:
   current".
2. June, entry #47: "**We are migrating to Postgres** — 3 machines writing. Status:
   current".
3. The curation pass runs (e.g. weekly), detects that #47 reverses #12, and produces
   the digest: "Store: **Postgres** (#47). Note: #12 (SQLite) superseded by #47".
4. Dev3 reviews a persistence change: `/loom` shows them the curated state first —
   impossible to mistake the March decision for the current one.

**Acceptance criterion:** after a session that reverses an earlier decision, the digest
marks it superseded and links the replacement; `loom log` distinguishes the two.

---

### R3.5 — OKF export + static visualizer

**The problem/opportunity.** Since D7 every entry is already an **OKF** concept (Open
Knowledge Format, Google's open format: markdown files with a YAML header that any agent
can ingest directly, with no SDK). But today they live as rows in the database,
accessible only through our JSON API. Anyone who wants to consume them needs our CLI.

**The idea.** Two pieces:
- **`GET /api/projects/{id}/okf`** returns the project's **bundle**: a directory (a zip?
  per-file paths? — Q-3.6) with one `.md` per entry plus a generated `index.md`. Thanks
  to D7 this is almost literal: write the `content_md` values to files.
- **Google's static visualizer** (a self-contained HTML file, with no backend, that
  renders the bundle as a browsable graph with search and filters) served over that
  bundle → a *read-only* frontend for free. It is the piece that can shrink the R3.2 UI.
- The same authentication as the API (a bundle contains all of the project's
  knowledge).

**Example 1 — human:** Dev2 wants to skim project C's history before inheriting it. They
open the visualizer → a graph of decisions by date, filter by `tags: ["c"]`, click a
decision and read its evidence. Zero CLI.

**Example 2 — agent (the beauty of the format):** the data team has its own Gemini
agent. Knowing nothing about Loom: it downloads the bundle, `cat`s the files, and its
LLM already has the decisions with provenance. The format IS the contract — there is no
need to integrate with our API.

**Acceptance criterion:** an agent without Loom tooling ingests the bundle; the
visualizer renders the graph; without a token there is no bundle.

---

### R3.6 — Team onboarding

**The problem.** Everything above is infrastructure; the real goal of the project is for
the team, whose members review each other's projects, to actually use it.

**The idea.** Bring the rest of the team on board: a user + token for each one, the
client installed on their machines (`install.sh` + `loom init` + `loom doctor`), and each
of them registering ≥1 real project — respecting the security policy: **nothing that
processes real customer data** without compliance sign-off (DESIGN §8). The RUNBOOK is
rewritten from "single-machine pilot" to "team deployment".

**Example — the complete flow, end to end at last:**
1. On Tuesday Dev2 works on project B: "we chose content-hash idempotency in the webhook
   because the provider resends duplicates". They close the session → an entry in the
   logbook, on its own.
2. On Thursday it is your turn to review their webhook PR. In your review session: `/loom
   project-b` → you see the decision and the rationale **before opening the diff**.
3. The question you were going to ask them on Slack ("why content-hash and not the
   provider's ID?") is already answered in the entry's evidence. You review in 20 min
   what used to take 45.

That point 3 is the project's whole thesis. Everything else exists so that this moment
happens reliably.

---

## How they relate (dependencies, one sentence each)

- **R2.2 runs R2.1**: the timer is what launches the sweep regularly.
- **R2.4 installs R2.2**: the installer sets up the timer.
- **R3.2 waits for R3.5**: the visualizer first; the UI covers only what it cannot
  (login, registration, merge, tokens).
- **R3.5 builds on D7 (phase 1)**: the export is trivial because the entries are already
  OKF.
- **R3.4 needs Q-3.5 resolved before it can be designed**: without a supersession
  mechanism there is no curation.
- **R3.6 needs R3.1**: without an exposed server there is no team to connect.
- **All of R3.x is conditional on the gate** (spec §6); all of R2.x goes ahead now.
