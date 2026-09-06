"""Shared runtime cost ledger for paid-API roles.

Converts adapter token usage to USD, enforces daily budget caps on
`tier: api` roles, and appends events to `logs/cost-tracking.jsonl`. CLI
roles (`cost_per_mtok_*: 0` in `models.yaml`) compute to $0 and bypass the
cap without a special case.

One ledger instance per process is shared across `chat_brain` and
`observer_agent`. Daily totals are grouped by local-tz date; boot re-seeds
by replaying today's JSONL entries. `check_preflight(role)` raises
`BudgetExhausted` when either the role sub-cap or the global cap is hit.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable, ClassVar, Mapping

import yaml

from tesseract.brain.cost import overage as _overage

logger = logging.getLogger(__name__)

# Cost UX overhaul (2026-04-27): instead of a hard cut at 100% of cap, the
# operator gets two distinct surfaces:
#   1. WARNING — at `warning_at_pct` of any cap (global / per-role /
#      per-voice-provider), a one-shot toast fires for the day. Spend
#      continues uninterrupted.
#   2. OVERAGE ASK — at 100%, the chat path raises `BudgetExhausted`; the
#      WS layer surfaces a confirm card ("continue today? extra spend will
#      show in red"). On approve, the ledger unlocks that scope for the
#      rest of the local-tz day. On deny, the turn aborts with the toast.
# Both flags are scope-keyed (`global`, `role:<name>`, `voice:<kind>:<provider>`)
# and reset on midnight rollover. The overage approval is WRITTEN DOWN
# (`cost/overage.py`), stamped with the day it belongs to; the warning flag is
# not, because re-showing a toast after a restart costs nothing. It was
# memory-only on the reasoning that a restart should re-ask, and that reasoning
# cost a day: an approval given minutes after a breaker tripped on the cap was
# invisible to every other reader, which went on believing the cap still
# refused. The day stamp is what makes persisting it safe — a file from
# yesterday answers for yesterday and is ignored.
# `warning_at_pct` lives in models.yaml so the operator can tune it per
# project without a code change.

from tesseract.orchestrator.turns.context import turn_identity as _turn_identity
from tesseract.paths import CONFIG_DIR as _CONFIG_DIR, home_dir as _home_dir

_DEFAULT_PROVIDERS_YAML = _CONFIG_DIR / "providers.yaml"
_DEFAULT_ROLES_YAML = _CONFIG_DIR / "roles.yaml"


class BudgetExhausted(Exception):
    """Raised by `CostLedger.check_preflight` when a cap is hit.

    Used for both role budgets (chat / observer) and per-provider voice
    budgets. `scope` is `"role"`, `"global"`, or `"voice"`. A voice
    exhaustion surfaces directly to the WS handler, which emits a
    `voice_instruction` toast: the engine deliberately does not route
    around a cap, because a cap is a decision rather than a fault.
    """

    def __init__(self, role: str, spent_usd: float, cap_usd: float, scope: str) -> None:
        self.role = role
        self.spent_usd = spent_usd
        self.cap_usd = cap_usd
        self.scope = scope  # "role" | "global" | "voice"
        super().__init__(
            f"{scope} budget exhausted for role={role}: "
            f"spent ${spent_usd:.4f} / cap ${cap_usd:.4f}"
        )

    def as_sentence(self) -> str:
        """The refusal in plain words, composed where the numbers are known.

        `str(self)` is machine text: it names a scope and a role and spells the
        amounts to four places. It reached a phone verbatim, was caught by the
        channel's leak filter because it contains a slash, and arrived as
        "I hit an error processing that. Try again?" — so a deliberate refusal
        with a known cause read as a fault with none, and the work stopped with
        nothing to act on.

        Says the consequence and then the remedy, and carries no path, so
        nothing downstream has to guess whether it is safe to send.
        """
        return (
            f"{self._spent_against_cap()}, so this turn did not run. Approve "
            f"going over, or it resets at midnight."
        )

    def as_question(self) -> str:
        """The same numbers, put as the decision they actually are.

        `as_sentence` is what a person reads when nobody could be asked. This
        is what they read when they can answer, on whatever surface they are
        standing on. The cap is a signal rather than a wall and the choice is
        theirs, so this says what each answer does, including the part they
        need before they can answer at all: a yes opens the rest of the day
        for this scope, not just the turn in front of them.
        """
        return (
            f"{self._spent_against_cap()}. Going over is your call. Approve "
            f"and this turn runs, and the rest of today stays open for it. "
            f"Reject and it waits until midnight."
        )

    def _spent_against_cap(self) -> str:
        """What ran out and by how much. No slash: the channel's leak filter
        reads one as a path marker, which is what ate this message live."""
        if self.scope == "global":
            what = "Today's total budget"
        elif self.scope == "voice":
            what = "Today's voice budget"
        else:
            what = f"Today's budget for {self.role}"
        return (
            f"{what} is spent: ${self.spent_usd:.2f} against a cap of "
            f"${self.cap_usd:.2f}"
        )

    def scope_key(self) -> str:
        """Derived stable key for `unlock_overage()` / overage-ask
        envelopes. Mirrors the format documented above
        `CostLedger.check_warning`: 'global' / 'role:<name>' /
        'voice:<kind>:<provider>'. Voice callers populate `role` as
        'voice:<kind>:<provider>' which we surface verbatim here."""
        if self.scope == "global":
            return "global"
        if self.scope == "voice":
            return self.role  # already 'voice:<kind>:<provider>'
        return f"role:{self.role}"


@dataclass(frozen=True)
class CostUsage:
    """Adapter → ledger DTO. Mirrors the STOP chunk `raw['usage']` shape.

    Named `CostUsage` (not `TokenUsage`) to avoid collision with
    `tesseract.kernel.state.TokenUsage`, which carries `total_tokens` rather
    than `cached_tokens` and lives on `LoopState`.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    # None means the provider did not report the class at all; 0 means it
    # reported none. Pricing treats both as zero today, but only the first is
    # a reason to go and look at the adapter, and collapsing them is how
    # `cache_creation_tokens` read as a settled zero on 236M cached tokens
    # while the field was simply never extracted.
    cached_tokens: int | None = None
    # Tokens written to the prompt cache during this turn. Anthropic reports it
    # directly. OpenAI implicit caching reports nothing and bills non-cached
    # input as a write, which is a pricing rule and lives with the catalog, not
    # here.
    cache_creation_tokens: int | None = None
    # Already inside `output_tokens` on the Responses API, so it is recorded
    # rather than billed: it says how much of an answer was thinking.
    reasoning_tokens: int | None = None

    @classmethod
    def from_raw(cls, usage: dict) -> "CostUsage":
        """Build from a STOP chunk's `raw["usage"]`.

        One constructor because three callers had written the same coercion by
        hand (`chat.py`, `metered_adapter.py`, `observer.py`) and all three
        spelled a missing key as `0`. A key absent here stays `None`, which is
        the whole point of the field: `int(x or 0)` is what made an unextracted
        class indistinguishable from a real zero.
        """
        def _opt(key: str) -> int | None:
            value = usage.get(key)
            return None if value is None else int(value)

        return cls(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cached_tokens=_opt("cached_tokens"),
            cache_creation_tokens=_opt("cache_creation_tokens"),
            reasoning_tokens=_opt("reasoning_tokens"),
        )


@dataclass(frozen=True)
class TtsUsage:
    """Voice-TTS usage DTO.

    Carries both quantities because TTS lanes do not agree on what they
    sell. A conventional lane bills per character of input text; a
    generative one bills per token of produced audio, which is a rate per
    second of speech and has no relation to how many characters produced
    it. Which field is charged is decided by the lane's `VoiceRate.unit`,
    never by the caller — the engine fills in both and lets the price
    decide.
    """

    char_count: int = 0
    seconds: float = 0.0


@dataclass(frozen=True)
class SttUsage:
    """Voice-STT usage DTO. Billed per second of input audio."""

    seconds: float = 0.0


#: What a voice lane's `VoiceRate.usd` is a price *of*. TTS lanes use
#: either; STT lanes are always `audio_hour`.
VOICE_UNIT_CHARS = "chars"
VOICE_UNIT_AUDIO_HOUR = "audio_hour"


