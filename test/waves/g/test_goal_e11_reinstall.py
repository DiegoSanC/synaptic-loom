"""FROZEN GOAL — E11 installer-idempotent-reinstall (P3.F6). Read-only to implementers.

Independent real-boundary oracle for the frozen interface ``p4-reinstall-idempotent``:
the REAL ``client/install.sh`` runs as a subprocess against a temp HOME/LOOM_HOME; the
host schedulers are PATH-shimmed record-replay stubs (NEVER the real crontab — on this
Mac crontab(1) writes hang for 30s+ under sandbox); the REAL ``uv`` binary drives the
VERIFIED-failure legs (RUNBOOK §7, 2026-08-11: ``uv venv`` refuses an existing dir with
the ``--clear`` hint and exits 2 BEFORE the trigger re-arm — reproduced empirically for
healthy, broken and cfg-only venv dirs while authoring this goal).

Kill-matrix (AC → test → expected TODAY, pre-E11):

  AC-F6.1  test_ac_f6_1_plain_reinstall_with_uv_keeps_healthy_venv        RED (exit 2)
             — the exact VERIFIED failure: plain --yes over a uv-created venv. Kills
               rm-and-recreate masquerading as idempotency via os.lstat(bin/python)
               .st_ino (lstat, not stat: uv's bin/python is a SYMLINK whose stat()
               would follow to the never-changing base interpreter).
           test_ac_f6_1_plain_reinstall_fallback_keeps_venv_and_reruns_editable
             — GREEN pin (hard constraint 4: the python3 -m venv fallback is already
               idempotent; must not regress). Kills keep-but-skip (pure --update
               semantics without the editable re-run): the fabricated venv's
               bin/python|pip are recorders, so "editable install re-ran" is asserted
               from their call log, offline.
  AC-F6.2  test_ac_f6_2_broken_venv_with_uv_recreated_exit_zero           RED (exit 2)
           test_ac_f6_2_broken_venv_fallback_recreated_exit_zero          GREEN pin
  AC-F6.3  test_ac_f6_3_fresh_install_fallback_byte_identical             GREEN pin
             (uv-side fresh-path pin lives in the f6_1 uv leg's FIRST run: exact
              "Creating the venv with uv" step-2 line asserted there).
  AC-F6.4  test_ac_f6_4_skip_venv_leg_byte_identical                      GREEN pin
           test_ac_f6_4_update_leg_byte_identical_keeps_venv              GREEN pin
             (also kills an --update that starts re-running pip: recorders stay silent)
  AC-F6.5  re-arm asserted RED-side inside test_ac_f6_1_plain_reinstall_with_uv_...
             (the phase-3 death is at step 2, BEFORE the step-5 re-arm), and
           test_ac_f6_5_reinstall_rearms_trigger_and_reaches_doctor      GREEN pin
             — crontab shim records a write during the SECOND plain run; the doctor
               step is evidenced on the --update flow over the same converged home
               (the plain flow's final section is a frozen seam that never execs
               doctor; only step 2 may change, so doctor-reached is pinned where
               install.sh actually reaches it, with a fake venv/bin/loom as in the
               wave-b goal).
  seams    test_seam_p3_uninstall_empty_removes_behavioral_smoke          GREEN pin
           test_seam_p3_trigger_env_block_four_line_shape                 GREEN pin
             — behavioral pins of the frozen phase-3 sections (byte-hashing the
               script would be brittle; these assert the frozen OBSERVABLES).

Toolchain seams (hard constraint 3 — runnable without network when uv is absent):
the fallback legs shim ``python3`` itself with a faithful recorder (real ``python3 -m
venv`` + a real editable pip install would need the network for the build backend);
the recorder mirrors the venv module's semantics the goal relies on — idempotent over
an existing dir, KEEPS an existing bin/python. The uv legs run the real toolchain end
to end (network allowed there; skip-marked when uv is absent, and LOOM_GATE=1 turns
that skip into a failure). Every shim carries an ABSOLUTE interpreter shebang so it
runs under the controlled minimal PATH.

HOST SAFETY: every PATH handed to install.sh is shims + an allowlist tail with NO
crontab and NO systemctl reachable (asserted, wave-c discipline). --update legs run
from a temp clone of this checkout (never pulling the real repo) with the LIVE
working-tree install.sh copied in, so the goal always gates the code under build.

Run: .venv/bin/python -m pytest test/waves/g/test_goal_e11_reinstall.py -q
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _goalkit as kit  # noqa: E402

INSTALL = kit.REPO_ROOT / "client" / "install.sh"
CRON_TAG = "# loom-process"

HAVE_UV = shutil.which("uv") is not None
LOOM_GATE = os.environ.get("LOOM_GATE") == "1"


def _require_uv() -> None:
    """The uv legs are the VERIFIED-failure boundary; a missing binary must not be
    able to green-light the gate by skipping them away (phase-3 e2e discipline)."""
    if HAVE_UV:
        return
    reason = "this leg needs the real uv binary (the verified re-install failure)"
    if LOOM_GATE:
        pytest.fail(f"LOOM_GATE=1 forbids skipping — {reason}")
    pytest.skip(reason)


# --------------------------------------------------------------------------- #
# Controlled PATH construction (host-safe on every platform)                   #
# --------------------------------------------------------------------------- #
def _safe_bin(tmp: Path) -> Path:
    """Allowlist tail: the essentials, NO crontab, NO systemctl, NO uv."""
    d = tmp / "goal_safe_bin"
    d.mkdir(exist_ok=True)
    for name in kit.SAFE_BIN_NAMES:
        target = shutil.which(name)
        if target and not (d / name).exists():
            (d / name).symlink_to(target)
    return d


def _uv_dir(tmp: Path) -> Path:
    """A COPY of the real uv in a controlled temp dir (wave-d toolbox rationale:
    never assume /opt/homebrew; any homebrew string later found is a baked-in guess).
    Callers run ``_require_uv()`` first."""
    d = tmp / "goal_uv"
    d.mkdir(exist_ok=True)
    dst = d / "uv"
    if not dst.exists():
        real = shutil.which("uv")
        assert real, "caller must gate on _require_uv() first"
        shutil.copy2(real, dst)
    return d


def _assert_host_safe(path: str, shims: Path) -> None:
    """The wave-c lesson: the developer's schedulers must be UNREACHABLE."""
    assert shutil.which("crontab", path=path) == str(shims / "crontab"), (
        "the only reachable crontab must be the recorder shim, never the host's")
    assert shutil.which("systemctl", path=path) is None, (
        "the host systemctl must be unreachable: install.sh would arm a real "
        "user timer against a temp LOOM_HOME that pytest then deletes")


