"""Loading of ``~/.loom/config.json`` and derivation of the client's paths (DESIGN.md §4.1).

Everything the processor and the CLI need from local disk is resolved
here out of ``LOOM_HOME``:

- ``LOOM_HOME`` = environment variable; defaults to ``~/.loom``. **Every** path
  (``queue/``, ``quarantine/``, ``failed/``, ``log/``, ``lock``, ``state.json``,
  ``projects_cache.json``, ``config.json``, ``prompts/summarizer_<version>.md``) derives
  from it.
- ``config.json`` (DESIGN §4.1; phase-2 keys are additive and optional):
  ``{"server_url", "token", "cache_ttl_s": 3600,
     "summarizer": {"model": "sonnet", "timeout_s": 300, "prompt_version": "v2"},
     "sweep": {"enabled": true, "settle_s": 1800, "transcripts_root": "~/.claude/projects"},
     "scanner": {"engine": "gitleaks", "gitleaks_version": "8.30.1"},
     "processor": {"tear_ratio_threshold": 0.5},
     "curation": {"candidate_k": 8, "timeout_s": 900, "digest_threshold": 15},
     "log": {"char_budget": 8000},
     "capture_mode": "opt_in" | "all_registered",
     "claude_config_dir": "~/.claude-personal"}``.
  ``capture_mode`` (D9) decides whether the local per-project gate is enforced. ABSENT
  means ``all_registered`` — the pre-D9 behaviour — so every config written before the
  key existed keeps loading and capturing exactly as it did. ``loom enroll`` writes
  ``opt_in``; ``loom enable`` / ``loom disable`` switch a machine to it.
  ``claude_config_dir`` is the Claude Code configuration directory this install serves:
  where install.sh put the hook, the ``CLAUDE.md`` block and the skills, where doctor
  looks for the hook, where the sweep finds transcripts, and the ``CLAUDE_CONFIG_DIR``
  the summarizer runs with. ABSENT means ``~/.claude``, Claude Code's own default. It is
  recorded rather than read from the environment because the periodic trigger and a
  plain terminal do not carry the ``CLAUDE_CONFIG_DIR`` a wrapper such as
  ``CLAUDE_CONFIG_DIR=~/.claude-x claude`` sets for Claude Code alone.
  Two extra keys, documented here, exist for testability:
    * ``claude_bin``  — summarizer binary (default ``"claude"``); in tests it points at
      a stub.
    * ``prompt_path`` — prompt path (default
      ``<LOOM_HOME>/prompts/summarizer_<prompt_version>.md``); lets tests pin the prompt
      text without depending on the installed layout.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

LOOM_HOME_ENV = "LOOM_HOME"
DEFAULT_LOOM_HOME = "~/.loom"

DEFAULT_CACHE_TTL_S = 3600
DEFAULT_MODEL = "sonnet"

# D9 — capture opt-in. ``all_registered`` is what an ABSENT key means (legacy, and what
# the frozen waves' hand-written configs get); ``opt_in`` is what the documented onboarding
# writes. A value that is neither lands on ``opt_in``: of the two ways a hand edit can go
# wrong, capturing less is the recoverable one (entries are append-only, D7).
CAPTURE_MODE_OPT_IN = "opt_in"
CAPTURE_MODE_ALL_REGISTERED = "all_registered"
CAPTURE_MODES = (CAPTURE_MODE_OPT_IN, CAPTURE_MODE_ALL_REGISTERED)
DEFAULT_CAPTURE_MODE = CAPTURE_MODE_ALL_REGISTERED
DEFAULT_TIMEOUT_S = 300
# Phase 4 (T.F3): fresh configs capture with the TYPED prompt. The version is what
# selects the prompt file (see :func:`prompt_filename`) and it is already part of
# ``compute_content_hash``'s material, so a pilot moving v1 → v2 re-summarizes without
# colliding with its own v1 entries. A config that pins "v1" keeps resolving
# ``summarizer_v1.md`` — install.sh ships both.
DEFAULT_PROMPT_VERSION = "v2"
# What a FRESH `loom enroll` writes (D11, then D15: v4 is v3 written in English).
# Onboarding moves forward while the constructor default above stays v2 — the D9 pattern: the frozen default is what a hand-built or
# pre-D11 config resolves to, and changing it would silently move every existing
# machine whose config omits the key. An enrolment that finds a config keeps its value.
ENROLL_PROMPT_VERSION = "v4"
DEFAULT_CLAUDE_BIN = "claude"
# Claude Code's configuration directory when CLAUDE_CONFIG_DIR is not set.
DEFAULT_CLAUDE_CONFIG_DIR = "~/.claude"
CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"

# A prompt version is a bare token: it is interpolated into a FILENAME under
# ``<LOOM_HOME>/prompts/``. config.json is hand-editable, and the resolved file is read
# and pasted verbatim into the ``claude -p`` argument (I3), so a value carrying a path
# separator ("../../.ssh/id_rsa") would turn a config typo into an arbitrary-file read
# whose content leaves the machine inside a prompt. Anything that is not this shape
# resolves to the default version instead; pointing the summarizer at a file elsewhere
# on purpose is what the explicit ``prompt_path`` key is for.
_PROMPT_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")

# Phase 2 (R2.1): orphan-transcript sweep. The settle threshold is a liveness
# heuristic, not a guarantee (DESIGN.md §12, Q-2.2); transcripts_root is
# overridable so tests never touch the real ~/.claude tree. Absent, it is the
# ``projects`` directory of the Claude Code configuration directory.
DEFAULT_SWEEP_ENABLED = True
DEFAULT_SWEEP_SETTLE_S = 1800

# Phase 2 (R2.3) scanner engine selection, changed by D16 (2026-10-15): gitleaks is
# mandatory for capture. "gitleaks" is the default; "auto" is read as a legacy alias of
# it (no fallback); "regex" is refused — capture pauses and doctor shows ✗.
# gitleaks_version pins the expected binary (None = unpinned, doctor warns); init and
# enroll write the pin install.sh downloads (scan_engine.GITLEAKS_PINNED_VERSION).
DEFAULT_SCANNER_ENGINE = "gitleaks"
DEFAULT_GITLEAKS_VERSION: str | None = None

# Phase 3/P3.F5 (E10): the torn/(torn+parsed) ratio at or above which a processed
# delta emits the REPORT-ONLY ``tear_ratio_high`` run-log event. Conservative on
# purpose — the event is a diagnostic, and half a delta lost to tears is already an
# unusual transcript. Additive key (p2-config-additive-keys): an absent
# ``processor`` section, an empty one, or an out-of-domain value all behave exactly
# as they did before the key existed.
DEFAULT_TEAR_RATIO_THRESHOLD = 0.5


# Phase 4 (T.F5, E15): batch curation. `candidate_k` is how many FTS5 neighbours are
# offered to the curation LLM as candidates for ONE new entry — the whole cost knob of
# the pass (payload size, and therefore tokens). Additive key
# (p2-config-additive-keys): an absent `curation` section behaves exactly as it did
# before the key existed.
DEFAULT_CANDIDATE_K = 8
# Ceiling for `candidate_k`. Every candidate is a line of entry text in a payload the
# operator pays for by the token, and a hand-edited `"candidate_k": 100000` would build
# one enormous prompt per new entry before anything downstream could object.
MAX_CANDIDATE_K = 50

# Wall clock for ONE batched curation pass. Its OWN key, not `summarizer.timeout_s`:
# that one budgets a single session delta (300s), while a curation call carries up to a
# hundred entry bodies plus `candidate_k` previews each. Reusing the smaller number is a
# loop with no exit — the pass times out, the watermark freezes, and the next run
# rebuilds exactly the same oversized batch. Additive key (p2-config-additive-keys).
DEFAULT_CURATION_TIMEOUT_S = 900
# Bounds. A pass that cannot finish in an hour will not finish; a `0` would make every
# pass fail instantly and freeze the watermark, which is the failure this key exists to
# prevent.
MIN_CURATION_TIMEOUT_S = 30
MAX_CURATION_TIMEOUT_S = 3600

# Phase 4 (T.F6, E16): how many NEW current entries a project must accumulate since its
# last digest before a curate pass pays for a digest LLM call. The whole cost gate of
# T.F6 — below it there is no call at all and the watermark is untouched. Additive key
# (p2-config-additive-keys), read out of the same `curation` section.
DEFAULT_DIGEST_THRESHOLD = 15
MIN_DIGEST_THRESHOLD = 1
# The count is taken over ONE page of the project's entry listing, which the server
# clamps at 500 rows: a threshold above that could never be reached, so it does not mean
# "digest rarely", it means "never" — a promise this key does not make. Out-of-domain
# values resolve to the default, exactly like every other additive key here; an operator
# who really wants no digests should not curate.
MAX_DIGEST_THRESHOLD = 500

# Phase 4 (T.F6, E16): hard character cap on ONE `loom log` / `/loom` rendering. The
# logbook is injected into an agent's context, so "memory never eats the context" is the
# whole point of the feature: the framing and the digest are printed first and the ENTRY
# list is what gets cut, with an explicit marker. Additive key, its own `log` section.
DEFAULT_LOG_CHAR_BUDGET = 8000
# The floor is not cosmetic: the I5 framing and the truncation marker are the two things
# the renderer may never drop, so a budget must be able to hold both with room for at
# least a little content. Below it the cap would silently stop meaning what it says.
MIN_LOG_CHAR_BUDGET = 1000
# Past this the budget no longer bounds anything an agent can read; a value that large is
# a typo (a byte count meant as something else), not an intention.
MAX_LOG_CHAR_BUDGET = 1_000_000


def _bounded_int(raw: object, default: int, low: int, high: int) -> int:
    """``raw`` when it is an int inside ``[low, high]``, else ``default``. TOTAL.

    The one rule behind every integer knob in this file, written once. Each of them
    comes out of a hand-editable ``config.json``, and a config that refuses to load
    stops far more than the feature the key belongs to — so an unusable value resolves
    to the documented default instead of raising.

    ``bool`` is rejected first because it is an ``int`` subclass (``true`` would read as
    1), and floats are rejected rather than truncated: ``8.9`` is a typo, not a pool
    size. The bounds are per-key and each one is justified where it is defined; what is
    shared is only the discipline.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return default
    return raw if low <= raw <= high else default


