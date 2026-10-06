"""Device-token lifecycle on the client (issue #20; owner decisions of 2026-09-22).

Three concerns, all small and all deliberately free of network code (the caller owns
the :class:`~loom.api_client.ApiClient` and the log):

- **When to renew.** A device token carries ``token_issued_at`` / ``token_expires_at``
  in ``config.json``. Renewal is due once less than :data:`RENEW_BELOW_FRACTION` of the
  issued lifetime remains (90 days → the last ~22). A legacy ``adduser`` token has no
  lifetime keys and is never due: it does not expire.
- **How to persist a successor.** :func:`persist_token` rewrites ``config.json``
  atomically (temp file + ``os.replace``, mode 0600) touching ONLY the three credential
  keys, then swaps the token on the live config and client so the same pass uses it.
- **The loud 401.** A rejected credential on an unattended path is recorded in
  ``<LOOM_HOME>/auth_rejected.json`` and surfaced by ``loom status``, ``loom doctor``
  and the ``Server error`` hint, so nobody has to run doctor by chance to learn that
  capture stopped. It is cleared when an entry lands or a renewal succeeds.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

from loom.config import Config

RENEW_BELOW_FRACTION = 0.25
_ISO = "%Y-%m-%dT%H:%M:%SZ"


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def lifetime(cfg: Config) -> tuple[datetime, datetime] | None:
    """``(issued_at, expires_at)`` of a device token; ``None`` for a legacy token."""
    issued, expires = _parse(cfg.token_issued_at), _parse(cfg.token_expires_at)
    if issued is None or expires is None or expires <= issued:
        return None
    return issued, expires


def renewal_due(cfg: Config, now: datetime | None = None) -> bool:
    """True once less than a quarter of the token's life remains (or it is already gone)."""
    window = lifetime(cfg)
    if window is None:
        return False
    issued, expires = window
    remaining = expires - (now or _now())
    return remaining < (expires - issued) * RENEW_BELOW_FRACTION


def persist_token(cfg: Config, api, body: dict) -> None:
    """Write the successor from an enrol/renew response into ``config.json`` and use it.

    Raises ``KeyError``/``TypeError`` on a malformed body and ``OSError`` on a disk
    failure; in both cases nothing was replaced and the old token stays in force.
    """
    token, issued_at, expires_at = body["token"], body["issued_at"], body["expires_at"]
    if not all(isinstance(v, str) and v for v in (token, issued_at, expires_at)):
        raise TypeError("enrol/renew body: token, issued_at and expires_at must be non-empty strings")
    data = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("config.json is not a JSON object")
    data["token"], data["token_issued_at"], data["token_expires_at"] = token, issued_at, expires_at
    _write_json_600(cfg.config_path, data)
    cfg.token, cfg.token_issued_at, cfg.token_expires_at = token, issued_at, expires_at
    api.token = token


def _write_json_600(path, data: dict) -> None:
    """Atomic, owner-only write: the file holds a credential and must never be half-written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# The loud 401                                                                 #
# --------------------------------------------------------------------------- #
def record_auth_rejected(cfg: Config, status: int, what: str) -> None:
    """Note that ``what`` (e.g. ``POST /api/entries``) was refused with ``status``."""
    try:
        _write_json_600(cfg.auth_rejected_path, {
            "status": status,
            "at": _now().strftime(_ISO),
            "detail": f"{what} → {status} {'forbidden' if status == 403 else 'unauthorized'}",
        })
    except OSError:
        pass  # diagnostics never fail a pass (same policy as the run log)


def clear_auth_rejected(cfg: Config) -> None:
    try:
        cfg.auth_rejected_path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def read_auth_rejected(cfg: Config) -> dict | None:
    try:
        data = json.loads(cfg.auth_rejected_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


REJECTED_HINT = ("The server refuses this machine's token (expired, revoked or deactivated). "
                 "Run 'loom enroll' with a fresh invitation code from the operator.")


# --------------------------------------------------------------------------- #
# doctor                                                                       #
# --------------------------------------------------------------------------- #
def describe_credential(cfg: Config, now: datetime | None = None) -> tuple[str, str]:
    """``(level, text)`` for the doctor row about the credential's lifetime."""
    if cfg.token_issued_at is None and cfg.token_expires_at is None:
        return ("warn", "token is a permanent adduser token (never expires, never rotates); "
                        "run 'loom enroll' to replace it with a renewable device token")
    window = lifetime(cfg)
    if window is None:
        return ("warn", "token lifetime keys in config.json are unreadable; "
                        "run 'loom enroll' to re-enrol this machine")
    issued, expires = window
    now = now or _now()
    if expires <= now:
        return ("fail", f"device token expired on {expires.strftime(_ISO)}; run 'loom enroll'")
    days_left = int((expires - now) / timedelta(days=1))
    renew_days = int((expires - issued) * RENEW_BELOW_FRACTION / timedelta(days=1))
    return ("ok", f"device token expires {expires.strftime(_ISO)} ({days_left} days left; "
                  f"renews automatically at ~{renew_days} days left)")
