"""Unit tests for the phase-4 sweep economics (epic E9: P3.F1–P3.F4).

Scope: the parts of ``loom.sweep`` + ``loom.coverage_cache`` whose failure mode is
SILENT — a transcript that quietly stops being swept — rather than a visible error.
The frozen goal (``test/waves/g/test_goal_e9_coverage_cache.py``) drives the real
boundary end to end; this file pins the units around it: the cache's refusal to
answer on anything ambiguous, the shapes of a registry cache the sweep must fail OPEN
on, the per-pass memo that keeps the precheck from forking ``git`` per candidate, and
the cache-file lifecycle (prune, no-wipe, degrade-on-error).

Real temporary filesystem; real ``git`` only where the remote boundary is the subject.
"""
from __future__ import annotations

import errno
import json
import os
import subprocess
import time
from pathlib import Path

from loom import coverage_cache, processor, sweep
from loom.config import Config

SID = "sess-p4-1"
ENCODED_DIR = "-Users-alice-Projects-webapp"
REGISTERED = "github.com/example-group/registered"
REGISTERED_URL = "git@github.com:example-group/registered.git"
UNREGISTERED_URL = "git@github.com:example-group/unregistered.git"


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _cfg(tmp_path: Path, **kwargs) -> Config:
    root = tmp_path / "transcripts"
    root.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        loom_home=tmp_path / "loom_home",
        server_url="http://127.0.0.1:9",
        token="t",
        sweep_transcripts_root=root,
        **kwargs,
    )
    cfg.ensure_dirs()
    return cfg


def _lines(cwd: str, session_id: str) -> list[dict]:
    return [
        {"type": "file-history-snapshot", "messageId": "m0", "snapshot": {}},
        {"type": "user", "uuid": f"{session_id}-u1", "cwd": cwd,
         "sessionId": session_id, "message": {"role": "user", "content": "hola"}},
        {"type": "assistant", "uuid": f"{session_id}-a1", "cwd": cwd,
         "sessionId": session_id,
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "vale"}]}},
    ]


def _orphan(cfg: Config, cwd: str = "/work/repo", *, sid: str = SID,
            aged: float = 3600) -> Path:
    path = cfg.sweep_transcripts_root / ENCODED_DIR / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in _lines(cwd, sid)) + "\n",
                    encoding="utf-8")
    ts = time.time() - aged
    os.utime(path, (ts, ts))
    return path


def _orphan_path(cfg: Config, sid: str) -> Path:
    return cfg.sweep_transcripts_root / ENCODED_DIR / f"{sid}.jsonl"


def _repo(path: Path, remote_url: str = REGISTERED_URL) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=path, check=True)
    return path


def _registry(cfg: Config, canonicals: list[str]) -> None:
    cfg.cache_path.write_text(
        json.dumps({"fetched_at": processor._now_iso(),
                    "projects": [{"id": i, "canonical_remote": c}
                                 for i, c in enumerate(canonicals)]}),
        encoding="utf-8")


def _cache(cfg: Config) -> dict:
    return json.loads(sweep.coverage_cache_path(cfg).read_text(encoding="utf-8"))


def _entry(st, *, line_count, cwd) -> dict:
    return {"stat_dev": st.st_dev, "stat_ino": st.st_ino,
            "stat_mtime_ns": st.st_mtime_ns, "stat_size": st.st_size,
            "line_count": line_count, "cwd": cwd}


