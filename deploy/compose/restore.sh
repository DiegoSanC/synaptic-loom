#!/usr/bin/env bash
# restore.sh — restore a backup into a NEW volume and put the server on it (DESIGN.md D8,
# D18; docs/SERVER-GUIDE.md Part 2 §8).
#
#   deploy/compose/restore.sh <backup-file>
#
# <backup-file> is a loom-<timestamp>.db on this host: pulled back from the off-host copy,
# or exported from the backups volume (the guide shows both).
#
# 1. The file is streamed into a new volume, <project>_loom_data_restored_<UTC time>, by a
#    one-off container of the server image, and must pass PRAGMA integrity_check there.
#    The running server is not touched until it has: a bad file costs no downtime, and the
#    new volume is removed again.
# 2. The new volume's name is recorded as LOOM_DATA_VOLUME in the env file, so every later
#    `docker compose` command keeps using it.
# 3. `loom` and `backup` are recreated on it — `--force-recreate`, so the switch does not
#    depend on whether a given Compose version counts a volume's new name as a change to
#    the service — and the script checks that the running server's /data IS the new volume.
#
# The volume the server ran on before is never mounted, written or removed: it stays as it
# was until you delete it with `docker volume rm`. Do not "clean up" with
# `docker compose down -v`: that deletes the volume the server now runs on, and the backups.
#
# Environment: LOOM_ENV_FILE is the env file read and recorded into (default: .env next to
# this script). COMPOSE_PROJECT_NAME and COMPOSE_FILE work as they do for docker compose.
set -euo pipefail
umask 077

die() {
  echo "restore.sh: $*" >&2
  exit 1
}

[ $# -eq 1 ] || die "usage: restore.sh <backup-file>"
[ -f "$1" ] && [ -r "$1" ] || die "no readable backup file at $1"
SRC="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
cd "$(dirname "${BASH_SOURCE[0]}")"
ENV_FILE="${LOOM_ENV_FILE:-.env}"
[ -f "$ENV_FILE" ] || die "no env file at $ENV_FILE (cp .env.example .env, as for the first start)"
[ -z "${LOOM_DATA_VOLUME:-}" ] || die "unset LOOM_DATA_VOLUME in your shell: restore.sh records it in $ENV_FILE"

compose() { docker compose --env-file "$ENV_FILE" "$@"; }

PROJECT="$(compose config | sed -n '1s/^name: //p')"
[ -n "$PROJECT" ] || die "could not read the Compose project name (is $ENV_FILE complete?)"
OLD="$(compose config | awk '
  /^volumes:/ { v = 1; next }
  v && /^[^ ]/ { exit }
  v && /^  loom_data:/ { f = 1; next }
  f && /^    name:/ { print $2; exit }
  f && /^  [^ ]/ { exit }')"
NEW="${PROJECT}_loom_data_restored_$(date -u +%Y%m%dT%H%M%SZ)"
! docker volume inspect "$NEW" >/dev/null 2>&1 || die "volume $NEW already exists"

# Runs as `app` in the one-off container (the image's entrypoint hands the new volume over
# first). stdin is the backup, so no host path has to be readable from inside it.
RESTORE_PY='
import os, shutil, sqlite3, sys
db = os.environ["LOOM_DB"]
data = os.path.dirname(db)
if os.listdir(data):
    sys.exit(f"restore.sh: {data} is not empty: a restore goes into a NEW volume only")
tmp = os.path.join(data, ".restore.db.tmp")
with open(tmp, "wb") as f:
    shutil.copyfileobj(sys.stdin.buffer, f)
    f.flush()
    os.fsync(f.fileno())
os.chmod(tmp, 0o600)
counts = {}
try:
    conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
    try:
        result = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        if result == ["ok"]:
            for table in ("users", "projects", "entries"):
                counts[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()
except sqlite3.Error as exc:
    result = [f"not a Loom database: {exc}"]
if result != ["ok"]:
    os.remove(tmp)
    sys.exit("restore.sh: the backup does not pass integrity_check (" + "; ".join(result[:5]) + ")")
os.replace(tmp, db)
print("restore.sh: restored " + ", ".join(f"{n} {t}" for t, n in counts.items()))
'

echo "restore.sh: restoring $SRC into the new volume $NEW (the server keeps running on ${OLD:-?})"
if ! LOOM_DATA_VOLUME="$NEW" compose run --rm --no-deps -T loom python -c "$RESTORE_PY" < "$SRC"; then
  docker volume rm "$NEW" >/dev/null 2>&1 || true
  die "nothing was changed: the server still runs on ${OLD:-its volume}, and $NEW was removed"
fi

TMP_ENV="$ENV_FILE.restore.$$"
{
  grep -v '^LOOM_DATA_VOLUME=' "$ENV_FILE" || true
  echo "# restore.sh, $(date -u +%Y-%m-%dT%H:%M:%SZ): restored $(basename "$SRC"); the volume before was ${OLD:-?}"
  echo "LOOM_DATA_VOLUME=$NEW"
} > "$TMP_ENV"
mv "$TMP_ENV" "$ENV_FILE"

if ! compose up -d --wait --force-recreate loom backup; then
  die "the server did not come up on $NEW. To go back: delete the LOOM_DATA_VOLUME line from $ENV_FILE, then \`docker compose up -d --wait --force-recreate loom backup\`"
fi
MOUNTED="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' \
  "$(compose ps -q loom)")"
[ "$MOUNTED" = "$NEW" ] || die "the server's /data is $MOUNTED, not $NEW"
HEALTH="$(compose exec -T -u app loom python -c \
  "import json, urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=5))['status'])")"
[ "$HEALTH" = "ok" ] || die "the server on $NEW answers health with $HEALTH"

echo "restore.sh: the server runs on $NEW, recorded as LOOM_DATA_VOLUME in $ENV_FILE; health ok"
echo "restore.sh: ${OLD:-the previous volume} is untouched; remove it with \`docker volume rm ${OLD:-<name>}\` once the drill passes"
echo "restore.sh: now the drill, from a client: an authenticated read and a ?q= search (SERVER-GUIDE Part 2 §8)"
