#!/usr/bin/env bash
# stop.sh — detiene el servidor loom arrancado con start.sh (fallback sin systemd).
# Si el servidor corre bajo systemd --user, usa en su lugar: systemctl --user stop loom-server.
set -euo pipefail

PIDFILE="$HOME/.loom-server/uvicorn.pid"
if [ ! -f "$PIDFILE" ]; then
  echo "No hay pidfile ($PIDFILE); nada que detener."
  echo "Si el servidor corre bajo systemd: systemctl --user stop loom-server"
  exit 0
fi
PID="$(cat "$PIDFILE" 2>/dev/null || true)"
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "loom-server detenido (pid $PID)."
else
  echo "El pid $PID ya no está activo."
fi
rm -f "$PIDFILE"