@dataclass(frozen=True)
class VoiceRate:
    """One voice lane's price, its daily ceiling, and the unit it is a
    price of.

    The unit travels with the rate rather than being implied by which
    side of the subsystem the lane sits on. Implying it — TTS means
    per-character, STT means per-audio-hour — holds only while every TTS
    lane is a local one billing $0. A cloud lane that
    bills per second of produced speech has no per-character rate to
    state, and inventing one by dividing through by a typical character
    count would put a fabricated number in the operator's spend view.
    """

    usd: float
    cap_usd: float
    unit: str


@dataclass(frozen=True)
class VoiceCostEvent:
    """One voice-billing entry. Distinct from `CostEvent` (token-based) —
    voice is billed in chars (TTS) or audio-seconds (STT), so the JSONL
    entry shape diverges from the chat-turn event."""

    timestamp: str
    local_date: str
    kind: str              # "voice_tts" | "voice_stt"
    provider: str
    # A TTS event carries both — the lane's rate unit decides which one was
    # charged, and recording only the charged quantity would leave the other
    # unavailable to anyone auditing the lane's price after the fact.
    char_count: int        # 0 for STT
    seconds: float         # 0.0 for a per-character TTS lane
    cost_usd: float
    daily_total_usd: float
    provider_total_usd: float


def _opt_float(fields: Mapping[str, Any], key: str) -> float | None:
    value = fields.get(key)
    return None if value is None else float(value)


@dataclass(frozen=True)
class ModelPrice:
    """What one model charges, exactly as its catalog entry declares it.

    Every rate that turns a token into money lives here and comes from
    `providers.yaml`. Nothing in this module supplies a default for one. Two
    module constants used to, and they were one provider's discount applied to
    every provider. The discount is not even a family property: Grok reads at
    16% of base, and GPT-5.4 at 25% while GPT-5.4-mini reads at 10%. A
    plausible total computed from the wrong rate is the one kind of error
    nothing downstream can see.

    `cache_write_from` is the piece that cannot be read off a usage payload,
    because the two providers disagree about what a cache write even is:

    - `reported` — the provider counts written tokens and says so, and they
      are ADDITIONAL to `input_tokens`. Anthropic.
    - `uncached_input` — the provider counts nothing, and bills the non-cached
      part of the prompt at the write rate INSTEAD of the base rate. OpenAI
      implicit caching. Only above `cache_write_min_prompt_tokens`, below which
      the prompt is not cacheable at all and no premium applies.
    - `""` — no cache-write charge.

    Getting that distinction wrong is not a rounding error: `reported` adds a
    term, `uncached_input` replaces one.

    `long_context_threshold_tokens` is the OTHER thing a usage payload cannot
    tell you: some models charge a second, higher set of rates once the prompt
    passes a size, and bill the WHOLE request at them. The payload reports the
    same token classes either way, so a tier that is not declared here is
    invisible and every large prompt bills short. Absent, which is most models,
    means one set of rates at every size. Present means all four long rates are
    required: the multipliers are not a family property and not derivable from
    the short ones (the model this was first measured on doubles input, cached
    input and cache write, and multiplies output by 1.5).
    """

    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float | None = None
    cache_write_per_mtok: float | None = None
    cache_write_from: str = ""
    cache_write_min_prompt_tokens: int = 0
    long_context_threshold_tokens: int = 0
    input_per_mtok_long: float | None = None
    output_per_mtok_long: float | None = None
    cache_read_per_mtok_long: float | None = None
    cache_write_per_mtok_long: float | None = None

    _SOURCES: ClassVar[tuple[str, ...]] = ("", "reported", "uncached_input")
    _LONG_FIELDS: ClassVar[tuple[str, ...]] = (
        "cost_per_mtok_in_long",
        "cost_per_mtok_out_long",
        "cost_per_mtok_cached_in_long",
        "cost_per_mtok_cache_write_long",
    )

    def for_prompt(self, input_tokens: int) -> "ModelPrice":
        """The rates that apply to a prompt this size.

        Returns a ModelPrice whose ordinary fields ARE the applicable rates, so
        every billing rule downstream reads one set and none of them learns
        that tiers exist. Adding the tier as a branch in `_compute_usd` instead
        would have meant four rules each asking the question again, which is
        how the cache-write rule came to be spelled differently in two places.
        """
        if not self.long_context_threshold_tokens:
            return self
        if input_tokens <= self.long_context_threshold_tokens:
            return self
        return replace(
            self,
            input_per_mtok=self.input_per_mtok_long,
            output_per_mtok=self.output_per_mtok_long,
            cache_read_per_mtok=self.cache_read_per_mtok_long,
            cache_write_per_mtok=self.cache_write_per_mtok_long,
        )

    @classmethod
    def from_fields(cls, fields: Mapping[str, Any], model: str) -> "ModelPrice | None":
        """One parser. Four call sites read the catalog (two constructors and
        two reload paths) and each used to spell this out by hand, which is how
        `cost_per_mtok_cached_in` came to be honoured in some and how a fifth
        rate would have been added to three of the four."""
        p_in = fields.get("cost_per_mtok_in")
        p_out = fields.get("cost_per_mtok_out")
        if p_in is None or p_out is None:
            return None
        source = str(fields.get("cache_write_from") or "")
        if source not in cls._SOURCES:
            raise RuntimeError(
                f"{model}: cache_write_from is {source!r}, which is not one of "
                f"{cls._SOURCES}. It says how the provider counts a cache "
                "write, and a guess here mis-bills every cached turn"
            )
        read = fields.get("cost_per_mtok_cached_in")
        write = fields.get("cost_per_mtok_cache_write")
        threshold = int(fields.get("long_context_threshold_tokens") or 0)
        declared = [f for f in cls._LONG_FIELDS if fields.get(f) is not None]
        # All or nothing, both ways. A threshold with a rate missing would bill
        # a large prompt at `None`; a rate with no threshold would never be
        # reached and reads as a tier that is switched on. Neither is something
        # to discover from an invoice.
        if threshold and len(declared) != len(cls._LONG_FIELDS):
            missing = [f for f in cls._LONG_FIELDS if fields.get(f) is None]
            raise RuntimeError(
                f"{model}: long_context_threshold_tokens is {threshold} but "
                f"{missing} are not declared. A long-context tier bills the "
                "whole request, so every rate it uses has to be stated"
            )
        if declared and not threshold:
            raise RuntimeError(
                f"{model}: {declared} are declared with no "
                "long_context_threshold_tokens, so nothing would ever bill at "
                "them. State the prompt size the tier begins at"
            )
        return cls(
            input_per_mtok=float(p_in),
            output_per_mtok=float(p_out),
            cache_read_per_mtok=None if read is None else float(read),
            cache_write_per_mtok=None if write is None else float(write),
            cache_write_from=source,
            cache_write_min_prompt_tokens=int(
                fields.get("cache_write_min_prompt_tokens") or 0
            ),
            long_context_threshold_tokens=threshold,
            input_per_mtok_long=_opt_float(fields, "cost_per_mtok_in_long"),
            output_per_mtok_long=_opt_float(fields, "cost_per_mtok_out_long"),
            cache_read_per_mtok_long=_opt_float(fields, "cost_per_mtok_cached_in_long"),
            cache_write_per_mtok_long=_opt_float(
                fields, "cost_per_mtok_cache_write_long"
            ),
        )

    @property
    def is_free(self) -> bool:
        """Nothing this model does costs money.

        Here rather than at the caller, because `MeteredAdapter` used to answer
        this by indexing the pricing dict's values as a 2-tuple. That is a
        second module holding an opinion about this one's internal shape, and
        it broke silently the moment the shape changed: every scheduled call
        would have raised `TypeError` before reaching the budget check the
        wrapper exists to perform.
        """
        return self.input_per_mtok == 0.0 and self.output_per_mtok == 0.0

    @classmethod
    def coerce(cls, value: Any) -> "ModelPrice":
        """Accept the legacy `(in, out)` pair. Fixtures across the older cost
        suites build a ledger with `pricing={"m": (1.0, 2.0)}` and mean exactly
        a model with no cache rates, which is what this produces."""
        if isinstance(value, ModelPrice):
            return value
        p_in, p_out = value
        return cls(input_per_mtok=float(p_in), output_per_mtok=float(p_out))


