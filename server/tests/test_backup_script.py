"""`server/backup.sh` (DESIGN.md D8 requirement, D18 Compose implementation), driven for real.

The script is run as a subprocess with bash, against throwaway SQLite files in tmp_path —
the same entry point the Compose `backup` service and an operator use. Nothing in it is
imported or mocked. The schema is a two-column stand-in: the copy is SQLite's online
backup API, which is schema-agnostic, so the server's schema would prove nothing more.

Expected values are literals: the file-name shape, the modes, the row sets planted here.
The Docker boundary (the `backup` service, its volume, restore into a new volume) is goal
wave `s`.
"""
import io
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "backup.sh"
BACKUP_NAME = re.compile(r"loom-\d{8}T\d{6}\.\d{6}Z\.db")
MARKER = "MARKER-ROW-AAAAAAAAAAAAAAAA"


def _env(db: Path, backups: Path, keep: str = "14") -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOOM_")}
    env.update(LOOM_DB=str(db), LOOM_BACKUP_DIR=str(backups), LOOM_BACKUP_KEEP=keep,
               LOOM_SERVER_PY=sys.executable)
    return env


def _run(db: Path, backups: Path, *args: str, keep: str = "14",
         umask: str | None = None) -> subprocess.CompletedProcess:
    cmd = ["bash", str(SCRIPT), *args]
    if umask is not None:
        cmd = ["bash", "-c", f'umask {umask}; exec bash "$0" "$@"', str(SCRIPT), *args]
    return subprocess.run(cmd, env=_env(db, backups, keep), capture_output=True,
                          timeout=60)


def _make_db(path: Path, rows, *, wal: bool = False) -> Path:
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("CREATE INDEX t_x ON t (x)")
    conn.executemany("INSERT INTO t (x) VALUES (?)", [(r,) for r in rows])
    conn.commit()
    conn.close()
    return path


def _rows(path: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return sorted(r[0] for r in conn.execute("SELECT x FROM t"))
    finally:
        conn.close()


def _corrupt_db(path: Path) -> Path:
    """A database that opens and copies, but whose index disagrees with its table.

    The marker is rewritten in the table's leaf page only (page 2 comes before the index's
    page 3 in the file), so the online backup copies the damage verbatim and only
    `PRAGMA integrity_check` can see it.
    """
    _make_db(path, [MARKER, "other-1", "other-2"])
    data = path.read_bytes()
    assert data.count(MARKER.encode()) == 2, "the marker must sit in the table and the index"
    path.write_bytes(data.replace(MARKER.encode(), MARKER.replace("A", "B").encode(), 1))
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)], (
            "the fixture must really be corrupt, or the failure test proves nothing")
    finally:
        conn.close()
    return path


def _backups(backups: Path) -> list[str]:
    return sorted(p.name for p in backups.iterdir() if BACKUP_NAME.fullmatch(p.name))


def _plant(backups: Path, name: str, rows=("planted",)) -> Path:
    backups.mkdir(mode=0o700, exist_ok=True)
    return _make_db(backups / name, rows)


# --------------------------------------------------------------------------- #
# One backup                                                                   #
# --------------------------------------------------------------------------- #
def test_a_backup_is_a_verified_self_contained_copy_named_by_its_utc_time(tmp_path):
    db = _make_db(tmp_path / "loom.db", ["a", "b", "c"], wal=True)
    backups = tmp_path / "backups"
    done = _run(db, backups)
    assert done.returncode == 0, done.stderr
    [name] = _backups(backups)
    assert _rows(backups / name) == ["a", "b", "c"]
    assert b"integrity_check ok" in done.stdout, done.stdout
    # One file, not a WAL database whose last commits sit in a sidecar a copy could miss.
    conn = sqlite3.connect(backups / name)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    finally:
        conn.close()
    assert sorted(p.name for p in backups.iterdir()) == [".lock", name]


