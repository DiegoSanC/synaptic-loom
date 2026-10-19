# Synaptic Loom — standing up a server

For whoever deploys a **new** instance: a second team, a fresh pilot, or whoever inherits
this project. Operating the instance that already exists is `docs/RUNBOOK.md`; installing
a client is `docs/USER-GUIDE.md`; local development quickstart is `server/README.md`.

This guide is not the RUNBOOK. The RUNBOOK is a record of one deployment, with its app name
and region interleaved with the steps that produced them. Nothing here is an id from that
deployment.

## Verification status

Every command below was **executed on 2026-09-29** against a throwaway Fly app
(`loom-scratch-2609`) created for this purpose and destroyed afterwards, and against a
locally built image. The one section that was not executed is marked in place and listed
again at the end. Where a step produced a surprise, the surprise is written down rather
than smoothed over — that is most of this document's value.

Part 2 (Docker Compose + Caddy) was **executed on 2026-10-16** by goal wave `r`, against the
real `deploy/compose` files on a local Docker host, with Caddy's internal CA standing in
for the public one; the public-DNS, public-CA path on a real VPS was not (listed at the end).
Its backups and restore (Part 2 §8) were **executed on 2026-10-17** by goal wave `s`, against
the same files; the SSH leg of the off-host pull was not.

---

## What you are building

Two supported topologies (`DESIGN.md` §6.2, D17): **Part 1, Fly.io**, below, and
**Part 2, one host with Docker Compose + Caddy**. Part 1:

One Fly app · one Machine · one volume · one region. SQLite on the volume, no ORM, no
second process. The single-Machine shape is not a simplification for the guide: a Fly
volume attaches to one Machine at a time, and `fly.toml`'s `[mounts]` block suppresses
Fly's default two-Machine redundancy for exactly that reason. `DESIGN.md §6.2` is the
normative contract.

## Before you start (Part 1)

| | |
|---|---|
| `flyctl` | `fly auth whoami` must print your email. |
| A Fly organization | This guide uses `personal`. All apps in one org share one bill. |
| `git` | You deploy from a clone. |
| `docker` | Only for the development section (Part 3), and only if you want it. |

No secrets are needed. **Verified:** `fly secrets list` against the existing production app
returns an empty set — `LOOM_DB` travels in `[env]` inside `fly.toml`, so there is no
secret-setting step in this guide and you should be suspicious of any version that adds one.

---

# Part 1 — Hosted on Fly.io

## 1. Clone, and work from the clone

```bash
git clone <repo-url> loom-deploy && cd loom-deploy
```

## 2. Change the app name BEFORE running any `fly` command

This is the single most dangerous moment in the whole procedure.

```toml
# fly.toml, line 9
app = "your-loom-app"
```

**Every `fly` command run from a repo root without `-a` targets whatever that line says.**
A forgotten flag on `fly deploy` deploys to that app; on `fly secrets set` it overwrites
its secrets. Edit the line first, so the default at the root is already yours:

```bash
sed -i 's/^app = .*/app = "<your-app-name>"/' fly.toml
grep -n '^app = ' fly.toml          # confirm before going further
```

Pass `-a <your-app-name>` on every command anyway. The two protections are cheap and
independent; rely on either alone and one slip reaches someone else's data.

> **Fly rejects some names.** `loom-verify-2609` was refused with
> `Name blocked by abuse filter. verify is a common phishing target`. If `fly apps create`
> fails on the name, that is what happened; pick another word.

Optionally also rename the volume in `[mounts]`. It is namespaced to the app, so it does
not have to change — this guide's verification run changed it purely to make the test
resources obvious at a glance.

## 3. Create the app

```bash
fly apps create <your-app-name> -o personal
fly apps list | grep <your-app-name>          # STATUS is `pending` — nothing deployed yet
```

## 4. Deploy

```bash
fly deploy --ha=false -a <your-app-name>
```

`--ha=false` is not optional. Without it Fly provisions a second Machine, which cannot
attach to the same volume. For the same reason the `bluegreen` deploy strategy is not
available to this app; use the default `rolling`, or `immediate`.

