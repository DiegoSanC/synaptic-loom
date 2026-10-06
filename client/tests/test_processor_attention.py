"""Unit tests for P3.F5 (E10) — tear-ratio escalation + persistent attention record.

Scope: the processor's REPORT-ONLY observability units in isolation (threshold
parsing, the ``tear_ratio_high`` predicate, the attention record's upsert/heal
rules and its failure isolation). The real-boundary pipeline behavior is the
frozen goal ``test/waves/g/test_goal_e10_attention.py``.

The load-bearing property under test is that NONE of this can influence capture:
every unit here is reachable only through :func:`processor._observe_delta`, which
is isolated so that a hostile/unwritable record cannot turn a good pass into a
failed one (AC-F5.6).
"""
from __future__ import annotations

import json
import os
import stat as stat_module
from pathlib import Path

import pytest

from loom import processor
from loom.config import DEFAULT_TEAR_RATIO_THRESHOLD, Config
from loom.transcript_adapter import Delta, Msg

SID = "sess-attn-1"
SECRET = "UNITATTNSECRET-4b7e"


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _cfg(tmp_path: Path, **kwargs) -> Config:
    cfg = Config(
        loom_home=tmp_path / "loom_home",
        server_url="http://127.0.0.1:9",
        token="t",
        sweep_transcripts_root=tmp_path / "transcripts",
        **kwargs,
    )
    cfg.ensure_dirs()
    return cfg


def _write_config(tmp_path: Path, extra: dict) -> Path:
    home = tmp_path / "loom_home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        json.dumps({"server_url": "http://127.0.0.1:9", "token": "t", **extra}),
        encoding="utf-8",
    )
    return home


def _delta(*, messages: int = 0, torn: int = 0, held: int = 0,
           new_last_line: int = 0) -> Delta:
    msgs = [Msg(role="user", text="hola", uuid=f"u{i}") for i in range(messages)]
    return Delta(
        messages=msgs,
        new_last_line=new_last_line,
        new_last_uuid=msgs[-1].uuid if msgs else None,
        skipped=0,
        torn=torn,
        held=held,
        bytes_read=0,
    )


def _events(cfg: Config) -> list[str]:
    if not cfg.log_file.exists():
        return []
    return [
        line for line in cfg.log_file.read_text(encoding="utf-8").splitlines()
        if len(line.split("\t")) >= 3 and line.split("\t")[2] == "tear_ratio_high"
    ]


