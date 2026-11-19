"""D22 — the redacted extract an entry was summarized from, stored next to the entry.

What is asserted, each through the API or the admin CLI (the boundaries the change owns):

- `POST /api/entries` takes an optional `extract`; `GET /api/entries/{id}/extract` gives
  back exactly that text, and the stored bytes are zlib-compressed;
- the extract is never searched: a word only it contains finds nothing;
- a project's `store_extracts` is on by default (also for a project that existed before
  the column), travels in the project listing, and when off the entry is stored and the
  extract is not;
- `admin.py extracts` turns it on and off, and `--purge` deletes what is stored: after it
  neither the database file nor its WAL holds the purged extracts' bytes;
- the bounds: an oversized extract is a 422, a duplicate entry leaves the stored extract
  as it was, an entry without one is a 404 on the read.
"""
import sqlite3
import zlib
from pathlib import Path

import pytest

from server import admin, db, extracts

REMOTE = "github.com/org/b"
EXTRACT = ("USER: the CSV importer emits None for blank cells\n"
           "ASSISTANT: then total() skips None; token was [REDACTED:github-pat]\n")


def _register(client, auth, remote=REMOTE):
    r = client.post("/api/projects", headers=auth, json={"remote_url": f"git@{remote.replace('/', ':', 1)}.git"})
    assert r.status_code in (201, 409)


def _entry(**over):
    base = {
        "project_remote": REMOTE,
        "session_id": "sess-1",
        "covered_from": "uuid-a",
        "covered_to": "uuid-b",
        "content_md": "## Context\ntotal() skips None values",
        "content_hash": "hash-1",
        "model": "sonnet",
        "prompt_version": "v4",
    }
    base.update(over)
    return base


@pytest.fixture
def dev(client, user_factory):
    user = user_factory("dev1")
    _register(client, user["auth"])
    return user


def test_an_entry_posted_with_its_extract_reads_it_back(client, dev):
    posted = client.post("/api/entries", headers=dev["auth"], json=_entry(extract=EXTRACT))

    assert posted.status_code == 201
    assert posted.json()["extract_stored"] is True
    entry_id = posted.json()["id"]
    read = client.get(f"/api/entries/{entry_id}/extract", headers=dev["auth"])
    assert read.status_code == 200
    assert read.json() == {"entry_id": entry_id, "extract": EXTRACT, "chars": len(EXTRACT)}


def test_the_extract_is_stored_compressed_in_its_own_table(client, dev, env):
    entry_id = client.post("/api/entries", headers=dev["auth"],
                           json=_entry(extract=EXTRACT)).json()["id"]

    conn = db.connect(env)
    try:
        row = conn.execute("SELECT body FROM entry_extracts WHERE entry_id = ?",
                           (entry_id,)).fetchone()
        content = conn.execute("SELECT content_md FROM entries WHERE id = ?",
                               (entry_id,)).fetchone()[0]
    finally:
        conn.close()
    assert zlib.decompress(row["body"]).decode("utf-8") == EXTRACT
    assert content == _entry()["content_md"]     # the entry itself is stored as posted


def test_a_word_only_the_extract_holds_finds_nothing(client, dev):
    item = "- [decision] **total() skips None values** — blank cells. Evidence: x."
    content = "## Context\nCSV totals.\n## Decisions\n" + item + "\n"
    entry_id = client.post("/api/entries", headers=dev["auth"], json=_entry(
        content_md=content, prompt_version="v3", extract=EXTRACT.replace("blank", "empty"),
        items=[{"item_type": "decision", "labelled": True, "text_md": item}])).json()["id"]
    pid = client.get("/api/projects", headers=dev["auth"]).json()["projects"][0]["id"]

    def entries(**params):
        r = client.get(f"/api/projects/{pid}/entries", params=params, headers=dev["auth"])
        assert r.status_code == 200
        return r.json()["entries"]

    def matches(q):
        r = client.get(f"/api/projects/{pid}/items",
                       params={"entry_ids": str(entry_id), "q": q}, headers=dev["auth"])
        assert r.status_code == 200
        return [i["match"] for i in r.json()["items"]]

    for word in ("importer", "emits"):           # in the extract only
        assert entries(q=word) == [], word
        assert entries(q=word, fallback="any_term") == [], word
        assert matches(word) == [False], word
    assert len(entries(q="skips")) == 1           # in the summary: found as before
    assert matches("skips") == [True]