# --------------------------------------------------------------------------- #
# coverage_cache.lookup — every ambiguity resolves to "re-read"                #
# --------------------------------------------------------------------------- #
def test_lookup_returns_the_entry_when_all_four_stat_fields_match(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text("x\n", encoding="utf-8")
    st = t.lstat()
    cache = {str(t): _entry(st, line_count=1, cwd="/work/repo")}

    assert coverage_cache.lookup(cache, str(t), st)["cwd"] == "/work/repo"


def test_lookup_misses_when_any_single_stat_field_differs(tmp_path):
    """A wrong hit strands a transcript forever, so all four vote — individually."""
    t = tmp_path / "t.jsonl"
    t.write_text("x\n", encoding="utf-8")
    st = t.lstat()

    for field in ("stat_dev", "stat_ino", "stat_mtime_ns", "stat_size"):
        entry = _entry(st, line_count=1, cwd="/work/repo")
        entry[field] += 1
        assert coverage_cache.lookup({str(t): entry}, str(t), st) is None, (
            f"a changed {field} alone must invalidate the entry")


def test_lookup_rejects_bools_and_non_int_stat_fields(tmp_path):
    """``True`` is an ``int``: read as ``stat_ino`` it would claim inode 1."""
    t = tmp_path / "t.jsonl"
    t.write_text("x\n", encoding="utf-8")
    st = t.lstat()

    for bad in (True, 1.0, "1", None):
        entry = _entry(st, line_count=1, cwd=None)
        entry["stat_dev"] = bad
        assert coverage_cache.lookup({str(t): entry}, str(t), st) is None, (
            f"stat_dev={bad!r} is not an exact int and must not be compared")


def test_lookup_rejects_a_missing_field_or_a_non_dict_entry(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text("x\n", encoding="utf-8")
    st = t.lstat()
    partial = _entry(st, line_count=1, cwd=None)
    del partial["stat_ino"]

    assert coverage_cache.lookup({str(t): partial}, str(t), st) is None
    assert coverage_cache.lookup({str(t): ["not", "a", "dict"]}, str(t), st) is None
    assert coverage_cache.lookup({}, str(t), st) is None


def test_lookup_rejects_a_payload_of_the_wrong_type(tmp_path):
    """A cwd that is not a str (or a line_count that is not an int) is an unusable
    answer — returning it would push the type error into the caller's decision."""
    t = tmp_path / "t.jsonl"
    t.write_text("x\n", encoding="utf-8")
    st = t.lstat()

    bad_cwd = _entry(st, line_count=1, cwd=None)
    bad_cwd["cwd"] = 42
    assert coverage_cache.lookup({str(t): bad_cwd}, str(t), st) is None

    bad_count = _entry(st, line_count=None, cwd=None)
    bad_count["line_count"] = "3"
    assert coverage_cache.lookup({str(t): bad_count}, str(t), st) is None

    # ...but a null CWD is a usable answer (the frozen goal pins it): "this read
    # completed and found no cwd".
    null_cwd = _entry(st, line_count=3, cwd=None)
    assert coverage_cache.lookup({str(t): null_cwd}, str(t), st) == null_cwd


def test_lookup_refuses_an_entry_whose_line_count_is_null(tmp_path):
    """NB1: the frozen entry shape permits ``line_count: null``, and E12 consumes this
    module API next wave. A null line count is indistinguishable from "not yet read",
    and serving it as a HIT binds ``total_lines = None`` in the sweep — which skips
    with zero opens and nothing to invalidate the entry. Every entry a seam-conformant
    writer emitted that way would be stranded forever. Ambiguity answers None."""
    t = tmp_path / "t.jsonl"
    t.write_text("x\n", encoding="utf-8")
    st = t.lstat()

    assert coverage_cache.lookup(
        {str(t): _entry(st, line_count=None, cwd="/work/repo")}, str(t), st) is None


# --------------------------------------------------------------------------- #
# coverage_cache.load / save — corrupt ≡ absent, and a write never aborts      #
# --------------------------------------------------------------------------- #
def test_load_treats_absent_corrupt_and_non_object_files_as_empty(tmp_path):
    assert coverage_cache.load(tmp_path / "nope.json") == {}

    garbage = tmp_path / "garbage.json"
    garbage.write_bytes(b"\x00\xff{{{ not json")
    assert coverage_cache.load(garbage) == {}

    a_list = tmp_path / "list.json"
    a_list.write_text("[1, 2, 3]", encoding="utf-8")
    assert coverage_cache.load(a_list) == {}, (
        "a non-object top level would be indexed by lookup(); it is a corrupt cache")

    a_dir = tmp_path / "dir.json"
    a_dir.mkdir()
    assert coverage_cache.load(a_dir) == {}


def test_save_swallows_write_errors_and_non_serializable_content(tmp_path):
    """The pass is holding the processor's flock and every decision it made is already
    final: a cache write must never be able to turn a good pass into a failed one."""
    occupied = tmp_path / "cache.json"
    occupied.mkdir()
    coverage_cache.save({"a": 1}, occupied)  # must not raise
    assert occupied.is_dir()

    fine = tmp_path / "ok.json"
    coverage_cache.save({"a": {1, 2}}, fine)  # a set is not JSON: must not raise
    assert not fine.exists()


def test_record_is_delivery_independent_and_holds_no_content(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text("secret payload\n", encoding="utf-8")
    st = t.lstat()
    cache: dict = {}

    coverage_cache.record(cache, str(t), st, 1, None)

    assert cache[str(t)] == _entry(st, line_count=1, cwd=None)
    assert "secret payload" not in json.dumps(cache), (
        "the cache holds counts, stats and a cwd — never transcript content (I5/I6)")


# --------------------------------------------------------------------------- #
# The registry snapshot — fail OPEN on anything that is not a usable registry  #
# --------------------------------------------------------------------------- #
def test_registry_shapes_that_disable_the_precheck(tmp_path):
    """Reading any of these as an EMPTY registry would silently withhold every
    transcript on the machine, and the sweep would look perfectly healthy."""
    cfg = _cfg(tmp_path)
    assert sweep._registry_remotes(cfg) is None, "absent file → disabled"

    for content in (b"\x00\xff{{{ not json", b"[]", b'"a string"',
                    b'{"fetched_at": "now"}', b'{"projects": "not-a-list"}',
                    b'{"projects": null}'):
        cfg.cache_path.write_bytes(content)
        assert sweep._registry_remotes(cfg) is None, (
            f"{content!r} is not a usable registry → precheck disabled (fail open)")


def test_registry_with_an_empty_project_list_is_enabled_and_empty(tmp_path):
    """Present + parseable is the whole condition. Zero registered projects is a
    legitimate answer ("nothing is registered"), NOT a broken cache."""
    cfg = _cfg(tmp_path)
    _registry(cfg, [])

    assert sweep._registry_remotes(cfg) == set()


def test_registry_drops_junk_members_without_failing_the_pass(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.cache_path.write_text(json.dumps({"fetched_at": "x", "projects": [
        {"canonical_remote": REGISTERED}, "not-a-dict", {"no_remote": 1},
        {"canonical_remote": None},
    ]}), encoding="utf-8")

    assert sweep._registry_remotes(cfg) == {REGISTERED}


def test_a_sweep_pass_never_writes_to_the_registry_cache(tmp_path):
    """AC-F2.4 at the unit level: read-only consumption, no refresh, no repair."""
    cfg = _cfg(tmp_path)
    _registry(cfg, [REGISTERED])
    before = (cfg.cache_path.read_bytes(), cfg.cache_path.stat().st_mtime_ns)
    _orphan(cfg, "/work/repo")

    sweep.run_sweep(cfg, {})

    assert (cfg.cache_path.read_bytes(), cfg.cache_path.stat().st_mtime_ns) == before


# --------------------------------------------------------------------------- #
# The per-cwd remote memo (cost, not correctness — but unbounded cost)         #
# --------------------------------------------------------------------------- #
def test_remote_derivation_is_memoized_once_per_distinct_cwd(tmp_path, monkeypatch):
    """The ineligible corpus is unbounded and every session of one project shares a
    cwd. Without the memo the precheck trades the per-pass READ cost this epic removes
    for a per-pass FORK cost — one ``git`` subprocess per candidate, every pass."""
    cfg = _cfg(tmp_path)
    repo_a = _repo(tmp_path / "repo_a", UNREGISTERED_URL)
    repo_b = _repo(tmp_path / "repo_b", UNREGISTERED_URL)
    _registry(cfg, [REGISTERED])
    for i in range(4):
        _orphan(cfg, str(repo_a), sid=f"sess-a-{i}", aged=3600 + i)
    for i in range(3):
        _orphan(cfg, str(repo_b), sid=f"sess-b-{i}", aged=4000 + i)

    calls: list[str] = []
    real = processor._git_remote
    monkeypatch.setattr(processor, "_git_remote",
                        lambda cwd: (calls.append(cwd), real(cwd))[1])

    report = sweep.run_sweep(cfg, {})

    assert (report.scanned, report.enqueued, report.ineligible) == (7, 0, 7)
    assert sorted(set(calls)) == sorted([str(repo_a), str(repo_b)])
    assert len(calls) == 2, (
        f"7 candidates over 2 distinct cwds must fork git twice, not 7 times; "
        f"calls={calls}")


def test_the_memo_does_not_survive_the_pass(tmp_path):
    """Anything remembered ACROSS passes is a tombstone: the same unchanged files must
    become eligible the moment the registry names their project."""
    cfg = _cfg(tmp_path)
    repo = _repo(tmp_path / "repo", REGISTERED_URL)
    _registry(cfg, ["github.com/example-group/somebody-else"])
    _orphan(cfg, str(repo), sid="sess-late")

    assert sweep.run_sweep(cfg, {}).ineligible == 1

    _registry(cfg, [REGISTERED])  # only the registry changes; the file does not.

    second = sweep.run_sweep(cfg, {})
    assert (second.enqueued, second.ineligible) == (1, 0)


def test_an_unnormalizable_remote_is_ineligible_not_a_crash(tmp_path):
    """``normalize`` raises ValueError on a remote it cannot parse; the processor
    drops such a marker, so the sweep withholds it rather than propagating."""
    cfg = _cfg(tmp_path)
    repo = _repo(tmp_path / "weird", "not-a-remote-at-all")
    _registry(cfg, [REGISTERED])
    _orphan(cfg, str(repo), sid="sess-weird")

    report = sweep.run_sweep(cfg, {})

    assert (report.enqueued, report.ineligible) == (0, 1)
    assert not list(cfg.queue_dir.glob("*.json"))


def test_an_unresolvable_cwd_is_named_in_the_log_not_only_counted(tmp_path):
    """L-1 (contract/ATTRIBUTION.md §6): the ONE ineligible class that is data loss
    gets a per-session line.

    A session that died without SessionEnd in a worktree that was then deleted has no
    marker and no captured remote, so the sweep asks the dead cwd the same question on
    every pass and withholds forever — while `registering the project later recovers the
    orphan`, the property the precheck advertises, is silently untrue for it. Before this
    line the only trace was `ineligible`, which an operator reads as routine."""
    cfg = _cfg(tmp_path)
    gone = tmp_path / "deleted-worktree"       # never created
    _registry(cfg, [REGISTERED])
    _orphan(cfg, str(gone), sid="sess-stranded")

    report = sweep.run_sweep(cfg, {})

    assert (report.enqueued, report.ineligible) == (0, 1)
    log = cfg.log_file.read_text(encoding="utf-8")
    assert "sweep_unresolvable_cwd" in log
    assert "sess-stranded" in log, "the line must name the session that is stranded"
    assert str(gone) in log, "and the cwd that can no longer answer"


def test_a_registered_miss_is_not_logged_per_session(tmp_path):
    """The other, routine half of `ineligible` stays quiet.

    `resolvable but not registered` is the expected majority — every transcript of every
    project the operator never registered. A per-session line for it would bury the one
    above, which is the only reason that one is worth having."""
    cfg = _cfg(tmp_path)
    repo = _repo(tmp_path / "repo", REGISTERED_URL)
    _registry(cfg, ["github.com/example-group/somebody-else"])
    _orphan(cfg, str(repo), sid="sess-unregistered")

    report = sweep.run_sweep(cfg, {})

    assert (report.enqueued, report.ineligible) == (0, 1)
    log = cfg.log_file.read_text(encoding="utf-8") if cfg.log_file.exists() else ""
    assert "sweep_unresolvable_cwd" not in log


# --------------------------------------------------------------------------- #
# The cache file's lifecycle                                                   #
# --------------------------------------------------------------------------- #
def test_the_cache_path_is_derived_beside_state_json_never_inside_it(tmp_path):
    cfg = _cfg(tmp_path)

    assert sweep.coverage_cache_path(cfg) == cfg.state_path.parent / "coverage_cache.json"
    assert sweep.coverage_cache_path(cfg) != cfg.state_path


def test_a_completed_read_records_the_cwd_even_with_no_new_content(tmp_path):
    """The half-filled-entry trap: this candidate is skipped at the line-count check,
    so a lazy implementation records ``cwd: null``. A later pass would then read that
    as "no cwd derivable", skip, and never re-read (the stat has not moved) — the file
    goes permanently invisible."""
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/work/repo")
    state = {str(t): {"last_line": 3, "last_uuid": f"{SID}-a1",
                      "updated_at": processor._now_iso()}}  # no stat fields

    report = sweep.run_sweep(cfg, state)

    assert (report.scanned, report.enqueued, report.skipped) == (1, 0, 1)
    entry = _cache(cfg)[str(t)]
    assert entry["line_count"] == 3 and entry["cwd"] == "/work/repo", (
        f"a completed read must record BOTH halves it produced; got {entry!r}")


def test_entries_for_vanished_transcripts_are_pruned(tmp_path):
    cfg = _cfg(tmp_path)
    keep = _orphan(cfg, "/work/repo", sid="sess-keep")
    gone = _orphan(cfg, "/work/repo", sid="sess-gone", aged=4000)

    sweep.run_sweep(cfg, {})
    assert set(_cache(cfg)) == {str(keep), str(gone)}

    gone.unlink()
    sweep.run_sweep(cfg, {})

    assert set(_cache(cfg)) == {str(keep)}, (
        "the cache must stay proportional to the transcripts that exist")


def test_an_empty_candidate_listing_never_wipes_the_cache(tmp_path):
    """A transcripts root that is missing or unmounted for one pass must cost nothing:
    wiping on a transient empty listing means re-reading the whole machine."""
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/work/repo")
    sweep.run_sweep(cfg, {})
    assert str(t) in _cache(cfg)

    cfg.sweep_transcripts_root = tmp_path / "does" / "not" / "exist"
    report = sweep.run_sweep(cfg, {})

    assert (report.scanned, report.enqueued, report.skipped) == (0, 0, 0)
    assert str(t) in _cache(cfg)


def test_the_cache_is_never_read_from_or_written_into_state_json(tmp_path):
    cfg = _cfg(tmp_path)
    _orphan(cfg, "/work/repo")
    state = {"pre": {"last_line": 1}}

    sweep.run_sweep(cfg, state)

    assert state == {"pre": {"last_line": 1}}, "state stays READ-ONLY to the sweep"
    assert not cfg.state_path.exists(), "the sweep never persists state.json"
    assert sweep.coverage_cache_path(cfg).exists()


# --------------------------------------------------------------------------- #
# The enriched record and its log line                                         #
# --------------------------------------------------------------------------- #
def test_the_pass_log_carries_the_new_counts_and_no_transcript_content(tmp_path):
    cfg = _cfg(tmp_path)
    repo = _repo(tmp_path / "repo", UNREGISTERED_URL)
    _registry(cfg, [REGISTERED])
    path = cfg.sweep_transcripts_root / ENCODED_DIR / "sess-log.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "uuid": "u1", "cwd": str(repo),
         "message": {"role": "user", "content": "SUPER-SECRET-PAYLOAD"}},
    ]) + "\n", encoding="utf-8")
    os.utime(path, (time.time() - 3600, time.time() - 3600))

    sweep.run_sweep(cfg, {})

    line = next(ln for ln in cfg.log_file.read_text(encoding="utf-8").splitlines()
                if "\tsweep_pass\t" in ln)
    assert "scanned=1 enqueued=0 skipped=1 stat_skips=0" in line, (
        f"the pre-existing columns keep their spelling and order; line={line!r}")
    assert "cache_hits=0 ineligible=1" in line
    assert "SUPER-SECRET-PAYLOAD" not in cfg.log_file.read_text(encoding="utf-8")


