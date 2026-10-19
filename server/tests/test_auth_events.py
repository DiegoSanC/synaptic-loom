"""`auth_events` (DESIGN §6.4, D20): the table, its event vocabulary, retention."""
import hashlib
import hmac
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from server import auth, auth_events, db

# DESIGN §6.4, "Events (event / target_type)", as literals.
VOCABULARY = {
    "ui_signin": "user", "ui_signout": "user", "setup_code_issued": "user",
    "user_invited": "user", "user_deactivated": "user", "promoted": "user",
    "demoted": "user", "device_revoked": "device",
}


def _user(env, email="ana@example.test") -> int:
    conn = db.connect(env)
    try:
        cur = conn.execute(
            "INSERT INTO users (name, email, token_hash, active, created_at) VALUES (?, ?, ?, 1, ?)",
            ("ana", email, db.hash_token(email), db.iso_now()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _events(env) -> list[tuple]:
    conn = sqlite3.connect(env)
    try:
        return conn.execute("SELECT actor_user_id, event, target_type, target_id, client_ip "
                            "FROM auth_events ORDER BY id").fetchall()
    finally:
        conn.close()


def _insert_at(env, *timestamps):
    conn = sqlite3.connect(env)
    try:
        conn.executemany("INSERT INTO auth_events (ts, event, target_type, target_id) "
                         "VALUES (?, 'promoted', 'user', 1)", [(ts,) for ts in timestamps])
        conn.commit()
    finally:
        conn.close()


def _timestamps(env) -> list[str]:
    conn = sqlite3.connect(env)
    try:
        return [r[0] for r in conn.execute("SELECT ts FROM auth_events ORDER BY ts")]
    finally:
        conn.close()


def test_the_table_and_index_are_section_6_4s(env):
    conn = sqlite3.connect(env)
    try:
        columns = [r[1:6] for r in conn.execute("PRAGMA table_info(auth_events)")]
        foreign = [(r[2], r[3], r[4]) for r in conn.execute("PRAGMA foreign_key_list(auth_events)")]
        index = [r[2] for r in conn.execute("PRAGMA index_info(idx_auth_events_target)")]
    finally:
        conn.close()
    # (name, type, notnull, dflt_value, pk)
    assert columns == [
        ("id", "INTEGER", 0, None, 1),
        ("ts", "TEXT", 1, None, 0),
        ("actor_user_id", "INTEGER", 0, None, 0),
        ("event", "TEXT", 1, None, 0),
        ("target_type", "TEXT", 1, None, 0),
        ("target_id", "INTEGER", 1, None, 0),
        ("client_ip", "TEXT", 0, None, 0),
    ]
    assert foreign == [("users", "actor_user_id", "id")]
    assert index == ["target_type", "target_id", "ts"]


@pytest.mark.parametrize("event,target_type", sorted(VOCABULARY.items()))
def test_record_accepts_every_event_of_the_vocabulary(env, event, target_type):
    actor = _user(env)
    conn = db.connect(env)
    try:
        auth_events.record(conn, actor_user_id=actor, event=event, target_type=target_type,
                           target_id=7, client_ip="192.0.2.1")
    finally:
        conn.close()
    assert _events(env) == [(actor, event, target_type, 7, "192.0.2.1")]


def test_record_stamps_the_time_in_the_schemas_format(env):
    conn = db.connect(env)
    try:
        before = db.iso_now()
        auth_events.record(conn, actor_user_id=None, event="promoted", target_type="user",
                           target_id=1)
        after = db.iso_now()
    finally:
        conn.close()
    (ts,) = _timestamps(env)
    assert before <= ts <= after
    assert datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.parametrize("event,target_type", [
    ("ui_signin_failed", "user"),      # failures stay log lines
    ("PROMOTED", "user"),
    ("promoted", "device"),            # the vocabulary fixes the target type too
    ("device_revoked", "user"),
])
def test_record_refuses_anything_outside_the_vocabulary(env, event, target_type):
    conn = db.connect(env)
    try:
        with pytest.raises(ValueError):
            auth_events.record(conn, actor_user_id=None, event=event, target_type=target_type,
                               target_id=1)
        assert not conn.in_transaction
    finally:
        conn.close()
    assert _events(env) == []


def test_record_commits_what_it_opened(env):
    conn = db.connect(env)
    try:
        auth_events.record(conn, actor_user_id=None, event="demoted", target_type="user",
                           target_id=1)
        assert not conn.in_transaction
    finally:
        conn.close()
    assert _events(env) == [(None, "demoted", "user", 1, None)]


def test_record_never_commits_a_transaction_it_did_not_open(env):
    conn = db.connect(env)
    try:
        conn.execute("BEGIN IMMEDIATE")
        auth_events.record(conn, actor_user_id=None, event="demoted", target_type="user",
                           target_id=1)
        assert conn.in_transaction
        conn.rollback()
    finally:
        conn.close()
    assert _events(env) == []


NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)


def test_prune_deletes_rows_older_than_180_days_by_default(env, monkeypatch):
    monkeypatch.delenv("LOOM_AUTH_EVENTS_RETENTION_DAYS", raising=False)
    # 2026-10-19T12:00:00Z minus 180 days is 2026-04-22T12:00:00Z.
    _insert_at(env, "2026-01-01T00:00:00Z", "2026-04-22T11:59:59Z",
               "2026-04-22T12:00:00Z", "2026-10-19T11:00:00Z")
    conn = db.connect(env)
    try:
        assert auth_events.prune(conn, now=NOW) == 2
        assert not conn.in_transaction
    finally:
        conn.close()
    assert _timestamps(env) == ["2026-04-22T12:00:00Z", "2026-10-19T11:00:00Z"]


def test_prune_reads_the_retention_from_the_environment(env, monkeypatch):
    monkeypatch.setenv("LOOM_AUTH_EVENTS_RETENTION_DAYS", "7")
    _insert_at(env, "2026-10-12T11:59:59Z", "2026-10-12T12:00:01Z")
    conn = db.connect(env)
    try:
        assert auth_events.prune(conn, now=NOW) == 1
    finally:
        conn.close()
    assert _timestamps(env) == ["2026-10-12T12:00:01Z"]


# The last three are numbers too large to be a date: §6.4 reads them as the default.
@pytest.mark.parametrize("raw", ["", "0", "-3", "ten", "800000", "999999999", "10" * 20])
def test_an_unusable_retention_falls_back_to_180_days(env, monkeypatch, raw):
    monkeypatch.setenv("LOOM_AUTH_EVENTS_RETENTION_DAYS", raw)
    _insert_at(env, "2026-04-22T11:59:59Z", "2026-04-22T12:00:01Z")
    conn = db.connect(env)
    try:
        auth_events.prune(conn, now=NOW)
    finally:
        conn.close()
    assert _timestamps(env) == ["2026-04-22T12:00:01Z"]


def test_the_server_prunes_at_startup(env, monkeypatch):
    monkeypatch.delenv("LOOM_AUTH_EVENTS_RETENTION_DAYS", raising=False)
    recent = db.iso_now()
    _insert_at(env, "2000-01-01T00:00:00Z", recent)
    from server.app import app

    with TestClient(app):
        pass
    assert _timestamps(env) == [recent]


def test_a_retention_reaching_back_before_the_year_1000_keeps_every_row(env, monkeypatch):
    """700000 days before 2026-10-19 is in the year 110: a date, so nothing is younger than
    it and nothing goes (the cutoff must not be written as "110-…", which sorts last)."""
    monkeypatch.setenv("LOOM_AUTH_EVENTS_RETENTION_DAYS", "700000")
    _insert_at(env, "2000-01-01T00:00:00Z", "2026-10-19T11:00:00Z")
    conn = db.connect(env)
    try:
        assert auth_events.prune(conn, now=NOW) == 0
    finally:
        conn.close()
    assert _timestamps(env) == ["2000-01-01T00:00:00Z", "2026-10-19T11:00:00Z"]


def test_a_retention_too_large_for_a_date_does_not_stop_the_server(env, monkeypatch):
    monkeypatch.setenv("LOOM_AUTH_EVENTS_RETENTION_DAYS", "1" + "0" * 12)
    recent = db.iso_now()
    _insert_at(env, "2000-01-01T00:00:00Z", recent)
    from server.app import app

    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
    assert _timestamps(env) == [recent]


# --------------------------------------------------------------------------- #
# An event commits with the change it records, or neither does                #
# --------------------------------------------------------------------------- #
def _refuse_events(env) -> None:
    """Make every event insert fail, at the database: a change that commits apart from
    its event is then visible."""
    conn = sqlite3.connect(env)
    try:
        conn.execute("CREATE TRIGGER refuse_events BEFORE INSERT ON auth_events "
                     "BEGIN SELECT RAISE(ABORT, 'refused by the test'); END")
        conn.commit()
    finally:
        conn.close()


def _count(env, table: str) -> int:
    conn = sqlite3.connect(env)
    try:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _browser():
    from server.app import app

    return TestClient(app, base_url="https://testserver", raise_server_exceptions=False)


def _sign_in(browser, user) -> None:
    code = browser.post("/api/ui-login", headers=user["auth"]).json()["code"]
    assert browser.post("/ui/session", json={"code": code}).status_code == 204


def test_a_sign_in_whose_event_cannot_be_written_opens_no_session(env, user_factory):
    user, browser = user_factory("ana"), _browser()
    code = browser.post("/api/ui-login", headers=user["auth"]).json()["code"]
    _refuse_events(env)

    assert browser.post("/ui/session", json={"code": code}).status_code == 500

    assert _count(env, "ui_sessions") == 0
    conn = sqlite3.connect(env)
    try:
        assert conn.execute("SELECT redeemed_at FROM ui_login_codes").fetchall() == [(None,)]
    finally:
        conn.close()


def test_a_sign_out_whose_event_cannot_be_written_ends_no_session(env, user_factory):
    user, browser = user_factory("ana"), _browser()
    _sign_in(browser, user)
    csrf = hmac.new(browser.cookies["loom_ui"].encode(), b"csrf", hashlib.sha256).hexdigest()
    _refuse_events(env)

    r = browser.post("/ui/signout", data={"csrf": csrf},
                     headers={"Origin": "https://loom-test.fly.dev"})

    assert r.status_code == 500
    assert _count(env, "ui_sessions") == 1


def test_deactivating_a_user_twice_records_one_event(env):
    """Two concurrent "Deactivate" clicks serialize on the write lock; the second finds
    the user inactive and records nothing."""
    target = _user(env, "gone@example.test")
    audit = {"actor_user_id": None, "client_ip": "192.0.2.9"}
    conn = db.connect(env)
    try:
        assert auth.deactivate(conn, "gone@example.test", audit=audit) is not None
        assert auth.deactivate(conn, "gone@example.test", audit=audit) is not None
    finally:
        conn.close()
    assert _events(env) == [(None, "user_deactivated", "user", target, "192.0.2.9")]
