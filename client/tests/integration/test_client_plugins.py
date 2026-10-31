"""D23 — client plugins: installed distributions the capture pass calls after an entry
is stored.

Asserted through the real CLI entry point (``loom process``), the REAL gitleaks 8.30.1,
a ``claude`` stub and a mock server. A plugin is installed the way pip installs one
(``install_client_plugin``), and runs where it will run in production: in a child
process of the pass, found by its entry point in ``loom.client_plugins``.

- After a pass stores an entry (201), each plugin is called once with that entry: its
  id, project, session, text, items, the extract the summarizer read, and whether the
  server kept it.
- An entry that is a duplicate (409) or quarantined is never handed to a plugin.
- A plugin that raises, cannot be imported, or outlives its timeout costs no capture:
  the entry, the pointer and the queue are settled before it runs. It leaves one
  ``plugin_failed`` line naming it, and nothing it printed.
- Plugins run in entry-point-name order, and one failing does not stop the next.
- ``run_model`` calls the summarizer's binary with the summarizer's isolation, and
  ``server_request`` reaches the configured server with the client's credential.
"""
from __future__ import annotations

import fcntl
import importlib
import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from werkzeug import Response

from loom import cli
from loom.config import Config

CANNED_SUMMARY = "## Context\nThe importer emits None.\n## Decisions\n- total() skips None.\n"
TURNS = [
    ("user", "The importer emits None for empty rows; make total() skip them."),
    ("assistant", "Done. total() now skips None and the importer test passes."),
]
REAL_GITLEAKS = shutil.which("gitleaks")
REMOTE = "github.com/org/b"     # the integration env's repo remote (conftest)
ENTRY_ID = 7
API_SECRET = "zX9j2kQ8vB4nM7cL1wR5tY6uI3oP0aSdFgHjKlZxCvB"   # gitleaks: generic-api-key

# Writes everything the plugin was handed to $PLUGIN_OUT/<name>-<entry id>.json.
RECORDER = """
    import dataclasses, json, os, pathlib

    def register(loom):
        def stored(entry):
            out = pathlib.Path(os.environ["PLUGIN_OUT"]) / f"{NAME}-{entry.entry_id}.json"
            out.write_text(json.dumps(dataclasses.asdict(entry)))
        loom.on_entry_stored(stored)
"""


def _recorder(name: str) -> str:
    return f"NAME = {name!r}\n" + textwrap.dedent(RECORDER)


def _transcript(env, turns) -> None:
    lines = [{"type": "file-history-snapshot", "messageId": "m0", "snapshot": {}}]
    for i, (role, text) in enumerate(turns):
        content = text if role == "user" else [{"type": "text", "text": text}]
        lines.append({"type": role, "uuid": f"t-{i}", "cwd": str(env.repo),
                      "timestamp": "2026-07-24T10:00:00Z",
                      "message": {"role": role, "content": content}})
    env.transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\n",
                              encoding="utf-8")


def _claude(env, calls: Path, *, summary: str = CANNED_SUMMARY) -> str:
    """A ``claude`` stub that records every call (argv, cwd, the guard, stdin). It
    answers the summarizer with ``summary``, ``PLUGIN PROMPT`` with ``model says hi``,
    and exits 3 on ``FAILING PROMPT``."""
    path = env.home / "claude_recorder.py"
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "stdin = sys.stdin.read()\n"
        f"with open({str(calls)!r}, 'a') as fh:\n"
        "    fh.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
        "                         'guard': os.environ.get('LOOM_SUMMARIZER'),\n"
        "                         'stdin': stdin}) + '\\n')\n"
        "if sys.argv[-1] == 'FAILING PROMPT':\n"
        "    sys.exit(3)\n"
        "if sys.argv[-1] == 'PLUGIN PROMPT':\n"
        "    sys.stdout.write('model says hi')\n"
        "else:\n"
        f"    sys.stdout.write({summary!r})\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path)


