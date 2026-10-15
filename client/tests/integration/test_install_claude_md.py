"""install.sh's CLAUDE.md block (D10): the one user-level instruction that makes the agent
run `loom check` at the start of a task.

Rules: the block lives between ``<!-- loom:start -->`` and ``<!-- loom:end -->`` in
``~/.claude/CLAUDE.md``; the installer creates the file when absent, never touches a byte
outside the markers, replaces only the inside on a re-run, asks before touching an
existing file unless ``--yes``, and backs the file up first. HOME is always a tmpdir.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from test_install_update import GIT_ENV, INSTALL_SCRIPT, _sandbox_path

START = "<!-- loom:start -->"
END = "<!-- loom:end -->"


def _run(tmp_path: Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "LOOM_HOME": str(home / ".loom"),
        "LOOM_SKIP_VENV": "1", "LOOM_SKIP_GITLEAKS": "1",
        "LOOM_SKIP_TRIGGER": "1",
        "PATH": _sandbox_path(tmp_path),
        **GIT_ENV,
    }
    return subprocess.run(
        ["bash", str(INSTALL_SCRIPT), *args],
        env=env, capture_output=True, text=True, input=stdin, timeout=180,
    )


def _claude_md(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".claude" / "CLAUDE.md"


def _block(text: str) -> str:
    assert text.count(START) == 1 and text.count(END) == 1, text
    return text.split(START, 1)[1].split(END, 1)[0]


def test_install_creates_claude_md_with_the_block(tmp_path):
    proc = _run(tmp_path, "--yes")

    assert proc.returncode == 0, proc.stderr
    text = _claude_md(tmp_path).read_text(encoding="utf-8")
    block = _block(text)
    assert "loom check" in block
    for state in ("not_registered", "registered_undecided", "enabled", "disabled", "unknown"):
        assert state in block, state
    assert "/loom" in block
    assert "use /loom-feedback when you finish" in " ".join(block.split()), (
        "slice F: the block points the agent at item feedback (dropped from #43)"
    )
    assert "CLAUDE.md" in proc.stdout


def test_the_enabled_branch_asks_for_a_closing_recap_of_only_what_was_said(tmp_path):
    """D10 amendment (2026-10-01): in an enabled repo, a task that decided something ends
    with a recap the summarizer can read; it may never supply a reason the user did not
    give, and it is skipped when nothing was decided."""
    assert _run(tmp_path, "--yes").returncode == 0
    block = " ".join(_block(_claude_md(tmp_path).read_text(encoding="utf-8")).split())

    enabled = block.split("- `enabled`:", 1)[1].split("- `unknown`:", 1)[0]
    for phrase in ("close with a short recap in the conversation",
                   "the alternative dropped", "standing rule", "pending work with its owner",
                   "only what the user said or confirmed",
                   "never a reason they did not give",
                   "mark your own unconfirmed conclusions as such",
                   "Skip it when nothing was decided"):
        assert phrase in enabled, phrase
    assert "recap" not in block.replace(enabled, ""), "the recap is for enabled repos only"


def test_install_twice_leaves_one_block_and_an_identical_file(tmp_path):
    assert _run(tmp_path, "--yes").returncode == 0
    first = _claude_md(tmp_path).read_bytes()

    proc = _run(tmp_path, "--yes")

    assert proc.returncode == 0, proc.stderr
    assert _claude_md(tmp_path).read_bytes() == first
    assert "already" in proc.stdout.lower()


def test_install_appends_to_an_existing_claude_md_and_keeps_every_byte_outside(tmp_path):
    md = _claude_md(tmp_path)
    md.parent.mkdir(parents=True)
    before = "# My rules\n\nAlways answer in Spanish.\n\n## Style\n- short\n"
    md.write_text(before, encoding="utf-8")

    proc = _run(tmp_path, "--yes")

    assert proc.returncode == 0, proc.stderr
    text = md.read_text(encoding="utf-8")
    assert text.startswith(before)
    assert "loom check" in _block(text)
    backups = sorted(md.parent.glob("CLAUDE.md.bak-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == before


def test_install_replaces_only_the_inside_of_a_stale_block(tmp_path):
    md = _claude_md(tmp_path)
    md.parent.mkdir(parents=True)
    before_block = "# Mine\n\n"
    after_block = "\n# Also mine\n- keep\n"
    md.write_text(f"{before_block}{START}\nOLD LOOM TEXT\n{END}{after_block}", encoding="utf-8")

    proc = _run(tmp_path, "--yes")

    assert proc.returncode == 0, proc.stderr
    text = md.read_text(encoding="utf-8")
    assert text.startswith(before_block) and text.endswith(after_block)
    assert "OLD LOOM TEXT" not in text
    assert "loom check" in _block(text)


def test_install_asks_before_touching_an_existing_file_and_a_no_leaves_it_alone(tmp_path):
    md = _claude_md(tmp_path)
    md.parent.mkdir(parents=True)
    before = "# Mine\n"
    md.write_text(before, encoding="utf-8")
    # settings.json gets its own prompt first (no hook yet): answer y to it, n to CLAUDE.md.
    proc = _run(tmp_path, stdin="y\nn\n")

    assert proc.returncode == 0, proc.stderr
    assert md.read_text(encoding="utf-8") == before
    assert list(md.parent.glob("CLAUDE.md.bak-*")) == []
    assert "CLAUDE.md" in proc.stdout and "unchanged" in proc.stdout.lower()


def test_install_skips_the_block_with_the_env_seam(tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        **os.environ, "HOME": str(home), "LOOM_HOME": str(home / ".loom"),
        "LOOM_SKIP_VENV": "1", "LOOM_SKIP_GITLEAKS": "1", "LOOM_SKIP_TRIGGER": "1", "LOOM_SKIP_CLAUDE_MD": "1",
        "PATH": _sandbox_path(tmp_path), **GIT_ENV,
    }
    proc = subprocess.run(["bash", str(INSTALL_SCRIPT), "--yes"], env=env,
                          capture_output=True, text=True, timeout=180)

    assert proc.returncode == 0, proc.stderr
    assert not _claude_md(tmp_path).exists()
