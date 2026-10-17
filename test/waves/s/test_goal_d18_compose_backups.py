"""FROZEN GOAL — D18: backups for the Docker Compose topology, and the restore.
Read-only to implementers.

Oracle properties: the REAL boundary (AGENTS.md §10). The stack under test is the repo's
own ``deploy/compose/docker-compose.yml``, brought up with a real
``docker compose up --build`` of the repo's own ``Dockerfile`` and real named volumes; the
restore is the repo's own ``deploy/compose/restore.sh``, run on the host as an operator
runs it. The ONLY layer on top is ``compose.goal.yml`` next to this file, which moves the
``backend`` subnet off wave r's so the two waves can share a Docker host. Caddy is never
started: the edge is wave r's, and what this wave grades is the data behind the API. The
interval and retention the wave runs with (2 s, 3 copies) come from its env file, the way
an operator's ``.env`` sets them; the defaults are graded on the production file.

What it proves (owner decision of 2026-10-17, D18; the D8 requirement):

* the ``backup`` service is the server's image run as ``app``, with no network and no
  published port, writing into its own volume;
* with the stack up and entries written through the API, the service produces a backup
  that passes ``PRAGMA integrity_check``, contains those entries and is readable by its
  owner only;
* retention keeps exactly ``LOOM_BACKUP_KEEP`` backups, the newest ones;
* a corrupt copy fails loudly, is not counted and purges nothing;
* the off-host pull (``backup.sh --export``) carries exactly the backups;
* ``restore.sh`` refuses a corrupt backup without changing anything, and otherwise puts
  the server on a NEW volume that answers health, an authenticated read and a ``?q=``
  search with the restored data — while the volume it replaced stays byte-for-byte as it
  was;
* ``docker compose stop`` stops the backup service cleanly.

Skips are scoped to the test that needs the tool, and under ``LOOM_GATE=1`` a missing tool
is a FAILURE (the wave-m discipline). Every product value asserted here is a literal in
this file next to the name of what it mirrors; nothing is imported from the stack.

The stack uses a FIXED subnet, so two runs of this wave cannot overlap on one Docker host;
each run tears its stack, volumes and images down.

Run: .venv/bin/python -m pytest test/waves/s/test_goal_d18_compose_backups.py -q
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _goalkit as kit  # noqa: E402

REPO_ROOT = kit.REPO_ROOT
COMPOSE_DIR = REPO_ROOT / "deploy" / "compose"
COMPOSE_FILE = COMPOSE_DIR / "docker-compose.yml"
ENV_EXAMPLE = COMPOSE_DIR / ".env.example"
RESTORE_SH = COMPOSE_DIR / "restore.sh"
GOAL_OVERRIDE = Path(__file__).with_name("compose.goal.yml")
API_PROBE = Path(__file__).with_name("fixtures") / "api.py"

LOOM_GATE = os.environ.get("LOOM_GATE") == "1"

# --------------------------------------------------------------------------- #
# Contract literals (duplicated, never imported)                               #
# --------------------------------------------------------------------------- #
SERVER, BACKUP, PROXY = "loom", "backup", "caddy"   # docker-compose.yml service names
DATA_KEY, BACKUPS_KEY = "loom_data", "loom_backups"  # docker-compose.yml volumes
DATA_VOLUME, BACKUPS_VOLUME = "loom_loom_data", "loom_loom_backups"  # project `loom`
BACKUP_SH = "/app/server/backup.sh"                  # Dockerfile COPY server/ /app/server/
DB_PATH = "/data/loom.db"                            # Dockerfile ENV LOOM_DB
BACKUPS_DIR = "/backups"
VENV_PY = "/app/.venv/bin/python"
DEFAULT_INTERVAL, DEFAULT_KEEP = "6h", "14"          # D18 defaults
GOAL_INTERVAL, GOAL_KEEP = "2s", 3                   # this wave's env file
BACKUP_NAME = re.compile(r"loom-\d{8}T\d{6}\.\d{6}Z\.db")  # backup.sh's file name
WROTE = re.compile(r"backup\.sh: wrote /backups/(loom-\S+\.db) ")
STOPPING = "backup.sh: stopping"
BACKUP_GRACE_S = 30                                  # backup.stop_grace_period
RESTORED_VOLUME = re.compile(r"(?P<project>.+)_loom_data_restored_\d{8}T\d{6}Z")
GOAL_REMOTE = "git@github.com:example-group/goal-s.git"
FIRST, SECOND = "zebrafirstmarker", "zebrasecondmarker"  # one FTS token each
MARKER = "MARKER-ROW-AAAAAAAAAAAAAAAA"


def _require_tool(name: str, why: str) -> None:
    if shutil.which(name) is not None:
        return
    reason = f"the Compose backup goal needs the real {name} binary: {why}"
    if LOOM_GATE:
        pytest.fail(f"LOOM_GATE=1 forbids skipping — {reason}")
    pytest.skip(reason)


def _run(args: list[str], *, env: dict | None = None, timeout: int = 900,
         input: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, timeout=timeout, env=env, input=input)


def _out(done: subprocess.CompletedProcess) -> str:
    return (done.stdout + done.stderr).decode(errors="replace")


def _docker(*args: str, timeout: int = 900) -> subprocess.CompletedProcess:
    return _run(["docker", *args], timeout=timeout)


def _clean_env() -> dict:
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("LOOM_", "COMPOSE_"))}


def _render(tmp_path_factory, env_text: str = "") -> dict:
    _require_tool("docker", "`docker compose config` renders the file this wave grades")
    env_file = tmp_path_factory.mktemp("envfile") / "render.env"
    env_file.write_text("LOOM_DOMAIN=loom.example.com\n" + env_text, encoding="utf-8")
    done = _run(["docker", "compose", "--env-file", str(env_file), "-f", str(COMPOSE_FILE),
                 "config", "--format", "json"], env=_clean_env())
    assert done.returncode == 0, _out(done)
    return json.loads(done.stdout)


def _corrupt_db(path: Path) -> bytes:
    """A database that opens and copies, but whose index disagrees with its table: the
    online backup copies the damage verbatim and only `integrity_check` can see it."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("CREATE INDEX t_x ON t (x)")
    conn.executemany("INSERT INTO t (x) VALUES (?)", [(MARKER,), ("o1",), ("o2",)])
    conn.commit()
    conn.close()
    data = path.read_bytes()
    assert data.count(MARKER.encode()) == 2
    path.write_bytes(data.replace(MARKER.encode(), MARKER.replace("A", "B").encode(), 1))
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)], (
            "the fixture must really be corrupt, or the failure legs prove nothing")
    finally:
        conn.close()
    return path.read_bytes()