def _configure(env, tmp_path, *, summary: str = CANNED_SUMMARY, plugins: dict | None = None,
               entry_status: int = 201, entry_body: dict | None = None,
               store_extracts: bool = True) -> Path:
    assert REAL_GITLEAKS, "the suite requires the real gitleaks (AGENTS.md)"
    calls = tmp_path / "claude_calls.jsonl"
    _transcript(env, TURNS)
    env.config(_claude(env, calls, summary=summary))
    data = env.config_json()
    data["scanner"] = {"engine": "gitleaks", "gitleaks_version": "8.30.1"}
    if plugins is not None:
        data["plugins"] = plugins
    (env.home / "config.json").write_text(json.dumps(data), encoding="utf-8")
    project = {"id": 1, "canonical_remote": REMOTE, "name": "loom-test",
               "created_by": "dev2", "created_at": "2026-07-01T00:00:00Z",
               "store_extracts": store_extracts}
    env.httpserver.expect_request("/api/projects", method="GET").respond_with_json(
        {"projects": [project]})
    body = entry_body if entry_body is not None else {"id": ENTRY_ID, "extract_stored": True}
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        body, status=entry_status)
    env.drop_marker("sess-plugin")
    return calls


@pytest.fixture
def plugin_out(tmp_path, monkeypatch) -> Path:
    out = tmp_path / "plugin_out"
    out.mkdir()
    monkeypatch.setenv("PLUGIN_OUT", str(out))
    return out


@pytest.fixture
def run_cli(env, monkeypatch, capsys):
    monkeypatch.setenv("LOOM_HOME", str(env.home))

    def _run(*argv: str) -> tuple[int, str]:
        rc = cli.main(list(argv))
        out, err = capsys.readouterr()
        return rc, out + err
    return _run


def _log_lines(env, action: str) -> list[str]:
    log = env.home / "log" / "processor.log"
    text = log.read_text(encoding="utf-8") if log.exists() else ""
    return [line for line in text.splitlines() if f"\t{action}\t" in line]


def _capture_settled(env) -> None:
    """The entry was posted, the queue is empty and the pointer moved past the delta."""
    assert len(env.posted_entries()) == 1
    assert list((env.home / "queue").glob("*.json")) == []
    assert env.pointer().get("last_line", 0) > 0


# --------------------------------------------------------------------------- #
# What a plugin is handed                                                      #
# --------------------------------------------------------------------------- #
def test_a_plugin_is_handed_the_stored_entry(env, run_cli, tmp_path, plugin_out,
                                             install_client_plugin):
    install_client_plugin("recorder", _recorder("recorder"))
    calls = _configure(env, tmp_path)

    rc, output = run_cli("process")

    assert rc == 0, output
    [posted] = env.posted_entries()
    summarizer_stdin = json.loads(calls.read_text(encoding="utf-8").splitlines()[0])["stdin"]
    handed = json.loads((plugin_out / f"recorder-{ENTRY_ID}.json").read_text())
    assert handed == {
        "entry_id": ENTRY_ID,
        "project_remote": REMOTE,
        "session_id": "sess-plugin",
        "content_md": posted["content_md"],
        "items": posted.get("items", []),
        "item_types": posted["item_types"],
        "extract": summarizer_stdin,
        "extract_stored": True,
    }
    assert "total() skips None" in handed["content_md"]
    assert [line for line in _log_lines(env, "plugin_done")
            if f"name=recorder entry={ENTRY_ID}" in line]


def test_the_extract_is_redacted_and_the_flag_follows_the_server(
        env, run_cli, tmp_path, plugin_out, install_client_plugin):
    install_client_plugin("recorder", _recorder("recorder"))
    env_turns = [("user", f'Set api_key = "{API_SECRET}" and make total() skip None.'),
                 TURNS[1]]
    _configure(env, tmp_path, entry_body={"id": ENTRY_ID, "extract_stored": False})
    _transcript(env, env_turns)

    rc, output = run_cli("process")

    assert rc == 0, output
    handed = json.loads((plugin_out / f"recorder-{ENTRY_ID}.json").read_text())
    assert API_SECRET not in handed["extract"]
    assert "[REDACTED:generic-api-key]" in handed["extract"]
    assert handed["extract_stored"] is False


