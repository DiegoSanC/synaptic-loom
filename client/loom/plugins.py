"""Client plugins (D23): installed distributions the capture pass calls after an entry is
stored.

A plugin declares an entry point in the group `loom.client_plugins` whose object is a
callable `register(loom)`; `loom` is a :class:`PluginHost`, and the plugin subscribes with
`loom.on_entry_stored(callback)`. After a `loom process` pass has settled every marker and
released its lock, it spools the entries it stored (201) in `plugins_spool/` and takes
`plugins.lock` without waiting. The holder runs each plugin once per spooled entry,
entries in order and plugins by entry-point name, until the spool is empty; a pass that
finds the lock held leaves its entries to the holder, or to the next pass. Each run is a
child process of its own (`python -P -m loom.plugin_runner <name>`) with
`plugins.timeout_s` of wall clock. When the run ends, however it ends, the child's whole
process group is killed, and the child kills its group itself if the pass dies first.

A plugin can cost no capture: the entry, the pointer and the queue are settled before it
runs, and whatever it does (raise, fail to import, hang) ends in one `plugin_failed`
line naming it, never in anything it printed (I5). Its work for that entry is not
retried. A client with no plugin installed starts no process and writes no line.

What a plugin is handed and what it sends through the host follow the core's own rules:
the extract is there only for a project that keeps extracts (D22), and a body given to
`server_request` passes the step-8 secret scan before anything is sent (I4).
"""
from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from importlib.metadata import entry_points
from pathlib import Path
from typing import Callable

import requests

from loom import scan_engine
from loom.api_client import ApiClient, ServerUnreachable
from loom.config import Config

GROUP = "loom.client_plugins"
# Where a pass leaves the entries it stored until a plugin run takes them (0700; each
# file 0600: an entry carries its extract).
SPOOL_DIR = "plugins_spool"
LOCK_FILE = "plugins.lock"
# The pid of the pass, so the child's watchdog knows whom to outlive no longer.
PARENT_ENV = "LOOM_PLUGIN_PARENT"
# Exit statuses the runner keeps for what it has to say itself, and the reason each one
# is logged as. They are away from the statuses programs exit with by convention (1 and
# 2, sysexits' 64-78, the shells' 126 and up for signals, 255), so a plugin's own
# `sys.exit(2)` is logged as `exit=2` and not as one of these.
NOT_INSTALLED = 201     # no installed distribution declares the name any more
BODY_REFUSED = 202      # a `BodyRefused` the plugin did not catch
RUNNER_REASONS = {NOT_INSTALLED: "not_installed", BODY_REFUSED: "body_refused"}


@dataclasses.dataclass(frozen=True)
class StoredEntry:
    """What a plugin is handed: one entry as the server stored it.

    ``content_md``, ``items`` and ``item_types`` are exactly what was posted;
    ``extract`` is the text the summarizer read (step 5b's output, every detected secret
    already replaced, D16), cut as D22 cuts it, when the project's listing says it keeps
    extracts, and empty when it does not: what was posted with the entry, and no more;
    ``extract_stored`` says whether the server kept it.
    """

    entry_id: int
    project_remote: str
    session_id: str
    content_md: str
    items: list
    item_types: list
    extract: str
    extract_stored: bool


class ModelCallFailed(Exception):
    """`run_model` could not get an answer: a timeout, a non-zero exit, or no binary."""


class BodyRefused(Exception):
    """`server_request` sent nothing: the secret scan flagged the body, or could not
    run (fail-closed, I4). The message names the kinds found or the error's type, never
    the body or a value."""