# --------------------------------------------------------------------------- #
# Record-replay shims (absolute-interpreter shebangs: they must run under the  #
# controlled minimal PATH, where `/usr/bin/env python3` is the fake below)     #
# --------------------------------------------------------------------------- #
def _crontab_shim(shims: Path, state_file: Path) -> Path:
    """File-backed crontab that faithfully distinguishes -l / - / -r (wave-d
    semantics: "no crontab" is the ABSENCE of the state file, -r deletes it)."""
    body = f"""#!{sys.executable}
import pathlib, sys
state = pathlib.Path({str(state_file)!r})
calls = pathlib.Path({str(state_file)!r} + ".calls")
calls.parent.mkdir(parents=True, exist_ok=True)
with calls.open("a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if "-l" in args:
    if state.exists():
        sys.stdout.write(state.read_text())
        sys.exit(0)
    sys.stderr.write("crontab: no crontab for user\\n")
    sys.exit(1)
if "-r" in args:
    if state.exists():
        state.unlink()
        sys.exit(0)
    sys.stderr.write("crontab: no crontab for user\\n")
    sys.exit(1)
if "-" in args:
    state.write_text(sys.stdin.read())
    sys.exit(0)
plain = [a for a in args if not a.startswith("-")]
if len(plain) == 1:
    state.write_text(pathlib.Path(plain[0]).read_text())
    sys.exit(0)
sys.stderr.write("crontab shim: unsupported argv: %r\\n" % (args,))
sys.exit(64)
"""
    p = shims / "crontab"
    p.write_text(body, encoding="utf-8")
    p.chmod(0o755)
    return p


