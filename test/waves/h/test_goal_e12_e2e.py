"""FROZEN GOAL — E12 phase-e2e-gate (phase-4 E2E, P3.F7). Read-only to implementers.

E12 ships NO product code: it FILLS the repo-owned ``client/tests/e2e/test_phase4_e2e.py``
(today a 0-test stub). This oracle is therefore the GATE OF THE GATE, on two axes:

1. **The gate module must be real, complete and honest.** Its source is audited for the
   load-bearing observables of every P3.F7 scenario (a)–(f) — so a hollow module that
   mentions nothing real fails the structural leg — AND targeted subsets of it are RUN
   for real (subprocess pytest, this venv), so a grep-satisfying module that cannot
   execute fails the execution leg. The phase-3 lesson is encoded head-on: a BLOCKING
   vacuity was found post-merge in the E8 territory, so this goal is UNSATISFIABLE by a
   module of empty, skipped or missing tests — collection minimums, per-scenario frozen
   test names, zero-skip execution under ``LOOM_GATE=1``, and both halves of the
   skip-conversion semantics are asserted.

2. **The gate's own discipline.** Real boundaries only (real uvicorn subprocess, real
   fs, real ``install.sh``): no ``unittest.mock``/``monkeypatch``, no in-process import
   of loom modules, host-safe PATH construction (the wave-c scheduler lesson), the
   crontab shim for installer scenarios, teardown that terminates the CURRENT server
   (the wave-d leak lesson), and host cleanliness snapshot-compared around every
   subprocess this goal spawns (crontab byte-identical via ``crontab -l`` only — this
   goal NEVER writes the real crontab; ~/.claude/settings.json hash; no ~/.loom
   creation; no ~/.config/systemd/user additions).

FROZEN CONTRACT for E12's implementer — the exact shape this oracle enforces
============================================================================
``client/tests/e2e/test_phase4_e2e.py`` must satisfy ALL of the following.

**Naming contract (THE scenario map — no wiggle room).** At least one module-level
test function per prefix:

- (a) cache lifecycle      → ``test_e2e_cache_lifecycle*``
- (b) starvation           → ``test_e2e_starvation*``
- (c) late registration    → ``test_e2e_late_registration*``
- (d) re-install           → ``test_e2e_reinstall*``
- (e) escalation/attention → ``test_e2e_attention*``
- (f) regression legs      → ``test_e2e_regression*``

plus: the uv-gated venv leg of (d) is a ``test_e2e_reinstall*`` function whose NAME
contains ``uv`` (this goal selects it with ``-k "e2e_reinstall and uv"``), and its
availability gate must run BEFORE any expensive work so the ``LOOM_GATE=1``
conversion is observable in seconds.

**Module hygiene.** ``pytestmark`` includes the ``e2e`` marker and carries NO
``skipif``/``skip`` (skips are per-test via the tool gate, never module-wide — a
module-level skip is a green-looking gate that executed nothing). ``LOOM_GATE``
semantics use the phase-3 gate's discipline: ``os.environ.get("LOOM_GATE")`` read
once, a ``def _require_tool(`` helper that ``pytest.fail(``-s under the gate and
``pytest.skip(``-s interactively. All tests collect under ``-m e2e`` (>= 6 total).

**Source anchors (case-insensitive regex unless stated).** AC ids ``AC-F7.1``
``AC-F7.2`` ``AC-F7.3`` literally. Per scenario:

- (a): ``sweep_last_run``; ``cache_hits``; ``stat_skips``; ``invalidat``; ``append``.
- (b): ``starv``; ``registry``; ``deliverab``; ``ineligible``; a first-pass claim
  (``first[-_ ]?pass`` or ``pass[-_ ]?1``). The registry cache is built by a REAL
  processor pass against the real server — the literal ``fetched_at`` is FORBIDDEN
  module-wide (hand-writing the registry file is the in-process fake this bans).
- (c): ``late``; ``orphan``; ``regist``.
- (d): literal ``install.sh``; literal ``--yes``; ``inode|st_ino`` (AC-F6.1 venv
  identity); ``doctor`` and ``trigger`` (AC-F6.5 reached); an explicit uv gate
  (``_require_tool("uv"`` or ``shutil.which("uv"``); ``def _crontab_shim(`` (the
  installer scenarios never touch a real crontab: shim on PATH, tmp HOME); the
  uv-named test's own body must NOT contain ``LOOM_SKIP_VENV`` (that env var bypasses
  exactly the step-2 venv path under test).
- (e): ``attention``; a quoted ``"status"``/``'status'`` (real ``loom status``);
  ``unparseab``; ``held``.
- (f) / AC-F7.2: the literals ``test/waves/a`` … ``test/waves/f`` (the phase-2/3 goal
  waves) and ``client/tests`` (the client suite); the literals ``test/waves/g`` and
  ``test/waves/h`` are FORBIDDEN module-wide (g is THIS phase's build oracle, run by
  the wave runner; h is this goal — running either from the gate is circular);
  a visible self-exclusion of the gate from the client-suite run (``--ignore``,
  ``deselect`` or ``not e2e``); ``darwin``, ``setsid`` and ``test_hook_install``
  (the single sanctioned regression exception, encoded not hoped).

**Real boundary.** Must contain ``uvicorn``, ``server.app:app``, ``subprocess.Popen``
(the real-server boot pattern) and ``.terminate()`` (terminate the CURRENT server —
the wave-d review fixed a fixture that leaked the replacement one). Must NOT import
``loom``/``waves``/``_goalkit`` (AST-checked: strings inside generated-runner literals
are fine, import statements are not), must NOT import ``unittest.mock`` and must not
mention ``monkeypatch``/``mock.patch`` at all. Host safety, same bar as phase 3: must
assert ``shutil.which("systemctl", path=…) is None`` and pin
``shutil.which("crontab", path=…)`` on built PATHs; must never read the real PATH
(``os.environ["PATH"]``/``.get("PATH")``/``os.getenv("PATH")``) nor contain the
literal ``/usr/bin:/bin``.

**Execution minimums (frozen).** Scenario subset (a)–(e), ``LOOM_GATE=1``, uv
present: >= 5 passed, 0 failed/errored/SKIPPED, < 900 s. With uv unresolvable:
``LOOM_GATE=1`` FAILS the uv leg (>= 1 failed, 0 skipped, output names LOOM_GATE);
without ``LOOM_GATE`` it SKIPS it (exit 0, >= 1 skipped, 0 failed). Regression leg
(``-k e2e_regression``, ``LOOM_GATE=1``): exit 0, >= 1 passed, 0 failed/skipped.

Kill matrix (AC → tests here)
=============================
- AC-F7.1 (each of P3.F1/F2/F3/F5/F6 real-boundary, no in-process fakes):
  test_gate_defines_every_frozen_scenario_test_name,
  test_gate_collects_at_least_six_e2e_tests,
  test_scenario_{a,b,c,d,e}_*_observables,
  test_gate_boots_real_uvicorn_and_terminates_it,
  test_gate_has_no_inprocess_fakes_of_loom_components,
  test_gate_source_is_host_safe,
  test_ac_f7_1_scenario_subset_all_green_under_loom_gate,
  test_scenario_subset_runtime_under_ceiling.
- AC-F7.2 (regression legs, darwin setsid sanctioned):
  test_scenario_f_regression_wiring, test_ac_f7_2_regression_leg_green.
- AC-F7.3 (LOOM_GATE converts environment skips to failures):
  test_gate_pytestmark_is_e2e_and_never_module_skipped,
  test_gate_encodes_loom_gate_skip_conversion_helper,
  test_ac_f7_3_loom_gate_converts_missing_uv_skip_to_failure,
  test_ac_f7_3_without_loom_gate_missing_uv_is_a_scoped_skip.
- Gate identity & host discipline:
  test_frozen_config_targets_this_goal_and_the_gate_module_exists,
  test_gate_anchors_all_three_ac_ids, test_host_*.

Run: .venv/bin/python -m pytest test/waves/h/test_goal_e12_e2e.py -q
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
VENV_PY = REPO_ROOT / ".venv" / "bin" / "python"
PHASE4_E2E = REPO_ROOT / "client" / "tests" / "e2e" / "test_phase4_e2e.py"
E2E_CONFIG = REPO_ROOT / "test" / "phase_gates" / "phase_3_e2e_config.json"

# Frozen naming contract: scenario letter -> required test-name prefix (>=1 each).
REQUIRED_SCENARIO_PREFIXES = {
    "a-cache-lifecycle": "test_e2e_cache_lifecycle",
    "b-starvation": "test_e2e_starvation",
    "c-late-registration": "test_e2e_late_registration",
    "d-reinstall": "test_e2e_reinstall",
    "e-attention": "test_e2e_attention",
    "f-regression": "test_e2e_regression",
}
SCENARIO_K = ("e2e_cache_lifecycle or e2e_starvation or e2e_late_registration "
              "or e2e_reinstall or e2e_attention")
UV_LEG_K = "e2e_reinstall and uv"
REGRESSION_K = "e2e_regression"

MIN_GATE_COLLECTED = 6          # one per scenario (a)-(f); the 0-test stub must fail
MIN_SCENARIO_PASSED = 5         # >=1 per scenario (a)-(e), all green, zero skips
SCENARIO_RUNTIME_CEILING_S = 900

pytestmark = [
    pytest.mark.skipif(shutil.which("gitleaks") is None,
                       reason="phase-4 gate goal requires the real gitleaks binary"),
    pytest.mark.skipif(shutil.which("jq") is None,
                       reason="the driven gate runs the real client/install.sh (jq)"),
    pytest.mark.skipif(shutil.which("uv") is None,
                       reason="the gate's uv venv leg must be able to run for real "
                              "(LOOM_GATE=1 forbids it skipping)"),
]


# --------------------------------------------------------------------------- #
# Scheduler-free PATHs for the driven gate (wave-c deviation lesson)           #
# --------------------------------------------------------------------------- #
# Generous allowlist — everything a legitimate e2e plausibly execs via PATH — with NO
# scheduler anywhere: not crontab, not systemctl, not launchctl, not at.
_ALLOWED_BINS = (
    "python3", "git", "jq", "bash", "sh", "env", "cat", "mv", "cp", "mkdir", "rm",
    "date", "diff", "grep", "sed", "chmod", "uname", "dirname", "basename", "head",
    "tail", "tr", "wc", "sort", "touch", "ln", "sleep", "mktemp", "readlink", "id",
    "awk", "cut", "find", "gitleaks", "sqlite3", "curl", "tar", "gzip", "xargs",
    "ps", "kill", "hostname", "true", "false", "printf", "expr", "stat", "od",
    "base64", "which", "uniq", "tee", "realpath", "nohup",
)
_FORBIDDEN_SCHEDULERS = ("crontab", "systemctl", "launchctl", "at", "batch")


def _allowlist_path(dest: Path, *, with_uv: bool) -> str:
    dest.mkdir(parents=True, exist_ok=True)
    names = _ALLOWED_BINS + (("uv",) if with_uv else ())
    for name in names:
        target = shutil.which(name)
        if target and not (dest / name).exists():
            (dest / name).symlink_to(target)
    return str(dest)


# --------------------------------------------------------------------------- #
# Host snapshot (READ-ONLY: this goal never writes any real host state)        #
# --------------------------------------------------------------------------- #
def _host_snapshot() -> dict:
    home = Path.home()
    snap: dict = {}

    if shutil.which("crontab"):
        cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True,
                              timeout=30)
        snap["crontab"] = (cron.returncode, cron.stdout)
    else:
        snap["crontab"] = None

    settings = home / ".claude" / "settings.json"
    snap["settings_sha"] = (hashlib.sha256(settings.read_bytes()).hexdigest()
                            if settings.exists() else None)
    snap["settings_backups"] = (sorted(p.name for p in
                                       (home / ".claude").glob("settings.json.bak-*"))
                                if (home / ".claude").exists() else [])

    loom = home / ".loom"
    snap["loom"] = sorted(p.name for p in loom.iterdir()) if loom.exists() else None

    systemd_user = home / ".config" / "systemd" / "user"
    snap["systemd_user"] = (sorted(p.name for p in systemd_user.iterdir())
                            if systemd_user.exists() else None)
    return snap


# --------------------------------------------------------------------------- #
# One module-scoped run of the targeted subsets, snapshot-bracketed            #
# --------------------------------------------------------------------------- #
def _run(cmd: list[str], *, env: dict, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, env=env, cwd=str(REPO_ROOT),
                          capture_output=True, text=True, timeout=timeout)


@pytest.fixture(scope="module")
def gate(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e12_gate")
    safe_path = _allowlist_path(tmp / "safe_bin", with_uv=True)
    nouv_path = _allowlist_path(tmp / "nouv_bin", with_uv=False)

    # The wave-c boundary assertions, on every PATH the driven gate will inherit.
    for path in (safe_path, nouv_path):
        for name in _FORBIDDEN_SCHEDULERS:
            assert shutil.which(name, path=path) is None, (
                f"the driven gate's PATH must not resolve the real {name!r}"
            )
        assert shutil.which("gitleaks", path=path) is not None, (
            "the real gitleaks must stay reachable — the gate's processor passes use it"
        )
    assert shutil.which("uv", path=safe_path) is not None, (
        "the scenario leg's PATH must resolve uv — LOOM_GATE=1 forbids the uv leg skipping"
    )
    assert shutil.which("uv", path=nouv_path) is None, (
        "the AC-F7.3 legs need a PATH where uv is genuinely unresolvable"
    )

    pre = _host_snapshot()

    # Vacuity leg: what does the gate module COLLECT under the e2e marker?
    collect = _run([str(VENV_PY), "-m", "pytest", str(PHASE4_E2E),
                    "--collect-only", "-q", "-m", "e2e"],
                   env={**os.environ}, timeout=120)

    # Scenario leg (a)-(e): LOOM_GATE=1, uv reachable — all green, zero skips.
    t0 = time.monotonic()
    scenarios = _run([str(VENV_PY), "-m", "pytest", str(PHASE4_E2E), "-q",
                      "-k", SCENARIO_K],
                     env={**os.environ, "PATH": safe_path, "LOOM_GATE": "1"},
                     timeout=1800)
    scenarios_elapsed = time.monotonic() - t0

    # AC-F7.3, both halves, on the uv leg with uv unresolvable:
    # without LOOM_GATE -> a scoped skip; with LOOM_GATE=1 -> a hard failure.
    env_nouv = {**os.environ, "PATH": nouv_path}
    env_nouv.pop("LOOM_GATE", None)
    skip_leg = _run([str(VENV_PY), "-m", "pytest", str(PHASE4_E2E), "-q", "-rs",
                     "-k", UV_LEG_K], env=env_nouv, timeout=600)
    fail_leg = _run([str(VENV_PY), "-m", "pytest", str(PHASE4_E2E), "-q",
                     "-k", UV_LEG_K],
                    env={**env_nouv, "LOOM_GATE": "1"}, timeout=600)

    # AC-F7.2 regression leg: the gate's own (f) tests drive waves a-f + client suite.
    # Normal environment — each driven goal owns its own isolation discipline.
    regression = _run([str(VENV_PY), "-m", "pytest", str(PHASE4_E2E), "-q",
                       "-k", REGRESSION_K],
                      env={**os.environ, "LOOM_GATE": "1"}, timeout=5400)

    post = _host_snapshot()

    return {"collect": collect, "scenarios": scenarios,
            "scenarios_elapsed": scenarios_elapsed, "skip_leg": skip_leg,
            "fail_leg": fail_leg, "regression": regression,
            "pre": pre, "post": post}


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
_COUNT_RE = re.compile(r"(\d+) (passed|failed|error|errors|skipped|xfailed|xpassed)")
_COLLECT_RE = re.compile(r"(\d+)(?:/\d+)? tests? collected")


def _pytest_counts(cp: subprocess.CompletedProcess) -> dict:
    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0,
              "xfailed": 0, "xpassed": 0}
    for line in reversed((cp.stdout or "").strip().splitlines()):
        found = _COUNT_RE.findall(line)
        if found:
            for n, kind in found:
                kind = "error" if kind == "errors" else kind
                counts[kind] += int(n)
            break
        if "no tests ran" in line:
            break
    return counts


def _collected_count(cp: subprocess.CompletedProcess) -> int:
    for line in reversed((cp.stdout or "").strip().splitlines()):
        m = _COLLECT_RE.search(line)
        if m:
            return int(m.group(1))
        if "no tests collected" in line:
            return 0
    return 0


def _tail(cp: subprocess.CompletedProcess, n: int = 12) -> str:
    text = (cp.stdout or "") + "\n" + (cp.stderr or "")
    return "\n".join(text.strip().splitlines()[-n:])


def _src() -> str:
    assert PHASE4_E2E.exists(), f"{PHASE4_E2E} must exist (repo-owned E12 deliverable)"
    return PHASE4_E2E.read_text(encoding="utf-8")


def _tree() -> ast.Module:
    return ast.parse(_src())


def _test_defs(tree: ast.Module) -> list[ast.FunctionDef]:
    return [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]


def _func_segment(src: str, node: ast.FunctionDef) -> str:
    lines = src.splitlines()
    first = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
    return "\n".join(lines[first:node.end_lineno])


# --------------------------------------------------------------------------- #
# Frozen boundary identity                                                     #
# --------------------------------------------------------------------------- #
def test_frozen_config_targets_this_goal_and_the_gate_module_exists():
    cfg = json.loads(E2E_CONFIG.read_text(encoding="utf-8"))
    assert cfg["e2e_test_path"] == "test/waves/h/test_goal_e12_e2e.py"
    assert "test/waves/h/test_goal_e12_e2e.py" in cfg["e2e_runner"]
    assert cfg["test_mode"] == "local_process" and cfg["container_parity"] is False
    assert PHASE4_E2E.exists(), "the repo-owned phase-4 e2e gate module must exist"


# --------------------------------------------------------------------------- #
# Structure & vacuity — the phase-3 lesson, enforced structurally              #
# --------------------------------------------------------------------------- #
def test_gate_pytestmark_is_e2e_and_never_module_skipped():
    """AC-F7.3 hygiene: e2e-marked, and NEVER skippable as a whole module.

    A module-level skipif is a green-looking phase gate that executed nothing —
    exactly the vacuity class this goal exists to kill."""
    src = _src()
    marks = [n for n in _tree().body if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "pytestmark"
                     for t in n.targets)]
    assert marks, "the gate must declare a module-level pytestmark"
    segment = "\n".join(ast.get_source_segment(src, n) or "" for n in marks)
    assert "e2e" in segment, f"pytestmark must carry the e2e marker: {segment!r}"
    assert "skipif" not in segment and "mark.skip" not in segment, (
        "pytestmark must not skip the module — skips are per-test via the tool gate "
        f"(LOOM_GATE=1 turns them into failures): {segment!r}"
    )


def test_gate_encodes_loom_gate_skip_conversion_helper():
    """AC-F7.3 mechanism: the phase-3 gate's _require_tool discipline, reused.

    ``os.environ.get("LOOM_GATE")`` read once; a ``_require_tool`` helper that
    ``pytest.fail``s under the gate and ``pytest.skip``s interactively."""
    src = _src()
    assert re.search(r"os\.environ\.get\(\s*[\"']LOOM_GATE[\"']", src), (
        "the gate must read LOOM_GATE via os.environ.get('LOOM_GATE')"
    )
    assert "def _require_tool(" in src, (
        "the gate must define the _require_tool helper (test_phase3_e2e.py discipline)"
    )
    assert "pytest.fail(" in src, "LOOM_GATE=1 must convert the skip into pytest.fail"
    assert "pytest.skip(" in src, "interactively a missing tool is a scoped pytest.skip"


def test_gate_defines_every_frozen_scenario_test_name():
    """AC-F7.1 completeness: >=1 test per scenario (a)-(f), by the FROZEN names."""
    defs = _test_defs(_tree())
    names = [n.name for n in defs]
    for scenario, prefix in REQUIRED_SCENARIO_PREFIXES.items():
        assert any(name.startswith(prefix) for name in names), (
            f"scenario {scenario} missing: no test named {prefix}* "
            f"(frozen naming contract; defined tests: {names})"
        )
    assert any(n.name.startswith("test_e2e_reinstall") and "uv" in n.name
               for n in defs), (
        "the uv-gated venv leg of (d) must be a test_e2e_reinstall* function whose "
        f"name contains 'uv' (this goal selects it with -k {UV_LEG_K!r}); got: {names}"
    )


def test_gate_anchors_all_three_ac_ids():
    src = _src()
    for ac in ("AC-F7.1", "AC-F7.2", "AC-F7.3"):
        assert re.search(re.escape(ac), src), f"{ac} must be anchored in the gate module"


def test_gate_collects_at_least_six_e2e_tests(gate):
    """Vacuity: the gate must COLLECT >= 6 e2e tests (the stub collects 0)."""
    count = _collected_count(gate["collect"])
    assert gate["collect"].returncode == 0 and count >= MIN_GATE_COLLECTED, (
        f"the gate module must collect >= {MIN_GATE_COLLECTED} tests under -m e2e "
        f"(got {count}):\n{_tail(gate['collect'])}"
    )


# --------------------------------------------------------------------------- #
# AC-F7.1 — per-scenario source observables (anchors frozen in the docstring)  #
# --------------------------------------------------------------------------- #
def test_scenario_a_cache_lifecycle_observables():
    """(a): >=2 passes vs a real server — full read once, stat-only after,
    invalidation on append — proven via the additive record, not hope."""
    src = _src()
    for pattern, why in [
        (r"sweep_last_run", "the run record read (p4-sweep-last-run-additive)"),
        (r"(?i)cache_hits", "the cache-hit counter (stat-only reuse observed)"),
        (r"(?i)stat_skips", "the stat-skip counter (frozen run-log field)"),
        (r"(?i)invalidat", "the invalidation-on-change claim"),
        (r"(?i)append", "the append that must invalidate the entry"),
    ]:
        assert re.search(pattern, src), f"scenario (a) observable missing: {why} ({pattern})"


def test_scenario_b_starvation_observables():
    """(b): newer non-deliverable corpus + older deliverable transcript + REAL
    registry cache -> delivered on pass 1."""
    src = _src()
    for pattern, why in [
        (r"(?i)starv", "the starvation scenario, named"),
        (r"(?i)registry", "the project-registry cache"),
        (r"(?i)deliverab", "the deliverable-vs-non-deliverable split"),
        (r"(?i)ineligible", "the withheld skip class (p4-sweep-eligibility)"),
        (r"(?i)(first[-_ ]?pass|pass[-_ ]?1)", "delivery on the FIRST pass"),
    ]:
        assert re.search(pattern, src), f"scenario (b) observable missing: {why} ({pattern})"
    assert "fetched_at" not in src, (
        "the registry cache must be built by a REAL processor pass against the real "
        "server — hand-writing {fetched_at, projects[]} is an in-process fake of "
        "loom's own component (AC-F7.1) and is forbidden module-wide"
    )


def test_scenario_c_late_registration_observables():
    """(c): orphans delivered after the project is registered mid-test (anti-tombstone)."""
    src = _src()
    for pattern, why in [
        (r"(?i)late", "the late-registration scenario, named"),
        (r"(?i)orphan", "the historical orphans that become eligible"),
        (r"(?i)regist", "the mid-test registration"),
    ]:
        assert re.search(pattern, src), f"scenario (c) observable missing: {why} ({pattern})"


def test_scenario_d_reinstall_observables():
    """(d): real ``install.sh --yes`` twice; venv identity; trigger+doctor reached;
    crontab shimmed; the uv leg never bypasses the venv path it exists to test."""
    src = _src()
    for pattern, why in [
        (r"install\.sh", "the real installer"),
        (r"--yes", "the plain --yes path (the phase-3 failure base)"),
        (r"(?i)(st_ino|inode)", "venv identity across re-install (AC-F6.1)"),
        (r"(?i)doctor", "the run reaches the doctor step (AC-F6.5)"),
        (r"(?i)trigger", "the run re-arms the trigger (AC-F6.5)"),
        (r"(_require_tool\(\s*[\"']uv[\"']|shutil\.which\(\s*[\"']uv[\"'])",
         "an explicit uv availability gate (skip interactively, fail under LOOM_GATE)"),
        (r"def _crontab_shim\(", "the crontab shim — installer scenarios must never "
                                 "invoke the real crontab (stub on PATH, tmp HOME)"),
    ]:
        assert re.search(pattern, src), f"scenario (d) observable missing: {why} ({pattern})"

    uv_legs = [n for n in _test_defs(_tree())
               if n.name.startswith("test_e2e_reinstall") and "uv" in n.name]
    for node in uv_legs:
        segment = _func_segment(src, node)
        assert "LOOM_SKIP_VENV" not in segment, (
            f"{node.name} must not set/mention LOOM_SKIP_VENV — that env var bypasses "
            "exactly the step-2 venv path the uv leg exists to prove (p4-reinstall-"
            "idempotent)"
        )


def test_scenario_e_attention_observables():
    """(e): unparseable transcript surfaces in the attention record AND ``loom status``."""
    src = _src()
    for pattern, why in [
        (r"attention", "the attention record (p4-attention-record)"),
        (r"[\"']status[\"']", "a real `loom status` invocation"),
        (r"(?i)unparseab", "the wholly-unparseable transcript trigger"),
        (r"(?i)held", "the held counter carried by the record"),
    ]:
        assert re.search(pattern, src), f"scenario (e) observable missing: {why} ({pattern})"


def test_scenario_f_regression_wiring():
    """AC-F7.2: waves a-f + client suite wired; g/h forbidden (circularity); the
    darwin setsid failure is THE single sanctioned exception, encoded."""
    src = _src()
    for wave in "abcdef":
        assert f"test/waves/{wave}" in src, (
            f"the regression leg must invoke the phase-2/3 goal wave test/waves/{wave}"
        )
    for wave in "gh":
        assert f"test/waves/{wave}" not in src, (
            f"test/waves/{wave} must NOT appear in the gate: wave g is this phase's "
            "build oracle (run by the wave runner) and wave h is this goal — either "
            "reference is circular"
        )
    assert "client/tests" in src, "the regression leg must run the client suite"
    assert re.search(r"(?i)(--ignore|deselect|not e2e)", src), (
        "the client-suite regression run must visibly exclude the gate module itself "
        "(--ignore/--deselect/-m 'not e2e') — recursing into test_phase4_e2e.py is "
        "circular"
    )
    for pattern, why in [
        (r"(?i)darwin", "the darwin platform condition of the sanctioned exception"),
        (r"setsid", "the setsid hook failure (the ONLY sanctioned exception)"),
        (r"test_hook_install", "the exception pinned to its exact test file, "
                               "not a blanket tolerance"),
    ]:
        assert re.search(pattern, src), f"AC-F7.2 observable missing: {why} ({pattern})"


# --------------------------------------------------------------------------- #
# AC-F7.1 — real boundary, no in-process fakes                                 #
# --------------------------------------------------------------------------- #
def test_gate_boots_real_uvicorn_and_terminates_it():
    """Real server subprocess (test_phase3_e2e.py pattern), and the wave-d teardown
    lesson: the CURRENT server is terminated, never leaked."""
    src = _src()
    assert "uvicorn" in src and "server.app:app" in src, (
        "the gate must boot the real server (uvicorn server.app:app)"
    )
    assert "subprocess.Popen" in src, (
        "the real-server boot must be a subprocess (the phase-2/3 gate pattern)"
    )
    assert ".terminate()" in src, (
        "teardown must terminate the CURRENT server handle (the wave-d review fixed "
        "a fixture that leaked the replacement server)"
    )


def test_gate_has_no_inprocess_fakes_of_loom_components():
    """No mocking/patching of loom's own components, no wave-goal-artifact imports.

    AST-checked so that loom imports inside generated-subprocess string literals
    (the audit-runner trick) stay legal while real in-process imports are not."""
    src = _src()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in ("loom", "waves"), (
                    f"in-process import of {alias.name!r}: the gate drives loom via "
                    "subprocesses at the real boundary, never in-process"
                )
                assert "_goalkit" not in alias.name, (
                    "the repo gate must not depend on wave-goal build artifacts"
                )
                assert not alias.name.startswith("unittest"), (
                    "unittest(.mock) is forbidden — no in-process fakes of loom "
                    "components (AC-F7.1)"
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.split(".")[0]
            assert root not in ("loom", "waves", "unittest"), (
                f"in-process import from {module!r} is forbidden in the gate"
            )
            assert "_goalkit" not in module, (
                "the repo gate must not depend on wave-goal build artifacts"
            )
    assert "monkeypatch" not in src and "mock.patch" not in src, (
        "monkeypatch/mock.patch are forbidden — the gate proves real boundaries"
    )
    assert re.search(r"sys\.path[^\n]*waves", src) is None, (
        "the gate must not put test/waves on sys.path"
    )


def test_gate_source_is_host_safe():
    """Wave-c lesson: no PATH the gate builds may reach the real schedulers."""
    src = _src()
    assert re.search(r"shutil\.which\(\s*[\"']systemctl[\"']\s*,\s*path=", src) and \
        "is None" in src, (
        "the gate must assert its boundary: "
        'shutil.which("systemctl", path=<built PATH>) is None'
    )
    assert re.search(r"shutil\.which\(\s*[\"']crontab[\"']\s*,\s*path=", src), (
        "the gate must pin what its built PATHs resolve `crontab` to (the shim)"
    )
    assert re.search(r"os\.environ(\.get\(|\[)\s*[\"']PATH[\"']", src) is None and \
        re.search(r"os\.getenv\(\s*[\"']PATH[\"']", src) is None, (
        "never build a PATH from the real one (the wave-c include_gitleaks=True bug)"
    )
    assert "/usr/bin:/bin" not in src, (
        "no literal /usr/bin:/bin: on Linux that PATH reaches the REAL crontab — "
        "build minimal PATHs from symlinked allowlists instead"
    )


# --------------------------------------------------------------------------- #
# Execution — the gate must RUN, green, honest, laptop-runnable                #
# --------------------------------------------------------------------------- #
def test_ac_f7_1_scenario_subset_all_green_under_loom_gate(gate):
    """AC-F7.1: scenarios (a)-(e) run for real under LOOM_GATE=1 — all green.

    A 0-test stub, a failing scenario, or a scenario that skips its way to exit 0
    all fail here. Skips are pinned to zero: gitleaks/jq/uv are present (goal
    skipif) and LOOM_GATE=1 converts any remaining environment skip to a failure."""
    counts = _pytest_counts(gate["scenarios"])
    assert gate["scenarios"].returncode == 0, (
        f"the gate's scenario subset must exit 0:\n{_tail(gate['scenarios'])}"
    )
    assert counts["passed"] >= MIN_SCENARIO_PASSED, (
        f"scenarios (a)-(e) must run >= {MIN_SCENARIO_PASSED} passing tests "
        f"(got {counts['passed']} — the bootstrap stub selects 0):\n"
        f"{_tail(gate['scenarios'])}"
    )
    assert counts["failed"] == 0 and counts["error"] == 0, _tail(gate["scenarios"])
    assert counts["skipped"] == 0, (
        f"no scenario test may skip under LOOM_GATE=1 on this host: {counts}\n"
        f"{_tail(gate['scenarios'])}"
    )


def test_scenario_subset_runtime_under_ceiling(gate):
    assert gate["scenarios_elapsed"] < SCENARIO_RUNTIME_CEILING_S, (
        f"scenarios (a)-(e) took {gate['scenarios_elapsed']:.0f}s — the gate must "
        f"stay laptop-runnable (< {SCENARIO_RUNTIME_CEILING_S}s)"
    )


def test_ac_f7_3_loom_gate_converts_missing_uv_skip_to_failure(gate):
    """AC-F7.3, the load-bearing half: with uv unresolvable, LOOM_GATE=1 FAILS.

    Run against the real uv leg under a PATH where uv genuinely does not resolve:
    the environment skip must become a hard failure that names LOOM_GATE — never a
    skip, never a silent pass."""
    counts = _pytest_counts(gate["fail_leg"])
    assert gate["fail_leg"].returncode != 0, (
        f"LOOM_GATE=1 with uv unresolvable must make -k {UV_LEG_K!r} fail:\n"
        f"{_tail(gate['fail_leg'])}"
    )
    assert counts["failed"] >= 1, (
        f"the uv leg must FAIL (not error-out, not vanish) under LOOM_GATE=1 "
        f"without uv: {counts}\n{_tail(gate['fail_leg'])}"
    )
    assert counts["skipped"] == 0, (
        f"LOOM_GATE=1 must leave ZERO skips: {counts}\n{_tail(gate['fail_leg'])}"
    )
    assert "LOOM_GATE" in (gate["fail_leg"].stdout + gate["fail_leg"].stderr), (
        "the failure must name LOOM_GATE so the operator knows the skip was "
        f"converted, not that uv itself broke:\n{_tail(gate['fail_leg'])}"
    )


def test_ac_f7_3_without_loom_gate_missing_uv_is_a_scoped_skip(gate):
    """AC-F7.3, the other half: interactively the same condition is a SCOPED skip.

    Exit 0 with >=1 skip and 0 failures — the skip stays on the uv leg alone and
    never module-wide (that is pinned structurally by the pytestmark test)."""
    counts = _pytest_counts(gate["skip_leg"])
    assert gate["skip_leg"].returncode == 0, (
        f"without LOOM_GATE the uv leg must skip cleanly (exit 0):\n"
        f"{_tail(gate['skip_leg'])}"
    )
    assert counts["skipped"] >= 1, (
        f"the uv leg must SKIP when uv is unresolvable and LOOM_GATE is unset "
        f"(a pass here would mean the gate never checked uv): {counts}\n"
        f"{_tail(gate['skip_leg'])}"
    )
    assert counts["failed"] == 0 and counts["error"] == 0, _tail(gate["skip_leg"])


def test_ac_f7_2_regression_leg_green(gate):
    """AC-F7.2: the gate's regression tests run — waves a-f + client suite pass.

    The (f) tests do the driving; this asserts they exist, execute and come back
    green with zero skips under LOOM_GATE=1 (the stub selects 0 and fails)."""
    counts = _pytest_counts(gate["regression"])
    assert gate["regression"].returncode == 0, (
        f"the gate's regression leg must exit 0:\n{_tail(gate['regression'])}"
    )
    assert counts["passed"] >= 1, (
        f"-k {REGRESSION_K!r} must select and pass >= 1 test: {counts}\n"
        f"{_tail(gate['regression'])}"
    )
    assert counts["failed"] == 0 and counts["error"] == 0, _tail(gate["regression"])
    assert counts["skipped"] == 0, (
        f"the regression leg may not skip under LOOM_GATE=1: {counts}\n"
        f"{_tail(gate['regression'])}"
    )


# --------------------------------------------------------------------------- #
# Host cleanliness AFTER running everything (snapshot-compare only)            #
# --------------------------------------------------------------------------- #
def test_host_crontab_untouched(gate):
    """The real crontab is byte-identical before/after (or consistently absent).

    Snapshot-compare only: this goal itself never writes or removes the crontab."""
    assert gate["post"]["crontab"] == gate["pre"]["crontab"], (
        "running the phase-4 gate changed the developer's real crontab — "
        f"before={gate['pre']['crontab']!r} after={gate['post']['crontab']!r}"
    )


def test_host_claude_settings_untouched(gate):
    assert gate["post"]["settings_sha"] == gate["pre"]["settings_sha"], (
        "~/.claude/settings.json was mutated by a gate run"
    )
    assert gate["post"]["settings_backups"] == gate["pre"]["settings_backups"], (
        "new ~/.claude/settings.json.bak-* backups imply the real hook install ran"
    )


def test_host_no_loom_home_created(gate):
    assert gate["post"]["loom"] == gate["pre"]["loom"], (
        "~/.loom was created or modified — a gate run escaped its temp LOOM_HOME"
    )


def test_host_no_systemd_user_additions(gate):
    assert gate["post"]["systemd_user"] == gate["pre"]["systemd_user"], (
        "~/.config/systemd/user changed — an installer run reached the real systemd"
    )
