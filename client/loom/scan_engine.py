"""Scanner engine dispatch (PHASE-2-3-SPEC R2.3) — FROZEN INTERFACE (Loom phase 2).

Facade in front of the secret scanners. Phase 1 shipped a single engine (the curated
regex set in :mod:`loom.secret_scan`); phase 2 adds gitleaks as the primary engine
with the regex set as built-in fallback. Both processor step 8 and
``loom push-quarantine`` call THIS module, never an engine directly, so engine
selection is decided in exactly one place (AC-2.3b).

Frozen contract (bootstrap delta; implementers fill the body, never change the shape):

- ``scan(text, cfg=None) -> list[secret_scan.Finding]`` — findings from the ACTIVE
  engine. ``cfg is None`` (or engine ``"regex"``) → regex set. Engine ``"auto"`` →
  gitleaks when the binary is available, else regex. Engine ``"gitleaks"`` → gitleaks;
  its absence is an ERROR (raise), which the fail-closed caller (I4) converts into
  quarantine — never a silent fallback for an explicit choice.
- ``active_engine(cfg=None) -> EngineInfo`` — which engine ``scan`` would use, with
  the live gitleaks version and the pinned one (doctor + contract check, AC-2.3d).
- Exceptions propagate to the caller: the fail-closed policy (scanner error == hit)
  lives in the processor/CLI, exactly as with phase-1 ``secret_scan.scan`` (I4).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from loom import secret_scan
from loom.config import Config

GITLEAKS_BINARY = "gitleaks"

# Pinned invocation, probed against gitleaks 8.30.1 (`gitleaks stdin --help`, 2026-07-30):
#
#   gitleaks stdin --no-banner --log-level error --ignore-gitleaks-allow \
#            --report-format json --report-path - --exit-code 2
#
# - ``stdin``: scans the text we feed it, no git repo and no temp file on disk.
# - ``--report-path -``: the JSON report goes to stdout; logs stay on stderr.
# - ``--log-level error``: gitleaks logs each finding at info/warn level — with the
#   secret in the line. Silencing them keeps the whole value out of stderr (I5/I6).
# - ``--ignore-gitleaks-allow``: MANDATORY here, not a preference. By default the
#   literal string ``gitleaks:allow`` anywhere on a line drops every finding on that
#   line and gitleaks then exits 0 — "clean", not an error, so nothing would raise and
#   nothing would quarantine. Verified on 8.30.1: the marker works in plain PROSE, no
#   comment syntax needed. What we scan is an LLM summary of a Claude Code transcript
#   (arbitrary, attacker-influenceable text, and sessions in repos that use gitleaks
#   quote that marker routinely), so without this flag one stray word disables every
#   gitleaks family — including the high-entropy generic token of AC-2.3a, which the
#   regex union cannot backstop.
# - ``--exit-code 2``: THE crux of AC-2.3c. gitleaks exits 1 both for "leaks found"
#   (default) and for its own fatal errors (verified: a missing ``--config`` file
#   exits 1). Moving the leak code to 2 makes the mapping unambiguous:
#       0 → clean · 2 → leaks found (a RESULT) · anything else → ERROR (raise).
GITLEAKS_LEAKS_EXIT_CODE = 2
GITLEAKS_TIMEOUT_S = 60
GITLEAKS_VERSION_TIMEOUT_S = 10

# gitleaks resolves its ruleset from ``$GITLEAKS_CONFIG`` / ``$GITLEAKS_CONFIG_TOML`` /
# ``./.gitleaks.toml`` and its ignore list from ``./.gitleaksignore`` (verified on
# 8.30.1: each of them silences the planted token). Since phase 2 ships the DEFAULT
# ruleset only, the subprocess runs in an empty temp cwd with those env vars stripped —
# otherwise a file in whatever directory the processor happens to run from could
# disable every rule, and the scanner would fail OPEN.
_GITLEAKS_CONFIG_ENV = ("GITLEAKS_CONFIG", "GITLEAKS_CONFIG_TOML")

# Applied with ``.match`` to the FIRST line of ``gitleaks version`` (exactly "8.30.1"
# on 8.30.1). Anchoring is deliberate: an unanchored search would pick a date or a
# commit hash out of a chattier build's output and report it as the version, silently
# breaking doctor's pin comparison (AC-2.3d). Unparseable → None, never a wrong answer.
_VERSION_RE = re.compile(r"v?(\d+\.\d+\.\d+)")


class GitleaksError(RuntimeError):
    """The gitleaks engine could not produce a trustworthy verdict.

    Always raised, never converted into an empty finding list: the fail-closed caller
    (processor step 8 / ``push-quarantine``, I4) treats it as a hit and quarantines.
    """


@dataclass(frozen=True)
class EngineInfo:
    """Active engine snapshot for doctor / contract checks.

    - ``name``: ``"gitleaks"`` or ``"regex"``.
    - ``version``: live engine version (``None`` for regex or an unqueryable binary).
    - ``pinned_version``: ``cfg.scanner_gitleaks_version`` (``None`` = unpinned).
    """

    name: str
    version: str | None
    pinned_version: str | None


def scan(text: str, cfg: Config | None = None) -> list[secret_scan.Finding]:
    """Findings from the active engine (see the frozen contract above).

    When gitleaks is the active engine the result is the UNION of both engines
    (defense in depth): gitleaks brings entropy/vendor rules the regex set has no
    chance against, the regex set brings families gitleaks does not ship — e.g. the
    ``email`` PII rule of phase 1. Exact duplicates are collapsed.
    """
    name, binary = _resolve(cfg)
    if name == "regex":
        return secret_scan.scan(text)
    # Both engines run; either one raising propagates (fail-closed, I4).
    return _merge(_scan_with_gitleaks(text, binary), secret_scan.scan(text))


def active_engine(cfg: Config | None = None) -> EngineInfo:
    """Engine that :func:`scan` would use for this ``cfg`` (doctor, AC-2.4c)."""
    name, binary = _resolve(cfg)
    return EngineInfo(
        name=name,
        version=_gitleaks_version(binary) if name == "gitleaks" else None,
        pinned_version=cfg.scanner_gitleaks_version if cfg else None,
    )


# --------------------------------------------------------------------------------- #
# Engine resolution                                                                   #
# --------------------------------------------------------------------------------- #
def _resolve(cfg: Config | None) -> tuple[str, str | None]:
    """``(engine_name, binary_path)`` for this config — the single decision point.

    ``scan`` and ``active_engine`` share it, so what doctor reports is exactly what
    the scan does (AC-2.3b/AC-2.3d). PATH is probed here, at call time.
    """
    if cfg is None:
        return "regex", None
    engine = (cfg.scanner_engine or "").strip().lower()
    if engine == "regex":
        # Never even look for the binary: an explicit "regex" must not execute it.
        return "regex", None
    if engine == "auto":
        binary = shutil.which(GITLEAKS_BINARY)
        # Silent fallback is fine ONLY for "auto"; active_engine makes it visible.
        return ("gitleaks", binary) if binary else ("regex", None)
    if engine == "gitleaks":
        binary = shutil.which(GITLEAKS_BINARY)
        if binary is None:
            # An explicit choice never degrades silently: raise so the fail-closed
            # caller quarantines instead of scanning with a weaker engine (I4).
            raise FileNotFoundError(
                f"scanner.engine is 'gitleaks' but no '{GITLEAKS_BINARY}' binary was "
                "found on PATH (install it or set scanner.engine to 'auto'/'regex')"
            )
        return "gitleaks", binary
    raise ValueError(
        f"unknown scanner.engine {cfg.scanner_engine!r} (expected 'auto', "
        "'gitleaks' or 'regex')"
    )


# --------------------------------------------------------------------------------- #
# gitleaks                                                                            #
# --------------------------------------------------------------------------------- #
def _gitleaks_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in _GITLEAKS_CONFIG_ENV:
        env.pop(key, None)
    return env


def _run_gitleaks(binary: str, args: list[str], *, stdin: bytes,
                  timeout_s: float) -> subprocess.CompletedProcess[bytes]:
    """Runs ``binary`` in an empty temp cwd with a sanitized env and a hard timeout."""
    with tempfile.TemporaryDirectory(prefix="loom-gitleaks-") as workdir:
        return subprocess.run(  # noqa: S603 — fixed argv, binary resolved via PATH
            [binary, *args],
            input=stdin,
            capture_output=True,
            timeout=timeout_s,
            cwd=workdir,
            env=_gitleaks_env(),
            check=False,
        )


def _scan_with_gitleaks(text: str, binary: str | None) -> list[secret_scan.Finding]:
    """Findings from the real gitleaks binary; ANY doubt about the run is an error."""
    if binary is None:  # pragma: no cover — _resolve never yields gitleaks without one
        raise FileNotFoundError(f"no '{GITLEAKS_BINARY}' binary resolved")
    args = [
        "stdin", "--no-banner", "--log-level", "error", "--ignore-gitleaks-allow",
        "--report-format", "json", "--report-path", "-",
        "--exit-code", str(GITLEAKS_LEAKS_EXIT_CODE),
    ]
    try:
        proc = _run_gitleaks(binary, args, stdin=text.encode("utf-8"),
                             timeout_s=GITLEAKS_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        # The partial output may hold the secret — only the bound is reported.
        raise GitleaksError(
            f"gitleaks timed out after {GITLEAKS_TIMEOUT_S}s"
        ) from exc
    except OSError as exc:
        raise GitleaksError(f"gitleaks could not be executed: {exc}") from exc

    if proc.returncode not in (0, GITLEAKS_LEAKS_EXIT_CODE):
        raise GitleaksError(
            f"gitleaks failed with exit code {proc.returncode} "
            f"({_stderr_note(proc.stderr)})"
        )
    if proc.returncode == 0 and proc.stderr.strip():
        # A healthy run is SILENT: verified on 8.30.1 that both "clean" and "leaks
        # found" write 0 bytes to stderr under --log-level error (empty, unicode,
        # multi-thousand-line inputs included). gitleaks can however log a hard error
        # and still exit 0 with an empty report (reproduced with a bad
        # --baseline-path), so "exit 0" alone does not prove the scan was complete —
        # anything on stderr makes the empty verdict untrustworthy, and untrustworthy
        # is an ERROR (I4), never a clean bill of health.
        raise GitleaksError(
            f"gitleaks exited 0 but logged to stderr ({_stderr_note(proc.stderr)}); "
            "the report cannot be trusted"
        )

    findings = _parse_report(proc.stdout)
    if proc.returncode == GITLEAKS_LEAKS_EXIT_CODE and not findings:
        # "Leaks found" with nothing to show: trusting the empty list would fail OPEN.
        raise GitleaksError("gitleaks reported leaks but its JSON report was empty")
    return findings


def _parse_report(stdout: bytes) -> list[secret_scan.Finding]:
    """Parses the ``--report-format json`` array; anything unexpected raises."""
    raw = stdout.decode("utf-8", errors="replace").strip()
    try:
        report = json.loads(raw) if raw else None
    except json.JSONDecodeError as exc:
        raise GitleaksError(f"gitleaks report is not valid JSON: {exc}") from exc
    if not isinstance(report, list):
        raise GitleaksError(
            f"gitleaks report is not a JSON array (got {type(report).__name__})"
        )
    findings: list[secret_scan.Finding] = []
    for item in report:
        if not isinstance(item, dict):
            raise GitleaksError("gitleaks report entry is not an object")
        # kind = RuleID (never the value); redacted = the phase-1 masking policy, so
        # quarantine metadata and logs look the same whichever engine found it (I5/I6).
        # ``secret_scan._redact`` is reused on purpose: one masking policy, no drift
        # (secret_scan is a frozen phase-1 contract, so the helper is stable).
        kind = str(item.get("RuleID") or "").strip() or GITLEAKS_BINARY
        matched = str(item.get("Secret") or item.get("Match") or "")
        findings.append(
            secret_scan.Finding(kind=kind, redacted=secret_scan._redact(matched))
        )
    return findings


def _stderr_note(stderr: bytes) -> str:
    """How much the binary wrote to stderr — deliberately NOT what it wrote.

    The note travels inside :class:`GitleaksError`, and the CLI can surface that
    message on the terminal, so quoting stderr would be a channel for the scanned
    content to escape (a binary that echoes its input, a future gitleaks that
    mentions the matched value in an error). Under ``--log-level error`` the text is
    about config/report paths anyway — worth little, unlike the risk (I5/I6). The
    operator can reproduce the failure with the argv pinned above.
    """
    return "no stderr" if not stderr else f"{len(stderr)} bytes of stderr, not quoted"


def _gitleaks_version(binary: str | None) -> str | None:
    """``x.y.z`` reported by the binary, or ``None`` if it cannot be queried.

    Unqueryable is NOT an error here (frozen docstring): doctor reports the absence,
    and a version probe must never be able to break a scan.
    """
    if binary is None:
        return None
    try:
        proc = _run_gitleaks(binary, ["version"], stdin=b"",
                             timeout_s=GITLEAKS_VERSION_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.decode("utf-8", errors="replace")
    first_line = next((line.strip() for line in out.splitlines() if line.strip()), "")
    match = _VERSION_RE.match(first_line)
    return match.group(1) if match else None


def _merge(*groups: list[secret_scan.Finding]) -> list[secret_scan.Finding]:
    """Order-preserving union; identical ``(kind, redacted)`` pairs collapse into one."""
    seen: set[tuple[str, str]] = set()
    merged: list[secret_scan.Finding] = []
    for group in groups:
        for finding in group:
            key = (finding.kind, finding.redacted)
            if key not in seen:
                seen.add(key)
                merged.append(finding)
    return merged
