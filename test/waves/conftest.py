"""Shared ACCESSORY wiring for the frozen wave goals — never an oracle.

The Compose waves (r, s) bring up the real ``deploy/compose`` stack on FIXED subnets, which
the goals assert, so two suites on one Docker host would collide ("Pool overlaps with
other one on this server"). Every ``stack`` fixture of a wave that layers a
``compose.goal.yml`` is therefore set up under ``kit.compose_host_lock`` and holds it until
the fixture is torn down: a second suite waits for the stacks, says so in one line, and
runs. See ``_goalkit.compose_host_lock`` for why it is one shared, re-entrant lock.
"""
from __future__ import annotations

import contextlib
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _goalkit as kit  # noqa: E402

COMPOSE_STACK_FIXTURE = "stack"


def _is_compose_stack(fixturedef) -> bool:
    if fixturedef.argname != COMPOSE_STACK_FIXTURE:
        return False
    source = inspect.getsourcefile(fixturedef.func)
    return source is not None and (Path(source).parent / "compose.goal.yml").is_file()


class ComposeStackLock:
    """A plugin, not a conftest hook: a session-scoped fixture is set up through the
    session's hooks, which leave out conftests below the rootdir — this one included."""

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        if not _is_compose_stack(fixturedef):
            return (yield)
        capture = request.config.pluginmanager.getplugin("capturemanager")

        def say(line: str) -> None:
            # Past pytest's capture, so a waiting run shows why it has stopped moving.
            with capture.global_and_fixture_disabled() if capture else contextlib.nullcontext():
                print(line, file=sys.stderr, flush=True)

        held = contextlib.ExitStack()
        held.enter_context(kit.compose_host_lock(on_wait=say))
        # Registered before the fixture's own teardown, so it runs after it.
        fixturedef.addfinalizer(held.close)
        try:
            return (yield)
        except BaseException:
            held.close()
            raise


def pytest_configure(config):
    config.pluginmanager.register(ComposeStackLock(), "goal-compose-stack-lock")
