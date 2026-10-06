"""FROZEN GOAL — Fly.io packaging and deployment (issue #2). Read-only to implementers.

Oracle properties: the REAL Docker boundary (AGENTS.md §10 — "Docker for containerizable
infrastructure"). Every assertion here is a property of the *image and its configuration*,
so the whole wave runs with no Fly resources and no spend: a real ``docker build`` of the
repo's own ``Dockerfile`` against the repo's own context, and real containers run against a
real bind mount standing in for the Fly volume.

It covers four of issue #2's six acceptance criteria end to end:

* **AC1** build reproducibility + FTS5 + a container that boots against an empty mount.
* **AC2** the Bearer contract, *including* ``/docs``, ``/redoc`` and ``/openapi.json`` —
  the assertion that pins the middleware fix and stops it regressing.
* **AC3** the database (and its WAL sidecars) living on the mount and surviving the
  destruction of the container that wrote it.
* **AC5** no secret-shaped file in the build context, in any image layer, or in the
  build history.

AC4 is covered here only where it is a *config* property (the port the Dockerfile serves,
the port ``fly.toml`` publishes and the port the container actually answers on are three
independently-edited values that must agree; ``[mounts]`` is what suppresses Fly's default
two-Machine redundancy). "Exactly one Machine exists" and AC6's bill are properties of a
real Fly app and are discharged by the Fly.io deployment records in ``docs/RUNBOOK.md`` §2.

**Skips are scoped to the test that needs the tool, and under ``LOOM_GATE=1`` a missing
tool is a failure rather than a skip** — the same discipline (and the same reason) as
``client/tests/e2e/test_blockt_e2e.py``: a module-level ``skipif`` turns a missing binary
into a green-looking wave that executed nothing.

Every product string this wave asserts on is duplicated here as a literal next to the name
of the thing it mirrors. A goal that imports the config it grades asserts only that the
config agrees with itself.

Run: .venv/bin/python -m pytest test/waves/m/test_goal_fly_packaging.py -q
"""
from __future__ import annotations

import fnmatch
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
import uuid
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _goalkit as kit  # noqa: E402

REPO_ROOT = kit.REPO_ROOT
DOCKERFILE = REPO_ROOT / "Dockerfile"
FLY_TOML = REPO_ROOT / "fly.toml"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
ENTRYPOINT = REPO_ROOT / "docker-entrypoint.sh"
UV_LOCK = REPO_ROOT / "uv.lock"
GITLEAKS_METADATA_CONFIG = Path(__file__).with_name("gitleaks-image-metadata.toml")

LOOM_GATE = os.environ.get("LOOM_GATE") == "1"

# --------------------------------------------------------------------------- #
# Contract literals (duplicated, never imported — see the module docstring)     #
# --------------------------------------------------------------------------- #
# `fly.toml` [mounts].destination — where the Fly volume is attached.
MOUNT_DESTINATION = "/data"
# `fly.toml` [env].LOOM_DB and the image's ENV LOOM_DB — `server/db.py:db_path()` reads it.
DB_PATH = f"{MOUNT_DESTINATION}/loom.db"
# SQLite writes these two next to the database under WAL (`server/db.py` PRAGMA journal_mode).
WAL_SIDECARS = ("loom.db-wal", "loom.db-shm")
# `server/search.py:_require_fts5` aborts startup without this compile option.
FTS5_COMPILE_OPTION = "ENABLE_FTS5"
# How far fly.toml's `kill_timeout` must sit ABOVE uvicorn's graceful-shutdown window.
MIN_SHUTDOWN_HEADROOM_S = 5
# `docker-entrypoint.sh` exits with this (EX_CONFIG) when LOOM_DB is not an absolute path.
ENTRYPOINT_BAD_CONFIG_EXIT = 78

# Filenames that must never reach an image layer. `*.pem`/`*.key` are deliberately NOT in
# this set: `python:3.12-slim` carries the Debian CA bundle (~150 `.pem` files under
# /etc/ssl/certs), so including them would make this assertion fail on the base image and
# teach the next reader to loosen it. Verified 2026-09-13: the set below matches nothing in
# the base image, so a hit is always something this repo's build put there.
SECRET_SHAPED = (
    "*.db", "*.db-wal", "*.db-shm", ".env", ".env.*", "revisor-token.txt",
    "credentials.json", ".credentials.json", ".netrc", "id_rsa", "id_ed25519",
)
# Trees that exist in the repo and must never reach the image: the client package, the test
# suites. `server/` imports nothing from `loom`, so the image
# carrying any of these means `.dockerignore` stopped working.
FORBIDDEN_CONTEXT_ENTRIES = (".git", "client", "test", "docs",
                             "prompts", "contract", ".venv")


# `useradd --create-home --home-dir /app` copies /etc/skel into /app, so these sit beside
# the venv and the server package. They are named ONE BY ONE rather than exempting "any
# file directly in /app", which is what this check used to do — that exemption would have
# waved through an `app/pyproject.toml` or an `app/anything.py` without a word.
HOME_SKELETON = frozenset({
    "app/.bashrc", "app/.profile", "app/.bash_logout",
})


def _require_tool(name: str, why: str) -> None:
    """Same contract as the Block-T gate's helper: under the gate, absence is a failure."""
    if shutil.which(name) is not None:
        return
    reason = f"the Fly packaging goal needs the real {name} binary: {why}"
    if LOOM_GATE:
        pytest.fail(f"LOOM_GATE=1 forbids skipping — {reason}")
    pytest.skip(reason)


def _run(args: list[str], *, timeout: int = 900,
         cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          cwd=str(cwd) if cwd else None)


def _docker(*args: str, timeout: int = 900) -> subprocess.CompletedProcess:
    return _run(["docker", *args], timeout=timeout)


def _tag(suffix: str) -> str:
    """A tag unique to this process, so concurrent runs never race over an image name."""
    return f"loom-goal-m-{suffix}:{os.getpid()}-{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------- #
# Fixtures — ONE real build and ONE running container for the whole wave        #
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def image() -> str:
    """The REAL image, built from the repo's own Dockerfile and the repo as context."""
    _require_tool("docker", "the image IS the boundary this goal validates (AGENTS.md §10)")
    assert DOCKERFILE.is_file(), f"{DOCKERFILE} must exist — it is what this wave grades"
    tag = _tag("img")
    built = _docker("build", "-t", tag, "-f", str(DOCKERFILE), str(REPO_ROOT), timeout=2400)
    assert built.returncode == 0, (
        f"`docker build` of the repo Dockerfile must succeed.\n"
        f"{built.stdout[-3000:]}\n{built.stderr[-3000:]}"
    )
    yield tag
    _docker("image", "rm", "-f", tag, timeout=300)


