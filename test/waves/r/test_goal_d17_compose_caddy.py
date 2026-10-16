"""FROZEN GOAL — D17: Docker Compose + Caddy, and the client address it resolves.
Read-only to implementers.

Oracle properties: the REAL boundary (AGENTS.md §10). The stack under test is the repo's
own ``deploy/compose/docker-compose.yml`` and ``Caddyfile``, brought up with a real
``docker compose up --build`` of the repo's own ``Dockerfile``, a real Caddy and a real
named volume. The ONLY layer on top is ``compose.goal.yml`` next to this file, and it
changes exactly two things, both because a test host cannot do otherwise:

* Caddy publishes on a free LOOPBACK port instead of the host's 80/443.
* The ``edge`` network gets a fixed subnet, so each client container gets a literal
  address (``docker run --ip``) and the audit line is checked against that literal.

TLS is not stubbed. ``LOOM_DOMAIN=loom.localhost`` is a name Caddy's automatic HTTPS
issues from its own internal CA (a public ACME CA can neither be reached nor validate a
name from a test host), and every HTTPS request the clients make is VERIFIED against
that CA's root and the hostname. The Caddyfile is the production one, unedited.

What it proves (owner decision of 2026-10-16, D17):

* only Caddy is published, the server is reachable through Caddy alone, and it resolves
  each client to that client's own address — two clients, two failure windows;
* a client-sent ``Fly-Client-IP`` / ``X-Forwarded-For`` neither buys a fresh window nor
  reaches the audit log;
* a valid token from an address whose failure window is full still works;
* the database survives ``docker compose down`` / ``up``, and ``docker compose stop``
  shuts the server down cleanly inside its grace period;
* an unknown ``LOOM_CLIENT_IP_SOURCE`` stops the server from starting at all.

Skips are scoped to the test that needs the tool, and under ``LOOM_GATE=1`` a missing tool
is a FAILURE (the wave-m discipline). Every product value asserted here is a literal in
this file next to the name of what it mirrors; nothing is imported from the stack.

The stack uses FIXED subnets (that is the topology), so two runs of this wave cannot
overlap on one Docker host; each run tears its stack, volumes and image down.

Run: .venv/bin/python -m pytest test/waves/r/test_goal_d17_compose_caddy.py -q
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _goalkit as kit  # noqa: E402

REPO_ROOT = kit.REPO_ROOT
COMPOSE_DIR = REPO_ROOT / "deploy" / "compose"
COMPOSE_FILE = COMPOSE_DIR / "docker-compose.yml"
ENV_EXAMPLE = COMPOSE_DIR / ".env.example"
DOCKERFILE = REPO_ROOT / "Dockerfile"
GOAL_OVERRIDE = Path(__file__).with_name("compose.goal.yml")
PROBE = Path(__file__).with_name("fixtures") / "probe.py"

LOOM_GATE = os.environ.get("LOOM_GATE") == "1"

# --------------------------------------------------------------------------- #
# Contract literals (duplicated, never imported)                               #
# --------------------------------------------------------------------------- #
SERVER = "loom"                      # docker-compose.yml service names
PROXY = "caddy"
INTERNAL_PORT = 8080                 # Dockerfile CMD --port / EXPOSE
DB_PATH = "/data/loom.db"            # Dockerfile ENV LOOM_DB, fly.toml [env]
BACKEND_SUBNET = "10.217.17.0/24"    # docker-compose.yml networks.backend
CADDY_BACKEND_IP = "10.217.17.2"     # caddy's ipv4_address == FORWARDED_ALLOW_IPS
EDGE_SUBNET = "10.217.18.0/24"       # compose.goal.yml networks.edge
CADDY_EDGE_IP = "10.217.18.2"
CLIENT_A, CLIENT_B, CLIENT_SPOOF, CLIENT_BLOCKED, CLIENT_DIRECT = (
    "10.217.18.11", "10.217.18.12", "10.217.18.13", "10.217.18.14", "10.217.18.15")
DOMAIN = "loom.localhost"
GRACE_S = 30                         # docker-compose.yml stop_grace_period
MIN_SHUTDOWN_HEADROOM_S = 5          # over the CMD's --timeout-graceful-shutdown (wave m)
AUTH_FAILURE_LIMIT = 10              # DESIGN.md §6.2: ten failures per address per minute
STARTUP_LINE = "client_ip_source mode=peer"   # server/app.py startup log (D17)


def _require_tool(name: str, why: str) -> None:
    if shutil.which(name) is not None:
        return
    reason = f"the Compose goal needs the real {name} binary: {why}"
    if LOOM_GATE:
        pytest.fail(f"LOOM_GATE=1 forbids skipping — {reason}")
    pytest.skip(reason)


def _run(args: list[str], *, env: dict | None = None, timeout: int = 900,
         cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          env=env, cwd=str(cwd) if cwd else None)


def _docker(*args: str, timeout: int = 900) -> subprocess.CompletedProcess:
    return _run(["docker", *args], timeout=timeout)


def _no_env_file(tmp_path_factory) -> Path:
    """An empty --env-file, so an operator's own deploy/compose/.env never leaks in."""
    path = tmp_path_factory.mktemp("envfile") / "empty.env"
    path.write_text("", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# The production file as Compose renders it — no override                      #
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def rendered(tmp_path_factory) -> dict:
    _require_tool("docker", "`docker compose config` renders the file this wave grades")
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOOM_")}
    env["LOOM_DOMAIN"] = "loom.example.com"
    done = _run(["docker", "compose", "--env-file", str(_no_env_file(tmp_path_factory)),
                 "-f", str(COMPOSE_FILE), "config", "--format", "json"], env=env)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_only_caddy_is_published_and_the_server_has_no_host_port(rendered):
    services = rendered["services"]
    assert set(services) == {SERVER, PROXY}, set(services)
    assert not services[SERVER].get("ports"), (
        f"the server must not be published on the host: {services[SERVER].get('ports')}")
    published = {(int(p["target"]), p.get("protocol", "tcp")) for p in services[PROXY]["ports"]}
    assert {(80, "tcp"), (443, "tcp")} <= published, published


def test_the_server_sits_only_on_an_internal_network_with_a_fixed_subnet(rendered):
    assert set(rendered["services"][SERVER]["networks"]) == {"backend"}
    backend = rendered["networks"]["backend"]
    assert backend.get("internal") is True, backend
    subnets = [c["subnet"] for c in backend["ipam"]["config"]]
    assert subnets == [BACKEND_SUBNET], subnets


def test_the_server_trusts_forwarded_headers_from_caddy_alone(rendered):
    server_env = rendered["services"][SERVER]["environment"]
    assert server_env["LOOM_CLIENT_IP_SOURCE"] == "peer"
    assert server_env["FORWARDED_ALLOW_IPS"] == CADDY_BACKEND_IP, server_env
    assert "*" not in server_env["FORWARDED_ALLOW_IPS"]
    caddy_net = rendered["services"][PROXY]["networks"]["backend"]
    assert caddy_net["ipv4_address"] == CADDY_BACKEND_IP
    # A fixed address inside the dynamic range could be handed to another container
    # before Caddy starts — the stack would then trust the wrong peer, or not start.
    config = rendered["networks"]["backend"]["ipam"]["config"][0]
    caddy = ipaddress.ip_address(CADDY_BACKEND_IP)
    assert caddy in ipaddress.ip_network(config["subnet"])
    assert caddy not in ipaddress.ip_network(config["ip_range"]), config


def test_the_database_lives_on_a_named_volume_at_data(rendered):
    server = rendered["services"][SERVER]
    assert server["environment"]["LOOM_DB"] == DB_PATH
    mounts = [v for v in server["volumes"] if v["target"] == "/data"]
    assert len(mounts) == 1 and mounts[0]["type"] == "volume", server["volumes"]
    assert mounts[0]["source"] in rendered["volumes"], mounts


def test_the_server_is_built_from_the_repo_dockerfile(rendered):
    build = rendered["services"][SERVER]["build"]
    assert Path(build["context"]).resolve() == REPO_ROOT.resolve(), build
    assert build.get("dockerfile", "Dockerfile") == "Dockerfile", build
    image = rendered["services"][PROXY]["image"]
    assert re.fullmatch(r"caddy:[\w.]+@sha256:[0-9a-f]{64}", image), (
        f"Caddy must be pinned by digest, like the Dockerfile's base image: {image}")


def test_the_grace_period_covers_the_graceful_shutdown_window(rendered):
    grace = rendered["services"][SERVER]["stop_grace_period"]
    assert grace == f"{GRACE_S}s", grace
    cmd = re.search(r"--timeout-graceful-shutdown\",\s*\"(\d+)\"", DOCKERFILE.read_text())
    assert cmd, "the Dockerfile CMD must set --timeout-graceful-shutdown"
    assert GRACE_S - int(cmd.group(1)) >= MIN_SHUTDOWN_HEADROOM_S


def test_compose_refuses_to_render_without_a_domain(tmp_path_factory):
    _require_tool("docker", "`docker compose config` is the refusal under test")
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOOM_")}
    done = _run(["docker", "compose", "--env-file", str(_no_env_file(tmp_path_factory)),
                 "-f", str(COMPOSE_FILE), "config"], env=env)
    assert done.returncode != 0
    assert "LOOM_DOMAIN" in done.stderr, done.stderr


def test_the_env_example_carries_only_a_generic_domain():
    values = dict(line.split("=", 1) for line in ENV_EXAMPLE.read_text().splitlines()
                  if line.strip() and not line.lstrip().startswith("#"))
    assert set(values) == {"LOOM_DOMAIN"}, values
    assert re.fullmatch(r"([a-z0-9-]+\.)*example\.(com|org|net)", values["LOOM_DOMAIN"]), (
        "AGENTS.md rule 15: an example value, never a real host")


# --------------------------------------------------------------------------- #
# The live stack                                                               #
# --------------------------------------------------------------------------- #
@dataclass
class Stack:
    project: str
    env: dict
    image: str
    ca_pem: str

    def compose(self, *args: str, timeout: int = 900) -> subprocess.CompletedProcess:
        return _run(["docker", "compose", "-p", self.project,
                     "--env-file", self.env["_ENV_FILE"],
                     "-f", str(COMPOSE_FILE), "-f", str(GOAL_OVERRIDE), *args],
                    env=self.env, timeout=timeout)

    def container(self, service: str) -> str:
        done = self.compose("ps", "-q", service)
        assert done.returncode == 0 and done.stdout.strip(), done.stderr
        return done.stdout.strip()

    def server_log(self) -> str:
        done = self.compose("logs", "--no-color", SERVER)
        assert done.returncode == 0, done.stderr
        return done.stdout + done.stderr

    def refresh_ca(self) -> None:
        done = self.compose("exec", "-T", PROXY, "cat",
                            "/data/caddy/pki/authorities/local/root.crt")
        assert done.returncode == 0 and "BEGIN CERTIFICATE" in done.stdout, done.stderr
        self.ca_pem = done.stdout

    def up(self, *, build: bool = False) -> None:
        done = self.compose("up", "-d", *(["--build"] if build else []), "--wait",
                            timeout=2400)
        assert done.returncode == 0, (
            f"`docker compose up` must succeed:\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"
            f"\n--- server log ---\n{self.compose('logs', '--no-color').stdout[-3000:]}")
        image = _docker("inspect", "-f", "{{.Image}}", self.container(SERVER))
        assert image.returncode == 0, image.stderr
        self.image = image.stdout.strip()
        # Caddy writes its internal root on first issue; wait for it rather than race it.
        deadline = time.time() + 60
        while True:
            try:
                self.refresh_ca()
                return
            except AssertionError:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)

    def client(self, ip: str, steps: list[dict]) -> list[dict]:
        """Run the probe from a client container at `ip` on the edge network."""
        done = _docker(
            "run", "--rm", "--network", f"{self.project}_edge", "--ip", ip,
            "--add-host", f"{DOMAIN}:{CADDY_EDGE_IP}", "-e", f"LOOM_CA={self.ca_pem}",
            "--entrypoint", "python", self.image, "-c", PROBE.read_text(), json.dumps(steps),
            timeout=600)
        assert done.returncode == 0, f"client probe failed: {done.stderr[-2000:]}"
        return json.loads(done.stdout.strip().splitlines()[-1])

    def adduser(self, name: str) -> str:
        made = self.compose("exec", "-T", "-u", "app", SERVER, "python", "-m",
                            "server.admin", "adduser", "--name", name,
                            "--email", f"{name}@example.invalid")
        assert made.returncode == 0, made.stderr
        return kit.parse_admin_token(made.stdout)


