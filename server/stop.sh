#!/usr/bin/env bash
# stop.sh — stops the loom server started with start.sh (fallback without systemd).
# If the server runs under systemd --user, use instead: systemctl --user stop loom-server.
set -euo pipefail

PIDFILE="$HOME/.loom-server/uvicorn.pid"
if [ ! -f "$PIDFILE" ]; then
  echo "No pidfile ($PIDFILE); nothing to stop."
  echo "If the server runs under systemd: systemctl --user stop loom-server"
  exit 0
fi
PID="$(cat "$PIDFILE" 2>/dev/null || true)"
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "loom-server stopped (pid $PID)."
else
  echo "pid $PID is no longer running."
fi
rm -f "$PIDFILE"