def _recorder_source(calls: Path, tag: str) -> str:
    """Source of a venv-tool recorder (fake bin/python or bin/pip): appends
    ``<tag> <argv>`` to the shared call log and exits 0."""
    return (
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        f"calls = pathlib.Path({str(calls)!r})\n"
        'with calls.open("a") as fh:\n'
        f'    fh.write({tag!r} + " " + " ".join(sys.argv[1:]) + "\\n")\n'
        'if "--version" in sys.argv or "-V" in sys.argv:\n'
        '    sys.stdout.write("Python 3.12.0\\n")\n'
        "sys.exit(0)\n"
    )


def _fake_python3(pydir: Path, calls: Path) -> Path:
    """The constraint-3 offline seam for the FALLBACK legs: a python3 whose only
    honored subcommand is ``-m venv`` and which mirrors the real venv module's
    contract the goal depends on — tolerant of an existing directory, and KEEPING an
    existing bin/python (so the st_ino assertion detects rm-and-recreate, exactly as
    it would against the real module)."""
    pydir.mkdir(parents=True, exist_ok=True)
    py_src = _recorder_source(calls, "venv-python")
    pip_src = _recorder_source(calls, "venv-pip")
    body = f"""#!{sys.executable}
import pathlib, sys
calls = pathlib.Path({str(calls)!r})
with calls.open("a") as fh:
    fh.write("python3 " + " ".join(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if args[:2] == ["-m", "venv"]:
    rest = [a for a in args[2:] if not a.startswith("-")]
    if "--help" in args or not rest:
        sys.exit(0)
    target = pathlib.Path(rest[0])
    (target / "bin").mkdir(parents=True, exist_ok=True)
    for name, src in (("python", {py_src!r}), ("pip", {pip_src!r})):
        f = target / "bin" / name
        if not f.exists():
            f.write_text(src)
            f.chmod(0o755)
    (target / "pyvenv.cfg").write_text("home = goal-fake-venv\\n")
    sys.exit(0)
if "--version" in args or "-V" in args:
    sys.stdout.write("Python 3.12.0\\n")
sys.exit(0)
"""
    p = pydir / "python3"
    p.write_text(body, encoding="utf-8")
    p.chmod(0o755)
    return p


def _fabricate_healthy_venv(loomhome: Path, calls: Path) -> Path:
    """A pre-existing HEALTHY venv per the frozen interface's criterion
    (``$LOOM_HOME/venv/bin/python`` executable), whose tools are recorders."""
    bin_dir = loomhome / "venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, tag in (("python", "venv-python"), ("pip", "venv-pip")):
        f = bin_dir / name
        f.write_text(_recorder_source(calls, tag), encoding="utf-8")
        f.chmod(0o755)
    (loomhome / "venv" / "pyvenv.cfg").write_text("home = goal-fabricated\n")
    return bin_dir / "python"


def _fake_loom(loomhome: Path, log: Path) -> Path:
    """Stand-in venv console script for the doctor step (wave-b --update technique:
    with no real editable install in the temp home there is no real ``loom``)."""
    p = loomhome / "venv" / "bin" / "loom"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        f"log = pathlib.Path({str(log)!r})\n"
        'with log.open("a") as fh:\n'
        '    fh.write(" ".join(sys.argv[1:]) + "\\n")\n'
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    p.chmod(0o755)
    return p


# --------------------------------------------------------------------------- #
# Plumbing                                                                     #
# --------------------------------------------------------------------------- #
def _homes(tmp: Path) -> tuple[Path, Path]:
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    return home, home / ".loom"


