"""Tests for transcript_adapter — over the real contract fixture and synthetic ones.

The real fixture (`contract/fixtures/real_session.jsonl`) is the primary input.
It also checks that the semantics match the reference extractor `read_delta`
in `contract/verify.py` (same number of messages, same last uuid).
"""
import importlib.util
import json
import shutil

import pytest

from loom.transcript_adapter import (
    TOOL_TRUNCATE,
    TRUNCATE_SUFFIX,
    AdapterError,
    Delta,
    Msg,
    is_torn_line,
    read_delta,
    render_for_summarizer,
)
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_FIXTURE = REPO_ROOT / "contract" / "fixtures" / "real_session.jsonl"
VERIFY_PY = REPO_ROOT / "contract" / "verify.py"
LOCAL_FIXTURES = Path(__file__).parent / "fixtures"


def _load_reference_read_delta():
    """Loads the reference `read_delta` from contract/verify.py (stdlib only)."""
    spec = importlib.util.spec_from_file_location("loom_contract_verify", VERIFY_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.read_delta


def _write_lines(path: Path, lines: list[str]) -> Path:
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


# --- Real fixture ----------------------------------------------------------------

def test_real_fixture_full_read():
    delta = read_delta(REAL_FIXTURE, 0)
    users = [m for m in delta.messages if m.role == "user"]
    assistants = [m for m in delta.messages if m.role == "assistant"]
    assert len(users) >= 1
    assert len(assistants) >= 1
    # number of lines in the file (== wc -l).
    wc = sum(1 for _ in REAL_FIXTURE.open(encoding="utf-8"))
    assert delta.new_last_line == wc
    assert delta.new_last_uuid
    # the last conversational message is the uuid anchor.
    assert delta.messages[-1].uuid == delta.new_last_uuid
    # metadata was skipped (allowlist): the fixture has more noise than conversation.
    assert delta.skipped > 0


def test_real_fixture_reread_from_eof_is_empty():
    delta = read_delta(REAL_FIXTURE, 0)
    again = read_delta(REAL_FIXTURE, delta.new_last_line)
    assert again.messages == []
    assert again.new_last_uuid is None
    assert again.new_last_line == delta.new_last_line  # the pointer does not go backwards


def test_real_fixture_agrees_with_reference():
    """Key AC: same semantics as contract/verify.py (message count + last uuid)."""
    ref_read_delta = _load_reference_read_delta()
    ref = ref_read_delta(REAL_FIXTURE, 0)
    mine = read_delta(REAL_FIXTURE, 0)
    assert len(mine.messages) == len(ref["messages"])
    assert mine.new_last_uuid == ref["new_last_uuid"]
    assert mine.new_last_line == ref["new_last_line"]
    assert mine.skipped == ref["skipped"]


# --- Synthetic: empty / metadata / errors ----------------------------------------

def test_empty_file_yields_empty_delta():
    delta = read_delta(LOCAL_FIXTURES / "empty.jsonl", 0)
    assert delta == Delta(messages=[], new_last_line=0, new_last_uuid=None, skipped=0)


def test_metadata_only_advances_pointer_without_messages():
    delta = read_delta(LOCAL_FIXTURES / "metadata_only.jsonl", 0)
    assert delta.messages == []
    assert delta.new_last_uuid is None
    assert delta.skipped == 5          # incl. an attachment line that does have a uuid
    assert delta.new_last_line == 5    # the pointer advances to EOF despite there being no content


def test_unparseable_interior_line_is_tolerated_as_a_tear():
    """Phase 2 (P2.F1 rule 1) supersedes the phase-1 fail-loud reading of this
    fixture: a line that does not parse as JSON is a *torn* line, and an interior
    one (a complete line follows) is skipped and counted, never fatal. The lines
    around it are still captured — that is the whole point of the change."""
    delta = read_delta(LOCAL_FIXTURES / "corrupt.jsonl", 0)
    assert [m.uuid for m in delta.messages] == ["u1", "a1"]
    assert delta.torn == 1
    assert delta.new_last_line == 3


def test_conversational_line_missing_uuid_raises():
    with pytest.raises(AdapterError):
        read_delta(LOCAL_FIXTURES / "missing_uuid.jsonl", 0)


def test_conversational_line_missing_message_raises(tmp_path):
    path = _write_lines(tmp_path / "no_message.jsonl", [
        '{"type":"attachment","uuid":"noise"}',
        '{"type":"assistant","uuid":"a1"}',
    ])
    with pytest.raises(AdapterError):
        read_delta(path, 0)


def test_last_line_at_or_past_eof_is_empty(tmp_path):
    path = _write_lines(tmp_path / "one.jsonl", [
        '{"type":"user","uuid":"u1","message":{"role":"user","content":"hi"}}',
    ])
    delta = read_delta(path, 1)     # last_line == number of lines
    assert delta.messages == []
    assert delta.new_last_uuid is None
    assert delta.new_last_line == 1


# --- Tool truncation -------------------------------------------------------------

def test_huge_tool_result_is_truncated(tmp_path):
    payload = "x" * 2_000_000
    path = _write_lines(tmp_path / "big.jsonl", [
        '{"type":"user","uuid":"u1","message":{"role":"user","content":'
        '[{"type":"tool_result","tool_use_id":"t1","content":"' + payload + '"}]}}',
    ])
    delta = read_delta(path, 0)
    rendered = render_for_summarizer(delta)
    assert TRUNCATE_SUFFIX in rendered
    assert "x" * TOOL_TRUNCATE in rendered           # 500 chars are kept
    assert "x" * (TOOL_TRUNCATE + 1) not in rendered  # and not one more
    assert len(rendered) < 1000                       # the 2 MB payload does not travel


# --- Incremental (resume) --------------------------------------------------------

def test_incremental_read_returns_only_tail(tmp_path):
    path = tmp_path / "session.jsonl"
    shutil.copyfile(LOCAL_FIXTURES / "incremental_base.jsonl", path)

    first = read_delta(path, 0)
    assert [m.uuid for m in first.messages] == ["u1", "a1", "u2"]
    assert first.skipped == 1
    assert first.new_last_line == 4
    assert first.new_last_uuid == "u2"

    # Simulates a `-r` that appends new turns to the same file.
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"type":"assistant","uuid":"a2","message":{"role":"assistant",'
                 '"content":[{"type":"text","text":"third reply"}]}}\n')
        fh.write('{"type":"user","uuid":"u3","message":{"role":"user",'
                 '"content":"third prompt"}}\n')

    tail = read_delta(path, first.new_last_line)
    assert [m.uuid for m in tail.messages] == ["a2", "u3"]
    assert tail.new_last_line == 6
    assert tail.new_last_uuid == "u3"
    assert tail.skipped == 0


# --- Render for the summarizer ---------------------------------------------------

def test_render_for_summarizer_format(tmp_path):
    path = _write_lines(tmp_path / "render.jsonl", [
        '{"type":"user","uuid":"u1","message":{"role":"user","content":"run echo"}}',
        '{"type":"attachment","uuid":"noise","content":"skipped"}',
        '{"type":"assistant","uuid":"a1","message":{"role":"assistant","content":'
        '[{"type":"text","text":"Running it."},'
        '{"type":"tool_use","name":"Bash","id":"t1","input":{"command":"echo hi"}}]}}',
        '{"type":"user","uuid":"u2","message":{"role":"user","content":'
        '[{"type":"tool_result","tool_use_id":"t1","content":"hi","is_error":false}]}}',
    ])
    delta = read_delta(path, 0)
    rendered = render_for_summarizer(delta)
    assert rendered == (
        "[user]: run echo\n"
        "[assistant]: Running it.\n"
        '[tool:Bash]: {"command": "echo hi"}\n'
        "[tool:result]: hi"
    )


def test_render_skips_empty_text_turns():
    # A thinking-only turn (empty text) must not emit an empty line.
    delta = Delta(
        messages=[
            Msg(role="assistant", text="", uuid="a0"),
            Msg(role="assistant", text="real content", uuid="a1"),
        ],
        new_last_line=2,
        new_last_uuid="a1",
        skipped=0,
    )
    assert render_for_summarizer(delta) == "[assistant]: real content"


# --- Torn lines (P2.F1, phase 2) -------------------------------------------------
#
# A *torn* line is a physical line that fails JSON parse OR strict UTF-8 decode:
# a crashed writer left half a line behind. Interior tears (something complete
# follows) are skipped and counted; a trailing tear is held (the writer may still
# be mid-append). A parsed-but-schema-invalid line is corruption, NOT a tear.

# Planted "partial secret": if it ever surfaces in a Msg/render/exception it is a leak.
TORN_MARK = "UNITTORNMARK-9c1f"

META = '{"type":"file-history-snapshot","messageId":"m0","snapshot":{}}'


def _user(uuid: str, text: str = "prompt") -> str:
    return json.dumps({"type": "user", "uuid": uuid,
                       "message": {"role": "user", "content": text}})


def _asst(uuid: str, text: str = "reply") -> str:
    return json.dumps({"type": "assistant", "uuid": uuid,
                       "message": {"role": "assistant",
                                   "content": [{"type": "text", "text": text}]}})


def _torn_prefix(uuid: str) -> bytes:
    """Half a JSON line, exactly as a SIGKILL leaves it."""
    return (b'{"type":"assistant","uuid":"' + uuid.encode()
            + b'","message":{"role":"assistant","content":[{"type":"text","text":"'
            + TORN_MARK.encode())


def _write_bytes(path: Path, blob: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return path


def test_interior_torn_line_is_skipped_and_counted(tmp_path):
    path = _write_bytes(tmp_path / "interior.jsonl", (
        META.encode() + b"\n" + _user("u1").encode() + b"\n"
        + _torn_prefix("torn") + b"\n" + _asst("a1").encode() + b"\n"
    ))
    delta = read_delta(path, 0)
    assert [m.uuid for m in delta.messages] == ["u1", "a1"]
    assert delta.torn == 1
    assert delta.skipped == 1              # the metadata line, unchanged semantics
    assert delta.new_last_line == 4        # interior tears ARE consumed
    assert all(TORN_MARK not in m.text for m in delta.messages)
    assert TORN_MARK not in render_for_summarizer(delta)


def test_interior_torn_mid_multibyte_line_does_not_raise(tmp_path):
    """The tear splits a UTF-8 sequence: a lone 0xC3 at the end of the line."""
    path = _write_bytes(tmp_path / "multibyte.jsonl", (
        _user("u1").encode() + b"\n" + _torn_prefix("torn") + b" caf\xc3\n"
        + _asst("a1").encode() + b"\n"
    ))
    delta = read_delta(path, 0)             # must NOT raise UnicodeDecodeError
    assert [m.uuid for m in delta.messages] == ["u1", "a1"]
    assert delta.torn == 1
    assert delta.new_last_line == 3


def test_complete_line_with_invalid_utf8_byte_is_torn_not_laundered(tmp_path):
    """A COMPLETE, schema-valid JSON line holding one invalid byte. Decoding with
    errors='replace' would turn it into a message — strict decode says: torn."""
    laundered = json.dumps({
        "type": "user", "uuid": "bad", "message": {"role": "user",
                                                   "content": f"{TORN_MARK} @@X@@"},
    }).encode().replace(b"@@X@@", b"\xff")
    path = _write_bytes(tmp_path / "launder.jsonl", (
        _user("u1").encode() + b"\n" + laundered + b"\n" + _asst("a1").encode() + b"\n"
    ))
    delta = read_delta(path, 0)
    assert [m.uuid for m in delta.messages] == ["u1", "a1"]
    assert delta.torn == 1
    assert all(TORN_MARK not in m.text for m in delta.messages)


def test_trailing_torn_line_holds_the_pointer_before_it(tmp_path):
    path = _write_bytes(tmp_path / "trailing.jsonl", (
        META.encode() + b"\n" + _user("u1").encode() + b"\n" + _torn_prefix("torn")
    ))
    delta = read_delta(path, 0)
    assert [m.uuid for m in delta.messages] == ["u1"]
    assert delta.new_last_line == 2         # stops BEFORE the tear
    assert delta.torn == 0                  # held, not skipped

    # Re-reading the unchanged file from the held pointer is empty and stable.
    again = read_delta(path, delta.new_last_line)
    assert again.messages == [] and again.new_last_line == 2


def test_trailing_torn_line_is_captured_once_it_heals(tmp_path):
    complete = _asst("a2", "healed").encode() + b"\n"
    path = _write_bytes(tmp_path / "heal.jsonl", (
        _user("u1").encode() + b"\n" + complete[: len(complete) // 2]
    ))
    first = read_delta(path, 0)
    assert first.new_last_line == 1

    with path.open("ab") as fh:             # the writer finishes its append
        fh.write(complete[len(complete) // 2:])
    healed = read_delta(path, first.new_last_line)
    assert [m.uuid for m in healed.messages] == ["a2"]
    assert healed.new_last_line == 2 and healed.new_last_uuid == "a2"


def test_run_of_trailing_torn_lines_holds_at_the_first_of_them(tmp_path):
    path = _write_bytes(tmp_path / "run.jsonl", (
        _user("u1").encode() + b"\n" + _torn_prefix("t1") + b"\n" + _torn_prefix("t2")
    ))
    delta = read_delta(path, 0)
    assert delta.new_last_line == 1
    assert delta.torn == 0


def test_schema_invalid_line_still_raises_even_with_a_valid_line_after(tmp_path):
    """Rule 3: it parses and decodes, so it is corruption — never a tear."""
    path = _write_bytes(tmp_path / "schema.jsonl", (
        _user("u1").encode() + b"\n" + b'{"type":"assistant"}\n'
        + _asst("a1").encode() + b"\n"
    ))
    with pytest.raises(AdapterError):
        read_delta(path, 0)


def test_adapter_error_message_carries_no_transcript_content(tmp_path):
    """I5: the diagnostic travels to the marker and the log — it may carry the line
    number and the line's ``type``, never its content (a partial secret)."""
    path = _write_bytes(tmp_path / "leaky.jsonl", (
        json.dumps({"type": "user", "note": TORN_MARK}).encode() + b"\n"
    ))
    with pytest.raises(AdapterError) as excinfo:
        read_delta(path, 0)
    assert TORN_MARK not in str(excinfo.value)


def test_torn_line_bytes_never_reach_the_exception_of_a_later_schema_error(tmp_path):
    path = _write_bytes(tmp_path / "mix.jsonl", (
        _torn_prefix("torn") + b"\n" + b'{"type":"user"}\n' + _asst("a1").encode() + b"\n"
    ))
    with pytest.raises(AdapterError) as excinfo:
        read_delta(path, 0)
    assert TORN_MARK not in str(excinfo.value)


def test_healthy_transcript_reports_zero_torn(tmp_path):
    path = _write_bytes(tmp_path / "healthy.jsonl", (
        META.encode() + b"\n" + _user("u1").encode() + b"\n" + _asst("a1").encode() + b"\n"
    ))
    delta = read_delta(path, 0)
    assert delta.torn == 0
    assert delta.new_last_line == 3
    assert delta.skipped == 1


def test_blank_line_after_a_tear_does_not_hold_the_pointer(tmp_path):
    """A blank line is neither content nor a tear: it closes a trailing run."""
    path = _write_bytes(tmp_path / "blank.jsonl", (
        _user("u1").encode() + b"\n" + _torn_prefix("t1") + b"\n" + b"\n"
    ))
    delta = read_delta(path, 0)
    assert delta.new_last_line == 3
    assert delta.torn == 1


def test_file_of_only_torn_lines_yields_an_empty_held_delta(tmp_path):
    path = _write_bytes(tmp_path / "alltorn.jsonl", _torn_prefix("t1"))
    delta = read_delta(path, 0)
    assert delta.messages == [] and delta.new_last_line == 0 and delta.torn == 0


def test_is_torn_line_classifier(tmp_path):
    assert is_torn_line(b'{"type":"user"}\n') is False
    assert is_torn_line(b"\n") is False
    assert is_torn_line(b"   \n") is False
    assert is_torn_line(b'{"type":"user"') is True
    assert is_torn_line(b'{"type":"user","x":"\xff"}\n') is True


# --- bytes_read: what the read actually consumed (wave-e review) ------------------
# ``bytes_read`` is the corroboration half of the pointer's stat pair: the processor
# only records ``(st_mtime_ns, st_size)`` when the read it describes saw exactly that
# many bytes. The invariant it rests on is that ``read_delta`` always iterates from
# index 0 to EOF (lines below ``last_line`` are ``continue``d, never seeked past), so
# ``bytes_read`` is the file's size at read time — independent of the pointer.

def test_bytes_read_equals_the_file_size(tmp_path):
    path = _write_lines(tmp_path / "sized.jsonl", [
        json.dumps({"type": "user", "uuid": "u1",
                    "message": {"role": "user", "content": "hola"}}),
        json.dumps({"type": "assistant", "uuid": "a1",
                    "message": {"role": "assistant", "content": "qué tal"}}),
    ])
    assert read_delta(path, 0).bytes_read == path.stat().st_size


def test_bytes_read_is_the_whole_file_even_when_the_pointer_is_at_eof(tmp_path):
    """The lines below ``last_line`` are read and discarded, not skipped by seeking:
    a pointer-relative ``bytes_read`` could never corroborate ``st_size``."""
    path = _write_lines(tmp_path / "resumed.jsonl", [
        json.dumps({"type": "user", "uuid": "u1",
                    "message": {"role": "user", "content": "primera"}}),
        json.dumps({"type": "assistant", "uuid": "a1",
                    "message": {"role": "assistant", "content": "segunda"}}),
        json.dumps({"type": "user", "uuid": "u2",
                    "message": {"role": "user", "content": "tercera"}}),
    ])
    size = path.stat().st_size
    for last_line in (0, 1, 2, 3, 99):
        delta = read_delta(path, last_line)
        assert delta.bytes_read == size, f"last_line={last_line}"


def test_bytes_read_counts_metadata_blank_and_torn_lines_too(tmp_path):
    """Every PHYSICAL line counts, whatever the adapter decides to do with it —
    otherwise a transcript of noise would under-report and never corroborate."""
    path = tmp_path / "noisy.jsonl"
    path.write_bytes(
        json.dumps({"type": "file-history-snapshot"}).encode() + b"\n"
        + b"\n"
        + b'{"type":"user","uuid":"u1"\n'                      # interior tear
        + json.dumps({"type": "user", "uuid": "u1",
                      "message": {"role": "user", "content": "ñ"}}).encode() + b"\n"
        + b'{"type":"assistant","half'                          # trailing tear (held)
    )
    delta = read_delta(path, 0)
    assert (delta.torn, delta.held) == (1, 1)
    assert delta.bytes_read == path.stat().st_size


def test_bytes_read_of_an_empty_transcript_is_zero(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_bytes(b"")
    assert read_delta(path, 0).bytes_read == 0


def test_bytes_read_matches_the_real_fixture_size():
    assert read_delta(REAL_FIXTURE, 0).bytes_read == REAL_FIXTURE.stat().st_size


# --- D9 amendment (2026-09-30): each turn carries its timestamp for the capture cutoff ---
def test_turns_carry_their_timestamp_and_an_unusable_one_is_none(tmp_path):
    from datetime import datetime, timezone

    def turn(uuid, **extra):
        return json.dumps({"type": "user", "uuid": uuid,
                           "message": {"role": "user", "content": "x"}, **extra})

    path = _write_lines(tmp_path / "t.jsonl", [
        turn("z", timestamp="2026-07-24T12:20:15.639Z"),   # SCHEMA.md's own example
        turn("offset", timestamp="2026-07-24T14:20:15+02:00"),
        turn("missing"),
        turn("prose", timestamp="yesterday"),
        turn("number", timestamp=1753359615),
        turn("naive", timestamp="2026-07-24T12:20:15"),    # no zone: not comparable
    ])

    at = {m.uuid: m.at for m in read_delta(path, 0).messages}

    assert at["z"] == datetime(2026, 7, 24, 12, 20, 15, 639000, tzinfo=timezone.utc)
    assert at["offset"] == datetime(2026, 7, 24, 12, 20, 15, tzinfo=timezone.utc)
    assert at["missing"] is None and at["prose"] is None
    assert at["number"] is None and at["naive"] is None
