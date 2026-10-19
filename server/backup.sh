#!/usr/bin/env bash
# backup.sh — a consistent, verified copy of the Loom database, with retention.
# DESIGN.md D8 states what a backup must be; D18 is this script in the Compose `backup`
# service (deploy/compose/docker-compose.yml, docs/SERVER-GUIDE.md Part 2 §8).
#
#   backup.sh                      one backup, then retention
#   backup.sh --every <interval>   one backup now and one every <interval> (a `sleep`
#                                  duration: 1800, 30m, 6h, 1d) until SIGTERM, which ends
#                                  it with exit 0. A failed backup ends it non-zero, so a
#                                  restart policy retries it and the failure is visible.
#   backup.sh --export             the current backups as a tar stream on stdout: what an
#                                  off-host copy pulls
#
# How the copy is made, and why:
# - SQLite's online backup API (Python's sqlite3.Connection.backup) gives a
#   transaction-consistent copy while the server keeps writing. The source is opened
#   read-only, so a backup can never create or change the live database.
# - The copy is written under a hidden temporary name, switched to journal_mode=DELETE so
#   it is one self-contained file, checked with PRAGMA integrity_check, fsynced, and only
#   then renamed to loom-<UTC timestamp>.db. An interrupted run leaves nothing that counts
#   as a backup. A copy that is not `ok` is deleted, the run exits non-zero and purges
#   nothing: a corrupt database never rotates the good backups out.
# - umask 077: a copy holds the same data as the database, so it is readable by its owner
#   only (files 0600; a directory this script creates, 0700).
# - One lock (<dir>/.lock) serializes runs and retention; an export holds it only while
#   it opens the files, so a stalled reader never holds up a backup.
# - Retention keeps the newest $LOOM_BACKUP_KEEP files with the name this script gives;
#   nothing else in the directory is ever deleted.
#
# Environment (the defaults are the retired loopback host's, RUNBOOK §3b; the Compose
# service sets all four):
#   LOOM_DB           source database        (default ~/.loom-server/loom.db)
#   LOOM_BACKUP_DIR   destination directory  (default ~/.loom-server/backups)
#   LOOM_BACKUP_KEEP  backups to keep        (default 56)
#   LOOM_SERVER_PY    a Python 3 interpreter (default <repo>/.venv/bin/python)
set -euo pipefail
umask 077

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${LOOM_DB:-$HOME/.loom-server/loom.db}"
BACKUP_DIR="${LOOM_BACKUP_DIR:-$HOME/.loom-server/backups}"
KEEP="${LOOM_BACKUP_KEEP:-56}"
PY="${LOOM_SERVER_PY:-$REPO_DIR/.venv/bin/python}"

usage() {
  echo "usage: backup.sh [--every <interval> | --export]" >&2
  exit 2
}

case "$KEEP" in
  '' | *[!0-9]* | 0 | 0*)
    echo "backup.sh: LOOM_BACKUP_KEEP must be a positive integer (got '$KEEP')" >&2
    exit 1 ;;
esac

# $1 = backup | export. The Python program arrives on stdin, so stdout stays the export's.
run() {
  "$PY" - "$1" "$DB" "$BACKUP_DIR" "$KEEP" <<'PYEOF'
import datetime
import fcntl
import os
import pathlib
import re
import sqlite3
import sys
import tarfile

mode, db, out_dir, keep = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
NAME = re.compile(r"loom-\d{8}T\d{6}\.\d{6}Z\.db")


def fail(message):
    print(f"backup.sh: {message}", file=sys.stderr)
    sys.exit(1)


def backups():
    return sorted(n for n in os.listdir(out_dir) if NAME.fullmatch(n))


def remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def fsync(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


os.makedirs(out_dir, mode=0o700, exist_ok=True)
lock = open(os.path.join(out_dir, ".lock"), "a")
fcntl.flock(lock, fcntl.LOCK_EX)

if mode == "export":
    # Open every backup under the lock, then let it go before streaming: a backup is never
    # rewritten once renamed, an open file survives its purge, and a reader that stalls
    # (a sleeping laptop, a slow link) must not hold up the next backup.
    files = [(name, open(os.path.join(out_dir, name), "rb")) for name in backups()]
    fcntl.flock(lock, fcntl.LOCK_UN)
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as tar:
        for name, handle in files:
            info = tar.gettarinfo(arcname=name, fileobj=handle)
            tar.addfile(info, handle)
            handle.close()
    sys.exit(0)

# Under the lock no other run is writing, so any temporary file is a killed run's.
for name in os.listdir(out_dir):
    if name.startswith(".loom-") and ".db.tmp" in name:
        remove(os.path.join(out_dir, name))

if not os.path.isfile(db):
    fail(f"no source database at {db}; nothing purged")

stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
final = os.path.join(out_dir, f"loom-{stamp}.db")
tmp = os.path.join(out_dir, f".loom-{stamp}.db.tmp")
try:
    src = sqlite3.connect(pathlib.Path(db).resolve().as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        result = [row[0] for row in dst.execute("PRAGMA integrity_check")]
    finally:
        dst.close()
        src.close()
except sqlite3.Error as exc:
    remove(tmp)
    remove(tmp + "-journal")
    fail(f"the backup of {db} failed ({exc}); nothing purged")
if result != ["ok"]:
    remove(tmp)
    fail(f"integrity_check of the copy of {db} is not ok ({'; '.join(result[:5])}); "
         "the copy was deleted and nothing purged")

os.chmod(tmp, 0o600)
fsync(tmp)
os.replace(tmp, final)
fsync(out_dir)
print(f"backup.sh: wrote {final} ({os.path.getsize(final)} bytes, integrity_check ok)")

present = backups()
purge = present[:-keep]
for name in purge:
    remove(os.path.join(out_dir, name))
if purge:
    print(f"backup.sh: purged {len(purge)} backup(s) beyond the newest {keep}")
print(f"backup.sh: {len(present) - len(purge)} backup(s) in {out_dir}")
PYEOF
}

every() {
  local interval="$1" sleeper=""
  # Docker stops a container with SIGTERM to PID 1, which is this shell. The sleep runs in
  # the background so the trap fires at once instead of when the interval ends; a backup
  # in progress is finished first (bash runs a trap after its foreground child exits).
  trap 'echo "backup.sh: stopping"; [ -z "$sleeper" ] || kill "$sleeper" 2>/dev/null; exit 0' TERM INT
  echo "backup.sh: one backup now and one every $interval (keep $KEEP)"
  while :; do
    run backup || {
      status=$?
      echo "backup.sh: backup failed (exit $status); stopping so the failure is seen" >&2
      exit "$status"
    }
    sleep "$interval" &
    sleeper=$!
    wait "$sleeper" || true
    sleeper=""
  done
}

case "$#:${1:-}" in
  0:) run backup ;;
  1:--export) run export ;;
  2:--every)
    if ! [[ "$2" =~ ^[0-9]+[smhd]?$ ]] || [[ "$2" =~ ^0+[smhd]?$ ]]; then
      echo "backup.sh: LOOM_BACKUP_INTERVAL must be a sleep duration such as 30m, 6h or 1d (got '$2')" >&2
      exit 1
    fi
    every "$2" ;;
  *) usage ;;
esac
