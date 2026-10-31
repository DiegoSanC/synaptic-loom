"""The child side of a client plugin run (D23): `python -P -m loom.plugin_runner <name>`.

Reads the stored entry (JSON) on stdin, loads the entry point `<name>` of the group
`loom.client_plugins`, calls its `register(loom)` with a :class:`loom.plugins.PluginHost`
and hands the entry to every callback it subscribed. Its own module, so that the
classes a plugin imports from `loom.plugins` are the ones it is handed. A watchdog
kills this process group when the pass that started it is gone.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from importlib.metadata import entry_points

from loom.config import Config
from loom.plugins import GROUP, PARENT_ENV, PluginHost, StoredEntry

# Exit status when no installed distribution declares the name (uninstalled meanwhile).
NOT_FOUND = 2


def _orphaned(parent: int) -> bool:
    return os.getppid() != parent


def _watch_parent(parent: int) -> None:
    while not _orphaned(parent):
        time.sleep(1)
    os.killpg(os.getpgrp(), signal.SIGKILL)


def main(argv: list[str] | None = None) -> int:
    [name] = sys.argv[1:] if argv is None else argv
    # The pass's pid comes from the pass itself: read from getppid() here, it would
    # already be the reaper's if the pass died while this process was starting.
    parent = int(os.environ.get(PARENT_ENV) or os.getppid())
    if _orphaned(parent):
        os.killpg(os.getpgrp(), signal.SIGKILL)
    threading.Thread(target=_watch_parent, args=(parent,), daemon=True).start()
    entry = StoredEntry(**json.loads(sys.stdin.read()))
    matches = [ep for ep in entry_points(group=GROUP) if ep.name == name]
    if not matches:
        return NOT_FOUND
    register = matches[0].load()
    host = PluginHost(Config.load())
    register(host)
    for callback in host.callbacks:
        callback(entry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
