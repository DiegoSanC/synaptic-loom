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


# --- D19: the invitation link `https://<host>/ui/join#code=<code>` ----------------------
# The link is pasted at the SAME hidden prompt as a bare code (never an argument) and
# carries the server URL, so a crafted link is the phishing vector: the CLI shows the
# host and asks y/N on a first enrolment or a repoint, refuses a link that contradicts
# an explicit --server-url, and never echoes what was pasted.
def _origin(httpserver):
    return httpserver.url_for("").rstrip("/")  # http://localhost:<port>, a loopback host


def _link(origin, code=CODE):
    return f"{origin}/ui/join#code={code}"


def _answer(monkeypatch, reply):
    """`input()` is the y/N confirmation only: record its prompts, give ``reply``."""
    asked = []

    def fake_input(prompt=""):
        asked.append(prompt)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr("builtins.input", fake_input)
    return asked


def _hidden_link(monkeypatch, value):
    prompts = []
    monkeypatch.setattr("getpass.getpass", lambda prompt="": prompts.append(prompt) or value)
    return prompts


def _existing_config(home, server_url):
    home.mkdir(parents=True)
    (home / "config.json").write_text(json.dumps({
        "server_url": server_url, "token": "old-token", "cache_ttl_s": 42,
    }), encoding="utf-8")


def test_enroll_link_with_a_matching_server_url_asks_nothing(monkeypatch, tmp_path, httpserver,
                                                              capsys):
    home = tmp_path / "loom_home"
    monkeypatch.setenv("LOOM_HOME", str(home))
    seen = []
    _mock(httpserver, seen=seen)
    prompts = _hidden_prompt(monkeypatch, code=_link(_origin(httpserver)))  # input() fails

    rc = cli.main(["enroll", "--server-url", httpserver.url_for(""), "--device-name", "m"])

    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert prompts and "code" in prompts[0].lower() and "link" in prompts[0].lower()
    assert seen == [{"code": CODE, "device_name": "m"}]  # the code, never the link
    data = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert data["server_url"] == _origin(httpserver)  # scheme, host and port; no path
    assert data["token"] == ENROL_BODY["token"]
    assert CODE not in captured.out + captured.err


def test_enroll_link_on_a_first_enrolment_shows_the_host_and_asks(monkeypatch, tmp_path,
                                                                  httpserver, capsys):
    home = tmp_path / "loom_home"
    monkeypatch.setenv("LOOM_HOME", str(home))
    seen = []
    _mock(httpserver, seen=seen)
    _hidden_link(monkeypatch, _link(_origin(httpserver)))
    asked = _answer(monkeypatch, "y")

    rc = cli.main(["enroll", "--device-name", "m"])  # no --server-url: the link carries it

    captured = capsys.readouterr()
    assert rc == 0, captured.err
    host = f"localhost:{httpserver.port}"
    assert len(asked) == 1 and "[y/N]" in asked[0]
    assert f"    {host}\n" in captured.out  # prominent: on a line of its own
    assert seen == [{"code": CODE, "device_name": "m"}]
    data = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert data["server_url"] == _origin(httpserver)
    assert CODE not in captured.out + captured.err + "".join(asked)


@pytest.mark.parametrize("reply", ["", "n", "no", "N", "yes please", EOFError()])
def test_enroll_link_not_confirmed_sends_nothing_and_writes_nothing(monkeypatch, tmp_path,
                                                                    httpserver, capsys, reply):
    home = tmp_path / "loom_home"
    monkeypatch.setenv("LOOM_HOME", str(home))
    _mock(httpserver)
    _hidden_link(monkeypatch, _link(_origin(httpserver)))
    _answer(monkeypatch, reply)  # EOF is a non-interactive run: never a silent yes

    rc = cli.main(["enroll", "--device-name", "m"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "not enrolled" in captured.err.lower()
    assert httpserver.log == []  # the host was not even contacted
    assert not (home / "config.json").exists()
    assert CODE not in captured.out + captured.err


@pytest.mark.parametrize("reply", ["y", "Y", "yes", " YES "])
def test_enroll_link_confirmations_accepted(monkeypatch, tmp_path, httpserver, reply):
    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "h"))
    _mock(httpserver)
    _hidden_link(monkeypatch, _link(_origin(httpserver)))
    _answer(monkeypatch, reply)

    assert cli.main(["enroll", "--device-name", "m"]) == 0


