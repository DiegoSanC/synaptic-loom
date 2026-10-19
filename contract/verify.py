#!/usr/bin/env python3
"""verify.py — Synaptic Loom contract test (PLAN.md §4, assertions 1–5).

Standalone, stdlib only. Re-runs the checks against the artifacts in
`contract/` and the live `claude` CLI. Exits with code != 0 if anything fails.

Flags corrected with respect to the PLAN per the T0 findings (see FLAGS.md):
  - `--bare`  -> `--safe-mode`  (D-1: --bare breaks OAuth auth on this machine)
  - `--allowedTools ""` -> `--tools ""`  (D-2: an empty allowlist does NOT block tools)

Usage:
  python3 verify.py                # also runs the live CLI checks
  LOOM_SKIP_LIVE=1 python3 verify.py   # static assertions only (offline)
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERSION_FILE = HERE / "VERSION"
FIXTURE = HERE / "fixtures" / "real_session.jsonl"
PAYLOAD = HERE / "sessionend_payload.json"
CLI_TIMEOUT = int(os.environ.get("LOOM_CLI_TIMEOUT", "180"))
SKIP_LIVE = os.environ.get("LOOM_SKIP_LIVE") == "1"

failures: list[str] = []
warnings: list[str] = []


def ok(msg: str) -> None:
    print(f"  [PASS] {msg}")


def fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")
    failures.append(msg)


def warn(msg: str) -> None:
    print(f"  [WARN] {msg}")
    warnings.append(msg)


# --- Reference extractor (SCHEMA.md contract; minimal mirror of T3) --------------

CONVERSATIONAL = {"user", "assistant"}


def read_delta(path: Path, last_line: int) -> dict:
    """Read lines with index > last_line. Returns
    {messages, new_last_line, new_last_uuid, skipped}.
    Raises ValueError (equivalent to AdapterError) if:
      - some line does not parse as JSON,
      - a user/assistant line has no uuid or message.
    Non-conversational line types are skipped silently (NOT an error). D-4.
    """
    messages: list[str] = []
    new_last_uuid = ""
    skipped = 0
    total = 0
    with open(path, encoding="utf-8") as fh:
        for idx, raw in enumerate(fh):
            total = idx + 1
            if idx < last_line:
                continue
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {idx} does not parse as JSON: {exc}")
            typ = obj.get("type")
            if typ not in CONVERSATIONAL:
                skipped += 1
                continue
            if "uuid" not in obj or "message" not in obj:
                raise ValueError(f"line {idx} ({typ}) has no uuid/message")
            msg = obj["message"]
            role = msg.get("role")
            content = msg.get("content")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    bt = block.get("type")
                    if bt == "text":
                        parts.append(block.get("text", ""))
                    elif bt == "tool_use":
                        parts.append(f"[tool:{block.get('name')}]: "
                                     f"{json.dumps(block.get('input', {}))[:500]}")
                    elif bt == "tool_result":
                        c = block.get("content")
                        c = c if isinstance(c, str) else json.dumps(c)
                        parts.append(f"[tool_result]: {c[:500]}")
                text = "\n".join(p for p in parts if p)
            messages.append(f"[{role}]: {text}")
            new_last_uuid = obj["uuid"]
    return {
        "messages": messages,
        "new_last_line": total,
        "new_last_uuid": new_last_uuid,
        "skipped": skipped,
    }


def run_cli(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["claude", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT,
    )


# --- Assertion 1: version -------------------------------------------------------

def assert_version() -> None:
    print("Assertion 1: claude --version vs contract/VERSION")
    if SKIP_LIVE:
        warn("LOOM_SKIP_LIVE=1 → skipping the CLI run")
        return
    try:
        cp = run_cli(["--version"])
    except Exception as exc:  # noqa: BLE001
        fail(f"could not run `claude --version`: {exc}")
        return
    live = cp.stdout.strip()
    if cp.returncode != 0 or not live:
        fail(f"`claude --version` exit={cp.returncode} stdout={live!r}")
        return
    ok(f"claude --version = {live!r}")
    if VERSION_FILE.exists():
        pinned = VERSION_FILE.read_text(encoding="utf-8").strip()
        if pinned != live:
            warn(f"version differs from the contract: pinned={pinned!r} live={live!r} "
                 "(WARN by design, not a failure)")
        else:
            ok("matches the pinned version")
    else:
        warn("contract/VERSION does not exist")


# --- Assertion 2: synthetic headless session -----------------------------------

def assert_headless_ok() -> None:
    print("Assertion 2: headless says LOOM_CONTRACT_OK (--safe-mode --tools '')")
    if SKIP_LIVE:
        warn("LOOM_SKIP_LIVE=1 → skipping")
        return
    try:
        cp = run_cli(["-p", "--safe-mode", "--tools", "", "--output-format", "text",
                      "Say exactly: LOOM_CONTRACT_OK"], stdin="")
    except Exception as exc:  # noqa: BLE001
        fail(f"error running headless: {exc}")
        return
    if cp.returncode != 0:
        fail(f"exit={cp.returncode} stderr={cp.stderr.strip()[:200]!r}")
        return
    if "LOOM_CONTRACT_OK" in cp.stdout:
        ok("stdout contains LOOM_CONTRACT_OK")
    else:
        fail(f"stdout does not contain the marker: {cp.stdout.strip()[:200]!r}")


# --- Assertion 3: stdin ---------------------------------------------------------

def assert_stdin() -> None:
    print("Assertion 3: stdin reaches the model (token XYZ)")
    if SKIP_LIVE:
        warn("LOOM_SKIP_LIVE=1 → skipping")
        return
    try:
        cp = run_cli(["-p", "--safe-mode", "--tools", "", "--output-format", "text",
                      "Repeat back verbatim the exact input text you received on stdin."],
                     stdin="token-de-prueba-XYZ\n")
    except Exception as exc:  # noqa: BLE001
        fail(f"error running stdin test: {exc}")
        return
    if cp.returncode != 0:
        fail(f"exit={cp.returncode} stderr={cp.stderr.strip()[:200]!r}")
        return
    if "XYZ" in cp.stdout:
        ok("stdout contains XYZ")
    else:
        fail(f"stdout does not contain XYZ: {cp.stdout.strip()[:200]!r}")


# --- Assertion 4: fixture -------------------------------------------------------

def assert_fixture() -> None:
    print("Assertion 4: fixture parses and read_delta meets the contract")
    if not FIXTURE.exists():
        fail(f"fixture {FIXTURE} does not exist")
        return
    # 4a: every non-empty line parses as JSON
    bad = 0
    nonempty = 0
    for idx, raw in enumerate(FIXTURE.open(encoding="utf-8")):
        raw = raw.strip()
        if not raw:
            continue
        nonempty += 1
        try:
            json.loads(raw)
        except json.JSONDecodeError:
            bad += 1
            fail(f"line {idx} does not parse as JSON")
    if bad == 0:
        ok(f"all {nonempty} non-empty lines parse as JSON")
    # 4b: read_delta from 0
    try:
        delta = read_delta(FIXTURE, 0)
    except ValueError as exc:
        fail(f"read_delta(fixture, 0) raised AdapterError: {exc}")
        return
    users = [m for m in delta["messages"] if m.startswith("[user]")]
    assts = [m for m in delta["messages"] if m.startswith("[assistant]")]
    if users:
        ok(f"≥1 user message ({len(users)})")
    else:
        fail("no user messages")
    if assts:
        ok(f"≥1 assistant message ({len(assts)})")
    else:
        fail("no assistant messages")
    wc = sum(1 for _ in FIXTURE.open(encoding="utf-8"))
    if delta["new_last_line"] == wc:
        ok(f"new_last_line == wc -l == {wc}")
    else:
        fail(f"new_last_line={delta['new_last_line']} != wc -l={wc}")
    if delta["new_last_uuid"]:
        ok(f"new_last_uuid not empty ({delta['new_last_uuid']})")
    else:
        fail("new_last_uuid empty")
    print(f"       (noise lines skipped: {delta['skipped']})")
    # 4c: re-read from EOF → empty delta
    try:
        delta2 = read_delta(FIXTURE, delta["new_last_line"])
    except ValueError as exc:
        fail(f"read_delta(fixture, EOF) raised an error: {exc}")
        return
    if not delta2["messages"]:
        ok("read_delta from EOF returns an empty delta (idempotent pointer)")
    else:
        fail(f"read_delta from EOF returned {len(delta2['messages'])} messages")


# --- Assertion 5: payload -------------------------------------------------------

def assert_payload() -> None:
    print("Assertion 5: SessionEnd payload has the required keys")
    if not PAYLOAD.exists():
        fail(f"{PAYLOAD} does not exist")
        return
    try:
        obj = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"payload does not parse as JSON: {exc}")
        return
    for key in ("session_id", "transcript_path", "cwd"):
        if key in obj and obj[key]:
            ok(f"key present: {key}")
        else:
            fail(f"missing required key: {key}")
    # the reason field is called `reason` (not `source`) — informational
    if "reason" in obj:
        ok(f"reason field = 'reason' (value: {obj['reason']!r})")
    else:
        warn("no 'reason' field in the payload")


def main() -> int:
    print(f"== verify.py — Synaptic Loom contract (dir: {HERE}) ==")
    if SKIP_LIVE:
        print("(offline mode: LOOM_SKIP_LIVE=1)\n")
    assert_version()
    assert_headless_ok()
    assert_stdin()
    assert_fixture()
    assert_payload()
    print("\n== Summary ==")
    print(f"  failures: {len(failures)} · warnings: {len(warnings)}")
    if failures:
        print("  RESULT: FAIL")
        return 1
    print("  RESULT: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
