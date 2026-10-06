"""Tests for scan_engine — engine dispatch, gitleaks parsing and fail-closed errors (R2.3).

These tests never need the REAL gitleaks binary: every gitleaks behaviour (report JSON,
"leaks found" exit, crash, hang, garbage output, ``version``) is reproduced by an
executable PATH shim, so the unit suite stays hermetic and fast. The real-boundary
oracle against gitleaks 8.30.1 lives in ``test/waves/a/test_goal_e3_scan_engine.py``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from loom import scan_engine, secret_scan
from loom.config import Config

SECRET = "zX9j2kQ8vB4nM7cL1wR5tY6uI3oP0aSdFgHjKlZxCvB"
PLANTED = f'api_key = "{SECRET}"\n'

# One report entry, exactly the shape gitleaks 8.30.1 emits (`--report-format json`).
REPORT = json.dumps(
    [{"RuleID": "generic-api-key", "Description": "Detected a Generic API Key",
      "StartLine": 1, "Match": f'api_key = "{SECRET}"', "Secret": SECRET,
      "Entropy": 5.28673, "Tags": []}]
)


# --- Shims ------------------------------------------------------------------------

def _write_shim(shims: Path, body: str) -> Path:
    shims.mkdir(parents=True, exist_ok=True)
    path = shims / "gitleaks"
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _gitleaks_shim(shims: Path, *, report: str = "[]", exit_code: int = 0,
                   version: str = "8.30.1", record: Path | None = None,
                   sleep: float = 0.0) -> Path:
    """Fake gitleaks: ``version`` prints ``version``; ``stdin`` prints ``report`` and
    exits with ``exit_code``, optionally recording argv/stdin/cwd/env into ``record``."""
    return _write_shim(shims, f"""
import json, os, pathlib, sys, time
if sys.argv[1:2] == ["version"]:
    sys.stdout.write({version!r} + "\\n")
    sys.exit(0)
data = sys.stdin.read()
time.sleep({sleep!r})
rec = {str(record)!r} if {record is not None!r} else None
if rec:
    pathlib.Path(rec).write_text(json.dumps({{
        "argv": sys.argv[1:], "stdin": data, "cwd": os.getcwd(),
        "env_keys": sorted(k for k in os.environ if k.startswith("GITLEAKS")),
    }}))
