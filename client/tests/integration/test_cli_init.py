"""Tests for `loom init` (PLAN.md §T6/§T5).

Non-interactive mode (flags): validates against the mock's /api/health, writes config.json
with 0600 permissions and creates the LOOM_HOME tree. Server down → aborts unless --force.
"""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from loom import cli

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_init_noninteractive_writes_config_600_and_dirs(monkeypatch, tmp_path, httpserver, capsys):
    home = tmp_path / "loom_home"
    monkeypatch.setenv("LOOM_HOME", str(home))
    httpserver.expect_request("/api/health", method="GET").respond_with_json(
        {"status": "ok", "version": "0.1.0"}
    )

    rc = cli.main(["init", "--server-url", httpserver.url_for(""), "--token", "tok-xyz"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "Server reachable" in out

    config_path = home / "config.json"
    assert config_path.exists()
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["server_url"] == httpserver.url_for("")
    assert data["token"] == "tok-xyz"
    assert data["cache_ttl_s"] == 3600
    # A fresh config captures with the TYPED prompt (T.F3); install.sh ships it.
    assert data["summarizer"] == {"model": "sonnet", "timeout_s": 300, "prompt_version": "v2"}
    # D16: gitleaks is mandatory, pinned to what install.sh downloads.
    assert data["scanner"] == {"engine": "gitleaks", "gitleaks_version": "8.30.1"}

    for sub in ("queue", "quarantine", "failed", "log", "prompts", "hooks"):
        assert (home / sub).is_dir()


def test_init_accepts_a_dash_leading_token_in_the_space_form(
        monkeypatch, tmp_path, httpserver, capsys):
    home = tmp_path / "loom_home"
    monkeypatch.setenv("LOOM_HOME", str(home))
    httpserver.expect_request("/api/health", method="GET").respond_with_json(
        {"status": "ok", "version": "0.1.0"}
    )

    rc = cli.main([
        "init", "--server-url", httpserver.url_for(""), "--token", "-Xy9abc"
    ])

    assert rc == 0
    data = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert data["token"] == "-Xy9abc"


def test_init_keeps_accepting_a_dash_leading_token_in_the_equals_form(
        monkeypatch, tmp_path, httpserver, capsys):
    home = tmp_path / "loom_home"
    monkeypatch.setenv("LOOM_HOME", str(home))
    httpserver.expect_request("/api/health", method="GET").respond_with_json(
        {"status": "ok", "version": "0.1.0"}
    )

    rc = cli.main([
        "init", f"--server-url={httpserver.url_for('')}", "--token=-Xy9abc"
    ])

    assert rc == 0
    data = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert data["token"] == "-Xy9abc"


def test_init_missing_token_value_still_errors():
    with pytest.raises(SystemExit) as raised:
        cli.main(["init", "--token"])

    assert raised.value.code == 2


def test_init_prompts_for_token_without_echoing_it(
        monkeypatch, tmp_path, httpserver, capsys):
    home = tmp_path / "loom_home"
    monkeypatch.setenv("LOOM_HOME", str(home))
    httpserver.expect_request("/api/health", method="GET").respond_with_json(
        {"status": "ok", "version": "0.1.0"}
    )
    prompts = []

    def hidden_prompt(prompt):
        prompts.append(prompt)
        return "secret-from-hidden-prompt"

    monkeypatch.setattr(cli.getpass, "getpass", hidden_prompt)
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail(
        "token entry must not use the echoing input() prompt"
    ))

    rc = cli.main(["init", "--server-url", httpserver.url_for("")])

    assert rc == 0
    assert prompts == ["Token (pasted from the server's 'admin.py adduser'): "]
    config = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert config["token"] == "secret-from-hidden-prompt"


def test_runbook_onboarding_puts_no_credential_on_the_command_line():
    """#19 gap 8, as re-derived for #20: the documented onboarding is `loom enroll`,
    which reads the enrolment code at a hidden prompt and has no flag for it, so no
    credential of any kind can land in the engineer's shell history. The pre-#20 form
    (`loom init` + a permanent token at a hidden prompt) stays documented as break-glass
    and must not regain a `--token`."""
    runbook = (REPO_ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    onboarding = runbook.split("## 5. Onboarding another dev machine", 1)[1].split(
        "## 6. Daily operation and validation", 1
    )[0]

    assert (
        "~/.loom/venv/bin/loom enroll --server-url=https://<app>.fly.dev"
        in onboarding
    )
    assert "§2 → “Administering users”" in onboarding
    assert "--token" not in onboarding
    assert "--code" not in onboarding
    assert "adduser" not in onboarding.split("```bash", 1)[1].split("```", 1)[0]


def test_init_server_down_noninteractive_aborts(monkeypatch, tmp_path, capsys):
    home = tmp_path / "loom_home"
    monkeypatch.setenv("LOOM_HOME", str(home))

    rc = cli.main(["init", "--server-url", "http://127.0.0.1:1", "--token", "tok"])

    assert rc == 1
    assert not (home / "config.json").exists()


def test_init_force_writes_despite_server_down(monkeypatch, tmp_path, capsys):
    home = tmp_path / "loom_home"
    monkeypatch.setenv("LOOM_HOME", str(home))

    rc = cli.main(["init", "--server-url", "http://127.0.0.1:1", "--token", "tok", "--force"])

    assert rc == 0
    config_path = home / "config.json"
    assert config_path.exists()
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
