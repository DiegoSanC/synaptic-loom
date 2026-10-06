"""Per-machine, per-project capture state — the second gate of D9.

``<LOOM_HOME>/projects_local.json`` maps a canonical remote (D1) to
``{"state": "enabled" | "disabled", "since": "<UTC ISO>"}``. For an enabled project
``since`` is also where capture starts (:func:`capture_since`). It is written by
``loom enable`` / ``loom disable`` (and by ``loom register`` for the person who registers,
owner decision B of 2026-09-22) and read by the processor in ``opt_in`` mode: a registered
project whose state here is not ``enabled`` is skipped with ``dropped_not_enabled``.

The file lives under LOOM_HOME and never inside a repository: it cannot be committed by
accident, and it survives a re-clone. Absent, unreadable or malformed → every project is
undecided, which in ``opt_in`` mode means nothing is captured (the recoverable error, D7).
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

from loom.config import Config

ENABLED = "enabled"
DISABLED = "disabled"
STATES = (ENABLED, DISABLED)


def load(cfg: Config) -> dict[str, dict]:
    """The whole map; ``{}`` when the file is absent or not a JSON object."""
    try:
        data = json.loads(cfg.projects_local_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)}


def state_of(cfg: Config, canonical: str) -> str | None:
    """``"enabled"``, ``"disabled"``, or ``None`` when this machine never decided."""
    entry = load(cfg).get(canonical)
    if not entry:
        return None
    state = entry.get("state")
    return state if state in STATES else None


def capture_since(cfg: Config, canonical: str) -> datetime | None:
    """When capture of an ENABLED project started on this machine: its ``since`` stamp.

    The processor drops the turns older than this (D9 amendment of 2026-09-30), so
    enabling a repo captures what happens from then on, never its history. ``None`` —
    no cutoff, the behaviour before the amendment — when the project is not enabled or
    the stamp is unusable.
    """
    entry = load(cfg).get(canonical) or {}
    if entry.get("state") != ENABLED or not isinstance(entry.get("since"), str):
        return None
    try:
        since = datetime.fromisoformat(entry["since"])
    except ValueError:
        return None
    return since.astimezone(timezone.utc) if since.tzinfo is not None else None

def set_state(cfg: Config, canonical: str, state: str) -> bool:
    """Record ``state`` for ``canonical``. Returns ``False`` when it already was that
    (idempotent: the ``since`` stamp is kept)."""
    if state not in STATES:
        raise ValueError(f"unknown local state: {state!r}")
    data = load(cfg)
    current = data.get(canonical, {})
    if current.get("state") == state:
        return False
    data[canonical] = {
        "state": state,
        "since": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _atomic_write(cfg, data)
    return True


def enabled_count(cfg: Config) -> int:
    return sum(1 for v in load(cfg).values() if v.get("state") == ENABLED)


def _atomic_write(cfg: Config, data: dict) -> None:
    path = cfg.projects_local_path
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
