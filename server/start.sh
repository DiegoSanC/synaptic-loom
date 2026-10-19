#!/usr/bin/env bash
# start.sh — PORTABLE start of the loom server WITHOUT systemd (PLAN §T7.1 fallback).
#
# On this pilot machine the server runs under systemd --user (unit loom-server.service);
# to manage it use `systemctl --user {start,stop,status} loom-server`. This script is the
# alternative for machines where `systemctl --user` is not available: it launches uvicorn with
# nohup (survives closing the shell) and leaves a pidfile in ~/.loom-server/uvicorn.pid.
#
# Config via environment (with defaults):
#   LOOM_DB         DB sqlite    (default ~/.loom-server/loom.db)
#   LOOM_HOST       bind host    (default 127.0.0.1 — pilot without TLS, localhost only)
#   LOOM_PORT       bind port    (default 8787)
#   LOOM_SERVER_PY  interpreter  (default <repo>/.venv/bin/python)
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
  echo "loom-server is already running (pid $(cat "$PIDFILE"))."
  exit 0
fi
if ss -tlnp 2>/dev/null | grep -q ":$PORT "; then
  echo "start.sh: port $PORT is already in use (systemd? use 'systemctl --user status loom-server')." >&2
  exit 1
fi

cd "$REPO_DIR"
nohup "$PY" -m uvicorn server.app:app --host "$HOST" --port "$PORT" \
  >>"$LOG" 2>&1 < /dev/null &
echo $! >"$PIDFILE"
echo "loom-server started (pid $(cat "$PIDFILE")) on http://$HOST:$PORT  (LOOM_DB=$LOOM_DB, log=$LOG)"
