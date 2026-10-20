"""Tests for `loom push-quarantine` (DESIGN.md §7, quarantine row of the §4.2 error matrix).

Rescans the payload (possibly hand-edited): if it is still dirty it refuses (I4) unless
--force; clean/--force → POST and delete the file (201 or 409).
"""
from __future__ import annotations

import json
import shutil

import pytest

from loom import cli

SECRET_MD = "## Decisiones\n- key filtrada: sk-test1234567890abcdef en el resumen"
CLEAN_MD = "## Decisiones\n- **Usar SQLite** — equipo pequeño. Estado: vigente."


def _setup(env, monkeypatch):
    env.config("claude")
    monkeypatch.setenv("LOOM_HOME", str(env.home))


def _quarantine_file(env, name, content_md):
    qdir = env.home / "quarantine"
    qdir.mkdir(parents=True, exist_ok=True)
    record = {
        "quarantined_at": "2026-07-01T00:00:00Z",
        "reason": "secret_scan",
        "findings": [{"kind": "openai_key", "redacted": "sk-t*"}],
        "project_remote": "github.com/org/b",
        "entry": {
            "project_remote": "github.com/org/b", "session_id": "sess-q",
            "covered_from": None, "covered_to": "u1", "content_md": content_md,
            "content_hash": "de" * 32, "model": "sonnet", "prompt_version": "v1",
        },
    }
    path = qdir / f"{name}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_push_dirty_refuses_and_keeps_file(env, monkeypatch, capsys):
    _setup(env, monkeypatch)
    path = _quarantine_file(env, "sess-q-1", SECRET_MD)

    rc = cli.main(["push-quarantine", "sess-q-1"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "openai_key" in err
    assert path.exists()                 # not deleted
    assert env.posted_entries() == []    # nothing left the machine


def test_push_clean_posts_and_deletes_file(env, monkeypatch, capsys):
    _setup(env, monkeypatch)
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json({"id": 9}, status=201)
    path = _quarantine_file(env, "sess-q-2", CLEAN_MD)

    rc = cli.main(["push-quarantine", "sess-q-2"])

    out = capsys.readouterr().out
    assert rc == 0
    assert not path.exists()
    assert len(env.posted_entries()) == 1
    assert "Entry uploaded ✓" in out


def test_push_409_duplicate_deletes_file(env, monkeypatch, capsys):
    _setup(env, monkeypatch)
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"error": "duplicate_entry"}, status=409
    )
    path = _quarantine_file(env, "sess-q-3", CLEAN_MD)

    rc = cli.main(["push-quarantine", "sess-q-3"])

    assert rc == 0
    assert not path.exists()


def test_push_force_overrides_dirty(env, monkeypatch, capsys):
    _setup(env, monkeypatch)
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json({"id": 10}, status=201)
    path = _quarantine_file(env, "sess-q-4", SECRET_MD)

    rc = cli.main(["push-quarantine", "sess-q-4", "--force"])

    assert rc == 0
    assert not path.exists()
    assert len(env.posted_entries()) == 1


def test_push_unknown_target_exits_one(env, monkeypatch, capsys):
    _setup(env, monkeypatch)
    (env.home / "quarantine").mkdir(parents=True, exist_ok=True)

    rc = cli.main(["push-quarantine", "no-such-entry"])

    assert rc == 1
    assert "No quarantined entry found" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# D13 (C6): the items travel re-derived from the EDITED content, never stale   #
# --------------------------------------------------------------------------- #
LEAKED = "sk-test1234567890abcdef"
REDACTED_V3_MD = (
    "---\ntype: decision-log-entry\ntitle: \"Rotar la clave del proveedor\"\n"
    "status: current\n---\n\n"
    "## Context\nIncidente con una clave.\n"
    "## Decisions\n"
    "- [decision] **Rotar la clave del proveedor** — la clave <redactada> se filtró. "
    "Evidence: incident.md.\n"
    "- [task] **Revocar la clave antigua** — Ana, hoy.\n"
)


def _stale_items_record(env, name, prompt_version, content_md):
    path = _quarantine_file(env, name, content_md)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["entry"]["prompt_version"] = prompt_version
    record["entry"]["item_types"] = ["decision", "task"]
    # What the processor put in the payload BEFORE the hand redaction.
    record["entry"]["items"] = [
        {"item_type": "decision", "labelled": True,
         "text_md": f"- [decision] **Rotar la clave del proveedor** — la clave {LEAKED} "
                    "se filtró. Evidence: incident.md."},
        {"item_type": "task", "labelled": True,
         "text_md": "- [task] **Revocar la clave antigua** — Ana, hoy."},
    ]
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_push_rederives_items_from_the_edited_content(env, monkeypatch):
    _setup(env, monkeypatch)
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 11}, status=201)
    _stale_items_record(env, "sess-q-5", "v3", REDACTED_V3_MD)

    assert cli.main(["push-quarantine", "sess-q-5"]) == 0

    [posted] = env.posted_entries()
    assert posted["items"] == [
        {"item_type": "decision", "labelled": True,
         "text_md": "- [decision] **Rotar la clave del proveedor** — la clave "
                    "<redactada> se filtró. Evidence: incident.md."},
        {"item_type": "task", "labelled": True,
         "text_md": "- [task] **Revocar la clave antigua** — Ana, hoy."},
    ]
    assert posted["item_types"] == ["decision", "task"]
    assert LEAKED not in json.dumps(posted), "the removed string must not leave in an item"


def test_push_of_an_unlabelled_entry_sends_no_items(env, monkeypatch):
    _setup(env, monkeypatch)
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"id": 12}, status=201)
    _stale_items_record(env, "sess-q-6", "v1", CLEAN_MD)

    assert cli.main(["push-quarantine", "sess-q-6"]) == 0

    [posted] = env.posted_entries()
    assert "items" not in posted
    assert LEAKED not in json.dumps(posted)


# --- D16: no gitleaks → the rescan cannot run → refuse, even with --force ------------

def _no_gitleaks(tmp_path, monkeypatch):
    safe = tmp_path / "safe_bin"
    safe.mkdir()
    for name in ("git", "python3"):
        target = shutil.which(name)
        if target:
            (safe / name).symlink_to(target)
    monkeypatch.setenv("PATH", str(safe))


@pytest.mark.parametrize("argv_tail", [[], ["--force"]])
@pytest.mark.parametrize("engine", ["gitleaks", "auto", "regex"])
def test_push_without_a_usable_scanner_refuses_and_says_why(env, monkeypatch, capsys, tmp_path,
                                                            engine, argv_tail):
    _setup(env, monkeypatch)
    data = env.config_json()
    data["scanner"] = {"engine": engine, "gitleaks_version": "8.30.1"}
    (env.home / "config.json").write_text(json.dumps(data), encoding="utf-8")
    _no_gitleaks(tmp_path, monkeypatch)
    path = _quarantine_file(env, "sess-q-d16", CLEAN_MD)

    rc = cli.main(["push-quarantine", "sess-q-d16", *argv_tail])

    err = capsys.readouterr().err
    assert rc == 1
    assert path.exists()
    assert env.posted_entries() == []
    assert "NOT uploading" in err
    assert ("regex" if engine == "regex" else "gitleaks") in err
    assert "install.sh" in err  # the remedy, not just the refusal
