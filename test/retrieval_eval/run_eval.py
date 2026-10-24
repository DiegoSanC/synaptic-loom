#!/usr/bin/env python3
"""Retrieval benchmark for the logbook search (DESIGN §12.3 RT.1).

How well does ``loom log --grep "<terms>"`` put the item a reader needs in front of them?
A fixed synthetic logbook (``corpus.md``: one made-up project, entries written in
summarizer-v4 form) and a fixed set of judged queries (``queries.json``: the terms an
agent would pass, the need behind them, and the items that answer it — judged by hand
from the corpus, never from a search run) are scored end to end, in process:

1. every entry is posted through the real ``POST /api/entries`` the way the processor
   posts it — OKF frontmatter from ``okf.wrap_entry``, items from
   ``processor.split_items``, uuids for the session and the covered range — with the
   server's clock set to the entry's date, so ``created_at`` is the corpus date;
2. every query runs the real ``loom log`` command with the ``/loom`` skill's flags
   (``loom log ledger --grep "<terms>" --source skill --limit 30``) against that server,
   with a ``config.json`` holding only the server and the token, so every other setting
   is the client's default — the character budget (8000) among them;
3. what the command prints is what the reader is shown: the item ids it prints, in the
   order it prints them, are the ranked list. Fetching, digests, links, the budget and
   the hiding of entries that matched only outside their items are therefore the
   client's own, never a copy that could drift from it.

Each category isolates one kind of miss, and :func:`validate` checks every query against
its category's definition on the corpus text. Words are compared after lowercasing and
dropping accents; "a form of" is a crude test, the same first four letters, so it also
accepts truncations (`postgres` / "PostgreSQL") and the odd unrelated pair
(`timeout` / "timestamp") — a `morphology` query is additionally judged by hand to
differ only by inflection, and a false alarm is answered by rewording the query, never
by loosening the check. "The entry" is everything the search indexes for it:
frontmatter, Context and items — so `to`, `from` and `status` are in every entry, via
`covered_to`, `covered_from` and `status: current`.

- ``exact``      every term is in each relevant item — the control;
- ``morphology`` a term is missing from the relevant item, and each missing one is a
                 form of a word in it (``migration`` / "migrations");
- ``split``      every term is in the relevant entry's items, but no single item holds
                 them all — the entry matches and ``loom log`` hides it;
- ``extra``      a term appears nowhere in the relevant entry (nor any form of it), and
                 every other term is in the relevant item;
- ``synonym``    no term, nor any form of one, is anywhere in the relevant entry
                 (``TTL`` / "expire");
- ``stale``      ``distractors`` are OLDER items saying the same thing with a value since
                 replaced; every term is in both, so ranking alone decides;
- ``durable``    ``distractors`` are NEWER items that mention the same words in passing
                 while the older relevant item still holds — what a boost for newer
                 entries costs;
- ``error``      a literal error message; the relevant item paraphrases it.

Only the current item counts as relevant: an item another one has replaced never does.

Scores, per category and over all queries: ``recall@5`` (the share of a query's relevant
items among the first five shown), ``MRR`` (the mean of 1 / the position of the first
relevant item shown, 0 when none is — what the budget cut off was not shown),
``empty`` (the share of queries shown no item) and ``hidden`` (the share where entries
matched but none of their items did, so ``loom log`` printed its "no item … answers this
search" note instead — a subset of ``empty``).

Unlike ``test/prompt_eval/`` it spends no model calls and is deterministic, so the suite
runs it: ``server/tests/test_retrieval_eval.py`` checks that ``RESULTS.md`` is what the
current search produces. A change to retrieval, server or client side, therefore shows
its effect as a diff of ``RESULTS.md``; regenerate it with

    .venv/bin/python test/retrieval_eval/run_eval.py --out test/retrieval_eval/RESULTS.md

Anything a later change adds to the corpus that a model would write — search keywords
for an item, say — is generated ONCE by the real prompt from ``corpus.md`` alone, never
with ``queries.json`` in view, and committed beside it; writing it by hand would leak the
judgments into the index.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import secrets
import sys
import tempfile
import unicodedata
import uuid
import warnings
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "corpus.md"
QUERIES = HERE / "queries.json"
RESULTS = HERE / "RESULTS.md"

# The closed set a query may name, in the order RESULTS.md reports them.
CATEGORIES = ("exact", "morphology", "split", "extra", "synonym", "stale", "durable", "error")
K = 5
# "A form of": two words sharing their first STEM_PREFIX letters (see the docstring).
STEM_PREFIX = 4
# The `/loom` skill's command, minus the project and the terms.
SKILL_FLAGS = ("--source", "skill", "--limit", "30")
PROMPT_VERSION = "v4"
MODEL = "sonnet"
PROJECT = "ledger"
REMOTE = "git@github.com:example-org/ledger.git"
CANONICAL = "github.com/example-org/ledger"
SERVER_URL = "http://testserver"
_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/example-org/ledger")

_ENTRY_RE = re.compile(r"^<!-- entry (?P<key>e\d{2}) (?P<date>\d{4}-\d{2}-\d{2}) -->$", re.M)
_ANY_MARKER_RE = re.compile(r"^<!--\s*entry\b", re.M)
# A labelled bullet; an "Open questions" bullet is not one, and is not an item.
_LABELLED_BULLET_RE = re.compile(r"^- \[", re.M)
_WORD_RE = re.compile(r"[^\W_]+")
# An item as `loom log` prints it: `<entry id>.<ordinal>`, two spaces, its text. A digest
# is printed whole and could hold such a line, so `loom_log` refuses any output with one;
# the corpus has none.
_SHOWN_ITEM_RE = re.compile(r"^(\d+)\.(\d+)  ", re.M)


@dataclass(frozen=True)
class Entry:
    key: str
    date: str       # YYYY-MM-DD
    summary: str


@dataclass(frozen=True)
class CorpusItem:
    entry: str          # the key of its entry
    date: str
    text: str
    entry_text: str = ""  # everything the search indexes for the entry; "" = its items


@dataclass(frozen=True)
class Query:
    id: str
    category: str
    need: str
    terms: str
    relevant: tuple[str, ...]
    distractors: tuple[str, ...] = ()


@dataclass(frozen=True)
class Outcome:
    query: Query
    ranked: tuple[str, ...]
    position: int | None    # of the first relevant item shown, 1-based
    recall: float
    rr: float
    hidden: bool


@dataclass(frozen=True)
class Report:
    entries: int
    items: int
    outcomes: tuple[Outcome, ...]


class BenchmarkError(Exception):
    """The benchmark's own inputs are wrong, or the server or client answered unexpectedly."""