def test_an_entry_without_an_extract_is_a_404_on_the_read(client, dev):
    posted = client.post("/api/entries", headers=dev["auth"], json=_entry())

    assert posted.status_code == 201
    assert posted.json()["extract_stored"] is False
    entry_id = posted.json()["id"]
    for path in (f"/api/entries/{entry_id}/extract", "/api/entries/999/extract"):
        r = client.get(path, headers=dev["auth"])
        assert r.status_code == 404
        assert r.json()["error"] == "extract_not_found"


def test_the_read_needs_a_bearer(client, dev):
    entry_id = client.post("/api/entries", headers=dev["auth"],
                           json=_entry(extract=EXTRACT)).json()["id"]

    assert client.get(f"/api/entries/{entry_id}/extract").status_code == 401


def test_an_oversized_extract_is_refused_and_nothing_is_stored(client, dev):
    # DESIGN §6.2: at most 501 000 characters (two halves of 250 000 and the cut line).
    too_long = "x" * 501_001

    r = client.post("/api/entries", headers=dev["auth"], json=_entry(extract=too_long))

    assert r.status_code == 422
    pid = client.get("/api/projects", headers=dev["auth"]).json()["projects"][0]["id"]
    assert client.get(f"/api/projects/{pid}/entries",
                      headers=dev["auth"]).json()["entries"] == []
    at_the_limit = "x" * 501_000
    assert client.post("/api/entries", headers=dev["auth"],
                       json=_entry(extract=at_the_limit)).status_code == 201


def test_a_duplicate_entry_leaves_the_stored_extract_as_it_was(client, dev):
    first = client.post("/api/entries", headers=dev["auth"], json=_entry(extract=EXTRACT))
    again = client.post("/api/entries", headers=dev["auth"],
                        json=_entry(extract="another text"))

    assert again.status_code == 409
    entry_id = first.json()["id"]
    assert client.get(f"/api/entries/{entry_id}/extract",
                      headers=dev["auth"]).json()["extract"] == EXTRACT


# --------------------------------------------------------------------------- #
# The per-project switch                                                       #
# --------------------------------------------------------------------------- #
def test_store_extracts_is_on_by_default_and_travels_in_the_listing(client, dev):
    listed = client.get("/api/projects", headers=dev["auth"]).json()["projects"]
    one = client.get("/api/projects", params={"remote": REMOTE},
                     headers=dev["auth"]).json()["project"]

    assert [p["store_extracts"] for p in listed] == [True]
    assert one["store_extracts"] is True


def test_turned_off_the_entry_is_stored_and_its_extract_is_not(client, dev, env, capsys):
    assert admin.main(["extracts", "--remote", f"git@github.com:org/b.git", "off"]) == 0
    assert "off" in capsys.readouterr().out

    posted = client.post("/api/entries", headers=dev["auth"], json=_entry(extract=EXTRACT))

    assert posted.status_code == 201
    assert posted.json()["extract_stored"] is False
    entry_id = posted.json()["id"]
    assert client.get(f"/api/entries/{entry_id}/extract",
                      headers=dev["auth"]).status_code == 404
    assert client.get("/api/projects", headers=dev["auth"]).json()[
        "projects"][0]["store_extracts"] is False

    assert admin.main(["extracts", "--remote", REMOTE, "on"]) == 0
    again = client.post("/api/entries", headers=dev["auth"],
                        json=_entry(extract=EXTRACT, content_hash="hash-2"))
    assert again.json()["extract_stored"] is True


