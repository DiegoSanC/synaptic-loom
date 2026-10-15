"""FROZEN GOAL — E3 gitleaks-engine (F2.3 / spec R2.3). Read-only to implementers.

Oracle properties: the REAL gitleaks binary (pinned 8.30.1 on this machine) is the
boundary; PATH manipulation covers absence. The trap was validated empirically at
plan time (2026-07-30): the planted high-entropy token is caught by gitleaks rule
``generic-api-key`` (entropy 5.29) and provably missed by the phase-1 regex set —
so a plausible-but-wrong implementation that quietly keeps using the regex set for
everything FAILS this goal.

Run: .venv/bin/python -m pytest test/waves/a/test_goal_e3_scan_engine.py -q
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _goalkit as kit  # noqa: E402

from loom import scan_engine, secret_scan  # noqa: E402
from loom.config import Config  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("gitleaks") is None,
    reason="E3 goal requires the real gitleaks binary (brew/apt install gitleaks)",
)

PLANTED = f"Decision: rotate the key.\n{kit.HIGH_ENTROPY_LINE}\n"
CLEAN = (
    "Reviewed commit 3f2a1b4c5d6e7f8091a2b3c4d5e6f708192a3b4c and issue "
    "id 550e8400-e29b-41d4-a716-446655440000; no credentials involved.\n"
)


def _cfg(tmp_path, *, engine: str, pin: str | None = kit.GITLEAKS_PIN) -> Config:
    home = kit.loom_home(tmp_path)
    return Config(
        loom_home=home, server_url="http://127.0.0.1:9", token="goal-token",
        scanner_engine=engine, scanner_gitleaks_version=pin,
    )


# --------------------------------------------------------------------------- #
# The trap is real (guard assertions — keep the goal honest)                   #
# --------------------------------------------------------------------------- #
def test_guard_regex_set_misses_the_planted_token():
    assert secret_scan.scan(PLANTED) == []


# --------------------------------------------------------------------------- #
# auto with the binary present → gitleaks is the primary engine (AC-2.3a)      #
# --------------------------------------------------------------------------- #
def test_auto_gitleaks_catches_high_entropy_token(tmp_path):
    cfg = _cfg(tmp_path, engine="auto")
    findings = scan_engine.scan(PLANTED, cfg)
    assert findings, "gitleaks must catch the planted high-entropy token"
    assert all(isinstance(f, secret_scan.Finding) for f in findings)  # AC-2.3c shape


def test_findings_never_leak_the_full_secret(tmp_path):
    cfg = _cfg(tmp_path, engine="auto")
    for f in scan_engine.scan(PLANTED, cfg):
        assert kit.HIGH_ENTROPY_SECRET not in f.kind
        assert kit.HIGH_ENTROPY_SECRET not in f.redacted


def test_clean_text_produces_no_findings(tmp_path):
    cfg = _cfg(tmp_path, engine="auto")
    assert scan_engine.scan(CLEAN, cfg) == []


def test_union_keeps_phase1_families(tmp_path):
    """Defense-in-depth: gitleaks has no email/PII rule — the phase-1 families must
    not regress when gitleaks is the primary engine."""
    cfg = _cfg(tmp_path, engine="auto")
    text = PLANTED + "Contact dev2@example.test for the rotation.\n"
    kinds = {f.kind for f in scan_engine.scan(text, cfg)}
    assert "email" in kinds, "phase-1 regex families must be unioned in"
    assert any(k != "email" for k in kinds), "gitleaks finding must also be present"


def test_active_engine_reports_gitleaks_and_pin(tmp_path):
    cfg = _cfg(tmp_path, engine="auto")
    info = scan_engine.active_engine(cfg)
    assert info.name == "gitleaks"
    assert info.version and re.fullmatch(r"\d+\.\d+\.\d+", info.version)
    assert info.pinned_version == kit.GITLEAKS_PIN  # AC-2.3d


# --------------------------------------------------------------------------- #
# Engine selection under PATH manipulation                                     #
# --------------------------------------------------------------------------- #
def test_auto_without_binary_no_longer_falls_back_to_regex(tmp_path, monkeypatch):
    """AMENDED by D16 (2026-10-15; was ``test_auto_without_binary_falls_back_to_regex``,
    which asserted the regex-only scan and ``active_engine().name == "regex"``). ``auto``
    is now a legacy alias of ``gitleaks``: no binary is an ERROR for every entry point,
    never a silent downgrade — the caller pauses capture or fails closed."""
    shims = kit.shim_dir(tmp_path)
    monkeypatch.setenv("PATH", kit.base_path_with(shims, include_gitleaks=False))
    cfg = _cfg(tmp_path, engine="auto")
    text = "token sk-AbCdEfGhIjKlMnOpQrStUv\n"
    with pytest.raises(scan_engine.ScannerUnavailable):
        scan_engine.scan(text, cfg)
    with pytest.raises(scan_engine.ScannerUnavailable):
        scan_engine.active_engine(cfg)
    with pytest.raises(scan_engine.ScannerUnavailable):
        scan_engine.ensure_ready(cfg)
    with pytest.raises(scan_engine.ScannerUnavailable):
        scan_engine.redact(text, cfg)


def test_explicit_gitleaks_without_binary_raises(tmp_path, monkeypatch):
    """An explicit engine choice NEVER silently falls back: the raise reaches the
    fail-closed caller (I4) and becomes quarantine."""
    shims = kit.shim_dir(tmp_path)
    monkeypatch.setenv("PATH", kit.base_path_with(shims, include_gitleaks=False))
    cfg = _cfg(tmp_path, engine="gitleaks")
    with pytest.raises(Exception):
        scan_engine.scan(PLANTED, cfg)


def test_explicit_regex_is_refused_and_never_invokes_gitleaks(tmp_path, monkeypatch):
    """engine='regex' must not touch the binary at all (poisoned shim proves it).

    AMENDED by D16 (2026-10-15; was ``test_explicit_regex_never_invokes_gitleaks``, which
    also asserted a regex-only scan returning ``password_assignment``). ``regex`` is no
    longer accepted for capture: every entry point REFUSES it (capture pauses, the other
    callers fail closed) — and the refusal still never executes the binary."""
    shims = kit.shim_dir(tmp_path)
    sentinel = tmp_path / "gitleaks_was_called"
    kit.write_shim(shims, "gitleaks", f"""