def test_without_plugins_nothing_is_run_or_logged(env, run_cli, tmp_path):
    _configure(env, tmp_path)

    rc, output = run_cli("process")

    assert rc == 0, output
    _capture_settled(env)
    assert _log_lines(env, "plugin_done") == []
    assert _log_lines(env, "plugin_failed") == []


@pytest.mark.parametrize("case", ["duplicate", "quarantined"])
def test_an_entry_that_was_not_stored_is_never_handed_over(
        env, run_cli, tmp_path, plugin_out, install_client_plugin, case):
    install_client_plugin("recorder", _recorder("recorder"))
    if case == "duplicate":
        _configure(env, tmp_path, entry_status=409,
                   entry_body={"error": {"code": "duplicate_entry", "message": "dup"}})
    else:
        _configure(env, tmp_path, summary=CANNED_SUMMARY + f'- Set api_key = "{API_SECRET}"\n')

    rc, output = run_cli("process")

    assert rc == 0, output
    assert list(plugin_out.iterdir()) == []
    assert _log_lines(env, "plugin_done") == []


# --------------------------------------------------------------------------- #
# A failing plugin costs no capture                                            #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("source", [
    """
    def register(loom):
        def stored(entry):
            raise RuntimeError("PLUGIN-SECRET-SENTINEL")
        loom.on_entry_stored(stored)
    """,
    """
    import module_that_is_not_installed

    def register(loom):
        pass
    """,
], ids=["raises", "cannot-import"])
def test_a_failing_plugin_costs_no_capture(env, run_cli, tmp_path, install_client_plugin,
                                           source):
    install_client_plugin("broken", source)
    _configure(env, tmp_path)

    rc, output = run_cli("process")

    assert rc == 0, output
    _capture_settled(env)
    [failed] = _log_lines(env, "plugin_failed")
    assert f"name=broken entry={ENTRY_ID} exit=1" in failed
    log = (env.home / "log" / "processor.log").read_text(encoding="utf-8")
    assert "PLUGIN-SECRET-SENTINEL" not in log
    assert "module_that_is_not_installed" not in log


def test_a_plugin_that_outlives_its_timeout_is_killed_with_what_it_started(
        env, run_cli, tmp_path, plugin_out, install_client_plugin):
    install_client_plugin("sleeper", """
        import os, pathlib, subprocess, time

        def register(loom):
            def stored(entry):
                child = subprocess.Popen(["sleep", "120"])
                (pathlib.Path(os.environ["PLUGIN_OUT"]) / "pid").write_text(str(child.pid))
                time.sleep(120)
            loom.on_entry_stored(stored)
    """)
    _configure(env, tmp_path, plugins={"timeout_s": 2})

    started = time.monotonic()
    rc, output = run_cli("process")
    elapsed = time.monotonic() - started

    assert rc == 0, output
    assert elapsed < 60
    _capture_settled(env)
    [failed] = _log_lines(env, "plugin_failed")
    assert f"name=sleeper entry={ENTRY_ID} timeout" in failed
    pid = int((plugin_out / "pid").read_text())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and Path(f"/proc/{pid}").exists():
        time.sleep(0.1)
    assert not Path(f"/proc/{pid}").exists(), "the plugin's own child survived it"