The deploy creates the volume declared in `[mounts]` (1 GB) and launches one Machine.
**Verified:** a volume created this way does pick up `snapshot_retention = 14` from
`fly.toml` — `fly volumes show <id>` printed `Snapshot retention: 14` and
`Scheduled snapshots: true`. That guarantee applies **only** to volumes `fly deploy`
creates; one made by hand or by `fly machine clone` needs
`fly volumes update <id> --snapshot-retention 14`.

## 5. Allocate IP addresses — the step the deploy does not do for you

**Read this even if the deploy said it succeeded.**

On the verification run `fly deploy` exited `0`, created the Machine and the volume, printed
`Visit your newly deployed app at https://<app>.fly.dev/` — and the app had **no IP
addresses at all and was unreachable**. Buried in its output:

```
Failed to provision IP addresses. Use `fly ips` commands to remediate it.
ERROR: error allocating ipv6 after detecting first deploy and presence of services:
       failed to add ip to app: org_slug is only supported with private_v6 type
```

Fix it explicitly:

```bash
fly ips allocate-v4 --shared -a <your-app-name>     # shared v4 is free; a dedicated one bills
fly ips allocate-v6 -a <your-app-name>
fly ips list -a <your-app-name>                     # expect one v6 and one v4 row
```

`--shared` matters for the bill. A shared IPv4 routes by SNI and costs nothing; a dedicated
IPv4 is a monthly line item you did not ask for.

## 6. Prove it is reachable — and do not trust the first failure

```bash
curl -sS https://<your-app-name>.fly.dev/api/health
# -> {"status":"ok","version":"0.1.0"}
```

If that fails, **the deployment is probably fine and DNS is not ready yet**. On the
verification run the name took several minutes to appear, and the failure was actively
misleading: the resolver returned `127.0.0.1` for
`<app>.fly.dev.<isp-search-domain>` — a wildcard the ISP serves for names that do not
exist — so the symptom was `Couldn't connect to server`, which reads like a dead app
rather than an absent DNS record.

Separate the two questions before concluding anything:

```bash
# Is the SERVER alive? Ask it by IP, letting SNI route the shared v4:
curl -sS --resolve <your-app-name>.fly.dev:443:<your-shared-v4> \
     https://<your-app-name>.fly.dev/api/health

# Is the MACHINE running?
fly status -a <your-app-name>          # STATE should be `started`
```

On the verification run the `--resolve` form returned `200` in 20 ms while the plain
hostname was still refusing connections. Both worked a few minutes later with no further
action.

## 7. Create the first user and the first enrolment code

This is the point where the deployment becomes usable.

`server/admin.py` must run **inside the Machine**, against the volume. Running it locally
against a copy creates a second, divergent set of `token_hash` values with nothing to
surface the divergence.

```bash
fly machine start <machine-id> -a <your-app-name>   # only if stopped; ssh will NOT wake it
fly ssh console -a <your-app-name> -u app -C "python -m server.admin listusers"
# -> (no users)

fly ssh console -a <your-app-name> -u app \
    -C "python -m server.admin invite --email dev@example.com --name dev"
```

`-u app` matters: `fly ssh console` is root by default, and a root-created WAL sidecar in
`/data` is a file the server process then cannot write.

`invite` prints a single-use **enrolment code**, valid 24 h by default (`--ttl 30m|24h|3d`).
The code is the only thing that travels; send it over the team's channel. The engineer
redeems it with `loom enroll` and **their** machine mints its own 90-day device token,
which never leaves that machine and renews itself.

`adduser` also exists and prints a permanent token. It is the break-glass path only — see
`server/README.md`.

## 8. Prove the round trip

Do this from a scratch `LOOM_HOME` so you do not repoint your real client at the new
server:

```bash
export LOOM_HOME=/tmp/loom-check && mkdir -p "$LOOM_HOME"
~/.loom/venv/bin/loom enroll --server-url=https://<your-app-name>.fly.dev \
                             --device-name=deploy-check
# paste the code at the hidden prompt
# -> Server reachable ✓ / Enrolled ✓ / Config written to $LOOM_HOME/config.json (600)

mkdir -p /tmp/loom-check-repo && cd /tmp/loom-check-repo && git init -q
git remote add origin git@github.com:example/check.git
~/.loom/venv/bin/loom register
# -> Registered ✓  github.com/example/check (id 1)

~/.loom/venv/bin/loom log        # reads it back through the client
```

