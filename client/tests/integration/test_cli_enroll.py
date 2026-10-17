"""Tests for `loom enroll` (issue #20).

The enrolment code is read at a hidden prompt and NEVER accepted as an argument: there
is no ``--code`` flag, so it cannot land in shell history. The device token comes back
from ``POST /api/enroll`` and is written to ``config.json`` (0600) together with its
lifetime, merging into an existing config rather than replacing it.
"""
from __future__ import annotations

import json
import stat

import pytest
from werkzeug.wrappers import Response

from loom import cli

CODE = "5xW1kq2lYvW1cQvV5s0m3P9jJ6rR8tT2uU4vV7wW0x"
ENROL_BODY = {
    "token": "dev-tok-never-typed",
    "issued_at": "2026-09-22T10:00:00Z",
    "expires_at": "2026-12-21T10:00:00Z",
    "user": {"id": 1, "name": "dev2"},
}


def _mock(httpserver, *, status=201, body=None, seen=None):
    httpserver.expect_request("/api/health", method="GET").respond_with_json(
        {"status": "ok", "version": "0.1.0"}
    )

    def handler(request):
        if seen is not None:
            seen.append(request.get_json())
        payload = body if body is not None else ENROL_BODY
        return Response(json.dumps(payload), status=status, content_type="application/json")

    httpserver.expect_request("/api/enroll", method="POST").respond_with_handler(handler)


def _hidden_prompt(monkeypatch, code=CODE):
    prompts = []

    def fake_getpass(prompt=""):
        prompts.append(prompt)
        return code

    monkeypatch.setattr("getpass.getpass", fake_getpass)
    # `input()` echoes; the code must never go through it.
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("input() called during enroll"))
    return prompts


def test_enroll_reads_the_code_hidden_and_writes_config_600(monkeypatch, tmp_path, httpserver, capsys):
    home = tmp_path / "loom_home"
    monkeypatch.setenv("LOOM_HOME", str(home))
    seen = []
    _mock(httpserver, seen=seen)
    prompts = _hidden_prompt(monkeypatch)

    rc = cli.main(["enroll", "--server-url", httpserver.url_for(""), "--device-name", "dev-laptop"])

    out = capsys.readouterr().out
    assert rc == 0, out
    assert prompts and "code" in prompts[0].lower()
    assert seen == [{"code": CODE, "device_name": "dev-laptop"}]
    assert "dev2" in out and "Config written" in out
    assert ENROL_BODY["token"] not in out  # the token is never printed either

    config_path = home / "config.json"
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["server_url"] == httpserver.url_for("")
    assert data["token"] == ENROL_BODY["token"]
    assert data["token_issued_at"] == ENROL_BODY["issued_at"]
    assert data["token_expires_at"] == ENROL_BODY["expires_at"]
    assert data["cache_ttl_s"] == 3600
    # D15: a fresh enrolment captures with v4 (D11's v3, in English); `Config()`'s own
    # default stays v2.
    assert data["summarizer"] == {"model": "sonnet", "timeout_s": 300, "prompt_version": "v4"}
    # D16: gitleaks is mandatory, pinned to what install.sh downloads.
    assert data["scanner"] == {"engine": "gitleaks", "gitleaks_version": "8.30.1"}
    for sub in ("queue", "quarantine", "failed", "log", "prompts", "hooks"):
        assert (home / sub).is_dir()