# --------------------------------------------------------------------------- #
# The production file as Compose renders it — no override                      #
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def rendered(tmp_path_factory) -> dict:
    return _render(tmp_path_factory)


def test_the_backup_service_is_the_server_image_with_no_network_and_no_port(rendered):
    services = rendered["services"]
    backup = services[BACKUP]
    assert backup["build"] == services[SERVER]["build"], "same image as the server"
    assert backup["user"] == "app"
    assert backup.get("network_mode") == "none", backup.get("network_mode")
    assert not backup.get("networks"), backup.get("networks")
    assert not backup.get("ports"), backup.get("ports")
    assert backup["restart"] == "unless-stopped"
    assert backup["depends_on"][SERVER]["condition"] == "service_healthy"


def test_the_backup_service_reads_the_database_volume_and_writes_its_own(rendered):
    backup = rendered["services"][BACKUP]
    mounts = {v["target"]: v for v in backup["volumes"]}
    assert set(mounts) == {"/data", BACKUPS_DIR}, mounts
    assert all(m["type"] == "volume" for m in mounts.values()), mounts
    assert mounts["/data"]["source"] == DATA_KEY
    assert mounts[BACKUPS_DIR]["source"] == BACKUPS_KEY != DATA_KEY
    server_data = [v for v in rendered["services"][SERVER]["volumes"] if v["target"] == "/data"]
    assert server_data[0]["source"] == DATA_KEY, "the backup must read the server's volume"
    # The names the guide's commands use (project `loom`).
    assert rendered["volumes"][DATA_KEY]["name"] == DATA_VOLUME
    assert rendered["volumes"][BACKUPS_KEY]["name"] == BACKUPS_VOLUME