def test_plugins_run_in_name_order_and_a_failure_does_not_stop_the_next(
        env, run_cli, tmp_path, plugin_out, install_client_plugin):
    order = """
        import os, pathlib

        def register(loom):
            def stored(entry):
                with open(pathlib.Path(os.environ["PLUGIN_OUT"]) / "order", "a") as fh:
                    fh.write(NAME + "\\n")
            loom.on_entry_stored(stored)
    """
    install_client_plugin("b_second", "NAME = 'b_second'\n" + textwrap.dedent(order))
    install_client_plugin("a_first", "NAME = 'a_first'\n" + textwrap.dedent(order))
    install_client_plugin("a_middle", """
        def register(loom):
            raise RuntimeError("cannot register")
    """)
    _configure(env, tmp_path)

    rc, output = run_cli("process")

    assert rc == 0, output
    assert (plugin_out / "order").read_text().splitlines() == ["a_first", "b_second"]
    assert [line.split("\t")[-1] for line in _log_lines(env, "plugin_failed")] == [
        f"name=a_middle entry={ENTRY_ID} exit=1"]


# --------------------------------------------------------------------------- #
# What a plugin can use                                                        #
# --------------------------------------------------------------------------- #
def test_run_model_calls_claude_with_the_summarizers_isolation(
        env, run_cli, tmp_path, plugin_out, install_client_plugin):
    install_client_plugin("modeller", """
        import os, pathlib

        def register(loom):
            def stored(entry):
                answer = loom.run_model("PLUGIN PROMPT", "plugin input", "haiku")
                (pathlib.Path(os.environ["PLUGIN_OUT"]) / "answer").write_text(answer)
            loom.on_entry_stored(stored)
    """)
    calls = _configure(env, tmp_path)

    rc, output = run_cli("process")

    assert rc == 0, output
    assert (plugin_out / "answer").read_text() == "model says hi"
    summarizer, plugin_call = [json.loads(x) for x in calls.read_text().splitlines()]
    assert plugin_call["argv"] == ["-p", "--safe-mode", "--tools", "", "--model", "haiku",
                                   "--output-format", "text", "PLUGIN PROMPT"]
    assert plugin_call["stdin"] == "plugin input"
    assert plugin_call["guard"] == "1"
    assert Path(plugin_call["cwd"]).resolve() == env.home.resolve()
    assert summarizer["argv"][:5] == plugin_call["argv"][:5]


def test_run_model_defaults_to_the_summarizers_model_and_raises_on_failure(
        env, run_cli, tmp_path, plugin_out, install_client_plugin):
    install_client_plugin("modeller", """
        import os, pathlib

        def register(loom):
            def stored(entry):
                out = pathlib.Path(os.environ["PLUGIN_OUT"])
                (out / "default").write_text(loom.run_model("PLUGIN PROMPT", "x"))
                try:
                    loom.run_model("FAILING PROMPT", "x")
                except loom.ModelCallFailed as exc:
                    (out / "raised").write_text(str(exc))
            loom.on_entry_stored(stored)
    """)
    calls = _configure(env, tmp_path)

    rc, output = run_cli("process")

    assert rc == 0, output
    assert (plugin_out / "default").read_text() == "model says hi"
    assert (plugin_out / "raised").read_text() == "exit=3"
    default_call = [json.loads(x) for x in calls.read_text().splitlines()][1]
    assert default_call["argv"][default_call["argv"].index("--model") + 1] == "sonnet"


def test_server_request_reaches_the_server_with_the_clients_credential(
        env, run_cli, tmp_path, plugin_out, install_client_plugin):
    install_client_plugin("poster", """
        import os, pathlib

        def register(loom):
            def stored(entry):
                resp = loom.server_request("POST", "/api/plugin-test",
                                           json={"entry": entry.entry_id})
                (pathlib.Path(os.environ["PLUGIN_OUT"]) / "status").write_text(
                    str(resp.status_code))
            loom.on_entry_stored(stored)
    """)
    _configure(env, tmp_path)
    env.httpserver.expect_request(
        "/api/plugin-test", method="POST",
        headers={"Authorization": "Bearer test-token"},
    ).respond_with_json({"ok": True}, status=201)

    rc, output = run_cli("process")

    assert rc == 0, output
    assert (plugin_out / "status").read_text() == "201"
    [request] = [r for r, _ in env.httpserver.log if r.path == "/api/plugin-test"]
    assert request.get_json() == {"entry": ENTRY_ID}


