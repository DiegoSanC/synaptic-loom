"""RT.1.4: a word search that finds nothing with every word may fall back to any of them.

The fallback is asked for, never imposed: a request carries ``fallback=any_term`` (the
current ``loom log --grep`` sends it), and a request without it is answered exactly as
before RT.1.4 — which is what the web UI, older clients and curation all send.

Asked for, `/entries?q=` first answers the query as written: every word required,
quoted (RT.1.2). When no entry but a digest holds them all, it answers again with ANY of
the words, in BM25 order, and each hit of that answer carries ``match_mode: any_term``.
`/items?q=` does the same per entry: an entry none of whose items passes every filter has
its items of the requested type matched against any of the words, and each item matched
that way carries ``match_mode: any_term``. An expression (quotes, ``*``, parentheses,
AND/OR/NOT/NEAR) says how its terms combine and never falls back; neither does a single
word.

Every entry here is posted through the API with literal items, and every expected id is
read off those literals, never from a search run.
"""
import pytest

from server import db, items

REMOTE = "git@github.com:org/fallback.git"
SIGNED = "- [decision] **Webhook payloads are signed with HMAC**"
DEADLINE = "- [fact] **The receiver deadline is ten seconds**"
PDF = "- [method] **Invoices render through WeasyPrint**"
BOTH = "- [fact] **A webhook deadline miss pages the on-call**"
DIGEST = ("---\ntype: digest\ntitle: \"Digest\"\n---\n## Current state\n"
          "Webhook delivery has a receiver deadline.\n")
FALLBACK = {"fallback": "any_term"}


@pytest.fixture
def seed(client, user_factory):
    """``post(*items) -> entry id`` into one project; returns (auth, project, post).

    A bullet becomes an item; a string that is not a bullet is posted as the whole
    content, without items (a digest)."""
    user = user_factory()
    project = client.post("/api/projects", json={"remote_url": REMOTE},
                          headers=user["auth"]).json()["id"]
    posted = []

    def post(*bullets: str) -> int:
        n = len(posted)
        itemized = all(b.startswith("- [") for b in bullets)
        content = ("## Context\nPlatform work.\n## Decisions\n" + "\n".join(bullets) + "\n"
                   if itemized else bullets[0])
        payload = {
            "project_remote": REMOTE, "session_id": f"s-{n}", "covered_from": None,
            "covered_to": f"u-{n}", "content_md": content, "content_hash": f"{n:064x}",
            "model": "sonnet", "prompt_version": "v4",
        }
        if itemized:
            payload["items"] = [{"item_type": b.split("[")[1].split("]")[0],
                                 "labelled": True, "text_md": b} for b in bullets]
        resp = client.post("/api/entries", headers=user["auth"], json=payload)
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
# Asked for, or not                                                            #
# --------------------------------------------------------------------------- #
def test_without_the_parameter_entries_answer_exactly_as_before(client, seed):
    auth, project, post = seed
    post(SIGNED), post(DEADLINE)
    assert _entries(client, auth, project, q="webhook deadline") == {
        "entries": [], "has_more": False}


def test_without_the_parameter_items_answer_exactly_as_before(client, seed):
    auth, project, post = seed
    split = post(SIGNED, DEADLINE)
    found = _items(client, auth, project, [split], q="webhook deadline")
    assert [found[(split, n)]["match"] for n in (1, 2)] == [False, False]
    assert all("match_mode" not in item and "snippet" not in item for item in found.values())


def test_the_item_search_the_web_ui_uses_never_falls_back(client, seed, env):
    """`ui_queries.current_view` calls `items_for_entries` over every entry of the
    project; a default call must stay the strict search."""
    auth, project, post = seed
    both, signed = post(BOTH), post(SIGNED)
    conn = db.connect(env)
    try:
        found = items.items_for_entries(conn, project, [both, signed], q="webhook deadline")
    finally:
        conn.close()
    assert [(i["entry_id"], i["match"]) for i in found] == [(both, True), (signed, False)]
    assert all("match_mode" not in i for i in found)


@pytest.mark.parametrize("route", ["entries", "items"])
def test_an_unknown_fallback_is_a_422(client, seed, route):
    auth, project, post = seed
    entry = post(SIGNED)
    resp = client.get(f"/api/projects/{project}/{route}", headers=auth,
                      params={"q": "webhook deadline", "entry_ids": str(entry),
                              "fallback": "everything"})
    assert resp.status_code == 422 and resp.json()["error"] == "invalid_fallback"


