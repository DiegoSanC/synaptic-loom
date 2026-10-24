# Synaptic Loom — pilot RUNBOOK

Operating the pilot. Since the **cutover** (issue #17) the authoritative
server is the **hosted Fly.io deployment** at `https://<app>.fly.dev` — platform
TLS, one Machine in `cdg`, SQLite on a persistent volume. Each engineer's client runs on
their own machine. One synthetic project, zero real customer data.

The phase-1 topology — uvicorn on `127.0.0.1:8787` under a user systemd unit, server and
clients on one WSL2 host — is **RETIRED**. Its unit is stopped and disabled and
`~/.loom-server/loom.db` is a frozen pre-cutover copy, not an authority. Sections below
that describe it are labelled as the retired topology and are kept for rollback history
only: **two live servers on two independent SQLite files split the logbook silently**, so
do not follow them to bring a second server up.

Originally deployed on **2026-07-24** . Complements `DESIGN.md §8` and
`server/README.md`. To uninstall the client: `client/UNINSTALL.md`.

---

## 0. Summary of the current deployment

| Component | Value |
|---|---|
| Server | `uvicorn server.app:app` hosted on Fly at `https://<app>.fly.dev` (platform TLS; app `<app>` as named in `fly.toml`, one Machine, region `cdg`) |
| Server management | the `fly` CLI — `fly status`, `fly logs`, `fly machine restart -a <app>` |
| Server DB | `/data/loom.db` on the app's one volume, `loom_data` (env `LOOM_DB`) |
| Backups | Fly volume snapshots, retention 14 days (§3a) |
| Local phase-1 server | **RETIRED at the cutover** — `loom-server.service` stopped and disabled; `~/.loom-server/loom.db` frozen, not an authority |
| Server interpreter | `<repo>/.venv/bin/python` (extras `[server,dev]`) |
| Client (dev) | `~/.loom/` + venv `~/.loom/venv` (`loom` package, editable, extra `[client]`) |
| SessionEnd hook | `~/.loom/hooks/sessionend.sh`, registered in `~/.claude/settings.json` |
| Skills | `~/.claude/skills/loom/` and `~/.claude/skills/loom-register/` |
| Dev config | `~/.loom/config.json` (chmod 600) → points at `https://<app>.fly.dev`, the machine's device token |
| Users and machines | `server.admin listusers` / `listdevices`, inside the Machine (§2 → "Administering users") |
| Pilot project | one synthetic project with a fictitious remote, `github.com/example-group/loom-pilot` |
| Dev machines | each enrolled with `loom enroll` (§5); periodic trigger is a systemd user timer or cron (§2) |
| Summarizer prompt (D11) | a fresh `loom enroll` writes the newest version; an enrolled machine keeps what its config says (§5). Do not trust a remembered version, read `loom doctor` |
| Curation (E7) | **Manual**, on ONE machine, always the same one — weekly, §6 → "Curating" |
| Capture mode (D9) | New machines land in `opt_in` through `loom enroll`. A machine enrolled before D9 stays on `all_registered` (legacy; `doctor` warns) until `loom enable` is run inside each repo it should capture |

`<repo>` = the checkout the client venv is installed from (editable). Run nothing from any
other, stale clone of the tool.

**Fly.io has been the only authority since the cutover** (`<app>`, one Machine in
`cdg`) — see §2 → "Fly.io deployment" and its "Migration from the phase-1 server" section.

---

## 1. Environment findings (WSL2, verified at the first deployment)

- **systemd (system):** `systemctl is-system-running` → `degraded` (it boots, with some
  non-critical unit failing), but functional.
- **systemd (user):** `systemctl --user is-system-running` → `running`. That is why the
  server is managed as a **user unit** (not root). `XDG_RUNTIME_DIR` and
  `DBUS_SESSION_BUS_ADDRESS` are present.
- **linger:** enabled (`loginctl enable-linger`), so the unit stays alive even with no login
  session open. *WSL2 note:* if ALL WSL sessions are closed, the distro may stop anyway;
  when WSL is reopened, systemd --user relaunches the unit.
- **⚠ Known WSL2 issue — `systemctl --user` can stop working.**
  `/run/user/1000/bus` is gone, so every `systemctl --user` / `journalctl --user` command in
  this file fails with `Failed to connect to bus: No such file or directory`. The user
  manager itself is alive (`systemd --user` is running) and *timers it already has loaded
  keep firing*, which is exactly what makes this silent: you lose the ability to inspect,
  stop or read the journal of a unit that is still running. `install.sh` probes
  `systemctl --user is-system-running` and, on that failure, installs the **cron** trigger
  instead (§2). **Cause:** two tmpfs are stacked on `/run/user/1000`
  (`/proc/self/mountinfo` shows two mounts on the same mountpoint, one the parent of the
  other). The lower one is systemd's and holds the bus and `systemd/private`; the upper one
  is WSLg's and is all that is visible — `pulse`, `wayland-0`. `systemctl --user` looks for
  the bus in the upper one and finds nothing. It **recurs on every boot**, `wsl --shutdown`
  included. Nothing in this repo can fix it, and Loom does not need it fixed — on such a
  host the trigger is cron.
- **cron:** `cron.service` **active**. The daily backup runs via cron (see §3).
- **Tools present:** `claude` 2.1.218 (matched `contract/VERSION` at the first deployment; when the host's CLI
  moves on, `loom doctor` warns about the drift — see §6 "After updating Claude Code"),
  `jq`, `uv` 0.9.26, `python3.12`, `sqlite3` (supports `.backup`), `git`.
- **SessionEnd kick launcher:** `setsid` (util-linux) is present, so the hook's kick uses
  tier 1 on this host. The kick has to start the processor in a *new session* or it dies
  with the Claude Code session that spawned it, and `setsid` is what buys that. `loom
  doctor` reports which tier a host actually resolves; see §5 for the per-platform table.
- **Claude auth:** **OAuth** subscription (no `ANTHROPIC_API_KEY`). The summarizer uses
  `--safe-mode --tools ""` (see `contract/FLAGS.md`; `--bare` does NOT work here).

**Process-management decision:** since `systemctl --user` works, the server runs as the
user unit `loom-server.service`. The `server/start.sh` / `server/stop.sh` scripts
(nohup + pidfile) remain as a **portable fallback** for machines without
`systemctl --user`; on THIS machine use them only if systemd were unavailable.

---

## 2. Server: start / stop / status

### Primary mechanism (user systemd)

```bash
systemctl --user status  loom-server        # status
systemctl --user restart loom-server         # restart
systemctl --user stop    loom-server         # stop
systemctl --user start   loom-server         # start
journalctl --user -u loom-server -n 50 --no-pager   # logs
```

The unit lives at `~/.config/systemd/user/loom-server.service` (a copy of
`server/loom-server.service`). After editing it: `systemctl --user daemon-reload`.

### Health check

```bash
curl -sf https://<app>.fly.dev/api/health   # -> {"status":"ok","version":"0.1.0"}
```

### Fallback without systemd (RETIRED topology — do not run during the pilot)

```bash
bash server/start.sh      # nohup uvicorn 127.0.0.1:8787, pidfile ~/.loom-server/uvicorn.pid
bash server/stop.sh
# Configured by environment: LOOM_DB, LOOM_HOST, LOOM_PORT, LOOM_SERVER_PY
```

### Running the server as a container (the Fly.io packaging)

This is what Fly will run, and it runs identically on any machine with Docker. It does
**not** touch the pilot server or `~/.loom-server/` — it is a separate server on a
separate database.

```bash
docker build -t loom-server .
mkdir -p /tmp/loom-vol                     # stands in for the Fly volume
docker run --rm -p 127.0.0.1:8080:8080 -v /tmp/loom-vol:/data loom-server
curl -sf http://127.0.0.1:8080/api/health  # -> {"status":"ok","version":"0.1.0"}
```

Everything else needs a token, `/docs` included. Create a user the same way as on the
host, but **inside** the container (`-u app`, so the files it writes stay writable by the
server process):

```bash
docker exec -u app <container> python -m server.admin adduser --name dev --email dev@example.com
```

Things worth knowing before changing any of it:

| Thing | Why it is the way it is |
|---|---|
| `LOOM_DB=/data/loom.db` | Set in the image AND in `fly.toml`. Unset, `server/db.py` falls back to `./loom.db` — which boots, answers health and even survives a restart, on a disk that dies with the container. |
| The entrypoint runs as root, briefly | Fly attaches the volume at runtime as `root:root`, so `COPY --chown` cannot help. It `chown -R`s `/data` and then `exec`s as `app`. |
| The `CMD` is in exec form | So uvicorn is PID 1 and gets SIGTERM directly. In shell form `/bin/sh -c` swallows it and the process is hard-killed on every idle stop. |
| `fly.toml`'s `kill_timeout` | Must stay `>=` uvicorn's `--timeout-graceful-shutdown`, or Fly kills it mid-shutdown. |
| The image has no client | `server/` imports nothing from `loom`, so the image carries the dependencies and `server/` and nothing else — not `client/`, not the test suites. |

All of the above is asserted by `test/waves/m/test_goal_fly_packaging.py` against a real
build and real containers, so a change that breaks one of them fails the suite rather than
the deployment. Requires a Docker daemon; under `LOOM_GATE=1` a missing one is a failure,
not a skip.

### Fly.io deployment (issue #2, Phase 3)

**Status: deployed, serving and AUTHORITATIVE since the cutover.** The pilot's data was
migrated onto the volume then and every client talks to this deployment over HTTPS.
`~/.loom-server/loom.db` on the phase-1 host is a frozen pre-cutover copy and must never
be served again. See "Migration from the phase-1 server" below.

| Component | Value |
|---|---|
| App | `<app>` (`fly.toml` → `app`) — https://<app>.fly.dev |
| Machine | `shared-cpu-1x` · 256MB · `cdg` — exactly one (AC4) |
| Volume | `loom_data` · 1GB · `cdg` · encrypted · daily snapshots, **retention 14 days** (§3 — the backup) |
| IPs | v4 **shared** (a dedicated v4 is $2/mo) · one v6 |
| DB path | `/data/loom.db` (`LOOM_DB`, image ENV + `fly.toml [env]`) |
| Image | whatever the last `fly deploy` built. History: `fly releases --image -a <app>` |

Commands below write `<machine-id>` and `<volume-id>`; read the current ones with
`fly machine list -a <app>` and `fly volumes list -a <app>`.

```bash
fly deploy --ha=false                 # --ha=false is not optional: see AC4
fly status -a <app>              # exactly one Machine
fly logs -a <app>                # WATCH THIS for a minute after every deploy
```

**Watch the logs after every deploy, by hand, until health checks exist.** There is one
Machine, no HA and no `[[http_service.checks]]` block (deferred deliberately — see the
comment in `fly.toml`). Nothing will tell you a release is crash-looping; the first
symptom is an engineer getting a timeout.

#### Hosted API security gate (issue #19)

Enable 2FA on the Fly account. This is a manual account control; the repository and Fly
CLI cannot prove it.

| Control | State |
|---|---|
| Fly account 2FA | **Manual** — enable it on the account; nothing here can check it |
| Failed-auth audit | Security-audit lines log `outcome`, UTC `timestamp` and validated `client_ip`; never a bearer value |
| Failed-auth limit | 10 failures per validated client IP (`Fly-Client-IP` here, D17) in 60 seconds; then `429` + `Retry-After` for further failures — a valid credential is still accepted |
| Public health limit | 120 requests per validated client IP in 60 seconds; remains unauthenticated |
| Transport header | `Strict-Transport-Security: max-age=31536000; includeSubDomains` on every response |

The two limiters live in this one Uvicorn process and reset on restart. That matches the
deployed one-worker, one-Machine topology. Do not add another worker or Machine without
moving the policy to shared state. `fly.toml [env]` sets `LOOM_CLIENT_IP_SOURCE=fly-client-ip`
(D17) and Fly Proxy supplies `Fly-Client-IP`; a missing or invalid value is grouped into one
`unavailable` bucket and is never copied verbatim into logs. At startup the server logs
`client_ip_source mode=fly-client-ip`, and a `WARNING` there means the mode and the Fly
marker disagree — fix `fly.toml` before anything else.

The health response deliberately remains `{"status":"ok","version":"0.1.0"}`. The
version is a non-secret semantic compatibility signal used by `loom init` and
`loom doctor`; retaining that diagnostic is worth the minor fingerprinting surface for
this synthetic-data-only pilot.

After deployment, verify the public boundary and inspect `fly logs` for the structured
failed-auth line. Use a disposable invalid value, never a real token:

```bash
curl -si https://<app>.fly.dev/api/health | grep -i strict-transport-security
curl -si -H 'Authorization: Bearer deliberately-invalid' \
  https://<app>.fly.dev/api/projects
fly logs -a <app>
```

#### Measured on the day of deploy — not assumed

Three things the plan listed as UNVERIFIED, settled empirically:

| Question | Answer |
|---|---|
| Does the Machine actually auto-stop with no health checks? | **Yes.** It stopped ~6 min after the last request. |
| Is cold start inside the client's budget (`DEFAULT_TIMEOUT = (connect 3.05 s, read 15.0 s)`)? | **Yes, with room.** From `stop`: `connect=0.118s`, total **4.48s**. From `suspend` (measured before the switch): `connect=0.049s`, total 0.69s. Warm either way: ~0.11s. |
| Where does the cold-start wait actually land? | In the **read** phase, not connect. The Fly proxy accepts the TCP connection immediately and holds the request while the Machine boots — so 4.48 s is charged against the 15.0 s read budget, not the 3.05 s connect budget. That is why `"stop"` is affordable. |
| Does `fly ssh console` wake a stopped Machine? | **No.** It fails with `app <app> has no started VMs`. You must start it first. |

#### Administering users (the ONLY supported way)

`server/admin.py` must run **inside the Machine**, against the volume. Running it locally
against a copy creates a second, divergent set of `token_hash` values with nothing to
surface the divergence.

```bash
fly machine start <machine-id> -a <app>       # REQUIRED first — ssh will not wake it
fly ssh console -a <app> -u app -C "python -m server.admin listusers"
# Onboard a machine (the default since #20): a single-use code, valid 24h, no token printed
fly ssh console -a <app> -u app -C "python -m server.admin invite --email dev@example.com --name dev"
fly ssh console -a <app> -u app -C "python -m server.admin invite --email dev@example.com"   # a second machine of an existing user
fly ssh console -a <app> -u app -C "python -m server.admin listdevices"
fly ssh console -a <app> -u app -C "python -m server.admin revokedevice --id 3"            # one lost or stolen machine
fly ssh console -a <app> -u app -C "python -m server.admin revokedevice --id 3 --no-cascade"   # one replaced machine (§4)
fly ssh console -a <app> -u app -C "python -m server.admin promote --email dev@example.com" # roles (D20, §4)
# Break-glass only: a PERMANENT token, printed once (what #20 exists to stop hand-delivering)
fly ssh console -a <app> -u app -C "python -m server.admin adduser --name dev --email dev@example.com"
```

`invite` prints an **enrolment code**, which is the only thing that travels: the engineer
redeems it with `loom enroll` (§5) and that machine mints its own 90-day device token,
renewed silently by the client. A revoked or expired device token shows up in Fly Logs
as `outcome=revoked_token` / `outcome=expired_token`, and on the machine as a `✗` row in
`loom doctor` and an `AUTH REJECTED` block in `loom status`.

`-u app` matters: `fly ssh console` is root by default, and a root-created WAL sidecar in
`/data` is a file the server process then cannot write.

#### Deploying the item model (D13): server first, snapshot before

The release that adds `items` to `POST /api/entries` (plan of 2026-09-23, slice C) migrates
the database at boot: `server/items.py` creates `entry_items`, its FTS index and
`item_links` — new tables only, nothing existing is altered. Order matters, because an
**old server silently drops** the `items` field (the request model ignores unknown
fields) and the entry lands un-itemized, permanently:

1. Take an on-demand snapshot first (§3a) — the migration runs on the one worker at boot.
2. `fly deploy --ha=false`, then check `GET /api/health` and that
   `GET /api/projects/<id>/items?entry_ids=<an id>` answers `200 {"items": []}`.
3. Only then update the clients (`install.sh --update`).

Make sure no entry is posted between the client code reaching `main` and the deploy
(queue and quarantine empty), so none lands un-itemized.

Rolling the image back after step 2 is safe for the data (an old image ignores the new
tables), but entries it accepts in that window carry no items. An entry posted without
items — by an old client, or from a v1/pre-OKF capture — is simply read as a whole, as
before; there is no backfill.

#### Deploying stemmed search (RT.1, amended 2026-10-24)

The release that tokenizes both search indexes with Porter stemming (`migration` finds
"migrations") rebuilds them at boot: `entries_fts` and `entry_items_fts` are dropped and
re-created from the stored entries and items in one transaction, before the first request
is served. They are indexes only — no entry, item or link is touched — and the rebuild
takes a moment proportional to the logbook's size.

1. Take an on-demand snapshot first (§3a), as for every release that migrates at boot.
2. `fly deploy --ha=false`, then check `GET /api/health` and that a `?q=` search answers
   `200` (a plural finding its singular is the visible change).

No client update is needed. Rolling the image back is safe: an older server finds the
indexes complete and keeps using them, stemming included, because the tokenizer is part of
each index's own definition.

#### Deploying feedback and stats (plan of 2026-09-23, slice F)

The release that adds `POST /api/feedback` and `GET /api/stats` creates one table at boot,
`recall_feedback` (it references `entry_items`, so it needs the D13 release before it).
Same order as above: snapshot (§3a), `fly deploy
--ha=false`, check that `GET /api/stats` answers `200` with an authenticated request,
then update the clients with `install.sh --update` — that ships the `/loom-feedback`
skill and the `CLAUDE.md` block line that points the agent at it. A client ahead of its
server is harmless: `loom feedback` and `loom stats` say the server does not support them
yet and exit 1; nothing else changes.

#### Deploying the web UI (D14, plan of 2026-09-29)

Server first, like every release: snapshot (§3a), `fly deploy --ha=false`, then check from
outside, with no credential:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://<app>.fly.dev/ui/login        # 200
curl -s -D - -o /dev/null https://<app>.fly.dev/ui/ | grep -i -E '^HTTP|content-security'
#   → 401, and the CSP header (every /ui response carries it)
```

The release creates two tables at boot (`ui_login_codes`, `ui_sessions`) and nothing else
changes for the API. Then update a client (`install.sh --update`) and run `loom ui` from it:
the browser lands on the projects overview, signed in. A client ahead of its server is
harmless: `loom ui` says the server does not serve the web UI yet and exits 1.

#### Deploying item links (plan of 2026-09-23, slice E)

The release that adds `POST /api/item-links` and `POST /api/item-links/{id}/revoke`
creates nothing at boot: `item_links` has existed, empty, since the D13 release.
Same order anyway — snapshot (§3a), `fly deploy --ha=false`, then prove the route with a
harmless probe from an updated client (it reads the token from `config.json`, never from
the command line): revoking a link that does not exist must fail with `link_not_found`,
not with "does not support item links yet":

```bash
~/.loom/venv/bin/loom unlink 999999 --reason "deploy probe"
# expected: exit 1, "Server error: HTTP 404 link_not_found: No item link 999999"
```

Then update the clients (`install.sh --update`). A client ahead of its server is safe
but useless: `loom curate` on itemized entries stops with "the server does not support
item links yet", posts no whole-entry link in their place and keeps the verdicts to
replay; `loom unlink` says the same. **That is why no `loom curate` runs against the
hosted server before this release is deployed.**

#### Deploying chain revocation (D19): count the orphans, then revoke them

Before D19, renewing the same device token twice (a lost `201`, a copied `config.json`, a
thief) overwrote the link to the first successor, which then stayed **live for up to 90
days and out of reach** of `revokedevice`. The D19 server never forks a chain again, but
it does not touch what is already in the database: it only **counts** those orphaned
tokens at every start. Revoking them is a **required** step of this upgrade:

```bash
# 1. Snapshot (§3a), then deploy.
fly deploy --ha=false
# 2. Read-only: the count the server logged at boot (Compose: docker compose logs loom | grep …)
fly logs -a <app> --no-tail | grep device_token_orphans
#   device_token_orphans count=0                       → nothing to do
#   device_token_orphans count=N: run `python -m server.admin revoke-orphans` …
# 3. Only when N > 0: revoke them. It prints every id it revoked, by the chain it hung
#    from; their number must be N.
fly ssh console -a <app> -u app -C "python -m server.admin revoke-orphans"
```

An orphaned branch may be the one a real machine kept using (the other branch then came
from a copy or a thief). That machine gets a **loud 401** (`loom status`, `loom doctor`)
and recovers by enrolling again with a fresh `invite`: the accepted trade-off. After step 3
the next start logs `count=0`.

#### Rollback restores the IMAGE, not the data

Fly's own wording. `fly deploy --image` puts an old image back; it does **not** roll back
the database, and it does not roll back `fly.toml`, env or secrets either — an old image
then runs against current config. Data rollback is a volume snapshot restore — §3.

```bash
fly releases --image -a <app>
fly deploy --image <previous-image-ref> -a <app>
```

#### Cost (AC6)

Fly enforces **no hard spend cap and offers no billing alerts**, so any budget is a sizing
target verified by hand, and the ceiling is **org-wide, not app-scoped** — every other app
and volume in the same Fly org bills against it too, including a suspended app's volume.

**`auto_stop_machines` is `"stop"`, not `"suspend"`, and the reason
is cost certainty rather than latency.** `"suspend"` retains a memory snapshot on disk and
we did not verify how Fly bills that retained RAM. Against an org-wide ceiling with no
spend cap and no billing alerts, an unverified billing mode is worth more than the ~3.8 s
of extra cold start it buys back. Note `fly config show` reports this as the legacy boolean
`auto_stop_machines = true`, not the string `"stop"` — the API normalises it; that is not
drift.

**There is NO cost alert, and Fly provides none.** `flyctl` has no `billing` command and
no spend-limit subcommand. The only budget protections in place are
structural: one Machine enforced by the volume, `shared-cpu-1x`/256MB, `min_machines_running
= 0` with verified auto-stop, a 1GB volume, and a **shared** IPv4 (a dedicated one is
$2/mo). If a budget alert exists in the Fly dashboard, it has to be set there by hand.

The month-to-date bill carries no signal on day zero: **recheck it once the deployment
has run a full billing cycle.** Worst case under `auto_start` is the always-on price,
≈$2.17/mo for this Machine plus its volume, so a future reader seeing the bill move has
the bound without re-deriving it. Check volume usage at the same time — nothing warns
before a full volume, and the first symptom is a wave of 500s:

```bash
fly volumes show <volume-id> -a <app>
```

#### Migration from the phase-1 server (issue #17, completed)

The pilot data was migrated from the phase-1 server onto the volume at the cutover; Fly
is the only authority since. The local unit is stopped and disabled (`is-active` →
`inactive`, `is-enabled` → `disabled`, nothing on port 8787). The execution record lives
in the pilot's private operations notes. What it established, for anyone repeating this
against another volume:

- **Keep a copy of the source and snapshot the target first** — the source backup outside
  the 14-file rotation (mode 400), an on-demand Fly snapshot immediately before the
  overwrite. Compare row counts and `integrity_check` on both sides afterwards.
- **`fly sftp put` writes the file as `root:root`.** Upload to a staging name, then
  `chown app:app` and `mv` into place. A database owned by `root` passes every read-only
  check and fails the first hosted *write* — i.e. after the clients have been repointed.
- **`count(*) FROM entries_fts` cannot detect a failed backfill.** It is an
  external-content table, so a full scan reads `entries` and reports the same number
  either way; assert `entries_fts_docsize` instead (see `search._needs_backfill`).
- **Prove a write end to end**: one capture through the real SessionEnd hook against a
  **cold** Machine, read back by a second user over public HTTPS.

**Rollback by re-enabling the local unit is no longer valid.** Once a hosted write exists,
bringing `loom-server` back up would create two divergent authorities. Recovery now means
restoring the Fly volume from a snapshot (§3a), not re-enabling systemd.

**The migration path itself was rehearsed** against a copy of the pilot database in the
real image, because the pilot DB predates Block T and gains `entries_fts`,
`entry_item_types` and `entry_links` on first boot — a migration this repo's goal waves
never exercise, since they only ever start from an empty volume. The rehearsal confirmed:
the additive DDL applies, the FTS5 index backfills, counts are preserved, an existing
pilot token still authenticates (200), and `?q=` search works on the migrated data.

### Client periodic trigger — `loom-process` every 15 min (phase 2, R2.2)

The SessionEnd hook's *kick* is best-effort: it does not survive a crash, a `kill`, or a
WSL shutdown, and it cannot detach at all on macOS (see the platform note below). So the
client also runs `loom process` **every 15 minutes**, installed by `bash client/install.sh`
(step 5, idempotent — re-running never duplicates it). The kick still gives the fast path;
the trigger is the safety net, and it is what drives the orphan sweep.

**On a WSL2 host whose `systemctl --user` broke (§1): cron.** `install.sh` takes the cron
branch below and `loom doctor` reports `✓ no systemd unit files installed (cron-branch
host)`. If timer units had been installed before the bus broke, **removing their files does
not stop the timer.** The user manager loaded it at boot and cannot be told to reload
without the bus, so both triggers keep firing every day the machine is up until the user
manager restarts. Harmless (the processor serialises on a `flock` over `~/.loom/lock`, so
nothing is posted twice) but invisible to `doctor`, which on a cron host only checks the
crontab; it surfaces as unexplained off-schedule passes.

> **Deleting a timer's unit files does not stop the timer.** Stop it first —
> `systemctl --user stop loom-process.timer`, then `disable`, then remove the files. On a
> host where that cannot connect (the broken-bus case above), the only way is to restart
> the user manager: `wsl --shutdown` from Windows. A laptop that sleeps instead of shutting
> down can carry a stale timer for days.

How to tell the two triggers apart, in the log and in the act:

- **The signature in `processor.log`.** cron passes land in the first seconds of `:00`,
  `:15`, `:30`, `:45`. A timer (`OnUnitActiveSec=15min` plus systemd's default one-minute
  `AccuracySec`) fires every **16:00**, always at the same second. Two interleaved cadences
  is the tell. A lone pass at any other time is a SessionEnd kick, and `hook.log` has a
  `kick dispatched` line at that second.
- **The parent, caught live.** Poll `pgrep -f 'loom process'` and walk
  `/proc/<pid>/status` → `PPid`: a cron pass descends from `/usr/sbin/cron`, a timer pass
  from `systemd --user`, and a timer pass also carries `INVOCATION_ID` in its environment.
  Poll at 0.1 s or faster — a cron pass lives well under a second.

**On a host where `systemctl --user` works: user systemd timer.**
`client/timer/loom-process.{service,timer}` are copied to `~/.config/systemd/user/`,
then `systemctl --user daemon-reload` + `systemctl --user enable --now loom-process.timer`.

> Only ONE trigger may be armed per machine. `install.sh` picks the branch by probing
> `systemctl --user is-system-running`; if you switch a host from one branch to the other,
> remove the old trigger — `install.sh --uninstall-trigger` removes **both**, so re-run
> `install.sh --yes` afterwards to arm the one you want.

```bash
systemctl --user list-timers loom-process.timer     # NEXT / LEFT / LAST / PASSED
systemctl --user status loom-process.timer          # timer state
systemctl --user status loom-process.service        # last run's result
journalctl --user -u loom-process -n 50 --no-pager  # what the last runs did
systemctl --user start loom-process.service         # force one pass right now
systemctl --user disable --now loom-process.timer   # stop the periodic trigger
```

After editing the units by hand: `systemctl --user daemon-reload`.

> The units shipped in `client/timer/` are static (`%h/.loom`), but install.sh resolves
> them as it copies: the **installed** `~/.config/systemd/user/loom-process.service` gets an
> `ExecStart=<LOOM_HOME>/venv/bin/loom process --quiet`, an
> `Environment=LOOM_HOME=<LOOM_HOME>` for the `LOOM_HOME` the install actually used, and an
> `Environment=PATH=<the trigger's minimal PATH>` (the same value as the cron block's `PATH=`
> line, below). All three matter: a systemd *user* service
> inherits the **user manager's** environment, not the environment of the shell that ran the
> installer, so without them a non-default `LOOM_HOME` would point the timer at a binary that
> was never created — and even if it started, the pass would resolve its home to `~/.loom`,
> work on a different queue, config and token, and look for `claude`, `git` and a fallback
> gitleaks on a PATH that does not have them (see "the trigger's PATH" below). If you move
> `LOOM_HOME`, or `claude`, `git` or `gitleaks` move, re-run `bash client/install.sh --yes`
> rather than hand-editing.