# --------------------------------------------------------------------------- #
# The timeout key                                                              #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw, expected", [
    (None, 600), (5, 5), (1, 1), (3600, 3600),
    (0, 600), (3601, 600), ("30", 600), (True, 600), (2.5, 600),
])
def test_the_plugin_timeout_key(tmp_path, raw, expected):
    data = {"server_url": "http://localhost", "token": "t"}
    if raw is not None:
        data["plugins"] = {"timeout_s": raw}
    (tmp_path / "config.json").write_text(json.dumps(data), encoding="utf-8")

    assert Config.load(tmp_path).plugin_timeout_s == expected


@pytest.mark.parametrize("section", [[], "600", None])
def test_an_unusable_plugins_section_keeps_the_default(tmp_path, section):
    data = {"server_url": "http://localhost", "token": "t", "plugins": section}
    (tmp_path / "config.json").write_text(json.dumps(data), encoding="utf-8")

    assert Config.load(tmp_path).plugin_timeout_s == 600


# --------------------------------------------------------------------------- #
# When plugins run, and what they leave behind                                 #
# --------------------------------------------------------------------------- #
OBSERVER = """
    import fcntl, json, os, pathlib

    def register(loom):
        def stored(entry):
            home = pathlib.Path(os.environ["LOOM_HOME"])
            out = pathlib.Path(os.environ["PLUGIN_OUT"])
            with open(home / "lock", "w") as fh:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    lock_free = True
                except OSError:
                    lock_free = False
            record = {
                "entry": entry.entry_id,
                "queue": sorted(p.name for p in (home / "queue").glob("*.json")),
                "pointers": sorted(json.loads((home / "state.json").read_text())),
                "posted": (home / "log" / "processor.log").read_text().count("\\tposted"),
                "lock_free": lock_free,
                "guard": os.environ.get("LOOM_SUMMARIZER"),
            }
            with open(out / "observed.jsonl", "a") as fh:
                fh.write(json.dumps(record) + "\\n")
            with open(out / "order", "a") as fh:
                fh.write(f"{entry.entry_id} observer\\n")
        loom.on_entry_stored(stored)
"""


def _two_sessions(env, tmp_path) -> Path:
    """Two queued sessions whose entries the mock server stores as 7 and 8."""
    calls = _configure(env, tmp_path)
    second = tmp_path / "transcript2.jsonl"
    second.write_text(env.transcript.read_text(encoding="utf-8").replace(
        "make total() skip them", "make average() skip them"), encoding="utf-8")
    env.drop_marker("sess-second", transcript=second)
    ids = iter([ENTRY_ID, ENTRY_ID + 1])
    env.httpserver.clear_all_handlers()
    env.httpserver.expect_request("/api/projects", method="GET").respond_with_json(
        {"projects": [{"id": 1, "canonical_remote": REMOTE, "name": "loom-test",
                       "created_by": "dev2", "created_at": "2026-07-01T00:00:00Z",
                       "store_extracts": True}]})
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_handler(
        lambda request: Response(json.dumps({"id": next(ids), "extract_stored": True}),
                                 status=201, content_type="application/json"))
    return second


