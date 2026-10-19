"""Silent device-token renewal and loud 401s in the unattended pass (issue #20).

Both unattended entry points (`loom process --quiet`, `python -m loom.processor
--quiet`) run :func:`loom.processor.run_once`; these tests drive it directly against the
mock server. The real-time expiry proof lives in ``client/tests/e2e``.
"""
from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone

from werkzeug.wrappers import Response

from loom import processor
from loom.config import Config

SUMMARY = "## Decision\n- **Keep the flock inside run_once** — it cannot race the kick.\n"
DAY = timedelta(days=1)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _device_config(env, *, issued_ago: timedelta, life: timedelta = 90 * DAY,
                   token: str = "dev-token-old") -> Config:
    """A config carrying a device token issued ``issued_ago`` ago with ``life`` total."""
    env.config(env.make_stub(stdout=SUMMARY))
    path = env.home / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    data.update({
        "token": token,
        "token_issued_at": _iso(now - issued_ago),
        "token_expires_at": _iso(now - issued_ago + life),
    })
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)
    return Config.load(env.home)


def _json(body, status=200):
    return Response(json.dumps(body), status=status, content_type="application/json")


def _requests(env, path):
    return [req for req, _ in env.httpserver.log if req.path == path]


def _actions(report):
    return [o.action for o in report.outcomes]


def _mock_renew(env, *, status=201, calls=None):
    def handler(request):
        if calls is not None:
            calls.append(request.headers.get("Authorization"))
        if status != 201:
            return _json({"error": "server_error"}, status)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        return _json({"token": "dev-token-new", "issued_at": _iso(now),
                      "expires_at": _iso(now + 90 * DAY), "user": {"id": 1, "name": "dev2"}}, 201)

    env.httpserver.expect_request("/api/device-tokens/renew", method="POST").respond_with_handler(handler)


# --------------------------------------------------------------------------- #
# Renewal                                                                      #
# --------------------------------------------------------------------------- #
def test_renews_when_less_than_a_quarter_of_the_life_remains(env):
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json({"id": 1}, status=201)
    calls = []
    _mock_renew(env, calls=calls)
    cfg = _device_config(env, issued_ago=70 * DAY)  # 20 of 90 days left → below 25 %
    env.drop_marker("sess-renew")

    report = processor.run_once(cfg)

    assert _actions(report) == ["posted"]
    assert calls == ["Bearer dev-token-old"]  # renewed with the token it still had
    # The successor is persisted, 0600, with its lifetime, and the rest of the config kept.
    path = env.home / "config.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["token"] == "dev-token-new"
    assert data["token_expires_at"] > data["token_issued_at"]
    assert data["summarizer"]["prompt_version"] == "v1"
    assert data["sweep"]["transcripts_root"] == str(env.home / "sweep_transcripts")
    # …and the SAME pass already used it: the entry went up under the new bearer.
    posts = _requests(env, "/api/entries")
    assert [r.headers["Authorization"] for r in posts] == ["Bearer dev-token-new"]
    assert cfg.token == "dev-token-new"


def test_no_renewal_while_plenty_of_life_remains(env):
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json({"id": 1}, status=201)
    calls = []
    _mock_renew(env, calls=calls)
    cfg = _device_config(env, issued_ago=10 * DAY)
    env.drop_marker("sess-fresh")

    report = processor.run_once(cfg)

    assert _actions(report) == ["posted"]
    assert calls == []
    assert json.loads((env.home / "config.json").read_text())["token"] == "dev-token-old"


def test_a_legacy_permanent_token_never_renews(env):
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json({"id": 1}, status=201)
    calls = []
    _mock_renew(env, calls=calls)
    cfg = env.config(env.make_stub(stdout=SUMMARY))  # token "test-token", no lifetime keys
    env.drop_marker("sess-legacy")

    report = processor.run_once(cfg)

    assert _actions(report) == ["posted"]
    assert calls == []


def test_a_failed_renewal_does_not_stop_the_pass(env):
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json({"id": 1}, status=201)
    _mock_renew(env, status=500)
    cfg = _device_config(env, issued_ago=70 * DAY)
    env.drop_marker("sess-renew-500")

    report = processor.run_once(cfg)

    assert _actions(report) == ["posted"]
    posts = _requests(env, "/api/entries")
    assert [r.headers["Authorization"] for r in posts] == ["Bearer dev-token-old"]
    assert json.loads((env.home / "config.json").read_text())["token"] == "dev-token-old"
    log = (env.home / "log" / "processor.log").read_text(encoding="utf-8")
    assert "token_renewal_failed" in log
    assert not (env.home / "auth_rejected.json").exists()


# D19: `cfg` is loaded before the processor takes its lock (`loom process`, `python -m
# loom.processor`). Another pass can renew and release the lock in between, so the pass
# must renew with the token `config.json` holds once the lock is ITS, not the one it
# loaded: re-issuing over a fresh successor would revoke it (renew re-issues, D19 B1b).
def _another_pass_rewrites_the_token(env, *, token: str, issued_ago: timedelta) -> None:
    path = env.home / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    data.update({"token": token, "token_issued_at": _iso(now - issued_ago),
                 "token_expires_at": _iso(now - issued_ago + 90 * DAY)})
    path.write_text(json.dumps(data), encoding="utf-8")


