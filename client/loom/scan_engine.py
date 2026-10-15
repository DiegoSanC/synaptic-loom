"""Scanner engine dispatch (PHASE-2-3-SPEC R2.3, D16) — FROZEN INTERFACE, additive since.

Facade in front of the secret scanners. Phase 1 shipped a single engine (the curated
regex set in :mod:`loom.secret_scan`); phase 2 added gitleaks. Since **D16**
(2026-10-15) gitleaks is MANDATORY for capture: every production scan is gitleaks ∪
the regex set, and there is no silent fallback to the regex set anywhere. Processor
step 5b (pre-summary redaction) and step 8, ``loom push-quarantine`` and ``loom
curate`` all call THIS module, never an engine directly, so engine selection is
decided in exactly one place (AC-2.3b).

Contract (the phase-2 shape is frozen; D16 changed the engine values and added
``ensure_ready``/``redact``/``resolve_gitleaks``):

- ``scan(text, cfg=None) -> list[secret_scan.Finding]`` — findings from the ACTIVE
  engine. ``cfg is None`` → the regex set alone: a LIBRARY path kept for phase-1
  callers; no production call site passes ``None``. Engine ``"gitleaks"`` (the
  default) or its legacy alias ``"auto"`` → gitleaks ∪ regex; no resolvable binary
  RAISES :class:`ScannerUnavailable` (also a ``FileNotFoundError``). Engine
  ``"regex"`` is no longer accepted for capture and RAISES :class:`ScannerUnavailable`
  without ever executing the binary; an unknown value raises it too (also a
  ``ValueError``). The fail-closed callers (I4) turn any raise into a hit.
- ``active_engine(cfg=None) -> EngineInfo`` — which engine ``scan`` would use, with
  the live gitleaks version and the pinned one (doctor + contract check, AC-2.3d).
  Raises exactly when ``scan`` would.
- ``ensure_ready(cfg) -> EngineInfo`` — the capture gate of D16: raises
  :class:`ScannerUnavailable` unless gitleaks resolves AND ``gitleaks version`` runs.
  A version that differs from the pin is NOT a failure (doctor warns about it).
- ``redact(text, cfg) -> (redacted_text, findings)`` — step 5b: every occurrence of
  every value the active engine detects is replaced by ``[REDACTED:<kind>]``, longest
  value first. The matched VALUES exist only in local variables inside this module:
  they are never logged, printed, written to disk or put in an exception message, and
  the findings handed back carry the same masked preview as ``scan``'s.
- gitleaks resolution: ``$LOOM_HOME/bin/gitleaks`` (what install.sh installs, checksum
  verified) first, then ``PATH``. The first lookup does not depend on the scheduler's
  PATH, which is what made cron/systemd passes degrade before D16 (P2.F5).
- Exceptions propagate to the caller: the fail-closed policy (scanner error == hit)
  lives in the processor/CLI/curate, exactly as with phase-1 ``secret_scan.scan`` (I4).
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
# The version install.sh downloads (checksum-pinned there) and init/enroll write as
# ``scanner.gitleaks_version`` (D16). install.sh carries the same literal; a unit test
# keeps the two equal.
GITLEAKS_PINNED_VERSION = "8.30.1"
# Engine values (D16). "auto" survives only as a legacy alias of "gitleaks".
ENGINE_GITLEAKS = "gitleaks"
_GITLEAKS_ALIASES = frozenset({ENGINE_GITLEAKS, "auto"})
ENGINE_REGEX = "regex"

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


class ScannerUnavailable(RuntimeError):
    """No scan can run at all under this config (D16): capture pauses, and every other
    caller refuses (fail-closed, I4). The message names the reason and the remedy and
    never any scanned content."""


class GitleaksNotFound(ScannerUnavailable, FileNotFoundError):
    """No gitleaks binary at ``$LOOM_HOME/bin`` nor on ``PATH``."""


class UnknownEngine(ScannerUnavailable, ValueError):
    """``scanner.engine`` holds a value this client does not know."""


_INSTALL_HINT = (
    f"install gitleaks {GITLEAKS_PINNED_VERSION} with 'bash client/install.sh --yes "
    "--update' (it lands in $LOOM_HOME/bin/gitleaks)"
)


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
    """Findings from the active engine (see the contract above).

    With gitleaks (every production config, D16) the result is the UNION of both
    engines (defense in depth): gitleaks brings entropy/vendor rules the regex set has
    no chance against, the regex set brings families gitleaks does not ship — e.g. the
    ``email`` PII rule of phase 1. Exact duplicates are collapsed.
    """
    name, binary = _resolve(cfg)
    if name == ENGINE_REGEX:
        return secret_scan.scan(text)
    # Both engines run; either one raising propagates (fail-closed, I4).
    return _merge(_findings(_scan_with_gitleaks(text, binary)), secret_scan.scan(text))


def active_engine(cfg: Config | None = None) -> EngineInfo:
    """Engine that :func:`scan` would use for this ``cfg`` (doctor, AC-2.4c)."""
    name, binary = _resolve(cfg)
    return EngineInfo(
        name=name,
        version=_gitleaks_version(binary) if name == ENGINE_GITLEAKS else None,
        pinned_version=cfg.scanner_gitleaks_version if cfg else None,
    )


def ensure_ready(cfg: Config) -> EngineInfo:
    """The D16 capture gate: gitleaks resolves AND ``gitleaks version`` runs, or raise.

    Called once at the start of every ``loom process`` pass (and by doctor/status), so
    a missing or broken binary pauses capture instead of being discovered marker by
    marker. A version that runs but differs from the pin — or prints a line we cannot
    parse — is NOT a failure here: doctor reports it as a warning.
    """
    name, binary = _resolve(cfg)
    if name != ENGINE_GITLEAKS or binary is None:  # pragma: no cover — _resolve raises
        raise ScannerUnavailable("gitleaks is not the active engine")
    proc = _version_proc(binary)
    if proc is None or proc.returncode != 0:
        raise ScannerUnavailable(
            f"'{binary} version' could not be run, so gitleaks cannot scan; "
            + _INSTALL_HINT
        )
    return EngineInfo(name=ENGINE_GITLEAKS, version=_parse_version(proc.stdout),
                      pinned_version=cfg.scanner_gitleaks_version)


def redact(text: str, cfg: Config | None) -> tuple[str, list[secret_scan.Finding]]:
    """``text`` with every detected secret value replaced by ``[REDACTED:<kind>]``.

    Processor step 5b (D16): the extract is redacted BEFORE the summarizer reads it.
    Values are replaced longest first (a value containing another is replaced whole,
    never as a shorter label plus a surviving tail) and EVERYWHERE they occur, not
    only where a rule matched — a key quoted once in an assignment and once bare must
    not survive in the bare spot. The caller re-scans the result (the fixpoint check);
    this function does not promise the result is clean, only that every value found
    in THIS scan is gone.

    The values never leave this function: the findings carry the masked preview of
    :func:`secret_scan._redact`, the same policy as :func:`scan`.
    """
    name, binary = _resolve(cfg)
    matches: list[tuple[str, str, str]] = []  # (kind, value, preview source)
    if name == ENGINE_GITLEAKS:
        matches.extend(_scan_with_gitleaks(text, binary))
    matches.extend(_regex_matches(text))
    findings = _merge(_findings(matches))
    # One label per value: the first engine to report it names it (gitleaks first).
    labels: dict[str, str] = {}
    for kind, value, _preview in matches:
        for candidate in _replacement_values(kind, value):
            labels.setdefault(candidate, kind)
    redacted = text
    for value in sorted(labels, key=len, reverse=True):
        redacted = redacted.replace(value, f"[REDACTED:{labels[value]}]")
    return redacted, findings


# --------------------------------------------------------------------------------- #
# Engine resolution                                                                   #
# --------------------------------------------------------------------------------- #
def _resolve(cfg: Config | None) -> tuple[str, str | None]:
    """``(engine_name, binary_path)`` for this config — the single decision point.

    ``scan``, ``redact``, ``active_engine`` and ``ensure_ready`` share it, so what
    doctor reports is exactly what the scan does (AC-2.3b/AC-2.3d). The binary is
    looked up here, at call time.
    """
    if cfg is None:
        return ENGINE_REGEX, None
    engine = (cfg.scanner_engine or "").strip().lower()
    if engine == ENGINE_REGEX:
        # Never even look for the binary. Refused since D16: the regex set alone misses
        # whole families (vendor keys, `X_PASSWORD=`, bearer headers) that gitleaks
        # catches, and a capture scanned only by it would be published permanently.
        raise ScannerUnavailable(
            "scanner.engine is 'regex', which is no longer accepted for capture (D16: "
            "gitleaks is mandatory); set scanner.engine to 'gitleaks' and "
            + _INSTALL_HINT
        )
    if engine in _GITLEAKS_ALIASES:
        binary = resolve_gitleaks(cfg)
        if binary is None:
            # D16: no fallback, whatever the spelling ("auto" included). The fail-closed
            # callers pause capture or refuse instead of scanning with a weaker engine.
            raise GitleaksNotFound(
                f"no '{GITLEAKS_BINARY}' binary at {cfg.loom_home / 'bin'} nor on PATH; "
                + _INSTALL_HINT
            )
        return ENGINE_GITLEAKS, binary
    raise UnknownEngine(
        f"unknown scanner.engine {cfg.scanner_engine!r} (expected 'gitleaks')"
    )


def resolve_gitleaks(cfg: Config) -> str | None:
    """``$LOOM_HOME/bin/gitleaks`` when it is an executable file, else ``PATH``, else None.

    install.sh puts a checksum-verified 8.30.1 at the first location (D16). Looking
    there FIRST, and by absolute path, makes the unattended pass independent of the
    scheduler's PATH; PATH remains the fallback for hosts that installed gitleaks
    themselves.
    """
    candidate = cfg.loom_home / "bin" / GITLEAKS_BINARY
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return shutil.which(GITLEAKS_BINARY)


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


def _scan_with_gitleaks(text: str, binary: str | None) -> list[tuple[str, str, str]]:
    """``(kind, value, value)`` per leak the real binary reports; ANY doubt is an error.

    The value is what :func:`redact` replaces and what the masked preview is cut from;
    it never leaves this module (see :func:`_findings`).
    """
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


def _parse_report(stdout: bytes) -> list[tuple[str, str, str]]:
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
    matches: list[tuple[str, str, str]] = []
    for item in report:
        if not isinstance(item, dict):
            raise GitleaksError("gitleaks report entry is not an object")
        # kind = RuleID (never the value). ``Secret`` is the credential itself (the
        # capture group of the rule); ``Match`` the whole rule match, used only when a
        # rule reports no separate secret.
        kind = str(item.get("RuleID") or "").strip() or GITLEAKS_BINARY
        matched = str(item.get("Secret") or item.get("Match") or "")
        matches.append((kind, matched, matched))
    return matches


def _findings(matches: list[tuple[str, str, str]]) -> list[secret_scan.Finding]:
    """The OUTWARD form of a match: kind + masked preview, never the value.

    redacted = the phase-1 masking policy, so quarantine metadata and logs look the
    same whichever engine found it (I5/I6). ``secret_scan._redact`` is reused on
    purpose: one masking policy, no drift (secret_scan is a frozen phase-1 contract, so
    the helper is stable).
    """
    return [secret_scan.Finding(kind=kind, redacted=secret_scan._redact(preview))
            for kind, _value, preview in matches]


# --------------------------------------------------------------------------------- #
# Redaction spans of the regex families (D16, step 5b)                               #
# --------------------------------------------------------------------------------- #
# The phase-1 ``private_key`` family matches the PEM HEADER only — enough to flag a
# block, not to remove it. For redaction the span runs to the matching END line, or,
# with no END in sight (a truncated paste), over the base64 run that follows.
_PEM_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"(?:[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----|[A-Za-z0-9+/=\s]*)"
)
# A bare password shorter than this is replaced only inside its own assignment, not
# everywhere: `password: is` in prose would otherwise rewrite every "is" in the extract
# into a label, and a two-character "value" is not a credential worth that damage.
_MIN_BARE_PASSWORD = 4
_VALUE_QUOTES = "'\"`,;.()[]{}<>"


def _regex_matches(text: str) -> list[tuple[str, str, str]]:
    """``(kind, value, preview source)`` for every hit of the phase-1 regex set.

    Same patterns, same order as :func:`secret_scan.scan` (its ``_PATTERNS`` table is
    read, never copied, so the two cannot drift); only the SPAN differs where the
    family's match is not the whole secret.
    """
    if not text:
        return []
    out: list[tuple[str, str, str]] = []
    for kind, pattern in secret_scan._PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(0)
            if kind == "private_key":
                block = _PEM_BLOCK_RE.match(text, match.start())
                value = block.group(0).rstrip() if block else value
            out.append((kind, value, match.group(0)))
    return out


def _replacement_values(kind: str, value: str) -> list[str]:
    """Every string :func:`redact` removes for one detected value.

    The value itself, always. For ``password_assignment`` the detected value is the
    whole ``password=…`` assignment, so the bare password (with and without quotes) is
    added: replacing only the assignment would leave the password wherever else it is
    repeated, and replacing only the password would leave ``password=[REDACTED…]`` —
    which the same rule matches again, so the fixpoint check would never pass.
    """
    values = [value] if value else []
    if kind == "password_assignment":
        found = re.match(r"(?is)pass(?:word|wd)\b\s*[=:]\s*(\S+)", value)
        if found:
            bare = found.group(1)
            for candidate in (bare, bare.strip(_VALUE_QUOTES)):
                if len(candidate) >= _MIN_BARE_PASSWORD:
                    values.append(candidate)
    return values


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


def _version_proc(binary: str) -> subprocess.CompletedProcess[bytes] | None:
    """``gitleaks version`` run the pinned way, or ``None`` if it could not start."""
    try:
        return _run_gitleaks(binary, ["version"], stdin=b"",
                             timeout_s=GITLEAKS_VERSION_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None


def _parse_version(stdout: bytes) -> str | None:
    out = stdout.decode("utf-8", errors="replace")
    first_line = next((line.strip() for line in out.splitlines() if line.strip()), "")
    match = _VERSION_RE.match(first_line)
    return match.group(1) if match else None


def _gitleaks_version(binary: str | None) -> str | None:
    """``x.y.z`` reported by the binary, or ``None`` if it cannot be queried.

    Unqueryable is NOT an error here (frozen docstring): doctor reports the absence,
    and a version probe must never be able to break a scan.
    """
    if binary is None:
        return None
    proc = _version_proc(binary)
    if proc is None or proc.returncode != 0:
        return None
    return _parse_version(proc.stdout)


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