# --------------------------------------------------------------------------- #
# Inputs                                                                       #
# --------------------------------------------------------------------------- #
def load_corpus(text: str) -> list[Entry]:
    marks = list(_ENTRY_RE.finditer(text))
    entries = []
    for n, mark in enumerate(marks):
        end = marks[n + 1].start() if n + 1 < len(marks) else len(text)
        entries.append(Entry(mark["key"], mark["date"], text[mark.end():end].strip()))
    return entries


def load_queries(path: Path = QUERIES) -> list[Query]:
    return [Query(q["id"], q["category"], q["need"], q["terms"], tuple(q["relevant"]),
                  tuple(q.get("distractors", ())))
            for q in json.loads(path.read_text(encoding="utf-8"))]


def words(text: str) -> set[str]:
    """The words of ``text``, lowercased and without accents (what the validator compares)."""
    folded = unicodedata.normalize("NFKD", text.lower())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return set(_WORD_RE.findall(folded))


def _is_form_of(word: str, vocabulary: set[str]) -> bool:
    if len(word) < STEM_PREFIX:
        return word in vocabulary
    return any(other[:STEM_PREFIX] == word[:STEM_PREFIX] for other in vocabulary)


def _uuid(entry: Entry, role: str) -> str:
    # The processor posts uuids, which index as hex fragments; any readable stand-in
    # ("first", "last", the entry key) would be a word every entry matches.
    return str(uuid.uuid5(_UUID_NAMESPACE, f"{entry.key}/{role}"))


