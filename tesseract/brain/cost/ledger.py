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
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable

import yaml

logger = logging.getLogger(__name__)

# OpenAI's published prompt-cache discount for GPT-5 family: cached input
# billed at 10% of the uncached rate. Default fallback when a model entry
# does not specify `cost_per_mtok_cached_in` explicitly. Anthropic cache-
# READ also bills at 10% of base, so this default works there too. Gemini
# bills cached input at ~25% of base — set the explicit field for that.
CACHED_INPUT_RATE = 0.1

# Anthropic prompt-cache WRITE surcharge: `cache_creation_input_tokens`
# from the message_start usage object are billed at 1.25× the base input
# rate. Other providers either don't report cache creation or fold it into
# uncached input — they leave `cache_creation_tokens=0` and this term
# contributes nothing.
CACHE_CREATION_RATE = 1.25

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
# and reset on midnight rollover. No on-disk persistence — restarting Mirror
# re-asks if a scope was previously unlocked, which is the correct safety
# bias (an unintended overage shouldn't survive an operator restart).
# `warning_at_pct` lives in models.yaml so the operator can tune it per
# project without a code change.

from tesseract.paths import CONFIG_DIR as _CONFIG_DIR, TESSERACT_HOME as _TESSERACT_HOME

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
    cached_tokens: int = 0
    # Anthropic-only: tokens written to the prompt cache during this turn.
    # Billed at 1.25× base input rate per the Anthropic prompt-cache docs
    # (creation surcharge). Adapters that don't expose this field leave it
    # at 0 and the surcharge term contributes nothing.
    cache_creation_tokens: int = 0


@dataclass(frozen=True)
class TtsUsage:
    """Voice-TTS usage DTO. Billed per character of synthesized text."""

    char_count: int = 0


@dataclass(frozen=True)
class SttUsage:
    """Voice-STT usage DTO. Billed per second of input audio."""

    seconds: float = 0.0


@dataclass(frozen=True)
class VoiceCostEvent:
    """One voice-billing entry. Distinct from `CostEvent` (token-based) —
    voice is billed in chars (TTS) or audio-seconds (STT), so the JSONL
    entry shape diverges from the chat-turn event."""

    timestamp: str
    local_date: str
    kind: str              # "voice_tts" | "voice_stt"
    provider: str
    char_count: int        # TTS only; 0 for STT
    seconds: float         # STT only; 0.0 for TTS
    cost_usd: float
    daily_total_usd: float
    provider_total_usd: float