def _record(cfg: Config) -> dict | None:
    if not cfg.attention_path.exists():
        return None
    return json.loads(cfg.attention_path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Config: the additive threshold key (p2-config-additive-keys)                 #
# --------------------------------------------------------------------------- #
def test_threshold_defaults_when_the_key_is_absent(tmp_path):
    home = _write_config(tmp_path, {})
    assert Config.load(home).tear_ratio_threshold == DEFAULT_TEAR_RATIO_THRESHOLD


def test_threshold_defaults_with_an_empty_processor_section(tmp_path):
    home = _write_config(tmp_path, {"processor": {}})
    assert Config.load(home).tear_ratio_threshold == DEFAULT_TEAR_RATIO_THRESHOLD


@pytest.mark.parametrize("value", [0.1, 0.5, 1.0, 1])
def test_threshold_accepts_the_frozen_domain(tmp_path, value):
    home = _write_config(tmp_path, {"processor": {"tear_ratio_threshold": value}})
    assert Config.load(home).tear_ratio_threshold == float(value)


@pytest.mark.parametrize(
    "value",
    [0, 0.0, -0.5, 1.5, "0.7", None, True, False, [0.5], {"x": 1}, float("nan")],
)
def test_threshold_outside_the_domain_falls_back_to_the_default(tmp_path, value):
    """An out-of-domain or non-numeric value NEVER disables the feature silently and
    never crashes the config load: additive keys preserve prior behavior."""
    home = _write_config(tmp_path, {"processor": {"tear_ratio_threshold": value}})
    assert Config.load(home).tear_ratio_threshold == DEFAULT_TEAR_RATIO_THRESHOLD


def test_a_non_object_processor_section_does_not_break_the_load(tmp_path):
    home = _write_config(tmp_path, {"processor": "nonsense"})
    assert Config.load(home).tear_ratio_threshold == DEFAULT_TEAR_RATIO_THRESHOLD


def test_attention_path_is_next_to_state_json(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.attention_path == cfg.state_path.parent / "attention.json"


# --------------------------------------------------------------------------- #
# tear_ratio_high: the ratio predicate                                         #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("torn", "parsed", "threshold", "fires"),
    [
        (3, 1, 0.5, True),     # 0.75 >= 0.5
        (3, 9, 0.5, False),    # 0.25 < 0.5 — same torn count as the firing case
        (1, 1, 0.5, True),     # exactly 0.5: the frozen seam says >=
        (3, 9, 0.1, True),     # the threshold comes from the config, not a constant
        (3, 1, 1.0, False),    # 0.75 < 1.0 — the top of the frozen (0,1] domain
        (3, 0, 0.5, True),     # every message torn: ratio 1.0
        (0, 9, 0.5, False),    # torn == 0 never fires, whatever the threshold
        (0, 0, 0.5, False),    # empty delta: no division, no event
    ],
)
def test_tear_ratio_event_fires_on_the_ratio_not_the_raw_count(
        tmp_path, torn, parsed, threshold, fires):
    cfg = _cfg(tmp_path, tear_ratio_threshold=threshold)
    processor._observe_delta(cfg, SID, tmp_path / "t.jsonl",
                             _delta(messages=parsed, torn=torn, new_last_line=parsed),
                             last_line=0)
    events = _events(cfg)
    assert len(events) == (1 if fires else 0), events
    if fires:
        assert f"torn={torn}" in events[0] and f"parsed={parsed}" in events[0]
        assert "ratio=" in events[0]


def test_tear_ratio_event_carries_counts_only(tmp_path):
    """AC-F5.5: no transcript content, no raw path, one log column."""
    cfg = _cfg(tmp_path)
    transcript = tmp_path / "se  ss" / f"{SECRET}.jsonl"
    processor._observe_delta(cfg, SID, transcript,
                             _delta(messages=1, torn=3, new_last_line=4), last_line=0)
    line = _events(cfg)[0]
    assert line.count("\n") == 0
    assert SECRET not in line and str(transcript) not in line
    assert line.split("\t")[3] == "torn=3 parsed=1 ratio=0.75"


def test_tear_ratio_event_fires_at_most_once_per_observation(tmp_path):
    """Called once per processed marker; two markers ⇒ two events, never more."""
    cfg = _cfg(tmp_path)
    delta = _delta(messages=1, torn=3, new_last_line=4)
    processor._observe_delta(cfg, SID, tmp_path / "t.jsonl", delta, last_line=0)
    assert len(_events(cfg)) == 1
    processor._observe_delta(cfg, SID, tmp_path / "t.jsonl", delta, last_line=0)
    assert len(_events(cfg)) == 2


# --------------------------------------------------------------------------- #
# The attention record: create / update in place / heal                        #
# --------------------------------------------------------------------------- #
def test_wholly_unparseable_delta_creates_one_entry(tmp_path):
    cfg = _cfg(tmp_path)
    transcript = tmp_path / "sessions" / "s.jsonl"
    processor._observe_delta(cfg, SID, transcript, _delta(held=3), last_line=0)

    rec = _record(cfg)
    assert rec is not None and list(rec) == [SID]
    entry = rec[SID]
    assert entry["transcript_path"] == str(transcript)
    assert entry["held"] == 3
    assert entry["first_seen"] == entry["last_seen"]


def test_repeated_passes_update_the_same_entry_in_place(tmp_path):
    cfg = _cfg(tmp_path)
    transcript = tmp_path / "sessions" / "s.jsonl"
    processor._observe_delta(cfg, SID, transcript, _delta(held=3), last_line=0)
    first = _record(cfg)[SID]

    processor._observe_delta(cfg, SID, transcript, _delta(held=4), last_line=0)
    rec = _record(cfg)
    assert list(rec) == [SID], "the entry is keyed by session_id, never appended"
    assert rec[SID]["held"] == 4
    assert rec[SID]["first_seen"] == first["first_seen"]
    assert rec[SID]["last_seen"] >= first["last_seen"]


def test_other_sessions_are_untouched_by_an_update(tmp_path):
    cfg = _cfg(tmp_path)
    processor._observe_delta(cfg, "other", tmp_path / "o.jsonl", _delta(held=2),
                             last_line=0)
    processor._observe_delta(cfg, SID, tmp_path / "s.jsonl", _delta(held=3),
                             last_line=0)
    assert sorted(_record(cfg)) == sorted([SID, "other"])


def test_a_parsing_delta_removes_the_entry_on_that_pass(tmp_path):
    cfg = _cfg(tmp_path)
    transcript = tmp_path / "s.jsonl"
    processor._observe_delta(cfg, SID, transcript, _delta(held=3), last_line=0)
    assert SID in _record(cfg)

    processor._observe_delta(cfg, SID, transcript,
                             _delta(messages=2, torn=3, new_last_line=5), last_line=0)
    assert SID not in (_record(cfg) or {})


@pytest.mark.parametrize(
    ("delta_kwargs", "last_line", "why"),
    [
        ({"held": 0, "new_last_line": 5}, 5, "empty delta with held=0 is just EOF"),
        ({"held": 2, "new_last_line": 5}, 5, "a held run past a parsed prefix is not "
                                             "a wholly-unparseable transcript"),
    ],
)
def test_an_empty_delta_alone_never_creates_an_entry(tmp_path, delta_kwargs,
                                                     last_line, why):
    cfg = _cfg(tmp_path)
    processor._observe_delta(cfg, SID, tmp_path / "s.jsonl", _delta(**delta_kwargs),
                             last_line=last_line)
    assert not (_record(cfg) or {}), why


def test_a_non_matching_pass_does_not_create_the_record_file(tmp_path):
    """No record file appears until there is something to record: an empty record and
    an absent one must stay indistinguishable for ``loom status`` (AC-F5.4)."""
    cfg = _cfg(tmp_path)
    processor._observe_delta(cfg, SID, tmp_path / "s.jsonl",
                             _delta(messages=1, new_last_line=2), last_line=0)
    assert not cfg.attention_path.exists()


def test_the_stored_path_is_whitespace_flattened(tmp_path):
    """AC-F5.5: paths are flattened exactly like the existing log lines."""
    cfg = _cfg(tmp_path)
    transcript = tmp_path / "se  ss" / "s\tx.jsonl"
    processor._observe_delta(cfg, SID, transcript, _delta(held=1), last_line=0)
    stored = _record(cfg)[SID]["transcript_path"]
    assert stored == " ".join(str(transcript).split())
    assert "  " not in stored and "\t" not in stored


def test_the_record_is_written_with_owner_only_permissions(tmp_path):
    cfg = _cfg(tmp_path)
    processor._observe_delta(cfg, SID, tmp_path / "s.jsonl", _delta(held=1),
                             last_line=0)
    mode = stat_module.S_IMODE(cfg.attention_path.stat().st_mode)
    assert mode & 0o077 == 0, f"the record must not be group/world readable; {oct(mode)}"


# --------------------------------------------------------------------------- #
# Hostile / degraded inputs: report-only must never become capture-affecting   #
# --------------------------------------------------------------------------- #
def test_an_unparseable_record_is_replaced_not_propagated(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.attention_path.write_text("{not json", encoding="utf-8")
    processor._observe_delta(cfg, SID, tmp_path / "s.jsonl", _delta(held=1),
                             last_line=0)
    assert list(_record(cfg)) == [SID]


def test_a_record_holding_a_json_array_is_tolerated(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.attention_path.write_text("[1, 2, 3]", encoding="utf-8")
    processor._observe_delta(cfg, SID, tmp_path / "s.jsonl", _delta(held=1),
                             last_line=0)
    assert list(_record(cfg)) == [SID]


def test_a_corrupt_entry_is_rebuilt_rather_than_trusted(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.attention_path.write_text(json.dumps({SID: "not-an-object"}), encoding="utf-8")
    processor._observe_delta(cfg, SID, tmp_path / "s.jsonl", _delta(held=1),
                             last_line=0)
    entry = _record(cfg)[SID]
    assert isinstance(entry, dict) and entry["held"] == 1
    assert entry["first_seen"] and entry["last_seen"]


def test_additive_fields_on_an_existing_entry_survive_an_update(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.attention_path.write_text(json.dumps({
        SID: {"transcript_path": "/tmp/s.jsonl", "held": 1,
              "first_seen": "2026-01-01T00:00:00Z", "last_seen": "2026-01-01T00:00:00Z",
              "extra_additive": True},
    }), encoding="utf-8")
    processor._observe_delta(cfg, SID, tmp_path / "s.jsonl", _delta(held=2),
                             last_line=0)
    entry = _record(cfg)[SID]
    assert entry["extra_additive"] is True
    assert entry["first_seen"] == "2026-01-01T00:00:00Z"
    assert entry["held"] == 2


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the write permission bit")
def test_an_unwritable_home_never_raises(tmp_path):
    """REPORT-ONLY (AC-F5.6): a record that cannot be written must not propagate an
    exception into the capture pipeline — the marker's outcome cannot depend on it."""
    cfg = _cfg(tmp_path)
    cfg.loom_home.chmod(0o500)
    try:
        processor._observe_delta(cfg, SID, tmp_path / "s.jsonl", _delta(held=1),
                                 last_line=0)
    finally:
        cfg.loom_home.chmod(0o700)


def test_observation_failures_never_raise_out(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)

    def boom(*_a, **_kw):
        raise RuntimeError("record backend exploded")

    monkeypatch.setattr(processor, "_write_attention", boom)
    processor._observe_delta(cfg, SID, tmp_path / "s.jsonl", _delta(held=1),
                             last_line=0)
    # The failure is reported, never silent, and never carries content.
    log = cfg.log_file.read_text(encoding="utf-8")
    assert "attention_record_error" in log
    assert "record backend exploded" not in log
