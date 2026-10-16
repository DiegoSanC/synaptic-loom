"""Where the client address comes from (D17): one setting, read once at startup.

The address keys both in-process limiters and the security-audit line, so it decides who
shares a bucket with whom. `LOOM_CLIENT_IP_SOURCE` names its source:

* ``peer`` (default) — the TCP peer, ``request.client.host``. Behind a reverse proxy that
  is the proxy, unless uvicorn's ProxyHeadersMiddleware rewrote it from
  ``X-Forwarded-For``, which it does only for a peer listed in ``FORWARDED_ALLOW_IPS``.
* ``fly-client-ip`` — the header the Fly proxy sets. Off Fly it is client-controlled.
* ``header:<name>`` — one single-valued header a trusted proxy sets
  (``header:cf-connecting-ip``). The multi-valued forwarding headers are refused: a
  comma-separated chain is never one valid address, and choosing a hop out of it is
  uvicorn's job under ``peer``.

Anything else refuses to start. Whatever the source, the value is validated with
``ipaddress``; a missing or invalid one, or one carrying an IPv6 scope id, is
``unavailable``, never the raw value. An IPv4-mapped IPv6 address keys as its IPv4 form.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from starlette.requests import Request

ENV_VAR = "LOOM_CLIENT_IP_SOURCE"
DEFAULT = "peer"
FLY_MODE = "fly-client-ip"
UNAVAILABLE = "unavailable"
# RFC 9110 field-name token.
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_MULTI_VALUED = frozenset({"x-forwarded-for", "forwarded"})


@dataclass(frozen=True)
class ClientIpSource:
    """The resolved setting: ``mode`` as it is logged, and the header it reads, if any."""

    mode: str
    header: str | None

    def client_ip(self, request: Request) -> str:
        if self.header is None:
            value = request.client.host if request.client else ""
        else:
            value = request.headers.get(self.header, "")
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return UNAVAILABLE
        if getattr(address, "scope_id", None) is not None:
            # `ipaddress` takes any text after `%` as a scope id, spaces included: never
            # let that become a bucket key or a word in the audit line.
            return UNAVAILABLE
        # A dual-stack socket reports an IPv4 client as ::ffff:a.b.c.d — one client, one key.
        return str(getattr(address, "ipv4_mapped", None) or address)


def parse(value: str | None) -> ClientIpSource:
    """The source a setting names; ``ValueError`` for anything that is not one."""
    if value is None:
        value = DEFAULT
    if value == "peer":
        return ClientIpSource("peer", None)
    if value == FLY_MODE:
        return ClientIpSource(FLY_MODE, FLY_MODE)
    if value.startswith("header:"):
        name = value[len("header:"):].lower()
        if name == FLY_MODE:
            return ClientIpSource(FLY_MODE, FLY_MODE)   # same header, same warnings
        if _HEADER_NAME.fullmatch(name) and name not in _MULTI_VALUED:
            return ClientIpSource(f"header:{name}", name)
    raise ValueError(
        f"{ENV_VAR}={value!r} is not a client-IP source. Expected 'peer' (the default: the "
        f"TCP peer, with uvicorn's FORWARDED_ALLOW_IPS behind a proxy), 'fly-client-ip' "
        f"(on Fly.io) or 'header:<name>' (one single-valued header set by a trusted proxy, "
        f"e.g. header:cf-connecting-ip; not x-forwarded-for or forwarded)."
    )


def startup_warnings(source: ClientIpSource, fly_app_name: str | None) -> list[str]:
    """What looks inconsistent with where the process is running."""
    if fly_app_name and source.mode != FLY_MODE:
        return [
            f"FLY_APP_NAME is set (running on Fly.io) but {ENV_VAR} is {source.mode!r}, "
            f"not 'fly-client-ip': behind the Fly proxy every client may share one "
            f"rate-limit bucket and one audit address."
        ]
    if source.mode == FLY_MODE and not fly_app_name:
        return [
            f"{ENV_VAR} is 'fly-client-ip' but FLY_APP_NAME is not set: off Fly.io the "
            f"Fly-Client-IP header is client-controlled unless a proxy in front strips it, "
            f"so the rate limit can be bypassed and the audit address forged."
        ]
    return []