@dataclass(frozen=True)
class CostEvent:
    """One appended ledger entry. UTC timestamp; daily grouping by local-tz date."""

    timestamp: str
    local_date: str
    role: str
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost_usd: float
    daily_total_usd: float
    role_total_usd: float
    cache_creation_tokens: int = 0


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
    tts: dict[str, tuple[float, float]] = {}
    stt: dict[str, tuple[float, float]] = {}
    if bundle.voice is None:
        return tts, stt
    voice = bundle.voice
    tts_chain = voice.tts.chain() if voice.tts is not None else ()
    stt_chain = voice.stt.chain() if voice.stt is not None else ()
    for entry in tts_chain:
        model = entry.ref.model
        if model.kind != "tts":
            continue
        rate = model.fields.get("cost_per_million_chars")
        if rate is None:
            continue
        tts[model.id] = (float(rate), float(entry.daily_budget_usd or 0.0))
    for entry in stt_chain:
        model = entry.ref.model
        if model.kind not in ("stt", "audio_stt"):
            continue
        rate = model.fields.get("cost_per_audio_hour", 0.0)
        stt[model.id] = (float(rate), float(entry.daily_budget_usd or 0.0))
    return tts, stt


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
    pricing: dict[str, tuple[float, float]]   # keyed by model name (provider catalog)
    log_path: Path
    # Per-model explicit cached-input rate. When absent for a model, the
    # ledger falls back to `cost_per_mtok_in × CACHED_INPUT_RATE`. Populated
    # from the optional `cost_per_mtok_cached_in` field on each catalog
    # entry — set it for providers (e.g. Gemini) whose cached rate isn't 10%.
    cached_pricing: dict[str, float] = field(default_factory=dict)
    voice_tts_pricing: dict[str, tuple[float, float]] = field(default_factory=dict)
    voice_stt_pricing: dict[str, tuple[float, float]] = field(default_factory=dict)
    providers_yaml: Path = _DEFAULT_PROVIDERS_YAML
    roles_yaml: Path = _DEFAULT_ROLES_YAML
    # Test-only — when set, ``reload()`` re-parses this single-file fixture
    # instead of going through the loader. None in production.
    _test_fixture_yaml: Path | None = None

    _daily_total_usd: float = 0.0
    _role_totals_usd: dict[str, float] = field(default_factory=dict)
    _voice_provider_totals_usd: dict[str, float] = field(default_factory=dict)
    _current_local_date: str = ""
    _lock: Lock = field(default_factory=Lock)
    _today_fn: Callable[[], str] = field(default=_local_today_iso)
    _subscribers: list[CostSubscriber] = field(default_factory=list)
    # Cost UX overhaul (see WARNING_AT_PCT). Both reset on midnight roll.
    _warned_today: set[str] = field(default_factory=set)
    _overage_unlocked_today: set[str] = field(default_factory=set)
    # Operator-paused spend sources (a role name or "global"). A paused source
    # hard-blocks in check_preflight regardless of remaining cap. Runtime-only
    # (no on-disk persistence) — a restart clears pauses, the same safety bias
    # as _overage_unlocked_today. Not reset at midnight (a pause is an explicit
    # operator hold, not a per-day budget flag).
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

        per_role_caps: dict[str, float] = {}
        for role_name, role_cfg in bundle.roles.items():
            cap = role_cfg.overrides.get("daily_budget_usd")
            if cap is not None:
                per_role_caps[role_name] = float(cap)

        pricing: dict[str, tuple[float, float]] = {}
        cached_pricing: dict[str, float] = {}
        for _ref, _conn, model in bundle.all_models():
            p_in = model.fields.get("cost_per_mtok_in")
            p_out = model.fields.get("cost_per_mtok_out")
            if p_in is None or p_out is None:
                continue
            pricing[model.model] = (float(p_in), float(p_out))
            p_cached = model.fields.get("cost_per_mtok_cached_in")
            if p_cached is not None:
                cached_pricing[model.model] = float(p_cached)

        voice_tts_pricing, voice_stt_pricing = _voice_pricing_from_bundle(bundle)

        resolved_log_path = log_path if log_path is not None else _TESSERACT_HOME / log_file

        ledger = cls(
            enabled=enabled,
            warning_at_pct=warning_at_pct,
            per_role_caps=per_role_caps,
            pricing=pricing,
            cached_pricing=cached_pricing,
            voice_tts_pricing=voice_tts_pricing,
            voice_stt_pricing=voice_stt_pricing,
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
        ``tesseract/tests/fix_pass_*`` keep using compact inline dicts
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

        pricing: dict[str, tuple[float, float]] = {}
        cached_pricing: dict[str, float] = {}
        for _role_name, role_cfg in (raw.get("roles") or {}).items():
            for entry in role_cfg.get("resolution") or []:
                model = entry.get("model")
                p_in = entry.get("cost_per_mtok_in")
                p_out = entry.get("cost_per_mtok_out")
                if model and p_in is not None and p_out is not None and model not in pricing:
                    pricing[model] = (float(p_in), float(p_out))
                    p_cached = entry.get("cost_per_mtok_cached_in")
                    if p_cached is not None:
                        cached_pricing[model] = float(p_cached)

        voice_raw = ct_raw.get("voice") or {}
        voice_tts: dict[str, tuple[float, float]] = {}
        for provider, cfg in (voice_raw.get("tts") or {}).items():
            rate = cfg.get("cost_per_million_chars")
            cap = cfg.get("daily_budget_usd")
            if rate is None or cap is None:
                continue
            voice_tts[provider] = (float(rate), float(cap))
        voice_stt: dict[str, tuple[float, float]] = {}
        for provider, cfg in (voice_raw.get("stt") or {}).items():
            rate = cfg.get("cost_per_audio_hour")
            cap = cfg.get("daily_budget_usd")
            if rate is None or cap is None:
                continue
            voice_stt[provider] = (float(rate), float(cap))

        resolved_log_path = log_path if log_path is not None else _TESSERACT_HOME / log_file

        ledger = cls(
            enabled=enabled,
            warning_at_pct=warning_at_pct,
            per_role_caps=per_role_caps,
            pricing=pricing,
            cached_pricing=cached_pricing,
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
            + sum(cap for _, cap in self.voice_tts_pricing.values())
            + sum(cap for _, cap in self.voice_stt_pricing.values())
        )

    @property
    def warning_usd(self) -> float:
        """Global warning threshold — same percentage that applies to
        every inner cap. Kept as a `_usd` value for the BudgetState DTO
        and HUD chip math."""
        return self.cap_usd * self.warning_at_pct

    # ── Public API ─────────────────────────────────────────────

    def record(self, role: str, model: str, usage: CostUsage) -> CostEvent:
        """Compute USD, append JSONL, return the event.

        Unknown `(role, model)` raises — silent zero-billing is a bug, not a
        feature. CLI roles priced at 0 still write an event (visibility)
        but contribute $0 to the totals. After the JSONL write, subscribers
        registered via `subscribe()` are fired outside the lock with
        `(event, budget_state)` so callbacks may invoke other ledger
        methods without deadlocking.
        """
        if not self.enabled:
            with self._lock:
                return self._build_event(role, model, usage, cost_usd=0.0)

        with self._lock:
            self._maybe_roll_midnight()
            cost = self._compute_usd(role, model, usage)
            self._daily_total_usd += cost
            self._role_totals_usd[role] = self._role_totals_usd.get(role, 0.0) + cost
            event = self._build_event(role, model, usage, cost_usd=cost)
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

        new_caps: dict[str, float] = {}
        for role_name, role_cfg in bundle.roles.items():
            cap = role_cfg.overrides.get("daily_budget_usd")
            if cap is not None:
                new_caps[role_name] = float(cap)

        new_pricing: dict[str, tuple[float, float]] = {}
        new_cached_pricing: dict[str, float] = {}
        for _ref, _conn, model in bundle.all_models():
            p_in = model.fields.get("cost_per_mtok_in")
            p_out = model.fields.get("cost_per_mtok_out")
            if p_in is None or p_out is None:
                continue
            new_pricing[model.model] = (float(p_in), float(p_out))
            p_cached = model.fields.get("cost_per_mtok_cached_in")
            if p_cached is not None:
                new_cached_pricing[model.model] = float(p_cached)

        new_voice_tts, new_voice_stt = _voice_pricing_from_bundle(bundle)

        with self._lock:
            # Roll midnight before the write so reload-after-midnight doesn't
            # strand yesterday's totals under today's caps. `budget_state()`
            # would roll on the next read anyway, but Phase 14 may also read
            # internal counters (or a `cap_usd` snapshot) right after reload.
            self._maybe_roll_midnight()
            self.enabled = new_enabled
            self.warning_at_pct = new_warn_pct
            self.per_role_caps = new_caps
            self.pricing = new_pricing
            self.cached_pricing = new_cached_pricing
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

        new_pricing: dict[str, tuple[float, float]] = {}
        new_cached_pricing: dict[str, float] = {}
        for _role_name, role_cfg in (raw.get("roles") or {}).items():
            for entry in role_cfg.get("resolution") or []:
                model = entry.get("model")
                p_in = entry.get("cost_per_mtok_in")
                p_out = entry.get("cost_per_mtok_out")
                if model and p_in is not None and p_out is not None and model not in new_pricing:
                    new_pricing[model] = (float(p_in), float(p_out))
                    p_cached = entry.get("cost_per_mtok_cached_in")
                    if p_cached is not None:
                        new_cached_pricing[model] = float(p_cached)

        voice_raw = ct_raw.get("voice") or {}
        new_voice_tts: dict[str, tuple[float, float]] = {}
        for provider, cfg in (voice_raw.get("tts") or {}).items():
            rate = cfg.get("cost_per_million_chars")
            cap = cfg.get("daily_budget_usd")
            if rate is None or cap is None:
                continue
            new_voice_tts[provider] = (float(rate), float(cap))
        new_voice_stt: dict[str, tuple[float, float]] = {}
        for provider, cfg in (voice_raw.get("stt") or {}).items():
            rate = cfg.get("cost_per_audio_hour")
            cap = cfg.get("daily_budget_usd")
            if rate is None or cap is None:
                continue
            new_voice_stt[provider] = (float(rate), float(cap))

        with self._lock:
            self._maybe_roll_midnight()
            self.enabled = new_enabled
            self.warning_at_pct = new_warn_pct
            self.per_role_caps = new_caps
            self.pricing = new_pricing
            self.cached_pricing = new_cached_pricing
            self.voice_tts_pricing = new_voice_tts
            self.voice_stt_pricing = new_voice_stt

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
            global_warning = self._daily_total_usd >= self.warning_usd
            global_blocked = self._daily_total_usd >= self.cap_usd
            roles: dict[str, dict[str, Any]] = {}
            # Always populate the four canonical roles so the chips render
            # $0.00 / $cap immediately on a fresh day, even if no turn has
            # been billed yet.
            for role in ("chat_brain", "observer_agent"):
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
                "tts": {
                    provider: {
                        "spent_usd": self._voice_provider_totals_usd.get(provider, 0.0),
                        "cap_usd": cap,
                        "rate": rate,
                    }
                    for provider, (rate, cap) in self.voice_tts_pricing.items()
                },
                "stt": {
                    provider: {
                        "spent_usd": self._voice_provider_totals_usd.get(provider, 0.0),
                        "cap_usd": cap,
                        "rate": rate,
                    }
                    for provider, (rate, cap) in self.voice_stt_pricing.items()
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
            return scope_key in self._overage_unlocked_today

    def unlock_overage(self, scope_key: str) -> None:
        """Operator approved continuing past 100%. Until midnight,
        future preflight checks for this scope will skip the cap test.
        Idempotent."""
        with self._lock:
            self._maybe_roll_midnight()
            self._overage_unlocked_today.add(scope_key)

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
        `voice_stt_pricing` shape. The cap is the second element of the
        pricing tuple — see `_parse_voice_pricing`.
        """
        if kind == "tts":
            entry = self.voice_tts_pricing.get(provider)
        elif kind == "stt":
            entry = self.voice_stt_pricing.get(provider)
        else:
            return None
        return entry[1] if entry is not None else None

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
        # Cap == 0 means the provider is free at use-time (local Piper /
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
        p_in, p_out = pricing
        uncached_input = max(0, usage.input_tokens - usage.cached_tokens)
        p_cached = self.cached_pricing.get(model, p_in * CACHED_INPUT_RATE)
        return (
            uncached_input * p_in
            + usage.cached_tokens * p_cached
            + usage.cache_creation_tokens * p_in * CACHE_CREATION_RATE
            + usage.output_tokens * p_out
        ) / 1_000_000

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
            rate, _cap = entry
            chars = getattr(usage, "char_count", 0)
            return chars * rate / 1_000_000
        if kind == "stt":
            entry = self.voice_stt_pricing.get(provider)
            if entry is None:
                raise RuntimeError(
                    f"no STT pricing for provider={provider} — add "
                    f"cost_tracking.voice.stt.{provider} to models.yaml"
                )
            rate, _cap = entry
            seconds = float(getattr(usage, "seconds", 0.0))
            return seconds * rate / 3600.0
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
        self, role: str, model: str, usage: CostUsage, cost_usd: float
    ) -> CostEvent:
        now_utc = datetime.now(timezone.utc).replace(microsecond=0)
        return CostEvent(
            timestamp=now_utc.isoformat().replace("+00:00", "Z"),
            local_date=self._today_fn(),
            role=role,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            cost_usd=cost_usd,
            daily_total_usd=self._daily_total_usd,
            role_total_usd=self._role_totals_usd.get(role, 0.0),
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
                            "cost_usd": round(event.cost_usd, 8),
                            "daily_total_usd": round(event.daily_total_usd, 8),
                            "role_total_usd": round(event.role_total_usd, 8),
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
            self._overage_unlocked_today = set()
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