@pytest.fixture(scope="session")
def saved_layers(image, tmp_path_factory) -> list[str]:
    """Every member path of every layer of the built image.

    `docker save` + extract, NOT the running container's filesystem: a live container
    cannot show what a discarded builder stage left behind in an earlier layer, and that
    is exactly the leak AC5 is about.
    """
    work = tmp_path_factory.mktemp("layers")
    archive = work / "image.tar"
    saved = _docker("save", image, "-o", str(archive), timeout=900)
    assert saved.returncode == 0, f"`docker save` must succeed: {saved.stderr[-2000:]}"
    extracted = work / "x"
    extracted.mkdir()
    with tarfile.open(archive, "r:*") as tf:
        tf.extractall(extracted, filter="data")

    names: list[str] = []
    layers = 0
    for blob in sorted(extracted.rglob("*")):
        if not blob.is_file():
            continue
        try:
            with tarfile.open(blob, "r:*") as layer:
                members = layer.getnames()
        except (tarfile.ReadError, EOFError):
            continue  # a manifest/config JSON blob, not a layer
        layers += 1
        names.extend(members)
    assert layers > 0, "no layer tar was found in the saved image — the scan proved nothing"
    return names


def _reclaim(mount: Path, image: str) -> None:
    """Give the mount back to this user.

    The entrypoint chowns the mount to the in-container `app` user, which on a bind mount
    is a real chown of a real host directory: without this the files are owned by a uid
    this user cannot unlink and the temp tree leaks for the rest of the session.
    """
    _docker("run", "--rm", "-v", f"{mount}:{MOUNT_DESTINATION}", "--entrypoint", "chown",
            image, "-R", f"{os.getuid()}:{os.getgid()}", MOUNT_DESTINATION, timeout=300)


def _new_mount(tmp_path_factory, name: str) -> Path:
    """A host directory standing in for the Fly volume.

    `0755` is not decoration. pytest creates temp directories `0700`, and the entrypoint
    chowns the mount to the in-container `app` user — which on a bind mount is a real
    chown of this directory, after which THIS process can no longer even list it. The Fly
    volume is not a pytest temp directory; the mode is a property of the harness, and
    widening it is what lets the host-side assertions below run at all. They are the ones
    that matter: reading /data from inside the container would look identical for a
    container started with no mount whatsoever.
    """
    mount = tmp_path_factory.mktemp(name)
    mount.chmod(0o755)
    return mount


def _start(image: str, mount: Path, port: int, internal_port: int) -> str:
    """Start a detached container publishing `internal_port` on loopback `port`."""
    started = _docker(
        "run", "-d", "--name", f"loom-goal-m-{uuid.uuid4().hex[:10]}",
        "-p", f"127.0.0.1:{port}:{internal_port}",
        "-v", f"{mount}:{MOUNT_DESTINATION}", image, timeout=300)
    assert started.returncode == 0, f"`docker run` must succeed: {started.stderr[-2000:]}"
    return started.stdout.strip()


def _wait_health(url: str, container: str, timeout_s: float = 60.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if requests.get(f"{url}/api/health", timeout=2).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.3)
    logs = _docker("logs", container, timeout=120)
    raise AssertionError(
        f"the container never served /api/health on {url}.\n"
        f"--- container logs ---\n{logs.stdout[-3000:]}\n{logs.stderr[-3000:]}"
    )


def _adduser(container: str, name: str = "goal-m") -> str:
    """Create a real user IN the container and return its bearer token.

    `-u app` deliberately: `docker exec` is root by default, and a root-created WAL sidecar
    in the mount is a file the non-root server process then cannot write.
    """
    made = _docker("exec", "-u", "app", container, "python", "-m", "server.admin",
                   "adduser", "--name", name, "--email", f"{name}@example.invalid",
                   timeout=300)
    assert made.returncode == 0, f"`server.admin adduser` must succeed: {made.stderr[-2000:]}"
    return kit.parse_admin_token(made.stdout)


