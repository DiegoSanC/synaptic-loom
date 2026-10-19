"""FROZEN GOAL — E8 phase-e2e-gate (phase-3 E2E, P2.F9). Read-only to implementers.

E8 ships NO product code: it fills the repo-owned ``client/tests/e2e/test_phase3_e2e.py``
and updates RUNBOOK gate sections. This oracle therefore validates DELIVERABLES, on two
axes at once:

1. **The repo E2E must exist, pass and be honest.** We run it for real (subprocess,
   this venv) and require >=5 tests, all green, zero skips, under a runtime ceiling —
   AND we audit its source for the load-bearing observables of each AC, so a hollow
   suite (vacuous tests that mention nothing real) fails the grep leg while a fake
   grep-satisfying suite that doesn't run fails the execution leg.
2. **The gate's own discipline.** Host cleanliness is snapshot-compared around every
   subprocess this goal spawns (crontab byte-identical via `crontab -l` only — this
   goal NEVER writes the real crontab; ~/.claude/settings.json hash; no ~/.loom
   creation; no ~/.config/systemd/user additions), and the driven suites run under a
   scheduler-free PATH (allowlist symlink dir; neither `crontab` nor `systemctl`
   resolvable) — the wave-c deviation lesson, enforced actively AND as a backstop.

FROZEN CONTRACT for E8's implementer — exact anchors this oracle greps
======================================================================
``client/tests/e2e/test_phase3_e2e.py`` source must contain (case-insensitive regex
unless stated):

- AC ids, literally: ``AC-F9.1`` ``AC-F9.2`` ``AC-F9.3`` ``AC-F9.4`` (test names or
  comments).
- AC-F9.1: ``torn``; ``resum``; ``multi[-_ ]?byte``; the two content halves named
  ``pre[-_ ]?tear`` and ``post[-_ ]?tear``; the real API surface ``/entries``;
  ``queue``; and a literal quoted ``"status"``/``'status'`` (the `loom status` CLI arg).
- AC-F9.2: an open-counting instrumentation identifier —
  ``sys.addaudithook``/``addaudithook`` or an identifier matching
  ``open_?count|count_?opens?|opened_?files|files_?opened`` — plus ``steady[-_ ]?state``,
  ``settled`` and ``zero``.
- AC-F9.3: a named minimal trigger environment —
  ``minimal[-_ ]?path|trigger[-_ ]?(env|path)|cron[-_ ]?(env|path|like)`` — plus
  ``gitleaks``, ``quarantin`` and ``findings|metadata`` (assert against quarantine
  metadata, not just file counts).
- AC-F9.4: ``uninstall-trigger`` (the real flag, case-sensitive); ``no crontab``;
  ``@daily`` (the seeded foreign entry); ``byte`` (byte-exact survival).
- Self-containment/boot: no import statement (nor sys.path entry) may reference
  ``_goalkit`` or ``waves`` (mentioning them in a docstring is fine); must contain
  ``uvicorn``, ``server.app:app`` and ``subprocess.Popen`` (the real-server boot
  pattern of test_phase2_e2e.py).
- Host safety (wave-c lesson, epic constraint): must contain the boundary assertion
  patterns ``shutil.which("systemctl", path=...)`` with ``is None`` and
  ``shutil.which("crontab", path=...)``; must NOT reference the real PATH when
  building one (no ``os.environ["PATH"]`` / ``os.environ.get("PATH")`` /
  ``os.getenv("PATH")``) and must NOT use the literal ``/usr/bin:/bin`` (on Linux
  that reaches the real crontab — build minimal PATHs from symlinked allowlists).

``docs/RUNBOOK.md`` frozen anchors (sections addressed by their ``## N.`` headings):

- §6 (AC-F9.6): must match ``schema[-\\s]?invalid`` (manual recovery narrowed to
  genuinely-corrupt transcripts) and must NO LONGER match ``not bounded`` (today's
  "adapter_error ... is NOT bounded" claim must be rewritten).
- §9 (AC-F9.6): must match ``re-?measured|post[-\\s]?fix|post[-\\s]?phase`` AND a
  zero-read figure ``\\b(zero|0)\\s+(transcript\\s+)?(files?|opens?|reads?|bytes)\\b``,
  and must NO LONGER match ``read cost is unbounded``.
- §7 (AC-F9.6): must match ``parity`` and ``scanner|gitleaks`` (WSL2 checklist
  extended with the trigger-context parity check).

Dynamic minimums (frozen): phase-3 e2e >=5 passed, 0 failed/errored/skipped, <600 s;
phase-2 e2e (extend-never-weaken) >=6 passed, 0 failed/errored/skipped; waves a–e
goals all green with >=44 passed; ``LOOM_SKIP_LIVE=1 contract/verify.py`` exit 0.

Run: .venv/bin/python -m pytest test/waves/f/test_goal_e8_e2e.py -q
"""
from __future__ import annotations

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
PHASE3_E2E = REPO_ROOT / "client" / "tests" / "e2e" / "test_phase3_e2e.py"
PHASE2_E2E = REPO_ROOT / "client" / "tests" / "e2e" / "test_phase2_e2e.py"
RUNBOOK = REPO_ROOT / "docs" / "RUNBOOK.md"
E2E_CONFIG = REPO_ROOT / "test" / "phase_gates" / "phase_2_e2e_config.json"

