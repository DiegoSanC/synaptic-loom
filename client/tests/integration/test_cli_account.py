"""The account rows of `loom status` and `loom doctor` (DESIGN §6.4 "Client", D20 2a).

`loom status` prints `account: <name> (<role>)` and the events of the last 30 days from
`GET /api/me`; `loom doctor` adds an `account` row, ✓ with the role, ⚠ when one of the
last 7 days' events was made by someone else (another user, or `admin.py`: actor null).
A server without `/api/me` (404) leaves both as they were, and neither command fails or
waits long on a server that does not answer.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from werkzeug import Response

from loom import cli

BOGUS_CLAUDE = "/nonexistent/claude-binary"


def _ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event(ts: str, event: str, actor: str | None, target_type: str = "user",
           target_id: int = 4) -> dict:
    return {"ts": ts, "event": event, "actor": actor, "target_type": target_type,
            "target_id": target_id}


def _me(env, *, role: str = "member", events: list[dict] | None = None) -> None:
    env.httpserver.expect_request("/api/me", method="GET").respond_with_json({
        "name": "ana", "email": "ana@example.test", "role": role, "device_id": 9,
        "events": events or []})


def _status(env, monkeypatch, capsys, **config) -> tuple[int, str]:
    env.config(BOGUS_CLAUDE, **config)
    monkeypatch.setenv("LOOM_HOME", str(env.home))
    rc = cli.main(["status"])
    return rc, capsys.readouterr().out


def _doctor(env, monkeypatch, capsys) -> tuple[int, str, list[str]]:
    env.config(BOGUS_CLAUDE)
    monkeypatch.setenv("LOOM_HOME", str(env.home))
    env.httpserver.expect_request("/api/health", method="GET").respond_with_json(
        {"status": "ok", "version": "0.1.0"})
    env.httpserver.expect_request("/api/projects", method="GET").respond_with_json(
        {"projects": []})
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    return rc, out, [line for line in out.splitlines() if "account" in line.lower()]


# --- loom status ------------------------------------------------------------------
def test_status_names_the_account_and_its_events_of_the_last_30_days(env, monkeypatch, capsys):
    recent, older, stale = _ago(days=2), _ago(days=20), _ago(days=31)
    _me(env, role="admin", events=[
        _event(recent, "promoted", None),
        _event(older, "device_revoked", "bob", target_type="device", target_id=12),
        _event(stale, "ui_signin", "ana"),
    ])

    rc, out = _status(env, monkeypatch, capsys)

    assert rc == 0
    lines = out.splitlines()
    assert "account: ana (admin)" in lines
    start = lines.index("account: ana (admin)")
    block = lines[start:lines.index("Pending markers (queue/): 0")]
    assert any(recent in line and "promoted" in line and "admin.py" in line for line in block)
    assert any(older in line and "device_revoked" in line and "device 12" in line
               and "bob" in line for line in block)
    assert stale not in out and "ui_signin" not in out


def test_status_says_when_the_last_30_days_hold_no_event(env, monkeypatch, capsys):
    _me(env, events=[_event(_ago(days=40), "promoted", None)])

    rc, out = _status(env, monkeypatch, capsys)

    assert rc == 0
    assert "account: ana (member)" in out
    assert "no account events in the last 30 days" in out
    assert "promoted" not in out


def test_status_on_a_server_without_api_me_prints_no_account_line(env, monkeypatch, capsys):
    env.httpserver.expect_request("/api/me", method="GET").respond_with_data("", status=404)

    rc, out = _status(env, monkeypatch, capsys)

    assert rc == 0
    assert "account" not in out.lower()


def test_status_survives_a_server_that_is_down(env, monkeypatch, capsys):
    rc, out = _status(env, monkeypatch, capsys, server_url="http://127.0.0.1:9")

    assert rc == 0
    assert "account: unknown (server unreachable)" in out
    assert "Pending markers (queue/): 0" in out


def test_status_survives_a_refused_token(env, monkeypatch, capsys):
    env.httpserver.expect_request("/api/me", method="GET").respond_with_data("", status=401)

    rc, out = _status(env, monkeypatch, capsys)

    assert rc == 0
    assert "account: unknown (GET /api/me returned 401)" in out


def test_status_does_not_wait_on_a_server_that_never_answers(env, monkeypatch, capsys):
    def _hang(request):
        time.sleep(4)
        return Response("{}", status=200, content_type="application/json")

    env.httpserver.expect_request("/api/me", method="GET").respond_with_handler(_hang)

    started = time.monotonic()
    rc, out = _status(env, monkeypatch, capsys)

    assert rc == 0
    assert time.monotonic() - started < 3.5
    assert "account: unknown (server unreachable)" in out


# --- loom doctor ------------------------------------------------------------------
def test_doctor_shows_the_role_with_a_tick(env, monkeypatch, capsys):
    # The user's own recent event, and someone else's from 8 days ago: neither warns.
    _me(env, events=[_event(_ago(days=1), "ui_signin", "ana"),
                     _event(_ago(days=8), "demoted", None)])

    _, _, rows = _doctor(env, monkeypatch, capsys)

    assert rows == ["✓ account: ana (member)"]


def test_doctor_warns_on_an_event_made_by_admin_py_in_the_last_7_days(env, monkeypatch, capsys):
    when = _ago(days=6)
    _me(env, role="admin", events=[_event(when, "promoted", None)])

    _, _, rows = _doctor(env, monkeypatch, capsys)

    assert len(rows) == 1 and rows[0].startswith("⚠ account: ana (admin)"), rows
    assert "promoted" in rows[0] and "admin.py" in rows[0] and when in rows[0]


def test_doctor_warns_on_a_machine_another_user_revoked(env, monkeypatch, capsys):
    _me(env, events=[_event(_ago(hours=3), "device_revoked", "bob", "device", 12)])

    rc, _, rows = _doctor(env, monkeypatch, capsys)

    assert len(rows) == 1 and rows[0].startswith("⚠ account: ana (member)"), rows
    assert "device_revoked" in rows[0] and "bob" in rows[0]


def test_doctor_on_a_server_without_api_me_has_no_account_row(env, monkeypatch, capsys):
    env.httpserver.expect_request("/api/me", method="GET").respond_with_data("", status=404)

    _, out, rows = _doctor(env, monkeypatch, capsys)

    assert rows == []
    assert "✓ token valid (GET /api/projects → 200)" in out


def test_doctor_with_the_server_down_asks_nothing_more(env, monkeypatch, capsys):
    env.config(BOGUS_CLAUDE, server_url="http://127.0.0.1:9")
    monkeypatch.setenv("LOOM_HOME", str(env.home))

    rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    assert rc == 1
    assert "server /api/health unreachable" in out
    assert "account" not in out.lower()
