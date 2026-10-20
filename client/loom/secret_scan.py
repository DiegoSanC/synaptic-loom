"""Deterministic secret scanning — the phase-1 regex set (DESIGN.md §1, P3).

Home-grown regex set, no external dependencies. Since phase 2 it is never called on its
own by production code: :mod:`loom.scan_engine` runs it UNIONED with gitleaks, which D16
(2026-10-15) made mandatory — the set alone misses whole families (vendor keys,
``X_PASSWORD=``, bearer headers), so a config that asks for it alone pauses capture. The
union runs over the transcript extract before the summarizer (step 5b, redaction) and
over the summary before any POST (step 8); a hit there (or a failure of the scan itself,
treated as a hit by the caller — fail-closed, I4) diverts the entry to ``quarantine/`` and
nothing leaves the machine. ``scan_engine`` also reads ``_PATTERNS`` to compute the
redaction spans, so this table is the single definition of the families.

Families covered (each with a positive and a negative test):
- ``aws_access_key``  — ``AKIA`` + 16 uppercase alphanumerics.
- ``openai_key``      — ``sk-`` + ≥16 of ``[A-Za-z0-9_-]``.
- ``github_pat``      — ``ghp_`` + 36 alphanumerics.
- ``jwt``             — three base64url segments, the first two starting with ``eyJ``.
- ``private_key``     — PEM header ``-----BEGIN … PRIVATE KEY-----``.
- ``password_assignment`` — ``password=``/``passwd=`` (or ``:``) with a non-empty value.
- ``url_credentials`` — ``scheme://user:pass@host`` (postgres/mysql/mongodb/redis/amqp/http[s]).
- ``email``           — email address (PII, rule 5 of the prompt).

False positives explicitly avoided: 40-hex git SHAs and UUIDs — no family captures
them (see tests).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Each pattern is (family_name, compiled regex). The order only affects the order of the
# findings, not the decision (one is enough for quarantine).
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("github_pat", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    # JWT: header.payload.signature; base64url header and payload start with "eyJ".
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # password= / passwd= (or ':') followed by a non-empty value.
    ("password_assignment", re.compile(r"(?i)\bpass(?:word|wd)\b\s*[=:]\s*(\S+)")),
    # scheme://user:pass@host — credentials embedded in a connection string / URL.
    (
        "url_credentials",
        re.compile(
            r"\b(?:postgres(?:ql)?|mysql|mongodb|redis|amqp|https?)://"
            r"[^\s:/@]+:[^\s:/@]+@[^\s/]+"
        ),
    ),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
]


def _redact(matched: str) -> str:
    """Non-reversible preview of the finding (the whole secret is never logged).

    Keeps up to the first 4 characters and masks the rest; useful for reporting the
    family in ``loom status`` / quarantine metadata without leaking the value.
    """
    keep = matched[:4]
    return keep + "*" * max(0, len(matched) - len(keep))


@dataclass(frozen=True)
class Finding:
    """A potential secret that was detected.

    - ``kind``: family name (e.g. ``"openai_key"``).
    - ``redacted``: masked preview (safe for logs/metadata).
    """

    kind: str
    redacted: str


def scan(text: str) -> list[Finding]:
    """Returns the secret findings in ``text`` (empty list if it is clean).

    Deterministic and stateless. The caller treats **any exception** from this function
    as equivalent to a finding (fail-closed, I4): that policy lives in the processor,
    not here.
    """
    if not text:
        return []
    findings: list[Finding] = []
    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            findings.append(Finding(kind=kind, redacted=_redact(match.group(0))))
    return findings