**Verified end to end**: enrol → register → `POST /api/entries` returning `201 {"id":1}` →
the entry read back by `loom log` with its "UNVERIFIED DATA" framing line intact.

When you are done, `rm -rf "$LOOM_HOME"` and unset it. Your real client lives in `~/.loom`
and this exercise must never touch it.

## 9. What it costs, plainly

All apps in one organization share one bill. A deployment is one 1 GB volume plus a
`shared-cpu-1x`/256 MB Machine that stops when idle (`auto_stop_machines = "stop"`,
`min_machines_running = 0`).

**A volume bills continuously even while the Machine is stopped.** Stopping the Machine
does not stop the volume. If you create an instance to try something, destroying the app
is not enough on its own — check the volume is gone too (§10).

`fly.toml` records the pilot's own cost constraint: a small fixed budget with **no spend
cap and no billing alerts**. Fly will not stop you; nothing will warn you.

## 10. Tearing down an instance you no longer want

```bash
fly apps destroy <your-app-name> --yes
fly apps list    | grep <your-app-name>    # expect no row
fly volumes list -a <your-app-name>        # expect the app not to exist
```

Verify both. The app disappearing is not proof the volume did.

## 11. Day two

Everything after "it is running and people can enrol" is `docs/RUNBOOK.md`: deploys and
what to watch afterwards, backups and the restore drill (§3a), rollback (§3), adding and
removing machines (§4), curation (§6). Read §0 first — it tells you which topology is
authoritative and which sections are rollback history.

---

# Part 2 — One host with Docker Compose + Caddy

For a team that runs its own Linux host (a VPS) instead of Fly. Supported and tested
(`DESIGN.md` D17): goal wave `r` brings up exactly the files below with
`docker compose up --build` and drives them from client containers; goal wave `s` does the
same for the backups and the restore (D18, §8).

```
internet ──80/443──▶ caddy ──backend (internal network)──▶ loom:8080 ──▶ volume /data
                                          backup (no network) ──reads /data──▶ volume /backups
another machine ──SSH, pulls──▶ the backups (§8)
```

Caddy is the **only** published service. It terminates TLS for your domain with automatic
HTTPS and forwards to the server, which has no host port and sits on a network with no
route to the outside — so the server is reachable through Caddy alone. Same single-process
shape as on Fly: one Uvicorn worker, SQLite on one volume.

## 1. Before you start

| | |
|---|---|
| A Linux host | Public IPv4, inbound **80 and 443** open — Caddy needs both to obtain and renew the certificate. IPv4 only: see §2. |
| Docker Engine 25+ with the Compose v2 plugin | `docker compose version`. The server's healthcheck uses `start_interval`, which older engines ignore. The database volume's name is built from `COMPOSE_PROJECT_NAME`, which Compose must set for interpolation (verified with Compose v5.5.1); a Compose that does not stops with an error naming it rather than start on an empty volume. |
| A DNS name | `loom.example.com` in this guide. |
| `git` | You deploy from a clone. |
| The subnet `10.217.17.0/24` free on the host | It is the server's private network (see §6). If it collides, change the subnet, its `ip_range`, Caddy's `ipv4_address` and `FORWARDED_ALLOW_IPS` together in `deploy/compose/docker-compose.yml`. |

## 2. Point the DNS name at the host

Create an `A` record for `loom.example.com` pointing at the host's public IPv4 address, and
check it resolves **before** the first start:

```bash
dig +short loom.example.com      # -> the host's public address
dig +short AAAA loom.example.com # -> nothing
```

**Do not publish an `AAAA` record.** The Compose networks are IPv4-only, so an IPv6
connection to a published port is relayed by Docker's userland proxy, and Caddy sees the
network's gateway instead of the client: every IPv6 client would share one rate-limit
bucket and one audit address. Supporting IPv6 means enabling it on the `edge` network,
which this topology does not do and wave `r` does not test.

