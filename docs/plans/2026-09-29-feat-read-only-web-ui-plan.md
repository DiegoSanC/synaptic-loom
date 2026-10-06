---
title: "feat: Read-only web UI for the logbook, entered with `loom ui`"
type: feat
date: 2026-09-29
complexity: high
decision: D14
resolves: PHASE-2-3-SPEC Q-3.3 (UI login); R3.2 read-side scope
baseline: e9805d1
branch_from: main (e9805d1)
branch: feat/read-only-web-ui
---

# feat: Read-only web UI for the logbook, entered with `loom ui`

## Overview

A team member opens a browser and sees, per project, what Loom holds and the state of
each piece of knowledge — without a terminal, without an agent, and without being able to
change anything. Access is limited to people already enrolled: the CLI that holds a
machine's device token asks the server for a single-use login code and opens the browser
with it.

Owner decisions of 2026-09-29, recorded as **D14** in `DESIGN.md` §1:

| # | Decision | Consequence here |
|---|---|---|
| 1 | The UI **helps the pilot**; it is built now, not after the verdict (pilot scope) | This plan is routable immediately; the pilot scope gains one line (slice 7) |
| 2 | Registered as a **new design decision** (D14) | The new routes and tables are a raised decision, not a local choice (AGENTS.md rule 13); §6.2 and §6.3 are amended in slice 7 |
| 3 | Browsing the UI **is not a recall** | No UI request writes `recall_events`; asserted (AC-9) |
| 4 | Item-level feedback **shows who gave it** | Lets the curator see which developer's agent consumed stale knowledge. Aggregate views (`/ui/usage`, the overview strip) stay person-free, as `feedback.stats` is today |
| 5 | Browser session **~12 h**; revoking a device **kills its sessions** | `ui_sessions.expires_at = min(now + 12 h, device token expiry)`; checked on every request |

### Vocabulary (used below, defined once)

- **Login code** — random, high-entropy, stored as `sha256`, valid 120 s, single use.
  The only secret that reaches the browser from the CLI. Same shape as an enrolment code.
- **UI session** — a server-side row (`ui_sessions`) identified by a random session id
  stored as `sha256`; the browser holds the plaintext id in a cookie.
- **Served status** — `current | superseded | duplicate`, derived on every read from
  *active* links (`entry_links` for entries, `item_links` for items). Authoritative per
  D12; the frontmatter's `status:` literal is never shown as the state.
- **Replaced** — an item whose served status is `superseded` or `duplicate`.
- **Digest entry** — an entry whose leading frontmatter says `type: digest`
  (`okf.DIGEST_TYPE`). That field is FROZEN as "the ONLY thing that tells it apart"
  (`client/loom/okf.py`), and `curate.is_digest` reads it the same way, so the UI does
  too. (The plan first said "`prompt_version` prefix `digest-`"; corrected in slice 2.)
- **Curation signal** — a `current` item with at least one `stale` or `not_applicable`
  feedback row and no active incoming item link.

## What exists already (verified on `main` at `e9805d1`)

- `server/app.py: auth_middleware` — an **exact-path allow-list** (`PUBLIC_PATHS`);
  everything else needs `Authorization: Bearer`. `AUTH_FAILURES` (10 failures / 60 s per
  `Fly-Client-IP` → `429`) and `_audit_auth` (outcome, UTC timestamp, validated IP, no
  secret) are reusable as they are.
- `server/auth.py` — enrolment codes and device tokens, `sha256` storage, `BEGIN
  IMMEDIATE` redemption, `ensure_auth_schema` idiom; `lookup_bearer` reports
  `device_id`, `expires_at`, `revoked_at`, `active`.
- Read substrate the pages reuse, unchanged: `items.items_for_entries` (items with
  `status`, `links_in`/`links_out` including revoked links, author names, revoke
  reason), `links.links_for_entries` (entry status), `search` FTS (`q`),
  `feedback.stats` (person-free totals).
- `recall_feedback` has `user_id`, `verdict`, `note`, `created_at` — enough to name the
  person on item pages with a join on `users`.
- `Dockerfile` copies `server/` verbatim (`COPY server/ /app/server/`) and installs deps
  with `uv sync --locked --extra server`: templates and static files under `server/`
  ship without packaging changes; new deps need the `server` extra and `uv.lock`.
- Uvicorn runs with its **access log on** (no `--no-access-log` in `CMD`) and logs the
  path **with its query string** — the reason the login code travels in the URL
  fragment.
- Nothing exists for browser sessions, HTML rendering or markdown rendering.

## Design

### Architecture