def test_not_scanned_is_reported_but_is_not_part_of_skipped(tmp_path):
    cfg = _cfg(tmp_path)
    for i in range(13):
        _orphan(cfg, "/work/repo", sid=f"sess-cap-{i:02d}", aged=3600 + i * 60)

    report = sweep.run_sweep(cfg, {})

    assert (report.scanned, report.enqueued, report.skipped) == (10, 10, 0)
    assert report.not_scanned == 3
    rec = json.loads(cfg.sweep_last_run_path.read_text(encoding="utf-8"))
    assert rec["enqueued"] + rec["skipped"] == rec["scanned"]
    assert rec["not_scanned"] == 3


def test_skip_classes_never_exceed_skipped_across_a_mixed_pass(tmp_path):
    """The partition property, stated as the invariant rather than as one fixture."""
    cfg = _cfg(tmp_path)
    repo_ok = _repo(tmp_path / "ok", REGISTERED_URL)
    repo_no = _repo(tmp_path / "no", UNREGISTERED_URL)
    _registry(cfg, [REGISTERED])
    statskip = _orphan(cfg, "/work/repo", sid="sess-statskip", aged=7000)
    cached = _orphan(cfg, "/work/repo", sid="sess-cached", aged=6000)
    _orphan(cfg, str(repo_no), sid="sess-inelig", aged=5000)
    _orphan(cfg, "/work/repo", sid="sess-unsettled", aged=1)
    _orphan(cfg, str(repo_ok), sid="sess-enq", aged=9000)
    st = statskip.lstat()
    state = {
        str(statskip): {"last_line": 3, "last_uuid": "sess-statskip-a1",
                        "updated_at": processor._now_iso(),
                        "stat_dev": st.st_dev, "stat_ino": st.st_ino,
                        "stat_mtime_ns": st.st_mtime_ns, "stat_size": st.st_size},
        str(cached): {"last_line": 3, "last_uuid": "sess-cached-a1",
                      "updated_at": processor._now_iso()},
    }
    sweep.coverage_cache_path(cfg).write_text(
        json.dumps({str(cached): _entry(cached.lstat(), line_count=3,
                                        cwd="/work/repo")}), encoding="utf-8")

    report = sweep.run_sweep(cfg, state)

    assert (report.scanned, report.enqueued, report.skipped) == (5, 1, 4)
    named = report.stat_skips + report.cache_hits + report.ineligible
    assert named <= report.skipped, "each skipped candidate belongs to ONE class"
    assert (report.stat_skips, report.cache_hits, report.ineligible) == (1, 1, 1)
    assert report.skipped - named == 1, "the unsettled candidate is the residue"