def _payload(entry: Entry) -> dict:
    """The body of ``POST /api/entries`` for ``entry``, built as the processor builds it."""
    from loom import okf, processor

    session, first, last = (_uuid(entry, role) for role in ("session", "first", "last"))
    items = processor.split_items(entry.summary, PROMPT_VERSION)
    item_types = sorted({item.item_type for item in items})
    content_md = okf.wrap_entry(
        entry.summary, canonical_remote=CANONICAL, session_id=session,
        covered_from=first, covered_to=last, model=MODEL, prompt_version=PROMPT_VERSION,
        item_types=item_types, timestamp=f"{entry.date}T10:00:00Z",
    )
    payload = {
        "project_remote": CANONICAL, "session_id": session,
        "covered_from": first, "covered_to": last, "content_md": content_md,
        "content_hash": hashlib.sha256(content_md.encode("utf-8")).hexdigest(),
        "model": MODEL, "prompt_version": PROMPT_VERSION, "item_types": item_types,
    }
    posted = processor.postable_items(items)
    if posted is not None:
        payload["items"] = posted
    return payload


def corpus_items(entries: list[Entry]) -> dict[str, CorpusItem]:
    """``{"e07.2": item}``: every entry's items, cut by the processor's own splitter."""
    from loom import processor

    found = {}
    for entry in entries:
        indexed = _payload(entry)["content_md"]
        for n, item in enumerate(processor.split_items(entry.summary, PROMPT_VERSION), 1):
            found[f"{entry.key}.{n}"] = CorpusItem(entry.key, entry.date, item.text_md, indexed)
    return found


def check_corpus(text: str) -> list[str]:
    """Every problem with the corpus text, one line each; empty when it is usable."""
    from loom import processor

    entries = load_corpus(text)
    problems = []
    markers = len(_ANY_MARKER_RE.findall(text))
    if markers != len(entries):
        problems.append(f"{markers} entry markers but {len(entries)} well-formed ones "
                        "(`<!-- entry eNN YYYY-MM-DD -->`)")
    seen: set[str] = set()
    for n, entry in enumerate(entries):
        if entry.key in seen:
            problems.append(f"{entry.key}: duplicate entry key")
        seen.add(entry.key)
        if n and entry.date < entries[n - 1].date:
            problems.append(f"{entry.key}: date {entry.date} is before the previous entry's")
        items = processor.split_items(entry.summary, PROMPT_VERSION)
        bullets = len(_LABELLED_BULLET_RE.findall(entry.summary))
        if not items:
            problems.append(f"{entry.key}: no item (loom log would print it whole)")
        elif len(items) != bullets:
            problems.append(f"{entry.key}: labelled bullets {bullets}, items {len(items)} "
                            "(a heading the splitter does not read?)")
        if any(not item.labelled for item in items):
            problems.append(f"{entry.key}: an item without a v4 label")
    return problems


def validate(queries: list[Query], items: dict[str, CorpusItem]) -> list[str]:
    """Every problem with the queries, one line each; empty when they are usable."""
    problems = []
    seen: set[str] = set()
    for q in queries:
        if q.id in seen:
            problems.append(f"{q.id}: duplicate query id")
        seen.add(q.id)
        if q.category not in CATEGORIES:
            problems.append(f"{q.id}: unknown category {q.category!r}")
        if not words(q.terms):
            problems.append(f"{q.id}: no search terms")
        if not q.relevant:
            problems.append(f"{q.id}: no relevant item judged")
        named = q.relevant + q.distractors
        if len(set(named)) != len(named):
            problems.append(f"{q.id}: an item is named twice")
        unknown = [key for key in named if key not in items]
        for key in unknown:
            problems.append(f"{q.id}: item {key} is not in the corpus")
        if q.category in CATEGORIES and words(q.terms) and q.relevant and not unknown:
            problems += [f"{q.id}: {p}" for p in _category_problems(q, items)]
    return problems


