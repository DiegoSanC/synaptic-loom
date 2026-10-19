"""The host-wide lock the Compose waves hold for the life of their stack (accessory).

Not a frozen goal: this grades ``conftest.py`` and ``_goalkit.compose_host_lock``, the
accessory that lets two suites on one Docker host serialize on waves r and s instead of
failing with Docker's "Pool overlaps with other one on this server" (their subnets are
fixed by design and asserted by the goals themselves).

The boundary is real pytest runs, as separate processes, of a synthetic wave tree that
carries copies of the real ``conftest.py`` and ``_goalkit.py`` under a root of its own
(``pytest.ini``), so the conftest sits below the rootdir exactly as it does in this repo.
Its waves ``x`` and ``y`` are Compose waves the way r and s are — a ``compose.goal.yml``
next to a session-scoped ``stack`` fixture — whose stacks only write timestamps. Every run
points ``GOAL_COMPOSE_LOCK`` at a temporary file, so these tests never contend with a real
r or s stack, including the one the process running them may hold.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

WAVES = Path(__file__).resolve().parent
WAIT_LINE = "waiting for the Compose-wave host lock"

FAKE_WAVE = '''
import os, pathlib, time
import pytest

WAVE = pathlib.Path(__file__).parent.name


def note(event):
    with open(os.environ["FAKE_LOG"], "a") as fh:
        fh.write(f"{time.time()} {os.getpid()} {WAVE} {event}\\n")


@pytest.fixture(scope="session")
def stack():
    note("up")
    if os.environ.get("FAKE_FAIL") == WAVE:
        raise RuntimeError("docker compose up failed")
    yield
    note("down")


def test_uses_the_stack(stack):
    time.sleep(float(os.environ.get("FAKE_HOLD", "0")))
'''

PLAIN_WAVE = '''
import fcntl, os


def test_the_lock_is_free():
    fd = os.open(os.environ["GOAL_COMPOSE_LOCK"], os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)
'''


@pytest.fixture
def tree(tmp_path) -> Path:
    root = tmp_path / "repo"
    waves = root / "test" / "waves"
    waves.mkdir(parents=True)
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    for name in ("conftest.py", "_goalkit.py"):
        shutil.copyfile(WAVES / name, waves / name)
    for wave in ("x", "y"):
        (waves / wave).mkdir()
        (waves / wave / "compose.goal.yml").write_text("services: {}\n", encoding="utf-8")
        (waves / wave / f"test_goal_{wave}.py").write_text(FAKE_WAVE, encoding="utf-8")
    (waves / "z").mkdir()
    (waves / "z" / "test_goal_z.py").write_text(PLAIN_WAVE, encoding="utf-8")
    return root


def _env(root: Path, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FAKE_", "PYTEST_"))}
    env.update(GOAL_COMPOSE_LOCK=str(root.parent / "compose.lock"),
               FAKE_LOG=str(root.parent / "events.log"), **extra)
    return env


def _pytest(root: Path, *waves: str, **extra: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         *(f"test/waves/{w}" for w in waves)],
        cwd=root, env=_env(root, **extra), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _finish(proc: subprocess.Popen, timeout: float = 60) -> str:
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        pytest.fail(f"the run did not finish in {timeout}s (deadlocked?):\n{out}")
    return out


def _events(root: Path) -> list[tuple[float, int, str, str]]:
    log = root.parent / "events.log"
    if not log.exists():
        return []
    rows = [line.split() for line in log.read_text(encoding="utf-8").splitlines()]
    return [(float(t), int(pid), wave, event) for t, pid, wave, event in rows]


def _wait_for(root: Path, pid: int, wave: str, event: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not any(e[1:] == (pid, wave, event) for e in _events(root)):
        assert time.monotonic() < deadline, f"{wave} {event} never happened: {_events(root)}"
        time.sleep(0.05)


def _lock_is_free(path: Path) -> bool:
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


def test_two_runs_serialize_on_the_stacks_and_the_waiting_one_says_so(tree):
    first = _pytest(tree, "x", "y", FAKE_HOLD="1")
    _wait_for(tree, first.pid, "x", "up")
    # The opposite order: one lock per subnet set would deadlock these two runs.
    second = _pytest(tree, "y", "x", FAKE_HOLD="1")
    first_out, second_out = _finish(first), _finish(second)
    assert first.returncode == 0, first_out
    assert second.returncode == 0, second_out

    assert WAIT_LINE not in first_out, first_out
    waits = [line for line in second_out.splitlines() if WAIT_LINE in line]
    assert len(waits) == 1, f"one line, once, in the default (captured) output:\n{second_out}"
    assert f"pid {first.pid}" in waits[0], waits[0]

    events = _events(tree)
    spans = {pid: (min(t for t, p, _, _ in events if p == pid),
                   max(t for t, p, _, _ in events if p == pid))
             for pid in (first.pid, second.pid)}
    assert len(events) == 8, events
    assert spans[first.pid][1] < spans[second.pid][0], (
        f"the second run's stacks came up while the first run's were still up: {events}")


def test_one_run_holds_both_stacks_at_once_without_waiting_on_itself(tree):
    run = _pytest(tree, "x", "y")
    out = _finish(run, timeout=30)
    assert run.returncode == 0, out
    assert WAIT_LINE not in out, out
    order = [(wave, event) for _, _, wave, event in _events(tree)]
    assert order.index(("y", "up")) < order.index(("x", "down")), (
        f"both stacks are up together in one session, as r and s are: {order}")
    assert _lock_is_free(tree.parent / "compose.lock")


def test_a_stack_that_fails_to_come_up_releases_the_lock_at_once(tree):
    run = _pytest(tree, "x", "z", FAKE_FAIL="x")
    out = _finish(run)
    assert "1 passed, 1 error" in out, (
        f"wave z, later in the SAME session, must find the lock free:\n{out}")


def test_an_interrupted_run_tears_its_stack_down_and_releases_the_lock(tree):
    run = _pytest(tree, "x", FAKE_HOLD="60")
    _wait_for(tree, run.pid, "x", "up")
    time.sleep(0.3)  # inside the test body, not the fixture
    run.send_signal(signal.SIGINT)
    out = _finish(run)
    assert run.returncode != 0, out
    assert (run.pid, "x", "down") in [e[1:] for e in _events(tree)], out
    assert _lock_is_free(tree.parent / "compose.lock")


def test_every_checkout_resolves_the_same_lock_whatever_home_and_tmpdir_say(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "GOAL_COMPOSE_LOCK"}
    probe = [sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
             "import _goalkit; print(_goalkit.compose_lock_path())", str(WAVES)]
    as_is = subprocess.run(probe, env=env, capture_output=True, text=True, check=True)
    moved = subprocess.run(probe, env={**env, "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
                                       "XDG_CACHE_HOME": str(tmp_path)},
                           cwd=tmp_path, capture_output=True, text=True, check=True)
    path = as_is.stdout.strip()
    assert moved.stdout.strip() == path
    assert path == os.path.expanduser("~/.cache/synaptic-loom/compose-waves.lock")