Caddy retries a failed issuance, but the public CA rate-limits failures; starting before
DNS is right only buys a wait.

## 3. Clone and configure

```bash
git clone <repo-url> loom && cd loom/deploy/compose
cp .env.example .env
$EDITOR .env                     # LOOM_DOMAIN=loom.example.com -> your name
```

`LOOM_DOMAIN` is the only setting. `.env` is gitignored; without it, `docker compose`
refuses to start and names the missing variable.

## 4. Start it

```bash
docker compose up -d --build --wait
docker compose ps                # loom (healthy), backup and caddy, nothing else
curl -sS https://loom.example.com/api/health
# -> {"status":"ok","version":"0.1.0"}
```

`--wait` returns once the server's own healthcheck passes; Caddy starts after it. The
first certificate is usually issued within seconds — `docker compose logs caddy` shows the
ACME exchange if the `curl` fails with a TLS error.

## 5. Create the first user and the first enrolment code

Exactly Part 1 §7, with `docker compose exec` in place of `fly ssh console`, from
`deploy/compose/`:

```bash
docker compose exec -u app loom python -m server.admin listusers
# -> (no users)
docker compose exec -u app loom python -m server.admin invite --email dev@example.com --name dev
```

`-u app` for the same reason as on Fly: `exec` is root by default, and a root-created WAL
sidecar in `/data` is a file the server process then cannot write. Then prove the round
trip as in Part 1 §8, with `--server-url=https://loom.example.com`.

## 6. Check that the server sees each client's own address

The client address keys the failed-authentication limit (10 per minute), the health limit
and the security-audit line (`DESIGN.md §6.2`). Getting it wrong either puts every client
in one bucket — one caller's typos lock everybody out — or lets a client name its own
address. Check it once after the first start:

```bash
docker compose logs loom | grep client_ip_source
# -> ... client_ip_source mode=peer        (and no WARNING line next to it)
```

Then, **from another machine** (not the host itself), send one disposable bad token with a
forged header, and read the audit line:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -H 'Authorization: Bearer not-a-real-token' \
     -H 'X-Forwarded-For: 192.0.2.1' https://loom.example.com/api/projects