MIN_GATE_TESTS = 5          # a hollowed-out gate (the 0-test stub) must fail
MIN_PHASE2_TESTS = 6        # the old gate's test count — extend, never weaken
MIN_WAVES_TESTS = 44        # 42 green today in a–c + at least one each from d and e
GATE_RUNTIME_CEILING_S = 600

pytestmark = [
    pytest.mark.skipif(shutil.which("gitleaks") is None,
                       reason="phase-3 gate goal requires the real gitleaks binary"),
    pytest.mark.skipif(shutil.which("jq") is None,
                       reason="the driven e2e suites run the real hook/install.sh (jq)"),
]


# --------------------------------------------------------------------------- #
# Scheduler-free PATH for the driven suites (wave-c deviation lesson)          #
# --------------------------------------------------------------------------- #
# Generous allowlist — everything a legitimate e2e plausibly execs via PATH — but
# with NO scheduler anywhere: not crontab, not systemctl, not launchctl, not at.
_ALLOWED_BINS = (
    "python3", "git", "jq", "bash", "sh", "env", "cat", "mv", "cp", "mkdir", "rm",
    "date", "diff", "grep", "sed", "chmod", "uname", "dirname", "basename", "head",
    "tail", "tr", "wc", "sort", "touch", "ln", "sleep", "mktemp", "readlink", "id",
    "awk", "cut", "find", "gitleaks", "sqlite3", "curl", "tar", "gzip", "xargs",
    "ps", "kill", "hostname", "true", "false", "printf", "expr", "stat", "od",
    "base64", "which", "uniq", "tee", "realpath", "nohup", "uv",
)
_FORBIDDEN_SCHEDULERS = ("crontab", "systemctl", "launchctl", "at", "batch")


def _scheduler_free_path(dest: Path) -> str:
    dest.mkdir(parents=True, exist_ok=True)
    for name in _ALLOWED_BINS:
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
# One module-scoped run of everything, snapshot-bracketed                      #
# --------------------------------------------------------------------------- #
def _run(cmd: list[str], *, env: dict, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, env=env, cwd=str(REPO_ROOT),
                          capture_output=True, text=True, timeout=timeout)


