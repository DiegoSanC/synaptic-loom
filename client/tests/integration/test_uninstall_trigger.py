"""Integration tests for ``install.sh --uninstall-trigger`` (phase 2/3, P2.F6).

The headline contract (`p3-uninstall-empty-removes`) is a distinction the wave-b crontab
shim could not even express: "the crontab was REMOVED" vs "an empty crontab was
INSTALLED". Both render as nothing useful from ``crontab -l``, but only the second leaves
a registered-but-empty crontab behind — the residue the macOS rehearsal found (RUNBOOK §7,
finding 2) and the reason an operator following UNINSTALL.md still had to run ``crontab -r``
by hand.

So the shim here is file-backed with three distinct states:
  * state file ABSENT  → "you have no crontab" (``-l`` exits 1 with the real message)
  * state file PRESENT → its content is the crontab (``-`` replaces it)
  * ``-r``             → deletes the file, i.e. removes the crontab
Every invocation is appended to ``<state>.calls``, so a test can assert HOW the crontab was
changed, not only what it ended up containing.

Hard rules (wave-b lesson — this suite once wrote the developer's real crontab):
- ``HOME``/``LOOM_HOME`` always point at a tmpdir and ``LOOM_SKIP_VENV=1``.
- PATH is shims + an allowlist of symlinks; the real ``crontab``/``systemctl`` are
  unreachable, which every test asserts before running the installer.
- ``crontab -r`` is NEVER used as test cleanup: the state lives in a tmp file.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_SCRIPT = REPO_ROOT / "client" / "install.sh"
CRON_TAG = "# loom-process"

SAFE_TOOLS = (
    "python3", "git", "jq", "bash", "sh", "env", "cat", "mv", "cp", "mkdir", "rm",
    "date", "diff", "grep", "sed", "chmod", "uname", "dirname", "basename", "head",
    "tail", "tr", "wc", "sort", "touch", "ln", "sleep", "mktemp", "readlink", "id",
    "awk", "cut", "find",
)

FOREIGN = [
    "# nightly backup",
    "PATH=/foreign-keep/bin:/usr/bin",
    "0 3 * * * /opt/backup/run.sh",
]


# --------------------------------------------------------------------------- #
# Sandbox                                                                      #
# --------------------------------------------------------------------------- #
def _sandbox_path(tmp_path: Path) -> str:
    shims = tmp_path / "shims"
    shims.mkdir(exist_ok=True)
    safe = tmp_path / "safe_bin"
    safe.mkdir(exist_ok=True)
    for name in SAFE_TOOLS:
        target = shutil.which(name)
        if target and not (safe / name).exists():
            (safe / name).symlink_to(target)
    path = f"{shims}{os.pathsep}{safe}"
    # The only reachable schedulers must be the shims a test installed — never the host's.
    assert shutil.which("crontab", path=str(safe)) is None
    assert shutil.which("systemctl", path=path) is None
    return path


def _crontab_shim(tmp_path: Path, state: Path) -> None:
    """File-backed ``crontab`` that distinguishes ``-l`` / ``-`` / ``-r``."""
    shim = tmp_path / "shims" / "crontab"
    shim.parent.mkdir(exist_ok=True)
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        f"state = pathlib.Path({str(state)!r})\n"
        f"calls = pathlib.Path({str(state)!r} + '.calls')\n"
        "args = sys.argv[1:]\n"
        "with calls.open('a') as fh:\n"
        "    fh.write(' '.join(args) + '\\n')\n"
        "if '-l' in args:\n"
        "    if state.exists():\n"
        "        sys.stdout.write(state.read_text())\n"
        "        sys.exit(0)\n"
        "    sys.stderr.write('crontab: no crontab for tester\\n')\n"
        "    sys.exit(1)\n"
        "if '-r' in args:\n"
        "    if state.exists():\n"
        "        state.unlink()\n"
        "        sys.exit(0)\n"
        "    sys.stderr.write('crontab: no crontab for tester\\n')\n"
        "    sys.exit(1)\n"
        "if '-' in args:\n"
        "    state.write_text(sys.stdin.read())\n"
        "    sys.exit(0)\n"
        "sys.stderr.write('unsupported argv\\n')\n"
        "sys.exit(64)\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)


def _calls(state: Path) -> list[str]:
    path = Path(str(state) + ".calls")
    return path.read_text().splitlines() if path.exists() else []


def _run(tmp_path: Path, home: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "HOME": str(home),
        "LOOM_HOME": str(home / ".loom"),
        "LOOM_SKIP_VENV": "1", "LOOM_SKIP_GITLEAKS": "1",
        "PATH": _sandbox_path(tmp_path),
    }
    return subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--yes", *args],
        env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=180,
    )


def _backups(home: Path) -> list[Path]:
    d = home / ".loom" / "backup"
    return sorted(d.glob("crontab.*")) if d.is_dir() else []


@pytest.fixture
def home(tmp_path) -> Path:
    h = tmp_path / "home"
    h.mkdir(exist_ok=True)
    return h


@pytest.fixture
def cron_state(tmp_path) -> Path:
    state = tmp_path / "cron.txt"
    _crontab_shim(tmp_path, state)
    return state


# --------------------------------------------------------------------------- #
# AC-F6.1 — a loom-only crontab is REMOVED, not emptied                        #
# --------------------------------------------------------------------------- #
def test_uninstall_removes_a_loom_only_crontab_entirely(tmp_path, home, cron_state):
    """AC-F6.1. Installing the filtered (empty) content back registers an empty crontab:
    `crontab -l` then reports a crontab that exists and holds nothing, some cron
    implementations start mailing about it, and "did loom leave anything behind?" answers
    yes. Uninstall means the file goes away."""
    assert _run(tmp_path, home).returncode == 0
    assert CRON_TAG in cron_state.read_text()

    proc = _run(tmp_path, home, "--uninstall-trigger")

    assert proc.returncode == 0, proc.stderr
    assert not cron_state.exists(), (
        f"an empty crontab was installed instead of removed: "
        f"{cron_state.read_text()!r}")
    assert "-r" in _calls(cron_state)


def test_uninstall_of_a_whitespace_only_remainder_also_removes(tmp_path, home, cron_state):
    """Blank lines around the block are not "foreign entries": what would be installed back
    is still an empty crontab."""
    assert _run(tmp_path, home).returncode == 0
    cron_state.write_text("\n   \n" + cron_state.read_text() + "\n\t\n")

    proc = _run(tmp_path, home, "--uninstall-trigger")

    assert proc.returncode == 0, proc.stderr
    assert not cron_state.exists()


def test_uninstall_backs_up_before_removing(tmp_path, home, cron_state):
    """AC-F6.3. `crontab -r` has no undo, so the backup discipline covers the REMOVAL too —
    it is the only copy of what was there."""
    assert _run(tmp_path, home).returncode == 0
    before = cron_state.read_text()

    assert _run(tmp_path, home, "--uninstall-trigger").returncode == 0

    backups = _backups(home)
    assert any(b.read_text().rstrip("\n") == before.rstrip("\n") for b in backups), (
        f"no backup matching the removed crontab: {[b.name for b in backups]}")


# --------------------------------------------------------------------------- #
# AC-F6.2 — foreign entries are rewritten, never removed                       #
# --------------------------------------------------------------------------- #
def test_uninstall_with_foreign_entries_rewrites_them_byte_exact(tmp_path, home, cron_state):
    """AC-F6.2. The crontab is shared: `crontab -r` here would destroy someone else's
    entries. Everything not ours survives byte for byte, in its original order."""
    cron_state.write_text("\n".join(FOREIGN) + "\n")
    assert _run(tmp_path, home).returncode == 0
    calls_before = len(_calls(cron_state))

    proc = _run(tmp_path, home, "--uninstall-trigger")

    assert proc.returncode == 0, proc.stderr
    assert cron_state.exists(), "a crontab with foreign entries must never be removed"
    assert cron_state.read_text().splitlines() == FOREIGN
    assert "-r" not in _calls(cron_state)[calls_before:]


def test_uninstall_removes_every_line_the_block_wrote(tmp_path, home, cron_state):
    """AC-F5.4. Not only the tag and the command: the environment assignments the block
    added are loom-owned too, and a leftover `PATH=` would keep steering every entry that
    follows it."""
    cron_state.write_text("\n".join(FOREIGN) + "\n")
    assert _run(tmp_path, home).returncode == 0
    block_lines = [ln for ln in cron_state.read_text().splitlines()
                   if ln.strip() and ln not in FOREIGN]
    assert len(block_lines) >= 3, f"the install wrote no block: {block_lines}"

    assert _run(tmp_path, home, "--uninstall-trigger").returncode == 0

    remaining = cron_state.read_text().splitlines()
    for line in block_lines:
        assert line not in remaining
    assert "loom" not in "\n".join(remaining)


# --------------------------------------------------------------------------- #
# AC-F6.4 — no crontab at all is a clean no-op                                 #
# --------------------------------------------------------------------------- #
def test_uninstall_without_any_crontab_is_a_clean_noop(tmp_path, home, cron_state):
    """AC-F6.4. `crontab -l` exiting 1 with "no crontab for <user>" means EMPTY, not
    broken: nothing to remove, nothing to write, nothing to back up, exit 0."""
    proc = _run(tmp_path, home, "--uninstall-trigger")

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert not cron_state.exists(), "a no-op must not conjure a crontab into being"
    calls = _calls(cron_state)
    assert "-" not in calls and "-r" not in calls, calls
    assert not _backups(home), "nothing was touched, so nothing to back up"


def test_uninstall_leaves_a_foreign_only_crontab_untouched(tmp_path, home, cron_state):
    """No loom block: the filter changes nothing, so nothing is written or removed — and
    no backup accumulates on every repeated uninstall."""
    cron_state.write_text("\n".join(FOREIGN) + "\n")
    calls_before = len(_calls(cron_state))

    proc = _run(tmp_path, home, "--uninstall-trigger")

    assert proc.returncode == 0, proc.stderr
    assert cron_state.read_text().splitlines() == FOREIGN
    assert "nothing to remove" in proc.stdout
    assert "-r" not in _calls(cron_state)[calls_before:]
    assert not _backups(home)


def test_uninstall_is_idempotent(tmp_path, home, cron_state):
    """Running it twice is not an error and does not resurrect a crontab."""
    assert _run(tmp_path, home).returncode == 0
    assert _run(tmp_path, home, "--uninstall-trigger").returncode == 0

    second = _run(tmp_path, home, "--uninstall-trigger")

    assert second.returncode == 0, second.stderr
    assert not cron_state.exists()


def test_the_crontab_backup_is_created_unreadable_to_other_users(tmp_path, home,
                                                                 cron_state):
    """The backup is a verbatim copy of the user's WHOLE crontab.

    Schedules, hostnames, and in plenty of real crontabs a token or a password on a
    command line — all of it, in a file this installer creates. RUNBOOK states mode 600
    as fact, so it has to be true by construction rather than by a best-effort `chmod`
    after the bytes are already on disk, and the directory holding it must not be
    listable by everyone either.
    """
    cron_state.write_text("\n".join(FOREIGN) + "\n")

    assert _run(tmp_path, home).returncode == 0

    backups = _backups(home)
    assert backups, "fixture: the install must have backed the crontab up"
    for backup in backups:
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600, (
            f"{backup.name} is readable beyond its owner")
    backup_dir = home / ".loom" / "backup"
    assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700


def test_a_foreign_path_line_carrying_our_bin_dir_survives_install_and_uninstall(
        tmp_path, home, cron_state):
    """The byte-exact foreign-preservation guarantee, at its sharpest.

    An operator who wants `loom` on the PATH of their own entries writes exactly this:
    `PATH=/usr/local/bin:$LOOM_HOME/venv/bin`. Claiming any `PATH=` line that mentions
    the marker ANYWHERE deletes it — silently changing what every entry after it can
    run, and breaking the promise RUNBOOK makes in as many words. Our line is the one
    `cron_block` writes: the marker is its FIRST component.
    """
    loom_home = home / ".loom"
    foreign_path = f"PATH=/usr/local/bin:{loom_home}/venv/bin"
    foreign = ["# nightly backup", foreign_path, "0 3 * * * /opt/backup/run.sh"]
    cron_state.write_text("\n".join(foreign) + "\n")

    assert _run(tmp_path, home).returncode == 0
    assert foreign_path in cron_state.read_text().splitlines(), (
        "install deleted the operator's own PATH assignment")

    assert _run(tmp_path, home, "--uninstall-trigger").returncode == 0

    assert cron_state.read_text().splitlines() == foreign
    assert "loom-process" not in cron_state.read_text()


def test_uninstall_under_a_different_loom_home_leaves_no_path_residue(tmp_path):
    """A LOOM_HOME that moved between install and uninstall.

    The command marker is already home-independent (`/venv/bin/loom process`) precisely
    so a moved home does not strand a live trigger, and `LOOM_HOME=` is matched by its
    prefix. The PATH line was the one piece still pinned to the CURRENT home, so it
    survived its own uninstall — a stale assignment left steering every entry after it.
    """
    old_home = tmp_path / "old_home"
    new_home = tmp_path / "new_home"
    old_home.mkdir()
    new_home.mkdir()
    state = tmp_path / "cron.txt"
    _crontab_shim(tmp_path, state)
    foreign = "0 3 * * * /opt/backup/run.sh"
    state.write_text(foreign + "\n")

    def run(home: Path, *args: str) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "HOME": str(home),
            "LOOM_HOME": str(home / ".loom"),
            "LOOM_SKIP_VENV": "1", "LOOM_SKIP_GITLEAKS": "1",
            "PATH": _sandbox_path(tmp_path),
        }
        return subprocess.run(
            ["bash", str(INSTALL_SCRIPT), "--yes", *args],
            env=env, capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=180,
        )

    assert run(old_home).returncode == 0
    installed = state.read_text()
    assert f"PATH={old_home}/.loom/venv/bin" in installed, "fixture: no PATH line written"

    proc = run(new_home, "--uninstall-trigger")

    assert proc.returncode == 0, proc.stderr
    remaining = state.read_text().splitlines()
    assert remaining == [foreign], f"loom residue left behind: {remaining}"


def test_uninstall_matches_our_path_line_verbatim_not_through_awk_escapes(tmp_path, home):
    """The filter's PATH marker carries `$LOOM_HOME`, and the uninstall path never runs it
    through `loom_home_is_scheduler_safe` — refusing to uninstall over an odd LOOM_HOME
    would strand a live trigger. So the marker must reach awk VERBATIM (via ENVIRON): a
    `-v` assignment expands backslash escapes, turning `...\\tb/venv/bin` into a marker with
    a literal tab that matches nothing, and the block would survive its own uninstall.
    """
    odd_home = tmp_path / "odd"
    loom_home = odd_home / ".loom"          # the crontab below spells it with a backslash
    spelled = f"{loom_home}\\tbin"
    state = tmp_path / "cron.txt"
    _crontab_shim(tmp_path, state)
    foreign = "PATH=/foreign-keep/bin:/usr/bin"
    state.write_text(
        f"# nightly\n{foreign}\n@daily keepme.sh\n"
        f"{CRON_TAG}\nLOOM_HOME={spelled}\nPATH={spelled}/venv/bin:/usr/bin\n"
        f"*/15 * * * * {spelled}/venv/bin/loom process --quiet\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "HOME": str(odd_home),
        "LOOM_HOME": spelled,
        "LOOM_SKIP_VENV": "1", "LOOM_SKIP_GITLEAKS": "1",
        "PATH": _sandbox_path(tmp_path),
    }
    proc = subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--yes", "--uninstall-trigger"],
        env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=180,
    )

    assert proc.returncode == 0, proc.stderr
    remaining = state.read_text().splitlines()
    assert "loom" not in "\n".join(remaining), (
        f"the block survived its own uninstall: {remaining}")
    # ...and the user's own PATH assignment is still none of our business.
    assert remaining == ["# nightly", foreign, "@daily keepme.sh"]


def test_uninstall_never_reads_a_crontab_it_cannot_trust(tmp_path, home):
    """HIGH-1, on the removal path. `crontab -l` failing for a reason OTHER than "no
    crontab yet" (denied spool, cron.deny, a refused macOS TCC prompt) must not be read as
    "empty": treating it as empty here would mean removing a crontab we never saw."""
    state = tmp_path / "cron.txt"
    shim = tmp_path / "shims" / "crontab"
    shim.parent.mkdir(exist_ok=True)
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        f"calls = pathlib.Path({str(state)!r} + '.calls')\n"
        "with calls.open('a') as fh:\n"
        "    fh.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if '-l' in sys.argv[1:]:\n"
        "    sys.stderr.write('crontab: you (tester) are not allowed to use this program\\n')\n"
        "    sys.exit(1)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)

    proc = _run(tmp_path, home, "--uninstall-trigger")

    assert proc.returncode != 0, "an unreadable crontab must not report a clean uninstall"
    assert "refusing to write" in proc.stderr
    assert "-r" not in _calls(state) and "-" not in _calls(state)