def _url(path: str, scheme: str = "https") -> str:
    return f"{scheme}://{DOMAIN}{path}"


def _bad(headers: dict | None = None) -> dict:
    return {"url": _url("/api/projects"),
            "headers": {"Authorization": "Bearer always-invalid", **(headers or {})}}


def _statuses(results: list[dict]) -> list:
    return [r.get("status", r.get("error")) for r in results]


def _audit_ips(log: str) -> list[str]:
    return re.findall(r"auth outcome=\S+ timestamp=\S+ client_ip=(\S+)", log)


@pytest.fixture(scope="session")
def stack(tmp_path_factory):
    _require_tool("docker", "the Compose stack IS the boundary this goal validates")
    project = f"loom-goal-r-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOOM_")}
    env.update(LOOM_DOMAIN=DOMAIN, GOAL_R_HTTPS_PORT=str(kit.find_free_port()),
               _ENV_FILE=str(_no_env_file(tmp_path_factory)))
    live = Stack(project, env, image="", ca_pem="")
    try:
        live.up(build=True)
        yield live
    finally:
        live.compose("down", "-v", "--rmi", "local", "--remove-orphans", timeout=600)


def test_health_is_served_through_caddy_over_verified_tls(stack):
    health, redirect = stack.client(CLIENT_A, [
        {"url": _url("/api/health")},
        {"url": _url("/api/health", scheme="http")},
    ])
    assert health.get("status") == 200, health
    assert json.loads(health["body"]) == {"status": "ok", "version": "0.1.0"}
    assert redirect.get("status") in (301, 308), redirect
    assert redirect["headers"].get("Location", "").startswith(f"https://{DOMAIN}/"), redirect