def _digest_threshold(raw: object) -> int:
    """The configured digest threshold, or the default for anything unusable.

    ``0`` and negatives are out of domain deliberately — a threshold of 0 would
    regenerate (and pay for) a digest on every single curate pass forever.
    """
    return _bounded_int(raw, DEFAULT_DIGEST_THRESHOLD,
                        MIN_DIGEST_THRESHOLD, MAX_DIGEST_THRESHOLD)


def _log_char_budget(raw: object) -> int:
    """The configured ``loom log`` character budget, or the default if unusable."""
    return _bounded_int(raw, DEFAULT_LOG_CHAR_BUDGET,
                        MIN_LOG_CHAR_BUDGET, MAX_LOG_CHAR_BUDGET)


def _curation_timeout_s(raw: object) -> int:
    """The configured curation timeout, or the default for anything unusable."""
    return _bounded_int(raw, DEFAULT_CURATION_TIMEOUT_S,
                        MIN_CURATION_TIMEOUT_S, MAX_CURATION_TIMEOUT_S)


def _candidate_k(raw: object) -> int:
    """The configured candidate pool size, or the default for anything unusable."""
    return _bounded_int(raw, DEFAULT_CANDIDATE_K, 1, MAX_CANDIDATE_K)


def _tear_ratio_threshold(raw: object) -> float:
    """The configured tear ratio threshold, or the default for anything unusable.

    The value comes from a hand-editable file, so every non-value it can hold —
    a string, ``null``, a list, ``true`` (an ``int`` subclass in Python, which is
    why the ``bool`` check comes first), ``NaN``, or a number outside the frozen
    ``(0, 1]`` domain — resolves to the default rather than raising. A config that
    refuses to load stops the processor from capturing anything at all, and this
    key only controls whether a diagnostic line is written: it must never be able
    to cost a capture. ``0`` is excluded deliberately — it would fire on every
    delta with a single tear and drown the very signal it exists to raise.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return DEFAULT_TEAR_RATIO_THRESHOLD
    value = float(raw)
    if not 0.0 < value <= 1.0:  # False for NaN too.
        return DEFAULT_TEAR_RATIO_THRESHOLD
    return value


def normalize_prompt_version(prompt_version: object) -> str:
    """The CANONICAL prompt version — the one value everything downstream must use.

    Normalizing at one point and reading the raw value everywhere else is worse than
    not normalizing at all, because the two then disagree silently:

    - ``"v1 "`` (a stray space in hand-edited JSON) is not a token, so the filename
      falls back to the default and the machine captures with **v2** while stamping
      every entry ``prompt: "v1 "`` — an operator who rolled back to v1 keeps getting
      v2 summaries and the entry's own provenance says otherwise.
    - ``"V1"`` IS token-shaped, so it survives; on a case-insensitive filesystem
      (APFS, NTFS) ``summarizer_V1.md`` opens ``summarizer_v1.md``, so the UNLABELLED
      prompt runs while the label parse, keyed on the literal ``"v1"``, treats the
      output as v2 and types every bullet ``[decision]``.

    Both disappear if the version is resolved ONCE, here, and that resolved value is
    what names the file, stamps the frontmatter, feeds ``content_hash`` and selects
    the parse. Whitespace is stripped and case is folded because those are typos, not
    intentions; anything still not a bare token (see :data:`_PROMPT_VERSION_RE`) is
    not a version at all and resolves to the default.
    """
    if isinstance(prompt_version, str):
        candidate = prompt_version.strip().lower()
        if _PROMPT_VERSION_RE.fullmatch(candidate):
            return candidate
    return DEFAULT_PROMPT_VERSION


def prompt_filename(prompt_version: object) -> str:
    """``summarizer_<version>.md`` — the prompt file a version resolves to (T.F3).

    Version-derived rather than a fixed name so that a pilot upgrade is ONE key
    (``summarizer.prompt_version``) instead of two that can silently disagree: a
    config on ``"v2"`` still reading ``summarizer_v1.md`` emits untyped summaries
    under a v2 ``content_hash``, which is the silent corruption T.F3 exists to avoid.

    Note that this closes the *derived* disagreement, not every one: an explicit
    ``prompt_path`` still overrides the derivation, so a config that pins
    ``prompt_version: "v2"`` **and** a ``prompt_path`` pointing at the v1 file gets
    exactly that corruption — v1 output parsed as v2, every bullet typed
    ``[decision]``. The key is documented as a testability override for that reason;
    ``loom doctor`` prints the resolved path so the two can be compared.
    """
    return f"summarizer_{normalize_prompt_version(prompt_version)}.md"


def _optional_str(raw: object) -> str | None:
    """``raw`` when it is a non-empty string, else ``None`` (a hand-edited key never crashes)."""
    return raw if isinstance(raw, str) and raw else None


def _capture_mode(raw: object) -> str:
    """``raw`` when it is one of :data:`CAPTURE_MODES`; absent → legacy; garbage → opt-in."""
    if raw is None:
        return DEFAULT_CAPTURE_MODE
    return raw if raw in CAPTURE_MODES else CAPTURE_MODE_OPT_IN


def get_loom_home() -> Path:
    """Resolved ``LOOM_HOME`` (env var or ``~/.loom``), with ``~`` expanded."""
    return Path(os.environ.get(LOOM_HOME_ENV) or DEFAULT_LOOM_HOME).expanduser()


@dataclass
class Config:
    """Client config + paths derived from ``loom_home``.

    Normally built via :meth:`load`, which reads ``config.json``. It can also be
    instantiated directly (tests). The paths are derived *properties*: changing
    ``loom_home`` moves the whole state tree.
    """

    loom_home: Path
    server_url: str
    token: str
    cache_ttl_s: int = DEFAULT_CACHE_TTL_S
    model: str = DEFAULT_MODEL
    timeout_s: int = DEFAULT_TIMEOUT_S
    prompt_version: str = DEFAULT_PROMPT_VERSION
    claude_bin: str = DEFAULT_CLAUDE_BIN
    prompt_path: Path | None = None
    sweep_enabled: bool = DEFAULT_SWEEP_ENABLED
    sweep_settle_s: int = DEFAULT_SWEEP_SETTLE_S
    sweep_transcripts_root: Path | None = None
    claude_config_dir: Path | None = None
    scanner_engine: str = DEFAULT_SCANNER_ENGINE
    scanner_gitleaks_version: str | None = DEFAULT_GITLEAKS_VERSION
    tear_ratio_threshold: float = DEFAULT_TEAR_RATIO_THRESHOLD
    curation_candidate_k: int = DEFAULT_CANDIDATE_K
    curation_timeout_s: int = DEFAULT_CURATION_TIMEOUT_S
    curation_digest_threshold: int = DEFAULT_DIGEST_THRESHOLD
    log_char_budget: int = DEFAULT_LOG_CHAR_BUDGET
    # Device token lifetime (issue #20). Both absent = a legacy permanent `adduser`
    # token, which is what every config written before enrolment existed carries.
    token_issued_at: str | None = None
    token_expires_at: str | None = None
    # D9: whether the per-machine, per-project gate is enforced (see module docstring).
    capture_mode: str = DEFAULT_CAPTURE_MODE

    def __post_init__(self) -> None:
        self.loom_home = Path(self.loom_home).expanduser()
        # Resolved ONCE, before anything reads it: this value names the prompt file,
        # stamps the entry's ``prompt:`` provenance, enters ``content_hash``'s
        # material and selects whether the label parse runs. They must all be the
        # same string — see :func:`normalize_prompt_version`.
        self.prompt_version = normalize_prompt_version(self.prompt_version)
        # An explicit ``prompt_path`` always wins (tests, and any hand-pinned prompt);
        # otherwise the file is DERIVED from the prompt version (T.F3).
        self.prompt_path = (
            Path(self.prompt_path)
            if self.prompt_path is not None
            else self.loom_home / "prompts" / prompt_filename(self.prompt_version)
        )
        if self.claude_config_dir is not None:
            self.claude_config_dir = Path(self.claude_config_dir).expanduser()
            # Spelled out, the default stays "absent": Claude Code does not treat
            # CLAUDE_CONFIG_DIR=~/.claude exactly as unset (where it keeps .claude.json),
            # so the summarizer must not be handed it.
            if self.claude_config_dir == Path(DEFAULT_CLAUDE_CONFIG_DIR).expanduser():
                self.claude_config_dir = None
        self.sweep_transcripts_root = (
            Path(self.sweep_transcripts_root).expanduser()
            if self.sweep_transcripts_root is not None
            else self.claude_home / "projects"
        )

    @property
    def claude_home(self) -> Path:
        """The Claude Code configuration directory this install serves."""
        return self.claude_config_dir or Path(DEFAULT_CLAUDE_CONFIG_DIR).expanduser()

    def claude_env(self) -> dict[str, str]:
        """The environment a ``claude`` subprocess runs with: this process's, with
        ``CLAUDE_CONFIG_DIR`` set to the directory the install serves, or removed when
        that is ``~/.claude``. Never inherited: a pass started from inside another
        profile's session (its wrapper exports the variable to every command) must still
        sign in with the account the hook was installed for."""
        env = dict(os.environ)
        if self.claude_config_dir is not None:
            env[CLAUDE_CONFIG_DIR_ENV] = str(self.claude_config_dir)
        else:
            env.pop(CLAUDE_CONFIG_DIR_ENV, None)
        return env

    # --- Derived paths -----------------------------------------------------------
    @property
    def queue_dir(self) -> Path:
        return self.loom_home / "queue"

    @property
    def quarantine_dir(self) -> Path:
        return self.loom_home / "quarantine"

    @property
    def failed_dir(self) -> Path:
        return self.loom_home / "failed"

    @property
    def log_dir(self) -> Path:
        return self.loom_home / "log"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "processor.log"

    @property
    def lock_path(self) -> Path:
        return self.loom_home / "lock"

    @property
    def state_path(self) -> Path:
        return self.loom_home / "state.json"

    @property
    def cache_path(self) -> Path:
        return self.loom_home / "projects_cache.json"

    @property
    def config_path(self) -> Path:
        return self.loom_home / "config.json"

    @property
    def projects_local_path(self) -> Path:
        """Per-machine, per-project capture state (D9): ``{canonical_remote: {state, since}}``.

        Under LOOM_HOME and never inside a repository, so it cannot be committed by
        accident and survives a re-clone of the project.
        """
        return self.loom_home / "projects_local.json"

    @property
    def auth_rejected_path(self) -> Path:
        """Record of the last credential rejection on an unattended path (issue #20)."""
        return self.loom_home / "auth_rejected.json"

    @property
    def scanner_paused_path(self) -> Path:
        """Record of the last pass the D16 scanner gate paused (cleared by a healthy one)."""
        return self.loom_home / "scanner_paused.json"

    @property
    def sweep_last_run_path(self) -> Path:
        return self.loom_home / "sweep_last_run.json"

    @property
    def curation_state_path(self) -> Path:
        """Per-project curation watermarks (t-curation-watermark, E15).

        Its OWN file, never ``state.json``: that one is the capture pipeline's pointer
        state, written under the PROCESSOR's lock on every pass, and a curation pass
        that shared it would either have to take that lock (blocking capture behind an
        LLM call) or race a writer that knows nothing about it.
        """
        return self.loom_home / "curation_state.json"

    @property
    def curate_lock_path(self) -> Path:
        """Exclusive lock for ``loom curate`` — its OWN lock, for the same reason."""
        return self.loom_home / "curate.lock"

    @property
    def attention_path(self) -> Path:
        """The processor-owned attention record (P3.F5, ``p4-attention-record``).

        Derived from :attr:`state_path` rather than from ``loom_home`` directly:
        the record is the pointer state's diagnostic companion — one entry per
        session whose transcript is currently wholly unparseable — and the two must
        never be able to drift into different directories.
        """
        return self.state_path.parent / "attention.json"

    # --- Helpers -----------------------------------------------------------------
    def ensure_dirs(self) -> None:
        """Creates the work directories (idempotent)."""
        for directory in (self.queue_dir, self.quarantine_dir, self.failed_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def read_prompt(self) -> str:
        """Text of the summarizer prompt (passed as an argument to ``claude -p``)."""
        return self.prompt_path.read_text(encoding="utf-8")

    @classmethod
    def load(cls, loom_home: str | Path | None = None) -> "Config":
        """Loads ``<LOOM_HOME>/config.json``.

        An explicit ``loom_home`` wins; otherwise :func:`get_loom_home` is used.
        Raises ``FileNotFoundError`` if there is no config, ``KeyError`` if
        ``server_url``/``token`` are missing.
        """
        home = Path(loom_home).expanduser() if loom_home is not None else get_loom_home()
        data = json.loads((home / "config.json").read_text(encoding="utf-8"))
        summarizer = data.get("summarizer") or {}
        sweep = data.get("sweep") or {}
        scanner = data.get("scanner") or {}
        # Additive section (P3.F5). ``or {}`` covers null; the isinstance guard covers
        # a section a human replaced with a scalar or a list — neither may make the
        # config unloadable, because that would stop every capture.
        proc = data.get("processor") or {}
        if not isinstance(proc, dict):
            proc = {}
        # Additive section (T.F5), guarded exactly like `processor` above.
        curation = data.get("curation") or {}
        if not isinstance(curation, dict):
            curation = {}
        # Additive section (T.F6), guarded exactly like the two above.
        log_section = data.get("log") or {}
        if not isinstance(log_section, dict):
            log_section = {}
        prompt_path = data.get("prompt_path")
        transcripts_root = sweep.get("transcripts_root")
        claude_config_dir = data.get("claude_config_dir")
        return cls(
            loom_home=home,
            server_url=data["server_url"],
            token=data["token"],
            cache_ttl_s=data.get("cache_ttl_s", DEFAULT_CACHE_TTL_S),
            model=summarizer.get("model", DEFAULT_MODEL),
            timeout_s=summarizer.get("timeout_s", DEFAULT_TIMEOUT_S),
            prompt_version=summarizer.get("prompt_version", DEFAULT_PROMPT_VERSION),
            claude_bin=data.get("claude_bin", DEFAULT_CLAUDE_BIN),
            prompt_path=Path(prompt_path) if prompt_path else None,
            sweep_enabled=sweep.get("enabled", DEFAULT_SWEEP_ENABLED),
            sweep_settle_s=sweep.get("settle_s", DEFAULT_SWEEP_SETTLE_S),
            sweep_transcripts_root=Path(transcripts_root) if transcripts_root else None,
            claude_config_dir=(Path(claude_config_dir)
                               if isinstance(claude_config_dir, str) and claude_config_dir
                               else None),
            scanner_engine=scanner.get("engine", DEFAULT_SCANNER_ENGINE),
            scanner_gitleaks_version=scanner.get(
                "gitleaks_version", DEFAULT_GITLEAKS_VERSION
            ),
            tear_ratio_threshold=_tear_ratio_threshold(
                proc.get("tear_ratio_threshold", DEFAULT_TEAR_RATIO_THRESHOLD)
            ),
            curation_candidate_k=_candidate_k(
                curation.get("candidate_k", DEFAULT_CANDIDATE_K)
            ),
            curation_timeout_s=_curation_timeout_s(
                curation.get("timeout_s", DEFAULT_CURATION_TIMEOUT_S)
            ),
            curation_digest_threshold=_digest_threshold(
                curation.get("digest_threshold", DEFAULT_DIGEST_THRESHOLD)
            ),
            log_char_budget=_log_char_budget(
                log_section.get("char_budget", DEFAULT_LOG_CHAR_BUDGET)
            ),
            token_issued_at=_optional_str(data.get("token_issued_at")),
            token_expires_at=_optional_str(data.get("token_expires_at")),
            capture_mode=_capture_mode(data.get("capture_mode")),
        )


def persist_capture_mode(cfg: Config, mode: str) -> None:
    """Write ``capture_mode`` into ``config.json`` (atomic, 0600: the file holds the token).

    Re-reads the file rather than serializing ``cfg``: the dataclass does not carry every
    key a human may have added, and none of them may be lost over a mode switch.
    """
    if mode not in CAPTURE_MODES:
        raise ValueError(f"unknown capture mode: {mode!r}")
    data = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("config.json is not a JSON object")
    data["capture_mode"] = mode
    cfg.config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config.json.", dir=cfg.config_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, cfg.config_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    cfg.capture_mode = mode