def test_purge_deletes_the_stored_extracts_of_that_project_only(client, dev, capsys):
    _register(client, dev["auth"], "github.com/org/c")
    kept = client.post("/api/entries", headers=dev["auth"],
                       json=_entry(project_remote="github.com/org/c", extract=EXTRACT,
                                   content_hash="hash-c")).json()["id"]
    purged = client.post("/api/entries", headers=dev["auth"],
                         json=_entry(extract=EXTRACT)).json()["id"]

    assert admin.main(["extracts", "--remote", REMOTE, "off", "--purge"]) == 0

    assert "1 stored extract" in capsys.readouterr().out
    assert client.get(f"/api/entries/{purged}/extract",
                      headers=dev["auth"]).status_code == 404
    flags = {p["canonical_remote"]: p["store_extracts"]
             for p in client.get("/api/projects", headers=dev["auth"]).json()["projects"]}
    assert flags == {REMOTE: False, "github.com/org/c": True}
    assert client.get(f"/api/entries/{kept}/extract",
                      headers=dev["auth"]).status_code == 200


def _file_bytes(db_file):
    """The database file and its WAL, as they are on disk."""
    return b"".join(Path(db_file + suffix).read_bytes() for suffix in ("", "-wal")
                    if Path(db_file + suffix).exists())


def test_after_a_purge_the_database_file_no_longer_holds_the_extract(client, dev, env):
    stored = zlib.compress(EXTRACT.encode("utf-8"), 9)
    client.post("/api/entries", headers=dev["auth"], json=_entry(extract=EXTRACT))
    assert stored in _file_bytes(env)       # the bytes searched for below are the stored ones

    assert admin.main(["extracts", "--remote", REMOTE, "off", "--purge"]) == 0

    assert stored not in _file_bytes(env)


def test_a_purge_leaves_no_older_copy_of_any_purged_extract_in_the_file(client, dev, env):
    """A table that has grown keeps older copies of its first rows in pages it has since
    rewritten, and a long extract spills over several pages: none of it may stay."""
    other = "github.com/org/c"
    _register(client, dev["auth"], other)
    texts = {remote: [f"{remote} session {n}: " + f"turn {n} of the conversation; " * 4
                      for n in range(150)]
             for remote in (REMOTE, other)}
    long_text = "".join(f"[{n:05d}] tool output {n * 7919:x}\n" for n in range(4000))
    for n in range(150):
        for remote in (REMOTE, other):
            posted = client.post("/api/entries", headers=dev["auth"], json=_entry(
                project_remote=remote, extract=texts[remote][n],
                content_hash=f"{remote}-{n}"))
            assert posted.json()["extract_stored"] is True
    client.post("/api/entries", headers=dev["auth"],
                json=_entry(extract=long_text, content_hash="long"))
    long_stored = zlib.compress(long_text.encode("utf-8"), 9)
    assert len(long_stored) > 3 * 4096      # several pages of the file
    # 32 bytes at a time: a page boundary cuts the stored blob, never every piece of it.
    pieces = [long_stored[at:at + 32] for at in range(0, len(long_stored) - 32, 32)]
    purged = [zlib.compress(text.encode("utf-8"), 9) for text in texts[REMOTE]]
    before = _file_bytes(env)
    assert all(blob in before for blob in purged)
    assert sum(piece in before for piece in pieces) > len(pieces) * 0.9

    assert admin.main(["extracts", "--remote", REMOTE, "off", "--purge"]) == 0

    after = _file_bytes(env)
    assert [n for n, blob in enumerate(purged) if blob in after] == []
    assert [n for n, piece in enumerate(pieces) if piece in after] == []
    pid = {p["canonical_remote"]: p["id"] for p in client.get(
        "/api/projects", headers=dev["auth"]).json()["projects"]}[other]
    kept = client.get(f"/api/projects/{pid}/entries", params={"limit": 200},
                      headers=dev["auth"]).json()["entries"]
    assert len(kept) == 150
    found = client.get(f"/api/projects/{pid}/entries", params={"limit": 200, "q": "skips"},
                       headers=dev["auth"]).json()["entries"]
    assert len(found) == 150                # the rewritten file still answers a search
    assert sorted(client.get(f"/api/entries/{e['id']}/extract",
                             headers=dev["auth"]).json()["extract"]
                  for e in kept) == sorted(texts[other])


