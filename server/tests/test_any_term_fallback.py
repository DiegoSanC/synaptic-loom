"""RT.1.4: a word search that finds nothing with every word falls back to any of them.

`/entries?q=` answers the query as written: every word required, quoted (RT.1.2). When
no entry holds them all, it answers again with ANY of the words, best first, and each hit
of that answer carries ``match_mode: any_term``. `/items?q=` does the same per entry: an
entry none of whose items holds every word has its items matched against any of them,
and each item matched that way carries ``match_mode: any_term``. Every other answer is
exactly what it was before (no hit carries the field), so a reader that does not know
it loses nothing. An expression (quotes, ``*``, parentheses, AND/OR/NOT/NEAR) says how its
terms combine and never falls back; neither does a single word.

Every entry here is posted through the API with literal items, and every expected id is
read off those literals, never from a search run.
"""
import pytest

from server import db

REMOTE = "git@github.com:org/fallback.git"
SIGNED = "- [decision] **Webhook payloads are signed with HMAC**"
DEADLINE = "- [fact] **The receiver deadline is ten seconds**"
PDF = "- [method] **Invoices render through WeasyPrint**"
BOTH = "- [fact] **A webhook deadline miss pages the on-call**"


@pytest.fixture
def seed(client, user_factory):
    """``post(*items) -> entry id`` into one project; returns (auth, project, post)."""
    user = user_factory()
    project = client.post("/api/projects", json={"remote_url": REMOTE},
                          headers=user["auth"]).json()["id"]
    posted = []

    def post(*bullets: str) -> int:
        n = len(posted)
        content = "## Context\nPlatform work.\n## Decisions\n" + "\n".join(bullets) + "\n"
        resp = client.post("/api/entries", headers=user["auth"], json={
            "project_remote": REMOTE, "session_id": f"s-{n}", "covered_from": None,
            "covered_to": f"u-{n}", "content_md": content, "content_hash": f"{n:064x}",
            "model": "sonnet", "prompt_version": "v4",
            "items": [{"item_type": b.split("[")[1].split("]")[0], "labelled": True,
                       "text_md": b} for b in bullets],
        })
        assert resp.status_code == 201, resp.text
        posted.append(resp.json()["id"])
        return posted[-1]

    return user["auth"], project, post


def _entries(client, auth, project, **params):
    resp = client.get(f"/api/projects/{project}/entries", params=params, headers=auth)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _items(client, auth, project, ids, **params):
    resp = client.get(f"/api/projects/{project}/items", headers=auth,
                      params={"entry_ids": ",".join(map(str, ids)), **params})
    assert resp.status_code == 200, resp.text
    return {(i["entry_id"], i["ordinal"]): i for i in resp.json()["items"]}


# --------------------------------------------------------------------------- #
# /entries                                                                     #
# --------------------------------------------------------------------------- #
def test_no_entry_holds_every_word_so_entries_holding_any_answer(client, seed):
    auth, project, post = seed
    signed, deadline, _pdf = post(SIGNED), post(DEADLINE), post(PDF)
    body = _entries(client, auth, project, q="webhook deadline")
    assert {h["match_mode"] for h in body["entries"]} == {"any_term"}
    assert sorted(h["id"] for h in body["entries"]) == sorted([signed, deadline])
    assert all("**" in h["snippet"] for h in body["entries"])


def test_an_entry_holding_every_word_keeps_the_answer_to_itself(client, seed):
    auth, project, post = seed
    post(SIGNED), post(DEADLINE)
    both = post(BOTH)
    body = _entries(client, auth, project, q="webhook deadline")
    assert all("match_mode" not in h for h in body["entries"])
    assert [h["id"] for h in body["entries"]] == [both]


def test_an_entry_holding_more_of_the_words_ranks_first(client, seed):
    auth, project, post = seed
    signed, deadline = post(SIGNED), post(DEADLINE)
    body = _entries(client, auth, project, q="webhook deadline seconds")
    assert {h["match_mode"] for h in body["entries"]} == {"any_term"}
    assert [h["id"] for h in body["entries"]] == [deadline, signed]


@pytest.mark.parametrize("q", ['"webhook deadline"', "webhook AND deadline",
                               "webhook NEAR deadline", "(webhook deadline)",
                               "zzqqxwyy"])
def test_an_expression_or_a_single_word_never_falls_back(client, seed, q):
    auth, project, post = seed
    post(SIGNED), post(DEADLINE)
    body = _entries(client, auth, project, q=q)
    assert body == {"entries": [], "has_more": False}


def test_the_fallback_finding_nothing_is_the_queries_own_empty_answer(client, seed):
    auth, project, post = seed
    post(PDF)
    body = _entries(client, auth, project, q="webhook deadline")
    assert body == {"entries": [], "has_more": False}


def test_a_listing_without_q_has_no_match_mode(client, seed):
    auth, project, post = seed
    post(SIGNED)
    body = _entries(client, auth, project)
    assert len(body["entries"]) == 1 and "match_mode" not in body["entries"][0]


def test_the_fallback_keeps_the_type_filter(client, seed):
    auth, project, post = seed
    _signed, deadline = post(SIGNED), post(DEADLINE)
    body = _entries(client, auth, project, q="webhook deadline", type="fact")
    assert {h["match_mode"] for h in body["entries"]} == {"any_term"}
    assert [h["id"] for h in body["entries"]] == [deadline]


