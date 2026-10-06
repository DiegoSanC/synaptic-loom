"""Tests for loom.okf — OKF-conformant frontmatter of the entries (D7)."""
from loom import okf

SUMMARY = (
    "## Contexto\n"
    "Elección de librería HTTP.\n\n"
    "## Decisiones\n"
    "- **httpx en lugar de requests** — necesitamos async. Estado: vigente.\n"
)


def _wrap(summary_md: str, *, timestamp: str | None = "2026-07-28T10:00:00Z") -> str:
    return okf.wrap_entry(
        summary_md,
        canonical_remote="github.com/example-group/loom-pilot",
        session_id="sess-1",
        covered_from=None,
        covered_to="uuid-b",
        model="sonnet",
        prompt_version="v1",
        timestamp=timestamp,
    )


# --- extract_title ----------------------------------------------------------------

def test_title_is_first_bold_decision():
    assert okf.extract_title(SUMMARY) == "httpx en lugar de requests"


def test_title_none_without_decisions():
    assert okf.extract_title("## Contexto\nSolo prosa.\n") is None


def test_title_truncated_at_60_with_ellipsis():
    long = "- **" + "palabra " * 20 + "**\n"
    title = okf.extract_title(long)
    assert title is not None and len(title) <= 60 and title.endswith("…")


def test_title_collapses_internal_whitespace():
    assert okf.extract_title("- **dos   espacios\ty salto**") == "dos espacios y salto"


# --- wrap_entry -------------------------------------------------------------------

def test_wrap_structure_and_fields():
    out = _wrap(SUMMARY)
    assert out.startswith("---\n")
    _, head, body = out.split("---\n", 2)
    assert "type: decision-log-entry" in head
    assert 'title: "httpx en lugar de requests"' in head
    assert 'timestamp: "2026-07-28T10:00:00Z"' in head
    assert "generated: true" in head
    assert "status: current" in head
    assert 'project: "github.com/example-group/loom-pilot"' in head
    assert 'tags: ["loom-pilot"]' in head
    assert '- session: "sess-1"' in head
    assert "covered_from: null" in head            # None → null (YAML)
    assert 'covered_to: "uuid-b"' in head
    assert 'model: "sonnet"' in head and 'prompt: "v1"' in head
    # The body is the summary untouched, with a single trailing newline.
    assert body.strip() == SUMMARY.strip()
    assert out.endswith("\n") and not out.endswith("\n\n")


def test_wrap_title_fallback_uses_date():
    out = _wrap("## Contexto\nSolo prosa.\n")
    assert 'title: "Session log 2026-07-28"' in out


def test_wrap_escapes_yaml_hostile_title():
    tricky = '- **usar "foo: bar" como clave** — motivo.\n'
    out = _wrap(tricky)
    assert 'title: "usar \\"foo: bar\\" como clave"' in out


def test_wrap_is_deterministic_given_timestamp():
    assert _wrap(SUMMARY) == _wrap(SUMMARY)


def test_wrap_generates_timestamp_when_omitted():
    out = _wrap(SUMMARY, timestamp=None)
    assert 'timestamp: "20' in out and 'Z"' in out