sys.stdout.write({report!r})
sys.exit({exit_code!r})
""")


@pytest.fixture
def shims(tmp_path, monkeypatch) -> Path:
    """PATH whose head is an empty shim dir (no gitleaks unless a test writes one)."""
    d = tmp_path / "shims"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    return d


@pytest.fixture
def no_binary(tmp_path, monkeypatch) -> None:
    """PATH with nothing on it at all — gitleaks is provably absent."""
    empty = tmp_path / "empty_bin"
    empty.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PATH", str(empty))


def _cfg(tmp_path, engine: str, pin: str | None = "8.30.1") -> Config:
    return Config(loom_home=tmp_path / "home", server_url="http://127.0.0.1:9",
                  token="t", scanner_engine=engine, scanner_gitleaks_version=pin)


# --- Engine resolution ------------------------------------------------------------

def test_cfg_none_is_pure_regex(shims, tmp_path):
    """``cfg is None`` keeps the phase-1 behaviour and never touches the binary."""
    sentinel = tmp_path / "called"
    _write_shim(shims, f"import pathlib, sys\n"
                       f"pathlib.Path({str(sentinel)!r}).write_text('x')\nsys.exit(99)\n")
    assert {f.kind for f in scan_engine.scan("sk-AbCdEfGhIjKlMnOpQrStUv", None)} == {"openai_key"}
    assert not sentinel.exists()


def test_engine_regex_never_invokes_the_binary(shims, tmp_path):
    sentinel = tmp_path / "called"
    _write_shim(shims, f"import pathlib, sys\n"
                       f"pathlib.Path({str(sentinel)!r}).write_text('x')\nsys.exit(99)\n")
    cfg = _cfg(tmp_path, "regex")
    assert {f.kind for f in scan_engine.scan("password=hunter2", cfg)} == {"password_assignment"}
    assert scan_engine.active_engine(cfg) == scan_engine.EngineInfo(
        name="regex", version=None, pinned_version="8.30.1"
    )
    assert not sentinel.exists()


def test_auto_without_binary_falls_back_to_regex(no_binary, tmp_path):
    cfg = _cfg(tmp_path, "auto")
    assert {f.kind for f in scan_engine.scan("sk-AbCdEfGhIjKlMnOpQrStUv", cfg)} == {"openai_key"}
    info = scan_engine.active_engine(cfg)
    assert (info.name, info.version, info.pinned_version) == ("regex", None, "8.30.1")


def test_explicit_gitleaks_without_binary_raises(no_binary, tmp_path):
    """An explicit choice NEVER silently degrades: the raise becomes quarantine (I4)."""
    cfg = _cfg(tmp_path, "gitleaks")
    with pytest.raises(FileNotFoundError):
        scan_engine.scan(PLANTED, cfg)
    with pytest.raises(FileNotFoundError):
        scan_engine.active_engine(cfg)


def test_unknown_engine_value_raises(no_binary, tmp_path):
    """A typo in config.json must not silently pick the weakest scanner."""
    cfg = _cfg(tmp_path, "gitleeks")
    with pytest.raises(ValueError):
        scan_engine.scan(PLANTED, cfg)
    with pytest.raises(ValueError):
        scan_engine.active_engine(cfg)


def test_engine_value_is_case_and_space_tolerant(shims, tmp_path):
    _gitleaks_shim(shims, report="[]")
    cfg = _cfg(tmp_path, "  GitLeaks ")
    assert scan_engine.active_engine(cfg).name == "gitleaks"


# --- Gitleaks invocation and report parsing ---------------------------------------

def test_gitleaks_findings_are_parsed_and_redacted(shims, tmp_path):
    _gitleaks_shim(shims, report=REPORT, exit_code=scan_engine.GITLEAKS_LEAKS_EXIT_CODE)
    findings = scan_engine.scan(PLANTED.replace(SECRET, "x"), _cfg(tmp_path, "gitleaks"))
    assert findings == [secret_scan.Finding(kind="generic-api-key",
                                           redacted="zX9j" + "*" * (len(SECRET) - 4))]
    assert isinstance(findings[0], secret_scan.Finding)


def test_findings_never_carry_the_full_secret(shims, tmp_path):
    _gitleaks_shim(shims, report=REPORT, exit_code=scan_engine.GITLEAKS_LEAKS_EXIT_CODE)
    for f in scan_engine.scan(PLANTED, _cfg(tmp_path, "gitleaks")):
        assert SECRET not in f.kind
        assert SECRET not in f.redacted


def test_leaks_exit_code_is_a_result_not_an_error(shims, tmp_path):
    """The non-zero "leaks found" exit is a RESULT — it must not raise."""
    _gitleaks_shim(shims, report=REPORT, exit_code=scan_engine.GITLEAKS_LEAKS_EXIT_CODE)
    assert scan_engine.scan(PLANTED, _cfg(tmp_path, "gitleaks"))


def test_clean_report_yields_no_findings(shims, tmp_path):
    _gitleaks_shim(shims, report="[]", exit_code=0)
    assert scan_engine.scan("nothing to see here\n", _cfg(tmp_path, "gitleaks")) == []


def test_invocation_is_pinned_and_isolated(shims, tmp_path, monkeypatch):
    """The pinned 8.30.1 argv, the exact stdin, an isolated cwd and a sanitized env
    (a stray ``GITLEAKS_CONFIG`` could disable every rule = silent fail-open)."""
    record = tmp_path / "record.json"
    _gitleaks_shim(shims, report="[]", record=record)
    monkeypatch.setenv("GITLEAKS_CONFIG", "/tmp/evil.toml")
    monkeypatch.setenv("GITLEAKS_CONFIG_TOML", "title='evil'")

    scan_engine.scan(PLANTED, _cfg(tmp_path, "gitleaks"))

    rec = json.loads(record.read_text())
    assert rec["argv"][0] == "stdin"
    assert rec["argv"][1:] == ["--no-banner", "--log-level", "error",
                               "--ignore-gitleaks-allow",
                               "--report-format", "json", "--report-path", "-",
                               "--exit-code", str(scan_engine.GITLEAKS_LEAKS_EXIT_CODE)]
    assert rec["stdin"] == PLANTED
    assert rec["env_keys"] == []                       # GITLEAKS_CONFIG* stripped
    assert rec["cwd"] != os.getcwd()                   # no ./.gitleaks.toml pickup


def test_argv_disables_the_gitleaks_allow_marker(shims, tmp_path):
    """Regression, review F1 (fail-open). Verified on 8.30.1: the literal string
    ``gitleaks:allow`` on a line — in PROSE, no comment syntax needed — drops every
    finding on it and gitleaks exits 0, i.e. "clean", so nothing raises and nothing
    quarantines. The scanned text is an LLM summary of a transcript, so the marker is
    attacker-influenceable; the flag must never fall out of the pinned argv again.
    (The real-binary half of this regression lives in the wave-a goal.)"""
    record = tmp_path / "record.json"
    _gitleaks_shim(shims, report="[]", record=record)
    scan_engine.scan("the agent ran gitleaks:allow\n", _cfg(tmp_path, "gitleaks"))
    assert "--ignore-gitleaks-allow" in json.loads(record.read_text())["argv"]


def test_scan_does_not_leave_its_temporary_workdir_behind(shims, tmp_path):
    record = tmp_path / "record.json"
    _gitleaks_shim(shims, report="[]", record=record)
    scan_engine.scan(PLANTED, _cfg(tmp_path, "gitleaks"))
    assert not Path(json.loads(record.read_text())["cwd"]).exists()


# --- Errors are ERRORS, never empty results (fail-closed, I4) ----------------------

def test_crashing_binary_raises(shims, tmp_path):
    _write_shim(shims, 'import sys\nsys.stderr.write("boom\\n")\nsys.exit(3)\n')
    with pytest.raises(scan_engine.GitleaksError):
        scan_engine.scan(PLANTED, _cfg(tmp_path, "gitleaks"))


def test_gitleaks_own_error_exit_1_raises(shims, tmp_path):
    """gitleaks exits 1 on fatal errors (e.g. unreadable config); because we move the
    "leaks found" code to 2, exit 1 is unambiguously an ERROR."""
    _write_shim(shims, 'import sys\n'
                       'sys.stderr.write("FTL unable to load gitleaks config\\n")\n'
                       'sys.exit(1)\n')
    with pytest.raises(scan_engine.GitleaksError):
        scan_engine.scan(PLANTED, _cfg(tmp_path, "gitleaks"))


def test_unparseable_report_raises(shims, tmp_path):
    _gitleaks_shim(shims, report="not json at all", exit_code=0)
    with pytest.raises(scan_engine.GitleaksError):
        scan_engine.scan(PLANTED, _cfg(tmp_path, "gitleaks"))


def test_empty_report_raises(shims, tmp_path):
    _gitleaks_shim(shims, report="", exit_code=0)
    with pytest.raises(scan_engine.GitleaksError):
        scan_engine.scan(PLANTED, _cfg(tmp_path, "gitleaks"))


def test_leaks_exit_with_empty_report_raises(shims, tmp_path):
    """"Leaks found" plus an empty report is incoherent — returning [] there would be
    a silent fail-open."""
    _gitleaks_shim(shims, report="[]", exit_code=scan_engine.GITLEAKS_LEAKS_EXIT_CODE)
    with pytest.raises(scan_engine.GitleaksError):
        scan_engine.scan(PLANTED, _cfg(tmp_path, "gitleaks"))


def test_non_list_report_raises(shims, tmp_path):
    _gitleaks_shim(shims, report='{"RuleID": "x"}', exit_code=0)
    with pytest.raises(scan_engine.GitleaksError):
        scan_engine.scan(PLANTED, _cfg(tmp_path, "gitleaks"))


def test_timeout_raises_and_does_not_return_empty(shims, tmp_path, monkeypatch):
    _gitleaks_shim(shims, report="[]", sleep=3.0)
    monkeypatch.setattr(scan_engine, "GITLEAKS_TIMEOUT_S", 0.3)
    with pytest.raises(scan_engine.GitleaksError):
        scan_engine.scan(PLANTED, _cfg(tmp_path, "gitleaks"))


def test_exit_zero_with_stderr_noise_raises(shims, tmp_path):
    """Regression, review F2. gitleaks can log a hard error and still exit 0 with an
    empty report (reproduced on 8.30.1 with a bad --baseline-path), so exit 0 alone
    does not prove the scan ran. A healthy run is silent — verified 0 bytes of stderr
    for clean AND leaks-found — so noise on a "clean" verdict is an ERROR."""
    _write_shim(shims, 'import sys\nsys.stdin.read()\n'
                       'sys.stderr.write("ERR Could not load baseline\\n")\n'
                       'sys.stdout.write("[]")\nsys.exit(0)\n')
    with pytest.raises(scan_engine.GitleaksError):
        scan_engine.scan(PLANTED, _cfg(tmp_path, "gitleaks"))


def test_exit_zero_with_silent_stderr_is_clean(shims, tmp_path):
    """The other half of F2: a silent exit 0 must stay a normal clean verdict."""
    _write_shim(shims, 'import sys\nsys.stdin.read()\nsys.stdout.write("[]")\n'
                       'sys.exit(0)\n')
    assert scan_engine.scan("nothing here\n", _cfg(tmp_path, "gitleaks")) == []


def test_leaks_exit_with_stderr_noise_still_reports_findings(shims, tmp_path):
    """Deliberate asymmetry: with findings in hand the caller quarantines either way,
    so noise on a leaks-found run is not promoted to an error."""
    _write_shim(shims, f'import sys\nsys.stdin.read()\n'
                       f'sys.stderr.write("ERR something\\n")\n'
                       f'sys.stdout.write({REPORT!r})\n'
                       f'sys.exit({scan_engine.GITLEAKS_LEAKS_EXIT_CODE})\n')
    assert scan_engine.scan(PLANTED, _cfg(tmp_path, "gitleaks"))


@pytest.mark.parametrize(
    "echoed",
    [
        '{secret}',            # bare, the only shape the old token check caught
        'secret="{secret}"',   # adjacent punctuation defeated the old check
        'content={secret}',
        '[{secret}]',
        '{secret},',
        'tok={short}',         # shorter than the old 8-char token floor
    ],
)
def test_error_message_never_echoes_the_scanned_content(shims, tmp_path, echoed):
    """Regression, review F3 (secret leak to the terminal). The message reaches the
    user as an uncaught traceback from push-quarantine, so NOTHING from stderr may be
    quoted — no substring/word heuristic, which punctuation or a short value defeats."""
    short = "hunter2"
    text = echoed.format(secret=SECRET, short=short) + "\n"
    _write_shim(shims, "import sys\nsys.stderr.write(sys.stdin.read())\nsys.exit(3)\n")
    with pytest.raises(scan_engine.GitleaksError) as excinfo:
        scan_engine.scan(text, _cfg(tmp_path, "gitleaks"))
    message = str(excinfo.value)
    assert SECRET not in message
    assert short not in message
    assert "exit code 3" in message                 # still diagnosable


def test_regex_engine_exception_still_propagates(shims, tmp_path, monkeypatch):
    """The phase-1 fail-closed contract (I4) is unchanged: a scanner crash reaches
    the caller, it is never swallowed into an empty finding list."""
    _gitleaks_shim(shims, report="[]")

    def boom(_text):
        raise RuntimeError("scanner exploded")

    monkeypatch.setattr(scan_engine.secret_scan, "scan", boom)
    with pytest.raises(RuntimeError):
        scan_engine.scan(PLANTED, _cfg(tmp_path, "gitleaks"))


# --- Defense in depth: union with the phase-1 families -----------------------------

def test_union_of_gitleaks_and_regex_families(shims, tmp_path):
    _gitleaks_shim(shims, report=REPORT, exit_code=scan_engine.GITLEAKS_LEAKS_EXIT_CODE)
    text = PLANTED + "write to dev2@example.test\ntoken sk-AbCdEfGhIjKlMnOpQrStUv\n"
    kinds = [f.kind for f in scan_engine.scan(text, _cfg(tmp_path, "auto"))]
    assert kinds[0] == "generic-api-key"          # gitleaks first, then the regex set
    assert {"email", "openai_key"} <= set(kinds)


def test_union_deduplicates_identical_findings(shims, tmp_path):
    """Both engines catching the same value must not double-report it."""
    dup = json.dumps([{"RuleID": "email", "Secret": "dev2@example.test"},
                      {"RuleID": "email", "Secret": "dev2@example.test"}])
    _gitleaks_shim(shims, report=dup, exit_code=scan_engine.GITLEAKS_LEAKS_EXIT_CODE)
    findings = scan_engine.scan("dev2@example.test\n", _cfg(tmp_path, "gitleaks"))
    assert findings == [secret_scan.Finding(kind="email", redacted="dev2" + "*" * 13)]


def test_report_entry_without_rule_id_still_reports(shims, tmp_path):
    _gitleaks_shim(shims, report=json.dumps([{"Secret": SECRET}]),
                   exit_code=scan_engine.GITLEAKS_LEAKS_EXIT_CODE)
    findings = scan_engine.scan(PLANTED, _cfg(tmp_path, "gitleaks"))
    assert [f.kind for f in findings] == ["gitleaks"]
    assert SECRET not in findings[0].redacted


# --- active_engine ----------------------------------------------------------------

def test_active_engine_reports_version_and_pin(shims, tmp_path):
    _gitleaks_shim(shims, version="8.30.1")
    info = scan_engine.active_engine(_cfg(tmp_path, "auto"))
    assert info == scan_engine.EngineInfo(name="gitleaks", version="8.30.1",
                                          pinned_version="8.30.1")


def test_active_engine_accepts_a_v_prefixed_version(shims, tmp_path):
    _gitleaks_shim(shims, version="v8.30.1")
    assert scan_engine.active_engine(_cfg(tmp_path, "auto")).version == "8.30.1"


@pytest.mark.parametrize(
    "printed, expected",
    [
        ("8.30.1", "8.30.1"),
        ("v8.30.1", "8.30.1"),
        ("8.30.1-rc2", "8.30.1"),
        ("8.30.1\nbuilt with go1.24", "8.30.1"),      # extra lines are ignored
        ("2026-07-30 commit 1a2b3c4 8.30.1", None),   # date first: NOT a version
        ("gitleaks version 8.30.1", None),
        ("unknown", None),
    ],
)
def test_version_is_read_from_the_start_of_the_first_line(shims, tmp_path,
                                                          printed, expected):
    """Regression, review F7. An unanchored search would pick the date or the commit
    hash out of a chattier build and report it as the version — doctor would then
    compare a bogus value against the pin (AC-2.3d). Unparseable must yield None,
    never a wrong answer."""
    _gitleaks_shim(shims, version=printed)
    assert scan_engine.active_engine(_cfg(tmp_path, "auto")).version == expected


def test_active_engine_version_is_none_when_unqueryable(shims, tmp_path):
    """An unqueryable version is reported as None (doctor's problem), never a crash."""
    _write_shim(shims, "import sys\nsys.exit(7)\n")
    info = scan_engine.active_engine(_cfg(tmp_path, "auto", pin=None))
    assert (info.name, info.version, info.pinned_version) == ("gitleaks", None, None)


def test_active_engine_with_cfg_none(tmp_path):
    assert scan_engine.active_engine() == scan_engine.EngineInfo(
        name="regex", version=None, pinned_version=None
    )