def test_a_purge_another_connection_holds_back_says_so_and_a_second_run_finishes_it(
        client, dev, env, capsys, monkeypatch):
    monkeypatch.setattr(db, "_BUSY_TIMEOUT_MS", 200)    # how long the purge waits for it
    stored = zlib.compress(EXTRACT.encode("utf-8"), 9)
    entry_id = client.post("/api/entries", headers=dev["auth"],
                           json=_entry(extract=EXTRACT)).json()["id"]
    reader = sqlite3.connect(env)
    try:
        reader.execute("BEGIN")
        assert reader.execute("SELECT count(*) FROM entry_extracts").fetchone() == (1,)

        assert admin.main(["extracts", "--remote", REMOTE, "off", "--purge"]) == 1

        said = capsys.readouterr()
        assert "Deleted 1 stored extract(s)." in said.out
        assert "Run the same command again" in said.err
        assert client.get(f"/api/entries/{entry_id}/extract",
                          headers=dev["auth"]).status_code == 404
        assert stored in _file_bytes(env)   # what the message is about
    finally:
        reader.close()

    assert admin.main(["extracts", "--remote", REMOTE, "off", "--purge"]) == 0

    said = capsys.readouterr()
    assert "Deleted 0 stored extract(s)." in said.out and said.err == ""
    assert stored not in _file_bytes(env)


def test_purge_needs_off(capsys, env):
    with pytest.raises(SystemExit):
        admin.main(["extracts", "--remote", REMOTE, "on", "--purge"])


def test_the_switch_on_an_unknown_project_fails_naming_it(env, capsys):
    assert admin.main(["extracts", "--remote", "github.com/org/ghost", "off"]) == 1
    assert "github.com/org/ghost" in capsys.readouterr().err
    assert admin.main(["extracts", "--remote", "", "off"]) == 1
    assert "not a git remote" in capsys.readouterr().err


def test_every_registration_answer_carries_the_flag(client, dev, capsys):
    again = client.post("/api/projects", headers=dev["auth"],
                        json={"remote_url": "git@github.com:org/b.git"})
    by_path = client.post("/api/projects", headers=dev["auth"],
                          json={"path": "/home/dev/x", "hostname": "wsl", "name": "x"})
    assert admin.main(["extracts", "--remote", REMOTE, "off"]) == 0
    off = client.post("/api/projects", headers=dev["auth"],
                      json={"remote_url": "git@github.com:org/b.git"})

    assert again.status_code == 409
    assert again.json()["project"]["store_extracts"] is True
    assert by_path.status_code == 201 and by_path.json()["store_extracts"] is True
    assert off.json()["project"]["store_extracts"] is False


def test_a_project_from_before_the_column_starts_on(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute("INSERT INTO projects (canonical_remote, name, created_at) "
                 "VALUES ('github.com/org/old', 'old', '2026-01-01T00:00:00Z')")
    conn.commit()
    conn.close()

    conn = db.init_db(path)
    try:
        row = conn.execute("SELECT store_extracts FROM projects").fetchone()
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()
    assert row["store_extracts"] == 1
    assert "entry_extracts" in tables


def test_the_entry_and_its_extract_are_stored_together_or_not_at_all(env, dev):
    """I6: an entry committed without its extract would answer the retry `409
    duplicate_entry`, which the processor reads as stored — the extract lost for good."""
    from fastapi.testclient import TestClient
    from server.app import app

    conn = db.connect(env)
    conn.execute("CREATE TRIGGER d22_abort BEFORE INSERT ON entry_extracts "
                 "BEGIN SELECT RAISE(ABORT, 'd22 test abort'); END")
    conn.commit()
    conn.close()
    with TestClient(app, raise_server_exceptions=False) as raw:
        failed = raw.post("/api/entries", headers=dev["auth"], json=_entry(extract=EXTRACT))
        listed = raw.get("/api/projects/1/entries", headers=dev["auth"]).json()["entries"]
        conn = db.connect(env)
        conn.execute("DROP TRIGGER d22_abort")
        conn.commit()
        conn.close()
        retried = raw.post("/api/entries", headers=dev["auth"], json=_entry(extract=EXTRACT))

    assert failed.status_code == 500, failed.text
    assert listed == []
    assert retried.status_code == 201
    assert retried.json()["extract_stored"] is True