def test_a_successor_written_after_cfg_was_loaded_is_not_renewed_again(env):
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json({"id": 1}, status=201)
    calls = []
    _mock_renew(env, calls=calls)
    cfg = _device_config(env, issued_ago=70 * DAY)  # loaded while renewal was due
    _another_pass_rewrites_the_token(env, token="dev-token-fresh", issued_ago=timedelta(0))
    env.drop_marker("sess-after-other-pass")

    report = processor.run_once(cfg)

    assert _actions(report) == ["posted"]
    assert calls == []  # the fresh successor is not re-issued over
    assert json.loads((env.home / "config.json").read_text())["token"] == "dev-token-fresh"
    posts = _requests(env, "/api/entries")
    assert [r.headers["Authorization"] for r in posts] == ["Bearer dev-token-fresh"]


def test_the_renewal_uses_the_token_in_config_as_re_read_after_the_lock(env):
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json({"id": 1}, status=201)
    calls = []
    _mock_renew(env, calls=calls)
    cfg = _device_config(env, issued_ago=80 * DAY, token="dev-token-loaded")
    _another_pass_rewrites_the_token(env, token="dev-token-on-disk", issued_ago=70 * DAY)
    env.drop_marker("sess-reread")

    report = processor.run_once(cfg)

    assert _actions(report) == ["posted"]
    assert calls == ["Bearer dev-token-on-disk"]
    assert json.loads((env.home / "config.json").read_text())["token"] == "dev-token-new"
    posts = _requests(env, "/api/entries")
    assert [r.headers["Authorization"] for r in posts] == ["Bearer dev-token-new"]


# --------------------------------------------------------------------------- #
# Loud 401                                                                     #
# --------------------------------------------------------------------------- #
def _auth_rejected(env) -> dict | None:
    path = env.home / "auth_rejected.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def test_401_on_upload_is_recorded_and_cleared_by_the_next_success(env):
    env.register_projects()
    calls = {"n": 0}

    def flaky(_request):
        calls["n"] += 1
        if calls["n"] == 1:
            return _json({"error": "unauthorized", "detail": "Device token expired"}, 401)
        return _json({"id": 7}, 201)

    env.httpserver.expect_request("/api/entries", method="POST").respond_with_handler(flaky)
    cfg = env.config(env.make_stub(stdout=SUMMARY))
    marker = env.drop_marker("sess-401")

    report1 = processor.run_once(cfg)

    assert _actions(report1) == ["kept_auth_rejected"]
    assert report1.errors == 1
    assert marker.exists()                                   # nothing is lost
    assert env.pointer() == {}
    record = _auth_rejected(env)
    assert record is not None
    assert record["status"] == 401 and "POST /api/entries" in record["detail"]
    assert record["at"].endswith("Z")
    log = (env.home / "log" / "processor.log").read_text(encoding="utf-8")
    assert "kept_auth_rejected" in log

    report2 = processor.run_once(cfg)

    assert _actions(report2) == ["posted"]
    assert not marker.exists()
    assert _auth_rejected(env) is None                       # cleared by a real 2xx


def test_403_on_upload_is_recorded_too(env):
    env.register_projects()
    env.httpserver.expect_request("/api/entries", method="POST").respond_with_json(
        {"error": "forbidden", "detail": "User is deactivated"}, status=403
    )
    cfg = env.config(env.make_stub(stdout=SUMMARY))
    env.drop_marker("sess-403")

    report = processor.run_once(cfg)

    assert _actions(report) == ["kept_auth_rejected"]
    assert _auth_rejected(env)["status"] == 403


def test_401_on_the_registry_refresh_is_recorded(env):
    env.httpserver.expect_request("/api/projects", method="GET").respond_with_json(
        {"error": "unauthorized", "detail": "Invalid token"}, status=401
    )
    cfg = env.config(env.make_stub(stdout=SUMMARY))
    marker = env.drop_marker("sess-reg-401")

    report = processor.run_once(cfg)

    assert marker.exists()
    assert report.errors == 1
    record = _auth_rejected(env)
    assert record is not None
    assert record["status"] == 401 and "GET /api/projects" in record["detail"]


def test_401_on_renewal_is_recorded(env):
    env.register_projects()
    env.httpserver.expect_request("/api/device-tokens/renew", method="POST").respond_with_json(
        {"error": "unauthorized", "detail": "Device token revoked"}, status=401
    )
    cfg = _device_config(env, issued_ago=70 * DAY)

    processor.run_once(cfg)  # empty queue: the renewal is the only authenticated call

    record = _auth_rejected(env)
    assert record is not None
    assert record["status"] == 401 and "renew" in record["detail"]
    assert json.loads((env.home / "config.json").read_text())["token"] == "dev-token-old"