def test_the_copy_is_consistent_while_a_writer_holds_an_open_transaction(tmp_path):
    db = _make_db(tmp_path / "loom.db", [f"committed-{i}" for i in range(100)], wal=True)
    writer = sqlite3.connect(db, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO t (x) VALUES ('uncommitted')")
        done = _run(db, tmp_path / "backups")
        assert done.returncode == 0, done.stderr
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    [name] = _backups(tmp_path / "backups")
    rows = _rows(tmp_path / "backups" / name)
    assert len(rows) == 100 and "uncommitted" not in rows


def test_the_backup_never_writes_the_source(tmp_path):
    db = _make_db(tmp_path / "loom.db", ["a"])
    before = db.read_bytes()
    assert _run(db, tmp_path / "backups").returncode == 0
    assert db.read_bytes() == before

    missing = tmp_path / "absent" / "loom.db"
    done = _run(missing, tmp_path / "backups")
    assert done.returncode != 0
    assert b"no source database" in done.stderr, done.stderr
    assert not missing.exists(), "a backup must never create the database it copies"


def test_a_corrupt_copy_fails_loudly_and_purges_nothing(tmp_path):
    backups = tmp_path / "backups"
    old = ["loom-20200101T000000.000000Z.db", "loom-20200102T000000.000000Z.db"]
    for name in old:
        _plant(backups, name)
    db = _corrupt_db(tmp_path / "loom.db")
    done = _run(db, backups, keep="2")
    assert done.returncode != 0, done.stdout
    assert b"integrity_check" in done.stderr and b"nothing purged" in done.stderr, done.stderr
    assert sorted(p.name for p in backups.iterdir()) == [".lock", *old], (
        "a failed copy must leave neither a new file nor a temporary one, and delete none")


def test_the_copy_and_its_directory_are_private_even_under_a_permissive_umask(tmp_path):
    db = _make_db(tmp_path / "loom.db", ["a"])
    backups = tmp_path / "new" / "backups"
    done = _run(db, backups, umask="000")
    assert done.returncode == 0, done.stderr
    [name] = _backups(backups)
    assert (backups / name).stat().st_mode & 0o777 == 0o600
    assert backups.stat().st_mode & 0o777 == 0o700


def test_retention_keeps_the_newest_and_deletes_only_what_it_named(tmp_path):
    backups = tmp_path / "backups"
    planted = [f"loom-2020010{d}T000000.000000Z.db" for d in range(1, 5)]
    for name in planted:
        _plant(backups, name)
    foreign = ["notes.txt", "loom-manual.db", "loom-2026-09-01-030000.db"]
    for name in foreign:
        (backups / name).write_text("not a backup this script wrote\n")
    db = _make_db(tmp_path / "loom.db", ["a"])
    done = _run(db, backups, keep="3")
    assert done.returncode == 0, done.stderr
    kept = _backups(backups)
    assert len(kept) == 3 and kept[:2] == planted[2:], kept
    assert _rows(backups / kept[2]) == ["a"], "the newest must be the copy just written"
    assert all((backups / name).exists() for name in foreign)
    assert b"purged 2" in done.stdout, done.stdout


def test_a_stale_temporary_file_is_neither_counted_nor_left_behind(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    stale = backups / ".loom-20990101T000000.000000Z.db.tmp"
    stale.write_bytes(b"half a database, from a run that was killed")
    db = _make_db(tmp_path / "loom.db", ["a"])
    done = _run(db, backups, keep="1")
    assert done.returncode == 0, done.stderr
    [name] = _backups(backups)
    assert sorted(p.name for p in backups.iterdir()) == [".lock", name]


@pytest.mark.parametrize("keep", ["0", "-1", "abc", "007"])
def test_an_invalid_keep_is_refused_before_anything_is_written(tmp_path, keep):
    db = _make_db(tmp_path / "loom.db", ["a"])
    done = _run(db, tmp_path / "backups", keep=keep)
    assert done.returncode != 0
    assert b"LOOM_BACKUP_KEEP" in done.stderr, done.stderr
    assert not (tmp_path / "backups").exists()


# --------------------------------------------------------------------------- #
# --export: what an off-host copy pulls                                        #
# --------------------------------------------------------------------------- #
def test_export_streams_exactly_the_backups_as_a_tar(tmp_path):
    backups = tmp_path / "backups"
    planted = ["loom-20200101T000000.000000Z.db", "loom-20200102T000000.000000Z.db"]
    for i, name in enumerate(planted):
        _plant(backups, name, rows=(f"row-{i}",))
        os.chmod(backups / name, 0o600)
    (backups / ".loom-20990101T000000.000000Z.db.tmp").write_bytes(b"partial")
    (backups / "notes.txt").write_text("not a backup\n")
    done = _run(tmp_path / "unused.db", backups, "--export")
    assert done.returncode == 0, done.stderr
    with tarfile.open(fileobj=io.BytesIO(done.stdout), mode="r:") as tar:
        members = tar.getmembers()
        assert [m.name for m in members] == planted
        assert all(m.isreg() and m.mode & 0o777 == 0o600 for m in members), members
        for member in members:
            assert tar.extractfile(member).read() == (backups / member.name).read_bytes()


# --------------------------------------------------------------------------- #
# --every: the Compose service's loop                                          #
# --------------------------------------------------------------------------- #
def _start_every(db: Path, backups: Path, interval: str) -> subprocess.Popen:
    return subprocess.Popen(["bash", str(SCRIPT), "--every", interval],
                            env=_env(db, backups), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)


def _wait_for_backups(backups: Path, count: int, proc: subprocess.Popen) -> None:
    deadline = time.monotonic() + 30
    while not (backups.is_dir() and len(_backups(backups)) >= count):
        assert proc.poll() is None, proc.communicate()
        assert time.monotonic() < deadline, f"fewer than {count} backups after 30 s"
        time.sleep(0.1)


def test_every_backs_up_at_once_and_then_once_per_interval(tmp_path):
    db = _make_db(tmp_path / "loom.db", ["a"])
    backups = tmp_path / "backups"
    proc = _start_every(db, backups, "1")
    try:
        _wait_for_backups(backups, 3, proc)
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=10)


def test_every_stops_at_once_and_cleanly_on_sigterm(tmp_path):
    db = _make_db(tmp_path / "loom.db", ["a"])
    backups = tmp_path / "backups"
    proc = _start_every(db, backups, "3600")
    _wait_for_backups(backups, 1, proc)
    started = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=10)
    assert proc.returncode == 0, err
    assert time.monotonic() - started < 5, "a sleeping loop must not wait out its interval"
    assert b"stopping" in out, out


@pytest.mark.parametrize("interval", ["bogus", "0", "", "6x", "-1"])
def test_every_refuses_an_interval_it_cannot_sleep(tmp_path, interval):
    db = _make_db(tmp_path / "loom.db", ["a"])
    done = _run(db, tmp_path / "backups", "--every", interval)
    assert done.returncode != 0
    assert b"LOOM_BACKUP_INTERVAL" in done.stderr, done.stderr
    assert not (tmp_path / "backups").exists()


def test_every_exits_with_the_failure_instead_of_looping_past_it(tmp_path):
    """A failed backup ends the loop non-zero, so the container's restart policy retries
    it and `docker compose ps` shows it — a loop that logged and slept would hide it."""
    proc = _start_every(tmp_path / "absent.db", tmp_path / "backups", "3600")
    out, err = proc.communicate(timeout=30)
    assert proc.returncode != 0
    assert b"no source database" in err, err


def test_an_unknown_argument_is_refused(tmp_path):
    db = _make_db(tmp_path / "loom.db", ["a"])
    done = _run(db, tmp_path / "backups", "--bogus")
    assert done.returncode != 0
    assert b"usage" in done.stderr.lower(), done.stderr