# --------------------------------------------------------------------------- #
# /entries                                                                     #
# --------------------------------------------------------------------------- #
def test_no_entry_holds_every_word_so_entries_holding_any_answer(client, seed):
    auth, project, post = seed
    signed, deadline, _pdf = post(SIGNED), post(DEADLINE), post(PDF)
    body = _entries(client, auth, project, q="webhook deadline", **FALLBACK)
    assert {h["match_mode"] for h in body["entries"]} == {"any_term"}
    assert sorted(h["id"] for h in body["entries"]) == sorted([signed, deadline])
    assert all("**" in h["snippet"] for h in body["entries"])


def test_an_entry_holding_every_word_keeps_the_answer_to_itself(client, seed):
    auth, project, post = seed
    post(SIGNED), post(DEADLINE)
    both = post(BOTH)
    body = _entries(client, auth, project, q="webhook deadline", **FALLBACK)
    assert all("match_mode" not in h for h in body["entries"])
    assert [h["id"] for h in body["entries"]] == [both]


def test_a_digest_holding_every_word_does_not_stop_the_fallback(client, seed):
    """`loom log` never shows a digest that merely matched (only the latest one), so an
    answer of digests alone would leave the reader with nothing."""
    auth, project, post = seed
    digest, signed, deadline = post(DIGEST), post(SIGNED), post(DEADLINE)
    body = _entries(client, auth, project, q="webhook deadline", **FALLBACK)
    assert {h["match_mode"] for h in body["entries"]} == {"any_term"}
    assert sorted(h["id"] for h in body["entries"]) == sorted([digest, signed, deadline])


def test_a_digest_beside_an_entry_holding_every_word_changes_nothing(client, seed):
    auth, project, post = seed
    digest, both = post(DIGEST), post(BOTH)
    post(SIGNED)
    body = _entries(client, auth, project, q="webhook deadline", **FALLBACK)
    assert all("match_mode" not in h for h in body["entries"])
    assert sorted(h["id"] for h in body["entries"]) == sorted([digest, both])


def test_the_fallback_answers_in_bm25_order(client, seed):
    auth, project, post = seed
    post(SIGNED), post(DEADLINE)
    body = _entries(client, auth, project, q="webhook deadline seconds", **FALLBACK)
    ranks = [h["rank"] for h in body["entries"]]
    assert len(ranks) == 2 and ranks == sorted(ranks)


@pytest.mark.parametrize("q", ['"webhook deadline"', "webhook AND deadline",
                               "webhook NEAR deadline", "(webhook deadline)",
                               "zzqqxwyy"])
def test_an_expression_or_a_single_word_never_falls_back(client, seed, q):
    auth, project, post = seed
    post(SIGNED), post(DEADLINE)
    body = _entries(client, auth, project, q=q, **FALLBACK)
    assert body == {"entries": [], "has_more": False}


def test_the_fallback_finding_nothing_is_the_queries_own_empty_answer(client, seed):
    auth, project, post = seed
    post(PDF)
    body = _entries(client, auth, project, q="webhook deadline", **FALLBACK)
    assert body == {"entries": [], "has_more": False}


def test_a_listing_without_q_has_no_match_mode(client, seed):
    auth, project, post = seed
    post(SIGNED)
    body = _entries(client, auth, project, **FALLBACK)
    assert len(body["entries"]) == 1 and "match_mode" not in body["entries"][0]


def test_the_fallback_keeps_the_type_filter(client, seed):
    auth, project, post = seed
    _signed, deadline = post(SIGNED), post(DEADLINE)
    body = _entries(client, auth, project, q="webhook deadline", type="fact", **FALLBACK)
    assert {h["match_mode"] for h in body["entries"]} == {"any_term"}
    assert [h["id"] for h in body["entries"]] == [deadline]


def test_the_fallback_keeps_the_since_filter(client, seed):
    auth, project, post = seed
    post(SIGNED), post(DEADLINE)
    body = _entries(client, auth, project, q="webhook deadline", since="2999-01-01",
                    **FALLBACK)
    assert body == {"entries": [], "has_more": False}