def test_the_backup_service_runs_backup_sh_every_6h_keeping_14(rendered):
    backup = rendered["services"][BACKUP]
    assert backup["command"] == [BACKUP_SH, "--every", DEFAULT_INTERVAL], backup["command"]
    assert backup["environment"] == {
        "LOOM_DB": DB_PATH, "LOOM_BACKUP_DIR": BACKUPS_DIR,
        "LOOM_BACKUP_KEEP": DEFAULT_KEEP, "LOOM_SERVER_PY": VENV_PY,
    }, backup["environment"]
    assert backup["stop_grace_period"] == f"{BACKUP_GRACE_S}s"


def test_the_env_example_documents_the_backup_settings_and_they_take_effect(
        tmp_path_factory):
    lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    for documented in (f"# LOOM_BACKUP_INTERVAL={DEFAULT_INTERVAL}",
                       f"# LOOM_BACKUP_KEEP={DEFAULT_KEEP}", "# LOOM_DATA_VOLUME="):
        assert documented in lines, f"{documented!r} must be in .env.example"
    changed = _render(tmp_path_factory, "LOOM_BACKUP_INTERVAL=30m\nLOOM_BACKUP_KEEP=5\n"
                                        "LOOM_DATA_VOLUME=some_restored_volume\n")
    backup = changed["services"][BACKUP]
    assert backup["command"] == [BACKUP_SH, "--every", "30m"]
    assert backup["environment"]["LOOM_BACKUP_KEEP"] == "5"
    assert changed["volumes"][DATA_KEY]["name"] == "some_restored_volume"