class PluginHost:
    """The `loom` a plugin's `register` receives (the child-process side)."""

    ModelCallFailed = ModelCallFailed
    ServerUnreachable = ServerUnreachable
    BodyRefused = BodyRefused

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self.callbacks: list[Callable[[StoredEntry], None]] = []

    def on_entry_stored(self, callback: Callable[[StoredEntry], None]) -> None:
        """Call `callback(entry)` with the stored entry this run is about."""
        self.callbacks.append(callback)

    def run_model(self, prompt: str, text: str, model: str | None = None) -> str:
        """The model's answer to `prompt` over `text`, through the summarizer's own
        invocation (`--safe-mode --tools ""`, cwd `LOOM_HOME`, the anti-recursion guard,
        the install's Claude Code configuration directory) and its timeout. `model`
        defaults to the summarizer's. Raises :class:`ModelCallFailed`."""
        from loom import processor   # the processor imports this module

        try:
            done = processor.run_claude(self._cfg, prompt, text,
                                        model=model or self._cfg.model)
        except subprocess.TimeoutExpired as exc:
            raise ModelCallFailed("timeout") from exc
        except OSError as exc:
            raise ModelCallFailed(type(exc).__name__) from exc
        if done.returncode != 0:
            raise ModelCallFailed(f"exit={done.returncode}")
        return done.stdout

    def server_request(self, method: str, path: str, json: object = None
                       ) -> requests.Response:
        """One request to the configured server with this client's bearer. `path` starts
        with `/`. Any HTTP answer is returned as it came, whatever its status; raises
        :class:`ServerUnreachable` only when the server cannot be reached.

        A body (`json` other than None) is serialized here and scanned as step 8 scans
        an entry (gitleaks ∪ regex) before anything is sent: a finding, or a scan that
        cannot run, raises :class:`BodyRefused` and nothing leaves. A request without a
        body is not scanned."""
        if not path.startswith("/"):
            raise ValueError("path must start with '/'")
        api = ApiClient(self._cfg.server_url, self._cfg.token)
        if json is None:
            return api._request(method, path)
        return api._request(method, path, data=_scanned_body(self._cfg, json),
                            headers={"Content-Type": "application/json"})