def test_the_fallback_keeps_the_limit_and_says_there_is_more(client, seed):
    auth, project, post = seed
    post(SIGNED), post(DEADLINE)
    body = _entries(client, auth, project, q="webhook deadline", limit=1, **FALLBACK)
    assert {h["match_mode"] for h in body["entries"]} == {"any_term"}
    assert len(body["entries"]) == 1 and body["has_more"] is True


def test_a_malformed_expression_is_still_a_422(client, seed):
    auth, project, post = seed
    post(SIGNED)
    resp = client.get(f"/api/projects/{project}/entries", headers=auth,
                      params={"q": '"unbalanced', **FALLBACK})
    assert resp.status_code == 422 and resp.json()["error"] == "invalid_query"


def test_a_read_answered_by_the_fallback_is_one_recall_event(client, seed, env):
    auth, project, post = seed
    post(SIGNED), post(DEADLINE), post(PDF)
    resp = client.get(f"/api/projects/{project}/entries",
                      params={"q": "webhook deadline", **FALLBACK},
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
    assert [h["id"] for h in _entries(client, auth, project, q="webhook deadline",
                                      **FALLBACK)["entries"]] == [split]
    found = _items(client, auth, project, [split], q="webhook deadline", **FALLBACK)
    assert [found[(split, n)]["match"] for n in (1, 2, 3)] == [True, True, False]
    assert [found[(split, n)].get("match_mode") for n in (1, 2, 3)] == [
        "any_term", "any_term", None]
    assert "**" in found[(split, 1)]["snippet"] and "**" in found[(split, 2)]["snippet"]
    assert "snippet" not in found[(split, 3)]


def test_an_item_holding_every_word_keeps_the_entrys_answer_to_itself(client, seed):
    auth, project, post = seed
    entry = post(BOTH, SIGNED)
    found = _items(client, auth, project, [entry], q="webhook deadline", **FALLBACK)
    assert [found[(entry, n)]["match"] for n in (1, 2)] == [True, False]
    assert [found[(entry, n)].get("match_mode") for n in (1, 2)] == [None, None]


def test_each_entry_of_the_request_falls_back_on_its_own(client, seed):
    auth, project, post = seed
    whole, split = post(BOTH, SIGNED), post(SIGNED, DEADLINE)
    found = _items(client, auth, project, [whole, split], q="webhook deadline", **FALLBACK)
    assert [found[(whole, n)]["match"] for n in (1, 2)] == [True, False]
    assert [found[(split, n)]["match"] for n in (1, 2)] == [True, True]
    assert [found[(split, n)].get("match_mode") for n in (1, 2)] == ["any_term"] * 2


def test_the_item_fallback_keeps_the_type_filter(client, seed):
    auth, project, post = seed
    split = post(SIGNED, DEADLINE)
    found = _items(client, auth, project, [split], q="webhook deadline", type="fact",
                   **FALLBACK)
    assert [found[(split, n)]["match"] for n in (1, 2)] == [False, True]
    assert found[(split, 2)]["match_mode"] == "any_term"
    assert "match_mode" not in found[(split, 1)] and "snippet" not in found[(split, 1)], (
        "an item of another type is not searched loosely at all")


def test_an_entry_whose_only_match_is_of_another_type_falls_back(client, seed):
    """The type decides whether an entry's own items answered: an item that holds every
    word but has another label does not, so the entry's items of that label are matched
    against any of the words."""
    auth, project, post = seed
    entry = post(BOTH, SIGNED)
    found = _items(client, auth, project, [entry], q="webhook deadline", type="decision",
                   **FALLBACK)
    assert [found[(entry, n)]["match"] for n in (1, 2)] == [False, True]
    assert found[(entry, 2)]["match_mode"] == "any_term"
    assert "match_mode" not in found[(entry, 1)]


@pytest.mark.parametrize("q", ['"webhook deadline"', "webhook AND deadline", "zzqqxwyy"])
def test_items_never_fall_back_for_an_expression_or_a_single_word(client, seed, q):
    auth, project, post = seed
    split = post(SIGNED, DEADLINE)
    found = _items(client, auth, project, [split], q=q, **FALLBACK)
    assert [found[(split, n)]["match"] for n in (1, 2)] == [False, False]
    assert all("match_mode" not in item for item in found.values())


def test_items_without_q_have_no_match_mode(client, seed):
    auth, project, post = seed
    split = post(SIGNED, DEADLINE)
    found = _items(client, auth, project, [split], **FALLBACK)
    assert all(item["match"] and "match_mode" not in item for item in found.values())