@pytest.fixture(scope="module")
def gate(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e8_gate")
    safe_path = _scheduler_free_path(tmp / "safe_bin")

    # The wave-c boundary assertions, on the PATH the driven suites will inherit.
    for name in _FORBIDDEN_SCHEDULERS:
        assert shutil.which(name, path=safe_path) is None, (
            f"the driven suites' PATH must not resolve the real {name!r}"
        )
    assert shutil.which("systemctl", path=safe_path) is None
    assert shutil.which("crontab", path=safe_path) is None
    assert shutil.which("gitleaks", path=safe_path) is not None, (
        "the real gitleaks must stay reachable — the parity/quarantine legs need it"
    )
    stripped_env = {**os.environ, "PATH": safe_path}

    pre = _host_snapshot()

    t0 = time.monotonic()
    p3 = _run([str(VENV_PY), "-m", "pytest", str(PHASE3_E2E), "-q"],
              env=stripped_env, timeout=900)
    p3_elapsed = time.monotonic() - t0

    p2 = _run([str(VENV_PY), "-m", "pytest", str(PHASE2_E2E), "-q"],
              env=stripped_env, timeout=900)

    # AC-F9.5: the wave goals run under the normal environment — each goal owns its
    # own isolation discipline. NEVER include test/waves/f here (this file).
    waves = _run([str(VENV_PY), "-m", "pytest",
                  "test/waves/a", "test/waves/b", "test/waves/c",
                  "test/waves/d", "test/waves/e", "-q"],
                 env={**os.environ}, timeout=1800)

    contract = _run([str(VENV_PY), "contract/verify.py"],
                    env={**os.environ, "LOOM_SKIP_LIVE": "1"}, timeout=300)

    post = _host_snapshot()

    return {"p3": p3, "p3_elapsed": p3_elapsed, "p2": p2, "waves": waves,
            "contract": contract, "pre": pre, "post": post}


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
_COUNT_RE = re.compile(r"(\d+) (passed|failed|error|errors|skipped|xfailed|xpassed)")


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


def _tail(cp: subprocess.CompletedProcess, n: int = 12) -> str:
    text = (cp.stdout or "") + "\n" + (cp.stderr or "")
    return "\n".join(text.strip().splitlines()[-n:])


def _src() -> str:
    assert PHASE3_E2E.exists(), f"{PHASE3_E2E} must exist (repo-owned E8 deliverable)"
    return PHASE3_E2E.read_text(encoding="utf-8")


def _runbook_section(num: int) -> str:
    text = RUNBOOK.read_text(encoding="utf-8")
    m = re.search(rf"(?ms)^## {num}\..*?(?=^## |\Z)", text)
    assert m, f"RUNBOOK has no '## {num}.' section"
    return m.group(0)


# --------------------------------------------------------------------------- #
# Frozen boundary identity                                                     #
# --------------------------------------------------------------------------- #
def test_frozen_config_targets_this_goal_and_the_repo_suite_exists():
    cfg = json.loads(E2E_CONFIG.read_text(encoding="utf-8"))
    assert cfg["e2e_test_path"] == "test/waves/f/test_goal_e8_e2e.py"
    assert "test/waves/f/test_goal_e8_e2e.py" in cfg["e2e_runner"]
    assert cfg["test_mode"] == "local_process" and cfg["container_parity"] is False
    assert PHASE3_E2E.exists(), "the repo-owned phase-3 e2e suite must exist"


# --------------------------------------------------------------------------- #
# Leg 1 — drive the repo-owned E2E (AC-F9.1..F9.4 must EXIST and PASS)         #
# --------------------------------------------------------------------------- #
def test_ac_f9_gate_suite_min_five_tests_all_green(gate):
    """AC-F9.1..AC-F9.4: the repo suite proving them runs green, >=5 real tests.

    A 0-test stub, a failing suite, or a suite that skips its way to exit 0 all fail
    here. Skips are pinned to zero because gitleaks/jq are present (goal skipif)."""
    counts = _pytest_counts(gate["p3"])
    assert gate["p3"].returncode == 0, (
        f"phase-3 e2e suite must exit 0:\n{_tail(gate['p3'])}"
    )
    assert counts["passed"] >= MIN_GATE_TESTS, (
        f"phase-3 e2e must run >= {MIN_GATE_TESTS} passing tests "
        f"(got {counts['passed']} — the bootstrap stub collects 0):\n{_tail(gate['p3'])}"
    )
    assert counts["failed"] == 0 and counts["error"] == 0, _tail(gate["p3"])
    assert counts["skipped"] == 0, (
        f"no phase-3 e2e test may skip on this host: {counts}\n{_tail(gate['p3'])}"
    )


def test_gate_suite_runtime_under_ceiling(gate):
    assert gate["p3_elapsed"] < GATE_RUNTIME_CEILING_S, (
        f"phase-3 e2e took {gate['p3_elapsed']:.0f}s — the gate must stay "
        f"laptop-runnable (< {GATE_RUNTIME_CEILING_S}s)"
    )


# --------------------------------------------------------------------------- #
# Leg 2 — AC-coverage audit of the suite's source (anchors frozen in docstring)#
# --------------------------------------------------------------------------- #
def test_ac_f9_1_source_covers_torn_resumed_recovery():
    """AC-F9.1: torn-then-resumed + mid-multibyte, both halves land as API entries."""
    src = _src()
    assert re.search(r"AC-F9\.1", src), "AC-F9.1 must be anchored in the suite"
    for pattern, why in [
        (r"(?i)torn", "the torn-transcript scenario"),
        (r"(?i)resum", "the resumed-session variant (tear is mid-file)"),
        (r"(?i)multi[-_ ]?byte", "the mid-multibyte tear variant"),
        (r"(?i)pre[-_ ]?tear", "the pre-tear content half, named and asserted"),
        (r"(?i)post[-_ ]?tear", "the post-tear content half, named and asserted"),
        (r"/entries", "entries asserted via the real API"),
        (r"(?i)queue", "the queue-drains claim"),
        (r"[\"']status[\"']", "a real `loom status` invocation (clean status claim)"),
    ]:
        assert re.search(pattern, src), f"AC-F9.1 observable missing: {why} ({pattern})"


def test_ac_f9_2_source_instruments_zero_read_steady_state():
    """AC-F9.2: instrumented steady-state pass opens zero transcript files."""
    src = _src()
    assert re.search(r"AC-F9\.2", src), "AC-F9.2 must be anchored in the suite"
    assert re.search(
        r"(?i)(addaudithook|open_?count|count_?opens?|opened_?files|files_?opened)",
        src), ("AC-F9.2 needs an open-counting instrumentation "
               "(addaudithook or an open/count identifier)")
    for pattern, why in [
        (r"(?i)steady[-_ ]?state", "the steady-state scenario"),
        (r"(?i)settled", "a settled, processed tree"),
        (r"(?i)zero", "the zero-open claim"),
    ]:
        assert re.search(pattern, src), f"AC-F9.2 observable missing: {why} ({pattern})"


def test_ac_f9_3_source_proves_trigger_path_parity():
    """AC-F9.3: minimal trigger-like PATH still quarantines via the gitleaks engine."""
    src = _src()
    assert re.search(r"AC-F9\.3", src), "AC-F9.3 must be anchored in the suite"
    assert re.search(r"(?i)(minimal[-_ ]?path|trigger[-_ ]?(env|path)|cron[-_ ]?(env|path|like))",
                     src), ("AC-F9.3 needs a named minimal trigger environment "
                            "(minimal_path / trigger_env / cron_like ...)")
    for pattern, why in [
        (r"(?i)gitleaks", "the gitleaks engine"),
        (r"(?i)quarantin", "the quarantine outcome"),
        (r"(?i)(findings|metadata)", "assertion against quarantine metadata"),
    ]:
        assert re.search(pattern, src), f"AC-F9.3 observable missing: {why} ({pattern})"


def test_ac_f9_4_source_asserts_uninstall_crontab_discipline():
    """AC-F9.4: uninstall leaves no crontab; a seeded foreign entry survives byte-exact."""
    src = _src()
    assert re.search(r"AC-F9\.4", src), "AC-F9.4 must be anchored in the suite"
    for pattern, why in [
        (r"uninstall-trigger", "the real --uninstall-trigger flag"),
        (r"(?i)no crontab", "the no-crontab-left outcome"),
        (r"@daily", "the seeded @daily foreign entry"),
        (r"(?i)byte", "byte-exact survival of the foreign entry"),
    ]:
        assert re.search(pattern, src), f"AC-F9.4 observable missing: {why} ({pattern})"


def test_gate_suite_self_contained_and_boots_real_uvicorn():
    """The repo suite never depends on wave-goal artifacts, and its boundary is real.

    Self-containment is checked at the import level (a docstring MENTIONING
    test/waves/_goalkit.py is fine — importing it is not)."""
    src = _src()
    assert re.search(r"(?m)^\s*(?:from|import)\s+[^\n#]*(_goalkit|\bwaves\b)", src) \
        is None, "test_phase3_e2e.py must not import test/waves/_goalkit (or waves.*)"
    assert re.search(r"sys\.path[^\n]*waves", src) is None, (
        "test_phase3_e2e.py must not put test/waves on sys.path"
    )
    assert "uvicorn" in src and "server.app:app" in src, (
        "the suite must boot the real server (uvicorn server.app:app)"
    )
    assert "subprocess.Popen" in src, (
        "the real-server boot must be a subprocess (test_phase2_e2e.py pattern)"
    )


def test_gate_suite_source_is_host_safe():
    """Wave-c lesson: no PATH the suite builds may reach the real schedulers."""
    src = _src()
    assert re.search(r"shutil\.which\(\s*[\"']systemctl[\"']\s*,\s*path=", src) and \
        "is None" in src, (
        "the suite must assert its boundary: "
        'shutil.which("systemctl", path=<built PATH>) is None'
    )
    assert re.search(r"shutil\.which\(\s*[\"']crontab[\"']\s*,\s*path=", src), (
        "the suite must pin what its built PATHs resolve `crontab` to (the shim)"
    )
    assert re.search(r"os\.environ(\.get\(|\[)\s*[\"']PATH[\"']", src) is None and \
        re.search(r"os\.getenv\(\s*[\"']PATH[\"']", src) is None, (
        "never build a PATH from the real one (the wave-c include_gitleaks=True bug)"
    )
    assert "/usr/bin:/bin" not in src, (
        "no literal /usr/bin:/bin: on Linux that PATH reaches the REAL crontab — "
        "build minimal trigger PATHs from symlinked allowlists instead"
    )


# --------------------------------------------------------------------------- #
# Leg 3 — AC-F9.5 regression (extend-never-weaken)                             #
# --------------------------------------------------------------------------- #
def test_ac_f9_5_waves_goals_regression_green(gate):
    """AC-F9.5: waves a–e goals all green (d/e missing or red is a fail, by design)."""
    counts = _pytest_counts(gate["waves"])
    assert gate["waves"].returncode == 0, (
        f"waves a–e goals must all run green (missing d/e dirs fail collection):\n"
        f"{_tail(gate['waves'])}"
    )
    assert counts["failed"] == 0 and counts["error"] == 0, _tail(gate["waves"])
    assert counts["passed"] >= MIN_WAVES_TESTS, (
        f"waves a–e must contribute >= {MIN_WAVES_TESTS} passing goal tests "
        f"(a–c alone were 42): {counts}"
    )


def test_ac_f9_5_contract_verifier_green(gate):
    """AC-F9.5: LOOM_SKIP_LIVE=1 contract/verify.py stays green."""
    assert gate["contract"].returncode == 0, _tail(gate["contract"])
    assert "RESULT: OK" in gate["contract"].stdout, _tail(gate["contract"])


def test_ac_f9_5_phase2_e2e_still_green(gate):
    """AC-F9.5: the phase-2 gate's repo E2E still passes — extend, never weaken."""
    counts = _pytest_counts(gate["p2"])
    assert gate["p2"].returncode == 0, (
        f"client/tests/e2e/test_phase2_e2e.py must stay green:\n{_tail(gate['p2'])}"
    )
    assert counts["passed"] >= MIN_PHASE2_TESTS, (
        f"phase-2 e2e shrank below its {MIN_PHASE2_TESTS}-test bar: {counts}"
    )
    assert counts["failed"] == 0 and counts["error"] == 0 and counts["skipped"] == 0, (
        f"{counts}\n{_tail(gate['p2'])}"
    )


# --------------------------------------------------------------------------- #
# Leg 4 — host cleanliness AFTER running everything (snapshot-compare only)    #
# --------------------------------------------------------------------------- #
def test_host_crontab_untouched(gate):
    """The real crontab is byte-identical before/after (or consistently absent).

    Snapshot-compare only: this goal itself never writes or removes the crontab."""
    assert gate["post"]["crontab"] == gate["pre"]["crontab"], (
        "running the e2e suites changed the developer's real crontab — "
        f"before={gate['pre']['crontab']!r} after={gate['post']['crontab']!r}"
    )


def test_host_claude_settings_untouched(gate):
    assert gate["post"]["settings_sha"] == gate["pre"]["settings_sha"], (
        "~/.claude/settings.json was mutated by an e2e run"
    )
    assert gate["post"]["settings_backups"] == gate["pre"]["settings_backups"], (
        "new ~/.claude/settings.json.bak-* backups imply the real hook install ran"
    )


def test_host_no_loom_home_created(gate):
    assert gate["post"]["loom"] == gate["pre"]["loom"], (
        "~/.loom was created or modified — an e2e run escaped its temp LOOM_HOME"
    )


def test_host_no_systemd_user_additions(gate):
    assert gate["post"]["systemd_user"] == gate["pre"]["systemd_user"], (
        "~/.config/systemd/user changed — an installer run reached the real systemd"
    )


# --------------------------------------------------------------------------- #
# Leg 5 — AC-F9.6 RUNBOOK gate sections (anchors frozen in the docstring)      #
# --------------------------------------------------------------------------- #
def test_ac_f9_6_runbook_s6_narrowed_to_schema_invalid():
    """AC-F9.6: §6 manual recovery narrowed to genuinely-corrupt (schema-invalid)."""
    sec6 = _runbook_section(6)
    assert re.search(r"(?i)schema[-\s]?invalid", sec6), (
        "RUNBOOK §6 must narrow manual recovery to schema-invalid transcripts"
    )
    assert re.search(r"(?i)not bounded", sec6) is None, (
        "RUNBOOK §6 still claims adapter_error is 'NOT bounded' — stale after P2.F1/F2"
    )


def test_ac_f9_6_runbook_s9_cost_remeasured_post_fix():
    """AC-F9.6: §9 carries a re-measured post-fix cost figure (zero steady-state reads)."""
    sec9 = _runbook_section(9)
    assert re.search(r"(?i)(re-?measured|post[-\s]?fix|post[-\s]?phase)", sec9), (
        "RUNBOOK §9 must tag the cost figure as re-measured post-fix"
    )
    assert re.search(r"(?i)\b(zero|0)\s+(transcript\s+)?(files?|opens?|reads?|bytes)\b",
                     sec9), (
        "RUNBOOK §9 must state the post-fix steady-state figure "
        "(zero transcript files/reads per pass)"
    )
    assert re.search(r"(?i)read cost is unbounded", sec9) is None, (
        "RUNBOOK §9 still lists the unbounded read cost as a current limitation"
    )


def test_ac_f9_6_runbook_s7_wsl2_parity_check():
    """AC-F9.6: §7 WSL2 checklist extended with the trigger-context parity check."""
    sec7 = _runbook_section(7)
    assert re.search(r"(?i)parity", sec7), (
        "RUNBOOK §7 must mention the trigger-context scanner parity check"
    )
    assert re.search(r"(?i)(scanner|gitleaks)", sec7), (
        "RUNBOOK §7 parity check must name the scanner/gitleaks"
    )