**Manual verification after installing/updating on the WSL2 host (AC-2.2c)** — do this
once against live systemd, it is the step the automated tests cannot cover (they shim
`systemctl`):

These commands need a working `systemctl --user`, which a broken-bus WSL2 host lacks (§1); on
the cron branch verify with `crontab -l`, `loom doctor` and the pass timestamps in
`~/.loom/log/processor.log` instead.

```bash
bash client/install.sh --yes                        # or --update
systemctl --user is-enabled loom-process.timer      # -> enabled
systemctl --user is-active  loom-process.timer      # -> active
systemctl --user list-timers loom-process.timer     # NEXT is <= 15 min away
systemctl --user start loom-process.service         # one pass on demand
journalctl --user -u loom-process -n 20 --no-pager  # exit status 0
~/.loom/venv/bin/loom doctor                        # trigger check is ✓
```

**WSL2 caveat.** A systemd *user* timer only counts time while the VM is alive: if WSL is
shut down (`wsl --shutdown`, host reboot, the 8-second idle collapse) the clock does not
advance and no run is missed *silently* — it is simply not run. `Persistent=true` in
`loom-process.timer` makes systemd fire the missed run **as soon as the VM comes back**,
and `OnBootSec=5min` covers a cold boot, so the queue catches up within a few minutes of
the next time you open WSL. Corollary: `loom doctor`'s sweep-age check can legitimately
be `⚠` (stale) right after a resume — that is not a fault, re-check after one period.
`linger` (`loginctl enable-linger`) keeps the user manager alive without a login session,
but it cannot keep the VM alive.

