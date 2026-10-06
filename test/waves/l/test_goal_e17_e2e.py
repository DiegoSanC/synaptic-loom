"""FROZEN GOAL — E17 blockt-e2e-gate (wave l). Authored at plan time
(2026-08-12, strong model, adversarial mandate). READ-ONLY to implementers.

This goal is the GATE-OF-THE-GATE (E12/p4-phase-gate precedent): it pins the
Block-T gate module's shape (e2e-marked, LOOM_GATE conversion, the five frozen
scenarios, real boundaries, host safety, regression wiring) and then RUNS a
scenario subset for real under LOOM_GATE=1.

Kill-targets: a gate that fakes the server in-process (TestClient), that
monkeypatches loom internals instead of driving real subprocess boundaries,
that silently skips when the environment is lacking, or whose "regression leg"
never actually invokes the goal waves.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _goalkit as kit  # noqa: E402

VENV_PY = str(kit.REPO_ROOT / ".venv" / "bin" / "python")
GATE = kit.REPO_ROOT / "client" / "tests" / "e2e" / "test_blockt_e2e.py"

FROZEN_SCENARIOS = (
    "test_blockt_scenario_a_reversal_curate_digest_budget",
    "test_blockt_scenario_b_hostile_search",
    "test_blockt_scenario_c_typed_capture",
    "test_blockt_scenario_d_regression_goals",
    "test_blockt_scenario_e_regression_suites",
)


def _src() -> str:
    return GATE.read_text(encoding="utf-8")


def _tree() -> ast.Module:
    return ast.parse(_src())


def _test_names() -> set[str]:
    return {n.name for n in ast.walk(_tree())
            if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")}


# ---------------------------------------------------------------------------- #
# Shape: module, marker, LOOM_GATE semantics                                    #
# ---------------------------------------------------------------------------- #
def test_frozen_config_targets_this_goal_and_gate_module_exists():
    import json

    cfg = json.loads((kit.REPO_ROOT / "test" / "phase_gates"
                      / "phase_4_e2e_config.json").read_text())
    assert cfg["e2e_test_path"] == "test/waves/l/test_goal_e17_e2e.py"
    assert GATE.is_file()


def test_gate_pytestmark_is_e2e_and_never_module_skipped():
    src = _src()
    assert re.search(r"^pytestmark\s*=\s*pytest\.mark\.e2e", src, re.MULTILINE)
    assert "pytest.skip(" not in src.split('"""', 2)[-1].split("def ")[0], (
        "no module-level skip: environment gaps go through _require_tool"
    )
    assert "test_placeholder" not in src, "the bootstrap stub must be replaced"


def test_gate_encodes_loom_gate_conversion_helper():
    src = _src()
    assert "def _require_tool(" in src, (
        "the LOOM_GATE=1 skip→failure conversion helper is the frozen mechanism "
        "(p4-phase-gate precedent)"
    )
    assert "LOOM_GATE" in src


def test_gate_defines_every_frozen_scenario():
    names = _test_names()
    missing = [s for s in FROZEN_SCENARIOS if s not in names]
    assert not missing, f"frozen scenario tests missing: {missing}"


def test_gate_anchors_the_spec_requirements():
    src = _src()
    for anchor in ("RT.1", "RT.2", "RT.3", "RT.4"):
        assert anchor in src, f"the gate must anchor spec requirement {anchor}"


# ---------------------------------------------------------------------------- #
# Real-boundary discipline                                                      #
# ---------------------------------------------------------------------------- #
def test_gate_boots_real_uvicorn_and_real_cli():
    src = _src()
    assert "uvicorn" in src, "a REAL uvicorn subprocess is the server boundary"
    assert re.search(r"loom.*curate|curate.*loom", src), (
        "the gate must drive the real curate CLI"
    )


def test_gate_has_no_inprocess_fakes_of_loom_components():
    src = _src()
    assert "TestClient" not in src, "no in-process HTTP fakes at the gate"
    assert not re.search(r"monkeypatch\.(setattr|setitem)\(\s*[\"']?(loom|server)\b",
                         src), "never monkeypatch loom/server internals at the gate"


def test_gate_source_is_host_safe():
    src = _src()
    assert ".claude/settings.json" not in src.replace("tmp", ""), (
        "the real ~/.claude must never be touched"
    )
    assert "expanduser(\"~/.loom\")" not in src and "Path.home() / \".loom\"" not in src, (
        "the real ~/.loom must never be touched"
    )


def test_scenario_a_observables_present():
    src = _src()
    assert "superseded by #" in src and "truncated at budget" in src, (
        "scenario (a) must assert the collapsed supersession annotation AND the "
        "budget truncation marker in the final loom log output"
    )
    assert "REFERENCE_FRAMING" in src or "UNVERIFIED DATA" in src, (
        "scenario (a) must assert the I5 framing survives"
    )


def test_scenario_wiring_for_regressions():
    src = _src()
    assert "test/waves" in src, "scenario (d) must run the goal waves"
    assert "regression_known_failures" in src or "setsid" in src, (
        "scenario (e) must honor the single sanctioned darwin exception"
    )


# ---------------------------------------------------------------------------- #
# Runtime legs — collect and RUN a subset under LOOM_GATE=1                     #
# ---------------------------------------------------------------------------- #
def _pytest(args: list[str], *, gate_env: bool, timeout: int = 1800):
    env = {**os.environ}
    env.pop("LOOM_HOME", None)
    if gate_env:
        env["LOOM_GATE"] = "1"
    else:
        env.pop("LOOM_GATE", None)
    return subprocess.run(
        [VENV_PY, "-m", "pytest", *args, "-q", "--no-header", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=str(kit.REPO_ROOT), env=env,
        timeout=timeout)


def test_gate_collects_at_least_five_e2e_tests():
    cp = _pytest([str(GATE), "--collect-only", "-m", "e2e"], gate_env=False)
    m = re.search(r"(\d+) test", cp.stdout + cp.stderr)
    assert m and int(m.group(1)) >= 5, cp.stdout[-800:]


def test_scenario_b_and_c_green_under_loom_gate():
    cp = _pytest([str(GATE),
                  "-k", "scenario_b_hostile_search or scenario_c_typed_capture",
                  "-m", "e2e"], gate_env=True)
    assert cp.returncode == 0, (
        f"gate scenarios b+c must be green under LOOM_GATE=1:\n"
        f"{cp.stdout[-2000:]}\n{cp.stderr[-800:]}"
    )
    assert re.search(r"\b2 passed\b", cp.stdout), cp.stdout[-400:]


def test_scenario_a_green_under_loom_gate():
    cp = _pytest([str(GATE), "-k", "scenario_a_reversal", "-m", "e2e"],
                 gate_env=True)
    assert cp.returncode == 0, (
        f"the spec §4 gate scenario must be green under LOOM_GATE=1:\n"
        f"{cp.stdout[-2000:]}\n{cp.stderr[-800:]}"
    )