def test_the_server_starts_in_peer_mode_and_warns_about_nothing(stack):
    log = stack.server_log()
    assert STARTUP_LINE in log, log[-2000:]
    assert "LOOM_CLIENT_IP_SOURCE is" not in log and "FLY_APP_NAME is" not in log, log[-2000:]


def test_the_server_cannot_be_reached_except_through_caddy(stack):
    inspected = _docker("inspect", "-f",
                        "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
                        stack.container(SERVER))
    server_ips = inspected.stdout.split()
    assert server_ips and all(ipaddress.ip_address(ip) in ipaddress.ip_network(BACKEND_SUBNET)
                              for ip in server_ips), server_ips
    attempts = [{"url": f"http://{SERVER}:{INTERNAL_PORT}/api/health", "timeout": 3}]
    attempts += [{"url": f"http://{ip}:{INTERNAL_PORT}/api/health", "timeout": 3}
                 for ip in server_ips]
    for result in stack.client(CLIENT_DIRECT, attempts):
        assert "error" in result, f"the server answered a client directly: {result}"


def test_two_clients_get_separate_failure_windows(stack):
    a = stack.client(CLIENT_A, [_bad()] * (AUTH_FAILURE_LIMIT + 1))
    assert _statuses(a) == [401] * AUTH_FAILURE_LIMIT + [429], _statuses(a)
    assert a[-1]["headers"].get("Retry-After"), a[-1]
    b = stack.client(CLIENT_B, [_bad()])
    assert _statuses(b) == [401], "client B must not share client A's full window"
    ips = _audit_ips(stack.server_log())
    assert ips.count(CLIENT_A) == AUTH_FAILURE_LIMIT + 1, ips
    assert ips.count(CLIENT_B) == 1, ips
    assert CADDY_BACKEND_IP not in ips, "the proxy's address must never key the window"