# -> 401
docker compose logs loom | grep 'outcome=invalid_token' | tail -1
# -> ... client_ip=<the address that machine connects from>
```

| `client_ip=` shows | Means |
|---|---|
| the other machine's address | Correct. |
| `10.217.17.2` | The server does not trust Caddy: `FORWARDED_ALLOW_IPS` is not Caddy's address. Every client shares one bucket. |
| `192.0.2.1` | Both layers believe the client's own header: `trusted_proxies` in the Caddyfile covers the client, **and** `FORWARDED_ALLOW_IPS` is wider than Caddy's address (e.g. `*`). Fix before going further. |
| a Docker gateway address (`172.x.x.1`) | The request reached Caddy through Docker's userland proxy: a test from the host itself, or over IPv6 (see §2). Retry from another machine, over IPv4. |

How it works, so the table above can be reasoned about rather than memorised:

- The server runs with `LOOM_CLIENT_IP_SOURCE=peer`: the client address is the TCP peer
  as uvicorn leaves it. uvicorn **0.52.4** (pinned in `uv.lock`) has proxy-header
  handling **on by default** and the Dockerfile `CMD` does not change it
  (no `--proxy-headers`/`--forwarded-allow-ips` flags); it reads `FORWARDED_ALLOW_IPS`
  from the environment (default `127.0.0.1,::1`) and rewrites the peer from
  `X-Forwarded-For` **only** when the connection comes from an address in that list.
  The Compose file sets it to Caddy's fixed address, `10.217.17.2`, and nothing else.
- Caddy (2.11.7, pinned by digest) has no `trusted_proxies` configured, so it treats
  every client as untrusted: it **replaces** whatever `X-Forwarded-For` the client sent
  with the address the connection came from. Other headers pass through untouched —
  `Fly-Client-IP` included, which is why `fly-client-ip` is the wrong mode here (the
  server warns at startup if it is set without `FLY_APP_NAME`). Both behaviours were
  observed on 2026-10-16 against a header-echoing upstream; wave `r` asserts their
  consequence — a client-sent `X-Forwarded-For` or `Fly-Client-IP` neither resets a
  failure window nor reaches the audit line — and, separately, that a peer on the
  server's network which is not Caddy cannot name its own address with
  `X-Forwarded-For`.

> ### Never set `FORWARDED_ALLOW_IPS=*`
>
> It makes uvicorn believe `X-Forwarded-For` from **any** peer. Anyone who can reach the
> server without going through Caddy — another container on the network, a port someone
> later publishes for debugging — can then name any address they like: a fresh rate-limit
> bucket per request, and whatever they want written into the audit log. Name Caddy's
> address, and only Caddy's.

Another proxy in front of Caddy (a CDN, a load balancer) changes the picture: the TCP peer
Caddy sees is then that proxy. `LOOM_CLIENT_IP_SOURCE=header:<name>` reads one
single-valued header such a proxy sets (`header:cf-connecting-ip`), and is only sound if
the origin accepts connections from that proxy alone. This guide does not cover it, and
wave `r` does not test it.

## 7. Day two

```bash
git pull && docker compose up -d --build --wait     # deploy a new version
docker compose stop                                 # drains up to 25 s; SIGKILL at 30 s
docker compose down                                 # removes containers, KEEPS the volumes
```

The database is the named volume `loom_loom_data` (project `loom`, volume `loom_data`; after
a restore, the volume `LOOM_DATA_VOLUME` names in `.env`, §8b), and the backups are
`loom_loom_backups`. `docker compose down -v` **deletes both**, together with Caddy's
certificates — never run it on a server whose logbook you want.

Take a backup before deploying a new version: the server migrates its schema when it
starts, and a rollback needs a copy from just before that.

```bash
docker compose exec backup /app/server/backup.sh   # -> backup.sh: wrote /backups/loom-….db (…)
```

## 8. Backups and restore (`DESIGN.md` D8, D18)

The `backup` service copies the database into a volume of its own, and **another machine
pulls those copies off the host**. Nothing on the server holds a credential for anywhere
else.

| | |
|---|---|
| What runs | The `backup` service: the server's image, user `app`, no network, no port. It runs `server/backup.sh` once when it starts and then every `LOOM_BACKUP_INTERVAL`. |
| A backup | SQLite's online backup API (consistent while the server writes), checked with `PRAGMA integrity_check` and renamed into place only when `ok`: `loom-<UTC time>.db`, mode 0600, in the volume `loom_loom_backups`. |
| Interval | `LOOM_BACKUP_INTERVAL` in `.env`, default `6h`. |
| Kept on the host | `LOOM_BACKUP_KEEP`, default 56: 14 days at 6 h (D8). Longer history is the off-host copy's. |
| RPO | Database volume lost or damaged: **≤ the interval** (6 h). Whole host lost: **≤ the interval + the pull schedule** (≤ 12 h with the 6-hourly pull below). D8 asks for ≤ 24 h. |
| A failed backup | The copy is deleted, no older backup is purged, and the service exits non-zero: Docker restarts it, which retries the backup, and `docker compose ps` shows `backup` restarting. Nothing alerts you — the check below is the only oracle. |

**Check it**, from `deploy/compose/`, weekly and after every deploy:

```bash
docker compose ps backup             # running — "Restarting" means backups are failing
docker compose logs --tail 4 backup
# -> backup.sh: wrote /backups/loom-20261017T120000.123456Z.db (… bytes, integrity_check ok)
```

### 8a. The off-host copy

Run this **on another machine** — one that shares neither the server's disk nor its
provider account — with SSH access to the server as a user allowed to run `docker`. Save
it as `~/bin/loom-pull`:

```bash
#!/usr/bin/env bash
# Copy the server's new backups into ~/loom-offsite. A file already here is never replaced.
set -euo pipefail
dest=~/loom-offsite
mkdir -p "$dest" && chmod 700 "$dest"
staging="$(mktemp -d)"; trap 'rm -rf "$staging"' EXIT
ssh loom-host 'cd ~/loom/deploy/compose && docker compose exec -T backup /app/server/backup.sh --export' \
  | tar -C "$staging" -xmf -