@dataclass(frozen=True)
class CostEvent:
    """One appended ledger entry. UTC timestamp; daily grouping by local-tz date."""

    timestamp: str
    local_date: str
    role: str
    model: str
    input_tokens: int
    output_tokens: int
    # `None` where the provider reported no such class. See `CostUsage`.
    cached_tokens: int | None
    cost_usd: float
    daily_total_usd: float
    role_total_usd: float
    cache_creation_tokens: int | None = None
    reasoning_tokens: int | None = None
    # Which piece of work paid, read off `orchestrator.turns.context` at the
    # write. Empty outside a turn, and that emptiness is the claim: a scheduled
    # job's row is nobody's turn rather than the last turn's.
    turn_id: str = ""
    task_id: str = ""
    # Which tier the call rode when the row was written: the caller's word
    # where it has one, else the catalog's for the model name. Kept on the
    # row so a model that later moves tiers does not rewrite what old rows
    # cost.
    tier: str = ""


@dataclass(frozen=True)
class BudgetState:
    spent_usd: float
    warning_usd: float
    cap_usd: float
    role_spent_usd: float
    role_cap_usd: float | None  # None when the role has no sub-cap
    warning: bool  # spent_usd >= warning_usd
    blocked: bool  # global cap hit OR role sub-cap hit


def _require(d: dict, key: str, where: str) -> Any:
    if key not in d:
        raise RuntimeError(f"missing required key '{key}' in {where}")
    return d[key]


def _local_today_iso() -> str:
    return date.today().isoformat()


def _caps_from_bundle(bundle) -> dict[str, float]:
    """Every daily ceiling, keyed by what it bills.

    One declaration, one file. A block under `roles.yaml::roles` is either a
    seat some work fills — chat, the delegation seats, the agent defaults — or
    a thing the app runs on its own, named for its manifest entry. The ledger
    has one notion of a spend source and treats both as one, which is why they
    can live together: `panel_writer` bills on the string `panel_writer`
    whichever kind of thing it is.

    The manifest declared these numbers until 2026-08-25 and no longer does. A
    ceiling has to be changeable by the person paying for it, and in Python it
    was sealed source in an installed app: unreachable from the cockpit, and
    unreachable from a phone.

    Read here at construction and again on every `reload()`, so an edit takes
    effect without a restart.
    """
    caps: dict[str, float] = {}
    for role_name, role_cfg in bundle.roles.items():
        cap = role_cfg.overrides.get("daily_budget_usd")
        if cap is not None:
            caps[role_name] = float(cap)
    return caps


def _voice_pricing_from_bundle(
    bundle,
) -> tuple[dict[str, tuple[float, float]], dict[str, tuple[float, float]]]:
    """Build per-provider TTS/STT pricing maps from the catalog + role lanes.

    Pricing (rate per million chars / per audio hour) lives on the providers
    catalog model entry. Per-lane daily caps live on
    ``roles.yaml::voice.{stt,tts}.<lane>.daily_budget_usd``. We key both maps
    by the catalog model id (``af_heart``, ``hfc_female``,
    ``large_v3_turbo``, ``gemini_flash_audio``) so existing callers that pass
    the engine name unchanged continue to work.
    """
    tts: dict[str, VoiceRate] = {}
    stt: dict[str, VoiceRate] = {}
    if bundle.voice is None:
        return tts, stt
    voice = bundle.voice
    tts_chain = voice.tts.chain() if voice.tts is not None else ()
    stt_chain = voice.stt.chain() if voice.stt is not None else ()
    for entry in tts_chain:
        model = entry.ref.model
        if model.kind != "tts":
            continue
        # A TTS entry declares exactly one of the two rate fields, and
        # which one it declares is what says how the lane bills. Per-char
        # is checked first only because every local lane uses it; an entry
        # carrying both is a catalog error, and the per-char reading wins
        # rather than the two being silently added together.
        rate = model.fields.get("cost_per_million_chars")
        if rate is not None:
            tts[model.id] = VoiceRate(
                float(rate), float(entry.daily_budget_usd or 0.0), VOICE_UNIT_CHARS,
            )
            continue
        rate = model.fields.get("cost_per_audio_hour")
        if rate is None:
            continue
        tts[model.id] = VoiceRate(
            float(rate), float(entry.daily_budget_usd or 0.0), VOICE_UNIT_AUDIO_HOUR,
        )
    for entry in stt_chain:
        model = entry.ref.model
        if model.kind not in ("stt", "audio_stt"):
            continue
        rate = model.fields.get("cost_per_audio_hour", 0.0)
        stt[model.id] = VoiceRate(
            float(rate), float(entry.daily_budget_usd or 0.0), VOICE_UNIT_AUDIO_HOUR,
        )
    return tts, stt


def _voice_rates_from_raw(block: Any, *, kind: str) -> dict[str, VoiceRate]:
    """Parse a `cost_tracking.voice.<kind>` mapping into rates.

    The single-file fixture path, used by `from_models_yaml` and by
    `reload`'s test branch — the bundle path is `_voice_pricing_from_bundle`
    above. Both existed as the same twelve lines twice over, which is how
    the two came to be edited separately; the unit rule lives here once.

    A lane with no rate field and no cap is skipped rather than defaulted:
    a lane priced at an assumed zero would spend against the global cap
    while reporting nothing.
    """
    out: dict[str, VoiceRate] = {}
    for provider, cfg in (block or {}).items():
        if not isinstance(cfg, dict):
            continue
        cap = cfg.get("daily_budget_usd")
        if cap is None:
            continue
        if kind == "tts" and cfg.get("cost_per_million_chars") is not None:
            out[provider] = VoiceRate(
                float(cfg["cost_per_million_chars"]), float(cap), VOICE_UNIT_CHARS,
            )
            continue
        rate = cfg.get("cost_per_audio_hour")
        if rate is None:
            continue
        out[provider] = VoiceRate(float(rate), float(cap), VOICE_UNIT_AUDIO_HOUR)
    return out


CostSubscriber = Callable[["CostEvent", "BudgetState"], None]