def test_spoofed_forwarding_headers_neither_reset_the_window_nor_reach_the_log(stack):
    def spoof(n: int) -> dict:
        return _bad({"X-Forwarded-For": f"192.0.2.{n}", "Fly-Client-IP": f"198.51.100.{n}",
                     "X-Real-IP": f"203.0.113.{n}"})

    results = stack.client(CLIENT_SPOOF, [spoof(n) for n in range(1, AUTH_FAILURE_LIMIT + 2)])
    assert _statuses(results) == [401] * AUTH_FAILURE_LIMIT + [429], _statuses(results)
    ips = _audit_ips(stack.server_log())
    assert ips.count(CLIENT_SPOOF) == AUTH_FAILURE_LIMIT + 1, ips
    for n in range(1, AUTH_FAILURE_LIMIT + 2):
        for forged in (f"192.0.2.{n}", f"198.51.100.{n}", f"203.0.113.{n}"):
            assert forged not in ips, f"a client-sent address reached the audit log: {forged}"


def test_a_valid_token_from_a_blocked_address_still_works(stack):
    token = stack.adduser("goal-r-blocked")
    valid = {"url": _url("/api/projects"), "headers": {"Authorization": f"Bearer {token}"}}
    results = stack.client(CLIENT_BLOCKED, [_bad()] * (AUTH_FAILURE_LIMIT + 1)
                           + [valid, valid, _bad()])
    assert _statuses(results) == ([401] * AUTH_FAILURE_LIMIT + [429]
                                  + [200, 200, 429]), _statuses(results)


