#!/bin/sh
# Hand the volume over to the unprivileged user, then get out of the way.
#
# Fly creates a volume as root:root and attaches it at RUNTIME, so `COPY --chown` in the
# Dockerfile cannot help: a process already running as `app` would take Permission denied
# on its first write to /data. This script is the only thing that runs as root, it runs as
# root only long enough to fix the ownership, and then it `exec`s — so the server, not this
# shell, is PID 1 and receives SIGTERM directly from Fly (or from `docker stop`).
#
# `chown -R` rather than a plain chown of the directory: the migration in issue #2's
# Phase 3 puts the database on the volume over `fly sftp`, which writes as root. Without
# the -R the server would come up unable to write the very file it was given. The volume
# holds three files, so the recursion costs nothing.
set -eu

DATA_DIR="$(dirname "${LOOM_DB:-/data/loom.db}")"

# Validate before handing an unvalidated string to a recursive chown running as root.
# `dirname` is perfectly happy to return "." for a bare `LOOM_DB=loom.db` — one mistyped
# `fly secrets set` is enough — which would re-own /app (the venv and the server code)
# instead of the volume, and then the app writes its database to the ephemeral disk while
# looking like a clean boot. `LOOM_DB=/loom.db` returns "/" and re-owns the whole
# container filesystem. Both are misconfigurations that would otherwise succeed silently,
# which is the worst way for this particular mistake to fail.
if [ "${DATA_DIR#/}" = "$DATA_DIR" ] || [ -z "$(printf %s "$DATA_DIR" | tr -d /)" ]; then
    printf 'docker-entrypoint: refusing to chown %s — LOOM_DB must be an absolute path\n' \
        "'$DATA_DIR'" >&2
    printf 'docker-entrypoint: inside the volume, e.g. /data/loom.db (got %s)\n' \
        "'${LOOM_DB:-/data/loom.db}'" >&2
    exit 78   # EX_CONFIG
fi

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    chown -R app:app "$DATA_DIR"
    exec gosu app "$@"
fi

# Already unprivileged (an explicit `docker run --user`): nothing to hand over, and
# attempting the chown would abort under `set -e`.
exec "$@"
