"""D21 — server plugins: installed distributions that add to the server.

A plugin is found the way the server will find it in production: as an installed
distribution (a `*.dist-info` with an `entry_points.txt` in the group
`loom.server_plugins`) on the path of a fresh interpreter that imports `server.app`. Each
test writes its plugins into its own directory and starts that interpreter, so the plugins
it installs are a literal of the test and never leak into the rest of the suite.
"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Imports the app, adds one user, then reads each path given on the command line once
# without a credential and once with that user's bearer. Prints the answers as JSON.
SERVE = textwrap.dedent("""
    import json, logging, secrets, sys
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(name)s %(message)s")
    from fastapi.testclient import TestClient
    from server import db
    from server.app import app

    token = secrets.token_urlsafe(32)
    conn = db.init_db()
    conn.execute("INSERT INTO users (name, email, token_hash, active, created_at) "
                 "VALUES ('dev1', 'dev1@example.com', ?, 1, ?)",
                 (db.hash_token(token), db.iso_now()))
    conn.commit()
    conn.close()
    answers = {}
    with TestClient(app) as client:
        for arg in sys.argv[1:]:
            method, path = arg.split(" ")
            anonymous = client.request(method, path)
            bearer = client.request(method, path, headers={"Authorization": f"Bearer {token}"})
            answers[arg] = {"anonymous": anonymous.status_code, "bearer": bearer.status_code,
                            "body": bearer.json() if bearer.status_code == 200 else None}
    print(json.dumps(answers))
""")

WHOAMI = """
from fastapi import Request

def register(app):
    @app.get("/plugin/whoami")
    def whoami(request: Request):
        return {"user_id": request.state.user_id}
"""


def _install(site: Path, name: str, source: str, target: str = "") -> None:
    """Install a one-module distribution `name` whose entry point `name` is `target`."""
    (site / f"{name}_plugin.py").write_text(textwrap.dedent(source))
    dist_info = site / f"{name}_plugin-0.1.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}-plugin\nVersion: 0.1\n")
    (dist_info / "entry_points.txt").write_text(
        f"[loom.server_plugins]\n{name} = {target or f'{name}_plugin:register'}\n")


def _start(tmp_path: Path, *requests: str) -> subprocess.CompletedProcess:
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    env = {**os.environ,
           "PYTHONPATH": os.pathsep.join([str(site), str(REPO_ROOT)]),
           "LOOM_DB": str(tmp_path / "loom.db")}
    return subprocess.run([sys.executable, "-c", SERVE, *requests], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60)


def _site(tmp_path: Path) -> Path:
    site = tmp_path / "site"
    site.mkdir()
    return site


def test_without_plugins_the_startup_line_says_none(tmp_path):
    started = _start(tmp_path)

    assert started.returncode == 0, started.stderr
    assert "uvicorn.error.loom.plugins server_plugins names=none" in started.stderr


def test_a_plugin_route_answers_behind_the_bearer(tmp_path):
    _install(_site(tmp_path), "whoami", WHOAMI)

    started = _start(tmp_path, "GET /plugin/whoami")

    assert started.returncode == 0, started.stderr
    assert json.loads(started.stdout) == {
        "GET /plugin/whoami": {"anonymous": 401, "bearer": 200, "body": {"user_id": 1}}}
    assert "uvicorn.error.loom.plugins server_plugins names=whoami" in started.stderr


def test_a_plugin_route_under_ui_takes_no_write_and_no_bearer(tmp_path):
    _install(_site(tmp_path), "page", """
        def register(app):
            @app.api_route("/ui/plugin-page", methods=["GET", "POST"])
            def page():
                return {"reached": True}
    """)

    started = _start(tmp_path, "GET /ui/plugin-page", "POST /ui/plugin-page")

    assert started.returncode == 0, started.stderr
    assert "uvicorn.error.loom.plugins server_plugins names=page" in started.stderr
    assert json.loads(started.stdout) == {
        "GET /ui/plugin-page": {"anonymous": 401, "bearer": 401, "body": None},
        "POST /ui/plugin-page": {"anonymous": 405, "bearer": 405, "body": None}}


def test_plugins_load_in_entry_point_name_order(tmp_path):
    site = _site(tmp_path)
    _install(site, "zeta", "def register(app):\n    pass\n")
    _install(site, "alpha", "def register(app):\n    pass\n")

    started = _start(tmp_path)

    assert started.returncode == 0, started.stderr
    assert "uvicorn.error.loom.plugins server_plugins names=alpha,zeta" in started.stderr


def test_a_plugin_redeclaring_a_core_route_stops_the_server(tmp_path):
    _install(_site(tmp_path), "shadow", """
        def register(app):
            @app.get("/api/health")
            def health():
                return {"status": "shadowed"}
    """)

    started = _start(tmp_path)

    assert started.returncode != 0
    assert ("server plugin 'shadow' declares GET /api/health, which the server already "
            "answers") in started.stderr


def test_a_plugin_redeclaring_another_plugins_route_stops_the_server(tmp_path):
    site = _site(tmp_path)
    _install(site, "first", WHOAMI)
    _install(site, "second", WHOAMI)

    started = _start(tmp_path)

    assert started.returncode != 0
    assert ("server plugin 'second' declares GET /plugin/whoami, which the server already "
            "answers") in started.stderr


def test_a_plugin_that_raises_stops_the_server(tmp_path):
    _install(_site(tmp_path), "broken", """
        def register(app):
            raise ValueError("misconfigured")
    """)

    started = _start(tmp_path)

    assert started.returncode != 0
    assert "server plugin 'broken' failed to register: misconfigured" in started.stderr


def test_a_plugin_that_cannot_be_loaded_stops_the_server(tmp_path):
    _install(_site(tmp_path), "missing", "", target="no_such_module:register")

    started = _start(tmp_path)

    assert started.returncode != 0
    assert "server plugin 'missing' could not be loaded" in started.stderr
