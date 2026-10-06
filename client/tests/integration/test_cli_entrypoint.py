"""Test of the installed entry point (``loom``) — PLAN.md §T6 AC.

Runs the real console script from the venv (sibling of the pytest interpreter) to prove
that ``loom --help`` lists the subcommands and that an unknown command exits ≠ 0.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

LOOM_BIN = Path(sys.executable).parent / "loom"

if not LOOM_BIN.exists():
    pytest.skip("console script 'loom' not installed in the venv", allow_module_level=True)

SUBCOMMANDS = ("process", "register", "log", "status", "push-quarantine", "init", "doctor")


def test_help_lists_all_subcommands():
    result = subprocess.run([str(LOOM_BIN), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    for sub in SUBCOMMANDS:
        assert sub in result.stdout, f"'{sub}' missing from --help"


def test_unknown_command_exits_nonzero():
    result = subprocess.run([str(LOOM_BIN), "definitely-not-a-command"], capture_output=True, text=True)
    assert result.returncode != 0