# --------------------------------------------------------------------------- #
# The live stack                                                               #
# --------------------------------------------------------------------------- #
@dataclass
class Stack:
    project: str
    env: dict
    env_file: Path
    work: Path
    token: str = ""
    project_id: int = 0
    first_backup: Path | None = None
    state: dict = field(default_factory=dict)

    def compose(self, *args: str, timeout: int = 900,
                input: bytes | None = None) -> subprocess.CompletedProcess:
        return _run(["docker", "compose", "-p", self.project, "--env-file", str(self.env_file),
                     "-f", str(COMPOSE_FILE), "-f", str(GOAL_OVERRIDE), *args],
                    env=self.env, timeout=timeout, input=input)

    def up(self, *, build: bool = False) -> None:
        done = self.compose("up", "-d", *(["--build"] if build else []), "--wait",
                            SERVER, BACKUP, timeout=2400)
        assert done.returncode == 0, (
            f"`docker compose up` must succeed:\n{_out(done)[-3000:]}"
            f"\n--- logs ---\n{_out(self.compose('logs', '--no-color'))[-3000:]}")

    def container(self, service: str) -> str:
        done = self.compose("ps", "-aq", service)
        assert done.returncode == 0 and done.stdout.strip(), _out(done)
        return done.stdout.decode().strip()

    def inspect(self, service: str, template: str) -> str:
        done = _docker("inspect", "-f", template, self.container(service))
        assert done.returncode == 0, _out(done)
        return done.stdout.decode().strip()

    def data_volume(self, service: str = SERVER) -> str:
        return self.inspect(service, '{{range .Mounts}}{{if eq .Destination "/data"}}'
                                     '{{.Name}}{{end}}{{end}}')

    def api(self, steps: list[dict]) -> list[dict]:
        done = self.compose("exec", "-T", "-u", "app", SERVER, "python", "-c",
                            API_PROBE.read_text(), json.dumps(steps), timeout=120)
        assert done.returncode == 0, f"API probe failed: {_out(done)[-2000:]}"
        return json.loads(done.stdout.decode().strip().splitlines()[-1])

    def writes(self) -> list[str]:
        """The backups the service reports writing, in order, from its own log."""
        done = self.compose("logs", "--no-color", BACKUP)
        assert done.returncode == 0, _out(done)
        return WROTE.findall(_out(done))

    def wait_for_writes(self, count: int, timeout: float = 90) -> list[str]:
        deadline = time.monotonic() + timeout
        while len(writes := self.writes()) < count:
            assert time.monotonic() < deadline, (
                f"the backup service wrote {len(writes)} backups, expected {count}:\n"
                f"{_out(self.compose('logs', '--no-color', BACKUP))[-2000:]}")
            time.sleep(0.5)
        return writes

    def run_backup_container(self, *args: str, input: bytes | None = None,
                             extra: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
        """A one-off container of the `backup` service, as an operator would start one."""
        return self.compose("run", "--rm", "--no-deps", "-T", *extra, BACKUP, *args,
                            input=input, timeout=300)

    def export(self) -> dict[str, tarfile.TarInfo | bytes]:
        """The documented off-host pull, run here instead of over SSH."""
        done = self.run_backup_container(BACKUP_SH, "--export")
        assert done.returncode == 0, _out(done)[-2000:]
        found = {}
        with tarfile.open(fileobj=io.BytesIO(done.stdout), mode="r:") as tar:
            for member in tar.getmembers():
                found[member.name] = (member, tar.extractfile(member).read())
        return found

    def listing(self) -> list[str]:
        done = self.run_backup_container("-A", BACKUPS_DIR, extra=("--entrypoint", "ls"))
        assert done.returncode == 0, _out(done)
        return sorted(done.stdout.decode().split())

    def volume_fingerprint(self, volume: str) -> dict:
        """Name, mode, size and sha256 of every file in a volume, mounted read-only."""
        script = ("import hashlib, json, os\nout = {}\n"
                  "for root, dirs, files in os.walk('/v'):\n"
                  "    for n in dirs + files:\n"
                  "        p = os.path.join(root, n); st = os.lstat(p)\n"
                  "        out[p] = [st.st_mode, st.st_size, hashlib.sha256(open(p, 'rb').read())"
                  ".hexdigest() if os.path.isfile(p) else None]\n"
                  "print(json.dumps(out, sort_keys=True))")
        image = self.inspect(SERVER, "{{.Image}}")
        done = _docker("run", "--rm", "--network", "none", "-v", f"{volume}:/v:ro",
                       "--entrypoint", "python", image, "-c", script, timeout=300)
        assert done.returncode == 0, _out(done)
        return json.loads(done.stdout)

    def entries_in_volume(self, volume: str, term: str) -> int:
        image = self.inspect(SERVER, "{{.Image}}")
        done = _docker("run", "--rm", "--network", "none", "-v", f"{volume}:/v:ro",
                       "--entrypoint", "python", image, "-c",
                       # A copy first: a WAL database cannot be opened on a read-only
                       # mount, and immutable=1 would ignore a -wal file's commits.
                       "import shutil, sqlite3, sys; shutil.copytree('/v', '/tmp/v'); "
                       "c = sqlite3.connect('/tmp/v/loom.db'); print(c.execute('SELECT "
                       "count(*) FROM entries WHERE content_md LIKE ?', "
                       "('%' + sys.argv[1] + '%',)).fetchone()[0])",
                       term, timeout=300)
        assert done.returncode == 0, _out(done)
        return int(done.stdout.decode().strip())

    def project_volumes(self) -> set[str]:
        done = _docker("volume", "ls", "-q", "--filter",
                       f"label=com.docker.compose.project={self.project}")
        assert done.returncode == 0, _out(done)
        return set(done.stdout.decode().split())

    def restore(self, backup_file: Path) -> subprocess.CompletedProcess:
        env = {**self.env, "COMPOSE_PROJECT_NAME": self.project,
               "COMPOSE_FILE": f"{COMPOSE_FILE}:{GOAL_OVERRIDE}",
               "LOOM_ENV_FILE": str(self.env_file)}
        return _run(["bash", str(RESTORE_SH), str(backup_file)], env=env, timeout=1200)


def _post_entry(stack: Stack, marker: str) -> None:
    content = f"## Decisions\n\n- [fact] The goal-s probe entry carries {marker}.\n"
    [made] = stack.api([{"method": "POST", "path": "/api/entries", "token": stack.token,
                         "body": {"project_remote": GOAL_REMOTE, "session_id": f"s-{marker}",
                                  "content_md": content,
                                  "content_hash": hashlib.sha256(content.encode()).hexdigest()}}])
    assert made["status"] == 201, made


def _search(stack: Stack, term: str) -> list[dict]:
    [found] = stack.api([{"path": f"/api/projects/{stack.project_id}/entries?q={term}",
                          "token": stack.token}])
    assert found["status"] == 200, found
    return found["body"]["entries"]


@pytest.fixture(scope="session")
def stack(tmp_path_factory):
    _require_tool("docker", "the Compose stack IS the boundary this goal validates")
    project = f"loom-goal-s-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    work = tmp_path_factory.mktemp("goal-s")
    env_file = work / "goal.env"
    env_file.write_text(f"LOOM_DOMAIN=loom.localhost\nLOOM_BACKUP_INTERVAL={GOAL_INTERVAL}\n"
                        f"LOOM_BACKUP_KEEP={GOAL_KEEP}\n", encoding="utf-8")
    live = Stack(project, _clean_env(), env_file, work)
    try:
        live.up(build=True)
        made = live.compose("exec", "-T", "-u", "app", SERVER, "python", "-m", "server.admin",
                            "adduser", "--name", "goal-s", "--email", "goal-s@example.invalid")
        assert made.returncode == 0, _out(made)
        live.token = kit.parse_admin_token(made.stdout.decode())
        [registered] = live.api([{"method": "POST", "path": "/api/projects",
                                  "token": live.token, "body": {"remote_url": GOAL_REMOTE}}])
        assert registered["status"] == 201, registered
        live.project_id = registered["body"]["id"]
        yield live
    finally:
        live.compose("down", "-v", "--rmi", "local", "--remove-orphans", timeout=600)
        # A restore leaves the volume it replaced, which the stack no longer declares.
        for volume in live.project_volumes():
            _docker("volume", "rm", "-f", volume, timeout=120)


def test_the_service_backs_up_entries_written_through_the_api_privately(stack):
    assert stack.inspect(BACKUP, "{{.HostConfig.NetworkMode}}") == "none"
    assert stack.inspect(BACKUP, "{{.Config.User}}") == "app"
    _post_entry(stack, FIRST)
    # Two more writes: the first may have started before the entry was committed.
    after = len(stack.writes()) + 2
    writes = stack.wait_for_writes(after)
    exported = stack.export()
    name = max(exported)
    assert name >= writes[after - 1], (
        f"the newest backup must be one that started after the entry: {name} {writes}")
    member, data = exported[name]
    assert BACKUP_NAME.fullmatch(name), name
    assert member.mode & 0o777 == 0o600 and member.uname == "app", (member.mode, member.uname)

    copy = stack.work / name
    copy.write_bytes(data)
    conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert conn.execute("SELECT count(*) FROM entries WHERE content_md LIKE ?",
                            (f"%{FIRST}%",)).fetchone() == (1,)
    finally:
        conn.close()
    stack.first_backup = copy

    modes = stack.compose("exec", "-T", BACKUP, "stat", "-c", "%a %U %n",
                          BACKUPS_DIR, f"{BACKUPS_DIR}/{name}")
    assert modes.returncode == 0, _out(modes)
    assert modes.stdout.decode().split("\n")[:2] == [
        f"700 app {BACKUPS_DIR}", f"600 app {BACKUPS_DIR}/{name}"], modes.stdout


def test_retention_keeps_exactly_keep_backups_the_newest(stack):
    stack.wait_for_writes(GOAL_KEEP + 2)
    container = stack.container(BACKUP)
    started = time.monotonic()
    stopped = stack.compose("stop", BACKUP, timeout=300)
    elapsed = time.monotonic() - started
    assert stopped.returncode == 0, _out(stopped)
    state = _docker("inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}", container)
    assert state.stdout.decode().split() == ["exited", "0"], state.stdout
    assert elapsed < BACKUP_GRACE_S, f"stopping the backup service took {elapsed:.1f}s"
    writes = stack.writes()
    exported = stack.export()
    assert sorted(exported) == sorted(writes[-GOAL_KEEP:]), (list(exported), writes)
    assert stack.listing() == sorted([".lock", *writes[-GOAL_KEEP:]]), (
        "nothing but the kept backups and the lock: no temporary file, no older backup")


def test_a_corrupt_copy_fails_loudly_is_not_counted_and_purges_nothing(stack):
    before = stack.listing()
    assert len([n for n in before if BACKUP_NAME.fullmatch(n)]) == GOAL_KEEP, before
    planted = stack.run_backup_container(
        "-c", f"printf partial > {BACKUPS_DIR}/.loom-20990101T000000.000000Z.db.tmp",
        extra=("--entrypoint", "sh"))
    assert planted.returncode == 0, _out(planted)
    corrupt = _corrupt_db(stack.work / "corrupt.db")
    done = stack.run_backup_container(
        "-c", f"cat > /tmp/corrupt.db && exec {BACKUP_SH}", input=corrupt,
        extra=("-e", "LOOM_DB=/tmp/corrupt.db", "--entrypoint", "sh"))
    assert done.returncode != 0, _out(done)
    assert "integrity_check" in _out(done) and "nothing purged" in _out(done), _out(done)
    assert stack.listing() == before, (
        "a failed copy must leave no file behind (not even the stale temporary one it "
        "found) and delete no backup")
    assert sorted(stack.export()) == [n for n in before if BACKUP_NAME.fullmatch(n)]


def test_a_restore_refuses_a_corrupt_backup_and_changes_nothing(stack):
    assert stack.first_backup is not None, "the first live test must have run"
    server = stack.container(SERVER)
    volume = stack.data_volume()
    env_before = stack.env_file.read_text(encoding="utf-8")
    volumes_before = stack.project_volumes()
    done = stack.restore(stack.work / "corrupt.db")
    assert done.returncode != 0, _out(done)
    assert "integrity_check" in _out(done) and "nothing was changed" in _out(done), _out(done)
    assert stack.env_file.read_text(encoding="utf-8") == env_before
    assert stack.project_volumes() == volumes_before, "the half-made volume must be removed"
    assert stack.container(SERVER) == server and stack.data_volume() == volume
    [health] = stack.api([{"path": "/api/health"}])
    assert health["status"] == 200, "a refused restore must not take the server down"


def test_restore_puts_the_server_on_a_new_volume_and_leaves_the_old_one_as_it_was(stack):
    assert stack.first_backup is not None, "the first live test must have run"
    _post_entry(stack, SECOND)          # after the backup: the restore must not have it
    old = stack.data_volume()
    assert old == f"{stack.project}_loom_data", old
    # Quiescent, so "untouched" can be checked byte for byte: a running server's own
    # checkpoint would change the file without the restore having touched it.
    assert stack.compose("stop", BACKUP, SERVER, timeout=300).returncode == 0
    before = stack.volume_fingerprint(old)

    done = stack.restore(stack.first_backup)
    assert done.returncode == 0, _out(done)[-3000:]
    assert "restored 1 users, 1 projects, 1 entries" in _out(done), _out(done)

    new = stack.data_volume()
    match = RESTORED_VOLUME.fullmatch(new)
    assert match and match["project"] == stack.project and new != old, new
    assert stack.data_volume(BACKUP) == new, "the backups must follow the restored volume"
    assert f"LOOM_DATA_VOLUME={new}" in stack.env_file.read_text(encoding="utf-8").splitlines()

    health, projects = stack.api([
        {"path": "/api/health"},
        {"path": "/api/projects", "token": stack.token},
    ])
    assert health["status"] == 200 and health["body"]["status"] == "ok", health
    assert projects["status"] == 200, projects
    assert [p["id"] for p in projects["body"]["projects"]] == [stack.project_id], projects
    hits = _search(stack, FIRST)
    assert len(hits) == 1 and FIRST in hits[0]["content_md"], hits
    assert _search(stack, SECOND) == [], "the server must be reading the restored data"

    assert stack.volume_fingerprint(old) == before, "the restore must not touch the old volume"
    assert stack.entries_in_volume(old, SECOND) == 1, "the old volume keeps what it had"


def test_compose_stop_stops_the_backup_service_cleanly(stack):
    stack.up()
    container = stack.container(BACKUP)
    stack.wait_for_writes(1)
    started = time.monotonic()
    stopped = stack.compose("stop", timeout=300)
    elapsed = time.monotonic() - started
    assert stopped.returncode == 0, _out(stopped)
    state = _docker("inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}", container)
    assert state.stdout.decode().split() == ["exited", "0"], (
        f"the backup loop must exit 0 on SIGTERM, not be SIGKILLed (137): {state.stdout}")
    assert elapsed < BACKUP_GRACE_S, f"`docker compose stop` took {elapsed:.1f}s"
    assert STOPPING in _out(stack.compose("logs", "--no-color", BACKUP))
