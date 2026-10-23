#!/usr/bin/env python3
"""Retrieval benchmark for the logbook search (DESIGN §12.3 RT.1).

How well does ``loom log --grep "<terms>"`` put the item a reader needs in front of them?
A fixed synthetic logbook (``corpus.md``: one made-up project, entries written in
summarizer-v4 form) and a fixed set of judged queries (``queries.json``: the terms an
agent would pass, the need behind them, and the items that answer it — judged by hand
from the corpus, never from a search run) are scored through the REAL server routes:

1. every entry is posted through ``POST /api/entries`` the way the processor posts it —
   OKF frontmatter from ``okf.wrap_entry``, items from ``processor.split_items`` — with
   the server's clock set to the entry's date, so ``created_at`` is the corpus date;
2. every query makes the two requests ``loom log --grep`` makes:
   ``GET /api/projects/{id}/entries?q=…&limit=30`` (the ranked entries), then
   ``GET /api/projects/{id}/items?entry_ids=…&q=…`` (which items of them match);
3. the ranked list is what the reader is shown: each ranked entry's matching items, in
   ordinal order. An entry that matched only outside its items contributes nothing, as
   in ``loom log``, which skips it and counts it in a closing note.

Scores, per category and over all queries: ``recall@5`` (the share of a query's relevant
items among the first five shown), ``MRR`` (the mean of 1 / the position of the first
relevant item, 0 when none is shown) and ``empty`` (the share of queries shown nothing).
Out of scope: the digest, the character budget and supersession links. The corpus has
none, so a ``stale`` query measures ranking alone — the case where nobody has curated.

Unlike ``test/prompt_eval/`` it spends no model calls and is deterministic, so the suite
runs it: ``server/tests/test_retrieval_eval.py`` checks that ``RESULTS.md`` is what the
current search produces. A change to retrieval therefore shows its effect as a diff of
``RESULTS.md``; regenerate it with

    .venv/bin/python test/retrieval_eval/run_eval.py --out test/retrieval_eval/RESULTS.md
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "corpus.md"
QUERIES = HERE / "queries.json"
RESULTS = HERE / "RESULTS.md"

# The closed set a query may name, in the order RESULTS.md reports them.
CATEGORIES = ("exact", "morphology", "partial", "synonym", "stale", "error")
K = 5
# What the `/loom` skill asks for (`loom log … --limit 30`).
ENTRY_LIMIT = 30
PROMPT_VERSION = "v4"
MODEL = "sonnet"
REMOTE = "git@github.com:example-org/ledger.git"
CANONICAL = "github.com/example-org/ledger"

_ENTRY_RE = re.compile(r"^<!-- entry (?P<key>e\d{2}) (?P<date>\d{4}-\d{2}-\d{2}) -->$", re.M)


@dataclass(frozen=True)
class Entry:
    key: str
    date: str       # YYYY-MM-DD
    summary: str


@dataclass(frozen=True)
class Query:
    id: str
    category: str
    need: str
    terms: str
    relevant: tuple[str, ...]


@dataclass(frozen=True)
class Outcome:
    query: Query
    ranked: tuple[str, ...]
    recall: float
    rr: float


@dataclass(frozen=True)
class Report:
    entries: int
    items: int
    unlabelled: int
    outcomes: tuple[Outcome, ...]


class BenchmarkError(Exception):
    """The benchmark's own inputs are wrong, or the server refused them."""


# --------------------------------------------------------------------------- #
# Inputs                                                                       #
# --------------------------------------------------------------------------- #
def load_corpus(path: Path = CORPUS) -> list[Entry]:
    text = path.read_text(encoding="utf-8")
    marks = list(_ENTRY_RE.finditer(text))
    entries = []
    for n, mark in enumerate(marks):
        end = marks[n + 1].start() if n + 1 < len(marks) else len(text)
        entries.append(Entry(mark["key"], mark["date"], text[mark.end():end].strip()))
    return entries


def load_queries(path: Path = QUERIES) -> list[Query]:
    return [Query(q["id"], q["category"], q["need"], q["terms"], tuple(q["relevant"]))
            for q in json.loads(path.read_text(encoding="utf-8"))]