def test_an_unknown_client_ip_source_refuses_to_start(stack):
    name = f"loom-goal-r-bogus-{uuid.uuid4().hex[:8]}"
    try:
        done = _docker("run", "--rm", "--name", name, "-e", "LOOM_CLIENT_IP_SOURCE=bogus",
                       stack.image, timeout=120)
    finally:
        _docker("rm", "-f", name, timeout=120)
    assert done.returncode != 0, "the server must not start on an unknown source"
    output = done.stdout + done.stderr
    assert "LOOM_CLIENT_IP_SOURCE='bogus'" in output, output[-2000:]
    assert "Application startup complete" not in output, output[-2000:]


# The two tests below change the stack's state, so they run last and each starts by
# (re)asserting the stack is up.
def test_the_database_survives_down_and_up(stack):
    stack.up()
    token = stack.adduser("goal-r-durable")
    before = stack.container(SERVER)
    down = stack.compose("down", timeout=600)
    assert down.returncode == 0, down.stderr
    volumes = _docker("volume", "ls", "-q", "--filter", f"name={stack.project}_loom_data")
    assert volumes.stdout.split() == [f"{stack.project}_loom_data"], volumes.stdout
    stack.up()
    assert stack.container(SERVER) != before, "`down` must have destroyed the container"
    result = stack.client(CLIENT_A, [{"url": _url("/api/projects"),
                                      "headers": {"Authorization": f"Bearer {token}"}}])
    assert _statuses(result) == [200], result


def test_compose_stop_shuts_the_server_down_cleanly_within_the_grace_period(stack):
    stack.up()
    container = stack.container(SERVER)
    started = time.monotonic()
    stopped = stack.compose("stop", timeout=300)
    elapsed = time.monotonic() - started
    assert stopped.returncode == 0, stopped.stderr
    state = _docker("inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}", container)
    assert state.stdout.split() == ["exited", "0"], (
        f"uvicorn must exit 0 on SIGTERM, not be SIGKILLed (137): {state.stdout}")
    assert elapsed < GRACE_S, f"`docker compose stop` took {elapsed:.1f}s"
    log = stack.server_log()
    assert "Application shutdown complete" in log, log[-2000:]