**Fallback without `systemctl --user` (macOS, hosts with no user manager): cron.**
install.sh appends a tagged block — same pattern as the `# loom-server-backup` one in §3 —
replacing its own block in place, never duplicating, and preserving every other entry:

```cron
# loom-process
LOOM_HOME=/home/<user>/.loom
PATH=/home/<user>/.loom/venv/bin:<dirs of claude, git, jq, python3>:/usr/bin:/bin:/usr/sbin:/sbin
*/15 * * * * /home/<user>/.loom/venv/bin/loom process --quiet
```

`LOOM_HOME=` and `PATH=` are **crontab environment assignments** (crontab syntax, not
shell): cron's environment is minimal, so without them the periodic pass would work on
`~/.loom` — a different queue, config and token than a non-default install — and would
resolve its binaries on cron's `/usr/bin:/bin`. They apply from their line to the end of the
file, which is why the block is always appended **last**. If you add your own entries
afterwards, put them *above* the block.

**The trigger's PATH (why the `PATH=` line exists, and why it is short).** The unattended
pass still resolves binaries through `PATH`: the summarizer (`claude`, or `claude_bin` from
`config.json`), `git` for project attribution, and a gitleaks you installed yourself when
there is no `$LOOM_HOME/bin/gitleaks`. Under cron's own `/usr/bin:/bin` a homebrew
(`/opt/homebrew/bin`), nvm or per-user (`~/.local/bin`) binary is invisible. That is how the
scanner went missing before (§7, finding 1); since D16 (2026-10-15) install.sh installs a
checksum-verified gitleaks 8.30.1 into `$LOOM_HOME/bin/gitleaks`, loom runs it **by absolute
path**, and with no gitleaks at all capture **pauses** instead of degrading (§6, "Capture
paused") — but `claude` and `git` still need the trigger's `PATH`.

That `PATH` is **minimal and deterministic**, never a copy of your interactive one:
`$LOOM_HOME/venv/bin` first (it is also how install.sh and doctor recognise the line as
theirs), then the directory each of `claude` (or `claude_bin`), `git`, `jq` and `python3` —
plus gitleaks, only when `$LOOM_HOME/bin` has none — resolves from in the shell that ran the
installer, de-duplicated and **in the order they appear on that shell's `PATH`** (so each one
resolves to the very same file it did there), then `/usr/bin:/bin:/usr/sbin:/sbin`.
Nothing else: no `/usr/local/bin` or homebrew prefix is guessed, and a tool living there
brings its own directory. An absolute `claude_bin` is run as is and needs no directory.
Like `python3` for a `#!/usr/bin/env python3` tool, **`node` comes along when claude needs
it**: install.sh reads the first line of the file `claude` (or `claude_bin`) resolves to,
following symlinks, and when it is a `#!/usr/bin/env node` script — how an npm global install
starts it, often from a prefix that is not node's own directory — node's directory is carried
too, under the same order, de-duplication and 900-byte rules, and a node that does not
resolve is reported by name. A native claude binary (the standalone installer, the Homebrew
cask) needs nothing extra.

The reason it must be short: macOS cron silently **drops** an environment line of 999
bytes or more. Apple's cron (`apple-oss-distributions/cron`, tag `cron-52`) reads each line
into a `MAX_ENVSTR` = 1000-byte buffer (`cron/cron.h`) and `load_env` (`lib/env.c`) rejects
an assignment once `strlen(name) + 1 + strlen(value) >= MAX_ENVSTR - 1` — without an error
from `crontab -` or from cron, and the job then runs on `_PATH_DEFPATH` (`/usr/bin:/bin`,
`cron/pathnames.h`). A real Mac's install-time `PATH` (nvm, Homebrew, cryptex paths) was 1040
bytes: every pass ran on `/usr/bin:/bin`, gitleaks was not found and capture paused at every
`:00/:15/:30/:45` while the SessionEnd kick (run from an interactive shell) kept working.
So install.sh:

- **reports** a needed binary that does not resolve in your shell (`WARNING: 'claude' is not
  on this shell's PATH ...`) or resolves from a directory a scheduler entry cannot carry
  (relative, or characters outside the whitelist below) — the install still completes, and
  you re-run it once the binary is in place;
- **fails the install** (exit 1, nothing written to the scheduler — your crontab or units
  are left exactly as they were) when the composed `PATH=` line would exceed **900 bytes**,
  a margin below cron's 998. With only a handful of directories that takes very deep install
  locations; move the tools somewhere shorter (or set an absolute `claude_bin`) and re-run.

The systemd unit gets the same value in `Environment=PATH=`, for parity. `loom doctor` has
two rows about it: **trigger scanner parity** (`✓` when the pass will run
`$LOOM_HOME/bin/gitleaks`; a warning when only a PATH gitleaks exists and the trigger's
`PATH` cannot reach it — that pass would pause), and **trigger tool parity** (a warning when
the trigger's `PATH` — or, with no `PATH=` line, cron's `/usr/bin:/bin` — does not resolve
the same `claude` and `git` this shell does, nor the `node` an npm claude starts with: unattended passes would fail to summarize or
attribute). Both are `⚠`, not `✗`: the SessionEnd kick still captures. Re-run
`bash client/install.sh --yes` to fix either.

> **Trust note.** The unattended pass resolves binaries from the directories your `claude`,
> `git`, `jq` and `python3` live in, so any other executable in those directories is
> reachable unattended too — far less than your whole `PATH`, but not nothing. Relative and
> empty `PATH` components (`.`, a trailing `:`) are never carried, since "the current
> directory" means something different in a 15-minute cron job than it does under your
> fingers. install.sh replaces the whole block on its next run, so a hand edit of the
> `PATH=` line does not survive a re-install.

```bash
crontab -l | grep -A3 'loom-process'          # check it
bash client/install.sh --uninstall-trigger    # remove it (timer and/or cron)
```

> **Remove it with `--uninstall-trigger`, not with a hand-rolled `grep`.** The obvious
> `crontab -l | grep -v -A1 'loom-process' | crontab -` is a **no-op**: `-v` selects the
> non-matching lines and `-A1` then re-adds one line of trailing context after each match,
> putting the loom lines straight back — the output is byte-identical to the input, and on
> GNU grep it also injects `--` separators that `crontab -` chokes on. If you must do it by
> hand: `crontab -l | grep -v -F -e '# loom-process' -e 'LOOM_HOME=' -e "PATH=$HOME/.loom/venv/bin" -e '/venv/bin/loom process' | crontab -`
> — note the `PATH=` pattern is anchored on **our** venv path, not on the bare `PATH=`
> prefix: `PATH=` is a shared namespace and dropping your own assignment would quietly
> change what every entry below it can run. And if that leaves an empty crontab, finish with
> `crontab -r` — which is exactly the bookkeeping `--uninstall-trigger` does for you.

**Safety rails on the crontab** (it is a single shared resource with no undo):

- the previous crontab is copied to `~/.loom/backup/crontab.<epoch>` (mode 600) before
  **every** write, install and uninstall alike, *including before a removal* — that is your
  undo;
- if `crontab -l` fails for any reason other than "no crontab for &lt;user&gt;" (denied
  spool, `cron.deny`, a refused macOS permission prompt), install.sh **refuses to write**
  rather than rebuild your crontab from an empty read;
- our block is matched by content (the tag, the `LOOM_HOME=` line, a `PATH=` line carrying
  our venv path, and any line running `/venv/bin/loom process`), never by position, so a
  stray `# loom-process` comment in a hand-edited crontab cannot cost you the entry
  underneath it, and a `PATH=` line of your own is neither read as ours nor removed;
- `LOOM_HOME` must be a plain path — letters, digits and `_ . / @ + -`, no leading `-`.
  Cron passes its command field to `/bin/sh`, so a path containing `;`, `|`, `&&`, backticks,
  `$( )`, quotes, a bare space or `%` is **rejected** (both branches) with a warning instead
  of being escaped; the install still succeeds, only the trigger is skipped;