def validate(queries: list[Query], item_keys: set[str]) -> list[str]:
    """Every problem with the queries, one line each; empty when they are usable."""
    problems = []
    seen: set[str] = set()
    for q in queries:
        if q.id in seen:
            problems.append(f"{q.id}: duplicate query id")
        seen.add(q.id)
        if q.category not in CATEGORIES:
            problems.append(f"{q.id}: unknown category {q.category!r}")
        if not q.terms.strip():
            problems.append(f"{q.id}: no search terms")
        if not q.relevant:
            problems.append(f"{q.id}: no relevant item judged")
        for key in q.relevant:
            if key not in item_keys:
                problems.append(f"{q.id}: relevant item {key} is not in the corpus")
    return problems


# --------------------------------------------------------------------------- #
# Metrics                                                                      #
# --------------------------------------------------------------------------- #
def recall_at_k(ranked: list[str] | tuple[str, ...], relevant: tuple[str, ...],
                k: int = K) -> float:
    """The share of the relevant items found among the first ``k`` shown."""
    wanted = set(relevant)
    return len(wanted & set(ranked[:k])) / len(wanted)


def reciprocal_rank(ranked: list[str] | tuple[str, ...], relevant: tuple[str, ...]) -> float:
    """1 / the position of the first relevant item shown; 0 when none is."""
    for position, key in enumerate(ranked, 1):
        if key in relevant:
            return 1 / position
    return 0.0


# --------------------------------------------------------------------------- #
# The real server, in process                                                  #
# --------------------------------------------------------------------------- #
@contextmanager
def _server(workdir: Path):
    """The API on a fresh database file in ``workdir``, with a user and the project."""
    from fastapi.testclient import TestClient

    from server import db
    from server.app import app

    keys = ("LOOM_DB", "LOOM_CLIENT_IP_SOURCE")
    saved = {key: os.environ.get(key) for key in keys}
    db_path = workdir / "loom.db"
    os.environ["LOOM_DB"] = str(db_path)
    os.environ["LOOM_CLIENT_IP_SOURCE"] = "peer"
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
        auth = {"Authorization": f"Bearer {token}"}
        with TestClient(app) as client:
            resp = client.post("/api/projects", json={"remote_url": REMOTE}, headers=auth)
            if resp.status_code != 201 or resp.json()["canonical_remote"] != CANONICAL:
                raise BenchmarkError(f"registering the project: {resp.status_code} {resp.text}")
            yield client, auth, resp.json()["id"]
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _post(client, auth, entry: Entry) -> tuple[int, int, int]:
    """Post one entry as the processor would; ``(entry id, items, unlabelled items)``."""
    from loom import okf, processor
    from server import db

    stamp = f"{entry.date}T10:00:00Z"
    items = processor.split_items(entry.summary, PROMPT_VERSION)
    item_types = sorted({item.item_type for item in items})
    content_md = okf.wrap_entry(
        entry.summary, canonical_remote=CANONICAL, session_id=f"bench-{entry.key}",
        covered_from=f"{entry.key}-first", covered_to=f"{entry.key}-last",
        model=MODEL, prompt_version=PROMPT_VERSION, item_types=item_types, timestamp=stamp,
    )
    payload = {
        "project_remote": CANONICAL, "session_id": f"bench-{entry.key}",
        "covered_from": f"{entry.key}-first", "covered_to": f"{entry.key}-last",
        "content_md": content_md,
        "content_hash": hashlib.sha256(content_md.encode("utf-8")).hexdigest(),
        "model": MODEL, "prompt_version": PROMPT_VERSION, "item_types": item_types,
    }
    posted = processor.postable_items(items)
    if posted is not None:
        payload["items"] = posted
    # The server stamps `created_at` itself; its clock is the one thing set here.
    with mock.patch.object(db, "iso_now", return_value=stamp):
        resp = client.post("/api/entries", json=payload, headers=auth)
    if resp.status_code != 201:
        raise BenchmarkError(f"posting {entry.key}: {resp.status_code} {resp.text}")
    return resp.json()["id"], len(items), sum(not item.labelled for item in items)