def _category_problems(q: Query, items: dict[str, CorpusItem]) -> list[str]:
    terms = words(q.terms)

    def vocab(key: str) -> set[str]:
        return words(items[key].text)

    def entry_keys(key: str) -> list[str]:
        return [k for k, item in items.items() if item.entry == items[key].entry]

    def items_vocab(key: str) -> set[str]:
        return set().union(*(vocab(k) for k in entry_keys(key)))

    def entry_vocab(key: str) -> set[str]:
        indexed = items[key].entry_text
        return words(indexed) if indexed else items_vocab(key)

    problems = []
    if q.category in ("stale", "durable"):
        if not q.distractors:
            problems.append(f"a {q.category} query names its distractors")
        for key in q.relevant + q.distractors:
            for term in sorted(terms - vocab(key)):
                problems.append(f"{term!r} is not in {key}, so matching decides, not ranking")
        for rel in q.relevant:
            for dis in q.distractors:
                if q.category == "stale" and not items[dis].date < items[rel].date:
                    problems.append(f"distractor {dis} is not older than {rel}")
                if q.category == "durable" and not items[dis].date > items[rel].date:
                    problems.append(f"distractor {dis} is not newer than {rel}")
        return problems
    if q.distractors:
        problems.append("only stale and durable queries name distractors")
    for key in q.relevant:
        missing = terms - vocab(key)
        if q.category == "exact":
            problems += [f"{t!r} is not in {key}" for t in sorted(missing)]
        elif q.category == "morphology":
            if not missing:
                problems.append(f"every term is in {key}: nothing differs in form")
            problems += [f"{t!r} is not a form of a word in {key}"
                         for t in sorted(missing) if not _is_form_of(t, vocab(key))]
        elif q.category == "split":
            problems += [f"{t!r} is not in an item of {key}'s entry"
                         for t in sorted(terms - items_vocab(key))]
            problems += [f"{k} holds every term" for k in entry_keys(key) if terms <= vocab(k)]
        elif q.category == "extra":
            whole = entry_vocab(key)
            absent = terms - whole
            if not absent:
                problems.append(f"every term is in {key}'s entry: nothing is extra")
            problems += [f"{t!r} is a form of a word in {key}'s entry"
                         for t in sorted(absent) if _is_form_of(t, whole)]
            problems += [f"{t!r} is in {key}'s entry but not in {key}"
                         for t in sorted((terms - absent) - vocab(key))]
        elif q.category == "synonym":
            whole = entry_vocab(key)
            problems += [f"{t!r} (or a form of it) is in {key}'s entry"
                         for t in sorted(terms) if _is_form_of(t, whole)]
        elif q.category == "error":
            if not missing:
                problems.append(f"every term is in {key}: that is an exact query")
    if q.category == "split" and len({items[k].entry for k in q.relevant}) != 1:
        problems.append("a split query's relevant items must share one entry")
    return problems


# --------------------------------------------------------------------------- #
# Metrics                                                                      #
# --------------------------------------------------------------------------- #
def recall_at_k(ranked: list[str] | tuple[str, ...], relevant: tuple[str, ...],
                k: int = K) -> float:
    """The share of the relevant items found among the first ``k`` shown."""
    wanted = set(relevant)
    return len(wanted & set(ranked[:k])) / len(wanted)


def first_relevant(ranked: list[str] | tuple[str, ...], relevant: tuple[str, ...]) -> int | None:
    """The 1-based position of the first relevant item shown, or ``None``."""
    for position, key in enumerate(ranked, 1):
        if key in relevant:
            return position
    return None


def reciprocal_rank(ranked: list[str] | tuple[str, ...], relevant: tuple[str, ...]) -> float:
    """1 / the position of the first relevant item shown; 0 when none is."""
    position = first_relevant(ranked, relevant)
    return 1 / position if position is not None else 0.0