import pathlib, sys
pathlib.Path({str(sentinel)!r}).write_text("called")
sys.exit(99)
""")
    monkeypatch.setenv("PATH", f"{shims}:{Path('/usr/bin')}:{Path('/bin')}")
    cfg = _cfg(tmp_path, engine="regex")
    for call in (lambda: scan_engine.scan("password=hunter2\n", cfg),
                 lambda: scan_engine.redact("password=hunter2\n", cfg),
                 lambda: scan_engine.active_engine(cfg),
                 lambda: scan_engine.ensure_ready(cfg)):
        with pytest.raises(scan_engine.ScannerUnavailable) as exc:
            call()
        assert "hunter2" not in str(exc.value)
    assert not sentinel.exists(), "regex engine must never execute gitleaks"


def test_broken_gitleaks_binary_raises_for_fail_closed(tmp_path, monkeypatch):
    """A binary that dies mid-scan is an ERROR (raise), never an empty result —
    otherwise a crashing scanner would let secrets through (AC-2.3c / I4)."""
    shims = kit.shim_dir(tmp_path)
    kit.write_shim(shims, "gitleaks", """
import sys
sys.stderr.write("boom: internal error\\n")
sys.exit(3)
""")
    monkeypatch.setenv("PATH", kit.base_path_with(shims, include_gitleaks=True))
    cfg = _cfg(tmp_path, engine="gitleaks")
    with pytest.raises(Exception):
        scan_engine.scan(PLANTED, cfg)


# --------------------------------------------------------------------------- #
# ADDED post-review (E3 security review, finding F1) — additive only, no        #
# existing assertion touched. The `gitleaks:allow` marker must not silence us.  #
# --------------------------------------------------------------------------- #
# Validated against the real 8.30.1 (2026-07-30): with the default flags BOTH
# variants below return rc=0 and an EMPTY report — "clean", not an error — so a
# scanner that omits --ignore-gitleaks-allow silently stops quarantining. The
# marker is per-line and needs no comment syntax: prose is enough. What we scan is
# an LLM summary of a Claude Code transcript, and sessions in repos that use
# gitleaks quote that marker routinely, so this is reachable input.
ALLOW_MARKER_COMMENT = f"{kit.HIGH_ENTROPY_LINE}  # gitleaks:allow (already rotated)\n"
ALLOW_MARKER_PROSE = (
    "The dev pasted gitleaks:allow into the chat, then "
    f'api_key = "{kit.HIGH_ENTROPY_SECRET}" appeared.\n'
)


@pytest.mark.parametrize("text", [ALLOW_MARKER_COMMENT, ALLOW_MARKER_PROSE])
def test_gitleaks_allow_marker_cannot_silence_the_scan(tmp_path, text):
    assert secret_scan.scan(text) == []          # the regex union cannot backstop this
    findings = scan_engine.scan(text, _cfg(tmp_path, engine="auto"))
    assert findings, "a 'gitleaks:allow' marker in the scanned text must not disarm the scanner"
    assert all(kit.HIGH_ENTROPY_SECRET not in f.redacted for f in findings)


# --------------------------------------------------------------------------- #
# Backwards compatibility (phase-1 call shape)                                 #
# --------------------------------------------------------------------------- #
def test_scan_with_none_cfg_behaves_like_phase1():
    kinds = {f.kind for f in scan_engine.scan("token sk-AbCdEfGhIjKlMnOpQrStUv", None)}
    assert "openai_key" in kinds