for f in "$staging"/loom-*.db; do
  [ -e "$f" ] || continue
  [ -e "$dest/${f##*/}" ] || mv "$f" "$dest/"
done
find "$dest" -name 'loom-*.db' -mtime +30 -delete    # the puller's own retention: 30 days
```

and schedule it every 6 h, away from the backup's own moment: `15 */6 * * * $HOME/bin/loom-pull`.

`--export` writes the server's current backups to stdout as one tar stream. It opens them
under the lock every backup takes, so a pull never catches a half-written file or one that
retention is deleting, and it lets go before streaming, so a slow or stalled pull never
holds up a backup. The script is written so that **the server cannot damage what the
puller already has**: an interrupted pull moves nothing (`pipefail` stops the script before
the loop), a name already present is never overwritten, and `-m` stamps each file with its
arrival time, so the 30-day pruning cannot be steered by a file time the server sends. It
uses `exec`, not `run`: a one-off container's output is also written to its Docker log on
the server's disk, every byte of the logbook included. `exec` needs the `backup` service
running; when it is not (§8's check), the pull fails too, which cron reports.

- **Restrict the key.** Being allowed to run `docker` is being root on that host. Give the
  puller a key of its own and pin it to this one command in the server's
  `~/.ssh/authorized_keys`: `command="cd ~/loom/deploy/compose && docker compose exec -T
  backup /app/server/backup.sh --export",restrict ssh-ed25519 AAAA… loom-pull`.
- **The copies are the whole logbook.** Keep the puller's directory as you keep the server:
  encrypted disk, mode 700, nobody else's account.