def _strings(value: object):
    """Every string in a JSON value: object keys, and strings at any depth."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _scanned_body(cfg: Config, payload: object) -> bytes:
    """`payload` as the JSON bytes to send, or :class:`BodyRefused`.

    The scan reads the serialized body and, after it, every string in it as plain text:
    JSON escapes the quotes and line ends inside a string, and a rule that reads
    `key = "value"` does not match `key = \\"value\\"`. The bytes returned are the ones
    scanned, so what is sent is what passed.
    """
    body = json.dumps(payload, allow_nan=False)
    try:
        findings = scan_engine.scan("\n".join([body, *_strings(payload)]), cfg)
    except Exception as exc:  # noqa: BLE001 — fail-closed: no verdict == a hit (I4).
        raise BodyRefused("body not sent: the secret scan could not run "
                          f"({type(exc).__name__})") from None
    if findings:
        kinds = ", ".join(sorted({finding.kind for finding in findings}))
        raise BodyRefused(f"body not sent: the secret scan flagged it ({kinds})")
    return body.encode("utf-8")


# --------------------------------------------------------------------------- #
# The pass side                                                                #
# --------------------------------------------------------------------------- #
def installed() -> list[tuple[str, str]]:
    """`(name, target)` of every installed plugin, by name; the first one found when two
    distributions declare the same name, as the child loads it. Nothing is imported."""
    found: dict[str, str] = {}
    for entry_point in entry_points(group=GROUP):
        found.setdefault(entry_point.name, entry_point.value)
    return sorted(found.items())


def declared_twice() -> list[str]:
    """Names more than one installed distribution declares (only the first one runs)."""
    names = [entry_point.name for entry_point in entry_points(group=GROUP)]
    return sorted({name for name in names if names.count(name) > 1})


def stored_entry(resp: requests.Response, payload: dict, extract: str) -> StoredEntry | None:
    """The entry a 201 to `POST /api/entries` stored, or None if the answer has no id.

    Total: it runs after the entry is stored and settled, where nothing may raise.
    """
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — an unreadable answer only means no plugin call.
        return None
    entry_id = body.get("id") if isinstance(body, dict) else None
    if isinstance(entry_id, bool) or not isinstance(entry_id, int):
        return None
    return StoredEntry(
        entry_id=entry_id,
        project_remote=payload["project_remote"],
        session_id=payload["session_id"],
        content_md=payload["content_md"],
        items=payload.get("items", []),
        item_types=payload["item_types"],
        extract=extract,
        extract_stored=body.get("extract_stored") is True,
    )


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


def _exited(child: subprocess.Popen) -> bool:
    """Has the child exited? Without reaping it where the platform allows, so its pid,
    and with it the process group id, cannot be reused before the group is killed."""
    if hasattr(os, "waitid"):
        return os.waitid(os.P_PID, child.pid,
                         os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    return child.poll() is not None


def _run_one(cfg: Config, name: str, entry: StoredEntry) -> str | None:
    """Run plugin `name` on `entry` in a child process. None on success, else why not."""
    child_env = {**os.environ, "LOOM_HOME": str(cfg.loom_home), "LOOM_SUMMARIZER": "1",
                 PARENT_ENV: str(os.getpid())}
    # The entry goes in through an unnamed file, not a pipe: a child that never reads
    # its stdin cannot block the pass on a full pipe buffer.
    with tempfile.TemporaryFile("w+", encoding="utf-8", dir=cfg.loom_home) as handed:
        json.dump(dataclasses.asdict(entry), handed)
        handed.seek(0)
        try:
            child = subprocess.Popen(
                [sys.executable, "-P", "-m", "loom.plugin_runner", name],
                stdin=handed, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=str(cfg.loom_home), env=child_env, start_new_session=True,
            )
        except OSError as exc:
            return f"error={type(exc).__name__}"
    deadline = time.monotonic() + cfg.plugin_timeout_s
    timed_out = False
    try:
        while not _exited(child):
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.05)
    finally:
        # However the run ended (done, failed, timed out, or the pass interrupted), what
        # the plugin started goes with it: nothing of a plugin outlives its run. The
        # group is killed BEFORE the child is reaped, while its id cannot be reused.
        _kill_group(child.pid)
        child.wait()
    if timed_out:
        return "timeout"
    if child.returncode == 0:
        return None
    return RUNNER_REASONS.get(child.returncode, f"exit={child.returncode}")


def _spool(spool: Path, entry: StoredEntry) -> None:
    """Write `entry` into the spool atomically, readable by this user only."""
    name = f"{time.time_ns():020d}-{os.getpid()}-{entry.entry_id}"
    tmp = spool / f".{name}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(dataclasses.asdict(entry), fh)
    os.replace(tmp, spool / f"{name}.json")


def _drain(cfg: Config, spool: Path, names: list[str],
           log: Callable[[Config, str, str, str], None]) -> None:
    """Run every plugin on every spooled entry, oldest first, until the spool is empty.
    Each file is removed before its runs start: a plugin's work is never retried. A file
    that cannot be removed is therefore not run, and ends the drain: it would be found,
    and run, again on every turn of this loop."""
    while True:
        pending = sorted(spool.glob("*.json"))
        if not pending:
            return
        for path in pending:
            try:
                entry = StoredEntry(**json.loads(path.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001 — a damaged file is dropped, not retried.
                entry = None
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                log(cfg, "plugins", "plugin_failed",
                    f"spool not removed file={path.name} error={type(exc).__name__}")
                return
            if entry is None:
                log(cfg, "plugins", "plugin_failed", f"spool error file={path.name}")
                continue
            for name in names:
                try:
                    why = _run_one(cfg, name, entry)
                except Exception as exc:  # noqa: BLE001 — isolate the plugin.
                    why = f"error={type(exc).__name__}"
                detail = f"name={name} entry={entry.entry_id}"
                if why is None:
                    log(cfg, entry.session_id, "plugin_done", detail)
                else:
                    log(cfg, entry.session_id, "plugin_failed", f"{detail} {why}")


def _drop_spool(cfg: Config, spool: Path,
                log: Callable[[Config, str, str, str], None]) -> None:
    """With no plugin left to run them, spooled entries (each with its extract) are
    deleted rather than kept on disk indefinitely."""
    try:
        leftovers = [path for path in spool.iterdir() if path.is_file()]
    except OSError:
        return
    dropped = 0
    for path in leftovers:
        try:
            path.unlink()
            dropped += path.suffix == ".json"
        except OSError:
            pass
    if dropped:
        log(cfg, "plugins", "plugin_spool_dropped", f"entries={dropped}")


def run_after_pass(cfg: Config, names: list[str] | None, entries: list[StoredEntry],
                   log: Callable[[Config, str, str, str], None]) -> None:
    """Spool `entries` and, unless another pass is already running plugins, run every
    plugin in `names` on everything spooled. Never waits for a lock, never raises an
    ``Exception``: nothing a plugin does reaches or holds up the pass. `names` None means
    the plugin list could not be read: the spool is left as it is for a later pass."""
    if names is None:
        return
    spool = cfg.loom_home / SPOOL_DIR
    if not names:
        _drop_spool(cfg, spool, log)
        return
    try:
        spool.mkdir(mode=0o700, exist_ok=True)
        for entry in entries:
            _spool(spool, entry)
    except OSError as exc:
        log(cfg, "plugins", "plugin_failed", f"spool error={type(exc).__name__}")
        return
    try:
        lock = open(cfg.loom_home / LOCK_FILE, "w")
    except OSError as exc:
        log(cfg, "plugins", "plugin_failed", f"lock error={type(exc).__name__}")
        return
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return      # the holder drains what we spooled, or the next pass does
        except OSError as exc:
            log(cfg, "plugins", "plugin_failed", f"lock error={type(exc).__name__}")
            return
        _drain(cfg, spool, names, log)
    finally:
        lock.close()