def _run_install(args: list[str], *, home: Path, loomhome: Path, path: str,
                 extra_env: dict | None = None, script: Path = INSTALL,
                 timeout: int = 180) -> subprocess.CompletedProcess:
    # D16 accessory: step 1b would download gitleaks (no network in tests); this wave
    # is about step 2, so the gitleaks step is skipped through its own seam.
    env = {**os.environ, "HOME": str(home), "LOOM_HOME": str(loomhome),
           "PATH": path, "LOOM_SKIP_GITLEAKS": "1"}
    env.pop("LOOM_SKIP_VENV", None)   # the venv path IS the surface under test
    env.pop("LOOM_SKIP_TRIGGER", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(["bash", str(script), *args], env=env,
                          capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=timeout)


def _fallback_env(tmp: Path) -> tuple[Path, Path, str, Path, Path]:
    """home, loomhome, PATH (fake python3 + shims + tail, NO uv), toolchain call
    log, crontab state file."""
    home, loomhome = _homes(tmp)
    shims = kit.shim_dir(tmp)
    state = tmp / "cron_state.txt"
    _crontab_shim(shims, state)
    calls = tmp / "toolchain_calls.log"
    calls.touch()
    pyfake = tmp / "pyfake"
    _fake_python3(pyfake, calls)
    path = os.pathsep.join([str(pyfake), str(shims), str(_safe_bin(tmp))])
    _assert_host_safe(path, shims)
    assert shutil.which("uv", path=path) is None, "the fallback legs must hide uv"
    assert shutil.which("python3", path=path) == str(pyfake / "python3")
    return home, loomhome, path, calls, state


def _uv_env(tmp: Path) -> tuple[Path, Path, str, Path]:
    """home, loomhome, PATH (shims + tail + controlled REAL uv), crontab state."""
    home, loomhome = _homes(tmp)
    shims = kit.shim_dir(tmp)
    state = tmp / "cron_state.txt"
    _crontab_shim(shims, state)
    uvdir = _uv_dir(tmp)
    path = os.pathsep.join([str(shims), str(_safe_bin(tmp)), str(uvdir)])
    _assert_host_safe(path, shims)
    assert shutil.which("uv", path=path) == str(uvdir / "uv")
    return home, loomhome, path, state


def _clone_with_live_installer(tmp: Path) -> Path:
    """Temp clone of this checkout for the --update legs, with the WORKING-TREE
    install.sh copied over the committed one, so the goal gates the code under
    build; --update's fetch/pull talk only to the local checkout, never the net."""
    work = tmp / "work"
    subprocess.run(["git", "clone", "-q", str(kit.REPO_ROOT), str(work)],
                   check=True, timeout=300)
    dst = work / "client" / "install.sh"
    shutil.copyfile(INSTALL, dst)
    dst.chmod(0o755)
    return work


def _editable_calls(calls: Path) -> list[str]:
    """Recorder-log lines that evidence an editable install run."""
    return [l for l in calls.read_text(encoding="utf-8").splitlines()
            if l.startswith(("venv-python", "venv-pip")) and "install" in l]


def _cron_calls(state: Path) -> list[str]:
    f = Path(str(state) + ".calls")
    return f.read_text(encoding="utf-8").splitlines() if f.exists() else []


def _backups(loomhome: Path) -> list[Path]:
    d = loomhome / "backup"
    return sorted(d.glob("crontab.*")) if d.is_dir() else []


# =========================================================================== #
# AC-F6.1 — plain --yes over a healthy existing venv: keep + editable re-run   #
# =========================================================================== #
def test_ac_f6_1_plain_reinstall_with_uv_keeps_healthy_venv(tmp_path):
    """RED today (the VERIFIED failure): the second plain --yes exits 2 at step 2
    ("Use the --clear flag") BEFORE the trigger re-arm. Must become: exit 0, venv
    KEPT (bin/python symlink inode unchanged — lstat, so rm-and-recreate cannot
    hide behind the base interpreter's inode), trigger re-armed (AC-F6.5's
    death-before-step-5 kill, asserted at this same real boundary)."""
    _require_uv()
    home, loomhome, path, state = _uv_env(tmp_path)

    first = _run_install(["--yes"], home=home, loomhome=loomhome, path=path,
                         timeout=600)
    assert first.returncode == 0, (
        f"fresh uv install must succeed (network/uv cache needed):\n"
        f"{first.stdout}\n{first.stderr}")
    # AC-F6.3 uv-side pin: the fresh uv path stays byte-identical to today's.
    assert (f"2/5  Creating the venv with uv in {loomhome}/venv and installing "
            "the package (editable, client extra) ...") in first.stdout.splitlines()
    py = loomhome / "venv" / "bin" / "python"
    assert py.exists() and os.access(py, os.X_OK)
    ino_before = os.lstat(py).st_ino
    writes_before = _cron_calls(state).count("-")

    second = _run_install(["--yes"], home=home, loomhome=loomhome, path=path,
                          timeout=600)
    assert second.returncode == 0, (
        "AC-F6.1: plain --yes over a healthy existing venv must exit 0 — today "
        "uv refuses the existing dir and the re-install dies with exit 2 before "
        f"the trigger re-arm (RUNBOOK §7):\n{second.stdout}\n{second.stderr}")
    assert os.lstat(py).st_ino == ino_before, (
        "AC-F6.1: the healthy venv must be KEPT — a changed bin/python inode is "
        "rm-and-recreate masquerading as idempotency")
    # AC-F6.5 (uv leg): the re-install re-armed the trigger — the shimmed crontab
    # recorded a new write and the block is present.
    assert _cron_calls(state).count("-") > writes_before, (
        "AC-F6.5: the re-install must reach step 5 and re-arm the trigger")
    assert state.exists() and CRON_TAG in state.read_text(encoding="utf-8")
    # The kept venv is still a WORKING install (kills keep-but-corrupt).
    probe = subprocess.run([str(py), "-c", "import loom"], capture_output=True,
                           text=True, timeout=60, cwd=str(tmp_path))
    assert probe.returncode == 0, (
        f"the kept venv must still import the editable package:\n{probe.stderr}")


def test_ac_f6_1_plain_reinstall_fallback_keeps_venv_and_reruns_editable(tmp_path):
    """GREEN pin (hard constraint 4): the uv-absent fallback is already idempotent
    and must stay so. Kills keep-but-skip: AC-F6.1's "re-runs the editable install"
    is asserted from the venv recorders' call log."""
    home, loomhome, path, calls, state = _fallback_env(tmp_path)

    first = _run_install(["--yes"], home=home, loomhome=loomhome, path=path)
    assert first.returncode == 0, first.stdout + first.stderr
    py = loomhome / "venv" / "bin" / "python"
    assert py.exists() and os.access(py, os.X_OK), "first install must build the venv"
    ino_before = os.lstat(py).st_ino

    calls.write_text("")  # only the SECOND run's toolchain activity matters below
    second = _run_install(["--yes"], home=home, loomhome=loomhome, path=path)
    assert second.returncode == 0, (
        f"AC-F6.1 (fallback leg): plain --yes over a healthy venv must exit 0:\n"
        f"{second.stdout}\n{second.stderr}")
    assert os.lstat(py).st_ino == ino_before, (
        "AC-F6.1: the healthy venv's bin/python must be the SAME file — the "
        "fallback path must not regress into rm-and-recreate")
    assert _editable_calls(calls), (
        "AC-F6.1: the re-install must RE-RUN the editable install (no venv tool "
        "recorded an 'install' invocation on the second run — that is --update's "
        f"keep-and-skip semantics, not F6.1's). Toolchain log:\n{calls.read_text()}")


# =========================================================================== #
# AC-F6.2 — plain --yes over a BROKEN venv dir: recreate + exit 0              #
# =========================================================================== #
def _broken_venv(loomhome: Path) -> Path:
    """venv dir present, no usable bin/python (the frozen interface's 'broken')."""
    venv = loomhome / "venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    (venv / "lib.junk").write_text("residue of a half-created venv\n")
    return venv


def test_ac_f6_2_broken_venv_with_uv_recreated_exit_zero(tmp_path):
    """RED today: uv refuses the non-empty dir (verified: exit 2 also for a dir
    with no pyvenv.cfg). Must become: recreated venv, editable installed, exit 0."""
    _require_uv()
    home, loomhome, path, state = _uv_env(tmp_path)
    venv = _broken_venv(loomhome)

    proc = _run_install(["--yes"], home=home, loomhome=loomhome, path=path,
                        timeout=600)
    assert proc.returncode == 0, (
        "AC-F6.2: plain --yes over a broken venv dir must recreate it and exit 0 "
        f"— today uv refuses the existing dir (exit 2):\n{proc.stdout}\n{proc.stderr}")
    py = venv / "bin" / "python"
    assert py.exists() and os.access(py, os.X_OK), (
        "AC-F6.2: the broken venv must come out recreated with a usable bin/python")
    assert (venv / "bin" / "loom").exists(), (
        "AC-F6.2: the editable install must have run into the recreated venv "
        "(no console script found)")


def test_ac_f6_2_broken_venv_fallback_recreated_exit_zero(tmp_path):
    """GREEN pin: the fallback path already tolerates the broken dir; recreation
    (a ``-m venv`` invocation on the dir) and the editable install are asserted
    from the recorders."""
    home, loomhome, path, calls, state = _fallback_env(tmp_path)
    venv = _broken_venv(loomhome)

    proc = _run_install(["--yes"], home=home, loomhome=loomhome, path=path)
    assert proc.returncode == 0, (
        f"AC-F6.2 (fallback leg): broken venv must not fail the install:\n"
        f"{proc.stdout}\n{proc.stderr}")
    py = venv / "bin" / "python"
    assert py.exists() and os.access(py, os.X_OK)
    logged = calls.read_text(encoding="utf-8").splitlines()
    assert any(l.startswith("python3 -m venv") and str(venv) in l for l in logged), (
        f"AC-F6.2: a broken venv dir must be RECREATED (no venv creation was "
        f"invoked on {venv}):\n{logged}")
    assert _editable_calls(calls), (
        "AC-F6.2: the recreated venv must receive the editable install")


# =========================================================================== #
# AC-F6.3 — fresh install (no venv dir): byte-identical to today's path        #
# =========================================================================== #
def test_ac_f6_3_fresh_install_fallback_byte_identical(tmp_path):
    """GREEN regression pin: behavior equivalence of the fresh path with uv hidden
    — today's exact step-2 message, venv creation BEFORE the editable install,
    exit 0. (The uv-side fresh pin is the first run of the f6_1 uv leg.)"""
    home, loomhome, path, calls, state = _fallback_env(tmp_path)
    assert not (loomhome / "venv").exists()

    proc = _run_install(["--yes"], home=home, loomhome=loomhome, path=path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (f"2/5  uv is not available; using python3 -m venv in {loomhome}/venv "
            "...") in proc.stdout.splitlines(), (
        f"AC-F6.3: the fresh fallback path's step-2 message must stay byte-"
        f"identical to today's:\n{proc.stdout}")
    py = loomhome / "venv" / "bin" / "python"
    assert py.exists() and os.access(py, os.X_OK)
    logged = calls.read_text(encoding="utf-8").splitlines()
    i_venv = next((i for i, l in enumerate(logged)
                   if l.startswith("python3 -m venv") and str(loomhome / "venv") in l),
                  None)
    assert i_venv is not None, f"fresh install must create the venv:\n{logged}"
    editable = [i for i, l in enumerate(logged)
                if l.startswith(("venv-python", "venv-pip")) and "install" in l]
    assert editable, f"fresh install must run the editable install:\n{logged}"
    assert i_venv < editable[0], (
        "fresh install order must stay: create venv, THEN editable install")


# =========================================================================== #
# AC-F6.4 — --update and LOOM_SKIP_VENV=1 paths byte-identical to today's      #
# =========================================================================== #
def test_ac_f6_4_skip_venv_leg_byte_identical(tmp_path):
    """GREEN pin: the LOOM_SKIP_VENV=1 seam (every other wave goal stands on it)
    keeps today's exact message and creates NO venv."""
    home, loomhome = _homes(tmp_path)
    shims = kit.shim_dir(tmp_path)
    state = tmp_path / "cron_state.txt"
    _crontab_shim(shims, state)
    path = os.pathsep.join([str(shims), str(_safe_bin(tmp_path))])
    _assert_host_safe(path, shims)

    proc = _run_install(["--yes"], home=home, loomhome=loomhome, path=path,
                        extra_env={"LOOM_SKIP_VENV": "1"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "2/5  LOOM_SKIP_VENV=1 → skipping venv creation." in \
        proc.stdout.splitlines(), (
        f"AC-F6.4: the LOOM_SKIP_VENV=1 step-2 message must stay byte-identical:\n"
        f"{proc.stdout}")
    assert not (loomhome / "venv").exists(), (
        "AC-F6.4: LOOM_SKIP_VENV=1 must keep creating NO venv")


def test_ac_f6_4_update_leg_byte_identical_keeps_venv(tmp_path):
    """GREEN pin: --update over an existing venv keeps today's exact step-2
    message, keeps the venv (same inode), and runs NO editable install (that is
    precisely what separates --update's step 2 from F6.1's — an implementation
    that merges the two paths breaks byte-identity here)."""
    home, loomhome = _homes(tmp_path)
    shims = kit.shim_dir(tmp_path)
    state = tmp_path / "cron_state.txt"
    _crontab_shim(shims, state)
    path = os.pathsep.join([str(shims), str(_safe_bin(tmp_path))])
    _assert_host_safe(path, shims)

    calls = tmp_path / "toolchain_calls.log"
    calls.touch()
    py = _fabricate_healthy_venv(loomhome, calls)
    ino_before = os.lstat(py).st_ino
    _fake_loom(loomhome, tmp_path / "doctor.log")   # --update ends in exec loom doctor
    work = _clone_with_live_installer(tmp_path)

    proc = _run_install(["--yes", "--update"], home=home, loomhome=loomhome,
                        path=path, script=work / "client" / "install.sh",
                        timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert ("2/5  --update → keeping the existing venv (the editable install "
            "picks the pull up).") in proc.stdout.splitlines(), (
        f"AC-F6.4: the --update step-2 message must stay byte-identical:\n"
        f"{proc.stdout}")
    assert os.lstat(py).st_ino == ino_before, "--update must keep the venv"
    assert not _editable_calls(calls), (
        "AC-F6.4: --update's step 2 runs NO editable install today (the pull is "
        f"picked up by the existing one); recorded:\n{calls.read_text()}")


# =========================================================================== #
# AC-F6.5 — the re-install re-arms the trigger and reaches the doctor step     #
# =========================================================================== #
def test_ac_f6_5_reinstall_rearms_trigger_and_reaches_doctor(tmp_path):
    """GREEN pin on the fallback boundary (the RED half of this AC — dying at
    step 2 before the re-arm — is asserted in the uv F6.1 leg). The second plain
    --yes must WRITE the crontab again (shim records the '-' install call), leave
    the tagged block armed, and complete past step 5; the doctor step is then
    evidenced where install.sh actually reaches doctor — the --update flow over
    the same converged home, with a fake venv/bin/loom (wave-b technique)."""
    home, loomhome, path, calls, state = _fallback_env(tmp_path)

    first = _run_install(["--yes"], home=home, loomhome=loomhome, path=path)
    assert first.returncode == 0, first.stdout + first.stderr
    assert (loomhome / "venv" / "bin" / "python").exists()
    writes_before = _cron_calls(state).count("-")

    second = _run_install(["--yes"], home=home, loomhome=loomhome, path=path)
    assert second.returncode == 0, (
        f"AC-F6.5: the plain re-install must not die before the trigger:\n"
        f"{second.stdout}\n{second.stderr}")
    assert _cron_calls(state).count("-") > writes_before, (
        "AC-F6.5: the re-install must invoke the (shimmed) crontab to re-arm "
        f"the trigger; calls:\n{_cron_calls(state)}")
    content = state.read_text(encoding="utf-8")
    assert CRON_TAG in content and "/venv/bin/loom process --quiet" in content, (
        f"AC-F6.5: the re-armed crontab must carry the loom block:\n{content}")
    assert "loom installed in" in second.stdout, (
        "AC-F6.5: the re-install must run to completion (final message), not "
        f"stop after step 2:\n{second.stdout}")

    # Doctor reached, over the SAME home whose venv the re-install just kept.
    doctor_log = tmp_path / "doctor.log"
    _fake_loom(loomhome, doctor_log)
    work = _clone_with_live_installer(tmp_path)
    upd = _run_install(["--yes", "--update"], home=home, loomhome=loomhome,
                       path=path, script=work / "client" / "install.sh",
                       timeout=300)
    assert upd.returncode == 0, upd.stdout + upd.stderr
    assert doctor_log.exists() and "doctor" in doctor_log.read_text(), (
        "AC-F6.5: the install flow must reach the doctor step")


# =========================================================================== #
# Frozen phase-3 seams — behavioral smoke pins (never byte-hashes)             #
# =========================================================================== #
def test_seam_p3_uninstall_empty_removes_behavioral_smoke(tmp_path):
    """p3-uninstall-empty-removes: a loom-only crontab is REMOVED entirely
    (`crontab -r`), backup first — the wave-d observable, re-pinned here so an E11
    change outside step 2 cannot drift the frozen uninstall section."""
    home, loomhome = _homes(tmp_path)
    shims = kit.shim_dir(tmp_path)
    state = tmp_path / "cron_state.txt"
    _crontab_shim(shims, state)
    path = os.pathsep.join([str(shims), str(_safe_bin(tmp_path))])
    _assert_host_safe(path, shims)

    ins = _run_install(["--yes"], home=home, loomhome=loomhome, path=path,
                       extra_env={"LOOM_SKIP_VENV": "1"})
    assert ins.returncode == 0, ins.stdout + ins.stderr
    assert state.exists() and CRON_TAG in state.read_text(encoding="utf-8")
    pre = state.read_text(encoding="utf-8")

    uni = _run_install(["--uninstall-trigger"], home=home, loomhome=loomhome,
                       path=path)
    assert uni.returncode == 0, uni.stdout + uni.stderr
    assert not state.exists(), (
        "frozen seam p3-uninstall-empty-removes: a loom-only crontab must be "
        "REMOVED, never re-installed empty")
    assert "-r" in _cron_calls(state), (
        f"removal must go through `crontab -r`: {_cron_calls(state)}")
    want = pre.rstrip("\n")
    assert any(b.read_text(encoding="utf-8").rstrip("\n") == want
               for b in _backups(loomhome)), (
        f"the removal must be preceded by a matching backup in {_backups(loomhome)}")


def test_seam_p3_trigger_env_block_four_line_shape(tmp_path):
    """p3-trigger-env-block: the armed cron block keeps its 4-line shape — tag,
    LOOM_HOME=, PATH= (our marker FIRST component), the */15 command — in order."""
    home, loomhome = _homes(tmp_path)
    shims = kit.shim_dir(tmp_path)
    state = tmp_path / "cron_state.txt"
    _crontab_shim(shims, state)
    path = os.pathsep.join([str(shims), str(_safe_bin(tmp_path))])
    _assert_host_safe(path, shims)

    ins = _run_install(["--yes"], home=home, loomhome=loomhome, path=path,
                       extra_env={"LOOM_SKIP_VENV": "1"})
    assert ins.returncode == 0, ins.stdout + ins.stderr
    lines = state.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4, (
        f"frozen seam p3-trigger-env-block: the block is exactly 4 lines:\n{lines}")
    assert lines[0] == CRON_TAG
    assert lines[1] == f"LOOM_HOME={loomhome}"
    assert lines[2].startswith("PATH="), lines
    first_component = lines[2][len("PATH="):].split(":", 1)[0]
    assert first_component == f"{loomhome}/venv/bin", (
        f"the PATH line's identity anchor is its FIRST component: {lines[2]}")
    assert lines[3] == f"*/15 * * * * {loomhome}/venv/bin/loom process --quiet"