def test_enroll_merges_into_an_existing_config(monkeypatch, tmp_path, httpserver):
    home = tmp_path / "loom_home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({
        "server_url": "http://127.0.0.1:8787",
        "token": "old-permanent-token",
        "cache_ttl_s": 42,
        "summarizer": {"model": "opus", "timeout_s": 120, "prompt_version": "v1"},
        "scanner": {"engine": "auto", "gitleaks_version": "8.30.1"},
        "sweep": {"transcripts_root": "/tmp/x"},
    }), encoding="utf-8")
    monkeypatch.setenv("LOOM_HOME", str(home))
    _mock(httpserver)
    _hidden_prompt(monkeypatch)

    rc = cli.main(["enroll", "--server-url", httpserver.url_for(""), "--device-name", "m"])

    assert rc == 0
    data = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert data["server_url"] == httpserver.url_for("")   # repointed
    assert data["token"] == ENROL_BODY["token"]           # replaced
    assert data["token_expires_at"] == ENROL_BODY["expires_at"]
    assert data["cache_ttl_s"] == 42                      # kept
    assert data["summarizer"] == {"model": "opus", "timeout_s": 120, "prompt_version": "v1"}
    # D16 (was: the legacy {"engine": "auto", …} kept verbatim): the scanner section is
    # the one setting enrolment normalizes, because no other engine value captures.
    assert data["scanner"] == {"engine": "gitleaks", "gitleaks_version": "8.30.1"}
    assert data["sweep"] == {"transcripts_root": "/tmp/x"}
    assert stat.S_IMODE((home / "config.json").stat().st_mode) == 0o600


@pytest.mark.parametrize("scanner", [
    {"engine": "regex", "gitleaks_version": None},
    {"engine": "auto"},
    {"engine": "gitleaks", "gitleaks_version": "8.18.0", "note": "kept"},
])
def test_enroll_normalizes_the_scanner_section_to_mandatory_gitleaks(monkeypatch, tmp_path,
                                                                    httpserver, scanner):
    home = tmp_path / "loom_home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({
        "server_url": "http://127.0.0.1:8787", "token": "old", "scanner": scanner,
    }), encoding="utf-8")
    monkeypatch.setenv("LOOM_HOME", str(home))
    _mock(httpserver)
    _hidden_prompt(monkeypatch)

    assert cli.main(["enroll", "--server-url", httpserver.url_for(""), "--device-name", "m"]) == 0

    data = json.loads((home / "config.json").read_text(encoding="utf-8"))
    expected = {k: v for k, v in scanner.items() if k not in ("engine", "gitleaks_version")}
    assert data["scanner"] == {**expected, "engine": "gitleaks", "gitleaks_version": "8.30.1"}


def test_enroll_has_no_code_flag(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "h"))
    with pytest.raises(SystemExit):
        cli.main(["enroll", "--server-url", "http://x", "--code", CODE])
    assert "unrecognized arguments" in capsys.readouterr().err


def test_enroll_prompts_for_the_url_when_absent(monkeypatch, tmp_path, httpserver):
    home = tmp_path / "loom_home"
    monkeypatch.setenv("LOOM_HOME", str(home))
    _mock(httpserver)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": CODE)
    monkeypatch.setattr("builtins.input", lambda prompt="": httpserver.url_for(""))

    rc = cli.main(["enroll", "--device-name", "m"])

    assert rc == 0
    assert json.loads((home / "config.json").read_text())["server_url"] == httpserver.url_for("")


# The default device name is the SHORT host name: macOS can report a DHCP-supplied FQDN,
# sometimes with the domain repeated, and the home network's domain is not the server's
# business. The OS host name is the one boundary simulated here.
@pytest.mark.parametrize(("os_hostname", "sent"), [
    ("laptop.home.example.home.example", "laptop"),
    ("laptop.local", "laptop"),
    ("laptop", "laptop"),
    (" laptop .home.example", "laptop"),
    ("", "device"),
    (".home.example", "device"),
    ("   ", "device"),
])
def test_enroll_device_name_defaults_to_the_short_host_name(monkeypatch, tmp_path, httpserver,
                                                             capsys, os_hostname, sent):
    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "h"))
    monkeypatch.setattr("socket.gethostname", lambda: os_hostname)
    seen = []
    _mock(httpserver, seen=seen)
    _hidden_prompt(monkeypatch)

    assert cli.main(["enroll", "--server-url", httpserver.url_for("")]) == 0
    assert seen == [{"code": CODE, "device_name": sent}]
    assert f"(device '{sent}')" in capsys.readouterr().out


def test_enroll_device_name_falls_back_when_the_host_name_is_unavailable(monkeypatch, tmp_path,
                                                                        httpserver):
    def no_hostname():
        raise OSError("no host name")

    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "h"))
    monkeypatch.setattr("socket.gethostname", no_hostname)
    seen = []
    _mock(httpserver, seen=seen)
    _hidden_prompt(monkeypatch)

    assert cli.main(["enroll", "--server-url", httpserver.url_for("")]) == 0
    assert seen == [{"code": CODE, "device_name": "device"}]


