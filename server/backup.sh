#!/usr/bin/env bash
# backup.sh — copia consistente de la DB con la API de backup online de sqlite + retención
# de 14 (PLAN §T7.2). Usa el `.backup` de sqlite (copia coherente aunque el servidor esté
# escribiendo). Si el binario `sqlite3` no soporta `.backup`, cae al equivalente por stdlib
# (`sqlite3.Connection.backup`), que es el MISMO mecanismo de backup online.
#
# El backup hereda la sensibilidad de los datos: mismo host, mismos permisos (DESIGN §8).
#
# Config por entorno:
#   LOOM_DB          DB origen   (default ~/.loom-server/loom.db)
#   LOOM_BACKUP_DIR  destino     (default ~/.loom-server/backups)
#   LOOM_BACKUP_KEEP retención   (default 14)
#   LOOM_SERVER_PY   intérprete para el fallback stdlib (default <repo>/.venv/bin/python)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${LOOM_DB:-$HOME/.loom-server/loom.db}"
BACKUP_DIR="${LOOM_BACKUP_DIR:-$HOME/.loom-server/backups}"
KEEP="${LOOM_BACKUP_KEEP:-14}"
PY="${LOOM_SERVER_PY:-$REPO_DIR/.venv/bin/python}"

if [ ! -f "$DB" ]; then
  echo "backup.sh: no existe la DB de origen: $DB" >&2
  exit 1
fi
mkdir -p "$BACKUP_DIR"
STAMP="$(date +%F-%H%M%S)"
OUT="$BACKUP_DIR/loom-$STAMP.db"

if command -v sqlite3 >/dev/null 2>&1 && sqlite3 "$DB" ".backup '$OUT'" 2>/dev/null; then
  :
else
  # Fallback: backup online por stdlib (idéntico mecanismo, sin depender del binario).
  "$PY" - "$DB" "$OUT" <<'PYEOF'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(src); d = sqlite3.connect(dst)
with d:
    s.backup(d)
d.close(); s.close()
PYEOF
fi

echo "Backup escrito: $OUT ($(du -h "$OUT" | cut -f1))"

# Retención: conservar los $KEEP más recientes; borrar el resto.
mapfile -t OLD < <(ls -1t "$BACKUP_DIR"/loom-*.db 2>/dev/null | tail -n +"$((KEEP + 1))")
if [ "${#OLD[@]}" -gt 0 ]; then
  rm -f "${OLD[@]}"
  echo "Purgados ${#OLD[@]} backup(s) antiguos (retención=$KEEP)."
fi
echo "Backups presentes: $(ls -1 "$BACKUP_DIR"/loom-*.db 2>/dev/null | wc -l)"
