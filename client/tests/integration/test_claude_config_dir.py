"""Claude Code's configuration directory when it is not ``~/.claude``.

Claude Code keeps its settings, ``CLAUDE.md``, skills, transcripts and sign-in under
``CLAUDE_CONFIG_DIR`` when that is set; a wrapper such as
``claudep() { CLAUDE_CONFIG_DIR=~/.claude-personal claude "$@"; }`` sets it for Claude Code
alone. Asserted through the real install.sh, the real CLI and a mock server:

- install.sh puts the hook, the ``CLAUDE.md`` block and the skills in ``$CLAUDE_CONFIG_DIR``
  and records it as ``claude_config_dir`` in an existing config; a recorded value wins over
  the variable, so an update from another profile cannot move the hook;
- `loom enroll` records the directory only when this install's hook is registered there;
- doctor looks for the hook there, the sweep reads its ``projects``, and the summarizer and
  curation run ``claude`` with it as ``CLAUDE_CONFIG_DIR`` — the periodic pass does not
  carry the variable, and without it ``claude -p`` signs in with a different account or
  none at all.

Every test runs with ``HOME`` at a tmpdir; ``client/tests/conftest.py`` drops an inherited
``CLAUDE_CONFIG_DIR``, so each one sets the variable itself.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from werkzeug.wrappers import Response

from loom import cli, curate
from loom.config import Config

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_SCRIPT = REPO_ROOT / "client" / "install.sh"
SKILLS = ("loom", "loom-register", "loom-feedback")
CANNED_SUMMARY = "## Context\nThe importer emits None.\n## Decisions\n- total() skips None.\n"


def _run_install(home: Path, *args: str, claude_config_dir: str | None = None,
                 cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home), "LOOM_HOME": str(home / ".loom"),
           "LOOM_SKIP_VENV": "1", "LOOM_SKIP_GITLEAKS": "1", "LOOM_SKIP_TRIGGER": "1"}
    env.pop("CLAUDE_CONFIG_DIR", None)
    if claude_config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = claude_config_dir
    return subprocess.run(["bash", str(INSTALL_SCRIPT), "--yes", *args],
                          text=True, capture_output=True, env=env, cwd=cwd)


def _hook_commands(settings: Path) -> list[str]:
    data = json.loads(settings.read_text(encoding="utf-8"))
    return [h.get("command") for g in data.get("hooks", {}).get("SessionEnd", [])
            for h in g.get("hooks", [])]


def _write_config(home: Path, **extra) -> Path:
    path = home / ".loom" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"server_url": "https://loom.example", "token": "t", **extra}),
                    encoding="utf-8")
    path.chmod(0o600)
    return path


# --------------------------------------------------------------------------- #
# install.sh                                                                   #
# --------------------------------------------------------------------------- #
def test_install_puts_everything_in_the_claude_config_dir(tmp_path):
    other = tmp_path / ".claude-personal"

    cp = _run_install(tmp_path, claude_config_dir=str(other))

    assert cp.returncode == 0, cp.stdout + cp.stderr
    hook = str(tmp_path / ".loom" / "hooks" / "sessionend.sh")
    assert _hook_commands(other / "settings.json") == [hook]
    assert "<!-- loom:start -->" in (other / "CLAUDE.md").read_text(encoding="utf-8")
    for skill in SKILLS:
        assert (other / "skills" / skill / "SKILL.md").is_file(), skill
    assert not (tmp_path / ".claude").exists()
    assert f"Claude Code configuration directory: {other}" in cp.stdout
    # No config yet: enrolment is what records the directory, so the installer says how.
    assert f"CLAUDE_CONFIG_DIR={other} still set" in cp.stdout


@pytest.mark.parametrize("spelled, cwd", [("~/.claude-personal", None),
                                          (".claude-personal", "home")],
                         ids=["tilde", "relative"])
def test_install_records_an_absolute_directory(tmp_path, spelled, cwd):
    config = _write_config(tmp_path)

    cp = _run_install(tmp_path, claude_config_dir=spelled,
                      cwd=tmp_path if cwd == "home" else None)

    assert cp.returncode == 0, cp.stdout + cp.stderr
    other = tmp_path / ".claude-personal"
    assert len(_hook_commands(other / "settings.json")) == 1
    assert json.loads(config.read_text(encoding="utf-8"))["claude_config_dir"] == str(other)


def test_install_without_the_variable_still_uses_dot_claude(tmp_path):
    cp = _run_install(tmp_path)

    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert len(_hook_commands(tmp_path / ".claude" / "settings.json")) == 1
    assert "Claude Code configuration directory" not in cp.stdout


def test_install_records_the_directory_in_an_existing_config(tmp_path):
    other = tmp_path / ".claude-personal"
    config = _write_config(tmp_path, capture_mode="opt_in")

    cp = _run_install(tmp_path, claude_config_dir=str(other))

    assert cp.returncode == 0, cp.stdout + cp.stderr
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["claude_config_dir"] == str(other)
    assert data["capture_mode"] == "opt_in" and data["token"] == "t"
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert Config.load(tmp_path / ".loom").claude_home == other


def test_install_leaves_a_default_install_config_without_the_key(tmp_path):
    config = _write_config(tmp_path)

    assert _run_install(tmp_path).returncode == 0

    assert "claude_config_dir" not in json.loads(config.read_text(encoding="utf-8"))


def test_the_recorded_directory_wins_over_the_variable(tmp_path):
    # An --update run from inside another profile's session must not move the hook (and
    # with it the capture) onto that profile's account.
    personal, work = tmp_path / ".claude-personal", tmp_path / ".claude-work"
    config = _write_config(tmp_path, claude_config_dir="~/.claude-personal")

    cp = _run_install(tmp_path, claude_config_dir=str(work))

    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert len(_hook_commands(personal / "settings.json")) == 1
    assert not work.exists()
    assert json.loads(config.read_text(encoding="utf-8"))[
        "claude_config_dir"] == "~/.claude-personal"


@pytest.mark.parametrize("with_config", [True, False], ids=["config", "no-config"])
def test_an_install_whose_hook_is_in_dot_claude_stays_there(tmp_path, with_config):
    # Installed before the key existed, then updated from inside another profile's
    # session (its wrapper exports CLAUDE_CONFIG_DIR to every command).
    assert _run_install(tmp_path).returncode == 0
    config = _write_config(tmp_path) if with_config else None
    work = tmp_path / ".claude-work"

    cp = _run_install(tmp_path, claude_config_dir=str(work))

    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert not work.exists()
    assert len(_hook_commands(tmp_path / ".claude" / "settings.json")) == 1
    assert "is ignored" in cp.stderr and str(work) in cp.stderr
    if config:
        assert "claude_config_dir" not in json.loads(config.read_text(encoding="utf-8"))


def test_an_empty_recorded_value_is_replaced(tmp_path):
    other = tmp_path / ".claude-personal"
    config = _write_config(tmp_path, claude_config_dir="")

    assert _run_install(tmp_path, claude_config_dir=str(other)).returncode == 0

    assert json.loads(config.read_text(encoding="utf-8"))["claude_config_dir"] == str(other)


# --------------------------------------------------------------------------- #
# loom enroll                                                                  #
# --------------------------------------------------------------------------- #
def _enroll(tmp_path, monkeypatch, httpserver, *, claude_config_dir, hook_there):
    home = tmp_path / ".loom"
    other = tmp_path / ".claude-personal"
    other.mkdir()
    hooks = ([{"hooks": [{"type": "command", "command": str(home / "hooks" / "sessionend.sh")}]}]
             if hook_there else [])
    (other / "settings.json").write_text(json.dumps({"hooks": {"SessionEnd": hooks}}),
                                         encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOOM_HOME", str(home))
    if claude_config_dir:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    httpserver.expect_request("/api/health", method="GET").respond_with_json(
        {"status": "ok", "version": "0.1.0"})
    httpserver.expect_request("/api/enroll", method="POST").respond_with_handler(
        lambda r: Response(json.dumps({"token": "dev-tok", "issued_at": "2026-09-22T10:00:00Z",
                                       "expires_at": "2026-12-21T10:00:00Z",
                                       "user": {"id": 1, "name": "dev2"}}),
                           status=201, content_type="application/json"))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "code-xyz")
    assert cli.main(["enroll", "--server-url", httpserver.url_for(""),
                     "--device-name", "laptop"]) == 0
    return json.loads((home / "config.json").read_text(encoding="utf-8")), other


def test_enroll_records_the_directory_this_installs_hook_is_in(tmp_path, monkeypatch,
                                                                 httpserver):
    data, other = _enroll(tmp_path, monkeypatch, httpserver,
                          claude_config_dir=True, hook_there=True)

    assert data["claude_config_dir"] == str(other)


@pytest.mark.parametrize("claude_config_dir, hook_there", [(True, False), (False, True)],
                         ids=["hook-not-there", "no-variable"])
def test_enroll_records_nothing_otherwise(tmp_path, monkeypatch, httpserver,
                                          claude_config_dir, hook_there):
    data, _ = _enroll(tmp_path, monkeypatch, httpserver,
                      claude_config_dir=claude_config_dir, hook_there=hook_there)

    assert "claude_config_dir" not in data


def test_enroll_keeps_a_recorded_directory(tmp_path, monkeypatch, httpserver):
    _write_config(tmp_path, claude_config_dir="/elsewhere/.claude-x")

    data, _ = _enroll(tmp_path, monkeypatch, httpserver,
                      claude_config_dir=True, hook_there=True)

    assert data["claude_config_dir"] == "/elsewhere/.claude-x"


def test_enroll_drops_an_empty_recorded_value(tmp_path, monkeypatch, httpserver):
    _write_config(tmp_path, claude_config_dir="")

    data, other = _enroll(tmp_path, monkeypatch, httpserver,
                          claude_config_dir=True, hook_there=True)

    assert data["claude_config_dir"] == str(other)


def test_init_records_the_directory_this_installs_hook_is_in(tmp_path, monkeypatch,
                                                               httpserver):
    home = tmp_path / ".loom"
    other = tmp_path / ".claude-personal"
    other.mkdir()
    (other / "settings.json").write_text(json.dumps({"hooks": {"SessionEnd": [
        {"hooks": [{"type": "command", "command": str(home / "hooks" / "sessionend.sh")}]}]}}),
        encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOOM_HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    httpserver.expect_request("/api/health", method="GET").respond_with_json(
        {"status": "ok", "version": "0.1.0"})

    assert cli.main(["init", "--server-url", httpserver.url_for(""), "--token", "tok"]) == 0

    data = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert data["claude_config_dir"] == str(other)


def test_init_keeps_a_recorded_directory(tmp_path, monkeypatch, httpserver):
    _write_config(tmp_path, claude_config_dir="/elsewhere/.claude-x")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOOM_HOME", str(tmp_path / ".loom"))
    httpserver.expect_request("/api/health", method="GET").respond_with_json(
        {"status": "ok", "version": "0.1.0"})

    assert cli.main(["init", "--server-url", httpserver.url_for(""), "--token", "tok"]) == 0

    data = json.loads((tmp_path / ".loom" / "config.json").read_text(encoding="utf-8"))
    assert data["claude_config_dir"] == "/elsewhere/.claude-x" and data["token"] == "tok"


# --------------------------------------------------------------------------- #
# doctor, the sweep                                                            #
# --------------------------------------------------------------------------- #
def _hook_row(out: str) -> str:
    [row] = [ln for ln in out.splitlines() if "SessionEnd hook" in ln and "settings.json" in ln]
    return row


@pytest.mark.parametrize("recorded", [True, False], ids=["recorded", "default"])
def test_doctor_looks_for_the_hook_in_the_install_directory(tmp_path, monkeypatch, capsys,
                                                            httpserver, recorded):
    home = tmp_path / ".loom"
    claude_home = tmp_path / (".claude-personal" if recorded else ".claude")
    claude_home.mkdir()
    (claude_home / "settings.json").write_text(json.dumps({"hooks": {"SessionEnd": [
        {"hooks": [{"type": "command", "command": str(home / "hooks" / "sessionend.sh")}]}]}}),
        encoding="utf-8")
    _write_config(tmp_path, **({"claude_config_dir": str(claude_home)} if recorded else {}))
    data = json.loads((home / "config.json").read_text(encoding="utf-8"))
    data["server_url"] = httpserver.url_for("")
    (home / "config.json").write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOOM_HOME", str(home))
    httpserver.expect_request("/api/health", method="GET").respond_with_json(
        {"status": "ok", "version": "0.1.0"})
    httpserver.expect_request("/api/projects", method="GET").respond_with_json({"projects": []})

    cli.main(["doctor"])

    row = _hook_row(capsys.readouterr().out)
    assert row.startswith("✓") and str(claude_home / "settings.json") in row, row


def test_doctor_names_claude_config_dir_when_the_hook_is_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOOM_HOME", str(tmp_path / ".loom"))

    cli.main(["doctor"])

    row = _hook_row(capsys.readouterr().out)
    assert row.startswith("✗") and "CLAUDE_CONFIG_DIR" in row, row


def test_doctor_without_a_config_looks_where_the_hook_was_installed(tmp_path, monkeypatch,
                                                                     capsys):
    home = tmp_path / ".loom"
    other = tmp_path / ".claude-personal"
    other.mkdir()
    (other / "settings.json").write_text(json.dumps({"hooks": {"SessionEnd": [
        {"hooks": [{"type": "command", "command": str(home / "hooks" / "sessionend.sh")}]}]}}),
        encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOOM_HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))

    cli.main(["doctor"])

    row = _hook_row(capsys.readouterr().out)
    assert row.startswith("✓") and str(other / "settings.json") in row, row


def test_a_recorded_default_directory_is_left_unset_for_claude(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    cfg = Config(loom_home=tmp_path, server_url="u", token="t", claude_config_dir="~/.claude/")

    assert cfg.claude_config_dir is None
    assert "CLAUDE_CONFIG_DIR" not in cfg.claude_env()


def test_the_sweep_reads_the_install_directorys_transcripts(tmp_path):
    cfg = Config(loom_home=tmp_path, server_url="u", token="t",
                 claude_config_dir="~/.claude-personal")
    pinned = Config(loom_home=tmp_path, server_url="u", token="t",
                    claude_config_dir="~/.claude-personal", sweep_transcripts_root=tmp_path / "x")
    default = Config(loom_home=tmp_path, server_url="u", token="t")

    assert cfg.sweep_transcripts_root == Path("~/.claude-personal/projects").expanduser()
    assert pinned.sweep_transcripts_root == tmp_path / "x"
    assert default.sweep_transcripts_root == Path("~/.claude/projects").expanduser()


# --------------------------------------------------------------------------- #
# The claude subprocesses                                                      #
# --------------------------------------------------------------------------- #
def _env_recording_stub(path: Path, record: Path, stdout: str) -> str:
    path.write_text(
        f"#!{sys.executable}\nimport os, pathlib, sys\nsys.stdin.read()\n"
        f"pathlib.Path({str(record)!r}).write_text("
        f"os.environ.get('CLAUDE_CONFIG_DIR', '<unset>') + '|' + "
        f"os.environ.get('LOOM_SUMMARIZER', '<unset>'))\n"
        f"sys.stdout.write({stdout!r})\n",
        encoding="utf-8")
    path.chmod(0o755)
    return str(path)


@pytest.mark.parametrize("recorded, seen", [("~/.claude-personal", None), (None, "<unset>")],
                         ids=["recorded", "default"])
def test_the_summarizer_signs_in_with_the_install_directory(env, monkeypatch, capsys,
                                                            recorded, seen):
    # Started from inside another profile's session: its variable is never inherited.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(env.home / ".claude-work"))
    record = env.home / "claude_env.txt"
    env.config(_env_recording_stub(env.home / "claude_rec.py", record, CANNED_SUMMARY))
    data = env.config_json()
    data["scanner"] = {"engine": "gitleaks", "gitleaks_version": "8.30.1"}
    if recorded:
        data["claude_config_dir"] = recorded
    (env.home / "config.json").write_text(json.dumps(data), encoding="utf-8")
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 1}, status=201)
    env.drop_marker("sess-env")
    monkeypatch.setenv("LOOM_HOME", str(env.home))

    rc = cli.main(["process"])

    assert rc == 0, capsys.readouterr()
    expected = seen or str(Path(recorded).expanduser())
    assert record.read_text(encoding="utf-8") == f"{expected}|1"
    assert len(env.posted_entries()) == 1


def test_curation_signs_in_with_the_install_directory(tmp_path):
    record = tmp_path / "claude_env.txt"
    cfg = Config(loom_home=tmp_path, server_url="u", token="t",
                 claude_bin=_env_recording_stub(tmp_path / "claude_rec.py", record, "[]"),
                 claude_config_dir=tmp_path / ".claude-personal")

    curate._run_llm(cfg, "prompt", "payload")

    assert record.read_text(encoding="utf-8") == f"{tmp_path / '.claude-personal'}|1"