@pytest.fixture(scope="session")
def fly_config() -> dict:
    assert FLY_TOML.is_file(), f"{FLY_TOML} must exist — it is the single-Machine contract"
    return tomllib.loads(FLY_TOML.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def internal_port(fly_config) -> int:
    return int(fly_config["http_service"]["internal_port"])


@pytest.fixture(scope="session")
def live(image, internal_port, tmp_path_factory):
    """One container on an EMPTY mount, plus one real user. Yields (url, token, id, mount).

    The mount starts empty on purpose: reaching `/api/health` at all is AC1's proof that
    the FastAPI `lifespan` applied the schema, FTS5 probe included.
    """
    mount = _new_mount(tmp_path_factory, "flyvol")
    port = kit.find_free_port()
    url = f"http://127.0.0.1:{port}"
    container = _start(image, mount, port, internal_port)
    try:
        _wait_health(url, container)
        token = _adduser(container)
        yield url, token, container, mount
    finally:
        _docker("rm", "-f", container, timeout=300)
        _reclaim(mount, image)


# --------------------------------------------------------------------------- #
# AC1 — reproducible build, FTS5, and a container that boots on an empty mount  #
# --------------------------------------------------------------------------- #
def test_ac1_both_stages_pin_the_base_image_by_digest():
    """"Reproducible" is a property of the inputs, not of a diff between two builds.

    Two builds of a digest-pinned base with a locked resolver are reproducible BY
    CONSTRUCTION; comparing two builds would test that premise rather than the artifact,
    and would need an invented comparison mechanism. So the assertion is on the two inputs
    that make it true: a digest-pinned base (a floating `:3.12-slim` tag would make
    "builds reproducibly" untrue on the day Debian rebuilds the image) and, below, a
    lockfile the build refuses to run against when stale.
    """
    froms = re.findall(r"^FROM\s+(\S+)", DOCKERFILE.read_text(encoding="utf-8"),
                       flags=re.MULTILINE)
    base = [ref for ref in froms if not ref.startswith("scratch")]
    assert base, "the Dockerfile must declare at least one FROM"
    python_bases = [ref for ref in base if ref.startswith("python:")]
    assert len(python_bases) >= 2, (
        f"both stages must start FROM the python base image, got {base!r}")
    for ref in python_bases:
        assert "@sha256:" in ref, (
            f"the base image must be pinned by digest, not by the floating tag {ref!r}")
    assert len(set(python_bases)) == 1, (
        f"both stages must pin the SAME digest, got {sorted(set(python_bases))!r}")


def test_ac1_lockfile_is_committed_and_current():
    """`uv.lock` exists, is tracked by git, and still agrees with `pyproject.toml`."""
    _require_tool("uv", "the lockfile is what makes the build reproducible")
    assert UV_LOCK.is_file(), "uv.lock must exist — pyproject.toml pins no version itself"
    tracked = _run(["git", "ls-files", "--error-unmatch", "uv.lock"], cwd=REPO_ROOT,
                   timeout=120)
    assert tracked.returncode == 0, "uv.lock must be COMMITTED, not merely present locally"
    check = _run(["uv", "lock", "--check"], cwd=REPO_ROOT, timeout=600)
    assert check.returncode == 0, (
        f"uv.lock is stale against pyproject.toml — the image would fail to build.\n"
        f"{check.stdout[-1500:]}\n{check.stderr[-1500:]}")


def test_ac1_a_stale_lockfile_fails_the_build(tmp_path):
    """The sharp edge of `uv sync --locked` over `--frozen`, exercised for real.

    This is the guard that keeps the previous test honest: without it, "the lockfile is
    current" is a claim about a file nobody enforces. Here a deliberately stale lockfile
    must make a REAL `docker build` fail, and fail *for that reason*.
    """
    _require_tool("docker", "the stale-lock rejection is a property of the build")
    context = tmp_path / "stale"
    context.mkdir()
    for name in ("Dockerfile", "uv.lock", "docker-entrypoint.sh"):
        shutil.copy2(REPO_ROOT / name, context / name)
    shutil.copytree(REPO_ROOT / "server", context / "server")
    # Add a dependency to the `server` extra and NOT to the lockfile.
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    stale = pyproject.replace('server = ["fastapi", "uvicorn", "jinja2", "markdown-it-py"]',
                              'server = ["fastapi", "uvicorn", "jinja2", "markdown-it-py", "httpx"]')
    assert stale != pyproject, (
        "the `server` extra no longer reads as this test expects — update the mutation so "
        "this test still makes the lockfile stale instead of silently building a valid one")
    (context / "pyproject.toml").write_text(stale, encoding="utf-8")

    tag = _tag("stale")
    built = _docker("build", "-t", tag, str(context), timeout=1800)
    _docker("image", "rm", "-f", tag, timeout=300)
    assert built.returncode != 0, (
        "a build whose uv.lock is stale against pyproject.toml MUST fail — that is what "
        "`uv sync --locked` buys over `--frozen`")
    output = (built.stdout + built.stderr).lower()
    assert "lock" in output, (
        f"the build failed, but not visibly because of the lockfile — this test would pass "
        f"on any unrelated breakage.\n{built.stdout[-2000:]}\n{built.stderr[-2000:]}")


def test_ac1_image_sqlite_provides_fts5(image):
    """`server/search.py:_require_fts5` ABORTS startup without it — a crash-loop, not a
    degrade, and under `auto_start_machines` a crash-loop is also a cost problem."""
    out = _docker("run", "--rm", image, "python", "-c",
                  "import sqlite3;print([r[0] for r in "
                  "sqlite3.connect(':memory:').execute('pragma compile_options')])",
                  timeout=300)
    assert out.returncode == 0, f"the image must run python: {out.stderr[-2000:]}"
    assert FTS5_COMPILE_OPTION in out.stdout, (
        f"the image's sqlite3 must report {FTS5_COMPILE_OPTION}: {out.stdout[-1000:]}")


def test_ac1_container_on_an_empty_mount_applies_the_schema_and_serves_health(live):
    """Health 200 on a mount that started EMPTY proves the lifespan schema init ran."""
    url, _token, _container, _mount = live
    response = requests.get(f"{url}/api/health", timeout=10)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"


# --------------------------------------------------------------------------- #
# AC2 — the Bearer contract, including the three FastAPI doc routes             #
# --------------------------------------------------------------------------- #
def test_ac2_health_is_reachable_without_any_header(live):
    url, _token, _container, _mount = live
    assert requests.get(f"{url}/api/health", timeout=10).status_code == 200


def test_ac2_a_protected_route_rejects_missing_and_bad_tokens_and_accepts_a_valid_one(live):
    """All four legs together, because a deployment can satisfy the negative ones while
    being completely broken: `apply_schema` only runs when `users` is absent, so a fresh
    unmigrated volume boots with zero users and 401s EVERYTHING — valid historical tokens
    included. "Rejects bad tokens" is satisfied by that broken deployment; the 200 is what
    distinguishes it from a working one.
    """
    url, token, _container, _mount = live
    assert requests.get(f"{url}/api/projects", timeout=10).status_code == 401
    assert requests.get(f"{url}/api/projects", timeout=10,
                        headers={"Authorization": "Bearer garbage"}).status_code == 401
    assert requests.get(f"{url}/api/projects", timeout=10,
                        headers={"Authorization": token}).status_code == 401, \
        "a token without the `Bearer ` scheme is a malformed header, not a credential"
    ok = requests.get(f"{url}/api/projects", timeout=10,
                      headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200, f"a VALID token must be accepted: {ok.text}"


@pytest.mark.parametrize("route", ["/docs", "/redoc", "/openapi.json"])
def test_ac2_the_fastapi_doc_routes_require_a_token(live, route):
    """The assertion that pins the middleware fix and stops it regressing.

    FastAPI serves these three by default and they are NOT shaped like `/api/*`, so a
    middleware that exempts "everything not under /api/" publishes the entire API schema —
    every route, field, validator and vocabulary — plus a working "Try it out" console, to
    any anonymous visitor. On loopback that was unreachable; on `*.fly.dev` it is a free
    recon surface. The fix is an allow-list (`path == "/api/health"`), not nulling the
    three doc URLs, so the NEXT non-`/api/` route anyone adds is protected by default.
    """
    url, token, _container, _mount = live
    assert requests.get(f"{url}{route}", timeout=10).status_code == 401, (
        f"{route} must not be reachable anonymously")
    with_token = requests.get(f"{url}{route}", timeout=10,
                              headers={"Authorization": f"Bearer {token}"})
    assert with_token.status_code == 200, (
        f"{route} must still work for an authenticated caller — the fix is an auth "
        f"boundary, not a deletion of the docs: {with_token.status_code}")


# --------------------------------------------------------------------------- #
# AC3 — the database lives on the mount and outlives the container              #
# --------------------------------------------------------------------------- #
def _register_and_write(url: str, token: str, remote: str, content: str) -> int:
    auth = {"Authorization": f"Bearer {token}"}
    created = requests.post(f"{url}/api/projects", timeout=15, headers=auth,
                            json={"remote_url": remote})
    assert created.status_code == 201, created.text
    entry = requests.post(f"{url}/api/entries", timeout=15, headers=auth, json={
        "project_remote": remote, "session_id": "goal-m-session",
        "content_md": content, "content_hash": f"hash-{uuid.uuid4().hex}",
    })
    assert entry.status_code == 201, entry.text
    return created.json()["id"]


def test_ac3_an_entry_survives_destroying_and_recreating_the_container(
        image, internal_port, tmp_path_factory):
    """`docker rm -f` and a NEW container on the SAME mount — never a restart.

    A restart proves nothing about the mount. If `LOOM_DB` were left at its CWD-relative
    default (`./loom.db`, `server/db.py:26`), the app would still boot, still answer
    health, and the entry would still "survive" a restart and an in-place redeploy — on
    the container's own ephemeral disk, having never touched the volume. Destroying the
    container is what tells those two worlds apart, and it is the local stand-in for
    AC3's real evidence: destroying and recreating the Fly Machine.
    """
    mount = _new_mount(tmp_path_factory, "flyvol-persist")
    remote = "git@github.com:example-group/fly-persistence.git"
    content = f"# persisted across a container destroy {uuid.uuid4().hex}"

    # ONE try/finally around both containers. Reclaiming only in the second container's
    # finally would leak the mount on exactly the path that matters: if the FIRST container
    # fails to boot — which is what happens when the image is genuinely broken, the case
    # this test exists to catch — the entrypoint has already chowned the mount to the
    # in-container uid, and the exception would escape leaving a temp directory this user
    # can neither list nor delete.
    try:
        first_port = kit.find_free_port()
        first_url = f"http://127.0.0.1:{first_port}"
        first = _start(image, mount, first_port, internal_port)
        try:
            _wait_health(first_url, first)
            token = _adduser(first, name="goal-m-persist")
            project_id = _register_and_write(first_url, token, remote, content)
        finally:
            destroyed = _docker("rm", "-f", first, timeout=300)
            assert destroyed.returncode == 0, destroyed.stderr

        second_port = kit.find_free_port()
        second_url = f"http://127.0.0.1:{second_port}"
        second = _start(image, mount, second_port, internal_port)
        try:
            _wait_health(second_url, second)
            listed = requests.get(f"{second_url}/api/projects/{project_id}/entries",
                                  timeout=15, headers={"Authorization": f"Bearer {token}"})
            assert listed.status_code == 200, (
                f"the token created against the destroyed container must still work — a "
                f"new empty database would 401 here: {listed.text}")
            bodies = [entry["content_md"] for entry in listed.json()["entries"]]
            assert content in bodies, (
                f"the entry written before the destroy must be readable after it: {bodies}")
        finally:
            _docker("rm", "-f", second, timeout=300)
    finally:
        _reclaim(mount, image)


def test_ac3_the_database_and_its_wal_sidecars_are_inside_the_mount_and_nowhere_else(live):
    """Three claims that together rule out "it works but not on the volume".

    `LOOM_DB` is a plain env var, not a secret, and is the single easiest thing in this
    whole change to leave unset — so it is asserted directly, from inside the container,
    rather than inferred from the fact that something worked.
    """
    url, token, container, mount = live
    _register_and_write(url, token, "git@github.com:example-group/fly-sidecars.git",
                        "# forces a WAL write")

    resolved = _docker("exec", container, "python", "-c",
                       "import os;print(os.environ.get('LOOM_DB', '<unset>'))", timeout=120)
    assert resolved.returncode == 0, resolved.stderr
    assert resolved.stdout.strip() == DB_PATH, (
        f"the container's LOOM_DB must resolve under the mount, got "
        f"{resolved.stdout.strip()!r} (expected {DB_PATH!r})")

    names = sorted(p.name for p in mount.iterdir())
    assert "loom.db" in names, f"the database must be ON the mount, found {names}"

    # The sidecars are NOT asserted by listing the directory now, and the reason is worth
    # recording: `server/db.py` opens one connection per operation and closes it, and
    # SQLite checkpoints and DELETES `-wal`/`-shm` when the last connection to a WAL
    # database closes cleanly. Their ABSENCE at rest is the healthy steady state, so a
    # naive `assert "loom.db-wal" in names` fails against a perfectly correct deployment.
    # What is actually worth proving is where they appear when they do exist — so hold a
    # real uncommitted write transaction open inside the container and look while it lives.
    # The transaction is never committed and dies with the process, so nothing persists.
    # `-i` and a blocking `sys.stdin.read()` rather than a `sleep`: closing the pipe is
    # what ends the transaction. `holder.kill()` would kill the local `docker exec` CLIENT,
    # not the process inside the container — `docker exec` without a TTY does not propagate
    # a client-side signal — so a sleeping holder would run to completion regardless and
    # the release below would be a coincidence of timing rather than a thing this test did.
    holder = subprocess.Popen(
        ["docker", "exec", "-i", "-u", "app", container, "python", "-c",
         "import sys, sqlite3\n"
         "conn = sqlite3.connect('/data/loom.db')\n"
         "conn.execute('PRAGMA journal_mode=WAL')\n"
         "conn.execute('BEGIN IMMEDIATE')\n"
         "conn.execute('CREATE TABLE goal_m_wal_probe (x)')\n"
         "print('held', flush=True)\n"
         "sys.stdin.read()\n"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.time() + 60
        seen: set[str] = set()
        while time.time() < deadline:
            seen = {entry.name for entry in mount.iterdir()}
            if set(WAL_SIDECARS) <= seen:
                break
            time.sleep(0.2)
        assert set(WAL_SIDECARS) <= seen, (
            f"while a write transaction is open, {WAL_SIDECARS} must appear beside the "
            f"database ON the mount — a sidecar written anywhere else is a database split "
            f"in two, with half of it on a disk that dies with the Machine: {sorted(seen)}")
    finally:
        if holder.stdin is not None:
            holder.stdin.close()          # EOF -> the holder exits -> transaction rolled back
        holder.wait(timeout=120)

    # And they are cleaned up again once that connection is gone. This is ASSERTED, not
    # merely waited for: the `find` below can only look OUTSIDE the mount, so without this
    # a broken WAL checkpoint that left the sidecars behind forever would pass the whole
    # test. The loop distinguishes "they went away" from "we ran out of patience".
    settled = time.time() + 60
    while time.time() < settled and any(
            (mount / sidecar).exists() for sidecar in WAL_SIDECARS):
        time.sleep(0.2)
    left_behind = sorted(s for s in WAL_SIDECARS if (mount / s).exists())
    assert not left_behind, (
        f"SQLite must checkpoint and remove the WAL sidecars when the last connection to "
        f"the database closes; these are still on the mount: {left_behind}")

    stray = _docker("exec", container, "sh", "-c",
                    f"find / -xdev -name 'loom.db*' -not -path '{MOUNT_DESTINATION}/*' "
                    f"2>/dev/null | head -20", timeout=300)
    assert stray.returncode == 0, stray.stderr
    assert stray.stdout.strip() == "", (
        f"no database file may exist outside the mount — these are on the container's own "
        f"ephemeral disk and would be lost with it:\n{stray.stdout}")


# --------------------------------------------------------------------------- #
# AC4 — the config values that must agree, and the one-Machine contract         #
# --------------------------------------------------------------------------- #
def _cmd_flag(flag: str) -> str:
    """Read a `--flag`'s value out of the Dockerfile's exec-form CMD."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    found = re.search(rf'"{re.escape(flag)}"\s*,\s*\\?\s*"([^"]+)"', text)
    assert found, f"the Dockerfile CMD must pass {flag}: {text}"
    return found.group(1)


def test_ac4_the_dockerfile_port_the_fly_port_and_the_served_port_all_agree(
        live, internal_port):
    """Three independently-edited values. The third one is not a re-read of a file: the
    container is published on the port `fly.toml` names, so if the image served anywhere
    else nothing would answer here at all.
    """
    url, _token, _container, _mount = live
    assert int(_cmd_flag("--port")) == internal_port, (
        f"the Dockerfile serves --port {_cmd_flag('--port')} but fly.toml publishes "
        f"internal_port {internal_port}")
    assert requests.get(f"{url}/api/health", timeout=10).status_code == 200, (
        f"the container must actually answer on the port fly.toml names ({internal_port})")
    assert _cmd_flag("--host") == "0.0.0.0", (
        "the container must bind all interfaces — a loopback bind is unreachable from the "
        "Fly proxy, which is the whole point of this issue")


def test_ac4_fly_kill_timeout_covers_the_graceful_shutdown_window(fly_config):
    """If Fly SIGKILLs before uvicorn finishes, the exec-form CMD bought nothing."""
    graceful = int(_cmd_flag("--timeout-graceful-shutdown"))
    raw = fly_config.get("kill_timeout")
    assert raw is not None, "fly.toml must set kill_timeout explicitly"
    seconds = int(raw) if isinstance(raw, int) else int(str(raw).rstrip("s"))
    assert seconds >= graceful + MIN_SHUTDOWN_HEADROOM_S, (
        f"fly.toml kill_timeout ({seconds}s) must exceed the uvicorn graceful-shutdown "
        f"window ({graceful}s) by at least {MIN_SHUTDOWN_HEADROOM_S}s. EQUAL IS NOT SAFE: "
        f"Fly SIGKILLs kill_timeout after SIGTERM, so two identical deadlines land at the "
        f"same instant and the process is killed exactly as it was about to exit cleanly, "
        f"which defeats the exec-form CMD entirely.")


def test_ac4_fly_config_pins_one_machine_backed_by_a_volume(fly_config):
    """`[mounts]` is what suppresses Fly's default two-Machine redundancy, and a volume
    attaches to one Machine at a time — so the mount IS the single-Machine contract, and
    it belongs in config rather than in a human's memory.
    """
    mounts = fly_config.get("mounts")
    assert mounts, "fly.toml must declare [mounts] — it is the single-Machine contract"
    assert mounts["destination"] == MOUNT_DESTINATION, mounts
    service = fly_config["http_service"]
    assert service["force_https"] is True, "AC2 requires HTTPS on *.fly.dev"
    assert service["min_machines_running"] == 0, (
        "cold start is the accepted owner decision: between sessions only the volume bills")
    assert service["auto_stop_machines"] in ("off", "stop", "suspend"), (
        f"auto_stop_machines is a three-way string enum in current Fly config, not the "
        f"boolean older examples use: {service['auto_stop_machines']!r}")


def test_ac4_loom_db_agrees_across_fly_config_the_image_and_the_mount(fly_config, image):
    """The env var, the volume destination and the image default are one decision written
    in three files; `server/db.py` silently falls back to `./loom.db` if they disagree."""
    assert fly_config["env"]["LOOM_DB"] == DB_PATH, fly_config["env"]
    assert posixpath.dirname(DB_PATH) == fly_config["mounts"]["destination"]
    inspected = _docker("image", "inspect", image, "--format", "{{json .Config.Env}}",
                        timeout=300)
    assert inspected.returncode == 0, inspected.stderr
    env = dict(item.split("=", 1) for item in json.loads(inspected.stdout))
    assert env.get("LOOM_DB") == DB_PATH, (
        f"the image's own default must match fly.toml, so a `docker run` without an "
        f"explicit LOOM_DB cannot quietly write to the container disk: {env.get('LOOM_DB')!r}")


# --------------------------------------------------------------------------- #
# AC5 — no secret reaches the context, a layer, or the build history            #
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def build_context(tmp_path_factory) -> Path:
    """The EFFECTIVE build context — what `.dockerignore` actually lets through.

    Exported by building `FROM scratch` + `COPY . /ctx/` against the real repo with the
    real `.dockerignore`, which is the only way to see the filter's true output. Reading
    `.dockerignore` and believing it would assert that a file says what it says.
    """
    _require_tool("docker", "the effective build context is what the daemon receives")
    assert DOCKERIGNORE.is_file(), ".dockerignore must exist — AC5's first line of defence"
    work = tmp_path_factory.mktemp("context")
    probe = work / "probe.Dockerfile"
    probe.write_text("FROM scratch\nCOPY . /ctx/\n", encoding="utf-8")
    out = work / "out"
    exported = _docker("build", "-f", str(probe), "--output",
                       f"type=local,dest={out}", str(REPO_ROOT), timeout=1800)
    assert exported.returncode == 0, (
        f"the context probe build must succeed: {exported.stderr[-2000:]}")
    root = out / "ctx"
    assert root.is_dir(), "the probe exported no context at all"
    return root


def test_ac5_the_build_context_excludes_secrets_and_every_tree_the_server_does_not_need(
        build_context):
    relative = [str(p.relative_to(build_context)) for p in build_context.rglob("*")]
    for entry in FORBIDDEN_CONTEXT_ENTRIES:
        offenders = [r for r in relative if r == entry or r.startswith(f"{entry}/")]
        assert not offenders, (
            f"`{entry}` must never enter the build context: {offenders[:10]}")
    for pattern in SECRET_SHAPED:
        hits = [r for r in relative if fnmatch.fnmatch(posixpath.basename(r), pattern)]
        assert not hits, f"a {pattern} file reached the build context: {hits[:10]}"
    assert any(r.startswith("server/") for r in relative), (
        "the probe must have exported something — an empty context would pass every "
        "assertion above while proving nothing")


def test_ac5_no_secret_shaped_file_reached_any_image_layer(saved_layers):
    for pattern in SECRET_SHAPED:
        hits = [n for n in saved_layers
                if fnmatch.fnmatch(posixpath.basename(n), pattern)]
        assert not hits, f"a {pattern} file is baked into an image layer: {hits[:10]}"


def test_ac5_the_image_carries_only_the_server_package_and_its_virtualenv(saved_layers):
    """The positive half of AC5, and the check that keeps `.dockerignore` honest: what IS
    under /app, not only what is not. `server/` imports nothing from `loom`, so the client
    has no business in this image — and neither do the test suites."""
    under_app = [n.lstrip("./") for n in saved_layers
                 if n.lstrip("./").startswith("app/") and n.lstrip("./") != "app/"]
    assert any(n == "app/server/app.py" for n in under_app), (
        "the image must actually contain the server package — without this the assertion "
        "below is satisfied by an empty /app")
    allowed_prefixes = ("app/.venv/", "app/server/")
    allowed_exact = {"app/.venv", "app/server"}   # the directory entries themselves
    strays = [n for n in under_app
              if not n.startswith(allowed_prefixes)
              and n not in allowed_exact and n not in HOME_SKELETON]
    assert not strays, (
        f"only the virtualenv and the server package may live under /app: {strays[:20]}")
    tests_in_image = [n for n in under_app if n.startswith("app/server/tests")]
    assert not tests_in_image, (
        f"the server's own test suite must not ship in the runtime image: "
        f"{tests_in_image[:10]}")


def test_ac5_the_build_history_exposes_no_credential(image):
    """`docker history --no-trunc` is public to anyone who can pull the image.

    Scoped to CREDENTIALS, not to filenames. `ENV LOOM_DB=/data/loom.db` is in this output
    and belongs there — it is a path, and it is the whole point of the image having a
    default. A `*.db` FILE reaching the build is caught by the layer scan above, which is
    strictly better evidence than grepping the commands that built it.
    """
    history = _docker("history", "--no-trunc", image, timeout=300)
    assert history.returncode == 0, history.stderr
    text = history.stdout.lower()
    # Deliberately NOT a bare "key": the base image carries `GPG_KEY=7169...` in its own
    # ENV, and a pattern that fails on the base image teaches the next reader to loosen it.
    for pattern in ("revisor-token", "token=", "loom_token", "--token", "bearer ",
                    "token_hash", "password", "passwd", "secret=", "secret_key",
                    "api_key", "apikey", "private_key", "sk_live", "sk_test"):
        assert pattern not in text, (
            f"{pattern!r} appears in the image's build history: {history.stdout[:2000]}")


def _base_image_ref() -> str:
    """The digest-pinned base this Dockerfile builds FROM (both stages pin the same one)."""
    froms = re.findall(r"^FROM\s+(\S+)", DOCKERFILE.read_text(encoding="utf-8"),
                       flags=re.MULTILINE)
    bases = {ref for ref in froms if ref.startswith("python:")}
    assert len(bases) == 1, f"expected one pinned base image, got {sorted(bases)}"
    return bases.pop()


def _base_layer_count() -> int:
    """How many layers the pinned base contributes, pulling it if it is not local.

    The pull is not belt-and-braces, it is required. `docker build` resolves the base
    through BuildKit's own cache and does NOT necessarily leave it in the image store
    under the `python:3.12-slim@sha256:...` reference, so `docker image inspect` answers
    "No such image" on a machine that has only ever built — which is every CI runner, and
    which is exactly how this was found. A developer who happens to have pulled the tag by
    hand sees it work locally and learns nothing.

    Pulling is safe to do unconditionally-on-failure: the reference is digest-pinned, so
    there is no version to drift and nothing to decide.
    """
    ref = _base_image_ref()
    counted = _docker("image", "inspect", ref, "--format", "{{len .RootFS.Layers}}",
                      timeout=300)
    if counted.returncode != 0:
        pulled = _docker("pull", "--quiet", ref, timeout=1800)
        assert pulled.returncode == 0, (
            f"the pinned base image must be obtainable to subtract its layers: "
            f"{pulled.stdout[-1000:]} {pulled.stderr[-1000:]}")
        counted = _docker("image", "inspect", ref, "--format", "{{len .RootFS.Layers}}",
                          timeout=300)
    assert counted.returncode == 0, (
        f"could not read the base image's layer count: {counted.stderr[-1000:]}")
    return int(counted.stdout.strip())


def _resolve_manifest(index: dict, blob) -> dict | None:
    """Walk OCI index indirection down to the real platform manifest.

    `docker save` writes an index pointing at an index, and the inner one lists the amd64
    manifest ALONGSIDE an attestation manifest whose platform is literally "unknown". Only
    the real platform manifest carries `layers`.
    """
    if "layers" in index:
        return index
    for entry in index.get("manifests", []):
        if entry.get("platform", {}).get("architecture") == "unknown":
            continue
        found = _resolve_manifest(blob(entry["digest"]), blob)
        if found:
            return found
    return None


@pytest.fixture(scope="session")
def our_layer_contents(image, tmp_path_factory) -> Path:
    """The CONTENTS of the layers this Dockerfile adds, extracted to a directory.

    The base image's layers are excluded by position: `RootFS.Layers` of a derived image
    is the base's list plus ours, in order, so `layers[len(base):]` is exactly what this
    build contributed. That is what makes a content scan affordable at all — it drops
    ~130MB of Debian and Python that no change here can affect, and with it every
    false positive that would come from scanning the distribution's own fixtures.

    Non-regular members are skipped: symlinks (`app/.venv/bin/python` points at an
    absolute path, which `filter="data"` refuses outright), hardlinks, devices and
    whiteouts. None of them can carry secret content — only regular files can.
    """
    _require_tool("docker", "the layers are the artifact being scanned")
    work = tmp_path_factory.mktemp("ourlayers")
    archive = work / "image.tar"
    saved = _docker("save", image, "-o", str(archive), timeout=900)
    assert saved.returncode == 0, f"`docker save` must succeed: {saved.stderr[-2000:]}"
    extracted = work / "x"
    extracted.mkdir()
    with tarfile.open(archive, "r:*") as tf:
        tf.extractall(extracted, filter="data")

    def blob(digest: str) -> dict:
        return json.loads(
            (extracted / "blobs" / "sha256" / digest.split(":", 1)[1]).read_text())

    manifest = _resolve_manifest(
        json.loads((extracted / "index.json").read_text()), blob)
    assert manifest, "could not resolve a platform manifest out of the saved image"
    layers = [entry["digest"] for entry in manifest["layers"]]

    base_count = _base_layer_count()
    ours = layers[base_count:]
    assert ours, (
        f"this build must add at least one layer over the base ({base_count} of "
        f"{len(layers)}) — otherwise this scan would cover nothing")

    out = work / "content"
    out.mkdir()
    for digest in ours:
        with tarfile.open(extracted / "blobs" / "sha256" / digest.split(":", 1)[1],
                          "r:*") as layer:
            for member in layer:
                if not member.isreg():
                    continue
                try:
                    layer.extract(member, out, filter="data")
                except (tarfile.TarError, OSError):
                    continue
    return out


def test_ac5_no_credential_in_the_contents_of_the_layers_this_build_adds(
        our_layer_contents, tmp_path):
    """gitleaks over what this build actually PUT in the image, not just its filenames.

    This is the leg that closes the `RUN curl ... > /etc/thing` shape: a secret fetched
    during the build appears in history as a command, never as a value, so the metadata
    scan cannot see it — but the file it wrote lands in one of these layers.

    Affordable because the base image's layers are subtracted: ~18MB and under a second,
    against ~150MB and a permanent false-positive source if Debian and CPython were in
    scope too.
    """
    _require_tool("gitleaks", "AC5 needs a real scanner over the layer contents")
    assert any(p.is_file() for p in our_layer_contents.rglob("*")), (
        "no regular file was extracted — this scan would pass over nothing")
    scan = _run(["gitleaks", "dir", str(our_layer_contents), "--no-banner", "--redact"],
                timeout=1800)
    assert scan.returncode == 0, (
        f"gitleaks found a credential inside a layer this build adds:\n"
        f"{scan.stdout[-3000:]}\n{scan.stderr[-2000:]}")


def test_ac5_the_layer_content_scan_would_catch_a_build_time_fetched_secret(tmp_path):
    """The bite-check for the leg above, on a copy rather than on the real image.

    A scan that covers real files but could never fail is worse than no scan: it carries
    AC5's name and proves nothing. This plants the exact shape the metadata scan is blind
    to — a credential written into a file during the build, under a name matching none of
    SECRET_SHAPED — and asserts gitleaks flags it.
    """
    _require_tool("gitleaks", "the bite-check needs the same scanner as the real leg")
    planted = tmp_path / "layer-content"
    (planted / "etc").mkdir(parents=True)
    (planted / "etc" / "runtime.conf").write_text(
        "index_token = sk_live_51HxKjQ2eZvKYlo2CabcdEFGH1234ijklMNOP\n", encoding="utf-8")
    scan = _run(["gitleaks", "dir", str(planted), "--no-banner", "--redact"], timeout=900)
    assert scan.returncode != 0, (
        "a credential written into a layer during the build MUST be caught — if this "
        "passes, the leg above is scenery")


def test_ac5_no_credential_appears_in_the_image_metadata(image, tmp_path):
    """gitleaks over the image's own history and config — not only over the context.

    The context scan CANNOT see this class of leak, structurally. A `--build-arg` value
    never touches a tracked file: it exists only at build-invocation time and in the
    resulting image. Yet `docker history --no-trunc` carries the `ARG` line *and* any `RUN`
    that referenced it, in plaintext, to anyone who can pull the image.

    Demonstrated during this change's review rather than imagined: a secret injected as
    `ARG INJECTED_SECRET=sk_live_...` plus `RUN echo "$INJECTED_SECRET" > /opt/runtime.conf`
    sat in the clear in `docker history` and in a real layer, while passing every
    filename-glob and fixed-keyword check in this wave. The next contributor to add an
    `ARG PRIVATE_INDEX_TOKEN` for a dependency — an entirely ordinary Docker pattern —
    would have shipped a live credential with a green wave.

    A secret FETCHED during the build (`RUN curl ... > /etc/thing`) is invisible HERE —
    history records the command, never the value — and is covered instead by the layer
    CONTENT scan above, which subtracts the base image's layers and so costs ~18MB and
    under a second rather than the ~150MB a naive full-image scan would.

    What AC5 covers, exactly, so nobody reads it as wider: the build context, the layer
    file NAMES, the positive space under /app, the contents of the layers this build adds,
    and this metadata. What it still does not cover: a secret that never lands in a
    regular file in one of our layers and never appears in a build command — for example
    one written and deleted within a single RUN, or embedded in a compiled binary.
    """
    _require_tool("gitleaks", "AC5 needs a real secret scanner over the image's metadata")
    history = _docker("history", "--no-trunc", image, timeout=300)
    assert history.returncode == 0, history.stderr
    config = _docker("image", "inspect", image, "--format", "{{json .Config}}", timeout=300)
    assert config.returncode == 0, config.stderr

    metadata = tmp_path / "image-metadata"
    metadata.mkdir()
    (metadata / "history.txt").write_text(history.stdout, encoding="utf-8")
    (metadata / "config.json").write_text(config.stdout, encoding="utf-8")

    scan = _run(["gitleaks", "dir", str(metadata), "--no-banner", "--redact",
                 "--config", str(GITLEAKS_METADATA_CONFIG)], timeout=900)
    assert scan.returncode == 0, (
        f"gitleaks found a credential in the image's build history or config — this is "
        f"readable by anyone who can pull the image:\n{scan.stdout[-3000:]}\n"
        f"{scan.stderr[-2000:]}")

    # The scan must still BITE. An allowlist and a `--config` flag are two easy ways to
    # turn a secret scanner into scenery, and a metadata scan that cannot fail would be
    # worse than no metadata scan at all — it would carry AC5's name while proving nothing.
    (metadata / "history.txt").write_text(
        history.stdout + '\nARG PROBE_TOKEN=sk_live_51HxKjQ2eZvKYlo2CabcdEFGH1234ijklMNOP\n',
        encoding="utf-8")
    planted = _run(["gitleaks", "dir", str(metadata), "--no-banner", "--redact",
                    "--config", str(GITLEAKS_METADATA_CONFIG)], timeout=900)
    assert planted.returncode != 0, (
        "a planted build-arg credential must be CAUGHT by this scan — if it is not, the "
        "config or the allowlist has neutered it and AC5's metadata leg proves nothing")


def test_the_entrypoint_refuses_a_loom_db_that_is_not_an_absolute_path(
        image, tmp_path_factory):
    """A misconfigured LOOM_DB must STOP the container, not silently chown the wrong tree.

    `docker-entrypoint.sh` derives its recursive chown target with `dirname "$LOOM_DB"`
    while running as root. A bare `LOOM_DB=loom.db` — one mistyped `fly secrets set` is
    enough — makes that target `.`, i.e. /app: the entrypoint would re-own the virtualenv
    and the server code instead of the volume, and the app would then write its database
    to the ephemeral disk while looking like a perfectly clean boot. That is the worst
    available failure for this particular mistake, so it is refused loudly instead.
    """
    mount = _new_mount(tmp_path_factory, "flyvol-badconfig")
    result = _docker("run", "--rm", "-e", "LOOM_DB=loom.db",
                     "-v", f"{mount}:{MOUNT_DESTINATION}", image, timeout=300)
    assert result.returncode == ENTRYPOINT_BAD_CONFIG_EXIT, (
        f"a relative LOOM_DB must abort startup with EX_CONFIG "
        f"({ENTRYPOINT_BAD_CONFIG_EXIT}), got {result.returncode}\n"
        f"{result.stdout[-1000:]}\n{result.stderr[-1000:]}")
    assert "absolute path" in (result.stdout + result.stderr), (
        f"the refusal must say what is wrong: {result.stdout!r} {result.stderr!r}")


def test_ac5_gitleaks_is_clean_over_the_build_context(build_context):
    _require_tool("gitleaks", "AC5 requires a secret scan over what the image is built from")
    scan = _run(["gitleaks", "dir", str(build_context), "--no-banner", "--redact"],
                timeout=900)
    assert scan.returncode == 0, (
        f"gitleaks found secrets in the build context:\n{scan.stdout[-3000:]}\n"
        f"{scan.stderr[-2000:]}")


# --------------------------------------------------------------------------- #
# Runtime shape — the two Dockerfile details the plan calls load-bearing        #
# --------------------------------------------------------------------------- #
def test_the_server_process_does_not_run_as_root(live):
    """Fly creates a volume as root:root, so the entrypoint must run as root only long
    enough to hand the mount over and then drop. Reading /proc/1 rather than trusting the
    entrypoint's source: this asserts what PID 1 actually IS."""
    _url, _token, container, _mount = live
    probe = _docker("exec", container, "python", "-c",
                    "import os, pwd; uid = os.stat('/proc/1').st_uid; "
                    "print(uid, pwd.getpwuid(uid).pw_name)", timeout=120)
    assert probe.returncode == 0, probe.stderr
    uid, name = probe.stdout.split()
    assert int(uid) != 0, "PID 1 must not be root once the mount has been handed over"
    assert name == "app", f"PID 1 should be the `app` user, got {name!r} (uid {uid})"


def test_sigterm_reaches_uvicorn_instead_of_being_swallowed_by_a_shell(
        image, internal_port, tmp_path_factory):
    """The exec-form CMD, measured rather than read.

    Shell-form CMD puts `/bin/sh -c` at PID 1, which does not forward SIGTERM: uvicorn
    would be hard-killed instead of running its graceful shutdown and the FastAPI lifespan
    exit. Under `auto_stop_machines` that happens on EVERY idle cycle, not rarely. A
    swallowed signal shows up here as the full `docker stop -t 20` window elapsing and an
    exit code of 137 (SIGKILL); a delivered one exits 0 almost immediately.
    """
    mount = _new_mount(tmp_path_factory, "flyvol-sigterm")
    port = kit.find_free_port()
    container = _start(image, mount, port, internal_port)
    try:
        _wait_health(f"http://127.0.0.1:{port}", container)
        started = time.monotonic()
        stopped = _docker("stop", "-t", "20", container, timeout=300)
        elapsed = time.monotonic() - started
        assert stopped.returncode == 0, stopped.stderr
        code = _docker("inspect", "-f", "{{.State.ExitCode}}", container, timeout=120)
        assert code.stdout.strip() != "137", (
            "PID 1 was SIGKILLed after ignoring SIGTERM — the CMD is not in exec form")
        assert code.stdout.strip() == "0", (
            f"uvicorn must exit cleanly on SIGTERM, got exit code {code.stdout.strip()}")
        assert elapsed < 10.0, (
            f"the container took {elapsed:.1f}s to stop against a 20s kill window — "
            f"SIGTERM is not reaching uvicorn promptly")
    finally:
        _docker("rm", "-f", container, timeout=300)
        _reclaim(mount, image)
