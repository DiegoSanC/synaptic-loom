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

---

## What you are building

One Fly app · one Machine · one volume · one region. SQLite on the volume, no ORM, no
second process. The single-Machine shape is not a simplification for the guide: a Fly
volume attaches to one Machine at a time, and `fly.toml`'s `[mounts]` block suppresses
Fly's default two-Machine redundancy for exactly that reason. `DESIGN.md §6.2` is the
normative contract.

## Before you start

| | |
|---|---|
| `flyctl` | `fly auth whoami` must print your email. |
| A Fly organization | This guide uses `personal`. All apps in one org share one bill. |
| `git` | You deploy from a clone. |
| `docker` | Only for the development section, and only if you want it. |

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

# Part 2 — Running it locally

> ### Development and testing only
>
> **Do not point a working client at a local server.** `docs/RUNBOOK.md §0` records the
> lesson this project already paid for: **two live servers on two independent SQLite files
> split the logbook silently.** Entries go to whichever server the client's `config.json`
> names, with no error and no warning, and the team simply stops seeing each other's work.
>
> A machine running a local server must not also run a client pointed at the hosted one.
> If you need both, use a separate `LOOM_HOME` as in §8 and never edit `~/.loom/config.json`.

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
