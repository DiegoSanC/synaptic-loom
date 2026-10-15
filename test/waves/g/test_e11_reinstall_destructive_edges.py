"""ADDITIVE (E11 implementer) — destructive edges of the p4-reinstall-idempotent
recreate path. Extends test/waves/g/, weakens nothing.

The frozen goal pins the "broken venv" case as a directory holding ``bin/`` and some
junk. Making that case converge required install.sh's uv branch to REMOVE the unusable
directory before ``uv venv`` (uv will not build into an existing one). This module pins
the edges of that removal — the risk surface of a shell ``rm -rf`` in an installer:

  * a STALE venv (pyvenv.cfg present, ``bin/python`` a dangling symlink) is recreated —
    the "-x" health test must judge the interpreter, not the presence of a pyvenv.cfg;
  * ``$LOOM_HOME/venv`` being a SYMLINK to an unrelated tree unlinks the symlink and
    leaves the target INTACT — the removal must never follow a link out of LOOM_HOME;
  * ``$LOOM_HOME/venv`` being a plain FILE is replaced rather than fataling.

Same host-safety discipline as the frozen goal: a controlled minimal PATH with NO
crontab and NO systemctl reachable, plus LOOM_SKIP_TRIGGER=1, so no scheduler on this
machine can be touched (macOS crontab(1) writes hang for 30s+ under sandbox). These legs
drive the REAL uv binary — they are skip-marked when it is absent, and LOOM_GATE=1 turns
that skip into a failure, exactly as the frozen goal does.

Run: PYTHONPATH=client:. .venv/bin/python -m pytest \
       test/waves/g/test_e11_reinstall_destructive_edges.py -q
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _goalkit as kit  # noqa: E402

# Reuse the frozen goal's own plumbing (read-only import by path: no re-collection of
# its tests, and the shims stay defined in exactly one place).
_GOAL_PATH = Path(__file__).with_name("test_goal_e11_reinstall.py")
_spec = importlib.util.spec_from_file_location("_e11_goal_helpers", _GOAL_PATH)
assert _spec and _spec.loader
goal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(goal)


def _uv_home(tmp: Path) -> tuple[Path, Path, str, Path]:
    """The frozen goal's uv environment (shims + allowlist tail + a controlled copy of
    the real uv), with its host-safety assertions already applied."""
    goal._require_uv()
    return goal._uv_env(tmp)


def _install(args, *, home, loomhome, path, **kw):
    """Plain install with the scheduler step switched off: these legs are about step 2,
    and LOOM_SKIP_TRIGGER=1 is a second belt over the PATH already hiding crontab."""
    extra = {"LOOM_SKIP_TRIGGER": "1", "LOOM_SKIP_GITLEAKS": "1"}  # D16: no download
    extra.update(kw.pop("extra_env", None) or {})
    env = {**os.environ}
    env.pop("LOOM_SKIP_VENV", None)
    return subprocess.run(
        ["bash", str(goal.INSTALL), *args],
        env={**env, "HOME": str(home), "LOOM_HOME": str(loomhome), "PATH": path, **extra},
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
        timeout=kw.pop("timeout", 600),
    )


def _assert_converged(proc, loomhome: Path) -> None:
    py = loomhome / "venv" / "bin" / "python"
    assert proc.returncode == 0, (
        f"the re-install must converge the venv and exit 0:\n{proc.stdout}\n{proc.stderr}")
    assert py.is_file() and os.access(py, os.X_OK), (
        f"the venv must come out with a usable bin/python:\n{proc.stdout}")
    assert (loomhome / "venv" / "bin" / "loom").exists(), (
        f"the editable install must have run into the venv:\n{proc.stdout}")


def test_stale_venv_with_dangling_python_is_recreated(tmp_path):
    """A venv that LOOKS like one (pyvenv.cfg) but whose interpreter is a dangling
    symlink is broken, and must be recreated. Kills a health check that trusts
    pyvenv.cfg — or one using `-e`/`-L`, which a dangling symlink satisfies."""
    home, loomhome, path, _state = _uv_home(tmp_path)
    bin_dir = loomhome / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    (loomhome / "venv" / "pyvenv.cfg").write_text("home = /nonexistent\n")
    (bin_dir / "python").symlink_to("/nonexistent/python")

    proc = _install(["--yes"], home=home, loomhome=loomhome, path=path)
    _assert_converged(proc, loomhome)
    assert not (loomhome / "venv" / "bin" / "python").is_symlink() or \
        (loomhome / "venv" / "bin" / "python").resolve().exists(), \
        "the recreated interpreter must actually resolve"


def test_venv_symlink_is_unlinked_and_its_target_survives(tmp_path):
    """HOST SAFETY: with $LOOM_HOME/venv a symlink pointing outside LOOM_HOME, the
    recreate must unlink the SYMLINK and leave the target tree untouched. A removal
    that followed the link would delete an unrelated directory on the operator's disk."""
    home, loomhome, path, _state = _uv_home(tmp_path)
    precious = tmp_path / "precious"
    (precious / "deep").mkdir(parents=True)
    keep = precious / "deep" / "keep.txt"
    keep.write_text("must survive the re-install\n")
    loomhome.mkdir(parents=True, exist_ok=True)
    (loomhome / "venv").symlink_to(precious, target_is_directory=True)

    proc = _install(["--yes"], home=home, loomhome=loomhome, path=path)
    _assert_converged(proc, loomhome)
    assert keep.is_file() and keep.read_text() == "must survive the re-install\n", (
        "the removal followed the symlink and destroyed the tree it pointed at")
    assert not (loomhome / "venv").is_symlink(), (
        "the symlink must have been replaced by a real venv directory")


