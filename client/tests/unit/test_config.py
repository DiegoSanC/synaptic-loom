"""Tests for config.Config — path derivation and config.json loading (PLAN.md §T5)."""
import json

from loom.config import Config, get_loom_home


def test_paths_derive_from_loom_home(tmp_path):
    cfg = Config(loom_home=tmp_path / "h", server_url="http://x", token="t")
    home = tmp_path / "h"
    assert cfg.queue_dir == home / "queue"
    assert cfg.quarantine_dir == home / "quarantine"
    assert cfg.failed_dir == home / "failed"
    assert cfg.log_file == home / "log" / "processor.log"
    assert cfg.lock_path == home / "lock"
    assert cfg.state_path == home / "state.json"
    assert cfg.cache_path == home / "projects_cache.json"
    assert cfg.config_path == home / "config.json"
    # the default prompt_path hangs off LOOM_HOME and is derived from the prompt
    # version (T.F3): one key selects the prompt, so the two cannot disagree.
    assert cfg.prompt_path == home / "prompts" / "summarizer_v2.md"


def test_get_loom_home_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "custom"))
    assert get_loom_home() == tmp_path / "custom"


def test_load_reads_config_and_defaults(tmp_path):
    home = tmp_path / "loom"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({
        "server_url": "https://loom.example",
        "token": "abc",
        "summarizer": {"model": "opus", "timeout_s": 120, "prompt_version": "v1"},
    }), encoding="utf-8")

    cfg = Config.load(home)
    assert cfg.server_url == "https://loom.example"
    assert cfg.token == "abc"
    assert cfg.cache_ttl_s == 3600            # default
    assert (cfg.model, cfg.timeout_s, cfg.prompt_version) == ("opus", 120, "v1")
    assert cfg.claude_bin == "claude"         # default
    # a config pinned to v1 keeps resolving the v1 prompt (pilot compatibility)
    assert cfg.prompt_path == home / "prompts" / "summarizer_v1.md"


def test_load_honours_testability_keys(tmp_path):
    home = tmp_path / "loom"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({
        "server_url": "u", "token": "t",
        "claude_bin": "/opt/stub/claude", "prompt_path": "/opt/prompt.md",
    }), encoding="utf-8")

    cfg = Config.load(home)
    assert cfg.claude_bin == "/opt/stub/claude"
    assert str(cfg.prompt_path) == "/opt/prompt.md"


# --- Device token lifetime keys (issue #20) -------------------------------------
def test_load_reads_the_device_token_lifetime_keys(tmp_path):
    home = tmp_path / "loom"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({
        "server_url": "https://loom.example", "token": "abc",
        "token_issued_at": "2026-09-22T10:00:00Z",
        "token_expires_at": "2026-12-21T10:00:00Z",
    }), encoding="utf-8")
    cfg = Config.load(home)
    assert cfg.token_issued_at == "2026-09-22T10:00:00Z"
    assert cfg.token_expires_at == "2026-12-21T10:00:00Z"
    assert cfg.auth_rejected_path == home / "auth_rejected.json"


def test_load_without_lifetime_keys_is_a_legacy_permanent_token(tmp_path):
    home = tmp_path / "loom"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({
        "server_url": "https://loom.example", "token": "abc",
        "token_expires_at": 12345,  # not a timestamp: treated as absent, never a crash
    }), encoding="utf-8")
    cfg = Config.load(home)
    assert cfg.token_issued_at is None
    assert cfg.token_expires_at is None


# --- capture_mode (D9): the local opt-in gate --------------------------------------------

def _home_with(tmp_path, extra: dict):
    home = tmp_path / "loom"
    home.mkdir(parents=True)
    (home / "config.json").write_text(json.dumps({
        "server_url": "https://loom.example", "token": "abc", **extra,
    }), encoding="utf-8")
    return home


def test_capture_mode_absent_means_all_registered(tmp_path):
    """Every config written before D9 keeps its behaviour: absent key = legacy mode."""
    cfg = Config.load(_home_with(tmp_path, {}))
    assert cfg.capture_mode == "all_registered"
    assert cfg.projects_local_path == tmp_path / "loom" / "projects_local.json"


def test_capture_mode_opt_in_is_read(tmp_path):
    cfg = Config.load(_home_with(tmp_path, {"capture_mode": "opt_in"}))
    assert cfg.capture_mode == "opt_in"


def test_capture_mode_unknown_value_falls_to_opt_in(tmp_path):
    """A hand-edited garbage value lands on the side that captures LESS (append-only,
    D7: a capture that should not have happened is permanent)."""
    cfg = Config.load(_home_with(tmp_path, {"capture_mode": "yes please"}))
    assert cfg.capture_mode == "opt_in"
    cfg = Config.load(_home_with(tmp_path / "b", {"capture_mode": 7}))
    assert cfg.capture_mode == "opt_in"
