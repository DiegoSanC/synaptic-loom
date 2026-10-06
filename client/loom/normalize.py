"""Canonical project identity derived from the git remote (DESIGN.md §3).

`normalize(url)` reduces any remote URL form to a single key that is stable
across machines and devs: lowercase ``host/org/repo``, with no protocol, no
credentials, no port, no ``.git`` suffix and no trailing slashes.

It is the project identity authority (D1). The server copies this very same
test table (PLAN.md §T2): client and server must agree character for
character.
"""
from __future__ import annotations


def normalize(url: str) -> str:
    """Returns the canonical ``host/org/repo`` form of the git remote.

    Accepts the usual forms: scp-like (``git@host:org/repo.git``),
    ``https://``/``http://``, ``ssh://`` (with a port) and ``git://``.

    Raises ``ValueError`` if the input is empty or is not shaped like a remote
    (host and path cannot be extracted).
    """
    if not isinstance(url, str):
        raise ValueError(f"remote is not a str: {type(url).__name__}")
    raw = url.strip()
    if not raw:
        raise ValueError("empty remote")

    if "://" in raw:
        # Form with a scheme: scheme://[user[:pass]@]host[:port]/path
        _scheme, _, rest = raw.partition("://")
        authority, _, path = rest.partition("/")
        host = authority.rpartition("@")[2]     # drop credentials
        host = host.split(":", 1)[0]            # drop the port
    else:
        # scp-like form: [user@]host:path  (no port in git practice)
        hostpath = raw.rpartition("@")[2]        # drop credentials
        host, sep, path = hostpath.partition(":")
        if not sep:
            raise ValueError(f"unrecognized remote: {url!r}")

    host = host.strip().lower()
    path = path.strip().strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    path = path.strip("/")

    if not host or not path:
        raise ValueError(f"remote with no usable host/path: {url!r}")

    return f"{host}/{path}".lower()
