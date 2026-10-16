"""Wave r's HTTP client. Runs INSIDE a client container on the stack's `edge` network.

Standard library only: it runs on the server image's own interpreter, so the wave pulls
nothing a host might not have. A container per client is what gives each client its own
source address; the address itself is the wave's literal (`docker run --ip`).

argv[1]: a JSON list of steps ``{"url", "headers"?, "timeout"?}``.
env ``LOOM_CA``: the PEM of Caddy's root, so HTTPS is VERIFIED against the hostname —
an unverified context would pass against any certificate at all.

Prints one JSON list, one element per step: ``{"status", "headers", "body"}``, or
``{"error"}`` when no HTTP response came back. Redirects are reported, never followed.
"""
import json
import os
import ssl
import sys
import urllib.error
import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


context = ssl.create_default_context(cadata=os.environ["LOOM_CA"])
opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=context))
results = []
for step in json.loads(sys.argv[1]):
    request = urllib.request.Request(step["url"], headers=step.get("headers", {}))
    try:
        with opener.open(request, timeout=step.get("timeout", 10)) as response:
            results.append({"status": response.status, "headers": dict(response.headers),
                            "body": response.read().decode()})
    except urllib.error.HTTPError as exc:
        results.append({"status": exc.code, "headers": dict(exc.headers),
                        "body": exc.read().decode()})
    except Exception as exc:  # noqa: BLE001 — every failure mode is an observation here
        results.append({"error": f"{type(exc).__name__}: {exc}"})
print(json.dumps(results))