def test_enroll_link_to_the_configured_server_asks_nothing(monkeypatch, tmp_path, httpserver):
    home = tmp_path / "loom_home"
    _existing_config(home, httpserver.url_for(""))  # same origin, trailing slash and all
    monkeypatch.setenv("LOOM_HOME", str(home))
    _mock(httpserver)
    _hidden_prompt(monkeypatch, code=_link(_origin(httpserver)))  # input() fails

    assert cli.main(["enroll", "--device-name", "m"]) == 0
    data = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert data["token"] == ENROL_BODY["token"] and data["cache_ttl_s"] == 42


def test_enroll_link_to_another_host_asks_and_refuses_a_silent_repoint(monkeypatch, tmp_path,
                                                                       httpserver, capsys):
    home = tmp_path / "loom_home"
    _existing_config(home, "https://loom.team.example")
    before = (home / "config.json").read_text(encoding="utf-8")
    monkeypatch.setenv("LOOM_HOME", str(home))
    _mock(httpserver)
    _hidden_link(monkeypatch, _link(_origin(httpserver)))
    asked = _answer(monkeypatch, "")  # Enter = the default, No

    rc = cli.main(["enroll", "--device-name", "m"])

    out = capsys.readouterr().out
    assert rc == 1
    assert len(asked) == 1 and "[y/N]" in asked[0]
    assert f"    localhost:{httpserver.port}\n" in out
    assert "loom.team.example" in out  # what it would be repointed AWAY from
    assert httpserver.log == []
    assert (home / "config.json").read_text(encoding="utf-8") == before


def test_enroll_link_to_another_host_repoints_once_confirmed(monkeypatch, tmp_path, httpserver):
    home = tmp_path / "loom_home"
    _existing_config(home, "https://loom.team.example")
    monkeypatch.setenv("LOOM_HOME", str(home))
    _mock(httpserver)
    _hidden_link(monkeypatch, _link(_origin(httpserver)))
    _answer(monkeypatch, "y")

    assert cli.main(["enroll", "--device-name", "m"]) == 0
    data = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert data["server_url"] == _origin(httpserver) and data["cache_ttl_s"] == 42


