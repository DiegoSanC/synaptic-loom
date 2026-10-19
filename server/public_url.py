"""The URL people reach this server at (D19): what an invitation link starts with.

`LOOM_PUBLIC_URL` names it. Unset on Fly, it is the app's own hostname,
``https://$FLY_APP_NAME.fly.dev``; the Compose stack sets it from ``LOOM_DOMAIN``. Off both
it is unknown, and nothing that needs it guesses one.
"""
from __future__ import annotations

import os

ENV_VAR = "LOOM_PUBLIC_URL"


def resolve(override: str | None = None) -> str | None:
    """``override`` (``invite --public-url``), else the setting, else Fly's hostname."""
    url = override or os.environ.get(ENV_VAR)
    if not url and os.environ.get("FLY_APP_NAME"):
        url = f"https://{os.environ['FLY_APP_NAME']}.fly.dev"
    return url.rstrip("/") if url else None


def join_link(base: str, code: str) -> str:
    """The invitation link. The code rides in the fragment, which no browser sends."""
    return f"{base}/ui/join#code={code}"