```
loom ui (CLI, holds device token)                    API process (one Fly Machine)
  1. POST /api/ui-login   Bearer <device token> ───► mint login code (120 s, 1 use)
     ◄── 201 {code, expires_at}
  2. open <server_url>/ui/login#code=<code>
                                   browser ───────► GET /ui/login        (public HTML)
                                                    GET /ui/static/login.js (public)
  3. login.js reads the fragment, erases it,
     POST /ui/session {code} ─────────────────────► burn code, create ui_sessions row
     ◄── 204 + Set-Cookie: loom_ui=…               (12 h, ≤ device expiry)
  4. GET /ui/…  Cookie: loom_ui ──────────────────► session → user active? device not
     ◄── server-rendered HTML                       revoked? → render from SQLite
```

Server-rendered HTML in the same FastAPI app (Jinja2 templates, one CSS file, one small
script used only by the login page). No SPA, no build step, no second deployment.

### Server — schema (additive, `ensure_ui_schema` in a new `server/ui_auth.py`, called from `db.init_db`)

```sql
CREATE TABLE IF NOT EXISTS ui_login_codes (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  device_token_id INTEGER REFERENCES device_tokens(id),   -- NULL for a legacy token
  code_hash TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  redeemed_at TEXT
);
CREATE TABLE IF NOT EXISTS ui_sessions (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  device_token_id INTEGER REFERENCES device_tokens(id),   -- inherited from the code
  session_hash TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ui_sessions_device ON ui_sessions(device_token_id);
```

`schema.sql` is not touched. Presence check first, never commits a transaction it did not
open — the `auth.ensure_auth_schema` rules.

### Server — routes