@pytest.mark.parametrize("server_url", [
    "https://loom.team.example",
    "http://127.0.0.1:{port}",          # same machine, other host spelling: another origin
    "http://localhost:1",               # same host, other port
    "https://localhost:{port}",         # same host and port, other scheme
    "http://localhost:{port}/loom",     # a server under a path is not the link's server
])
def test_enroll_link_contradicting_server_url_is_refused(monkeypatch, tmp_path, httpserver,
                                                         capsys, server_url):
    home = tmp_path / "loom_home"
    monkeypatch.setenv("LOOM_HOME", str(home))
    _mock(httpserver)
    _hidden_prompt(monkeypatch, code=_link(_origin(httpserver)))  # input() fails: no question

    rc = cli.main(["enroll", "--server-url", server_url.format(port=httpserver.port),
                   "--device-name", "m"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "--server-url" in captured.err and "not enrolled" in captured.err.lower()
    assert [r.path for r, _ in httpserver.log] == []
    assert not (home / "config.json").exists()
    assert CODE not in captured.out + captured.err


MALFORMED = [
    "https://evil-host.example/ui/join",                         # no fragment
    "https://evil-host.example/ui/join#",                        # empty fragment
    "https://evil-host.example/ui/join#code=",                   # empty code
    "https://evil-host.example/ui/join#token=S3cretC0de",        # another key
    "https://evil-host.example/ui/join#code=S3cretC0de&next=x",  # more than the code
    "https://evil-host.example/ui/join#code=S3cret/C0de",        # not a code's alphabet
    "https://evil-host.example/ui/join?#code=S3cretC0de",        # a query, even empty
    "https://evil-host.example/ui/join?x=1#code=S3cretC0de",
    "https://evil-host.example/ui/join/#code=S3cretC0de",        # path not exactly /ui/join
    "https://evil-host.example/join#code=S3cretC0de",
    "https://evil-host.example/#code=S3cretC0de",
    "https://evil-host.example#code=S3cretC0de",
    "https://evil-host.example/UI/JOIN#code=S3cretC0de",
    "https://user@evil-host.example/ui/join#code=S3cretC0de",    # userinfo
    "https://evil-host.example:99999/ui/join#code=S3cretC0de",   # no such port
    "https:///ui/join#code=S3cretC0de",                          # no host
    "https://[evil-host.example/ui/join#code=S3cretC0de",        # unclosed IPv6 bracket
    "https://evil_host.example/ui/join#code=S3cretC0de",         # not a host name
    "https://evil-hоst.example/ui/join#code=S3cretC0de",         # Cyrillic о: a look-alike
    "https://evil-host.example/ui/join#code=S3cret C0de",
    "ftp://evil-host.example/ui/join#code=S3cretC0de",           # not https
    "evil-host.example/ui/join#code=S3cretC0de",                 # no scheme
    "//evil-host.example/ui/join#code=S3cretC0de",
    "S3cretC0de evil-host.example",                              # neither code nor link
]


@pytest.mark.parametrize("pasted", MALFORMED)
def test_enroll_malformed_link_is_refused_without_echo(monkeypatch, tmp_path, httpserver, capsys,
                                                       pasted):
    home = tmp_path / "loom_home"
    monkeypatch.setenv("LOOM_HOME", str(home))
    _mock(httpserver)
    _hidden_prompt(monkeypatch, code=pasted)  # input() fails: no question, no URL prompt

    rc = cli.main(["enroll", "--device-name", "m"])

    captured = capsys.readouterr()
    shown = captured.out + captured.err
    assert rc == 1
    assert "loom enroll:" in captured.err
    assert "S3cret" not in shown and "evil-h" not in shown  # nothing of the pasted value
    assert httpserver.log == []
    assert not (home / "config.json").exists()


@pytest.mark.parametrize("pasted", [
    "http://loom.team.example/ui/join#code=S3cretC0de",
    "http://10.0.0.5:8787/ui/join#code=S3cretC0de",
    "http://localhost.evil-host.example/ui/join#code=S3cretC0de",
    "http://127.0.0.1.evil-host.example/ui/join#code=S3cretC0de",
])
def test_enroll_plain_http_link_to_a_non_loopback_host_is_refused(monkeypatch, tmp_path,
                                                                  httpserver, capsys, pasted):
    home = tmp_path / "loom_home"
    monkeypatch.setenv("LOOM_HOME", str(home))
    _hidden_prompt(monkeypatch, code=pasted)

    rc = cli.main(["enroll", "--device-name", "m"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "https" in captured.err
    assert "S3cret" not in captured.out + captured.err
    assert not (home / "config.json").exists()


def test_enroll_link_on_loopback_ip_over_plain_http_is_accepted(monkeypatch, tmp_path,
                                                                httpserver):
    """The loopback exception is the address, not only the name ``localhost``."""
    origin = f"http://127.0.0.1:{httpserver.port}"
    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "h"))
    seen = []
    _mock(httpserver, seen=seen)
    _hidden_prompt(monkeypatch, code=_link(origin))

    assert cli.main(["enroll", "--server-url", origin, "--device-name", "m"]) == 0
    assert seen == [{"code": CODE, "device_name": "m"}]


def test_enroll_bare_code_without_server_url_still_asks_for_the_url(monkeypatch, tmp_path,
                                                                   httpserver, capsys):
    """A bare code carries no server, so the URL prompt stays; the code is still hidden."""
    home = tmp_path / "loom_home"
    monkeypatch.setenv("LOOM_HOME", str(home))
    seen = []
    _mock(httpserver, seen=seen)
    prompts = _hidden_link(monkeypatch, CODE)
    asked = _answer(monkeypatch, httpserver.url_for(""))

    assert cli.main(["enroll", "--device-name", "m"]) == 0
    assert prompts and len(asked) == 1 and "URL" in asked[0]
    assert seen == [{"code": CODE, "device_name": "m"}]
    assert CODE not in capsys.readouterr().out


# --- D19 B2: re-enrolling sends the old token as the bearer, for the SAME server only ----
# Proof of possession: the server revokes that token's chain when it is the code's user's.
# It travels as `Authorization: Bearer`, never in the body (`EnrolRequest` forbids extra
# fields) and never to another server: a token is only ever shown to the server that
# issued it.
def _mock_seeing_headers(httpserver):
    seen = []
    httpserver.expect_request("/api/health", method="GET").respond_with_json(
        {"status": "ok", "version": "0.1.0"})

    def handler(request):
        seen.append({"authorization": request.headers.get("Authorization"),
                     "body": request.get_json()})
        return Response(json.dumps(ENROL_BODY), status=201, content_type="application/json")

    httpserver.expect_request("/api/enroll", method="POST").respond_with_handler(handler)
    return seen


@pytest.mark.parametrize("pasted", ["code", "link"])
def test_re_enrolling_with_the_same_server_sends_the_old_token_as_the_bearer(
        monkeypatch, tmp_path, httpserver, pasted):
    home = tmp_path / "loom_home"
    _existing_config(home, httpserver.url_for(""))  # trailing slash: still the same server
    monkeypatch.setenv("LOOM_HOME", str(home))
    seen = _mock_seeing_headers(httpserver)
    if pasted == "code":
        _hidden_prompt(monkeypatch)
        argv = ["enroll", "--server-url", _origin(httpserver), "--device-name", "m"]
    else:
        _hidden_prompt(monkeypatch, code=_link(_origin(httpserver)))
        argv = ["enroll", "--device-name", "m"]

    assert cli.main(argv) == 0

    assert seen == [{"authorization": "Bearer old-token",
                     "body": {"code": CODE, "device_name": "m"}}]
    data = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert data["token"] == ENROL_BODY["token"]


def test_enrolling_with_another_server_never_sends_the_old_token(monkeypatch, tmp_path,
                                                                httpserver):
    home = tmp_path / "loom_home"
    _existing_config(home, "https://old-loom.example.test")
    monkeypatch.setenv("LOOM_HOME", str(home))
    seen = _mock_seeing_headers(httpserver)
    _hidden_prompt(monkeypatch)

    assert cli.main(["enroll", "--server-url", _origin(httpserver), "--device-name", "m"]) == 0

    assert seen == [{"authorization": None, "body": {"code": CODE, "device_name": "m"}}]
    assert all("old-token" not in str(dict(req.headers)) for req, _ in httpserver.log)


def test_a_first_enrolment_sends_no_bearer(monkeypatch, tmp_path, httpserver):
    home = tmp_path / "loom_home"
    monkeypatch.setenv("LOOM_HOME", str(home))
    seen = _mock_seeing_headers(httpserver)
    _hidden_prompt(monkeypatch)

    assert cli.main(["enroll", "--server-url", _origin(httpserver), "--device-name", "m"]) == 0

    assert seen == [{"authorization": None, "body": {"code": CODE, "device_name": "m"}}]