def retrieve(client, auth, project_id: int, terms: str,
             item_key: dict[tuple[int, int], str]) -> list[str]:
    """The items ``loom log --grep "<terms>"`` shows, in the order it shows them."""
    resp = client.get(f"/api/projects/{project_id}/entries",
                      params={"q": terms, "limit": ENTRY_LIMIT}, headers=auth)
    if resp.status_code != 200:
        raise BenchmarkError(f"searching {terms!r}: {resp.status_code} {resp.text}")
    entries = resp.json()["entries"]
    if not entries:
        return []
    resp = client.get(f"/api/projects/{project_id}/items",
                      params={"entry_ids": ",".join(str(e["id"]) for e in entries),
                              "q": terms},
                      headers=auth)
    if resp.status_code != 200:
        raise BenchmarkError(f"items for {terms!r}: {resp.status_code} {resp.text}")
    matching: dict[int, list[int]] = {}
    for item in resp.json()["items"]:
        if item["match"]:
            matching.setdefault(item["entry_id"], []).append(item["ordinal"])
    return [item_key[(e["id"], ordinal)]
            for e in entries for ordinal in sorted(matching.get(e["id"], []))]


# --------------------------------------------------------------------------- #
# The run                                                                      #
# --------------------------------------------------------------------------- #
def evaluate(workdir: Path, corpus: list[Entry] | None = None,
             queries: list[Query] | None = None) -> Report:
    corpus = load_corpus() if corpus is None else corpus
    queries = load_queries() if queries is None else queries
    with _server(workdir) as (client, auth, project_id):
        item_key: dict[tuple[int, int], str] = {}
        total = unlabelled = 0
        for entry in corpus:
            entry_id, n_items, n_unlabelled = _post(client, auth, entry)
            for ordinal in range(1, n_items + 1):
                item_key[(entry_id, ordinal)] = f"{entry.key}.{ordinal}"
            total += n_items
            unlabelled += n_unlabelled
        problems = validate(queries, set(item_key.values()))
        if problems:
            raise BenchmarkError("the queries are not usable:\n  " + "\n  ".join(problems))
        outcomes = []
        for query in queries:
            ranked = tuple(retrieve(client, auth, project_id, query.terms, item_key))
            outcomes.append(Outcome(query, ranked, recall_at_k(ranked, query.relevant),
                                    reciprocal_rank(ranked, query.relevant)))
    return Report(len(corpus), total, unlabelled, tuple(outcomes))


def _row(label: str, outcomes: list[Outcome]) -> str:
    n = len(outcomes)
    recall = sum(o.recall for o in outcomes) / n
    mrr = sum(o.rr for o in outcomes) / n
    empty = sum(not o.ranked for o in outcomes) / n
    return f"| {label} | {n} | {recall:.2f} | {mrr:.2f} | {empty:.2f} |"


def _first_relevant(outcome: Outcome) -> str:
    return f"{round(1 / outcome.rr)}" if outcome.rr else "—"


def render(report: Report) -> str:
    """RESULTS.md: deterministic, so the suite can compare it byte for byte."""
    lines = [
        "# Retrieval benchmark — results",
        "",
        "<!-- Generated by test/retrieval_eval/run_eval.py from corpus.md and queries.json;",
        "     do not edit by hand. server/tests/test_retrieval_eval.py checks it is current. -->",
        "",
        f"{report.entries} entries and {report.items} items of one made-up project; "
        f"{len(report.outcomes)} judged queries, each run the way `loom log --grep` runs it "
        f"(`--limit {ENTRY_LIMIT}`). **recall@{K}**: the share of a query's relevant items "
        f"among the first {K} shown. **MRR**: the mean of 1 / the position of the first "
        "relevant item (0 when none is shown). **empty**: the share of queries shown "
        "nothing. Each category isolates one kind of miss, so the `all` row (an average "
        "over queries, not over categories) depends on the mix: it compares two runs, it "
        "does not estimate real-world quality.",
        "",
        f"| category | queries | recall@{K} | MRR | empty |",
        "|---|---|---|---|---|",
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
        lines.append(f"| {o.query.id} | {o.query.category} | `{o.query.terms}` | "
                     f"{', '.join(o.query.relevant)} | {shown} | {_first_relevant(o)} |")
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
