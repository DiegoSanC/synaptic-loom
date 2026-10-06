"""Git remote normalization to the project's canonical identity (DESIGN.md §3).

Server-side copy of the same function the client uses (PLAN.md §T2/§T3): the client
sends the raw or normalized URL and the server re-normalizes — it is the authority.
Both satisfy the SAME table of tests, which guarantees consistency.

    git@github.com:Org/Repo.git             -> github.com/org/repo
    https://github.com/Org/Repo.git         -> github.com/org/repo
    https://user:pass@gitlab.com/a/b/       -> gitlab.com/a/b
    ssh://git@bitbucket.org:7999/x/y.git     -> bitbucket.org/x/y   (port dropped)
    git@github.com:Org/Repo                 -> github.com/org/repo  (no .git)

Stdlib only.
"""
import re

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


def normalize(url: str) -> str:
    """Return the canonical "host/path" identity (lowercase, no credentials,
    no protocol, no port, no .git suffix and no trailing slashes)."""
    s = url.strip()
    if not s:
        raise ValueError("empty remote url")

    if _SCHEME_RE.match(s):
        # <scheme>://[user[:pass]@]host[:port]/path
        rest = s.split("://", 1)[1]
    elif ":" in s and "/" not in s.split(":", 1)[0]:
        # scp-like without a scheme: [user@]host:path -> the first ':' splits host/path
        host_part, path_part = s.split(":", 1)
        rest = host_part + "/" + path_part
    else:
        rest = s

    # rest = [user[:pass]@]host[:port]/path...
    if "/" in rest:
        authority, path = rest.split("/", 1)
    else:
        authority, path = rest, ""

    # strip credentials (rsplit in case the password contains '@')
    if "@" in authority:
        authority = authority.rsplit("@", 1)[1]
    # strip the port
    host = authority.split(":", 1)[0]

    # clean up the path: trailing slashes and .git suffix
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]

    result = host + "/" + path if path else host
    return result.lower()