def test_enroll_explicit_device_name_is_sent_verbatim(monkeypatch, tmp_path, httpserver, capsys):
    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "h"))
    monkeypatch.setattr("socket.gethostname", lambda: "laptop.home.example")
    seen = []
    _mock(httpserver, seen=seen)
    _hidden_prompt(monkeypatch)

    rc = cli.main(["enroll", "--server-url", httpserver.url_for(""), "--device-name", "Diego's Mac.lan"])
    assert rc == 0
    assert seen == [{"code": CODE, "device_name": "Diego's Mac.lan"}]
    assert "(device 'Diego's Mac.lan')" in capsys.readouterr().out


def test_enroll_help_states_the_short_host_name_default(capsys):
    with pytest.raises(SystemExit):
        cli.main(["enroll", "--help"])
    assert "default: short host name, without the domain" in " ".join(capsys.readouterr().out.split())


def test_enroll_rejected_code_writes_nothing(monkeypatch, tmp_path, httpserver, capsys):
    home = tmp_path / "loom_home"
    monkeypatch.setenv("LOOM_HOME", str(home))
    _mock(httpserver, status=401, body={"error": "invalid_code",
                                        "detail": "Invalid, expired or already redeemed enrolment code"})
    _hidden_prompt(monkeypatch)

    rc = cli.main(["enroll", "--server-url", httpserver.url_for(""), "--device-name", "m"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "invalid_code" in err
    assert not (home / "config.json").exists()


def test_enroll_server_unreachable_writes_nothing(monkeypatch, tmp_path, capsys):
    home = tmp_path / "loom_home"
    monkeypatch.setenv("LOOM_HOME", str(home))
    _hidden_prompt(monkeypatch)

    rc = cli.main(["enroll", "--server-url", "http://127.0.0.1:1", "--device-name", "m"])

    assert rc == 1
    assert "Could not validate the server" in capsys.readouterr().err
    assert not (home / "config.json").exists()


# --- D9: enrolment is the documented onboarding, so it lands the machine in opt-in -----
def test_enroll_fresh_config_lands_in_opt_in_mode(monkeypatch, tmp_path, httpserver, capsys):
    home = tmp_path / "loom_home"
    monkeypatch.setenv("LOOM_HOME", str(home))
    _mock(httpserver)
    _hidden_prompt(monkeypatch)

    rc = cli.main(["enroll", "--server-url", httpserver.url_for(""), "--device-name", "mac"])

    assert rc == 0
    cfg = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert cfg["capture_mode"] == "opt_in"
    out = capsys.readouterr().out
    assert "loom enable" in out


def test_enroll_into_a_legacy_config_without_the_key_switches_to_opt_in(monkeypatch, tmp_path, httpserver):
    home = tmp_path / "loom_home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({
        "server_url": "http://old", "token": "old", "cache_ttl_s": 60,
    }), encoding="utf-8")
    monkeypatch.setenv("LOOM_HOME", str(home))
    _mock(httpserver)
    _hidden_prompt(monkeypatch)

    rc = cli.main(["enroll", "--server-url", httpserver.url_for(""), "--device-name", "mac"])

    assert rc == 0
    cfg = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert cfg["capture_mode"] == "opt_in"
    assert cfg["cache_ttl_s"] == 60


def test_enroll_preserves_an_explicit_capture_mode(monkeypatch, tmp_path, httpserver):
    home = tmp_path / "loom_home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({
        "server_url": "http://old", "token": "old", "capture_mode": "all_registered",
    }), encoding="utf-8")
    monkeypatch.setenv("LOOM_HOME", str(home))
    _mock(httpserver)
    _hidden_prompt(monkeypatch)

    rc = cli.main(["enroll", "--server-url", httpserver.url_for(""), "--device-name", "mac"])

    assert rc == 0
    cfg = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert cfg["capture_mode"] == "all_registered"