# --------------------------------------------------------------------------- #
# The real server and the real client, in process                              #
# --------------------------------------------------------------------------- #
@contextmanager
def _server(workdir: Path):
    """The API on a fresh database in ``workdir``, a user, the project, and a LOOM_HOME
    whose config points the client at it. Yields ``(test client, auth header)``."""
    from fastapi.testclient import TestClient

    from server import db
    from server.app import app

    keys = ("LOOM_DB", "LOOM_CLIENT_IP_SOURCE", "LOOM_HOME")
    saved = {key: os.environ.get(key) for key in keys}
    db_path = workdir / "loom.db"
    loom_home = workdir / "loom-home"
    os.environ["LOOM_DB"] = str(db_path)
    os.environ["LOOM_CLIENT_IP_SOURCE"] = "peer"
    os.environ["LOOM_HOME"] = str(loom_home)
    try:
        db.init_db(str(db_path)).close()
        token = secrets.token_urlsafe(32)
        conn = db.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO users (name, email, token_hash, active, created_at) "
                "VALUES (?, ?, ?, 1, ?)",
                ("bench", "bench@example.test", db.hash_token(token), db.iso_now()),
            )
            conn.commit()
        finally:
            conn.close()
        loom_home.mkdir()
        (loom_home / "config.json").write_text(
            json.dumps({"server_url": SERVER_URL, "token": token}), encoding="utf-8")
        auth = {"Authorization": f"Bearer {token}"}
        with TestClient(app, base_url=SERVER_URL) as client:
            resp = client.post("/api/projects", json={"remote_url": REMOTE, "name": PROJECT},
                               headers=auth)
            if resp.status_code != 201 or resp.json()["canonical_remote"] != CANONICAL:
                raise BenchmarkError(f"registering the project: {resp.status_code} {resp.text}")
            yield client, auth
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _post(client, auth, entry: Entry) -> int:
    """Post one entry as the processor would; returns its id."""
    from server import db

    # The server stamps `created_at` itself; its clock is the one thing set here.
    with mock.patch.object(db, "iso_now", return_value=f"{entry.date}T10:00:00Z"):
        resp = client.post("/api/entries", json=_payload(entry), headers=auth)
    if resp.status_code != 201:
        raise BenchmarkError(f"posting {entry.key}: {resp.status_code} {resp.text}")
    return resp.json()["id"]


class _InProcessSession:
    """The TestClient as the client's HTTP session. ``ApiClient`` only ever calls
    ``request``; the ``timeout`` it passes is a socket's, which an in-process call does not
    have (and which Starlette warns about)."""

    def __init__(self, client) -> None:
        self._client = client

    def request(self, method, url, **kwargs):
        kwargs.pop("timeout", None)
        return self._client.request(method, url, **kwargs)


def loom_log(client, terms: str) -> str:
    """What ``loom log ledger --grep "<terms>"`` prints, run as the `/loom` skill runs it.

    The one thing replaced is the client's HTTP session, which goes to the in-process
    server instead of the network; argument parsing, the config, the requests, the
    rendering and the budget are the client's own.

    Anything on stderr fails the run. ``loom log`` degrades out loud there and still
    exits 0 — "item ids unavailable … entries are shown whole" when the items request
    fails — and that rendering has no ids, so it would be scored as a page of empty
    results. Python warnings are kept off stderr so that it holds only the client's words.
    """
    from loom import cli
    from loom.api_client import ApiClient

    session = _InProcessSession(client)

    def in_process(server_url, token, **kwargs):
        return ApiClient(server_url, token, session=session, **kwargs)

    out, err = io.StringIO(), io.StringIO()
    with warnings.catch_warnings(record=True), \
            mock.patch.object(cli, "ApiClient", in_process), \
            redirect_stdout(out), redirect_stderr(err):
        code = cli.main(["log", PROJECT, "--grep", terms, *SKILL_FLAGS])
    if code != 0 or err.getvalue():
        raise BenchmarkError(f"loom log --grep {terms!r} exited {code}, stderr: "
                             f"{err.getvalue()!r}")
    printed = out.getvalue()
    if cli.DIGEST_HEADING in printed:
        raise BenchmarkError(f"loom log --grep {terms!r} printed a digest; the benchmark "
                             "reads item ids from the output and does not model digests")
    return printed


def shown_items(printed: str, item_key: dict[tuple[int, int], str]) -> list[str]:
    """The corpus keys of the items ``loom log`` printed, in the order it printed them."""
    shown = []
    for match in _SHOWN_ITEM_RE.finditer(printed):
        ref = (int(match[1]), int(match[2]))
        if ref not in item_key:
            raise BenchmarkError(f"loom log printed an item the corpus does not have: {ref}")
        shown.append(item_key[ref])
    return shown