def test_the_fallback_keeps_the_since_filter(client, seed):
    auth, project, post = seed
    post(SIGNED), post(DEADLINE)
    body = _entries(client, auth, project, q="webhook deadline", since="2999-01-01")
    assert body == {"entries": [], "has_more": False}


def test_the_fallback_keeps_the_limit_and_says_there_is_more(client, seed):
    auth, project, post = seed
    post(SIGNED), post(DEADLINE)
    body = _entries(client, auth, project, q="webhook deadline", limit=1)
    assert {h["match_mode"] for h in body["entries"]} == {"any_term"}
    assert len(body["entries"]) == 1 and body["has_more"] is True


def test_a_malformed_expression_is_still_a_422(client, seed):
    auth, project, post = seed
    post(SIGNED)
    resp = client.get(f"/api/projects/{project}/entries", params={"q": '"unbalanced'},
                      headers=auth)
    assert resp.status_code == 422 and resp.json()["error"] == "invalid_query"


def test_a_read_answered_by_the_fallback_is_one_recall_event(client, seed, env):
    auth, project, post = seed
    post(SIGNED), post(DEADLINE), post(PDF)
    resp = client.get(f"/api/projects/{project}/entries", params={"q": "webhook deadline"},
                      headers={**auth, "X-Loom-Source": "skill"})
    assert resp.status_code == 200
    conn = db.connect(env)
    try:
        rows = conn.execute("SELECT source, query, result_count FROM recall_events").fetchall()
    finally:
        conn.close()
    assert [tuple(r) for r in rows] == [("skill", "webhook deadline", 2)]


# --------------------------------------------------------------------------- #
# /items                                                                       #
# --------------------------------------------------------------------------- #
def test_an_entry_whose_items_split_the_words_shows_the_items_holding_any(client, seed):
    auth, project, post = seed
    split = post(SIGNED, DEADLINE, PDF)
    assert [h["id"] for h in _entries(client, auth, project, q="webhook deadline")
            ["entries"]] == [split]
    found = _items(client, auth, project, [split], q="webhook deadline")
    assert [found[(split, n)]["match"] for n in (1, 2, 3)] == [True, True, False]
    assert [found[(split, n)].get("match_mode") for n in (1, 2, 3)] == [
        "any_term", "any_term", None]
    assert "**" in found[(split, 1)]["snippet"] and "**" in found[(split, 2)]["snippet"]
    assert "snippet" not in found[(split, 3)]


def test_an_item_holding_every_word_keeps_the_entrys_answer_to_itself(client, seed):
    auth, project, post = seed
    entry = post(BOTH, SIGNED)
    found = _items(client, auth, project, [entry], q="webhook deadline")
    assert [found[(entry, n)]["match"] for n in (1, 2)] == [True, False]
    assert [found[(entry, n)].get("match_mode") for n in (1, 2)] == [None, None]


def test_each_entry_of_the_request_falls_back_on_its_own(client, seed):
    auth, project, post = seed
    whole, split = post(BOTH, SIGNED), post(SIGNED, DEADLINE)
    found = _items(client, auth, project, [whole, split], q="webhook deadline")
    assert [found[(whole, n)]["match"] for n in (1, 2)] == [True, False]
    assert [found[(split, n)]["match"] for n in (1, 2)] == [True, True]
    assert [found[(split, n)].get("match_mode") for n in (1, 2)] == ["any_term"] * 2


def test_the_item_fallback_keeps_the_type_filter(client, seed):
    auth, project, post = seed
    split = post(SIGNED, DEADLINE)
    found = _items(client, auth, project, [split], q="webhook deadline", type="fact")
    assert [found[(split, n)]["match"] for n in (1, 2)] == [False, True]
    assert [found[(split, n)].get("match_mode") for n in (1, 2)] == [None, "any_term"], (
        "an item that does not match is not marked as matched any way")


def test_an_entry_whose_only_match_is_of_another_type_falls_back(client, seed):
    """The type decides whether an entry's own items answered: an item that holds every
    word but has another label does not, so the entry's items of that label are matched
    against any of the words."""
    auth, project, post = seed
    entry = post(BOTH, SIGNED)
    found = _items(client, auth, project, [entry], q="webhook deadline", type="decision")
    assert [found[(entry, n)]["match"] for n in (1, 2)] == [False, True]
    assert found[(entry, 2)]["match_mode"] == "any_term"


@pytest.mark.parametrize("q", ['"webhook deadline"', "webhook AND deadline", "zzqqxwyy"])
def test_items_never_fall_back_for_an_expression_or_a_single_word(client, seed, q):
    auth, project, post = seed
    split = post(SIGNED, DEADLINE)
    found = _items(client, auth, project, [split], q=q)
    assert [found[(split, n)]["match"] for n in (1, 2)] == [False, False]
    assert all("match_mode" not in item for item in found.values())


def test_items_without_q_have_no_match_mode(client, seed):
    auth, project, post = seed
    split = post(SIGNED, DEADLINE)
    found = _items(client, auth, project, [split])
    assert all(item["match"] and "match_mode" not in item for item in found.values())