def test_venv_path_is_a_plain_file(tmp_path):
    """A regular file where the venv belongs must be replaced, not fatal."""
    home, loomhome, path, _state = _uv_home(tmp_path)
    loomhome.mkdir(parents=True, exist_ok=True)
    (loomhome / "venv").write_text("not a venv at all\n")

    proc = _install(["--yes"], home=home, loomhome=loomhome, path=path)
    _assert_converged(proc, loomhome)


def test_healthy_venv_is_never_removed(tmp_path):
    """The counterpart of the three above: a HEALTHY venv must take the keep branch —
    no removal, no recreation. Pinned on the real uv boundary by inode identity of a
    marker file that only a directory removal could take away."""
    home, loomhome, path, _state = _uv_home(tmp_path)

    first = _install(["--yes"], home=home, loomhome=loomhome, path=path)
    _assert_converged(first, loomhome)
    marker = loomhome / "venv" / "loom-goal-marker.txt"
    marker.write_text("planted between the two runs\n")

    second = _install(["--yes"], home=home, loomhome=loomhome, path=path)
    _assert_converged(second, loomhome)
    assert marker.is_file(), (
        "a healthy venv must be KEPT: the marker planted inside it disappeared, so the "
        "directory was removed and rebuilt")


def test_uv_gating_matches_the_frozen_goal():
    """Constraint 3, re-pinned for this module: with uv absent every leg here skips
    (so the suite needs no network), and LOOM_GATE=1 turns that skip into a failure —
    a missing binary must never be able to green-light the gate.

    Note the ``BaseException``: pytest's skip/fail raise ``OutcomeException``, which
    descends from BaseException, not Exception."""
    assert goal.HAVE_UV == (shutil.which("uv") is not None)
    assert goal.LOOM_GATE == (os.environ.get("LOOM_GATE") == "1")
    assert kit.REPO_ROOT.is_dir()

    if goal.HAVE_UV:
        goal._require_uv()  # uv present: a no-op, the legs run for real
        return
    with pytest.raises(BaseException) as excinfo:
        goal._require_uv()
    outcome = type(excinfo.value).__name__
    assert outcome == ("Failed" if goal.LOOM_GATE else "Skipped"), (
        f"uv-absent gating must {'FAIL under LOOM_GATE=1' if goal.LOOM_GATE else 'skip'}, "
        f"got {outcome}")