| Route | Auth | Behaviour |
|---|---|---|
| `POST /api/ui-login` | Bearer (device or legacy) | `201 {"code", "expires_at"}`. The client builds the URL from its own `server_url` (the server does not know its public origin behind Fly). Audited `ui_login_code_issued user_id=… device_token_id=…` |
| `GET /ui/login` | public | Static HTML: "Signing you in…", loads `/ui/static/login.js`. No code is ever read server-side from this request |
| `GET /ui/static/login.js` · `GET /ui/static/favicon.svg` · `GET /ui/static/ui.css` | public | Served by explicit routes, **not** a `StaticFiles` mount — the allow-list stays exact paths (the rule in `auth_middleware`'s docstring) |
| `POST /ui/session` | public, shares `AUTH_FAILURES` | Body `{"code"}`. Redemption in one `BEGIN IMMEDIATE` transaction (the `auth.redeem` shape): unknown, expired, redeemed, deactivated-user **or revoked-device** codes are one `401 invalid_code`, count as a failed authentication, audited `ui_login_invalid_code`. Success: `204`, `Set-Cookie: loom_ui=<id>; HttpOnly; Secure; SameSite=Strict; Path=/ui; Max-Age=<s>`, audited `ui_session_opened user_id=… device_token_id=… ui_session_id=…` |
| `GET /ui/…` pages (below) | cookie | See middleware |

### Server — middleware (one new branch in `auth_middleware`)

1. `PUBLIC_PATHS` gains exactly `/ui/login`, `/ui/session`, `/ui/static/login.js`,
   `/ui/static/favicon.svg` (slice 1) and `/ui/static/ui.css` (slice 3, so the login
   and signed-out pages are styled too). `/ui/session` is gated by
   `_too_many_auth_failures` like `/api/enroll`; the others share the health allowance
   (`HEALTH_REQUESTS`). The favicon is public and linked from every page because a
   browser that finds no icon link asks for `/favicon.ico`, which the API branch answers
   `401` **and counts as a failed authentication**: ten page loads a minute would
   throttle the reader (found while implementing slice 1).
2. Any other path equal to `/ui` or starting with `/ui/`:
   - method not `GET`/`HEAD` → `405`, before any credential is read;
   - a `Bearer` header is **ignored** here (the browser credential and the machine
     credential stay separate);
   - no `loom_ui` cookie, unknown hash, `expires_at <= now`, user inactive, or
     `device_token_id` revoked → `401` HTML page ("Your session has ended — run
     `loom ui` in a terminal") through `_reject_auth` (counts, audited with outcomes
     `ui_no_session`, `ui_invalid_session`, `ui_expired_session`,
     `ui_deactivated_user`, `ui_revoked_device`). Same `401` whether or not the page
     exists, like the API's unknown paths.
3. `/api/*` keeps its current branch **byte for byte**: a `loom_ui` cookie is never
   consulted there.

`request.state.user_id` / `user_name` are set as today, so templates can say who is
signed in.

### Server — response headers on every `/ui` response

`Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self';
img-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none';
frame-ancestors 'none'` · `Referrer-Policy: no-referrer` ·
`X-Content-Type-Options: nosniff` · `Cache-Control: no-store` · HSTS (already global).

### Server — rendering untrusted markdown (I5)

`content_md` and `text_md` are LLM output over transcripts: untrusted. One function,
`server/ui_render.py: render_md(text) -> Markup`, is the **only** producer of markup
marked safe:

- `markdown-it-py`, `commonmark` preset, **`html=False`** (raw HTML is escaped);
- link validation left on (rejects `javascript:`, `vbscript:`, `file:`, `data:`);
- images rendered as their alt text plus the URL as text, never an `<img>` (no beacon
  even if CSP were relaxed later);
- Jinja2 `autoescape=True` for every template; no other `|safe` in any template.

Frontmatter is split off before rendering and shown as a key/value table in which
`status` is replaced by the **served** status (`superseded (by #N)`), the D12 overlay
`loom log` already applies.

### Server — pages (all `GET`, all read-only, all person-scoped as described)

Every page carries the I5/D6 banner: *"Automatically generated from developers' Claude
Code sessions. Unverified reference — check it against the code and the PR."* UI chrome
is English, like the CLI; captured content is shown in the language it was captured in.

**A. `/ui/` — Projects.** One row per project, sorted by last capture (newest first):
name and `canonical_remote`; registered by / at; entries (digests excluded); items
current / replaced; open tasks (current items of type `task`); last capture (date,
author); authors active in the last 30 days; curation signals (count). Header strip:
`feedback.stats` over the last 7 days — recalls by source, feedback by verdict (no
names). Empty state: "No project registered yet — run `loom register` in a repository."

**B. `/ui/projects/{id}`** — three tabs, selected with `?tab=` (`current` default,
`timeline`, `curation`); every filter is a query parameter so a view can be linked.

- **`current`** — the logbook as of now.
  - *Digest panel* (when a digest exists): latest digest id, date, the entry range it
    covers, and "K entries captured since" when K > 0 (the #7 staleness disclosure).
    Links to the entry page.
  - *Items*, grouped in this order: `decision`, `task`, `method`, `fact`. Each row:
    public id (`17.2`), rendered text, author and date of its entry, feedback counts per
    verdict, an "unlabelled" marker when `labelled = 0`.
  - Filters: `type`, `author`, `q` (the existing `entry_items_fts` search).
  - `show_replaced=1` adds replaced items, visibly struck, each followed by
    "→ replaced by 21.1 · Ana · 2026-09-25 · <rationale>" (or "duplicate of …"), from
    the active link.
  - *Entries without items* (pre-D13): count and list with their entry status, each
    linking to its entry page.
  - Digest entries never appear in the item list.
- **`timeline`** — non-digest and digest entries, newest first, 30 per page
  (`?before=<id>` pagination): title (frontmatter `title`), author, date, model,
  prompt version, items "k current of n", entry status. Filters: `author`, `since`.
- **`curation`** — the curator's inbox.
  1. Curation signals: each item with its feedback rows **named** (person, verdict,
     note, date).
  2. Un-itemized entries whose status is `current`.
  3. Item-link history, newest first, including revoked links (who revoked, when, why).

**C. `/ui/items/{entry_id}.{ordinal}`** — text, type, `labelled`, served status.
*Lineage*: the chain of active `supersedes`/`duplicates` links walked both ways from
this item (bounded at 50 hops; cycles are unrepresentable, `items.create_link`).
*All links*, active and revoked, with author, rationale and revocation. *Provenance*:
entry id, author, `created_at`, `session_id`, `covered_from`/`covered_to`, model, prompt
version. *Feedback*: every row, **named** (decision 4). Unknown id → `404` page.

**D. `/ui/entries/{id}`** — frontmatter table with the served-status overlay, the
rendered body, and the entry's items with their ids and statuses.

**E. `/ui/usage`** — `feedback.stats` for `?since=7d|30d|all`: recalls by source,
feedback by verdict, top-10 useful items linking to their item pages. Person-free.

All reads go through a new `server/ui_queries.py` that composes the existing substrate
(`items.items_for_entries` in chunks of `items.MAX_ENTRY_IDS`, `links.links_for_entries`,
`feedback.stats`) plus the few aggregates above. **No page calls `recall.record`.**

### Client — `loom ui`

`loom ui [project]` (positional, the idiom of `loom log` / `loom feedback`):

1. Loads `config.json`; `POST /api/ui-login` through `ApiClient` (new method
   `ui_login()`), sharing the loud-401 handling of the other commands
   (`auth_rejected.json`, the `loom enroll` hint).
2. Builds `<server_url>/ui/login#code=<code>` (with `&next=/ui/projects/<id>` inside the
   fragment when a project name resolves; `login.js` only follows a `next` that starts with
   `/ui/`).
3. Calls `webbrowser.open(url)` and **always** prints: "Opening Loom in your browser. If
   nothing opened, open this link within 2 minutes (it works once): <url>". The code is
   never an argv of any process the CLI spawns other than the browser, never logged to
   `~/.loom/`, never written to disk.

Why print it: `webbrowser` finds no browser on a stock WSL2 (the curation machine). The
RUNBOOK documents `BROWSER=wslview` (from `wslu`) or `BROWSER=explorer.exe`. A code in
terminal scrollback is single-use and dies in 120 s; that is the same exposure class as
the enrolment code on the operator's screen.

### Dependencies

`jinja2` and `markdown-it-py` in the `server` extra of `pyproject.toml`; `uv.lock`
regenerated. The client gains nothing (`webbrowser` is stdlib).

## Decisions raised, not improvised

1. **No new frozen goal wave** — the reasoning of the #20 plan: one author, so the ACs
   are asserted in the suites that own each boundary. Wave `m` (image boundary) must
   keep passing with the new deps and files.
2. **Code in the URL fragment, redeemed by a POST from `login.js`**, not a query
   parameter: Uvicorn's access log records query strings, and Fly Logs keep them.
3. **No logout in v1.** Sessions end at 12 h, on `deactivate`, or on `revokedevice`. A
   logout is the first cookie-authenticated `POST`; it can come with the first write.
4. **`/ui` ignores bearers and `/api` ignores cookies.** Two credentials, two surfaces.
5. **No non-CLI login.** Everyone who should see the UI is enrolled. A person without
   the CLI (an operator-minted link, or GitHub OAuth) is a later decision.
6. **Digest detection by the frontmatter's `type: digest`**, read only inside the
   leading `---` block — the rule `curate.is_digest` applies. The server cannot import
   the client (`client/` is not in the image), so `server/ui_render.py` reimplements the
   reader; the golden fixtures pin both to the same literals.
7. **Every enrolled, active user sees every project**, as the API already allows. No
   per-project authorization is introduced.

## Out of scope (v1)

Any write from the UI (registration, feedback, links, curation, token management); path
merge (R3.3); OKF export (R3.5); the local pipeline state of each machine (pending
markers, quarantine, `dropped_not_enabled`, `auth_rejected.json`) — it lives only on the
machine (D3, D9), `loom status` stays its surface, and "last capture per author" is the
UI's only proxy; per-person usage statistics; logout; non-CLI login.

## Acceptance criteria → assertions

Written as failing tests **before** each slice's implementation (AGENTS.md rule 9).
Expected values come from literal fixtures (a seeded database built in the test from
explicit rows), never from `ui_queries` itself (rule 4).

| # | Acceptance criterion | Where asserted |
|---|---|---|
| AC-1 | `POST /api/ui-login` needs a valid bearer; returns a code that works **once**; a second redemption, a redemption after 120 s, a code of a deactivated user and a code whose device was revoked all answer the same `401 invalid_code` | `server/tests/test_ui_auth.py` (real expiry with a short TTL override, like `LOOM_DEVICE_TOKEN_TTL_S`) |
| AC-2 | The 11th failed `/ui/session` from one IP gets `429`; issued, redeemed, invalid and rejected outcomes are audited with no code, cookie or session id in the log line | `test_ui_auth.py` (`caplog`, 11 bad codes) |
| AC-3 | The cookie is `HttpOnly; Secure; SameSite=Strict; Path=/ui` with `Max-Age` ≤ 12 h and ≤ the device token's remaining life | `test_ui_auth.py` (parse `Set-Cookie`) |
| AC-4 | A session stops working on the next request after `admin.py deactivate`, after `admin.py revokedevice` on its originating device, and after its expiry | `test_ui_auth.py` |
| AC-5 | `/ui/*` without a session answers `401` for existing and non-existing pages alike; a `Bearer` header does not open `/ui`; a `loom_ui` cookie does not open `/api/*` | `test_ui_auth.py` |
| AC-6 | Read-only is structural: `POST`/`PUT`/`PATCH`/`DELETE` on every `/ui` route except `/ui/session` answer `405` with a valid session; no `/ui` request changes any table (row counts of every table before/after a crawl of all pages) | `server/tests/test_ui_views.py` |
| AC-7 | Hostile markdown renders inert: `<script>`, `<img onerror>`, `[x](javascript:…)`, `![](https://evil/?leak)`, an HTML comment and a raw `<iframe>` in `content_md` or `text_md` produce the expected escaped output | `server/tests/test_ui_render.py` against a **golden manifest** (`server/tests/golden/ui_render/*.md` → `*.html`, rule 11) |
| AC-8 | Every `/ui` response carries the CSP, `Referrer-Policy`, `nosniff` and `no-store` headers listed above | `test_ui_views.py` |
| AC-9 | Browsing is not a recall: a crawl of every page leaves `recall_events` unchanged | `test_ui_views.py` |
| AC-10 | Served status wins: an entry whose frontmatter says `status: current` but is superseded by `#N` shows `superseded (by #N)` on the entry page and in the timeline | `test_ui_views.py` (seeded `entry_links` row) |
| AC-11 | `current` tab: a superseded item is absent by default and present, struck and followed by its replacing item, author, date and rationale with `show_replaced=1`; a revoked link restores it to current; digest entries never appear as items; pre-D13 entries appear under "Entries without items" | `test_ui_views.py` (seeded literal rows) |
| AC-12 | Item feedback is **named** on the item page and on the curation tab (person, verdict, note, date); `/ui/usage` and the overview strip contain no user name | `test_ui_views.py` (seeded users with distinctive names; assert presence/absence in the HTML) |
| AC-13 | Overview numbers match the seeded fixture: entries, items current/replaced, open tasks, last capture, 30-day authors, curation signals — for two projects | `test_ui_views.py` |
| AC-14 | The digest panel states the covered range and "K entries captured since" | `test_ui_views.py` |
| AC-15 | `loom ui` prints a URL whose fragment holds the code, calls the browser with that URL, never passes the code in a query string, and follows the loud-401 path on a rejected token | `client/tests/integration/test_cli_ui.py` (mock server; `BROWSER` set to a recorder script) |
| AC-16 | End to end: real uvicorn, real `loom ui` subprocess, `BROWSER` pointing at a recorder; the recorded URL's code opens a session via `POST /ui/session` and a project page renders an entry captured by a real processor pass | `client/tests/e2e/test_ui_e2e.py` |
| AC-17 | The image still boots and passes wave `m` with the new deps and files; `/ui/login` is served from the container | `make test` (sweeps `test/waves`, Docker required) |
| AC-18 | In a real browser, the login page consumes the fragment, erases it from the address bar and history, and lands on `/ui/`; the CSP reports no violation in the console | Manual QA with a real browser, recorded in the PR with screenshots |
| AC-19 | `DESIGN.md` §6.2/§6.3, `PLAN.md` §3, `docs/PHASE-2-3-SPEC.md` Q-3.3, the pilot scope and `docs/RUNBOOK.md` §5–6 describe the shipped behaviour | this PR (self-review, rule 8) |

## Slices (TDD, each red → green; suite green before and after every refactor)

1. **Login codes and sessions (server).** `ensure_ui_schema`, `POST /api/ui-login`,
   `POST /ui/session`, middleware branch, `401` page. → AC-1…AC-5.
2. **Rendering.** `render_md`, frontmatter split + served-status overlay, golden
   manifest. → AC-7, AC-10 (overlay unit).
3. **Page shell and headers.** Templates, CSS, banner, `405` rule, security headers,
   crawl helper for the tests. → AC-6, AC-8, AC-9.
4. **Projects overview and project tabs.** `ui_queries`, pages A and B. → AC-11,
   AC-13, AC-14, AC-12 (curation tab).
5. **Item, entry and usage pages.** Pages C, D, E. → AC-10, AC-12.
6. **`loom ui` (client) and e2e.** `ApiClient.ui_login`, subcommand, fallback print,
   RUNBOOK `BROWSER` note. → AC-15, AC-16.
7. **Docs, image, manual QA.** Deps in `pyproject.toml` + `uv.lock`; `DESIGN.md` §6.2
   route rows and §6.3 rewritten to point at D14; `PLAN.md` §3; Q-3.3 marked resolved;
   one line in the pilot scope (owner decision 1); RUNBOOK §5 (`loom ui`, WSL
   `BROWSER`) and §6 (reading the UI; revoking a person or a device ends their
   sessions). `make test` + `LOOM_SKIP_LIVE=1 make contract`; real-browser QA;
   self-review. → AC-17…AC-19.

Deployment follows RUNBOOK §2: the server ships first (no client depends on it until
`loom ui` exists on a machine), then each machine updates its client.