# --------------------------------------------------------------------------- #
# The run                                                                      #
# --------------------------------------------------------------------------- #
def evaluate(workdir: Path, corpus_text: str | None = None,
             queries: list[Query] | None = None) -> Report:
    from loom import cli

    corpus_text = CORPUS.read_text(encoding="utf-8") if corpus_text is None else corpus_text
    queries = load_queries() if queries is None else queries
    entries = load_corpus(corpus_text)
    items = corpus_items(entries)
    problems = check_corpus(corpus_text) + validate(queries, items)
    if problems:
        raise BenchmarkError("the benchmark's inputs are not usable:\n  "
                             + "\n  ".join(problems))
    with _server(workdir) as (client, auth):
        item_key: dict[tuple[int, int], str] = {}
        for entry in entries:
            entry_id = _post(client, auth, entry)
            for key in items:
                if items[key].entry == entry.key:
                    item_key[(entry_id, int(key.rsplit(".", 1)[1]))] = key
        outcomes = []
        for query in queries:
            printed = loom_log(client, query.terms)
            ranked = tuple(shown_items(printed, item_key))
            outcomes.append(Outcome(
                query, ranked, first_relevant(ranked, query.relevant),
                recall_at_k(ranked, query.relevant), reciprocal_rank(ranked, query.relevant),
                hidden=not ranked and cli.NO_ITEM_MATCH_NOTE in printed,
            ))
    return Report(len(entries), len(items), tuple(outcomes))


def _row(label: str, outcomes: list[Outcome]) -> str:
    n = len(outcomes)
    if not n:
        return f"| {label} | 0 | — | — | — | — |"
    recall = sum(o.recall for o in outcomes) / n
    mrr = sum(o.rr for o in outcomes) / n
    empty = sum(not o.ranked for o in outcomes) / n
    hidden = sum(o.hidden for o in outcomes) / n
    return f"| {label} | {n} | {recall:.2f} | {mrr:.2f} | {empty:.2f} | {hidden:.2f} |"


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _first_relevant_cell(outcome: Outcome) -> str:
    if outcome.position is not None:
        return str(outcome.position)
    return "— (hidden)" if outcome.hidden else "—"


def render(report: Report) -> str:
    """RESULTS.md: deterministic, so the suite can compare it byte for byte."""
    lines = [
        "# Retrieval benchmark — results",
        "",
        "<!-- Generated by test/retrieval_eval/run_eval.py from corpus.md and queries.json;",
        "     do not edit by hand. server/tests/test_retrieval_eval.py checks it is current. -->",
        "",
        f"{report.entries} entries and {report.items} items of one made-up project; "
        f"{len(report.outcomes)} judged queries, each run through the real "
        f"`loom log {PROJECT} --grep \"<terms>\" {' '.join(SKILL_FLAGS)}` (the `/loom` "
        f"skill's command, default character budget). **recall@{K}**: the share of a "
        f"query's relevant items among the first {K} shown. **MRR**: the mean of 1 / the "
        "position of the first relevant item shown (0 when none is). **empty**: the share "
        "of queries shown no item. **hidden**: the share where entries matched but none of "
        "their items did, so `loom log` printed its \"no item\" note (counted in empty too). "
        "The categories are defined in `run_eval.py`; each isolates one kind of miss, so "
        "the `all` row (an average over queries, not over categories) depends on the mix: "
        "it compares two runs, it does not estimate real-world quality.",
        "",
        f"| category | queries | recall@{K} | MRR | empty | hidden |",
        "|---|---|---|---|---|---|",
    ]
    for category in CATEGORIES:
        lines.append(_row(category, [o for o in report.outcomes
                                     if o.query.category == category]))
    lines.append(_row("**all**", list(report.outcomes)))
    lines += [
        "",
        "## Queries",
        "",
        f"| id | category | terms | relevant | first {K} shown | first relevant at |",
        "|---|---|---|---|---|---|",
    ]
    for o in report.outcomes:
        shown = ", ".join(o.ranked[:K]) or "—"
        lines.append(f"| {o.query.id} | {o.query.category} | `{_cell(o.query.terms)}` | "
                     f"{', '.join(o.query.relevant)} | {shown} | {_first_relevant_cell(o)} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, help="write the report here instead of stdout")
    args = ap.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="loom-retrieval-") as workdir:
        text = render(evaluate(Path(workdir)))
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
