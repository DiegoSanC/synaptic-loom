#!/usr/bin/env python3
"""Evaluation harness for the summarizer prompts (plan 2026-09-23, slice B, D11).

NOT part of the product and NOT collected by pytest: it spends real model calls. It
runs every case in ``cases/`` through the REAL ``claude`` CLI with the product's own
invocation (``processor._run_summarizer``: same flags, same env, same cwd discipline)
and scores each output against the case's ``.expect.json``, which was written from the
case's design before any run. The scoring reuses the product's parsers wherever the
product has one (``parse_item_types`` for the fallback count, ``split_items`` for what
an item is), so "passes the eval" means "the product would read it this way".

Pass rule, fixed before the first run: a prompt passes a case only if EVERY run passes.
A flaky case is a reason to revise the prompt, never the rule. v2 runs as the baseline:
it is scored against the same expectations, and its failures are the evidence for v3.

Since D15 (2026-10-01) every output must also be English, whatever the case's language
(:func:`spanish_words`): that is v4's change, and v3 on the Spanish cases is its baseline.

    PYTHONPATH=client:. .venv/bin/python test/prompt_eval/run_eval.py \
        --prompts v3 v4 --runs 3 --out test/prompt_eval/RESULTS.md

Raw outputs land in ``test/prompt_eval/runs/<stamp>/`` (git-ignored).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CASES = HERE / "cases"
LABELS = ("decision", "method", "task", "fact")
V3_HEADINGS = {"## Context", "## Decisions", "## Open questions"}

_BULLET_RE = re.compile(r"^(?P<indent>[ \t]*)[-*+][ \t]+")
_STATUS_RE = re.compile(r"Status:\s*(current|reverted|superseded)\b[^\n]*$", re.I)
# D15: Spanish function words English prose never needs. More than two distinct ones
# outside code spans is Spanish prose; a name or one untranslated term is not.
_SPANISH_WORDS = frozenset({"el", "la", "los", "las", "que", "para", "por", "una", "con",
                            "del", "pero", "sin", "porque", "está", "también", "según",
                            "es", "se", "un", "en", "al", "más", "hay", "son", "su", "lo"})
_CODE_SPAN_RE = re.compile(r"`[^`]*`")
# Ticket ids are not prose: "CON-818" would otherwise read as the Spanish "con".
_TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
SPANISH_WORDS_MAX = 2


def items_of(summary: str) -> list[tuple[str | None, str]]:
    """``(label or None, text)`` of every item, cut by the product's own splitter —
    exactly the rows the server would store (D13). ``None`` = an unlabelled item."""
    from loom import processor

    return [(item.item_type if item.labelled else None, item.text_md)
            for item in processor.split_items(summary.strip(), "v3")]


def section(summary: str, heading: str) -> str | None:
    out, inside, found = [], False, False
    for line in summary.splitlines():
        if line.startswith("## "):
            inside = line.strip() == heading
            found = found or inside
            continue
        if inside:
            out.append(line)
    return "\n".join(out) if found else None


def spanish_words(summary: str) -> set[str]:
    """The distinct :data:`_SPANISH_WORDS` in ``summary``, code spans and ticket ids
    excluded."""
    prose = _CODE_SPAN_RE.sub(" ", _TICKET_RE.sub(" ", summary)).lower()
    return set(re.findall(r"[a-záéíóúñü]+", prose)) & _SPANISH_WORDS


def _has(text: str, term: str) -> bool:
    return term.lower() in text.lower()


def score(summary: str, expect: dict) -> list[str]:
    """Every failed check, as a short reason. Empty list = pass."""
    from loom import processor

    out = summary.strip()
    if expect.get("no_entry"):
        return [] if out == "NO_ENTRY" else ["expected exactly NO_ENTRY"]
    if out == "NO_ENTRY":
        return ["answered NO_ENTRY"]

    fails: list[str] = []
    # --- the v3 contract, checked on every case ---------------------------------------
    headings = [ln.strip() for ln in out.splitlines() if ln.startswith("## ")]
    stray = [h for h in headings if h not in V3_HEADINGS]
    if stray:
        fails.append(f"headings outside the literal-English set: {stray}")
    if "## Decisions" not in headings:
        fails.append("no '## Decisions' section")
    _types, fallbacks = processor.parse_item_types(out, "v2")   # the v2 grammar == v3's
    if fallbacks:
        fails.append(f"label_fallback={fallbacks}")
    items = items_of(out)
    if any(_STATUS_RE.search(text.splitlines()[0]) for _l, text in items):
        fails.append("per-item Status: field")
    open_q = section(out, "## Open questions")
    if open_q is not None and not any(_BULLET_RE.match(ln) for ln in open_q.splitlines()):
        fails.append("empty '## Open questions' section written instead of omitted")
    spanish = spanish_words(out)
    if len(spanish) > SPANISH_WORDS_MAX:
        fails.append(f"not English (D15): {sorted(spanish)}")

    # --- the case's own expectations ---------------------------------------------------
    labels = {label for label, _t in items if label}
    for label in expect.get("required_labels", []):
        if label not in labels:
            fails.append(f"missing [{label}]")
    if "exact_labels" in expect and labels != set(expect["exact_labels"]):
        fails.append(f"labels {sorted(labels)} != {sorted(expect['exact_labels'])}")
    for label in expect.get("forbidden_labels", []):
        if label in labels:
            fails.append(f"forbidden [{label}]")
    if "first_item_mentions_any" in expect:
        first = items[0][1] if items else ""
        head = re.search(r"\*\*(.+?)\*\*", first)
        head = head.group(1) if head else first
        if not any(_has(head, t) for t in expect["first_item_mentions_any"]):
            fails.append(f"first item is not the final choice: {head[:80]!r}")
    decisions_text = "\n".join(text for _l, text in items)
    for term in expect.get("decisions_mention_all", []):
        if not _has(decisions_text, term):
            fails.append(f"Decisions never mention {term!r}")
    for term, partners in expect.get("items_mentioning_must_also_mention", {}).items():
        for _l, text in items:
            if _has(text, term) and not any(_has(text, p) for p in partners):
                fails.append(f"an item about {term!r} alone (discarded option as an item)")
    for label, terms in expect.get("label_item_mentions_any", {}).items():
        texts = [text for lab, text in items if lab == label]
        if not any(_has(text, t) for text in texts for t in terms):
            fails.append(f"no [{label}] item mentions any of {terms}")
    for label, terms in expect.get("label_items_must_not_mention", {}).items():
        for lab, text in items:
            if lab == label and any(_has(text, t) for t in terms):
                fails.append(f"a [{label}] item mentions one of {terms}")
    if expect.get("requires_open_questions") and open_q is None:
        fails.append("no '## Open questions' section")
    if "open_questions_mention_any" in expect:
        if open_q is None or not any(_has(open_q, t)
                                     for t in expect["open_questions_mention_any"]):
            fails.append(f"Open questions mention none of {expect['open_questions_mention_any']}")
    for term in expect.get("open_questions_must_not_mention", []):
        if open_q is not None and _has(open_q, term):
            fails.append(f"Open questions mention {term!r} (pending work is a [task])")
    return fails


def _run_one(claude_bin: str, model: str, prompt_path: Path, version: str,
             delta: str, timeout_s: int) -> str:
    from loom import processor
    from loom.config import Config

    with tempfile.TemporaryDirectory(prefix="loom-eval-") as home:
        cfg = Config(loom_home=Path(home), server_url="http://unused", token="unused",
                     model=model, prompt_version=version, prompt_path=prompt_path,
                     claude_bin=claude_bin, timeout_s=timeout_s)
        cp = processor._run_summarizer(cfg, delta)
    if cp.returncode != 0:
        return f"<<claude exited {cp.returncode}: {cp.stderr.strip()[:300]}>>"
    return cp.stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--prompts", nargs="+", default=["v3", "v4"])
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--cases", nargs="*", help="case names (default: all)")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--out", type=Path, help="write a markdown report here")
    args = ap.parse_args(argv)

    claude_bin = shutil.which("claude")
    if not claude_bin:
        print("claude CLI not on PATH", file=sys.stderr)
        return 2
    version = subprocess.run([claude_bin, "--version"], capture_output=True,
                             text=True).stdout.strip()
    names = args.cases or sorted(p.stem for p in CASES.glob("*.txt"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw_dir = HERE / "runs" / stamp
    raw_dir.mkdir(parents=True, exist_ok=True)

    jobs = [(name, prompt, run) for name in names for prompt in args.prompts
            for run in range(1, args.runs + 1)]

    def work(job):
        name, prompt, run = job
        delta = (CASES / f"{name}.txt").read_text(encoding="utf-8")
        out = _run_one(claude_bin, args.model, REPO / "prompts" / f"summarizer_{prompt}.md",
                       prompt, delta, args.timeout)
        (raw_dir / f"{name}.{prompt}.run{run}.md").write_text(out, encoding="utf-8")
        expect = json.loads((CASES / f"{name}.expect.json").read_text(encoding="utf-8"))
        return job, score(out, expect)

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = dict(pool.map(work, jobs))

    lines = [
        f"# Summarizer prompt eval — {stamp}",
        "",
        f"`{version}` · model `{args.model}` · {args.runs} run(s) per case and prompt · "
        "pass = every run passes",
        "",
        "| case | " + " | ".join(args.prompts) + " |",
        "|---|" + "---|" * len(args.prompts),
    ]
    verdict = {p: True for p in args.prompts}
    for name in names:
        cells = []
        for prompt in args.prompts:
            ok = sum(not results[(name, prompt, r)] for r in range(1, args.runs + 1))
            verdict[prompt] &= ok == args.runs
            cells.append(f"{'✅' if ok == args.runs else '❌'} {ok}/{args.runs}")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += ["", "**Verdict:** " + ", ".join(
        f"{p} {'PASS' if verdict[p] else 'FAIL'}" for p in args.prompts), "",
        "## Failures", ""]
    for (name, prompt, run), fails in sorted(results.items()):
        if fails:
            lines.append(f"- `{name}` · {prompt} · run {run}: " + "; ".join(fails))
    report = "\n".join(lines) + "\n"
    print(report)
    print(f"raw outputs: {raw_dir}", file=sys.stderr)
    if args.out:
        args.out.write_text(report, encoding="utf-8")
    return 0 if all(verdict.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