# --------------------------------------------------------------------------- #
# Wave-g review fixes — every one of these ends in a silently unswept file     #
# --------------------------------------------------------------------------- #
def test_b1_a_raising_git_remote_does_not_abort_the_pass(tmp_path, monkeypatch):
    """B1: ``_git_remote`` was the first unguarded raise path in the per-candidate
    loop, and it sits at the enqueue decision. ``run_once`` isolates the sweep, so an
    escape kills the PASS: no record, no cache save, and — because the candidate order
    is deterministic — every candidate behind it is never examined again, forever.

    Both confirmed triggers are exercised: an ``OSError`` whose errno escapes
    ``Path.is_dir``'s swallow list (macOS TCC returns EPERM for ~/Documents to a
    launchd-run process without Full Disk Access) and a ``UnicodeDecodeError`` from
    ``subprocess.run(text=True)`` on a ``.git/config`` with non-UTF-8 bytes."""
    for tag, boom in (
        ("EPERM", OSError(errno.EPERM, "Operation not permitted")),
        ("decode", UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")),
    ):
        cfg = _cfg(tmp_path / tag)
        repo_ok = _repo(tmp_path / tag / "ok", REGISTERED_URL)
        _registry(cfg, [REGISTERED])
        # Newest first: the exploding candidate is scanned BEFORE the deliverable one.
        _orphan(cfg, "/tcc/protected", sid="sess-boom", aged=3600)
        _orphan(cfg, str(repo_ok), sid="sess-behind", aged=9000)

        def exploding(cwd, _boom=boom):
            if cwd == "/tcc/protected":
                raise _boom
            return REGISTERED_URL

        monkeypatch.setattr(processor, "_git_remote", exploding)

        report = sweep.run_sweep(cfg, {})  # must not raise

        assert (cfg.queue_dir / "sess-behind.json").exists(), (
            f"{tag}: the candidate BEHIND the exploding one must still be examined — "
            f"an escaping exception starves the whole tail of the pass, deterministically")
        assert (report.scanned, report.enqueued) == (2, 1)
        assert report.ineligible == 1, (
            f"{tag}: 'cannot derive a remote' is already a first-class answer here, so "
            f"an unexpected failure must land on it")
        assert cfg.sweep_last_run_path.exists(), (
            f"{tag}: the pass must still record itself — doctor reads its age, so a "
            f"pass that dies silently looks like a sweep that never ran")
        assert str(_orphan_path(cfg, "sess-behind")) in _cache(cfg), (
            f"{tag}: the coverage cache must still be saved")


def test_b1_no_candidate_can_take_the_whole_pass_down(tmp_path, monkeypatch):
    """B1 (belt-and-braces): whatever a future call inside ``_sweep_one`` raises, the
    candidate is skipped and the pass carries on. The guard is at the loop, so it
    covers every line of the per-candidate decision, not just today's known raisers."""
    cfg = _cfg(tmp_path)
    _orphan(cfg, "/work/repo", sid="sess-boom", aged=3600)
    _orphan(cfg, "/work/repo", sid="sess-behind", aged=9000)
    real = sweep._line_count

    def boom(path):
        if path.stem == "sess-boom":
            raise RuntimeError("anything at all")
        return real(path)

    monkeypatch.setattr(sweep, "_line_count", boom)

    report = sweep.run_sweep(cfg, {})  # must not raise

    assert (cfg.queue_dir / "sess-behind.json").exists()
    assert (report.scanned, report.enqueued, report.skipped) == (2, 1, 1)
    assert report.enqueued + report.skipped == report.scanned, (
        "a candidate that blew up is a skip, so the pass arithmetic still balances")
    log = cfg.log_file.read_text(encoding="utf-8")
    assert "sweep_candidate_failed" in log and "RuntimeError" in log, (
        "a swallowed candidate failure must be visible in the log, never silent")


def test_b1_the_guard_never_swallows_test_instrumentation(tmp_path, monkeypatch):
    """The loop guard must not contain ``AssertionError``. The suite's read-guards
    around the stat skip work by replacing ``_line_count`` with a function that asserts
    it must not be called (``_forbid_line_count`` in test_sweep.py, plus two in
    test_sweep_process.py). A guard that swallows those turns nine "this must not read
    the file" tests into tests that pass whether or not the read happened — the pass
    still reports (1, 0, 1) either way, so nothing looks wrong. That is exactly the
    invisible regression this epic exists to prevent, so it is pinned here."""
    cfg = _cfg(tmp_path)
    _orphan(cfg, "/work/repo", sid="sess-guarded")

    def must_not_be_called(path):
        raise AssertionError(f"_line_count must not be called for {path}")

    monkeypatch.setattr(sweep, "_line_count", must_not_be_called)

    try:
        sweep.run_sweep(cfg, {})
    except AssertionError:
        return  # the instrumentation reached the test, as it must
    raise AssertionError(
        "run_sweep swallowed an AssertionError raised by test instrumentation — "
        "every read-guard in the suite is now vacuous")


def test_b2_an_unhashable_canonical_remote_is_dropped_not_fatal(tmp_path):
    """B2: ``_remotes_of`` builds a SET, so a ``canonical_remote`` that is a JSON array
    or object is unhashable → TypeError → out of ``_registry_remotes`` → out of
    ``run_sweep``, BEFORE a single candidate is scanned: zero scanned, no cache saved,
    no record written. The shape is server-controlled (this cache is written from
    ``resp.json()``), and the seam forbids exactly this outcome.

    The junk member is dropped and the usable ones survive — the same doctrine already
    applied to non-dict members. Fatal is the one outcome that is never acceptable."""
    for tag, bad in (("list", ["a", "b"]), ("dict", {"x": 1}), ("nested", [["a"]])):
        cfg = _cfg(tmp_path / tag)
        repo_ok = _repo(tmp_path / tag / "ok", REGISTERED_URL)
        cfg.cache_path.write_text(json.dumps({"fetched_at": "x", "projects": [
            {"id": 1, "canonical_remote": bad},
            {"id": 2, "canonical_remote": REGISTERED},
        ]}), encoding="utf-8")
        _orphan(cfg, str(repo_ok), sid=f"sess-{tag}-ok", aged=3600)
        _orphan(cfg, "/no/remote/here", sid=f"sess-{tag}-no", aged=9000)

        assert sweep._registry_remotes(cfg) == {REGISTERED}, (
            f"{tag}: the unusable member is dropped; the usable one still counts")

        report = sweep.run_sweep(cfg, {})  # must not raise

        assert (report.scanned, report.enqueued, report.ineligible) == (2, 1, 1), (
            f"{tag}: the pass must complete and the surviving registry entry must "
            f"still decide eligibility; report={report}")
        assert (cfg.queue_dir / f"sess-{tag}-ok.json").exists()
        assert cfg.sweep_last_run_path.exists(), (
            f"{tag}: a pass that died before scanning leaves no record at all")


def test_b2_an_exploding_registry_parse_disables_the_precheck(tmp_path, monkeypatch):
    """B2 (the outer net): whatever else a malformed registry can do to the derivation,
    it lands on "unparseable" — which the seam defines as DISABLED (fail open), never
    on a dead pass."""
    cfg = _cfg(tmp_path)
    _registry(cfg, [REGISTERED])
    _orphan(cfg, "/work/repo", sid="sess-open")
    monkeypatch.setattr(processor, "_remotes_of",
                        lambda projects: (_ for _ in ()).throw(TypeError("unhashable")))

    assert sweep._registry_remotes(cfg) is None

    report = sweep.run_sweep(cfg, {})  # must not raise

    assert report.enqueued == 1, (
        f"fail OPEN — today's behavior, the processor still owns the drop; "
        f"report={report}")


def test_b3_a_transient_read_failure_is_never_cached_as_no_cwd(tmp_path, monkeypatch):
    """B3: the line count and the cwd came from two separate ``open()`` calls, and
    ``read_cwd`` collapses "no cwd in this file" into the same ``None`` it returns for
    "I could not read this file". When the second open failed transiently (a chmod
    racing the pass, EIO/ESTALE on a network-mounted root, fd pressure) the pass
    recorded a real ``line_count`` beside a ``cwd: null`` that was never determined —
    and the stat is unchanged, so nothing ever invalidates it. Pre-phase-4 the next
    pass re-read and enqueued; with the cache the file is gone for good, counted as a
    healthy ``cache_hits``."""
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/work/repo", sid="sess-flaky")
    real_open = Path.open
    opens = {"n": 0}

    def flaky_open(self, *args, **kwargs):
        if str(self) == str(t):
            opens["n"] += 1
            if opens["n"] == 2:  # the cwd read; the line count already succeeded
                raise OSError(errno.EIO, "Input/output error")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", flaky_open)
    first = sweep.run_sweep(cfg, {})
    monkeypatch.undo()

    assert first.enqueued == 0, "harness: with no cwd derivable the pass cannot enqueue"
    assert str(t) not in coverage_cache.load(sweep.coverage_cache_path(cfg)), (
        "B3: the read did NOT complete, so there is nothing true to record — an entry "
        "here asserts 'this file has no cwd' on the strength of a failed read")

    second = sweep.run_sweep(cfg, {})  # the transient failure is gone

    assert second.enqueued == 1 and (cfg.queue_dir / "sess-flaky.json").exists(), (
        f"B3: the next pass must re-read and enqueue, exactly as it did pre-phase-4; "
        f"report={second}")
    assert second.cache_hits == 0, (
        "a suppressed read must never be counted as a healthy cache hit")
    assert _cache(cfg)[str(t)]["cwd"] == "/work/repo"


def test_b3_a_torn_tail_is_still_cached_as_no_cwd(tmp_path):
    """B3 (the other half): a ``UnicodeDecodeError`` must NOT suppress the record. A
    torn tail is a property of the BYTES, not of our ability to read them, so caching
    that ``None`` is correct — and it self-heals, because healing the tail moves the
    stat and invalidates the entry. Suppressing it would put this class straight back
    into the read-forever residual the epic exists to close."""
    cfg = _cfg(tmp_path)
    t = cfg.sweep_transcripts_root / ENCODED_DIR / "sess-torn.jsonl"
    t.parent.mkdir(parents=True, exist_ok=True)
    # Line 2 carries the undecodable bytes; line 3 is healthy, so the trailing-torn
    # hold does not confuse what `line_count` means here.
    t.write_bytes(json.dumps({"type": "user", "uuid": "u1", "message": {}}
                             ).encode("utf-8") + b"\n"
                  + b'{"type":"user","cwd":"/caf\xff/repo","message":{}}' + b"\n"
                  + json.dumps({"type": "user", "uuid": "u3", "cwd": "/late/repo",
                                "message": {}}).encode("utf-8") + b"\n")
    os.utime(t, (time.time() - 3600, time.time() - 3600))

    report = sweep.run_sweep(cfg, {})

    assert report.enqueued == 0, (
        "strict decode stops at the broken bytes, so the late cwd is never laundered "
        "into an enqueue")
    entry = _cache(cfg)[str(t)]
    assert entry["cwd"] is None and entry["line_count"] == 3, (
        f"a completed read over undecodable bytes IS a completed read; got {entry!r}")


def test_nb1_a_null_line_count_entry_is_re_read_not_stranded(tmp_path):
    """NB1 end to end: a cache entry with ``line_count: null`` and a matching stat —
    the shape the frozen seam permits and E12 may write next wave. Served as a hit it
    binds ``total_lines = None``, the candidate skips with zero opens, and nothing can
    ever invalidate it. It must be treated as "not yet read" instead."""
    cfg = _cfg(tmp_path)
    t = _orphan(cfg, "/work/repo", sid="sess-nullcount")
    sweep.coverage_cache_path(cfg).write_text(
        json.dumps({str(t): _entry(t.lstat(), line_count=None, cwd="/work/repo")}),
        encoding="utf-8")

    report = sweep.run_sweep(cfg, {})

    assert report.enqueued == 1 and (cfg.queue_dir / "sess-nullcount.json").exists(), (
        f"NB1: a null line count must force the full read, not strand the file; "
        f"report={report}")
    assert report.cache_hits == 0
    assert _cache(cfg)[str(t)]["line_count"] == 3, "the re-read repopulates the entry"


# --------------------------------------------------------------------------- #
# read_cwd, strict (P3.F4)                                                     #
# --------------------------------------------------------------------------- #
def test_read_cwd_stops_at_invalid_utf8_instead_of_skipping_the_line(tmp_path):
    """Skipping the undecodable line and continuing would be as wrong as laundering
    it: the bytes after a tear are not trustworthy content."""
    t = tmp_path / "bad.jsonl"
    bad = json.dumps({"type": "assistant", "uuid": "a1",
                      "message": {"content": "caf@@X@@"}}
                     ).encode("utf-8").replace(b"@@X@@", b"\xff")
    good = json.dumps({"type": "user", "uuid": "u1", "cwd": "/late",
                       "message": {}}).encode("utf-8")
    t.write_bytes(bad + b"\n" + good + b"\n")

    assert sweep.read_cwd(t) is None


def test_read_cwd_returns_a_cwd_found_before_the_broken_bytes(tmp_path):
    t = tmp_path / "torn.jsonl"
    good = json.dumps({"type": "user", "uuid": "u1", "cwd": "/early",
                       "message": {}}).encode("utf-8")
    t.write_bytes(good + b"\n" + b'{"text":"caf\xc3')  # torn mid-character, no newline

    assert sweep.read_cwd(t) == "/early"


def test_read_cwd_still_tolerates_crlf_and_junk_lines(tmp_path):
    t = tmp_path / "crlf.jsonl"
    t.write_bytes(b"\r\n{not json\r\n"
                  + json.dumps({"type": "user", "cwd": "/win", "message": {}}
                               ).encode("utf-8") + b"\r\n")

    assert sweep.read_cwd(t) == "/win"