- **Provider disk snapshots** of the server are a good complement (they also cover the
  host's configuration), not a substitute: they are crash-consistent images nobody
  integrity-checks, kept in the same provider account as the server.

### 8b. Restore — always into a new volume

```bash
cd ~/loom/deploy/compose
# 1. Pick a backup. From the host's backups volume:
docker compose exec -T backup /app/server/backup.sh --export | tar -tvf -
mkdir -p ~/restore && chmod 700 ~/restore
docker compose exec -T backup /app/server/backup.sh --export \
  | tar -C ~/restore -xf - loom-20261017T120000.123456Z.db
#    …or copy one back from the off-host machine when the host's volume is gone.

# 2. Restore it.
./restore.sh ~/restore/loom-20261017T120000.123456Z.db
# -> restore.sh: restored 3 users, 2 projects, 41 entries
# -> restore.sh: the server runs on loom_loom_data_restored_20261017T130000Z, recorded as LOOM_DATA_VOLUME in .env; health ok
# -> restore.sh: loom_loom_data is untouched; remove it with `docker volume rm loom_loom_data` once the drill passes
```

`restore.sh` streams the file into a **new** volume through a one-off container and checks
it there with `PRAGMA integrity_check`: a bad file is refused while the server keeps
running, and the half-made volume is removed. Only then does it record the new volume as
`LOOM_DATA_VOLUME` in `.env` — so every later `docker compose` command uses it — recreate
`loom` and `backup` on it, and check that the running server's `/data` is that volume. The
volume the server ran on before is never mounted, written or deleted. Entries written after
the backup was taken are not in the restore; they are still on the old volume, which is one
reason it stays until you remove it.

**To go back** to the volume from before the restore: delete the `LOOM_DATA_VOLUME` line
from `.env`, then `docker compose up -d --wait --force-recreate loom backup`.

**On a new host**, when the old one is gone: §3 (clone, `.env`), `docker compose build`,
`./restore.sh` with the newest off-host copy, then `docker compose up -d --wait` for Caddy.

### 8c. The drill

A backup that was never restored is a hope. Right after the restore, and once a month,
prove the restored server answers **with the data**, from a client machine:

```bash
curl -sS https://loom.example.com/api/health          # {"status":"ok",…}
~/.loom/venv/bin/loom log <project>                   # an authenticated read
~/.loom/venv/bin/loom log <project> --grep "<a word you know is in an entry>"   # ?q= search
```

The monthly drill is a restore of the newest **off-host** copy on a second Docker machine,
following "on a new host" above, so the copy that survives a lost host is the one proven.
Without DNS there, make the same three checks from inside the server container:

```bash
# TOKEN: a device token, e.g. `token` in a client's ~/.loom/config.json.
TOKEN='<device token>' docker compose exec -T -u app -e TOKEN loom python -c '
import json, os, urllib.request
def get(path):
    request = urllib.request.Request("http://127.0.0.1:8080" + path,
                                     headers={"Authorization": "Bearer " + os.environ["TOKEN"]})
    return json.load(urllib.request.urlopen(request, timeout=10))
print(get("/api/health"))
projects = get("/api/projects")["projects"]
print([p["name"] for p in projects])
print(len(get("/api/projects/%d/entries?q=WORD" % projects[0]["id"])["entries"]))'
```

## 9. Tearing it down

```bash
docker compose down -v --rmi local     # containers, images, AND every volume: data and backups gone
```

A volume a restore replaced (§8b) is no longer part of the stack, so `down -v` leaves it:
`docker volume ls` and `docker volume rm` it by name.

---

# Part 3 — Running it locally

> ### Development and testing only
>
> **Do not point a working client at a local server.** `docs/RUNBOOK.md §0` records the
> lesson this project already paid for: **two live servers on two independent SQLite files
> split the logbook silently.** Entries go to whichever server the client's `config.json`
> names, with no error and no warning, and the team simply stops seeing each other's work.
>
> A machine running a local server must not also run a client pointed at the hosted one.
> If you need both, use a separate `LOOM_HOME` as in Part 1 §8 and never edit `~/.loom/config.json`.

Two ways, both **verified 2026-09-29**.

**The real image** — what Fly actually runs, so it catches image-level breakage:

```bash
docker build -t loom-local .
mkdir -p ./loom-data
docker run -d --name loom-local -p 8099:8080 \
       -e LOOM_DB=/data/loom.db -v "$PWD/loom-data:/data" loom-local
curl -sS http://127.0.0.1:8099/api/health
# -> {"status":"ok","version":"0.1.0"}

docker exec -u app loom-local python -m server.admin listusers
```

The build produced a 216 MB image; the container created `loom.db` on the mounted host
directory and the admin CLI ran inside it.

**Or straight from the source tree**, when you are changing server code:

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[server,dev]"
export LOOM_DB=./loom.db
.venv/bin/python -m uvicorn server.app:app --port 8765      # or `make serve`
```

`server/README.md` covers this path, the admin CLI and the smoke curls.

---

## What was NOT executed

Stated so nobody mistakes the rest for equally solid:

- **Restoring from a snapshot.** Snapshots were verified to exist and to carry the right
  retention; the restore path (`docs/RUNBOOK.md §3a`) was not exercised here.
- **Whether the Machine actually stops when idle.** `fly.toml` flags this as an open
  question in its own comments — including whether proxy health checks would count as
  traffic and keep it awake. Unchanged by this guide, still unmeasured.
- **Custom domains and certificates.** Everything here uses the `*.fly.dev` hostname and
  Fly's platform TLS.
- **Any region other than `cdg`, and any second Machine.** Out of scope by design.
- **Part 2 on a real VPS with a public DNS name.** Wave `r` runs the real Compose files,
  but with `LOOM_DOMAIN=loom.localhost`, which Caddy serves from its internal CA: ACME
  issuance and renewal against a public CA, and a client address arriving through a real
  host's published ports, were not executed.
- **Part 2's off-host pull over SSH from a real second machine (§8a).** Wave `s` runs the
  `--export` stream the pull carries (through `exec`, as there) and unpacks it, on one Docker
  host; the SSH hop, the restricted key, the `loom-pull` script and its cron line were not
  executed.
- **Part 2's monthly drill on a second machine (§8c).** Wave `s` restores on the same host
  and makes the drill's checks (health, an authenticated read, a `?q=` search) against the
  restored server from inside its container; `loom log` against a restored server was not.
