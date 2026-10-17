"""Wave s's HTTP client. Runs INSIDE the server container (`docker compose exec`).

It talks to the server's own port, 127.0.0.1:8080: what wave s grades is the data behind
the API after a backup and a restore, not the edge in front of it (wave r grades Caddy).
Standard library only — it runs on the server image's interpreter.

argv[1]: a JSON list of steps ``{"path", "method"?, "token"?, "body"?}``.
Prints one JSON list, one ``{"status", "body"}`` per step; ``body`` is the parsed JSON, or
the text when the response is not JSON.
"""
import json
import sys
import urllib.error
import urllib.request

results = []
for step in json.loads(sys.argv[1]):
    data = json.dumps(step["body"]).encode() if "body" in step else None
    request = urllib.request.Request("http://127.0.0.1:8080" + step["path"], data=data,
                                     method=step.get("method", "GET"))
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if step.get("token"):
        request.add_header("Authorization", f"Bearer {step['token']}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        body = json.loads(raw)
    except ValueError:
        body = raw.decode(errors="replace")
    results.append({"status": status, "body": body})
print(json.dumps(results))
