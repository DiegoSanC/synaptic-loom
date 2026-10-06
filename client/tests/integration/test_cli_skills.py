"""Tests for the thin skills and their installation (PLAN.md §T6, P2).

The skills are logic-free wrappers that invoke ``~/.loom/venv/bin/loom``. Here we check
their frontmatter and that ``install.sh`` copies them to ``~/.claude/skills/`` (HOME under
tmp, NEVER the real one).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS_SRC = REPO_ROOT / "client" / "skills"
INSTALL_SCRIPT = REPO_ROOT / "client" / "install.sh"


def _frontmatter(text: str) -> dict[str, str]:
    """Parses the flat YAML frontmatter (name/description) of a SKILL.md."""
    assert text.startswith("---\n"), "el SKILL.md debe empezar con frontmatter"
    _, block, _body = text.split("---\n", 2)
    out = {}
    for line in block.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip()
    return out


@pytest.mark.parametrize("name", ["loom", "loom-register", "loom-feedback"])
def test_skill_frontmatter_and_invokes_cli(name):
    text = (SKILLS_SRC / name / "SKILL.md").read_text(encoding="utf-8")
    fm = _frontmatter(text)
    assert fm["name"] == name
    assert fm["description"]  # non-empty description
    assert "~/.loom/venv/bin/loom" in text  # invokes the venv CLI, does not reimplement logic


def test_install_copies_skills_to_claude_dir(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "LOOM_HOME": str(home / ".loom"),
        "LOOM_SKIP_VENV": "1",
        # HOME is a tmpdir but `crontab` is not isolated by it: without this seam the
        # installer's step 5 would write the developer's REAL crontab on every run.
        "LOOM_SKIP_TRIGGER": "1",
    }
    result = subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--yes"],
        text=True, capture_output=True, env=env,
    )
    assert result.returncode == 0, result.stderr

    for name in ("loom", "loom-register", "loom-feedback"):
        dst = home / ".claude" / "skills" / name / "SKILL.md"
        assert dst.exists()
        assert dst.read_text(encoding="utf-8") == (SKILLS_SRC / name / "SKILL.md").read_text(encoding="utf-8")


# --- D10: what the skills say about WHEN to use them --------------------------------------
def test_loom_skill_triggers_before_work_not_only_at_review_time():
    """Issue #13's gap: the skill was framed for PR review / inheriting a project."""
    text = (SKILLS_SRC / "loom" / "SKILL.md").read_text(encoding="utf-8")
    fm = _frontmatter(text)
    for word in ("planning", "diagnosis", "implementation"):
        assert word in fm["description"], fm["description"]
    assert "loom check" in text
    assert "enabled" in text


def test_loom_register_skill_says_registering_enables_locally():
    text = (SKILLS_SRC / "loom-register" / "SKILL.md").read_text(encoding="utf-8")
    assert "loom enable" in text
    assert "this machine" in text


def test_loom_skill_declares_its_reads_as_skill_consultations():
    """Slice C: the server counts recalls by source; the skill's read must say `skill`."""
    text = (SKILLS_SRC / "loom" / "SKILL.md").read_text(encoding="utf-8")
    command_lines = [line for line in text.splitlines() if "loom log" in line]
    assert command_lines, text
    assert all("--source skill" in line for line in command_lines), command_lines


def test_loom_skill_documents_item_ids_and_item_narrowing():
    """D13, slice D: the agent must know what `17.2` is, that `--type`/`--grep` narrow an
    itemized entry to its matching items, and how to get whole entries back."""
    text = (SKILLS_SRC / "loom" / "SKILL.md").read_text(encoding="utf-8")
    assert "`<entry>.<n>`" in text and "`17.2`" in text
    assert "→ replaced by" in text and "→ duplicate of" in text
    assert "1 of 3 items" in text
    assert "--whole-entries" in text


def test_loom_feedback_skill_cites_item_ids_and_asks_before_recording():
    """Slice F: feedback is per item (D13), with the ids `loom log` printed, and the user
    confirms every verdict before anything is recorded."""
    text = (SKILLS_SRC / "loom-feedback" / "SKILL.md").read_text(encoding="utf-8")
    assert "`<entry>.<n>`" in text and "--item <id>" in text
    for verdict in ("useful", "not_applicable", "stale"):
        assert f"`{verdict}`" in text
    assert "confirm" in text and "Record nothing they did not confirm" in text


def test_loom_skill_points_at_loom_feedback():
    text = (SKILLS_SRC / "loom" / "SKILL.md").read_text(encoding="utf-8")
    assert "/loom-feedback" in text