def test_plugins_run_after_every_entry_of_the_pass_is_settled_and_the_lock_is_free(
        env, run_cli, tmp_path, plugin_out, install_client_plugin):
    install_client_plugin("observer", OBSERVER)
    install_client_plugin("zz_after", """
        import os, pathlib

        def register(loom):
            def stored(entry):
                with open(pathlib.Path(os.environ["PLUGIN_OUT"]) / "order", "a") as fh:
                    fh.write(f"{entry.entry_id} zz_after\\n")
            loom.on_entry_stored(stored)
    """)
    second = _two_sessions(env, tmp_path)

    rc, output = run_cli("process")

    assert rc == 0, output
    assert len(env.posted_entries()) == 2
    observed = [json.loads(x) for x in (plugin_out / "observed.jsonl").read_text().splitlines()]
    assert [o["entry"] for o in observed] == [ENTRY_ID, ENTRY_ID + 1]
    for seen in observed:
        assert seen["queue"] == []
        assert seen["pointers"] == sorted([str(env.transcript), str(second)])
        assert seen["posted"] == 2
        assert seen["lock_free"] is True
        assert seen["guard"] == "1"
    # Entries in the outer loop, plugins by name in the inner one.
    assert (plugin_out / "order").read_text().splitlines() == [
        f"{ENTRY_ID} observer", f"{ENTRY_ID} zz_after",
        f"{ENTRY_ID + 1} observer", f"{ENTRY_ID + 1} zz_after"]


def test_a_project_that_keeps_no_extracts_still_hands_the_plugin_its_extract(
        env, run_cli, tmp_path, plugin_out, install_client_plugin):
    install_client_plugin("recorder", _recorder("recorder"))
    calls = _configure(env, tmp_path, store_extracts=False,
                       entry_body={"id": ENTRY_ID, "extract_stored": False})

    rc, output = run_cli("process")

    assert rc == 0, output
    [posted] = env.posted_entries()
    assert "extract" not in posted
    handed = json.loads((plugin_out / f"recorder-{ENTRY_ID}.json").read_text())
    summarizer_stdin = json.loads(calls.read_text(encoding="utf-8").splitlines()[0])["stdin"]
    assert handed["extract"] == summarizer_stdin
    assert handed["extract_stored"] is False


def test_a_201_without_an_id_is_handed_to_no_plugin(env, run_cli, tmp_path, plugin_out,
                                                     install_client_plugin):
    install_client_plugin("recorder", _recorder("recorder"))
    _configure(env, tmp_path, entry_body={"extract_stored": True})

    rc, output = run_cli("process")

    assert rc == 0, output
    _capture_settled(env)
    assert list(plugin_out.iterdir()) == []
    assert _log_lines(env, "plugin_done") == []


def test_nothing_a_plugin_prints_reaches_the_terminal_or_the_log(
        env, monkeypatch, tmp_path, capfd, install_client_plugin):
    monkeypatch.setenv("LOOM_HOME", str(env.home))
    install_client_plugin("loud", """
        import sys

        def register(loom):
            def stored(entry):
                print("LOUD-STDOUT-SENTINEL")
                print("LOUD-STDERR-SENTINEL", file=sys.stderr)
                raise RuntimeError("LOUD-RAISE-SENTINEL")
            loom.on_entry_stored(stored)
    """)
    _configure(env, tmp_path)

    rc = cli.main(["process"])

    out, err = capfd.readouterr()
    assert rc == 0, out + err
    log = (env.home / "log" / "processor.log").read_text(encoding="utf-8")
    for sentinel in ("LOUD-STDOUT-SENTINEL", "LOUD-STDERR-SENTINEL", "LOUD-RAISE-SENTINEL"):
        assert sentinel not in out + err
        assert sentinel not in log


def test_the_documented_imports_are_the_classes_a_plugin_meets(
        env, run_cli, tmp_path, plugin_out, install_client_plugin):
    install_client_plugin("importer", """
        import os, pathlib
        from loom.plugins import ModelCallFailed, StoredEntry

        def register(loom):
            def stored(entry):
                out = pathlib.Path(os.environ["PLUGIN_OUT"])
                (out / "isinstance").write_text(str(isinstance(entry, StoredEntry)))
                try:
                    loom.run_model("FAILING PROMPT", "x")
                except ModelCallFailed as exc:
                    (out / "caught").write_text(str(exc))
            loom.on_entry_stored(stored)
    """)
    _configure(env, tmp_path)

    rc, output = run_cli("process")

    assert rc == 0, output
    assert (plugin_out / "isinstance").read_text() == "True"
    assert (plugin_out / "caught").read_text() == "exit=3"
    assert [line for line in _log_lines(env, "plugin_done") if "name=importer" in line]