- the `PATH=` value is whitelisted *per component* under a slightly wider rule — cron reads
  an assignment value literally, so `:` (the separator itself) is allowed there, while a
  newline, a CR or a `%` (cron's "the rest of the line is stdin" separator) never is. A
  needed binary whose directory cannot be written safely is **left out** of the trigger's
  `PATH`, with a warning on stderr naming the binary (never the directory); the line is
  still written for the rest.

Hosts with neither `systemctl --user` nor `crontab` get a warning from install.sh (the
install still succeeds) and `loom doctor` reports the missing trigger. `launchd` support
on macOS is deliberately out of scope: cron is present there and the pilot host is WSL2.
Set `LOOM_SKIP_TRIGGER=1` to skip step 5 entirely (CI, or a host whose scheduler is
managed by other means).

> **Known platform issue — darwin.** `client/hooks/sessionend.sh` detaches the kick with
> `setsid`, which does not exist on macOS, so on a Mac the session-close kick does not run
> and the marker just sits in `~/.loom/queue/`. The periodic trigger covers exactly this:
> the entry lands within one period (≤ 15 min) instead of within seconds. Making the kick
> portable is out of scope for phase 2 (the hook is a frozen contract); the pilot host is
> WSL2, where the kick works.

---

## 3. Backup and restore

Two topologies, two mechanisms. **3a** is the hosted deployment on Fly.io and becomes the
only one that matters once the migration in §2 is done. **3b** is the phase-1 localhost
server and is retired with it (#17).

### 3a. Fly.io — volume snapshots (DESIGN.md D8)

**Decision.** The pilot's backup is Fly's own daily block-level snapshot of the app's
volume, retained **14 days**. The off-volume encrypted pipeline built for issue #8 (PR #26:
two scheduled workflows, a private GitHub repo, `age` recipients, two alert oracles, a
monthly drill) was closed unmerged in the original development repository, as
disproportionate for a small pilot with synthetic data; D8 names the trigger that would
bring it back.

| Property | Value |
|---|---|
| Mechanism | Fly automatic daily volume snapshot (block-level, stored by Fly, survives loss of the volume's host) |
| **RPO** | **≤ 24 h** — entries written after the last snapshot are lost in a volume-loss event. Same figure the rejected pipeline had. |
| RTO | ~10 min: create a volume from the snapshot, clone the Machine onto it, verify, destroy the old one. |
| Retention | **14 days**. `fly.toml [mounts].snapshot_retention = 14` covers volumes a future deploy creates; the existing volume needs the by-hand command below. |
| Alerting | **None.** Fly does not alert on a missing snapshot. The monthly check below is the only oracle. |
| What it does NOT give | A copy independent of the Fly account (lose the account, lose the snapshots), keys Fly does not hold, application-level consistency (a block snapshot of a live SQLite is crash-consistent: the WAL replays on open). |

**Apply the retention to the existing volume — a one-time, by-hand step** (the
`fly.toml` key does not touch volumes that already exist):

```bash
fly volumes update <volume-id> --snapshot-retention 14 -a <app>
fly volumes show   <volume-id> -a <app>        # confirm "Snapshot retention: 14", "Scheduled snapshots: true"
```

**Snapshot on demand before anything that rewrites `/data`** — the migration (§2), a
schema-changing deploy, a restore:

```bash
fly volumes snapshots create <volume-id> -a <app>
fly volumes snapshots list   <volume-id> -a <app>   # wait for STATUS=created
```

**Restore — into a NEW volume, never over the live one.** A volume attaches to one Machine
at a time and there is exactly one Machine (AC4), so the restore is a clone-and-replace:

```bash
# 1. Pick the snapshot. Automatic ones are daily; on-demand ones are whatever you took.
fly volumes snapshots list <volume-id> -a <app>

# 2. Clone the Machine onto a new volume restored from that snapshot ('last' = newest).
#    The clone gets a NEW volume named loom_data in the same region; the old Machine and
#    volume are untouched until step 4.
fly machine clone <machine-id> -a <app> -r cdg --from-snapshot <snapshot-id|last>

# 3. Verify on the CLONE before touching anything else: it must answer health, an
#    authenticated read AND a `?q=` search — the FTS5 index is what a naive restore loses.
fly machine list -a <app>                                   # note the new Machine id
fly ssh console -a <app> -u app --machine <new-id> \
  -C "python -c \"import sqlite3; c=sqlite3.connect('/data/loom.db'); \
      print(c.execute('PRAGMA integrity_check').fetchall()); \
      print([(t, c.execute(f'select count(*) from {t}').fetchone()[0]) \
             for t in ('users','projects','project_paths','entries','entry_item_types','entry_links')])\""
curl -sf https://<app>.fly.dev/api/health
curl -sf -H "Authorization: Bearer $T" "https://<app>.fly.dev/api/projects/1/entries?q=<a+word+you+expect>"

# 4. Only when step 3 passed: retire the old Machine, then its volume. This is the
#    destructive step and it is deliberately last. Afterwards exactly one Machine remains.
fly machine destroy <machine-id> -a <app> --force
fly volumes destroy <volume-id> -a <app>
fly status -a <app>                                        # exactly one Machine (AC4)

# 5. The volume a clone creates is born with Fly's DEFAULT retention (5 days), not the
#    14 in fly.toml — that key only governs volumes `fly deploy` creates. Re-apply it:
fly volumes update <new-vol-id> --snapshot-retention 14 -a <app>
fly volumes show   <new-vol-id> -a <app>                   # Snapshot retention: 14

# 6. From here on, <machine-id> and <volume-id> in this runbook mean the clone and its volume.
```

Between steps 2 and 4 the app briefly has two Machines. The Fly proxy may route to either;
do the verification through `--machine <new-id>` and do not leave the app in that state.

**Monthly check** — same cadence and owner as the cost audit in §2, one more line:

```bash
fly volumes snapshots list <volume-id> -a <app>   # newest CREATED AT < 24 h, ~14 rows
```

If the newest snapshot is older than a day, or the list is short, the backup has silently
stopped and nothing else will say so.

### Restore drill

A snapshot that was never restored is a hope, not a backup. Perform the restore above once
against the deployed app, and repeat it after any change to the Machine, the volume or the
image's storage layout. A drill may stop before step 4 — destroy the clone and its volume
instead of the original — and still prove the mechanics. What the drills taught:

1. **A restored volume is born with retention 5, not 14.** `fly.toml`'s
   `snapshot_retention` governs only volumes `fly deploy` creates; `fly machine clone
   --from-snapshot` uses Fly's default. Step 5 of the procedure above was added for it.
2. Between steps 2 and 4 the Fly proxy may route the public `/api/health` to the started
   clone while the original stays `stopped` — convenient for verification, but it means
   the app serves from the restored copy before anyone decided it should. Do not leave
   the two-Machine state standing.
3. **A drill against an empty database proves only the mechanics.** The authenticated
   read and the `?q=` search in step 3 need users, entries and an FTS index worth
   searching, so the drill that matters is one run on real data.

### 3b. Phase-1 localhost server (retired with the cutover, #17)

Script: `server/backup.sh` — a consistent copy using SQLite's online backup API (through
the server venv's Python), checked with `PRAGMA integrity_check` before it counts, keeping
14. It is also the tool that produces the migration snapshot in §2, and since D18 it is what
the Compose topology's `backup` service runs (`docs/SERVER-GUIDE.md` Part 2 §8).

```bash
LOOM_DB=~/.loom-server/loom.db bash server/backup.sh
# -> ~/.loom-server/backups/loom-<UTC time>.db  (purges what it named past the newest 14)
```

Backups taken before D18 are named `loom-<date>-<time>.db`; the script never purges a name
it does not write, so delete those by hand once they are not needed.

**Scheduling (cron):**

```cron
# loom-server-backup
0 3 * * * /home/<user>/<repo>/server/backup.sh >> /home/<user>/.loom-server/backup.log 2>&1
```

> **After the cutover this entry is dead weight.** Left installed, it still runs nightly,
> but it backs up `~/.loom-server/loom.db` — the **retired** phase-1 database (§0). It
> protects nothing: the authority is the Fly volume, whose backups are snapshots (§3a).
> Remove it with `crontab -e`; do not mistake it for live protection.

To view/edit: `crontab -l` / `crontab -e`. On machines without cron, run it by hand or
hook it to server startup.

**Restore** (stop the server, copy a backup over the DB, start it again):

```bash
systemctl --user stop loom-server
cp ~/.loom-server/backups/loom-<UTC time>.db ~/.loom-server/loom.db
systemctl --user start loom-server
```

> The backup inherits the sensitivity of the data (same host, same permissions) — DESIGN §8.

---

## 4. Adding / removing users and machines

On the hosted deployment every command below runs inside the Machine (§2 → "Administering
users"). Shown here against a local `LOOM_DB` for brevity.

```bash
cd <repo>
export LOOM_DB=~/.loom-server/loom.db
# Add a user AND invite their first machine — prints a single-use enrolment code, never a token
.venv/bin/python -m server.admin invite --name dev2 --email dev2@example.test     # --ttl 24h default
.venv/bin/python -m server.admin invite --email dev2@example.test                 # their second machine
.venv/bin/python -m server.admin listusers
.venv/bin/python -m server.admin listdevices [--email dev2@example.test]
.venv/bin/python -m server.admin revokedevice --id 3                              # ONE machine, lost or stolen
.venv/bin/python -m server.admin revokedevice --id 3 --no-cascade                 # ONE machine, replaced (retire)
.venv/bin/python -m server.admin deactivate --email dev2@example.test             # the person: every token of theirs
.venv/bin/python -m server.admin revoke-orphans                                   # once, on the D19 upgrade (§2)
.venv/bin/python -m server.admin promote --email dev2@example.test                # role admin (D20)
.venv/bin/python -m server.admin demote --email dev2@example.test                 # role member
# Break-glass: a permanent token printed once (pre-#20 flow; still valid, never expires)
.venv/bin/python -m server.admin adduser --name dev2 --email dev2@example.test
```

**Roles (D20).** Every user is a `member` (`invite` and `adduser` create members, and an
existing database's users all start as members); `listusers` shows the role in its last
column. **Bootstrap the first admin** by promoting an existing user, normally yourself
right after your own `invite` and `loom enroll`:

```bash
.venv/bin/python -m server.admin promote --email dev2@example.test   # -> User dev2@example.test promoted to admin.
```

Once there is an active admin the server never loses its last one: `demote` and
`deactivate` of the **last active admin** exit 1 with `last_admin` and change nothing, so
promote the successor first. A deactivated admin does not count. `promote`/`demote` of a
user who already has that role says so and changes nothing; an unknown email exits 1.
They are the only `admin.py` commands that record an `auth_events` row (`promoted` /
`demoted`, with no actor, since nobody is signed in to a shell); the other commands
record none, as before (retention of these rows: "Settings for the web's writes" below).

**From the web (D20, DESIGN §6.4).** An admin does the same in the browser: `loom ui` →
*Users* (`/ui/admin/users`) lists every user with their role, state, pending invitation and
machines. **Invite** (email, name, role) has `invite`'s semantics — a new email creates the
user with that name and role, a known one keeps both and gets a new 24 h code — and shows
the invitation link once. **Deactivate** and **Change role** have `deactivate`'s and
`promote`/`demote`'s effects, the last-admin refusal included (`409 last_admin`), and each
live machine has a **Retire** and a **Revoke (lost or stolen)** button (`revokedevice
--no-cascade` and `revokedevice`; "Retire or revoke" below). Every web change records an
`auth_events` row naming the admin (`user_invited`, `user_deactivated`, `promoted`,
`demoted`, `device_retired`, `device_revoked`); a member gets `403` on that page.

**Settings for the web's writes (D20).** Three server settings, read from the environment
(`fly.toml [env]`; on Compose, the `loom` service's `environment:` in
`deploy/compose/docker-compose.yml`):

| Setting | Default | What it does |
|---|---|---|
| `LOOM_PUBLIC_URL` | Fly: `https://$FLY_APP_NAME.fly.dev`; Compose: `https://${LOOM_DOMAIN}`; otherwise none | The URL people open the UI at. Every `/ui` write must come from that origin (`403 bad_origin` otherwise), so set it whenever the browser reaches the server under another name. With no URL at all, every write — sign-out, "Add a machine", "Revoke", the *Users* forms — answers `403 public_url_unset` while browsing still works, and startup logs one `WARNING` from the logger `uvicorn.error.loom.public_url`: `public_url_unset: no LOOM_PUBLIC_URL (or FLY_APP_NAME), so every /ui write is refused (D20)`. Seeing it after a deploy means: set `LOOM_PUBLIC_URL` and restart |
| `LOOM_SETUP_CODE_TTL_S` | `600` (10 minutes) | Life of a setup code from *My machines* → "Add a machine", in seconds; read when a code is issued. A non-positive or non-numeric value is the default |
| `LOOM_AUTH_EVENTS_RETENTION_DAYS` | `180` | `auth_events.client_ip` (the address of every web action and sign-in) is **personal data**, so the server deletes `auth_events` rows older than this many days every time it starts. A non-positive or non-numeric value is the default |

Revoking a machine or deactivating a person also ends their open web-UI sessions (D14) on
their next request; there is nothing UI-specific to revoke.

`revokedevice --id` takes **any** id of the machine's chain (the `chain` column of
`listdevices` names it by its first token) and revokes the whole chain: every token the
machine has renewed through, so no older one keeps working. `deactivate` does it for every
machine of the person at once, **expires their pending enrolment codes** and refuses their
break-glass `adduser` token, and prints how many tokens and codes it revoked and expired.
It is **one-way** for machines and codes: there is no `reactivate`, and flipping
`users.active` back by SQL brings no machine and no code back (only a break-glass token,
which was refused, not revoked). A returning person is re-created with a new email (or by
SQL) and enrols again.

**The issuer cascade (D20).** A machine added from the web (*My machines* → "Add a
machine") was enrolled with a setup code that names the machine whose `/ui` session issued
it. Every chain revocation — `revokedevice`, `deactivate`, the web's "Revoke" and B2's
re-enrolment — also revokes, in the same transaction, every chain minted from a setup code
one of the revoked tokens issued, then the chains those issued, until nothing changes; and
it expires every code still unredeemed, setup code or web invitation, that any of those
tokens issued. A web invitation that someone already redeemed is not followed: the person
it invited keeps their machine. Machines enrolled from an `admin.py invite` name no
issuer, so revoking another machine never reaches them. The counts `revokedevice` and
`deactivate` print include the cascade.

**Retire or revoke (D20).** *Retiring* a machine — `revokedevice --id N --no-cascade`, or
"Retire" in the web — revokes its chain and expires every code its tokens issued that
nobody redeemed yet, **without** the cascade: a machine added from it keeps working, and
nothing else is touched. Use it for a machine that was **replaced**, sold or wiped: the
normal way to replace a laptop is "Add a machine" from the old one, enrol the new one, then
retire the old one. Use plain `revokedevice` ("Revoke (lost or stolen)" in the web) for a
machine that was **lost or stolen**: whoever holds it may have added machines from it.
`--no-cascade` prints `Device token N and its renewal chain retired (<n> token(s); …)`, or
the same `error:` line and exit 1 when the chain has no live token. Neither form of
`revokedevice` records an `auth_events` row (of `admin.py`'s commands only `promote` and
`demote` do); the web's Retire and Revoke record `device_retired` and `device_revoked`.

**An enrolment code sent to the wrong person or channel** is not a reason to `deactivate`
(that kills the person's working machines). Expire that user's unredeemed codes, then send
a fresh `invite`. The image has no `sqlite3` binary; Python's own CLI does the same:

```bash
.venv/bin/python -m sqlite3 "$LOOM_DB" "UPDATE enrolment_codes SET expires_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE redeemed_at IS NULL AND expires_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND user_id = (SELECT id FROM users WHERE email = 'dev2@example.test')"
# On Fly, inside the Machine (the volume's LOOM_DB is /data/loom.db):
fly ssh console -a <app> -u app -C "python -m sqlite3 /data/loom.db \"UPDATE enrolment_codes SET expires_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE redeemed_at IS NULL AND expires_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND user_id = (SELECT id FROM users WHERE email = 'dev2@example.test')\""
```

Send the enrolment code through the team's channel; it is useless once redeemed and dead
after its TTL. Device tokens live 90 days and renew themselves while the machine keeps
running passes; a machine idle for longer than that re-enrols with a fresh invitation.

**Re-enrolling a machine** (a reinstall, a token rejected after a long idle) is a fresh
`invite` for the same email and `loom enroll` on that machine. When the machine re-enrols
with the **same server** it already uses, the client sends its current token along and
the server revokes that token's whole chain (D19 B2): `listdevices` then shows the old
chain `revoked` and the new token `active`, with nothing to run. Only a live device token of the invited person counts: a machine
moving from another server, one whose `config.json` is gone, or a laptop handed to
someone else (invited under their own email) leaves the old chain alive, so revoke it
with `revokedevice --id` as above.

---

## 5. Onboarding another dev machine

Requirements: `git`, `jq`, `uv` (or `python3 -m venv`), `claude` (same version as
`contract/VERSION`), a session-detach launcher for the kick (`setsid` **or** `python3` —
see "Supported platforms" below), `curl`, `tar` and `sha256sum` or `shasum` (install.sh
downloads gitleaks with them), and HTTPS access to `<app>.fly.dev` and to `github.com`.
**gitleaks is mandatory (D16)**: install.sh downloads the official gitleaks **8.30.1**
release asset for darwin/linux × x64/arm64, verifies its sha256 against the values pinned
in the script, and installs it as `~/.loom/bin/gitleaks` (no sudo). A checksum mismatch or
another platform aborts the install with nothing installed; on another platform install
gitleaks 8.30.1 on your `PATH` yourself and run the installer with `LOOM_SKIP_GITLEAKS=1`.
An offline machine can be given the asset with `LOOM_GITLEAKS_TARBALL=<path to
gitleaks_8.30.1_<os>_<arch>.tar.gz>` — it is verified exactly like a download. Before this
flow, the operator runs `invite` for this machine through §2 → “Administering users”
(inside the Fly Machine, against the mounted database) and sends the **enrolment code**
through the team's agreed channel. The code is single-use and expires (24 h by default);
nothing permanent ever travels. Step 3 reads it at a hidden prompt — there is no flag to
pass it on the command line, so it cannot land in shell history.

**Onboarding with a link (D19).** When the server knows its public URL (`LOOM_PUBLIC_URL`;
on Fly it defaults to `https://<app>.fly.dev`; SERVER-GUIDE Part 1 §7), `invite` ends with
an **invitation link**, `https://<app>.fly.dev/ui/join#code=…`, and that link is what the
operator sends instead of the bare code. Opened in a browser, it shows a static page with
the steps below; the code rides in the link's fragment, which the browser never sends to
the server. At step 3 the engineer pastes the **whole link** at the same hidden prompt,
in place of the bare code. If `invite` printed no link (a hint on stderr says why), pass
`--public-url https://<app>.fly.dev` or send the bare code as before.

```bash
# 1. Clone the tool's repo
git clone <repo-url> loom && cd loom

# 2. Install the client (creates ~/.loom, installs the checksum-verified gitleaks 8.30.1 in
#    ~/.loom/bin, the venv, the hook in settings.json with a backup, the skills, the 15-min
#    periodic trigger — systemd user timer, or cron, see §2 — and the loom block in
#    ~/.claude/CLAUDE.md that makes the agent run `loom check` at task start)
bash client/install.sh            # asks to confirm each merge; --yes for non-interactive

# 3. Enrol this machine: paste the invitation link (or the bare enrolment code) at the
#    hidden prompt. The machine mints its own 90-day device token, written to
#    ~/.loom/config.json (600) and renewed silently.
~/.loom/venv/bin/loom enroll --server-url=https://<app>.fly.dev
#    The machine is listed server-side under its short host name (the part before the
#    first dot, so no network domain; 'device' if there is none); --device-name=NAME
#    sends NAME verbatim instead. A machine enrolled before 2026-10-17 keeps the full host
#    name it sent then, until it re-enrols.

# 4. Verify the environment (all ✓ except, at most, optional warnings)
~/.loom/venv/bin/loom doctor      # includes "device token expires … (N days left; renews automatically …)"

# 5. Register a project (inside its repo, which must have a git remote; NO real customer data)
cd <project-repo> && ~/.loom/venv/bin/loom register
#   or the /loom-register skill from Claude Code

# 6. A project a teammate already registered is NOT captured from this machine until you
#    say so here (D9: enrolment leaves the machine in opt-in mode). Inside that repo:
~/.loom/venv/bin/loom enable      # loom disable records the "no"; loom status lists what was skipped
#    Capture starts at that moment (D9 amendment, 2026-09-30): older sessions of the repo
#    are read once, never summarized, and logged as dropped_before_enable.
#    You do not have to remember this: the CLAUDE.md block makes the agent run `loom check`
#    at the start of a task, and in a registered-but-undecided repo it asks you once.
~/.loom/venv/bin/loom check       # not_registered | registered_undecided | enabled | disabled | unknown

# 7. See what Loom holds, in a browser, signed in (D14; D20: My machines, Sign out)
~/.loom/venv/bin/loom ui          # the projects overview;  loom ui <project> opens that project
#    WSL2: Python finds no browser on a stock install, so the link is always printed too
#    (single-use, 2 minutes). To have it open by itself: sudo apt install wslu, then
#    export BROWSER=wslview   (or BROWSER=explorer.exe) in your shell profile.
```

`loom init --server-url=…` (a permanent `adduser` token at a hidden prompt, no lifetime
keys) remains the break-glass form of step 3; `loom doctor` flags such a token with a `⚠`
and suggests `loom enroll`.

From then on, whenever a Claude Code session closes in a registered repo, the hook marks
the transcript and launches the processor (kick); the entry appears in the logbook within
seconds. **No manual `loom process` is needed any more**: whenever the kick does not run
(crash, `kill`, WSL shutdown, or no launcher at all — see the table below and §2) the
periodic trigger picks the marker up on its next pass, so the worst case is ≤ 15 minutes
instead of "never".

#### Supported platforms — how the kick detaches

The kick must start the processor in a **new session**, or it is killed along with the
Claude Code session that spawned it. Backgrounding alone (`&`, `nohup`) does not do that:
it survives the hook *exiting* while leaving the process in the hook's own process group
and session. The hook therefore tries two launchers, in this order:

| Host | Tier used | Prerequisite |
|---|---|---|
| Linux / WSL2 | `setsid` | util-linux, present on every supported distro |
| macOS | `python3` | macOS does **not** ship `setsid`. `python3` comes with the Xcode Command Line Tools (`xcode-select --install`) |
| Neither available | *none* | The kick is **skipped and logged**; the 15-minute trigger is the only recovery |

Check a host with one line — it settles which tier that machine will use:

```bash
command -v setsid python3          # and/or: ~/.loom/venv/bin/loom doctor
```

`loom doctor` carries a row for this: `✓ SessionEnd kick session-detach launcher: …` when
one resolves, `⚠ no session-detach launcher on PATH …` when none does. It is a warning and
never a failure — a missing launcher costs *latency*, not correctness, because the periodic
pass still drains the queue.

What an operator sees when no launcher is present: `loom doctor` shows the `⚠` row, and
`~/.loom/log/hook.log` records `kick skipped (no session-detach launcher); the marker will
be processed by the periodic pass` for each closed session. Entries still land — up to 15
minutes late instead of within seconds.

**What hook.log can and cannot tell you.** When a launcher *is* found, hook.log records
`kick dispatched (launcher=…)`. It says *dispatched*, not *fired*, and the difference is
real: confirming the launcher reached the processor would mean waiting on it, which is the
one thing a SessionEnd hook must never do. So hook.log tells you which tier the hook handed
the work to; whether it then worked shows up in `~/.loom/log/processor.log`, or simply in
the queue draining.

**The one case all three signals miss — macOS without the Xcode Command Line Tools.** There,
`/usr/bin/python3` is a stub that satisfies `command -v` and then prompts to install the
tools instead of running. The launch is backgrounded with stdin at `/dev/null`, so it can
never block or prompt inside the session shutdown — but the kick does not happen, hook.log
still says `dispatched`, `loom doctor` still says the launcher resolved (it would have to
*run* the stub to learn otherwise, which is what triggers the prompt), and because the
prompt is a native dialog rather than a Python error, `processor.log` may stay empty too.
The queue draining only on the 15-minute pass is the symptom. `xcode-select --install`, or
any real python3 earlier on PATH, is the fix. This is the open risk in choosing `python3`
over `perl` for tier 2 — `/usr/bin/perl` is stock on macOS and needs no such provisioning
— and it is why the check below is worth running on a pilot Mac before relying on the
immediate kick there.

> **Verified on darwin.** On a real Mac (Xcode Command Line Tools present), closing a real
> Claude Code session inside a registered repo logged `kick dispatched (launcher=python3)`
> and the entry was `posted` seconds after the session closed — the kick works on macOS
> through the `python3` tier, exactly as the Linux CI exercises it
> (`client/tests/integration/test_hook_install.py`), and the full suite passed there. What
> remains open is only the bookkeeping: the darwin entry in the
> `regression_known_failures` of the four `test/phase_gates/phase_N_e2e_config.json` can
> now be removed.

**macOS: the cron trigger blocks until you grant cron access to `~/Documents`.** The
client is installed *editable* (`install.sh` step 2), so every unattended pass imports
`loom` from the checkout. When the checkout lives under `~/Documents` (or `~/Desktop`,
`~/Downloads`), macOS's privacy layer (TCC) stops the **cron-launched** pass on its first
file open and shows a "cron would like to access files in your Documents folder" dialog;
the process sleeps in that `open()` until the dialog is answered — a pass can sit for
minutes with no line in `processor.log`, while the SessionEnd kick (which inherits the
terminal's consent) captures normally. Click **Allow** once, or add
`/usr/sbin/cron` under *System Settings → Privacy & Security → Full Disk Access*; later
passes run unprompted. Until then the kick is the only path that captures on that Mac,
and each 15-minute pass adds one more sleeping process. `loom doctor` cannot see this
(it runs from the terminal, not from cron); the symptom is `sweep_last_run.json` never
advancing on the quarter hour.

**Updating an existing machine** (pull + re-copy + re-arm the trigger + doctor, in one go;
it keeps the venv and does not touch `settings.json` when the hook is already registered):

```bash
cd <repo> && bash client/install.sh --update         # lists the incoming commits, then asks
cd <repo> && bash client/install.sh --yes --update   # no prompt; exits with doctor's exit code
```

> **Trust assumption — `--update` runs code you have not read.** Whatever the pull brings
> down is executed by the SessionEnd hook and installed as a **persistent user timer**;
> `--ff-only` constrains the *shape* of the history (no rebase, no force-push landing
> silently), not its *contents*. A commit that rewrites `loom-process.service`'s `ExecStart`
> would be installed and enabled. We accept this: small team, private remote,
> same trust level as the repo itself. That is why the interactive form prints the incoming
> commit range and asks before applying — **`--yes` skips that check**, so keep `--yes` for
> your own CI and prefer the plain form when updating from a remote you have not just
> reviewed. There is no signature verification and none is planned for phase 2.

**Consumption (reviewer):** `/loom <project>` from Claude Code, or
`~/.loom/venv/bin/loom log <project> --limit 30`.

### Upgrading a pilot machine to the typed prompt (v2)

Phase 4 ships a second summarizer prompt, `prompts/summarizer_v2.md`, in which every
`## Decisions` bullet leads with one label from a closed vocabulary — `[decision]`,
`[method]`, `[task]`, `[fact]` — so a standing rule ("always run `make contract` after
CLI updates") stops being indistinguishable from a one-off choice. The **processor**
parses those labels (never the LLM) and records the aggregate as `item_types:` in the
entry's frontmatter and in the POST payload; a bullet that does not match the grammar
is captured as `[decision]` and counted, never dropped and never failed.

A fresh `loom init` writes `"prompt_version": "v2"` already. An **existing** machine
keeps whatever its `~/.loom/config.json` says (pilots installed before phase 4 say
`"v1"`), so the upgrade is one key:

```bash
bash client/install.sh --yes --update    # ships summarizer_v2.md next to v1 (v1 keeps working)

# then edit ~/.loom/config.json:  "summarizer": { ..., "prompt_version": "v2" }
# NOTE the umask: the temp file holds your bearer token and would otherwise be
# created world-readable (and survive a failed mv). Same rule as install.sh/loom init.
(umask 077; jq '.summarizer.prompt_version = "v2"' ~/.loom/config.json \
   > ~/.loom/config.json.new) && mv ~/.loom/config.json.new ~/.loom/config.json

~/.loom/venv/bin/loom doctor             # "summarizer prompt present (.../summarizer_v2.md)"
```

Notes:

- **Only `prompt_version` changes.** The prompt file is derived from it
  (`~/.loom/prompts/summarizer_<version>.md`), so there is no second key to keep in
  sync. The optional `prompt_path` key still overrides the derivation when it is set —
  if your config has one (test machines do), point it at the v2 file or delete it. A
  `prompt_version: "v2"` with a `prompt_path` still on v1 is the one combination that
  corrupts silently (v1 output parsed as v2, every item typed `[decision]`); `loom
  doctor` prints the resolved path, so compare the two before you walk away.
- **Typos resolve, they do not half-apply.** The version is normalized before anything
  uses it: `"V1"`/`"v1 "` are read as `v1`, and a value that is not a bare token at all
  falls back to the default. A *token-shaped but unknown* version (`"v9"`) is the one
  that bites — its prompt file does not exist, `loom doctor` says "summarizer prompt
  missing", and until it is fixed the processor keeps every marker queued (`loom status`
  shows the errors) rather than capturing with the wrong prompt.
- **Rolling back is the same edit** in reverse (`"v2"` → `"v1"`). Both prompts stay
  installed: `install.sh` adds v2, it never replaces v1.
- **No duplicates and no re-typing.** `prompt_version` is part of every entry's
  `content_hash`, so the same session summarized under v1 and later under v2 are two
  distinct entries rather than a dedupe collision. Entries already captured under v1
  are left exactly as they are — they carry no `item_types` and are never re-parsed.
- **What to check afterwards:** the next entry's frontmatter carries an `item_types:`
  line. Items that missed the grammar show up in `~/.loom/log/processor.log` as a
  single `label_fallback  count=<n>` line per CAPTURED entry — written next to the
  entry's `posted`/`duplicate`/`quarantined` line, so a server outage and its retries
  cannot inflate it (counts only; the log never records label or summary text). A
  rising count means the model is drifting off the grammar — most likely translating
  the labels in a non-English session — not that entries were lost: every fallback
  item was still captured, as `[decision]`.

### Moving a pilot machine to summarizer v3 (D11)

`prompts/summarizer_v3.md` keeps v2's four labels and its `## Decisions` section, and
changes what an item is: only what still holds at the **end** of the session (a choice
reversed mid-session is one item whose rationale names the discarded option), no
per-item `Status:`, **all** pending work as `[task]` (the `## Open items` section is
gone; `## Open questions` holds only undecided questions), `[decision]` only for a real
choice between alternatives (otherwise `[fact]`), and headings always in literal English
whatever the session's language. Its behaviour is measured by `test/prompt_eval/`
(`RESULTS.md`: v3 passes every case in every run; v2 is the baseline).

**Do this before the pilot window opens.** An entry is permanent (D7): a session
captured under v2 keeps v2's problems forever.

- A **fresh** `loom enroll` writes `"prompt_version": "v3"`. `loom init` and a config
  that omits the key still resolve to v2 — that default is frozen.
- An **enrolled** machine keeps what its config says. Move it with the same one-key edit
  as the v2 upgrade above:

```bash
bash client/install.sh --yes --update    # ships summarizer_v3.md next to v1 and v2
(umask 077; jq '.summarizer.prompt_version = "v3"' ~/.loom/config.json \
   > ~/.loom/config.json.new) && mv ~/.loom/config.json.new ~/.loom/config.json
~/.loom/venv/bin/loom doctor             # "summarizer prompt present (.../summarizer_v3.md)"
```

The resulting key is `"summarizer": { ..., "prompt_version": "v3" }`. Rolling back is the
same edit with `"v2"`; entries already captured are never re-parsed, and the next entry's
frontmatter says `prompt: "v3"`.

### Moving a pilot machine to summarizer v4 (D15)

`prompts/summarizer_v4.md` is v3 with the entry written **in English** whatever the
session's language — prose, rationale and evidence (a quote is translated and kept in
quotation marks; file names, commits, identifiers and code stay as written). Everything
else is v3's, byte for byte. Measured by `test/prompt_eval/` (`RESULTS.md`, v3 as the
baseline), which since D15 also fails any output that is not English. On 2026-10-01 v4
passed every case in every run; v3 failed every Spanish case — and 3 of its 15 runs on
*English* cases, where it wrote the entry in Spanish anyway.

- A **fresh** `loom enroll` writes `"prompt_version": "v4"`.
- An **enrolled** machine moves with the same one-key edit:

```bash
bash client/install.sh --yes --update    # ships summarizer_v4.md next to v1, v2 and v3
(umask 077; jq '.summarizer.prompt_version = "v4"' ~/.loom/config.json \
   > ~/.loom/config.json.new) && mv ~/.loom/config.json.new ~/.loom/config.json
~/.loom/venv/bin/loom doctor             # "summarizer prompt present (.../summarizer_v4.md)"
```

Entries captured under v3 keep their language (D7). To re-summarize a project's entries
under v4, rewind the pointers of their transcripts and supersede the old entries with
the new ones — see "Re-summarizing entries under a newer prompt" in §6.

---

## 6. Daily operation and validation

```bash
~/.loom/venv/bin/loom status        # pending markers, failed/, quarantine, errors, cache, version
~/.loom/venv/bin/loom doctor        # environment checklist (incl. trigger, sweep age, scanner)
```

Processing is unattended: the kick handles the normal case and the 15-min trigger (§2) the
rest, so running `loom process` by hand is only a debugging tool now (`--quiet` off gives
you the verbose pass). To force one pass immediately:
`systemctl --user start loom-process.service` on systemd hosts, or just
`~/.loom/venv/bin/loom process` anywhere.

- **Trigger health**: `loom doctor` reports whether the periodic trigger is installed
  (`loom-process.timer` enabled+active, or the `# loom-process` crontab tag) and how old the
  last orphan sweep is. A stale/absent sweep is a `⚠`, never a failure — the timer may not
  have fired yet, and after a WSL2 resume it is expected for one period (§2). A **missing**
  trigger is a `✗`: re-run `bash client/install.sh --yes` to reinstall it.
- **Scanner** (the first row): `✓ scanner engine gitleaks 8.30.1 (pin matched)` is the
  healthy state. A version other than the pin, or one it cannot parse, is a `⚠` — capture
  still runs. A `✗ scanner engine unusable … capture is PAUSED (D16)` means the next
  `loom process` will stop before touching any marker; see "Capture paused" below.
- **Trigger scanner parity**: a row of its own, because "the scanner is fine" and "the
  trigger is armed" were both true on the day the *triggered* scans were running with the
  weaker engine (§2 "scanner parity", §7 finding 1). With gitleaks in `~/.loom/bin` it is a
  `✓` (the pass runs it by absolute path); with only a `PATH` gitleaks it compares what the
  installed trigger's environment can resolve with what your shell resolves, and a `⚠`
  means the unattended pass would find no gitleaks and pause capture — re-run
  `bash client/install.sh --yes`. It is never a `✗`: the kick from the SessionEnd hook runs
  with your session's environment and still captures.
- **Capture paused (D16)**: gitleaks is mandatory. When it is missing (no
  `~/.loom/bin/gitleaks` and none on `PATH`), when `gitleaks version` cannot run, or when
  `scanner.engine` is anything but `gitleaks` (`auto` is read as `gitleaks`; `regex` is no
  longer accepted), every `loom process` pass stops **before the first marker**: nothing is
  summarized or uploaded, every marker stays in `queue/` byte-identical and no attempt is
  counted, so nothing is lost and nothing is parked. The pass logs one `paused_no_scanner`
  line and exits 1 (the systemd unit shows as failed); `loom status` opens with
  `✗ CAPTURE PAUSED since <time>: <reason>`, and `loom doctor`'s scanner row is a `✗`.
  `loom push-quarantine` and `loom curate` refuse to run for the same reason. To recover:
  `bash client/install.sh --yes --update` (it downloads and verifies gitleaks 8.30.1 into
  `~/.loom/bin` and sets `scanner` in `config.json` to `gitleaks` / `8.30.1`), check
  `loom doctor`, then run `~/.loom/venv/bin/loom process` once — the queued markers are
  captured in that pass.
- **Redaction before the summarizer (D16, step 5b)**: the transcript extract is scanned
  (gitleaks ∪ the regex set) and every detected value is replaced by `[REDACTED:<kind>]`
  before the summarizer reads it; the run log records `redacted_before_summary` with the
  kinds and counts only. If the redacted text still trips the scanner, or the scan fails,
  the summarizer is not run: the marker is kept with a `pipeline_error` naming kinds and
  counts, retried, and parked in `failed/` after 3 attempts like any pipeline failure.
- **Unit integrity** (systemd hosts): compares the installed
  `~/.config/systemd/user/loom-process.{service,timer}` against the templates shipped in
  `client/timer/`, after resolving the substitutions install.sh makes (`ExecStart`,
  `Environment=LOOM_HOME=`, `Environment=PATH=`). Drift — a hand edit, a bad `--update`, a
  hostile pull that rewrote `ExecStart` — is a `⚠` naming the unit file; re-run
  `bash client/install.sh --yes` to restore it, or diff it yourself first if you did not
  expect it. On cron hosts the row simply reports there is nothing to compare. Like every
  other doctor row this is a **configuration** check (see the box below): it proves the files
  on disk match the checkout, not that what ran was what the files say.

> **`loom doctor` is a configuration check, not an attestation.** It resolves `crontab`,
> `systemctl` and (as a fallback to `~/.loom/bin`) `gitleaks` through `PATH`, by design, so
> anyone who can write to an early `PATH` entry — or to `~/.loom` — for your user can decide
> what it reports — a fake `gitleaks` that prints the
> pinned version and exits clean makes every scanner line green while nothing is scanned.
> A green doctor means "this machine looks configured the way we expect", and it must never
> be cited as evidence that secret scanning ran or that the trigger fired. For that, look at
> the actual artefacts: `journalctl --user -u loom-process`, `~/.loom/log/processor.log`,
> `~/.loom/sweep_last_run.json`, and the entries themselves.
- **Quarantine** (a secret detected in a summary): review the file in
  `~/.loom/quarantine/`, redact it and upload it with
  `~/.loom/venv/bin/loom push-quarantine <file>` (it rescans; rejects if still dirty).
- **Stuck marker** (`failed/`, `adapter_error`, `registry_error`, `pipeline_error`): shows up in
  `loom status`. Diagnose in `~/.loom/log/processor.log` and `~/.loom/log/hook.log`.
  - `registry_error` / `kept_server_unreachable` are self-healing: they retry on the next
    pass and clear once the server or the registration is back.
  - `kept_summarizer_failed` is bounded — three attempts and the marker moves to `failed/`.
  - `kept_pipeline_error` means the processor hit a bug or environmental failure outside
    its named failure paths. It is bounded the same way: after three attempts the marker
    moves to `failed/`. Inspect the sanitized `pipeline_error` and nearby log lines, fix
    the underlying condition, then clear the park as described below.
  - **A torn transcript is no longer an operator problem.** A session killed mid-write
    leaves its last JSONL line truncated — sometimes inside a multi-byte character. Since
    P2.F1 the delta reader *classifies* such a line as torn instead of failing on it: an
    interior tear is skipped and counted (`torn_lines_skipped` in `processor.log`), and a
    **trailing** tear ends the delta before it, so the pointer parks below EOF and the
    held lines are reported (`held_lines_at_eof`) but not consumed. When the session is
    resumed and the half-written line is finished, the next pass captures it and the entry
    chains onto the previous one. Nothing is stranded, nothing is retried forever, and no
    human has to touch the file. (Proven end to end, for both a mid-line and a
    mid-multi-byte tear, by `client/tests/e2e/test_phase3_e2e.py`.)
  - **Manual recovery is now only for genuinely-corrupt transcripts — the schema-invalid
    case.** That is a line which parses as JSON *and* decodes as UTF-8 but is not a valid
    Claude Code message: a `user`/`assistant` line with no `uuid` or no `message`. It still
    raises `AdapterError`, and since P2.F2 that is bounded exactly like a summarizer
    failure — three attempts, then the marker is moved to `~/.loom/failed/` and the sweep
    stops re-enqueueing that session (so it costs no reads while it sits there). In
    `loom status` it appears under `Parked (failed/)` as `sess-xxx [adapter_error]`, and
    `~/.loom/log/processor.log` names the offending line number.
    **Resolution**: read that log line, then either repair the transcript (delete or fix the
    offending line — everything else is captured on the next pass) or accept the loss for
    that session. Either way you must **clear the park by deleting
    `~/.loom/failed/<session-id>.json`**: that tombstone is what keeps the sweep away from
    the session, so until it is gone even a repaired transcript is never picked up again.
    (If the marker is still pending rather than parked, the file to remove is
    `~/.loom/queue/<session_id>.json`.)
    Before assuming corruption, check *what* the recorded `adapter_error` says: a transient
    I/O error currently parks a session the same way a corrupt one does — see §7 checklist
    row 11 and the phase-4 item `p4-adapter-errno-split`.
- **Sessions that ran in an agent worktree never show up** (issue #4). Agent harnesses run
  each session in a git worktree they delete the moment the agent finishes — usually
  seconds after `SessionEnd`, always before the 15-minute pass. Project identity is
  resolved from the marker's `cwd`, so by then there is nothing to resolve.
  Since issue #4 the SessionEnd hook captures the remote **while the worktree still
  exists** and writes it into the marker as `cwd_remote_url`; the processor uses it only
  when the live `cwd` no longer answers. The authority rule is
  `contract/ATTRIBUTION.md` §2. Three lines in `~/.loom/log/processor.log` tell you which
  path a session took:

  | Line | Means | Action |
  |---|---|---|
  | `attribution_from_marker <canonical>` | the worktree was gone and the captured remote was used — **this is the fix working** | none |
  | `attribution_superseded <canonical>` | the `cwd` still resolved and disagreed with the capture, so the **live** value won | none, unless the canonical named is not the project you expect — then the path was recycled for another repo (`ATTRIBUTION.md` §6 L-2) |
  | `dropped_no_remote  fallback not normalizable` | a capture was present but unusable | the marker is gone; the transcript is still on disk and the pointer never moved, so re-marking the session re-processes it |
  | `dropped_no_remote  no git remote origin` | no `cwd`, no capture — nothing to attribute to | see the two rows below |
  | `kept_pipeline_error  attempts=N ...` | the processor hit a bug or environmental failure while handling this marker | inspect the sanitized error and nearby lines; it retries up to three times, then follow the `failed/` recovery procedure above |

  **If you see `dropped_no_remote` on a worktree session that should have worked**, check
  `loom doctor` first: `install.sh` copies the hook into `~/.loom/hooks/`, so a machine
  that installed before this feature keeps running the old hook and loses every worktree
  session *silently*. The doctor row names it — re-run `install.sh` to update.

  **`sweep_unresolvable_cwd <session> <cwd>` in the log is the residual (L-1).** A session
  that died *without* `SessionEnd` — crash, `SIGKILL`, WSL shutdown — in a worktree that
  was then deleted has no marker and therefore no captured remote. The sweep cannot
  attribute it and withholds it on every pass. This line is the only signal; the transcript
  is intact under `~/.claude/projects/` and the pointer never moved, so the content is
  recoverable by hand if a session matters. The line repeats every pass by design — the
  verdict is recomputed with no tombstone precisely so that registering the project later
  can still recover ordinary orphans.

- **After updating Claude Code** (on any machine): `loom doctor` warns about the version
  change; run `make contract` before trusting that machine's captures. If it fails, fix
  `transcript_adapter`/flags, then update `contract/VERSION` and the fixtures.

### Pilot metrics: `loom stats` and `loom feedback`

What the pilot measures (issues #14/#15) is team totals, never a person:

```bash
~/.loom/venv/bin/loom stats --since 7d     # recalls by source, feedback by verdict, top items
~/.loom/venv/bin/loom feedback --item 17.2 --verdict useful --note "kept the flush from blocking writers"
```

The item id is the one `loom log` prints in front of each item (`<entry>.<n>`); entries
with no ids (pre-D13 captures) cannot receive feedback. The agent files feedback through
`/loom-feedback` at the end of a task, after the person confirms each verdict. The same
feedback twice is reported as `Already recorded` and stored once. The "most useful" list
counts PEOPLE per item, not rows: one person filing the same item twice counts once.

### Reading the logbook in a browser: `loom ui` (D14)

`loom ui` opens the web UI signed in; `loom ui <project>` lands on that project.
The link it prints works once, within two minutes; the browser session then lasts 7 days
and renews itself while it is used (`LOOM_UI_SESSION_TTL_S`), never past the machine's token.

- **Projects** — one row per project: entries, items current and replaced, open tasks,
  last capture, authors of the last 30 days, curation signals. The strip on top is the
  team's last 7 days, never per person.
- **A project** — `Current` (what stands today, by type; tick *Show replaced* to see what
  was reversed and by which item), `Timeline` (every entry with its served status) and
  `Curation` (items someone found stale or not applicable, with who said so, and the item
  link history).
- **An item** — its lineage, every link (revoked ones included), where it came from, and
  every feedback row with its author: the curation tab tells you whose agent consumed a
  stale item.
- **Usage** — the same totals as `loom stats`, over 7 days, 30 days or all time.
- **My machines** (D20) — one row per machine the person enrolled (enrolled, last renewed,
  expiry, state) with "Revoke" on each live one, and "Add a machine": a single-use setup
  code and its `/ui/join#code=…` link, shown once, for `loom enroll` on the new machine
  (10 minutes; §4 → "The issuer cascade").
- **Users** (admins only, D20) — invitations, roles, deactivation and any machine's
  revocation from the browser (§4 → "From the web").

**Sign out** (top right) ends that browser session only. The logbook itself cannot be
changed from the UI (no registration, feedback, links or curation); browsing it is not
counted as a recall.

### Curating (manual, one machine)

Curation links a newer decision to the older one it replaces or restates, so `loom log`
stops presenting the old one as current. It stays **manual** for the pilot (owner decision
E7 of 2026-09-23), until the owner has judged the quality of item links on real entries.
Run it on **one machine only**, always the same one. The curation watermark lives in that
machine's `~/.loom/curation_state.json`, so a second machine would curate from zero again,
pay for the LLM calls a second time and could disagree. Run it about **once a week**:

```bash
~/.loom/venv/bin/loom curate loom-pilot      # one batched LLM pass over the new entries
~/.loom/venv/bin/loom log loom-pilot         # read the new "→ replaced by" notes
~/.loom/venv/bin/loom unlink 7 --reason "the gRPC switch only covered streaming"
```

- An itemized entry (captured with `v2`/`v3` since D13) is judged **item by item**: a link
  hides ONE item (`17.2 → replaced by 21.1 on <date> (link 7)`), never its siblings.
  `loom curate` prints every item link it created with its id.
- A wrong link is taken back with `loom unlink <id> --reason "<one line>"`: the link stays
  on the server as history, with who revoked it and why, and it stops counting (the item
  is current again unless another link still replaces it — `loom log` shows which). No
  later `loom curate` posts it again: the watermark is past it, and a pass that was
  interrupted replays only the links it had not posted yet.
- An entry is only compared with entries of its own kind. Entries with no item ids
  (pre-D13 captures) are still linked to each other as whole entries (`superseded by
  #N`), and those links have no `unlink` (there is no `DELETE /api/links`). An itemized
  entry is never linked to one of them, in either direction: a whole-entry link would
  hide every item of an itemized entry, and an item cannot point at an entry.
- A pass that fails is counted (`loom status` shows `attempts=N`); after 3 the project is
  parked until `loom curate --reset`.

### Re-summarizing entries under a newer prompt

Entries never change (D7). To have a project's entries under a newer prompt (v4 after
D15, say), summarize the same transcripts again and make each new entry **supersede**
its old one (D12): `loom log`, `/loom` and the UI then serve only the new ones, and the
old ones stay stored, reachable with `loom log --all`. It needs the transcripts, so it
runs on the machine that captured them.

1. Move the client to the new prompt first (§5) and check `loom doctor` names it. The
   new prompt version must be in `processor.LABELLED_PROMPT_VERSIONS`, or the new
   entries are posted untyped, which is permanent.
2. List the entries and their transcripts. The session id is the transcript's file
   name. Subagent transcripts live in `<session>/subagents/agent-*.jsonl` and are
   entries of their own.
3. **Holding the processor lock** (`~/.loom/lock`, `flock`; refuse if a pass holds
   it), back up `state.json`, delete the transcripts' keys from it, and queue one
   marker per transcript in `~/.loom/queue/<session_id>.json`:
   `{"session_id", "transcript_path", "cwd": <the repo>, "reason": "resummarize",
   "marked_at", "origin": "operator"}`. The capture cutoff (D9) still applies, so the
   new range is the old one, plus whatever an open session has appended since.
4. `~/.loom/venv/bin/loom process`. Expect one `posted` per marker. The new
   `prompt_version` changes `content_hash`, so nothing comes back as a duplicate.
5. For each pair, `POST /api/links {"from_entry": <new>, "to_entry": <old>, "kind":
   "supersedes", "rationale": "..."}` with the device token, then check that the old
   entries are served as `superseded`.

### What the periodic pass costs

> **Superseded in part by phase 3 (P2.F4).** The baseline below is the pre-P2.F4 cost and is
> kept because it is the measured starting point. What changed, and what did **not**, is
> described in "After the stat-skip" at the end of this section. The re-measured figure lands
> in §9 with the phase-3 gate.

Worth knowing before blaming the timer for disk activity. Every pass re-reads **in full**
every transcript that is settled (mtime older than `sweep.settle_s`, default 30 min) and has
no marker pending, because "has this file grown?" is answered by counting its lines. Files
that are still live, and files already queued, are not read at all.

Measured on a synthetic tree the size of a real developer's history — 301 transcripts /
225 MiB, matching the ~300-transcript / ~200 MiB trees seen in practice —
a steady-state pass opens **301 files and reads all 225 MiB**, in ~60 ms of CPU with
the page cache warm. At the 15-minute cadence that is ~96 passes and **~21 GiB of reads per
day**, growing linearly with the transcript tree. It is cheap on a laptop SSD and invisible
in `journalctl`; it is worth remembering on a spinning disk, an encrypted volume, a network
home directory or a metered cloud disk. If it ever matters, the dials are
`sweep.settle_s` (larger = same cost, later) and the timer cadence itself
(`~/.config/systemd/user/loom-process.timer`) — the read is intrinsic to the current design
and reducing it needs a new state field, not a config change.

#### After the stat-skip (P2.F4)

That new state field now exists. Each `state.json` entry records the transcript's
`(stat_dev, stat_ino, stat_mtime_ns, stat_size)` as of the read its pointer came from, and the
sweep skips the line-count entirely while all of them still match — so a transcript that has
not changed since it was last processed costs **one `stat` instead of a full read**.

**The saving is bounded to transcripts loom actually captures, and that is the honest claim.**
The pair is written only when a session's pointer advances, which happens only when an entry is
delivered. A transcript that never delivers — a project with no git remote, a project not
registered with loom, a transcript that disappeared — never acquires a pair and is still read in
full on the passes that reach it. On a machine whose `~/.claude/projects` is mostly unregistered
projects, that residual is what the pass still costs. Closing it needs a sweep-owned
read-coverage cache independent of the delivery pointer, which is **phase-4 work**
(`p4-sweep-read-coverage-cache`); it also closes the related waste where those same
never-delivering sessions re-consume enqueue slots every pass. Do not read "stat-skip" as
"the sweep no longer reads anything".

**Measured, not argued** (2026-08-10, the phase-3 gate): on a settled tree in which every
transcript has been delivered, a pass opens **zero transcript files**; on a realistic tree of
301 transcripts / 226 MiB where only one project of thirty is registered, a pass reads
**7.5 MiB**. The residual does *not* grow with the size of the unregistered corpus, because
`MAX_ENQUEUE_PER_PASS = 10` stops the scan after ten synthetic markers — so the same ten newest
non-delivering transcripts are read every pass, forever, and the rest are not even looked at.
Bounded, but never draining. Full figures and method in §9.

**How to tell whether it is working on this host.** Each pass logs a `stat_skips=N` count in the
run log: `N` is how many transcripts were resolved by `stat` alone. Compare it with the pass's
`scanned` count in `~/.loom/sweep_last_run.json` — the gap is what still costs a full read.
`sweep_last_run.json` itself is unchanged (its four keys are a frozen phase-2 contract), and its
`skipped` counter deliberately does **not** distinguish a one-syscall skip from a
full-read-then-skip; that split is phase-4 work. Until then `stat_skips` is the signal to read.

### Validating a build (phase 2)

Three commands, in this order, from `<repo>`. All three run entirely against temp
homes/DBs/ports and never touch this machine's pilot server, `~/.loom`, crontab or systemd.

```bash
# 1. Phase gate (the frozen end-to-end oracle; test/phase_gates/phase_N_e2e_config.json)
.venv/bin/python -m pytest test/waves/f/test_goal_e8_e2e.py -q     # phase-3 gate (current)
.venv/bin/python -m pytest test/waves/c/test_goal_e4_e2e.py -q     # phase-2 gate -> 3 passed

# 2. Full regression suite (unit + integration + the repo's own phase-2/phase-3 e2e)
.venv/bin/python -m pytest -q            # or: make test

# 3. Contract check against the installed Claude Code CLI
LOOM_SKIP_LIVE=1 .venv/bin/python contract/verify.py               # -> 0 failures
```

- The gate needs the **real `gitleaks`** (8.30.1 is what the pilot is validated against) and
  `git`, `jq` and the `.venv`; without gitleaks the phase-2 e2e tests **skip** rather than
  fail, so read the skip count, not just the exit code. Since D16 the processor, doctor and
  redaction tests need it as well, and there its absence is a failure.
- The repo's own phase-2 e2e lives in `client/tests/e2e/test_phase2_e2e.py` and runs as part
  of step 2. It covers the same ground as the gate (swept-orphan capture across a server
  outage, planted-secret quarantine → `push-quarantine`, fresh install → green doctor) plus
  hook-vs-sweep entry equivalence, the entries' API shape, and no-duplication across repeated
  passes.
- `client/tests/e2e/test_phase3_e2e.py` is the phase-3 half, also part of step 2: a torn
  transcript (mid-line and mid-multi-byte) resumed and captured whole, an instrumented
  steady-state pass proving zero transcript reads, trigger-context scanner parity under a
  minimal PATH, and `--uninstall-trigger` leaving no crontab while a foreign entry survives
  byte-exact.
- **One known failure, on macOS only:**
  `client/tests/integration/test_hook_install.py::test_hook_marks_transcript_and_kicks_processor`
  — the hook detaches its kick with `setsid`, which does not exist on darwin (same root cause
  as the platform note in §2). Pre-existing and expected there; on the WSL2 pilot host the
  whole suite is green. Anything else red is a real regression.
- **The automated tests deliberately shim `systemctl` and `crontab`**, so they prove the
  installer's *logic*, never that this host's scheduler accepted it. The live systemd branch
  (AC-2.2c) is verified by hand, once per install or update, with the block in **§2**
  ("Manual verification after installing/updating on the WSL2 host"). Do that before
  believing a build is deployed.

---

## 7. Pilot gate

**Window:** starting at the deployment. Gate review at 2–3 weeks.

Lightweight record-keeping: after **every** real review of the pilot project, the reviewer
notes down (in a shared md or a team channel):

1. Did they consult `/loom` during the review?
2. Estimated minutes saved?
3. Any wrong, confabulated or noisy entry?
4. Was anything missing that the author's session did contain?

**Decision rule at closing time:**

- **Continue to phase 2/3** if ≥50 % of the reviews consulted the logbook, the perceived
  saving is net positive and there were **zero** trust incidents (a false entry taken as true).
- **Iterate the prompt** (not the infrastructure) if the problem is noise/vagueness.
- **Stop** if reviewers stop consulting it despite it being available.

### Gate-day checklist

Tick these on the WSL2 pilot host before deciding anything; the first four are the
machine's own evidence, the rest is the human signal the decision rule needs.

| # | Check | How | Pass looks like |
|---|---|---|---|
| 1 | Build is validated | §6 "Validating a build" — all three commands | gate 3 passed · suite green (macOS: the one `test_hook_install` failure) · contract 0 failures |
| 2 | Live trigger armed | §2 manual WSL2 block (AC-2.2c) | `enabled` · `active` · NEXT ≤ 15 min · a manual `start` exits 0 |
| 3 | Environment healthy | `~/.loom/venv/bin/loom doctor` | exit 0; the three phase-2 rows present, none a ✗ |
| 4 | Nothing stuck | `~/.loom/venv/bin/loom status` | `failed/` 0 · quarantine reviewed and empty · no marker older than a day, in particular no `[adapter_error]` (§6) |
| 5 | Capture is actually unattended | on the cron branch (§2): the `sweep_pass` timestamps in `~/.loom/log/processor.log`; on a timer host: `journalctl --user -u loom-process --since '-7 days'` | passes firing every 15 min; entries appearing without anyone running `loom process`. Two interleaved cadences mean two triggers are armed — see §2 |
| 6 | Orphan capture proven in the wild | at least one entry whose session was killed / WSL-shut-down rather than closed cleanly | present — this is the phase-2 feature; if there is none, the sweep is unproven on real data |
| 7 | Secret scanning proven in the wild | `~/.loom/quarantine/` history, or the scanner row in `loom doctor` | either a real quarantine that was reviewed, or an explicit note that nothing triggered (do **not** read a green doctor as proof — §6) |
| 8 | Reviewer records collected | the shared md / channel notes, questions 1–4 above | one entry per real review of the pilot project |
| 9 | Trust incidents | count of "a false entry taken as true" | **zero** — this one is a veto, not a percentage |
| 10 | Decision recorded | the decision rule above, written down with its evidence | continue / iterate the prompt / stop |
| 11 | Transient-I/O parking reviewed | after **several days** of real pilot use: `ls ~/.loom/failed/` and read each parked marker's `adapter_error` | either empty, or every parked session has a *genuinely* corrupt transcript — **not** a transient I/O error (see below) |
| 12 | Trigger-context scanner parity | `loom doctor`'s trigger-parity row, then confirm it by hand: read the trigger's own environment (the `PATH=` line in `crontab -l` on a cron host, or `systemctl --user show -p Environment loom-process.service` on a timer host) and check that value resolves the scanner — `PATH=<that value> command -v gitleaks` | the row is `✓`, and the trigger's PATH resolves the **same gitleaks** (8.30.1, pin matched) that an interactive `command -v gitleaks` does. Since D16, with `~/.loom/bin/gitleaks` installed (the default) the row is `✓` by construction and the hand check is `~/.loom/bin/gitleaks version` → `8.30.1`. A `⚠`, or a `command -v` that finds nothing when there is no `~/.loom/bin/gitleaks`, means the unattended pass would pause capture (before D16: scan with the weaker regex engine) |

Checks 1–4 take about five minutes and are also the right thing to run after **any**
`install.sh --update`, independently of the gate. Check 11 is different: it needs days of
elapsed pilot time, so start the clock early and read it near the gate. Check 12 takes
seconds and belongs with 1–4.

**Why check 12 exists.** The macOS rehearsal (finding 1 below) found the failure this catches:
a scheduler's own minimal `PATH` could not see a brew-installed `gitleaks`, so
`scanner.engine=auto` silently downgraded the **triggered** scans to regex while an
interactive `loom doctor` happily reported `✓ gitleaks 8.30.1 (pin matched)`. Nothing in
either view was wrong; they were just answering about different environments. P2.F5 fixes it
by making `install.sh` carry a `PATH` into the trigger (a `PATH=` line in the crontab
block, `Environment=PATH=` in the systemd unit) — since 2026-10-19 a minimal one built from
the directories the needed tools resolve from, because macOS cron drops an over-long
`PATH=` line (§2) — and the property is asserted end to
end in `client/tests/e2e/test_phase3_e2e.py` — the command line the installer wrote, executed
under a minimal gitleaks-free PATH, must still quarantine a planted high-entropy secret with a
**gitleaks** rule id in the quarantine record's findings. This row is the same property
checked on the real host, because a test can only prove the installer's logic, never that this
machine's scheduler kept the environment it was handed. Doctor's row is a configuration check
(see the box in §6): the `command -v` confirmation is what makes it evidence. D16 removed the
failure mode at its root — gitleaks now lives in `~/.loom/bin` and is run by absolute path,
and a pass with no gitleaks pauses instead of downgrading — so the same e2e test now proves
the control leg PAUSES (nothing published, the session left for the next healthy pass).

**Why check 11 exists (phase-3 wave-d review, deferred to phase 4).** Adapter-class failures
are bounded — three attempts, then the session is parked in `~/.loom/failed/` and skipped by
the sweep until a human clears it. That bound currently treats **`OSError` the same as a
corrupt transcript**. Three unlucky transient errors on the same session therefore park a
perfectly healthy transcript permanently — silent capture loss with a diagnostic that names
an I/O error rather than corruption.

*How likely is that here?* **Unverified, and worth measuring rather than assuming.** On the
default `sweep.transcripts_root` (`~/.claude/projects`) under WSL2 the transcripts live on the
VM's own ext4, where transient `EIO`/`ESTALE` are unlikely. The exposure appears only if
`transcripts_root` is pointed at a Windows drive (`/mnt/c/…`, 9p) or any network mount, where
transient I/O errors are realistic. Check which applies on this host before weighting the
result. This was left unfixed on purpose:
the frozen `p3-adapter-failure-bounding` seam prescribes exactly this behaviour and a green
goal assertion pins it, so the repair is a **phase-4** seam amendment (split the handling by
errno class — `EIO`/`ESTALE` → keep the marker *without* counting an attempt, as network
errors already do; `ENOENT`/`EACCES` → stay bounded). What the pilot must supply is the
evidence: if parked sessions show transient I/O errors, that amendment is worth doing; if
`failed/` stays empty over days of 9p use, it is theoretical. To clear a false park, delete
`~/.loom/failed/<session-id>.json` — the next sweep re-enqueues the session.

### Deployment rehearsal findings

The cron branch was rehearsed on a real macOS build host against the real host boundary
(real user crontab, real cron daemon, real gitleaks 8.30.1, real uvicorn server, real
`install.sh`) with HOME/`LOOM_HOME`/data sandboxed, and re-rehearsed after phase 3; the
dated records are kept in the pilot's private operations notes. Two findings from the
first rehearsal, recorded as follow-ups:

1. **Cron's minimal `PATH` silently downgrades the scanner on darwin/homebrew.** Under
   `PATH=/usr/bin:/bin` (cron's default), `scanner.engine=auto` cannot find
   `/opt/homebrew/bin/gitleaks` and falls back to regex — while an interactive `loom doctor`
   reports `✓ gitleaks 8.30.1 (pin matched)`. Timer-fired scans then run with the weaker
   engine and nothing surfaces it. On the WSL2 pilot this is unlikely to bite (apt installs
   to `/usr/bin`, present in systemd's user-manager PATH), but on any cron host check where
   gitleaks lives, and either add a `PATH=` assignment to the crontab block by hand or set
   `scanner.engine` to `gitleaks` explicitly (fail-closed: a missing binary then quarantines
   instead of downgrading). *Superseded by D16 (2026-10-15):* install.sh installs gitleaks
   into `~/.loom/bin`, `auto` means `gitleaks`, and a missing binary pauses capture.
2. **`--uninstall-trigger` leaves an installed-but-empty crontab** when our block was the
   only content (original state was "no crontab at all"). Harmless — zero jobs — but not a
   byte-exact restore; `crontab -r` afterwards removes the residue if you care.

> Both findings above were fixed in phase 3 (P2.F5 trigger parity, P2.F6 clean uninstall)
> and **re-verified against the real host in the phase-3 re-rehearsal**: foreign crontab
> entries (a foreign `PATH=` line included) survive install and uninstall byte-exact, a
> real cron fire quarantines a planted secret with a **gitleaks** rule id (checklist item
> 12 on a real scheduler), and a loom-only crontab is removed entirely.

Findings from the re-rehearsal (operational, no product change required):

3. **A bare re-run of `install.sh --yes` over an existing install fails at step 2**: current
   `uv venv` refuses to overwrite an existing venv (`error: Failed to create virtual
   environment … Use the --clear flag`), exit 2 before the trigger step. The documented
   re-run path — `install.sh --yes --update` — handles it explicitly ("keeping the existing
   venv") and is unaffected. The E7 goal could not see this: tests run under
   `LOOM_SKIP_VENV=1`. Fix candidate for phase 4 (pass `--clear`/`UV_VENV_CLEAR=1`, or
   detect-and-skip); until then: **re-runs go through `--update`**.
4. **cron does not fire while a laptop sleeps.** The rehearsal's first `*/15` windows fell
   in a lid-closed gap and simply did not run; on wake, vixie-cron ran the pending job once,
   minutes after wake. No data is lost — the next pass
   sweeps everything — but on laptop hosts "passes every 15 min" means *while awake*. The
   WSL2 pilot host is a VM and does not have this failure mode in normal operation.
5. **`crontab(1)` writes on macOS can be intermittently very slow** (30 s to several
   minutes per invocation, apparently syspolicy assessment; they also hang under a seatbelt
   sandbox). `install.sh` is correct but can look stuck at step 5 or during
   `--uninstall-trigger` — give it minutes before assuming failure.

**WSL2 host — systemd branch: verify on the real host.** The systemd leg has only been verified
through PATH-shimmed recorders and unit structural checks. Before the gate, run on the real
host: install gitleaks 8.30.1 → `git pull` + `install.sh --yes --update` → the §2 manual
block (AC-2.2c): timer `enabled`/`active` on the real (degraded) user manager, one full
15-min cycle observed via `journalctl --user -u loom-process`, doctor fully green with the
real `claude` summarizer. That run is checklist item 2 above and is the one piece no test
or rehearsal on macOS can substitute.

---

## 8. Uninstalling

**Stopping one repository, not the client:** `loom disable` inside the repo (D9). From
then on this machine skips that project's sessions with `dropped_not_enabled`; nothing
already uploaded is touched (entries are append-only). `loom enable` reverses it.

Client: remove the periodic trigger **first**, while the checkout is still around — it
handles both branches (systemd units and/or the crontab block), backs the crontab up before
touching it, and shares the installer's filter so removal cannot drift from installation:

```bash
cd <repo> && bash client/install.sh --uninstall-trigger
crontab -l    # -> "no crontab for <user>" if loom's block was all there was
```

What it does with the crontab, in the three cases that actually occur:

| Crontab before | After `--uninstall-trigger` |
|---|---|
| only the loom block | the crontab is **removed** (`crontab -r`), not rewritten empty |
| loom block + your entries | rewritten with your entries **byte-exact**, in order; never removed |
| no crontab at all | clean no-op, exit 0 — nothing written, nothing backed up |

The first row is the point: installing the filtered (empty) content back would leave an
empty-but-registered crontab that `crontab -l` still reports and that some cron
implementations start mailing about — the residue the macOS rehearsal found (§7, finding 2).
A backup is taken **before** the removal too: `crontab -r` has no undo, so
`~/.loom/backup/crontab.<epoch>` is the only copy of what was there. Copy it out before
`rm -rf ~/.loom`.

Then follow `client/UNINSTALL.md` (restore `~/.claude/settings.json` from the
`settings.json.bak-<ts>` backup or delete just the loom entry with `jq`, then `rm -rf ~/.loom`
— note that this also deletes `~/.loom/backup/`, so copy out any crontab backup you still
want). By hand, if the checkout is already gone:

```bash
systemctl --user disable --now loom-process.timer
rm -f ~/.config/systemd/user/loom-process.{timer,service} && systemctl --user daemon-reload
# cron hosts (see the warning in §2 about the `grep -v -A1` idiom — it does nothing).
# The PATH pattern is anchored on OUR venv path on purpose: a bare `-e 'PATH='` would also
# delete an assignment of your own and change what every entry below it can run.
crontab -l > ~/crontab.bak && \
  grep -v -F -e '# loom-process' -e 'LOOM_HOME=' -e "PATH=$HOME/.loom/venv/bin" \
             -e '/venv/bin/loom process' ~/crontab.bak | crontab -
# If that leaves nothing but blank lines, remove the crontab instead of installing it empty:
crontab -r
```

Server:

```bash
systemctl --user disable --now loom-server
rm ~/.config/systemd/user/loom-server.service && systemctl --user daemon-reload
# same fix here: `grep -v -A1 'loom-server-backup'` re-adds what it removes (§2).
crontab -l > ~/crontab.bak && \
  grep -v -F -e '# loom-server-backup' -e 'server/backup.sh' ~/crontab.bak | crontab -
# The DB and the backups stay in ~/.loom-server/ (delete them by hand if appropriate).
```

---

## 9. TODO — phases 2 and 3 (out of the pilot's scope)

**Phase 2 (robustness) — delivered, validated per §6:**
- ~~Orphan sweep (sessions not closed cleanly: crash/kill/WSL shutdown)~~ — **done**: the
  sweep runs inside `loom process`, under the lock the processor already holds, and enqueues
  a synthetic marker (`origin: sweep`) for any settled transcript with unconsumed content.
  Bounded to 10 new markers per pass so the first pass after an upgrade cannot turn into an
  hours-long backfill.
- ~~Full secret scanning (integrate gitleaks; today it is a homegrown set of regexes)~~ —
  **done**: `scanner.engine` (`auto`/`gitleaks`/`regex`), pinned to gitleaks 8.30.1 for the
  pilot, fail-closed (a scanner that cannot produce a verdict quarantines), with the same
  engine selection on `push-quarantine`'s rescan. `loom doctor` reports the engine in force.
  **Tightened by D16 (2026-10-15):** gitleaks is mandatory — install.sh installs a
  checksum-verified 8.30.1 into `~/.loom/bin`, `auto` is a legacy alias of `gitleaks`,
  `regex` is refused, capture pauses without gitleaks, and the extract is redacted before
  the summarizer reads it (§6).
- ~~Packaging the installer and a systemd timer/cron for the processor~~ — **done**:
  `install.sh` installs `loom-process.timer` (or the cron fallback) and gained `--update`;
  see §2. The kick is no longer the only path to a processed marker.

**Known phase-2 limitations, accepted at the phase-2 gate — both closed in phase 3**
(kept here because they are what the phase-2 record said, and what changed is the point):

- ~~**Torn transcripts never drain.**~~ — **closed (P2.F1 + P2.F2).** The delta reader now
  classifies a torn line instead of failing on it: interior tears are skipped and counted,
  a trailing tear holds the pointer below EOF until the session resumes and finishes the
  line. Schema-invalid lines still raise, and are now *bounded* (three attempts, then
  parked in `~/.loom/failed/`) rather than retried forever. The phase-2 record's caveat —
  "crashed-session recovery is proven for cleanly truncated transcripts, not for ones torn
  mid-line" — is **retired**: `client/tests/e2e/test_phase3_e2e.py` drives a real repo
  session torn mid-line, and one torn inside a multi-byte character, through the sweep, the
  real summarizer seam and the real API, and asserts that both content halves land as
  chained entries after the resume. Operator handling in §6 ("Stuck marker") now covers
  only the schema-invalid case.

- ~~**The steady-state sweep re-reads every settled transcript.**~~ — **closed for the
  captured set (P2.F4), and re-measured post-fix on 2026-08-10.** Each `state.json` entry
  records the transcript's `(dev, ino, mtime_ns, size)` as of the read its pointer came
  from, and the sweep resolves an unchanged transcript with a single `stat`.

  **Method.** Same shape as the §6 baseline (301 transcripts / 226 MiB), but deliberately
  realistic in the way the wave-e review demanded — **one registered project (60
  transcripts, 45 MiB) and 29 unregistered ones (241 transcripts, 181 MiB)**. Bytes were
  counted by wrapping the file reads for the measurement, over five consecutive `loom
  process` passes; the figure was identical on every pass.

  | Tree | Measured cost of one pass |
  |---|---|
  | before P2.F4 (the §6 baseline) | 301 transcripts opened, **226 MiB** read → ~21 GiB/day |
  | captured set only (every transcript delivered, tree settled) | **zero transcript files opened, 0 bytes read** |
  | the realistic tree above (mostly unregistered) | 10 transcripts, 20 opens, **7.5 MiB** read → ~0.7 GiB/day |

  So the honest claim is: **bounded cost for the captured set; residual 7.5 MiB/pass for
  non-deliverable transcripts (phase 4)** — *not* "zero total". The residual exists because
  the stat pair is written only by the delivery pointer, so a transcript that never delivers
  (no git remote, project not registered, transcript gone) never acquires one. It is
  **bounded but never draining**: `MAX_ENQUEUE_PER_PASS = 10` stops the scan after ten
  synthetic markers, so the same ten newest non-delivering transcripts are re-read every
  pass (twice each — the line count, then the `cwd` probe) while the remaining 231 are never
  reached at all, and those ten permanently consume every enqueue slot. On a machine with
  larger transcripts the same ten reads cost proportionally more, which is why this is a
  *per-transcript*, not a per-tree, residual.

  Closing it needs a sweep-owned read-coverage cache, keyed by transcript path and
  invalidated by the stat tuple, carrying no delivery semantics — registered as the phase-4
  item **`p4-sweep-read-coverage-cache`**, which closes both halves (the residual reads and
  the enqueue-slot starvation) at once. Until then, read §6 "After the stat-skip" for how to
  tell on a given host how much of a pass is skip and how much is still read
  (`stat_skips=N` in the run log versus `scanned` in `sweep_last_run.json`).

**Phase 3 (full team):**
- **TLS / exposing the server — decided, done (issue #2).** Get the
  server off `127.0.0.1`. The two options this entry used to name — an internal VM with
  Caddy behind an internal certificate, or `tailscale serve` inside the team's VPN — are
  **superseded**: both assumed infrastructure the pilot does not have. The
  decision is **Fly.io**, one Machine in `cdg` with platform TLS on `*.fly.dev` and
  SQLite on a persistent volume (`DESIGN.md` §6.2). The packaging has landed and is
  proven locally (§2 → "Running the server as a container"). The deployment is **live and
  authoritative since the cutover (#17)**: every pilot client reaches it over
  HTTPS from any machine, and `127.0.0.1` is no longer served.
- **Minimal web UI:** the read-only part (login, projects, browsable logbook, item state,
  curation signals) shipped with D14 (2026-09-29, §6 "Reading the logbook in a browser").
  Still to do: registration/merge and token management from the UI.
- **Path-based fallback** (`project_paths`) for repos with no git remote + merging paths into a
  logical project from the UI (today `loom register` without a remote exits with instructions and exit 1).
- **OKF export:** a `GET /api/projects/{id}/okf` endpoint that emits the project's bundle
  (entries have been self-contained OKF concepts since 2026-07-28, D7/§5.1
  of DESIGN.md) + Google's static HTML viewer as a read-only frontend
  with no backend. Our own UI covers what OKF does not: login, registration, merge, tokens.
- Onboard the team; periodic curation digest using the OKF model: mark
  `status: superseded` and link each decision to the one that supersedes it.