@dataclass
class CostLedger:
    """Shared runtime cost accountant.

    Construct once at boot via `from_models_yaml(...)` and thread through to
    `ChatSession` + `Observer`. All methods are safe from any thread / async
    task — a single Lock guards counters and the JSONL writer.

    Voice spend is tracked in two parallel lanes: `_voice_provider_totals_usd`
    holds per-provider running totals (separate from chat `_role_totals_usd`),
    and `voice_pricing_*` maps carry per-provider unit rates loaded from
    `cost_tracking.voice.*`. Voice spend rolls up into `_daily_total_usd`
    so the global cap covers everything, but per-provider voice caps are
    independent of chat per-role caps.
    """

    enabled: bool
    warning_at_pct: float
    per_role_caps: dict[str, float]
    # Model name to its declared rates. A legacy `(in, out)` pair is accepted
    # and coerced, which is what the older fixtures pass.
    pricing: dict[str, ModelPrice]
    log_path: Path
    voice_tts_pricing: dict[str, VoiceRate] = field(default_factory=dict)
    voice_stt_pricing: dict[str, VoiceRate] = field(default_factory=dict)
    # Model name to catalog tier (`api` / `cli` / `local`), from the same walk
    # that builds `pricing`. Written on every row, see `CostEvent.tier`.
    tiers: dict[str, str] = field(default_factory=dict)
    providers_yaml: Path = _DEFAULT_PROVIDERS_YAML
    roles_yaml: Path = _DEFAULT_ROLES_YAML
    # Test-only — when set, ``reload()`` re-parses this single-file fixture
    # instead of going through the loader. None in production.
    _test_fixture_yaml: Path | None = None

    # (mtime_ns, size) of the log the in-memory totals were last built from.
    # The FILE is the canonical record of spend; these totals are a cache of
    # it, and more than one process appends to it — the Mirror (which hosts
    # chat, Telegram, voice and vision alike) and the agent controller. A cache
    # nobody revalidates is how two processes each authorise spend up to the
    # same cap.
    _log_fingerprint: tuple[int, int] | None = None

    _daily_total_usd: float = 0.0
    _role_totals_usd: dict[str, float] = field(default_factory=dict)
    _voice_provider_totals_usd: dict[str, float] = field(default_factory=dict)
    _current_local_date: str = ""
    _lock: Lock = field(default_factory=Lock)
    _today_fn: Callable[[], str] = field(default=_local_today_iso)
    _subscribers: list[CostSubscriber] = field(default_factory=list)
    # Cost UX overhaul (see WARNING_AT_PCT). Both reset on midnight roll.
    _warned_today: set[str] = field(default_factory=set)
    # "The operator approved going past the cap." Held here as a cache and
    # written down by `cost/overage.py`, stamped with the day it belongs to.
    # It used to be memory-only, on the reasoning that a restart should re-lock
    # the cap. That cost a day: an approval given minutes after a breaker
    # tripped on the cap could not be seen by anything but the process that
    # asked for it, so every other reader went on believing the cap still
    # refused. The day stamp is what makes persisting it safe, and it is now
    # the same answer in every process rather than one answer per process.
    _overage_unlocked_today: set[str] = field(default_factory=set)
    # (mtime, size) of the approvals file the set above was built from.
    _unlocks_fingerprint: tuple[int, int] | None = None
    # Operator-paused spend sources (a role name or "global"). A paused source
    # hard-blocks in check_preflight regardless of remaining cap. Runtime-only
    # (no on-disk persistence) — a restart clears pauses. Not reset at midnight
    # either: a pause is an explicit operator hold rather than a per-day budget
    # flag, so it has no day to be stamped with, which is what makes the
    # approval above safe to keep and this not.
    _paused_sources: set[str] = field(default_factory=set)

    @classmethod
    def from_bundle(
        cls,
        bundle,
        log_path: Path | None = None,
        today_fn: Callable[[], str] | None = None,
    ) -> CostLedger:
        """Build a ledger from a loaded :class:`ConfigBundle`.

        `log_path` overrides the yaml-declared `log_file` (tests pass a tmp
        path). `today_fn` overrides `date.today().isoformat()` for midnight-
        rollover tests.

        Pricing is keyed by **model name** (single catalog source), not by
        ``(role, model)``. Per-role daily caps come from each role's
        ``daily_budget_usd`` override in roles.yaml.
        """
        ct_raw = bundle.cost_tracking
        if not ct_raw:
            raise RuntimeError("missing 'cost_tracking' block in providers.yaml")
        where = "providers.yaml cost_tracking"
        enabled = bool(_require(ct_raw, "enabled", where))
        warning_at_pct = float(_require(ct_raw, "warning_at_pct", where))
        log_file = _require(ct_raw, "log_file", where)

        per_role_caps: dict[str, float] = _caps_from_bundle(bundle)

        pricing: dict[str, ModelPrice] = {}
        tiers: dict[str, str] = {}
        for ref, _conn, model in bundle.all_models():
            tiers.setdefault(model.model, ref.split(".", 1)[0])
            price = ModelPrice.from_fields(model.fields, model.model)
            if price is not None:
                pricing[model.model] = price

        voice_tts_pricing, voice_stt_pricing = _voice_pricing_from_bundle(bundle)

        # `home_dir()` is called HERE, not captured at import. The ledger is
        # operator history and belongs on the half that follows them; a
        # module-level `TESSERACT_HOME` freezes whatever the environment said
        # when this module was first imported, which is the wrong answer in
        # any process that sets the home after import — every test that
        # redirects it, and the packaged app, where the shell points it at
        # `home/`.
        resolved_log_path = log_path if log_path is not None else _home_dir() / log_file

        ledger = cls(
            enabled=enabled,
            warning_at_pct=warning_at_pct,
            per_role_caps=per_role_caps,
            pricing=pricing,
            voice_tts_pricing=voice_tts_pricing,
            voice_stt_pricing=voice_stt_pricing,
            tiers=tiers,
            log_path=resolved_log_path,
            providers_yaml=bundle.providers_path,
            roles_yaml=bundle.roles_path,
            _today_fn=today_fn or _local_today_iso,
        )
        ledger._seed_from_log()
        return ledger

    @classmethod
    def from_models_yaml(
        cls,
        models_yaml: Path,
        log_path: Path | None = None,
        today_fn: Callable[[], str] | None = None,
    ) -> CostLedger:
        """Test-only — parse a single-file YAML in the pre-split shape.

        Production never lands here (``tesseract/config/models.yaml`` was
        deleted in the providers/roles split). Cost-ledger fixtures under
        the older suites keep using compact inline dicts
        for readability; rather than rewrite each one to emit a
        providers.yaml + roles.yaml pair, we keep a small parser scoped
        to that shape.

        New code should use :meth:`from_bundle` against an already-loaded
        :class:`ConfigBundle`.
        """
        raw = yaml.safe_load(Path(models_yaml).read_text(encoding="utf-8")) or {}
        ct_raw = raw.get("cost_tracking")
        if not ct_raw:
            raise RuntimeError("missing 'cost_tracking' block in test fixture YAML")
        where = "test fixture cost_tracking"
        enabled = bool(_require(ct_raw, "enabled", where))
        warning_at_pct = float(_require(ct_raw, "warning_at_pct", where))
        log_file = _require(ct_raw, "log_file", where)

        per_role_raw = ct_raw.get("per_role") or {}
        per_role_caps = {role: float(cap) for role, cap in per_role_raw.items()}

        pricing: dict[str, ModelPrice] = {}
        for _role_name, role_cfg in (raw.get("roles") or {}).items():
            for entry in role_cfg.get("resolution") or []:
                model = entry.get("model")
                if not model or model in pricing:
                    continue
                price = ModelPrice.from_fields(entry, model)
                if price is not None:
                    pricing[model] = price

        voice_raw = ct_raw.get("voice") or {}
        voice_tts = _voice_rates_from_raw(voice_raw.get("tts"), kind="tts")
        voice_stt = _voice_rates_from_raw(voice_raw.get("stt"), kind="stt")

        # `home_dir()` is called HERE, not captured at import. The ledger is
        # operator history and belongs on the half that follows them; a
        # module-level `TESSERACT_HOME` freezes whatever the environment said
        # when this module was first imported, which is the wrong answer in
        # any process that sets the home after import — every test that
        # redirects it, and the packaged app, where the shell points it at
        # `home/`.
        resolved_log_path = log_path if log_path is not None else _home_dir() / log_file

        ledger = cls(
            enabled=enabled,
            warning_at_pct=warning_at_pct,
            per_role_caps=per_role_caps,
            pricing=pricing,
            voice_tts_pricing=voice_tts,
            voice_stt_pricing=voice_stt,
            log_path=resolved_log_path,
            _test_fixture_yaml=Path(models_yaml),
            _today_fn=today_fn or _local_today_iso,
        )
        ledger._seed_from_log()
        return ledger

    # ── Derived caps ────────────────────────────────────────────
    # The global daily cap is the sum of every inner cap. The umbrella
    # is whatever the channels add up to — there is no separate
    # `daily_budget_usd` line that could drift from the sum.

    @property
    def cap_usd(self) -> float:
        return (
            sum(self.per_role_caps.values())
            + sum(r.cap_usd for r in self.voice_tts_pricing.values())
            + sum(r.cap_usd for r in self.voice_stt_pricing.values())
        )

    @property
    def warning_usd(self) -> float:
        """Global warning threshold — same percentage that applies to
        every inner cap. Kept as a `_usd` value for the BudgetState DTO
        and HUD chip math."""
        return self.cap_usd * self.warning_at_pct

    # ── Public API ─────────────────────────────────────────────

    def record(self, role: str, model: str, usage: CostUsage, *, tier: str = "") -> CostEvent:
        """Compute USD, append JSONL, return the event.

        Unknown `(role, model)` raises — silent zero-billing is a bug, not a
        feature. CLI roles priced at 0 still write an event (visibility)
        but contribute $0 to the totals. After the JSONL write, subscribers
        registered via `subscribe()` are fired outside the lock with
        `(event, budget_state)` so callbacks may invoke other ledger
        methods without deadlocking.

        `tier` is the caller's word for which tier the call actually rode
        (`AdapterOptions.tier`), and it wins; a caller that has none gets the
        catalog's answer for the model name, which is ambiguous only where one
        name is listed under two tiers.
        """
        if not self.enabled:
            with self._lock:
                return self._build_event(role, model, usage, cost_usd=0.0, tier=tier)

        with self._lock:
            self._maybe_roll_midnight()
            cost = self._compute_usd(role, model, usage)
            self._daily_total_usd += cost
            self._role_totals_usd[role] = self._role_totals_usd.get(role, 0.0) + cost
            event = self._build_event(role, model, usage, cost_usd=cost, tier=tier)
            self._append_jsonl(event)
            state = self._budget_state_locked(role)

        for cb in list(self._subscribers):
            try:
                cb(event, state)
            except Exception:
                logger.exception("cost ledger subscriber failed")
        return event

    def budget_state(self, role: str) -> BudgetState:
        with self._lock:
            self._maybe_roll_midnight()
            return self._budget_state_locked(role)

    def subscribe(self, callback: CostSubscriber) -> None:
        """Register a callback fired after every `record()` with
        `(event, budget_state)`. Callbacks run outside the internal lock
        and must swallow their own exceptions — the ledger logs but does
        not propagate subscriber faults. Intended for Mirror WS fan-out.
        """
        self._subscribers.append(callback)

    def reload(self) -> None:
        """Re-read `cost_tracking` + pricing from providers.yaml + roles.yaml.

        Updates caps, warning threshold, per-role sub-caps, and pricing
        in place. **Daily totals and the JSONL file are preserved** — the
        operator tweaking a cap mid-day expects their spend-to-date to
        carry over, not reset. Subscribers stay registered.
        """
        if self._test_fixture_yaml is not None:
            self._reload_from_test_fixture()
            return

        from tesseract.config.loader import load_config

        bundle = load_config(providers_path=self.providers_yaml, roles_path=self.roles_yaml)
        ct_raw = bundle.cost_tracking
        if not ct_raw:
            raise RuntimeError("missing 'cost_tracking' block in providers.yaml")
        where = "providers.yaml cost_tracking"
        new_enabled = bool(_require(ct_raw, "enabled", where))
        new_warn_pct = float(_require(ct_raw, "warning_at_pct", where))

        new_caps: dict[str, float] = _caps_from_bundle(bundle)

        new_pricing: dict[str, ModelPrice] = {}
        new_tiers: dict[str, str] = {}
        for ref, _conn, model in bundle.all_models():
            new_tiers.setdefault(model.model, ref.split(".", 1)[0])
            price = ModelPrice.from_fields(model.fields, model.model)
            if price is not None:
                new_pricing[model.model] = price

        new_voice_tts, new_voice_stt = _voice_pricing_from_bundle(bundle)

        with self._lock:
            # Roll midnight before the write so reload-after-midnight doesn't
            # strand yesterday's totals under today's caps. `budget_state()`
            # would roll on the next read anyway, but a caller may also read
            # internal counters (or a `cap_usd` snapshot) right after reload.
            self._maybe_roll_midnight()
            self.enabled = new_enabled
            self.warning_at_pct = new_warn_pct
            self.per_role_caps = new_caps
            self.pricing = new_pricing
            self.tiers = new_tiers
            self.voice_tts_pricing = new_voice_tts
            self.voice_stt_pricing = new_voice_stt

    def _reload_from_test_fixture(self) -> None:
        """Test-only — re-parse the single-file fixture set by from_models_yaml."""
        path = self._test_fixture_yaml
        assert path is not None
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        ct_raw = raw.get("cost_tracking")
        if not ct_raw:
            raise RuntimeError("missing 'cost_tracking' block in test fixture")
        where = "test fixture cost_tracking"
        new_enabled = bool(_require(ct_raw, "enabled", where))
        new_warn_pct = float(_require(ct_raw, "warning_at_pct", where))

        per_role_raw = ct_raw.get("per_role") or {}
        new_caps = {role: float(cap) for role, cap in per_role_raw.items()}

        new_pricing: dict[str, ModelPrice] = {}
        for _role_name, role_cfg in (raw.get("roles") or {}).items():
            for entry in role_cfg.get("resolution") or []:
                model = entry.get("model")
                if not model or model in new_pricing:
                    continue
                price = ModelPrice.from_fields(entry, model)
                if price is not None:
                    new_pricing[model] = price

        voice_raw = ct_raw.get("voice") or {}
        new_voice_tts = _voice_rates_from_raw(voice_raw.get("tts"), kind="tts")
        new_voice_stt = _voice_rates_from_raw(voice_raw.get("stt"), kind="stt")

        with self._lock:
            self._maybe_roll_midnight()
            self.enabled = new_enabled
            self.warning_at_pct = new_warn_pct
            self.per_role_caps = new_caps
            self.pricing = new_pricing
            self.voice_tts_pricing = new_voice_tts
            self.voice_stt_pricing = new_voice_stt

    #: The windows the spend panel offers, and how many local days each spans.
    #: Named here rather than at the call site so the route, the panel and the
    #: tests cannot disagree about what "week" means.
    #: `ClassVar` because this class is a dataclass — a bare dict annotation
    #: here is read as a field with a mutable default and refuses to build.
    WINDOW_DAYS: ClassVar[dict[str, int]] = {"day": 1, "week": 7, "month": 30}

    def daily_totals(self) -> dict[str, float]:
        """Every local date in the ledger, with what was spent on it.

        Reads the whole file rather than today's rows — `_seed_from_log` needs
        only today, this needs the history behind it. Malformed lines are
        skipped exactly as they are there: a ledger that cannot be read at all
        is worse than one missing a row somebody corrupted by hand.
        """
        totals: dict[str, float] = {}
        if not self.log_path.exists():
            return totals
        try:
            with self.log_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    day = entry.get("local_date")
                    if not isinstance(day, str):
                        continue
                    totals[day] = totals.get(day, 0.0) + float(entry.get("cost_usd", 0.0))
        except OSError as exc:
            logger.warning("cost ledger unreadable for windows: %s", exc)
        return totals

    def windows(self) -> dict[str, Any]:
        """Spend over the last day, week and month, each with the window
        BEFORE it so a trend compares like with like.

        The comparison window is the reason this is computed here rather than
        in the panel: "up 20%" against nothing is not a trend, and only the
        ledger knows whether the earlier period exists. `history_days` is the
        count of distinct dates present, and a caller that wants to draw a
        direction has to check it — a first-week install comparing against
        seven empty days would report every figure as an infinite rise.
        """
        totals = self.daily_totals()
        today = date.fromisoformat(self._today_fn())

        def _sum(start_offset: int, span: int) -> float:
            days = [
                (today - timedelta(days=start_offset + i)).isoformat()
                for i in range(span)
            ]
            return round(sum(totals.get(d, 0.0) for d in days), 6)

        out: dict[str, Any] = {}
        for name, span in self.WINDOW_DAYS.items():
            out[name] = {
                "days": span,
                "spent_usd": _sum(0, span),
                # The same span, immediately before this one.
                "previous_usd": _sum(span, span),
            }
        dates = sorted(totals)
        return {
            "windows": out,
            "history_days": len(dates),
            "first_date": dates[0] if dates else None,
            "last_date": dates[-1] if dates else None,
            "local_date": today.isoformat(),
        }

    def snapshot(self) -> dict[str, Any]:
        """Read-only view of today's spend. Used by the Mirror WS catch-up
        envelope (`cost_state`) and the `GET /api/cost/state` REST surface
        so the HUD chips show correct values immediately on connect /
        reload, without waiting for the next billed turn.

        Shape:
            {
              "global": {spent_usd, warning_usd, cap_usd, warning, blocked},
              "roles": {
                "<role>": {role_total_usd, role_cap_usd, last_model},
                ...
              },
              "voice_providers": {
                "tts": {"<provider>": {spent_usd, cap_usd, rate}},
                "stt": {"<provider>": {spent_usd, cap_usd, rate}},
              },
              "local_date": "YYYY-MM-DD",
              "enabled": bool,
            }

        Voice rolls up across providers into `roles.voice_tts` /
        `roles.voice_stt` so the HUD VoiceCostChip can read it without
        a per-provider key. Per-provider detail lives under
        `voice_providers` for the Settings panel.
        """
        with self._lock:
            self._maybe_roll_midnight()
            self._resync_unlocks()
            global_warning = self._daily_total_usd >= self.warning_usd
            global_blocked = self._daily_total_usd >= self.cap_usd
            roles: dict[str, dict[str, Any]] = {}
            # Every role that declares a ceiling, plus every role that has
            # actually spent. The list was hardcoded to chat_brain and
            # observer_agent, so the ceilings the other roles declare in
            # roles.yaml — autonomy's among them — never reached a surface:
            # they counted against the global cap while the breakdown that
            # explains the global cap did not mention them. A role with a cap
            # is listed on a fresh day too, so its chip reads $0.00 / $cap
            # before anything has been billed.
            for role in sorted(set(self.per_role_caps) | set(self._role_totals_usd)):
                roles[role] = {
                    "role_total_usd": self._role_totals_usd.get(role, 0.0),
                    "role_cap_usd": self.per_role_caps.get(role),
                    "last_model": "",
                }
            tts_total = sum(
                spent for provider, spent in self._voice_provider_totals_usd.items()
                if provider in self.voice_tts_pricing
            )
            stt_total = sum(
                spent for provider, spent in self._voice_provider_totals_usd.items()
                if provider in self.voice_stt_pricing
            )
            roles["voice_tts"] = {
                "role_total_usd": tts_total,
                "role_cap_usd": None,
                "last_model": "",
            }
            roles["voice_stt"] = {
                "role_total_usd": stt_total,
                "role_cap_usd": None,
                "last_model": "",
            }
            voice_providers = {
                # `rate_unit` travels with `rate` so a reader can label it.
                # Without it the spend view would print one number for a
                # per-million-character price and a per-audio-hour one, which
                # differ by five orders of magnitude.
                "tts": {
                    provider: {
                        "spent_usd": self._voice_provider_totals_usd.get(provider, 0.0),
                        "cap_usd": r.cap_usd,
                        "rate": r.usd,
                        "rate_unit": r.unit,
                    }
                    for provider, r in self.voice_tts_pricing.items()
                },
                "stt": {
                    provider: {
                        "spent_usd": self._voice_provider_totals_usd.get(provider, 0.0),
                        "cap_usd": r.cap_usd,
                        "rate": r.usd,
                        "rate_unit": r.unit,
                    }
                    for provider, r in self.voice_stt_pricing.items()
                },
            }
            return {
                "global": {
                    "spent_usd": self._daily_total_usd,
                    "warning_usd": self.warning_usd,
                    "cap_usd": self.cap_usd,
                    "warning": global_warning,
                    "blocked": global_blocked,
                },
                "roles": roles,
                "voice_providers": voice_providers,
                "local_date": self._current_local_date or self._today_fn(),
                "enabled": self.enabled,
                # Cost UX overhaul: per-scope flags. Frontend uses
                # `overage_unlocked` to render HUD chips in red when
                # `spent_usd > cap_usd` AND the scope was approved
                # (otherwise spent>cap means we're blocked). `warned`
                # suppresses duplicate toasts across page reloads in
                # the same day.
                "overage_unlocked": sorted(self._overage_unlocked_today),
                "warned": sorted(self._warned_today),
            }

    # ── Cost UX overhaul ───────────────────────────────────────
    # Scope keys are stable identifiers used by both warning and
    # overage-unlock state. Format:
    #   "global"                          — daily_budget_usd
    #   "role:<role_name>"                — per_role[role_name]
    #   "voice:<kind>:<provider>"         — voice tts/stt provider cap
    # Frontend uses the same keys on `cost_overage_response`.

    def _log_state(self) -> tuple[int, int] | None:
        try:
            st = self.log_path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _resync_from_log(self) -> None:
        """Rebuild today's totals from the log when another writer touched it.

        Cheap in the common case: a stat, and a re-read only when the file has
        actually moved. `_seed_from_log` ACCUMULATES rather than assigns, so
        the totals are zeroed first — seeding twice without this would count
        every row again and the cap would trip at half its value.

        Must be called with `_lock` held.
        """
        state = self._log_state()
        if state == self._log_fingerprint:
            return
        self._daily_total_usd = 0.0
        self._role_totals_usd.clear()
        self._voice_provider_totals_usd.clear()
        self._seed_from_log()
        self._log_fingerprint = state

    def check_warning(self, scope_key: str, spent: float, cap: float) -> bool:
        """Return True ONCE per day per scope when spend crosses
        `warning_at_pct` of cap. Caller fires a toast on True; subsequent
        calls return False until midnight rollover. Lock-free
        read-modify-write under `_lock`. Disabled ledger returns False."""
        if not self.enabled or cap <= 0:
            return False
        with self._lock:
            self._maybe_roll_midnight()
            if scope_key in self._warned_today:
                return False
            if spent < self.warning_at_pct * cap:
                return False
            self._warned_today.add(scope_key)
            return True

    def is_overage_unlocked(self, scope_key: str) -> bool:
        with self._lock:
            self._maybe_roll_midnight()
            self._resync_unlocks()
            return scope_key in self._overage_unlocked_today

    def unlock_overage(self, scope_key: str) -> None:
        """Operator approved continuing past 100%. Until midnight,
        future preflight checks for this scope will skip the cap test.
        Idempotent.

        Written down as well as remembered, so the other process sharing this
        budget and the recovery pass explaining a refusal both see the same
        answer. See `cost/overage.py` for why the day stamp is what makes that
        safe."""
        with self._lock:
            self._maybe_roll_midnight()
            self._overage_unlocked_today.add(scope_key)
            _overage.record(
                scope_key,
                today=self._current_local_date,
                when=datetime.now().astimezone(),
                path=_overage.unlocks_path(self.log_path),
            )
            self._unlocks_fingerprint = _overage.fingerprint(
                _overage.unlocks_path(self.log_path)
            )

    def _resync_unlocks(self) -> None:
        """Pick up an approval given somewhere else. Must hold `_lock`.

        A stat in the common case. The file is the record and this set is a
        cache of it, the same shape `_resync_from_log` uses for spend and for
        the same reason: two processes share one budget, and a cache nobody
        revalidates is how they disagree about it.

        A union, not an assignment, and the file is append-only for the same
        reason: within a day an approval is only ever granted. Nothing revokes
        one before midnight, so there is no state the file could carry that
        this set should drop.
        """
        path = _overage.unlocks_path(self.log_path)
        state = _overage.fingerprint(path)
        if state == self._unlocks_fingerprint:
            return
        self._overage_unlocked_today |= set(
            _overage.read(self._current_local_date, path)
        )
        self._unlocks_fingerprint = state

    # ── Operator budget controls (MCP budget.* verbs, P3) ──────────

    def set_role_cap(self, role: str, cap_usd: float) -> None:
        """Runtime override of a role's daily cap. Ephemeral — ``reload()``
        re-reads caps from roles.yaml (config remains the authority). Raises on
        a negative cap. ``cap_usd == 0`` blocks the role for the rest of today."""
        if cap_usd < 0:
            raise ValueError(f"cap_usd must be >= 0, got {cap_usd}")
        with self._lock:
            self.per_role_caps[role] = float(cap_usd)

    def pause_source(self, source: str) -> None:
        """Hard-pause a spend source (a role name or ``"global"``). Blocks in
        ``check_preflight`` until :meth:`resume_source` or a restart. Idempotent."""
        with self._lock:
            self._paused_sources.add(source)

    def resume_source(self, source: str) -> None:
        """Lift a pause set by :meth:`pause_source`. Idempotent."""
        with self._lock:
            self._paused_sources.discard(source)

    def is_source_paused(self, source: str) -> bool:
        with self._lock:
            return source in self._paused_sources

    def budget_summary(self) -> dict[str, Any]:
        """Global spend snapshot + per-role caps + paused sources — the
        ``budget.status`` read surface."""
        with self._lock:
            self._maybe_roll_midnight()
            return {
                "enabled": self.enabled,
                "spent_usd": self._daily_total_usd,
                "cap_usd": self.cap_usd,
                "warning_at_pct": self.warning_at_pct,
                "per_role_caps": dict(self.per_role_caps),
                "role_totals_usd": dict(self._role_totals_usd),
                "paused_sources": sorted(self._paused_sources),
            }

    def check_preflight(self, role: str) -> None:
        """Raise `BudgetExhausted` if role or global cap is hit. Idempotent.

        Cost UX overhaul: scopes the operator has already approved-to-
        continue for the day are skipped. The unlock is per-scope
        (role vs global), so a chat-role unlock does NOT silently allow
        the global cap to overflow further than chat actually needs.

        All state reads happen inside a single `_lock` acquire so role
        cap, global cap, and unlock checks see a consistent snapshot —
        otherwise concurrent `record()` calls could squeeze through a
        TOCTOU window between separate lock acquires."""
        if not self.enabled:
            return
        with self._lock:
            self._maybe_roll_midnight()
            # Revalidate against the log before deciding. Every door into the
            # assistant that spends money writes to this one file — the Mirror
            # process carries chat, Telegram, voice and vision together, and
            # the agent controller is a second process appending to the same
            # place. Without this the two hold independent in-memory totals and
            # each authorises up to the full cap, so the ceiling is per-process
            # instead of per-day.
            self._resync_from_log()
            self._resync_unlocks()
            state = self._budget_state_locked(role)
            role_unlocked = f"role:{role}" in self._overage_unlocked_today
            global_unlocked = "global" in self._overage_unlocked_today
            paused = role in self._paused_sources or "global" in self._paused_sources
        # An operator pause hard-blocks — it precedes cap checks and is NOT
        # bypassable by an overage unlock (a pause is an explicit hold).
        if paused:
            raise BudgetExhausted(
                role=role, spent_usd=state.role_spent_usd, cap_usd=0.0, scope="paused",
            )
        if (
            not role_unlocked
            and state.role_cap_usd is not None
            and state.role_spent_usd >= state.role_cap_usd
        ):
            raise BudgetExhausted(
                role=role,
                spent_usd=state.role_spent_usd,
                cap_usd=state.role_cap_usd,
                scope="role",
            )
        if not global_unlocked and state.spent_usd >= state.cap_usd:
            raise BudgetExhausted(
                role=role,
                spent_usd=state.spent_usd,
                cap_usd=state.cap_usd,
                scope="global",
            )

    # ── Voice billing ──────────────────────────────────────────

    def voice_provider_total_usd(self, provider: str) -> float:
        """Per-provider voice spend so far today (rolled at midnight)."""
        with self._lock:
            self._maybe_roll_midnight()
            return self._voice_provider_totals_usd.get(provider, 0.0)

    def voice_provider_cap_usd(self, kind: str, provider: str) -> float | None:
        """Daily cap for a voice provider, or None if not configured.

        `kind` is `"tts"` or `"stt"`. Mirrors `voice_tts_pricing` /
        `voice_stt_pricing` shape.
        """
        if kind == "tts":
            entry = self.voice_tts_pricing.get(provider)
        elif kind == "stt":
            entry = self.voice_stt_pricing.get(provider)
        else:
            return None
        return entry.cap_usd if entry is not None else None

    def voice_check_preflight(self, kind: str, provider: str) -> None:
        """Raise `BudgetExhausted(scope="voice")` if the provider's daily
        voice cap is hit, or if the global daily cap is hit.

        The WS handler catches this and emits a `voice_instruction`
        toast. The TTS chain does not fall through to the next lane on a
        cap — only on a fault.

        `kind` must be `"tts"` or `"stt"`. A `None` cap (provider missing
        from yaml) is a config error, not an unbounded permit; we raise
        loudly rather than silently allowing.
        """
        if not self.enabled:
            return
        cap = self.voice_provider_cap_usd(kind, provider)
        if cap is None:
            raise RuntimeError(
                f"no voice pricing for kind={kind} provider={provider} — "
                f"add cost_tracking.voice.{kind}.{provider} to models.yaml"
            )
        # Global daily cap is the outer envelope: when it's hit, *no* voice
        # call can proceed regardless of per-provider headroom. Check it
        # first so the BudgetExhausted scope is reported as "global" and
        # the operator sees the right surface — otherwise a voice call
        # made after a chat-spend overshoot would mislabel the cause.
        with self._lock:
            self._maybe_roll_midnight()
            global_spent = self._daily_total_usd
            global_cap = self.cap_usd
        global_key = "global"
        voice_key = f"voice:{kind}:{provider}"
        # Same `cap > 0.0` guard applied to the per-provider check below:
        # if every role/voice budget were $0 the summed global cap would
        # be $0, and a literal `0 >= 0` would block the very first call.
        # Treat zero global cap as "no envelope — only per-provider caps
        # apply" so an all-local config can run without overage prompts.
        if (
            global_cap > 0.0
            and not self.is_overage_unlocked(global_key)
            and global_spent >= global_cap
        ):
            raise BudgetExhausted(
                role=voice_key,
                spent_usd=global_spent,
                cap_usd=global_cap,
                scope="global",
            )
        spent = self.voice_provider_total_usd(provider)
        # Cap == 0 means the provider is free at use-time (a local lane /
        # local Whisper at $0/M chars). A literal `spent >= 0` check would
        # trip BudgetExhausted on the very first call — treat zero cap as
        # "no per-provider ceiling" so only the global daily cap applies.
        if cap > 0.0 and not self.is_overage_unlocked(voice_key) and spent >= cap:
            raise BudgetExhausted(
                role=voice_key,
                spent_usd=spent,
                cap_usd=cap,
                scope="voice",
            )

    def record_voice(
        self,
        kind: str,
        provider: str,
        usage: TtsUsage | SttUsage,
    ) -> VoiceCostEvent:
        """Record voice spend in USD, append JSONL, return the event.

        TTS pricing is `cost_per_million_chars`; STT pricing is
        `cost_per_audio_hour`. Local engines priced at $0 still write an
        event (visibility) but contribute $0 to the daily total. Provider
        + global daily totals are updated; subscribers fire with a
        synthesized `(CostEvent, BudgetState)` shape so existing Mirror
        cost-broadcast plumbing works without re-keying. The synthesized
        event uses `role="voice_tts"|"voice_stt"` and `model=provider`,
        which is consumed by `make_cost_delta` to drive the kind tag in
        the envelope.
        """
        if kind not in ("tts", "stt"):
            raise RuntimeError(f"voice kind must be 'tts' or 'stt', got {kind!r}")

        if not self.enabled:
            with self._lock:
                return self._build_voice_event(kind, provider, usage, cost_usd=0.0)

        with self._lock:
            self._maybe_roll_midnight()
            cost = self._compute_voice_usd(kind, provider, usage)
            self._daily_total_usd += cost
            self._voice_provider_totals_usd[provider] = (
                self._voice_provider_totals_usd.get(provider, 0.0) + cost
            )
            event = self._build_voice_event(kind, provider, usage, cost_usd=cost)
            self._append_voice_jsonl(event)
            # Synthesize a CostEvent + BudgetState pair for fan-out so the
            # Mirror cost-broadcast subscriber works untouched.
            synth_event = CostEvent(
                timestamp=event.timestamp,
                local_date=event.local_date,
                role=f"voice_{kind}",
                model=provider,
                input_tokens=0,
                output_tokens=0,
                cached_tokens=0,
                cost_usd=event.cost_usd,
                daily_total_usd=event.daily_total_usd,
                role_total_usd=event.provider_total_usd,
            )
            synth_state = BudgetState(
                spent_usd=self._daily_total_usd,
                warning_usd=self.warning_usd,
                cap_usd=self.cap_usd,
                role_spent_usd=self._voice_provider_totals_usd[provider],
                role_cap_usd=self.voice_provider_cap_usd(kind, provider),
                warning=self._daily_total_usd >= self.warning_usd,
                blocked=self._daily_total_usd >= self.cap_usd,
            )

        for cb in list(self._subscribers):
            try:
                cb(synth_event, synth_state)
            except Exception:
                logger.exception("cost ledger voice subscriber failed")
        return event

    # ── Internals ──────────────────────────────────────────────

    def _budget_state_locked(self, role: str) -> BudgetState:
        """Compute BudgetState; caller must hold `self._lock` and have
        already called `_maybe_roll_midnight()`.

        `blocked` is unlock-aware: a scope the operator approved for
        overage today is not "blocked" — `check_preflight` will pass on
        it. Without this, HUD chips would show the `--bad` band (red)
        on a still-blocked turn even when the operator approved
        continuing, and the new `is-overage` background styling would
        never visibly apply."""
        role_spent = self._role_totals_usd.get(role, 0.0)
        role_cap = self.per_role_caps.get(role)
        warning = self._daily_total_usd >= self.warning_usd
        global_unlocked = "global" in self._overage_unlocked_today
        role_unlocked = f"role:{role}" in self._overage_unlocked_today
        global_blocked = (not global_unlocked) and self._daily_total_usd >= self.cap_usd
        role_blocked = (
            (not role_unlocked)
            and role_cap is not None
            and role_spent >= role_cap
        )
        blocked = global_blocked or role_blocked
        return BudgetState(
            spent_usd=self._daily_total_usd,
            warning_usd=self.warning_usd,
            cap_usd=self.cap_usd,
            role_spent_usd=role_spent,
            role_cap_usd=role_cap,
            warning=warning,
            blocked=blocked,
        )

    def _compute_usd(self, role: str, model: str, usage: CostUsage) -> float:
        pricing = self.pricing.get(model)
        if pricing is None:
            raise RuntimeError(
                f"no pricing for model={model} (role={role}) in providers.yaml — "
                "add cost_per_mtok_in / cost_per_mtok_out to the catalog entry"
            )
        # The tier is chosen once, here, from the prompt this request actually
        # sent. Everything below reads one set of rates.
        price = ModelPrice.coerce(pricing).for_prompt(usage.input_tokens)

        # Four rules, and they have to hold together. Held apart, each of them
        # has already been the way to get this wrong:
        #
        #   1. Every token is billed exactly once. `cached` is a subset of
        #      `input_tokens` on both providers, so the base term is the
        #      remainder and never the whole.
        #   2. A class the provider REPORTED that the catalog cannot price
        #      raises. Pricing it at zero is what let 236M cached tokens
        #      through under one provider's discount applied to all of them.
        #   3. A class the provider did not report contributes nothing and
        #      raises nothing. `None` is not an unpriced charge, it is no
        #      charge, and conflating the two would fail every local model.
        #   4. A cache write ADDS a term where the provider counts one, and
        #      REPLACES the base term where it does not. See `ModelPrice`.
        def _rate(value: float | None, what: str, tokens: int) -> float:
            if value is None:
                raise RuntimeError(
                    f"{model} (role={role}) billed {tokens} {what} tokens and "
                    f"providers.yaml declares no rate for them. Add it to the "
                    f"catalog entry: a missing rate is not a free token"
                )
            return value

        cached = usage.cached_tokens or 0
        uncached = max(0, usage.input_tokens - cached)

        total = usage.output_tokens * price.output_per_mtok
        if cached:
            total += cached * _rate(price.cache_read_per_mtok, "cached input", cached)

        writes_the_prompt = (
            price.cache_write_from == "uncached_input"
            and usage.input_tokens >= price.cache_write_min_prompt_tokens
        )
        if writes_the_prompt:
            # Rule 4, replacing. The provider counts no writes and bills the
            # fresh part of a cacheable prompt at the write rate. Below the
            # minimum the prompt is not cacheable at all, so the base rate
            # stands and adding the premium there would overcharge every short
            # call — which is the same error in the other direction.
            total += uncached * _rate(price.cache_write_per_mtok, "cache write", uncached)
        else:
            total += uncached * price.input_per_mtok

        if price.cache_write_from == "reported":
            written = usage.cache_creation_tokens or 0
            if written:
                # Rule 4, adding. These are not inside `input_tokens`.
                total += written * _rate(price.cache_write_per_mtok, "cache write", written)

        return total / 1_000_000

    def _compute_voice_usd(
        self, kind: str, provider: str, usage: TtsUsage | SttUsage
    ) -> float:
        if kind == "tts":
            entry = self.voice_tts_pricing.get(provider)
            if entry is None:
                raise RuntimeError(
                    f"no TTS pricing for provider={provider} — add "
                    f"cost_tracking.voice.tts.{provider} to models.yaml"
                )
            if entry.unit == VOICE_UNIT_AUDIO_HOUR:
                seconds = float(getattr(usage, "seconds", 0.0))
                return seconds * entry.usd / 3600.0
            chars = getattr(usage, "char_count", 0)
            return chars * entry.usd / 1_000_000
        if kind == "stt":
            entry = self.voice_stt_pricing.get(provider)
            if entry is None:
                raise RuntimeError(
                    f"no STT pricing for provider={provider} — add "
                    f"cost_tracking.voice.stt.{provider} to models.yaml"
                )
            seconds = float(getattr(usage, "seconds", 0.0))
            return seconds * entry.usd / 3600.0
        raise RuntimeError(f"voice kind must be 'tts' or 'stt', got {kind!r}")

    def _build_voice_event(
        self,
        kind: str,
        provider: str,
        usage: TtsUsage | SttUsage,
        cost_usd: float,
    ) -> VoiceCostEvent:
        now_utc = datetime.now(timezone.utc).replace(microsecond=0)
        return VoiceCostEvent(
            timestamp=now_utc.isoformat().replace("+00:00", "Z"),
            local_date=self._today_fn(),
            kind=f"voice_{kind}",
            provider=provider,
            char_count=getattr(usage, "char_count", 0),
            seconds=float(getattr(usage, "seconds", 0.0)),
            cost_usd=cost_usd,
            daily_total_usd=self._daily_total_usd,
            provider_total_usd=self._voice_provider_totals_usd.get(provider, 0.0),
        )

    def _append_voice_jsonl(self, event: VoiceCostEvent) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "ts": event.timestamp,
                            "local_date": event.local_date,
                            "kind": event.kind,
                            "provider": event.provider,
                            "char_count": event.char_count,
                            "seconds": round(event.seconds, 4),
                            "cost_usd": round(event.cost_usd, 8),
                            "daily_total_usd": round(event.daily_total_usd, 8),
                            "provider_total_usd": round(event.provider_total_usd, 8),
                        }
                    )
                    + "\n"
                )
        except OSError as exc:
            logger.warning("cost ledger voice JSONL write failed: %s (continuing)", exc)

    def _build_event(
        self, role: str, model: str, usage: CostUsage, cost_usd: float, tier: str = ""
    ) -> CostEvent:
        now_utc = datetime.now(timezone.utc).replace(microsecond=0)
        who = _turn_identity()
        return CostEvent(
            timestamp=now_utc.isoformat().replace("+00:00", "Z"),
            local_date=self._today_fn(),
            role=role,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            cost_usd=cost_usd,
            daily_total_usd=self._daily_total_usd,
            role_total_usd=self._role_totals_usd.get(role, 0.0),
            turn_id=who.turn_id,
            task_id=who.task_id,
            tier=tier or self.tiers.get(model, ""),
        )

    def _append_jsonl(self, event: CostEvent) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "ts": event.timestamp,
                            "local_date": event.local_date,
                            "role": event.role,
                            "model": event.model,
                            "input_tokens": event.input_tokens,
                            "output_tokens": event.output_tokens,
                            "cached_tokens": event.cached_tokens,
                            "cache_creation_tokens": event.cache_creation_tokens,
                            "reasoning_tokens": event.reasoning_tokens,
                            "cost_usd": round(event.cost_usd, 8),
                            "daily_total_usd": round(event.daily_total_usd, 8),
                            "role_total_usd": round(event.role_total_usd, 8),
                            "turn_id": event.turn_id,
                            "task_id": event.task_id,
                            "tier": event.tier,
                        }
                    )
                    + "\n"
                )
        except OSError as exc:
            # Warn-and-degrade: a disk error must not kill a turn.
            logger.warning("cost ledger JSONL write failed: %s (continuing)", exc)

    def _maybe_roll_midnight(self) -> None:
        today = self._today_fn()
        if self._current_local_date and self._current_local_date != today:
            self._daily_total_usd = 0.0
            self._role_totals_usd = {}
            self._voice_provider_totals_usd = {}
            # Cost UX overhaul: both warning + overage flags are
            # *daily*. Yesterday's "I approved overage" must NOT carry
            # into today — operator gets a clean budget at midnight.
            self._warned_today = set()
            # Yesterday's file is ignored by its own date stamp, so the cache
            # is all there is to clear. Dropping the fingerprint too, or the
            # next resync would see an unmoved file and skip the read that
            # would have told it today has nothing approved.
            self._overage_unlocked_today = set()
            self._unlocks_fingerprint = None
        self._current_local_date = today

    def _seed_from_log(self) -> None:
        today = self._today_fn()
        self._current_local_date = today
        if not self.log_path.exists():
            return
        try:
            with self.log_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("local_date") != today:
                        continue
                    cost = float(entry.get("cost_usd", 0.0))
                    self._daily_total_usd += cost
                    # Voice entries are tagged with `kind: voice_tts|voice_stt`
                    # and `provider: ...` instead of `role: ...`.
                    kind = entry.get("kind", "")
                    if kind.startswith("voice_"):
                        provider = entry.get("provider", "")
                        if provider:
                            self._voice_provider_totals_usd[provider] = (
                                self._voice_provider_totals_usd.get(provider, 0.0) + cost
                            )
                        continue
                    role = entry.get("role", "")
                    if role:
                        self._role_totals_usd[role] = (
                            self._role_totals_usd.get(role, 0.0) + cost
                        )
        except OSError as exc:
            logger.warning("cost ledger seed-from-log failed: %s (starting at 0)", exc)