def _gone(pid: int, seconds: float = 10) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and Path(f"/proc/{pid}").exists():
        time.sleep(0.1)
    return not Path(f"/proc/{pid}").exists()


def test_what_a_plugin_starts_does_not_outlive_its_run(env, run_cli, tmp_path, plugin_out,
                                                      install_client_plugin):
    install_client_plugin("leaver", """
        import os, pathlib, subprocess

        def register(loom):
            def stored(entry):
                child = subprocess.Popen(["sleep", "120"])
                (pathlib.Path(os.environ["PLUGIN_OUT"]) / "pid").write_text(str(child.pid))
            loom.on_entry_stored(stored)
    """)
    _configure(env, tmp_path)

    rc, output = run_cli("process")

    assert rc == 0, output
    assert [line for line in _log_lines(env, "plugin_done") if "name=leaver" in line]
    assert _gone(int((plugin_out / "pid").read_text())), "a background helper survived"


def test_a_plugin_does_not_outlive_a_killed_pass(env, tmp_path, plugin_out,
                                                 install_client_plugin):
    install_client_plugin("sleeper", """
        import os, pathlib, subprocess, time

        def register(loom):
            def stored(entry):
                child = subprocess.Popen(["sleep", "120"])
                out = pathlib.Path(os.environ["PLUGIN_OUT"])
                (out / "pids").write_text(f"{os.getpid()} {child.pid}")
                time.sleep(120)
            loom.on_entry_stored(stored)
    """)
    _configure(env, tmp_path)
    pass_ = subprocess.Popen(
        [sys.executable, "-c", "import sys; from loom.cli import main; "
                               "sys.exit(main(['process']))"],
        env={**os.environ, "LOOM_HOME": str(env.home)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not (plugin_out / "pids").exists():
            time.sleep(0.1)
        assert (plugin_out / "pids").exists(), "the plugin never started"
        time.sleep(0.2)
        pass_.send_signal(signal.SIGKILL)
        pass_.wait(timeout=10)
        plugin_pid, helper_pid = (int(x) for x in (plugin_out / "pids").read_text().split())
        assert _gone(plugin_pid), "the plugin survived the pass that started it"
        assert _gone(helper_pid), "the plugin's helper survived the pass that started it"
    finally:
        if pass_.poll() is None:
            pass_.kill()


def test_a_pass_never_waits_for_plugins_and_a_later_pass_runs_what_it_left(
        env, run_cli, tmp_path, plugin_out, install_client_plugin):
    install_client_plugin("recorder", _recorder("recorder"))
    _configure(env, tmp_path)

    with open(env.home / "plugins.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        started = time.monotonic()
        rc, output = run_cli("process")
        assert time.monotonic() - started < 30
        assert rc == 0, output
        _capture_settled(env)
        assert list(plugin_out.iterdir()) == []     # someone else holds the plugins
        spool = env.home / "plugins_spool"
        [spooled] = list(spool.iterdir())
        assert spool.stat().st_mode & 0o777 == 0o700
        assert spooled.stat().st_mode & 0o777 == 0o600

    rc, output = run_cli("process")                  # nothing new to capture

    assert rc == 0, output
    assert (plugin_out / f"recorder-{ENTRY_ID}.json").exists()
    assert len(_log_lines(env, "plugin_done")) == 1
    assert list((env.home / "plugins_spool").iterdir()) == []


def test_a_module_in_loom_home_cannot_shadow_the_runner(env, run_cli, tmp_path, plugin_out,
                                                       install_client_plugin):
    install_client_plugin("recorder", _recorder("recorder"))
    _configure(env, tmp_path)
    shadow = env.home / "loom"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("raise SystemExit(9)\n", encoding="utf-8")

    rc, output = run_cli("process")

    assert rc == 0, output
    assert (plugin_out / f"recorder-{ENTRY_ID}.json").exists()


def test_an_entry_whose_run_was_cut_short_is_not_run_again(env, run_cli, tmp_path, plugin_out,
                                                          install_client_plugin):
    install_client_plugin("once", """
        import os, pathlib, time

        def register(loom):
            def stored(entry):
                with open(pathlib.Path(os.environ["PLUGIN_OUT"]) / "starts", "a") as fh:
                    fh.write(f"{entry.entry_id}\\n")
                time.sleep(120)
            loom.on_entry_stored(stored)
    """)
    _configure(env, tmp_path)
    pass_ = subprocess.Popen(
        [sys.executable, "-c", "import sys; from loom.cli import main; "
                               "sys.exit(main(['process']))"],
        env={**os.environ, "LOOM_HOME": str(env.home)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not (plugin_out / "starts").exists():
            time.sleep(0.1)
        assert (plugin_out / "starts").exists(), "the plugin never started"
        pass_.send_signal(signal.SIGKILL)
        pass_.wait(timeout=10)
    finally:
        if pass_.poll() is None:
            pass_.kill()
    time.sleep(2)       # the plugin's watchdog notices its pass is gone

    rc, output = run_cli("process")

    assert rc == 0, output
    assert (plugin_out / "starts").read_text().splitlines() == [str(ENTRY_ID)]


def test_a_runner_whose_pass_is_already_gone_kills_itself(tmp_path):
    runner = subprocess.run(
        [sys.executable, "-P", "-m", "loom.plugin_runner", "anything"],
        input="{}", text=True, capture_output=True, timeout=30, start_new_session=True,
        env={**os.environ, "LOOM_HOME": str(tmp_path), "LOOM_PLUGIN_PARENT": "1"})

    assert runner.returncode == -signal.SIGKILL


def test_entries_left_for_plugins_that_are_gone_are_dropped(
        env, run_cli, tmp_path, plugin_out, install_client_plugin):
    install_client_plugin("recorder", _recorder("recorder"))
    _configure(env, tmp_path)
    with open(env.home / "plugins.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        rc, output = run_cli("process")
        assert rc == 0, output
    assert len(list((env.home / "plugins_spool").iterdir())) == 1
    shutil.rmtree(tmp_path / "client_site" / "recorder_plugin-0.1.dist-info")
    importlib.invalidate_caches()

    rc, output = run_cli("process")

    assert rc == 0, output
    assert list((env.home / "plugins_spool").iterdir()) == []
    assert list(plugin_out.iterdir()) == []
    [dropped] = _log_lines(env, "plugin_spool_dropped")
    assert dropped.endswith("entries=1")


def test_a_plugin_list_that_cannot_be_read_keeps_the_spool(
        env, run_cli, tmp_path, plugin_out, install_client_plugin):
    install_client_plugin("recorder", _recorder("recorder"))
    _configure(env, tmp_path)
    with open(env.home / "plugins.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        rc, output = run_cli("process")
        assert rc == 0, output
    broken = tmp_path / "client_site" / "broken_plugin-0.1.dist-info"
    broken.mkdir()
    (broken / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: broken-plugin\nVersion: 0.1\n", encoding="utf-8")
    (broken / "entry_points.txt").write_text(
        "[loom.client_plugins]\nthis line declares nothing\n", encoding="utf-8")
    importlib.invalidate_caches()

    rc, output = run_cli("process")

    assert rc == 0, output
    assert len(list((env.home / "plugins_spool").iterdir())) == 1
    assert _log_lines(env, "plugin_spool_dropped") == []
    assert [line for line in _log_lines(env, "plugin_failed") if "discovery error=" in line]

    shutil.rmtree(broken)
    importlib.invalidate_caches()
    rc, output = run_cli("process")

    assert rc == 0, output
    assert (plugin_out / f"recorder-{ENTRY_ID}.json").exists()
