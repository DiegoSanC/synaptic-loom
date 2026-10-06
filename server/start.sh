#!/usr/bin/env bash
# start.sh — arranque PORTÁTIL del servidor loom SIN systemd (fallback de PLAN §T7.1).
#
# En esta máquina piloto el servidor corre bajo systemd --user (unidad loom-server.service);
# para gestionarlo usa `systemctl --user {start,stop,status} loom-server`. Este script es la
# alternativa para máquinas donde `systemctl --user` no está disponible: lanza uvicorn con
# nohup (sobrevive al cierre del shell) y deja un pidfile en ~/.loom-server/uvicorn.pid.
#
# Config por entorno (con defaults):
#   LOOM_DB         DB sqlite    (default ~/.loom-server/loom.db)
#   LOOM_HOST       bind host    (default 127.0.0.1 — piloto sin TLS, solo localhost)
#   LOOM_PORT       bind port    (default 8787)
#   LOOM_SERVER_PY  intérprete   (default <repo>/.venv/bin/python)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${LOOM_SERVER_PY:-$REPO_DIR/.venv/bin/python}"
HOST="${LOOM_HOST:-127.0.0.1}"
PORT="${LOOM_PORT:-8787}"
STATE_DIR="$HOME/.loom-server"
export LOOM_DB="${LOOM_DB:-$STATE_DIR/loom.db}"
PIDFILE="$STATE_DIR/uvicorn.pid"
LOG="$STATE_DIR/uvicorn.log"

mkdir -p "$STATE_DIR"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
  echo "loom-server ya está corriendo (pid $(cat "$PIDFILE"))."
  exit 0
fi
if ss -tlnp 2>/dev/null | grep -q ":$PORT "; then
  echo "start.sh: el puerto $PORT ya está ocupado (¿systemd? usa 'systemctl --user status loom-server')." >&2
  exit 1
fi

cd "$REPO_DIR"
nohup "$PY" -m uvicorn server.app:app --host "$HOST" --port "$PORT" \
  >>"$LOG" 2>&1 < /dev/null &
echo $! >"$PIDFILE"
echo "loom-server arrancado (pid $(cat "$PIDFILE")) en http://$HOST:$PORT  (LOOM_DB=$LOOM_DB, log=$LOG)"
