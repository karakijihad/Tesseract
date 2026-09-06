"""Kernel infra bootstrap — shared between the Mirror backend
and the Mirror backend.

Everything in this module is "build the runtime from yaml": adapters, tool
registry, observer, memory bundle, embeddings index. No interactive I/O,
no CLI-only concerns.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

import httpx
import yaml

if TYPE_CHECKING:
    from tesseract.permissions.policy import PermissionPolicy

from tesseract import http_client
from tesseract.agents.loader import load_agent
from tesseract.brain.compaction_control import open_chat_sessions
from tesseract.brain.cost import CostLedger
from tesseract.brain.observer import Observer, build_observer_from_config
from tesseract.brain.tools import ToolRegistry
from tesseract.config.loader import (
    DEFAULT_COMPACT_RATIO,
    DEFAULT_HEADROOM_MULTIPLIER,
    DEFAULT_PROMPT_CHAR_BUDGET,
    DEFAULT_KEEP_RECENT_TURNS,
    PROVIDERS_YAML,
    ROLES_YAML,
    ConfigBundle,
    ConfigError,
    ResolvedRef,
    RoleConfig,
    load_config,
    require_field as _require,
    resolve_output_cap,
    resolve_temperature,
)
from tesseract.brain.lazy_adapter import LazyAdapter
from tesseract.kernel.adapters._estimate import CHARS_PER_TOKEN
from tesseract.kernel.adapters.anthropic import AnthropicAdapter
from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.kernel.adapters.gemini import GeminiAdapter
from tesseract.kernel.adapters.openai import OpenAIAdapter
from tesseract.kernel.adapters.screened import ScreenedAdapter
from tesseract.agents.contract import check_shipped_cards
from tesseract.brain.playbook_contract import check_playbooks
from tesseract.kernel.tools.base import VALID_RISK_CLASSES, check_tool_contract
from tesseract.kernel.tools.agent_create import AgentCreateTool
from tesseract.kernel.tools.agent_promote import AgentPromoteTool
from tesseract.kernel.tools.skill_create import SkillCreateTool
from tesseract.kernel.tools.skill_promote import SkillPromoteTool
from tesseract.kernel.tools.playbook_search import PlaybookSearchTool
from tesseract.kernel.tools.skill_refine import SkillRefineTool
from tesseract.kernel.tools.bash_tool import BashTool
from tesseract.kernel.tools.breaker_reset import BreakerResetTool
from tesseract.kernel.tools.breaker_status import BreakerStatusTool
from tesseract.kernel.tools.conscience import ConscienceStatusTool
from tesseract.kernel.tools.context7 import Context7LookupTool
from tesseract.kernel.tools.delegate_coder import DelegateCoderTool
from tesseract.kernel.tools.delegate_auditor import DelegateAuditorTool
from tesseract.kernel.tools.agent_ask import AgentAskTool
from tesseract.kernel.tools.delegate_second_opinion import DelegateSecondOpinionTool
from tesseract.kernel.tools.delegate_agent_controller import (
    DelegateAgentControllerTool,
)
from tesseract.kernel.tools.start_controller_session import (
    StartControllerSessionTool,
)
from tesseract.kernel.tools.diary_append import DiaryAppendTool
from tesseract.kernel.tools.tasks_set import TasksSetTool
from tesseract.kernel.tools.tasks_update import TasksUpdateTool
from tesseract.kernel.tools.spawn_check import SpawnCheckTool
from tesseract.kernel.tools.spawn_await import SpawnAwaitTool
from tesseract.kernel.tools.spawn_cancel import SpawnCancelTool
from tesseract.kernel.tools.file_read import FileReadTool
from tesseract.kernel.tools.file_transfer import FileCopyTool, FileMoveTool
from tesseract.kernel.tools.file_write import FileWriteTool
from tesseract.kernel.tools.git_tool import GitTool
from tesseract.kernel.tools.glob_tool import GlobTool
from tesseract.kernel.tools.grep_tool import GrepTool
from tesseract.kernel.tools.log_triage import LogTriageTool
from tesseract.kernel.tools.system_diagnose import SystemDiagnoseTool
from tesseract.kernel.tools.channel_history import ChannelHistoryReadTool
from tesseract.kernel.tools.channel_send import (
    ChannelReactTool,
    ChannelSendAnimationTool,
    ChannelSendDocumentTool,
    ChannelSendLocationTool,
    ChannelSendPhotoTool,
    ChannelSendPollTool,
    ChannelSendStickerTool,
    ChannelSendVideoNoteTool,
    ChannelSendVideoTool,
    ChannelSendVoiceTool,
)
from tesseract.kernel.tools.image_generate import ImageGenerateTool
from tesseract.kernel.tools.invoke_agent import InvokeAgentTool
from tesseract.kernel.tools.session_tools import (
    SessionCloseTool,
    SessionListTool,
    SessionOpenTool,
    SessionResultTool,
    SessionSendTool,
)
from tesseract.kernel.tools.controller_session_list import (
    ControllerSessionListTool,
)
from tesseract.kernel.tools.memory_forget import MemoryForgetTool
from tesseract.kernel.tools.memory_promote import MemoryPromoteTool
from tesseract.kernel.tools.memory_save import MemorySaveTool
from tesseract.kernel.tools.memory_search import MemorySearchTool
from tesseract.kernel.tools.memory_update import MemoryUpdateTool
from tesseract.kernel.tools.pdf_read import PdfReadTool
from tesseract.kernel.tools.alarm_cancel import AlarmCancelTool
from tesseract.kernel.tools.alarm_list import AlarmListTool
from tesseract.kernel.tools.alarm_set import AlarmSetTool
from tesseract.kernel.tools.alarm_snooze import AlarmSnoozeTool
from tesseract.kernel.tools.memory_get import MemoryGetTool
from tesseract.kernel.tools.workspace_read import WorkspaceReadTool
from tesseract.kernel.tools.atlas_query import AtlasQueryTool
from tesseract.kernel.tools.brief_read import BriefReadTool
from tesseract.kernel.tools.brief_render import BriefRenderTool
from tesseract.kernel.tools.ask_clarification import AskClarificationTool
from tesseract.kernel.tools.credential_list import CredentialListTool
from tesseract.kernel.tools.credential_request import CredentialRequestTool
from tesseract.kernel.tools.api_request import ApiRequestTool
from tesseract.kernel.tools.command_run import CommandRunTool
from tesseract.kernel.tools.credential_setup import CredentialSetupTool
from tesseract.kernel.tools.project_link import ProjectLinkTool
from tesseract.kernel.tools.project_list import ProjectListTool
from tesseract.kernel.tools.project_new import ProjectNewTool
from tesseract.kernel.tools.project_open import ProjectOpenTool
from tesseract.kernel.tools.schedule_create import ScheduleCreateTool
from tesseract.kernel.tools.schedule_list import ScheduleListTool
from tesseract.kernel.tools.schedule_remove import ScheduleRemoveTool
from tesseract.kernel.tools.schedule_run import ScheduleRunTool
from tesseract.kernel.tools.schedule_update import ScheduleUpdateTool
from tesseract.kernel.tools.lane_attach import LaneAttachTool
from tesseract.kernel.tools.lane_close import LaneCloseTool
from tesseract.kernel.tools.lane_list import LaneListTool
from tesseract.kernel.tools.lane_named_ensure import LaneNamedEnsureTool
from tesseract.kernel.tools.lane_named_get import LaneNamedGetTool
from tesseract.kernel.tools.lane_named_list import LaneNamedListTool
from tesseract.kernel.tools.lane_open import LaneOpenTool
from tesseract.kernel.tools.lane_read import LaneReadTool
from tesseract.kernel.tools.surface_bind_session import SurfaceBindSessionTool
from tesseract.kernel.tools.surface_close import SurfaceCloseTool
from tesseract.kernel.tools.open_target import OpenTool
from tesseract.kernel.tools.os_launch import OsLaunchTool
from tesseract.kernel.tools.os_open_url import OsOpenUrlTool
from tesseract.kernel.tools.surface_create import SurfaceCreateTool
from tesseract.kernel.tools.surface_focus import SurfaceFocusTool
from tesseract.kernel.tools.surface_highlight import SurfaceHighlightTool
from tesseract.kernel.tools.screen_look import ScreenLookTool
from tesseract.kernel.tools.surface_list import SurfaceListTool
from tesseract.kernel.tools.surface_update import SurfaceUpdateTool
from tesseract.kernel.tools.lane_send import LaneSendTool
from tesseract.kernel.tools.lane_turn import LaneTurnTool
from tesseract.kernel.tools.work_send import WorkSendTool
from tesseract.kernel.tools.lane_status import LaneStatusTool
from tesseract.kernel.tools.set_mood import SetMoodTool
from tesseract.kernel.tools.set_state import EntityAffect, SetStateTool
from tesseract.kernel.tools.propose_change import ProposeChangeTool
from tesseract.kernel.tools.soul_growth_propose import SoulGrowthProposeTool
from tesseract.kernel.tools.workspace_post import WorkspacePostTool
from tesseract.kernel.tools.workspace_reply import WorkspaceReplyTool
from tesseract.kernel.tools.tavily_extract import TavilyExtractTool
from tesseract.kernel.tools.tavily_search import TavilySearchTool
from tesseract.kernel.tools.vault_ingest import VaultIngestTool
from tesseract.kernel.tools.vault_lint import VaultLintTool
from tesseract.kernel.tools.vault_query import VaultQueryTool
from tesseract.kernel.tools.vault_search import VaultSearchTool
from tesseract.kernel.tools.web_search import WebSearchTool
from tesseract.kernel.tools.browser_tools import (
    BrowserNavigateTool, BrowserSnapshotTool, BrowserClickTool,
    BrowserFillFormTool, BrowserScreenshotTool,
    BrowserNetworkRequestsTool, BrowserCloseTool,
    BrowserKeyTool, BrowserMediaTool, BrowserScrollTool,
    BrowserHoverTool, BrowserSelectTool, BrowserWaitForTool,
)
from tesseract.kernel.tools.tool_search import ToolSearchTool
from tesseract.memory.dreaming import DreamingEngine
from tesseract.memory.embeddings import EmbeddingIndex
from tesseract.memory.fts_index import FTSIndex
from tesseract.memory.index import MemoryIndex
from tesseract.memory.librarian import Librarian
from tesseract.memory.retrieval import RetrievalPipeline
from tesseract.memory.store import MemoryStore
from tesseract.memory.vault_indexer import VaultIndexer
from tesseract.memory.vault_librarian import VaultLibrarian
from tesseract.memory.vault_manager import VaultManager
from tesseract.orchestrator.mood_state import MoodState
from tesseract.scheduler.alarms import AlarmRegistry, alarms_state_path

# ── Paths ────────────────────────────────────────────────

logger = logging.getLogger(__name__)

from tesseract.paths import CONFIG_DIR, ROOT, TESSERACT_HOME, home_dir, home_logs_root, log_dir, workspace_dir
from tesseract.paths import user_agents_dir

ENV_PATH = TESSERACT_HOME / ".env"
PERMISSIONS_YAML = CONFIG_DIR / "permissions.yaml"
VAULT_YAML = CONFIG_DIR / "vault.yaml"

# Tool-schema tiering. Every registered tool defaults to
# `Tool.tier == "extended"` (schema hidden from the chat model until
# `tool_search` surfaces it); the names in `working_set.yaml::core` are marked
# `tier = "core"` at the end of `build_tool_registry` so their schemas are
# always in the per-turn payload.
#
# The roster is NOT here. It was a frozenset in this file until 2026-08-21,
# inside the tree that becomes the sealed `app/` on an install, so an installed
# operator could not change which tools their assistant loads every turn — not
# by editing config, not through the Mirror, not at all. It is the last major
# behavioural knob to leave source. `config/working_set.py` owns the read and
# the floor; this module owns the guard, because this is where a registry first
# exists to check a name against.
_CORE_TOOL_NAMES_CACHE: frozenset[str] | None = None


def core_tool_names(*, refresh: bool = False) -> frozenset[str]:
    """The configured working set. Cached per process; `refresh=True` re-reads.

    Cached because `_apply_tool_tiers` and the rebuild path both want it and a
    registry build should not depend on how many times the file is read.
    """
    global _CORE_TOOL_NAMES_CACHE
    if refresh or _CORE_TOOL_NAMES_CACHE is None:
        from tesseract.config.working_set import load_core_tool_names

        _CORE_TOOL_NAMES_CACHE = load_core_tool_names()
    return _CORE_TOOL_NAMES_CACHE


# Tools that register CONDITIONALLY rather than always. Two jobs: a name
# here is exempt from `_apply_tool_tiers`' missing-tool guard (so a core
# entry that legitimately did not register does not hard-fail a
# credential-less CI run or a minimal test boot), and `check_tool_claims`
# unions this set so a workspace document may name one without the guard
# calling it invented. Every other working-set entry still hard-fails
# when missing — that is the typo net.
_CONDITIONAL_CORE_TOOL_NAMES: frozenset[str] = frozenset({
    "session_open",
    "invoke_agent",  # same adapter guard as session_open
    # Registers AFTER boot, once the STT engine is up (`transcribe_audio:
    # registered against local STT engine`) — absent at yaml-sync time on
    # every boot, and permanently absent when STT is unavailable.
    "transcribe_audio",
})

_SHELL_VAR_RE = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}$")


def resolve_env(value: str) -> str:
    """Expand ${VAR:-default} syntax; returns value unchanged if no match.

    Kept here for back-compat with callers that still resolve a plain
    string (e.g. external config knobs in vault.yaml). New code should
    let `tesseract.config.loader` resolve env vars during catalog parse.
    """
    m = _SHELL_VAR_RE.match(value or "")
    if not m:
        return value
    return os.environ.get(m.group(1)) or (m.group(2) or "")


# ── Config readers ───────────────────────────────────────




@dataclass(frozen=True)
class ChatBrainConfig:
    """Typed view of one resolved chat_brain entry (primary or fallback).

    Built from a :class:`tesseract.config.loader.ResolvedRef` plus role-level
    overrides (compact_threshold, keep_recent_turns, *_override fields). The
    raw provider connection block stays accessible via ``provider_cfg`` for
    legacy call sites that still poke at timeout / max_retries / base_url
    directly.
    """
    provider: str
    model: str
    tier: str
    temperature: float | None
    max_output_tokens: int
    context_window: int
    reasoning_effort: str
    knowledge_cutoff: str
    use_responses_api: bool
    compact_threshold: float
    keep_recent_turns: int
    # Sliding-window knobs. See
    # tesseract/brain/chat.py module docstring.
    head_anchor_messages: int
    summary_char_budget: int
    provider_cfg: dict
    ref: ResolvedRef
    tool_iteration_cap: int
    consecutive_error_cap: int
    # Per-model catalog quirk; absence means stream. See `AdapterOptions.stream`.
    stream: bool = True
    # Per-model catalog quirk; absence means the provider picks its own
    # cache breakpoint. See `AdapterOptions.prompt_cache_explicit`.
    prompt_cache_explicit: bool = False
    # Global, from `roles.yaml::compaction`, not a role override: it describes
    # how compaction works rather than who is using it. Defaulted because
    # there is exactly one shipped value and callers do not choose it.
    headroom_multiplier: float = DEFAULT_HEADROOM_MULTIPLIER
    # Also global and for the same reason: it describes the guard rather than
    # who is using it. The hard ceiling on one assembled prompt.
    prompt_char_budget: int = DEFAULT_PROMPT_CHAR_BUDGET


def _provider_cfg_dict(ref: ResolvedRef) -> dict:
    """Flatten the typed connection back to the dict shape legacy callers expect."""
    conn = ref.connection
    out: dict = {
        "tier": conn.tier,
        "provider": conn.name,
        "adapter": conn.adapter,
        "timeout_seconds": conn.timeout_seconds,
        "max_retries": conn.max_retries,
    }
    if conn.base_url is not None:
        out["base_url"] = conn.base_url
    if conn.api_key_env is not None:
        out["api_key_env"] = conn.api_key_env
    if conn.command is not None:
        out["command"] = conn.command
    if conn.stream_json_capable:
        out["stream_json_capable"] = True
    out.update(dict(conn.extra))
    return out


def _compact_ratio(
    role_overrides: Mapping[str, Any],
    compaction: Mapping[str, Any] | None,
    context_window: int,
    char_budget: int,
    where: str,
) -> float:
    """The one number that decides when a conversation folds.

    A role may still name `compact_threshold` and it wins; none ships one.
    Whichever answers, it is checked against the guard before it is returned,
    so a ratio nothing can reach fails at boot rather than at the first fold
    that never comes.
    """
    ratio = float(
        role_overrides.get(
            "compact_threshold",
            (compaction or {}).get("compact_ratio", DEFAULT_COMPACT_RATIO),
        )
    )
    _check_ratio_fits_the_guard(ratio, context_window, char_budget, where)
    return ratio


def _check_ratio_fits_the_guard(
    ratio: float, context_window: int, char_budget: int, where: str
) -> None:
    """Refuse a `compact_ratio` whose payload cannot fit the char guard.

    The guard is a hard ceiling on one assembled prompt, in characters, and a
    fold that is supposed to happen above it can never happen: the guard trims
    the payload back down first, every turn, and the conversation is bounded by
    the guard rather than by the dial. That is the state this check exists to
    make impossible to configure.

    It replaces `compaction.trigger_share`, which answered the same danger by
    deriving a SECOND fold trigger below the guard. That trigger was lower than
    the ratio's on every real configuration, so it decided every fold and the
    operator's dial decided nothing, which is the whole reason this check is
    written as a refusal instead.

    Refused rather than clamped: a value the runtime silently corrected is a
    setting the operator believes is in force. The message carries the highest
    ratio that does fit, because a refusal a person cannot act on is half a
    message.
    """
    if context_window <= 0 or char_budget <= 0:
        return
    wanted_chars = ratio * context_window * CHARS_PER_TOKEN
    if wanted_chars <= char_budget:
        return
    highest = char_budget / (context_window * CHARS_PER_TOKEN)
    raise ValueError(
        f"{where}: roles.yaml::compaction.compact_ratio is {ratio}, which folds "
        f"at about {wanted_chars:,.0f} characters, and the tightest model in "
        f"this chain accepts {char_budget:,}. The guard would trim the payload "
        f"back under that ceiling before the fold could ever run, so the "
        f"conversation would be bounded by the guard and not by this dial. "
        f"Lower compact_ratio to {highest:.3f} or below, or raise that model's "
        f"providers.yaml::max_prompt_chars if it really does accept more"
    )


def _chat_brain_from_ref(
    ref: ResolvedRef,
    role_overrides: dict,
    where: str,
    compaction: Mapping[str, Any] | None = None,
) -> ChatBrainConfig:
    """Build a ChatBrainConfig from a resolved ref, layering role overrides.

    Model fields come from the catalog; role-level *_override keys (e.g.
    ``reasoning_effort_override``, ``max_output_tokens_override``) replace the
    catalog value. ``keep_recent_turns`` lives only on the role.
    ``compact_threshold`` may too, and wins there, but no shipped role names
    one: the answer is ``roles.yaml::compaction.compact_ratio``, which is why
    every caller passes the ``compaction`` block.

    `context_window` is required. `temperature` is not: an entry that declares
    none is saying the model takes none — `cli.claude.opus_5` and
    `api.anthropic.opus_5` both do — and adapters omit the field rather than
    invent a value. The output cap is resolved through the same helper the
    scheduler uses, so `max_output_ratio` is accepted here too. Those two
    consumers previously disagreed: pointing `chat_brain` at a CLI ref raised
    at boot while the identical ref built fine for a scheduler job.
    """
    fields = ref.model.fields
    _where_ref = f"{where} {ref.ref}"
    eff_reasoning = role_overrides.get(
        "reasoning_effort_override",
        fields.get("reasoning_effort", "none"),
    )
    context_window = int(_require(fields, "context_window", _where_ref))
    # The ceiling belongs to the model. `providers.yaml` states it per entry,
    # measured where a model rejects on characters and derived from its token
    # window where it does not. The `compaction` block is what an entry that
    # declares nothing falls back to, so a new catalog row is guarded before
    # anyone remembers the field.
    char_budget = int(
        fields.get("max_prompt_chars")
        or (compaction or {}).get("prompt_char_budget", DEFAULT_PROMPT_CHAR_BUDGET)
    )
    eff_max_out = int(role_overrides.get(
        "max_output_tokens_override",
        resolve_output_cap(fields, context_window, _where_ref),
    ))
    return ChatBrainConfig(
        provider=ref.connection.name,
        model=ref.model.model,
        tier=ref.connection.tier,
        temperature=resolve_temperature(fields),
        max_output_tokens=eff_max_out,
        context_window=context_window,
        reasoning_effort=str(eff_reasoning),
        knowledge_cutoff=str(fields.get("knowledge_cutoff", "")),
        use_responses_api=bool(fields.get("use_responses_api", False)),
        prompt_cache_explicit=bool(fields.get("prompt_cache_explicit", False)),
        stream=bool(fields.get("stream", True)),
        compact_threshold=_compact_ratio(
            role_overrides, compaction, context_window, char_budget, where,
        ),
        headroom_multiplier=float(
            (compaction or {}).get("headroom_multiplier", DEFAULT_HEADROOM_MULTIPLIER),
        ),
        prompt_char_budget=char_budget,
        keep_recent_turns=int(
            role_overrides.get("keep_recent_turns", DEFAULT_KEEP_RECENT_TURNS)
        ),
        head_anchor_messages=int(role_overrides.get("head_anchor_messages", 3)),
        summary_char_budget=int(role_overrides.get("summary_char_budget", 8_000)),
        # Required YAML keys — no module-level fallbacks. The chat-loop tool
        # cap and adapter-error breaker are owned by `roles.yaml::roles.<role>`
        # so operators can tune them via the Settings panel.
        tool_iteration_cap=int(_require(role_overrides, "tool_iteration_cap", where)),
        consecutive_error_cap=int(_require(role_overrides, "consecutive_error_cap", where)),
        provider_cfg=_provider_cfg_dict(ref),
        ref=ref,
    )


def load_bundle() -> ConfigBundle:
    """Read providers.yaml + roles.yaml on every call.

    Cheap (two file reads) and inherently fresh — Mirror's hot-reload path
    relies on each call seeing the current on-disk state. `load_config()`'s
    bare call resolves `config_dir()` fresh each time (CL-m6) — tests point
    this at fixture files via `monkeypatch.setenv("TESSERACT_HOME", ...)` +
    seeding `<home>/config/{providers,roles}.yaml`, not by monkeypatching
    the (frozen, back-compat-only) `PROVIDERS_YAML` / `ROLES_YAML` module
    constants, which this function does not consult.
    """
    return load_config()


@dataclass(frozen=True)
class ChainConfig:
    """Knobs that govern `FallbackAdapter` retry-then-advance behavior.

    Read from the top-level `chain:` block in ``providers.yaml``. All
    keys are required — a missing key raises rather than falling back
    to a hardcoded model id, URL or timeout.
    """

    transient_retries: int
    transient_backoff_ms: int
    cooldown_max_failures: int
    # Two windows, because a dropped socket and a spent balance do not clear
    # on the same clock. Which kind of failure takes which is
    # `adapter_chain.py::_WINDOW_CLASS`.
    cooldown_seconds: float
    cooldown_seconds_until_fixed: float


def load_chain_config(bundle: ConfigBundle | None = None) -> ChainConfig:
    """Read the `chain:` block from providers.yaml — required."""
    bundle = bundle or load_bundle()
    chain_raw = _require(dict(bundle.providers_raw), "chain", "providers.yaml")
    return ChainConfig(
        transient_retries=int(_require(chain_raw, "transient_retries", "providers.yaml chain")),
        transient_backoff_ms=int(_require(chain_raw, "transient_backoff_ms", "providers.yaml chain")),
        cooldown_max_failures=int(_require(chain_raw, "cooldown_max_failures", "providers.yaml chain")),
        cooldown_seconds=float(_require(chain_raw, "cooldown_seconds", "providers.yaml chain")),
        cooldown_seconds_until_fixed=float(
            _require(chain_raw, "cooldown_seconds_until_fixed", "providers.yaml chain")
        ),
    )


def build_fallback_adapter(
    chain: list[tuple[ModelAdapter, AdapterOptions]],
    cfg: ChainConfig | None = None,
):
    """Build a `FallbackAdapter` wired to providers.yaml's `chain:` block.

    Sole call site for production code — keeps the
    transient_retries/backoff/cooldown knobs in config rather than
    scattered across boot/repl/mirror. Tests construct `FallbackAdapter`
    directly with explicit args.
    """
    from tesseract.brain.adapter_chain import FallbackAdapter

    cfg = cfg or load_chain_config()
    return FallbackAdapter(
        chain,
        transient_retries=cfg.transient_retries,
        transient_backoff_ms=cfg.transient_backoff_ms,
        cooldown_max_failures=cfg.cooldown_max_failures,
        cooldown_seconds=cfg.cooldown_seconds,
        cooldown_seconds_until_fixed=cfg.cooldown_seconds_until_fixed,
    )


def load_chat_brain_config() -> ChatBrainConfig:
    """Return the typed primary chat_brain config (the role's `primary` ref)."""
    return load_chat_brain_chain()[0]


def load_chat_brain_chain(bundle: ConfigBundle | None = None) -> list[ChatBrainConfig]:
    """Return the full ordered chat_brain fallback chain.

    Walks ``roles.chat_brain.primary`` then each of ``roles.chat_brain.fallbacks``,
    parses each into a typed ChatBrainConfig, and returns the list. Raises
    on missing role / missing required field — config errors fail loudly.
    Runtime availability (API keys, live endpoints) is a later concern.

    ``bundle`` lets a caller assembling several views of the same config read
    it once — matching ``load_chain_config``. Absent, it reads its own.
    """
    bundle = bundle or load_bundle()
    try:
        role = bundle.role("chat_brain")
    except ConfigError as exc:
        raise RuntimeError(str(exc)) from exc
    overrides = dict(role.overrides)
    refs: list[ResolvedRef] = [role.primary, *role.fallbacks]
    configs = [
        _chat_brain_from_ref(
            ref, overrides, "roles.yaml roles.chat_brain",
            compaction=bundle.roles_raw.get("compaction") or {},
        )
        for ref in refs
    ]
    return _apply_chain_ceiling(configs, "roles.yaml roles.chat_brain")


def _apply_chain_ceiling(
    configs: list[ChatBrainConfig], where: str
) -> list[ChatBrainConfig]:
    """Give every member of a chain the chain's tightest character ceiling.

    A fold has to produce a payload sendable to whichever member ANSWERS, and
    failover picks that at the moment it happens. Bounding by the primary's own
    ceiling means a turn that fits the primary and not its fallback fails the
    moment the primary is rate-limited, which is precisely when a fallback is
    supposed to save the turn.

    Applied here rather than left to the caller, because there are two callers
    and the one that reads `[0]` would have been reading the primary's number.
    A one-member chain is the same rule with nothing to minimise.

    The ratio is re-checked against the result: the per-ref check has already
    run against each model's own ceiling, and this is the same question asked
    against the one that actually binds.
    """
    if not configs:
        return configs
    ceiling = min(cfg.prompt_char_budget for cfg in configs)
    _check_ratio_fits_the_guard(
        configs[0].compact_threshold, configs[0].context_window, ceiling, where,
    )
    return [
        cfg if cfg.prompt_char_budget == ceiling
        else dataclasses.replace(cfg, prompt_char_budget=ceiling)
        for cfg in configs
    ]


def build_chat_brain_adapter(cfg: ChatBrainConfig) -> ModelAdapter:
    """Construct the chat_brain ModelAdapter from a typed config."""
    return build_adapter(cfg.ref)


def build_chat_brain_chain(
    chain: list[ChatBrainConfig],
) -> list[tuple[ModelAdapter, AdapterOptions]]:
    """Build (adapter, options) pairs for every config whose adapter can be
    constructed. Entries whose provider lacks an API key in the environment
    are logged and skipped so a missing secondary doesn't crash startup.
    """
    built: list[tuple[ModelAdapter, AdapterOptions]] = []
    for idx, cfg in enumerate(chain):
        try:
            adapter = build_chat_brain_adapter(cfg)
        except RuntimeError as exc:
            logger.info(
                "chat_brain chain idx=%d (%s/%s) unavailable: %s",
                idx, cfg.provider, cfg.model, exc,
            )
            continue
        built.append((adapter, adapter_options_from_chat_brain(cfg)))
    return built


def resolve_provider_ref_runtime(
    ref: str,
) -> tuple[ChatBrainConfig, ModelAdapter, AdapterOptions] | None:
    """Build a single-entry adapter for a direct ``<tier>.<provider>.<model>`` ref.

    A card's ``model_role`` may name either a role or a catalog entry, and
    ``invoke_agent._resolve_sub_adapter`` used to fall a ref through to the
    parent adapter, so the pin was quietly ignored. This helper honours it.

    Returns ``None`` when the ref is malformed, the model is missing from
    ``providers.yaml``, or the adapter can't be constructed (no API key,
    etc.). Caller falls back to the parent adapter in that case — same
    discipline as :func:`resolve_role_runtime`.

    Reuses chat_brain's loop-cap knobs since the sub-session loop is the
    same one those caps govern. Role-side overrides do not apply (no
    role to read overrides from); the catalog's model fields are the
    source of truth.
    """
    ref = (ref or "").strip()
    if not ref:
        return None

    bundle = load_bundle()
    try:
        resolved_ref = bundle.resolve(ref)
    except ConfigError as exc:
        logger.info("resolve_provider_ref_runtime: ref %r unresolved: %s", ref, exc)
        return None

    # Use chat_brain's loop caps + a synthetic ``overrides`` payload so
    # _chat_brain_from_ref can build a ChatBrainConfig without inventing
    # any per-role knob. Required keys (temperature, max_output_tokens,
    # context_window, reasoning_effort, knowledge_cutoff, use_responses_api)
    # come from the catalog's model fields.
    cb_role = bundle.role("chat_brain")
    overrides = {
        "tool_iteration_cap": int(
            _require(dict(cb_role.overrides), "tool_iteration_cap", "roles.yaml::chat_brain")
        ),
        "consecutive_error_cap": int(
            _require(dict(cb_role.overrides), "consecutive_error_cap", "roles.yaml::chat_brain")
        ),
    }

    try:
        cfg = _chat_brain_from_ref(
            resolved_ref,
            overrides,
            f"agent.model_role={ref}",
            compaction=bundle.roles_raw.get("compaction") or {},
        )
    except (ConfigError, ValueError, KeyError) as exc:
        logger.info("resolve_provider_ref_runtime: cfg-build failed for %r: %s", ref, exc)
        return None

    try:
        adapter = build_adapter(resolved_ref)
    except RuntimeError as exc:
        logger.info("resolve_provider_ref_runtime: adapter unavailable for %r: %s", ref, exc)
        return None

    opts = adapter_options_from_chat_brain(cfg)
    # Stamp the role slot with the ref itself so audit logs surface the
    # exact pin. dataclasses.replace propagates think/keep_alive if set.
    opts = dataclasses.replace(opts, role=ref)
    return cfg, adapter, opts


def resolve_role_runtime(
    role_name: str,
) -> tuple[
    ChatBrainConfig,
    ModelAdapter,
    AdapterOptions,
    list[tuple[ModelAdapter, AdapterOptions]],
] | None:
    """Build the live adapter chain for any active role in ``roles.yaml``.

    Codex audit 2026-05-19 P1 #3: ``invoke_agent`` was passing the parent
    chat_brain adapter to every sub-session even when ``agent.model_role``
    pointed at ``agents_default`` (or another specialist role). This
    helper lets ``invoke_agent`` resolve the agent's declared role to a
    role-specific adapter+options pair at invocation time.

    Returns ``None`` when the role is inactive, missing, or no entry in
    the chain has a buildable adapter (e.g. missing API key for every
    fallback). Callers should fall back to the parent's adapter in that
    case so a misconfigured sub-agent doesn't blank the whole turn.

    Reuses the chat_brain machinery: every role inherits chat_brain's
    ``tool_iteration_cap`` / ``consecutive_error_cap`` since those knobs
    govern the sub-session loop the same way they govern the parent.
    Role-level ``*_override`` keys (temperature / max_output_tokens /
    reasoning_effort) still win.
    """
    role_name = (role_name or "").strip()
    if not role_name or role_name == "chat_brain":
        return None  # caller already has chat_brain wired

    bundle = load_bundle()
    try:
        role = bundle.role(role_name)
    except ConfigError:
        logger.info("resolve_role_runtime: role %r missing from roles.yaml", role_name)
        return None
    if role.mode != "active" or role.primary is None:
        logger.info(
            "resolve_role_runtime: role %r is %s — not constructing chain",
            role_name, role.mode,
        )
        return None

    # Inherit chat_brain's loop-cap knobs (the sub-session loop is the
    # same loop). Role-specific overrides for temperature etc. ride on
    # the role's own overrides via _chat_brain_from_ref. Hard-require
    # the caps from chat_brain rather than defaulting silently — they
    # are required keys on roles.yaml::chat_brain.
    cb_role = bundle.role("chat_brain")
    inherited = {
        "tool_iteration_cap": int(
            _require(dict(cb_role.overrides), "tool_iteration_cap", "roles.yaml::chat_brain")
        ),
        "consecutive_error_cap": int(
            _require(dict(cb_role.overrides), "consecutive_error_cap", "roles.yaml::chat_brain")
        ),
    }
    merged_overrides = {**inherited, **dict(role.overrides)}

    refs: list[ResolvedRef] = [role.primary, *role.fallbacks]
    chain: list[tuple[ChatBrainConfig, ModelAdapter, AdapterOptions]] = []
    for idx, ref in enumerate(refs):
        try:
            cfg = _chat_brain_from_ref(
                ref,
                merged_overrides,
                f"roles.yaml roles.{role_name}",
                compaction=bundle.roles_raw.get("compaction") or {},
            )
        except (ConfigError, ValueError, KeyError) as exc:
            logger.info(
                "resolve_role_runtime: role=%r idx=%d ref=%s unbuildable: %s",
                role_name, idx, ref.ref, exc,
            )
            continue
        reason = adapter_unavailable_reason(ref)
        if reason is not None:
            logger.info(
                "resolve_role_runtime: role=%r idx=%d (%s/%s) unavailable: %s",
                role_name, idx, cfg.provider, cfg.model, reason,
            )
            continue
        adapter: ModelAdapter = LazyAdapter(ref)
        opts = adapter_options_from_chat_brain(cfg)
        # Stamp the actual role name into options.role so audit logs
        # show that the sub-session ran under (e.g.) ``agents_default``
        # rather than mislabelled as ``chat_brain``. Use dataclasses.replace
        # so future fields on AdapterOptions (think/keep_alive/etc.) carry
        # through without an explicit listing here.
        opts = dataclasses.replace(opts, role=role_name)
        chain.append((cfg, adapter, opts))

    if not chain:
        return None
    primary_cfg, primary_adapter, primary_options = chain[0]
    return (
        primary_cfg,
        primary_adapter,
        primary_options,
        [(adapter, options) for _cfg, adapter, options in chain],
    )


class ChatBrainUnavailable(RuntimeError):
    """Raised by `resolve_chat_brain_runtime()` when every chat_brain
    candidate failed to build.

    `str(self)` (== `args[0]`) is the full per-candidate technical
    breakdown — model ids, `providers.yaml` flag names, etc. — meant for
    the log only. `summary` is the short, plain-language form a non-
    developer can act on: what happened, why (no key vs disabled — the
    one distinction that matters), and the one thing to do. That's what
    `NullChatAdapter` raises into the chat transcript, never the detail.
    """

    def __init__(self, detail: str, summary: str) -> None:
        super().__init__(detail)
        self.summary = summary


def _summarize_chat_brain_failure(failures: list[str]) -> str:
    """Plain-language, user-facing summary of why chat isn't available.

    `failures` holds each candidate's raw `build_adapter()` message. Every
    branch in `build_adapter` raises one of exactly two shapes for a
    chat_brain candidate: "<KEY> missing from .env" (no key) or "...
    disabled in providers.yaml (...)" (turned off) — classified by
    substring so this stays a plain reader of `boot.py`'s own message
    text rather than a second source of truth for the reason.
    """
    no_key = any("missing from .env" in f for f in failures)
    disabled = any("disabled in providers.yaml" in f for f in failures)
    env_path = home_dir() / ".env"
    if no_key and not disabled:
        return (
            f"No chat provider can answer yet — no API key is set for any "
            f"configured provider. Add one to {env_path}, then restart TESSERACT."
        )
    if disabled and not no_key:
        # No restart in this branch: the `enabled` switches are hot-reloaded
        # by the config watcher, so turning one back on takes effect on the
        # next turn. Only the .env branches below need a restart.
        return (
            "No chat provider can answer yet — every configured provider is "
            "switched off in providers.yaml. Switch one back on in "
            "Settings -> Capabilities."
        )
    return (
        f"No chat provider can answer yet — none is available (no API key set, "
        f"or a provider switched off in providers.yaml). Add a key to "
        f"{env_path}, then restart TESSERACT."
    )


#: What `resolve_chat_brain_runtime` returns: primary cfg, primary adapter,
#: primary options, and the full built chain. Named because it is now passed
#: BETWEEN builders rather than re-resolved by each of them — resolving costs
#: seconds (one SDK client per chain entry), and the daemon would otherwise
#: pay it twice per boot, once in `_rebuild_adapter` and again inside
#: `build_tool_registry`.
ChatBrainRuntime = tuple[
    ChatBrainConfig,
    ModelAdapter,
    AdapterOptions,
    list[tuple[ModelAdapter, AdapterOptions]],
]


def resolve_chat_brain_runtime() -> ChatBrainRuntime:
    """Return the first available chat_brain entry plus the full live chain.

    `load_chat_brain_chain()` validates config shape; adapter construction is
    a separate concern. The runtime should use the first entry whose adapter
    can actually be built, not blindly pin itself to `resolution[0]`.

    Nothing in the chain is a hard requirement (no API key/provider is
    mandatory) — if every candidate fails, raises `ChatBrainUnavailable`
    carrying both the full technical breakdown (for the log) and a short
    plain-language `summary` (for the caller — `_build_chat_infra` — to hand
    to a degraded placeholder adapter instead of leaving chat silently dead).

    Nothing is constructed here. Each candidate is asked whether it WOULD
    build — a dict lookup and a PATH probe — and the ones that would become
    lazy entries that construct their client the first time a turn actually
    reaches them. There is nothing left to overlap, so there is no thread
    pool here.
    """
    chain_cfgs = load_chat_brain_chain()
    built_chain: list[tuple[ChatBrainConfig, ModelAdapter, AdapterOptions]] = []
    failures: list[str] = []
    for idx, cfg in enumerate(chain_cfgs):
        reason = adapter_unavailable_reason(cfg.ref)
        if reason is not None:
            logger.info(
                "chat_brain chain idx=%d (%s/%s) unavailable: %s",
                idx, cfg.provider, cfg.model, reason,
            )
            failures.append(f"{cfg.provider} ({cfg.model}): {reason}")
            continue
        built_chain.append(
            (cfg, LazyAdapter(cfg.ref), adapter_options_from_chat_brain(cfg))
        )
    if not built_chain:
        detail = "no chat provider available — " + "; ".join(failures)
        raise ChatBrainUnavailable(detail, _summarize_chat_brain_failure(failures))
    primary_cfg, primary_adapter, primary_options = built_chain[0]
    return (
        primary_cfg,
        primary_adapter,
        primary_options,
        [(adapter, options) for _cfg, adapter, options in built_chain],
    )


def adapter_options_from_chat_brain(cfg: ChatBrainConfig) -> AdapterOptions:
    """Build AdapterOptions from a typed ChatBrainConfig — no fallbacks.

    Per-provider chain-policy overrides (`transient_retries` /
    `transient_backoff_ms` / `cooldown_max_failures` / `cooldown_seconds`
    on the connection block) ride along in ``extra`` so
    ``FallbackAdapter`` can read them per-entry without needing the
    connection plumbed through. Keys present only when the operator
    set them; absent → inherit ``providers.yaml::chain.*`` global.
    """
    extra: dict[str, Any] = {}
    conn = cfg.ref.connection
    if conn.transient_retries is not None:
        extra["chain_transient_retries"] = conn.transient_retries
    if conn.transient_backoff_ms is not None:
        extra["chain_transient_backoff_ms"] = conn.transient_backoff_ms
    if conn.cooldown_max_failures is not None:
        extra["chain_cooldown_max_failures"] = conn.cooldown_max_failures
    if conn.cooldown_seconds is not None:
        extra["chain_cooldown_seconds"] = conn.cooldown_seconds
    return AdapterOptions(
        model=cfg.model,
        provider=cfg.provider,
        role="chat_brain",
        tier=cfg.tier,
        temperature=cfg.temperature,
        max_output_tokens=cfg.max_output_tokens,
        context_window=cfg.context_window,
        reasoning_effort=cfg.reasoning_effort,
        knowledge_cutoff=cfg.knowledge_cutoff,
        use_responses_api=cfg.use_responses_api,
        prompt_cache_explicit=cfg.prompt_cache_explicit,
        stream=cfg.stream,
        extra=extra,
    )


@dataclass(frozen=True)
class VaultConfig:
    """Typed view of `tesseract/config/vault.yaml`.

    Every field is required. `load_vault_config()` raises if any key is
    missing — no silent fallbacks.
    """
    max_extract_chars: int
    scale_split_threshold: int
    stale_grace_days: int
    contradiction_pair_limit: int
    max_seed_slugs: int
    max_expanded_slugs: int
    synthesis_max_pages: int
    synthesis_page_chars: int
    synthesis_char_budget: int
    search_rrf_k: int
    search_default_top_k: int


def load_vault_config() -> VaultConfig:
    """Read vault.yaml; return a typed VaultConfig or raise on missing keys."""
    raw = yaml.safe_load(VAULT_YAML.read_text(encoding="utf-8"))
    ingest = _require(raw, "ingest", "vault.yaml")
    lint = _require(raw, "lint", "vault.yaml")
    query = _require(raw, "query", "vault.yaml")
    search = _require(raw, "search", "vault.yaml")
    # The three synthesis budgets fail SILENTLY when non-positive: the walk
    # breaks on its first iteration and vault_query answers "No readable wiki
    # pages found" over a vault full of pages. A wrong answer that looks like
    # an empty vault is worse than a boot error, so they are range-checked.
    for key in ("synthesis_max_pages", "synthesis_page_chars", "synthesis_char_budget"):
        if int(_require(query, key, "vault.yaml query")) <= 0:
            raise RuntimeError(f"vault.yaml query.{key} must be positive")
    return VaultConfig(
        max_extract_chars=int(_require(ingest, "max_extract_chars", "vault.yaml ingest")),
        scale_split_threshold=int(_require(lint, "scale_split_threshold", "vault.yaml lint")),
        stale_grace_days=int(_require(lint, "stale_grace_days", "vault.yaml lint")),
        contradiction_pair_limit=int(_require(lint, "contradiction_pair_limit", "vault.yaml lint")),
        max_seed_slugs=int(_require(query, "max_seed_slugs", "vault.yaml query")),
        max_expanded_slugs=int(_require(query, "max_expanded_slugs", "vault.yaml query")),
        synthesis_max_pages=int(_require(query, "synthesis_max_pages", "vault.yaml query")),
        synthesis_page_chars=int(_require(query, "synthesis_page_chars", "vault.yaml query")),
        synthesis_char_budget=int(_require(query, "synthesis_char_budget", "vault.yaml query")),
        search_rrf_k=int(_require(search, "rrf_k", "vault.yaml search")),
        search_default_top_k=int(_require(search, "default_top_k", "vault.yaml search")),
    )


def load_embeddings_cfg() -> dict:
    """Read embeddings settings — derived from the providers catalog entry
    that ``roles.yaml::embeddings.primary`` points at.

    Returns a dict in the legacy shape expected by EmbeddingIndex / Mirror's
    ollama probe: ``provider``, ``base_url``, ``model``, ``dimensions``,
    ``timeout_seconds``, ``max_retries``, ``host``, ``auto_start_ollama``.
    """
    bundle = load_bundle()
    ref = bundle.embeddings
    conn = ref.connection
    if not conn.tier_enabled or not conn.enabled:
        # Embeddings tier/provider switched off — return empty so
        # `build_memory_bundle` skips the EmbeddingIndex build cleanly
        # (BM25-only retrieval, the same degraded path it takes when
        # Ollama is unreachable).
        logger.info(
            "embeddings: ref=%s disabled (tier_enabled=%s, enabled=%s) — skipping",
            ref.ref, conn.tier_enabled, conn.enabled,
        )
        return {}
    cfg: dict = {
        "provider": conn.name,
        # Loopback by address for the same reason `providers.yaml` uses one:
        # `localhost` costs ~2s per connection failing over from ::1.
        "base_url": conn.base_url or "http://127.0.0.1:11434",
        "model": ref.model.model,
        "dimensions": int(ref.model.fields.get("dimensions", 768)),
        "timeout_seconds": int(ref.model.fields.get("timeout_seconds", conn.timeout_seconds)),
        "max_retries": int(ref.model.fields.get("max_retries", conn.max_retries)),
        "host": str(conn.extra.get("host", "this_pc")),
        "auto_start_ollama": bool(conn.extra.get("auto_start", False)),
    }
    return cfg


def ollama_slots() -> list[tuple[str, ResolvedRef]]:
    """Every config slot that points at a model served by local Ollama,
    paired with a human label for the slot that reached it first.

    Ordered as config reads — embeddings first, because that is the slot
    whose absence degrades in silence — then the reranker, then each active
    role's primary and fallbacks, then the voice lanes. Deduped by ref, so
    a ref named twice is labelled by its earliest mention.

    Fallbacks are included on purpose: a fallback nobody pulled is a
    fallback that fails at the one moment it is reached.

    Disabled tier or provider yields an empty list, matching the way every
    other consumer treats a switched-off provider.
    """
    bundle = load_bundle()
    out: list[tuple[str, ResolvedRef]] = []
    seen: set[str] = set()

    def _add(slot: str, ref: ResolvedRef | None) -> None:
        if ref is None:
            return
        conn = ref.connection
        if conn.tier != "local" or conn.name != "ollama":
            return
        if not conn.tier_enabled or not conn.enabled:
            return
        if ref.ref in seen:
            return
        seen.add(ref.ref)
        out.append((slot, ref))

    _add("embeddings", bundle.embeddings)
    _add("reranker", bundle.reranker)
    for role in bundle.roles.values():
        if role.mode != "active":
            continue
        _add(f"role {role.name}", role.primary)
        for fallback in role.fallbacks:
            _add(f"role {role.name} (fallback)", fallback)
    if bundle.voice is not None:
        for lane_name, lane in (("stt", bundle.voice.stt), ("tts", bundle.voice.tts)):
            if lane is None or lane.mode != "active":
                continue
            for entry in lane.chain():
                _add(f"voice {lane_name}", entry.ref)
    return out


def ollama_refs() -> list[ResolvedRef]:
    """The refs from :func:`ollama_slots`, without the slot labels."""
    return [ref for _slot, ref in ollama_slots()]


def load_reranker_cfg(bundle: ConfigBundle | None = None) -> dict:
    """Read reranker settings — derived from the providers catalog entry
    that ``roles.yaml::reranker.primary`` points at.

    Returns ``{}`` when the role is absent or its tier/provider is disabled
    (retrieval keeps pure RRF order). Otherwise: ``model_path``,
    ``tokenizer_path`` (under ``<TESSERACT_HOME>/models/reranker/``),
    ``max_seq_len``, ``candidate_cap``, ``download`` (fetch-script URLs).

    ``bundle`` lets a caller that already holds a freshly-loaded
    :class:`ConfigBundle` (e.g. `rebuild_adapters`, which also needs
    `bundle.reranker` for its own reporting) skip a second on-disk read;
    defaults to a fresh :func:`load_bundle` call otherwise.
    """
    bundle = bundle if bundle is not None else load_bundle()
    ref = bundle.reranker
    if ref is None:
        return {}
    conn = ref.connection
    if not conn.tier_enabled or not conn.enabled:
        logger.info(
            "reranker: ref=%s disabled (tier_enabled=%s, enabled=%s) — skipping",
            ref.ref, conn.tier_enabled, conn.enabled,
        )
        return {}
    return _reranker_cfg_from_ref(ref)


def _reranker_cfg_from_ref(ref) -> dict:
    """Catalog entry → reranker settings. Missing keys raise loudly —
    config is the single source of truth, no silent Python defaults."""
    from tesseract.config.loader import ConfigError

    from tesseract.lib.pinned_fetch import _unsafe_filename_reason

    fields = ref.model.fields
    where = f"providers.yaml entry for {ref.ref}"
    for key in ("tokenizer", "max_seq_len", "candidate_cap"):
        if key not in fields:
            raise ConfigError(f"{where} missing required key '{key}'")

    # `model` and `tokenizer` are FILENAMES inside the reranker directory, and
    # nothing checked that until now. They are joined onto `models_dir` to
    # produce paths that are read from AND fetched into — `capability/models.py
    # ::reranker_lane` takes `model_path.parent` as its download destination —
    # so a catalog entry naming `../..` or an absolute path moved both outside
    # the state root entirely.
    #
    # `pinned_fetch` already guards the `files:` keys of a download block with
    # exactly this rule; the model NAME sat one level above it and inherited
    # none of that. Reached for rather than restated so one definition of
    # "this is a filename" governs both.
    for key in ("model", "tokenizer"):
        value = str(getattr(ref.model, key, None) if key == "model" else fields[key])
        unsafe = _unsafe_filename_reason(value)
        if unsafe is not None:
            raise ConfigError(
                f"{where}: '{key}' is {value!r}, which {unsafe} — it names a file "
                f"inside the reranker directory, never a path to anywhere else"
            )
    # `home_dir()`, not the frozen `TESSERACT_HOME` constant. The constant is
    # bound once, at first import, so a `TESSERACT_HOME` set afterwards is
    # ignored — and this value becomes a directory something then READS from
    # and FETCHES into. Under pytest, where the env var is set per test but
    # this module was imported at collection, that meant the reranker path
    # resolved to the operator's real install however carefully a test
    # isolated itself.
    models_dir = home_dir() / "models" / "reranker"
    return {
        "model_path": models_dir / str(ref.model.model),
        "tokenizer_path": models_dir / str(fields["tokenizer"]),
        "max_seq_len": int(fields["max_seq_len"]),
        "candidate_cap": int(fields["candidate_cap"]),
        # Only the fetch script consumes this; it reports absence itself.
        "download": dict(fields.get("download") or {}),
    }


# ── Adapter + observer builders ──────────────────────────

#: The env var each key-gated adapter kind reads when the connection block
#: names none of its own. Also the set of adapter kinds gated on a key at all —
#: `cli` and `ollama` are gated on a binary instead.
_DEFAULT_API_KEY_ENV = {
    "gemini": "GOOGLE_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


def _api_key_env(conn) -> str:
    return conn.api_key_env or _DEFAULT_API_KEY_ENV[conn.adapter]


def adapter_unavailable_reason(ref: ResolvedRef) -> str | None:
    """Why :func:`build_adapter` would refuse this ref, or None if it would build.

    Every gate ``build_adapter`` applies lives here and only here, so asking
    whether a provider is usable costs a dict lookup and a PATH probe — never
    an SDK client. Constructing one to answer the question is what made
    `/api/capabilities` a 5-27s route: the client the probe discards drags in
    ~1,300 lazily-imported modules and reads the system CA bundle off disk to
    build an SSL context, once per candidate, on every poll.

    The message text is contractual: `_summarize_chat_brain_failure` and
    `mirror/server/routes/_capability_report.py` both classify a failure by
    reading it ("missing from .env" vs "disabled in providers.yaml").
    """
    conn = ref.connection
    if not conn.tier_enabled:
        return f"tier '{conn.tier}' is disabled in providers.yaml ({conn.tier}.enabled=false)"
    if not conn.enabled:
        return (
            f"provider '{conn.tier}.{conn.name}' is disabled in providers.yaml "
            f"({conn.tier}.{conn.name}.enabled=false)"
        )
    adapter = conn.adapter
    if adapter in _DEFAULT_API_KEY_ENV:
        key_env = _api_key_env(conn)
        if not os.environ.get(key_env):
            return f"{key_env} missing from .env"
        return None
    if adapter == "cli":
        # Subprocess-backed adapter — uses the operator's CLI subscription
        # auth (no API key). `command` selects the binary; today only
        # `codex` is fully wired (parses `--json` event stream). `claude`
        # falls back to plain-text mode.
        if not conn.command:
            return (
                f"cli provider '{conn.tier}.{conn.name}' missing 'command' in providers.yaml"
            )
        # Mirror the API-key gate: if the binary isn't on PATH the chain
        # should skip this entry instead of returning a dead adapter that
        # fails at first stream() call. `.cmd` shim covers Windows. Same
        # pair check lives in `CLIAdapter.check_available` for runtime
        # health probes — keep both in sync if either lookup rule changes.
        if shutil.which(conn.command) is None and shutil.which(f"{conn.command}.cmd") is None:
            return (
                f"cli binary '{conn.command}' not on PATH "
                f"(provider '{conn.tier}.{conn.name}')"
            )
        return None
    if adapter == "ollama":
        if not conn.base_url:
            return (
                f"local provider '{conn.tier}.{conn.name}' missing 'base_url' in providers.yaml"
            )
        # Mirror the cli branch's binary gate: absent Ollama makes the chain
        # skip this entry rather than hold an adapter that dies at first
        # stream(). `ollama_exe`, not `shutil.which` — an install from the
        # Settings panel completes inside this process and never updates its
        # PATH. Imported here, not at module scope: `ollama_boot` imports
        # `ollama_up` from this module.
        from tesseract.memory.ollama_boot import ollama_exe
        if ollama_exe() is None:
            return (
                f"ollama is not installed — '{ref.ref}' unavailable "
                f"(install it from Settings → Local models)"
            )
        return None
    return f"no adapter wired for adapter='{adapter}' (ref={ref.ref})"


def adapter_class_for(ref: ResolvedRef) -> type[ModelAdapter]:
    """Which PROVIDER class `build_adapter` would construct — without
    constructing it. Not the screening wrapper it returns: the caller wants
    the class carrying the token-estimate staticmethod.

    Exists for one caller: the chain's context-window guard asks every entry
    it considers for a token estimate, and the estimate is a `staticmethod` on
    the provider's class precisely so the guard can ask a fallback that has
    never been built.

    It is a second reading of `_build_provider_adapter`'s dispatch, which is the kind of
    duplication this codebase refuses — so it is held by a test
    (`test_lazy_adapter.py`) asserting every adapter name the shipped catalog
    uses appears here. A branch added below and forgotten here fails that
    test rather than silently estimating with the wrong heuristic.
    """
    from tesseract.kernel.adapters.cli import CLIAdapter

    adapter = ref.connection.adapter
    classes: dict[str, type[ModelAdapter]] = {
        "gemini": GeminiAdapter,
        "openai": OpenAIAdapter,
        "anthropic": AnthropicAdapter,
        "cli": CLIAdapter,
        # Ollama serves an OpenAI-compatible surface, so the same class drives
        # it — the same reason `build_adapter`'s ollama branch returns one.
        "ollama": OpenAIAdapter,
    }
    if adapter not in classes:
        raise RuntimeError(f"no adapter wired for adapter='{adapter}' (ref={ref.ref})")
    return classes[adapter]


def build_adapter(ref: ResolvedRef) -> ModelAdapter:
    """Construct a ModelAdapter from a resolved provider/model reference.

    Dispatch key is ``ref.connection.adapter`` (declared in providers.yaml).
    Whether the ref is usable at all is :func:`adapter_unavailable_reason`'s
    answer, asked once up front — so a disabled tier or provider, a missing
    API key or an absent binary raises before any client is built, and the
    fallback chain skips the entry.

    **Everything it returns is wrapped in a `ScreenedAdapter`.** This is the
    one place a provider adapter is constructed in this runtime, so it is the
    one place that can promise a stored credential is checked for before a
    payload leaves the machine. Someone adding a provider writes a class and a
    branch below; the wrap happens on the way out and their code never takes
    part in it, which is the difference between a mechanism and a convention
    an install we do not ship could quietly drop.
    """
    return ScreenedAdapter(_build_provider_adapter(ref))


def _build_provider_adapter(ref: ResolvedRef) -> ModelAdapter:
    reason = adapter_unavailable_reason(ref)
    if reason is not None:
        raise RuntimeError(reason)
    conn = ref.connection
    adapter = conn.adapter
    if adapter == "gemini":
        return GeminiAdapter(
            api_key=os.environ.get(_api_key_env(conn), ""),
            timeout=conn.timeout_seconds,
            max_retries=conn.max_retries,
        )
    if adapter == "openai":
        return OpenAIAdapter(
            api_key=os.environ.get(_api_key_env(conn), ""),
            base_url=conn.base_url or "https://api.openai.com/v1",
            timeout=conn.timeout_seconds,
            max_retries=conn.max_retries,
            supports_prompt_cache_key=conn.supports_prompt_cache_key,
            supports_stream_usage=conn.supports_stream_usage,
            cache_routing_header=conn.cache_routing_header,
        )
    if adapter == "anthropic":
        return AnthropicAdapter(
            api_key=os.environ.get(_api_key_env(conn), ""),
            timeout=conn.timeout_seconds,
            max_retries=conn.max_retries,
            base_url=conn.base_url,
            health_check_model=ref.model.model,
        )
    if adapter == "cli":
        from tesseract.kernel.adapters.cli import CLIAdapter
        return CLIAdapter(
            command=conn.command,
            model_id=ref.model.model,
            timeout=conn.timeout_seconds,
            stream_json=conn.stream_json_capable,
        )
    if adapter == "ollama":
        # Ollama serves an OpenAI-compatible surface at `<base_url>/v1`, so
        # the existing adapter drives it — streaming, tool-calls, usage
        # accounting and the chain's error taxonomy all already work. The
        # bare `base_url` stays what it is because the embedding path hits
        # `/api/embeddings` on it; only this branch appends `/v1`.
        return OpenAIAdapter(
            # The daemon ignores auth entirely; the SDK requires a non-empty
            # string. No `api_key_env` exists on a local-tier provider.
            api_key="ollama",
            base_url=f"{conn.base_url.rstrip('/')}/v1",
            timeout=conn.timeout_seconds,
            max_retries=conn.max_retries,
            # Not a knob: the compat surface has no `prompt_cache_key` param.
            supports_prompt_cache_key=False,
            # This one IS a knob — older Ollama builds reject `stream_options`,
            # and `supports_stream_usage: false` in providers.yaml is the fix.
            supports_stream_usage=conn.supports_stream_usage,
            cache_routing_header=None,
        )
    raise RuntimeError(f"no adapter wired for adapter='{adapter}' (ref={ref.ref})")


def build_observer(cost_ledger: CostLedger | None = None) -> Observer | None:
    """Walk the `observer_agent` role's primary + fallbacks, return the first
    Observer we can actually build. None if the role is missing or every entry
    lacks credentials — observer is optional infrastructure.
    """
    bundle = load_bundle()
    if "observer_agent" not in bundle.roles:
        return None
    role = bundle.role("observer_agent")
    if role.mode != "active" or role.primary is None:
        logger.info("observer: role is %s — not constructing an observer", role.mode)
        return None

    try:
        agent_def = load_agent("observer")
    except FileNotFoundError:
        logger.info("observer: tesseract/agents/observer.md missing, skipping")
        return None

    overrides = dict(role.overrides)
    for ref in (role.primary, *role.fallbacks):
        try:
            adapter = build_adapter(ref)
        except RuntimeError as exc:
            logger.info("observer: cannot build %s — %s", ref.ref, exc)
            continue
        _where = f"providers.yaml entry for {ref.ref}"
        try:
            entry = {
                "provider": ref.connection.name,
                "tier": ref.connection.tier,
                "model": ref.model.model,
                "use_responses_api": bool(ref.model.fields.get("use_responses_api", False)),
                "prompt_cache_explicit": bool(
                    ref.model.fields.get("prompt_cache_explicit", False)
                ),
                "stream": bool(ref.model.fields.get("stream", True)),
                "context_window": int(_require(ref.model.fields, "context_window", _where)),
                "max_output_tokens": int(overrides.get(
                    "max_output_tokens_override",
                    _require(ref.model.fields, "max_output_tokens", _where),
                )),
                "temperature": float(_require(ref.model.fields, "temperature", _where)),
                "reasoning_effort": str(overrides.get(
                    "reasoning_effort_override",
                    ref.model.fields.get("reasoning_effort", "low"),
                )),
            }
        except ConfigError as exc:
            logger.warning("observer: skipping %s — %s", ref.ref, exc)
            continue
        provider_cfg = _provider_cfg_dict(ref)
        try:
            observer = build_observer_from_config(
                adapter, entry, provider_cfg, agent_def, cost_ledger=cost_ledger,
            )
        except Exception:
            logger.exception("observer: build failed for ref=%s", ref.ref)
            continue
        logger.info("observer: built from ref=%s", ref.ref)
        return observer
    return None


def build_cost_ledger() -> CostLedger:
    """Construct the shared cost ledger from the providers/roles bundle.

    The same instance is threaded into `ChatSession.cost_ledger` and
    `Observer._cost_ledger` so both roles debit one daily total.
    """
    return CostLedger.from_bundle(load_bundle())


# ── Memory + tool registry ───────────────────────────────

# One client for every liveness probe, built on first use and never replaced.
#
# `httpx.get` builds a throwaway `Client` per call, and constructing one costs
# ~1.5s on Windows — an SSL context over the certifi bundle and the system
# trust store, paid whether or not the request is https. Settings polls this
# probe every 30s and the panel blocks on it, so that construction was most of
# a 2.9s `/api/system/ollama`. Reusing the client also keeps the loopback
# connection in the pool instead of opening a socket per probe.
#
# `httpx.Client` is thread-safe, which is what the `asyncio.to_thread` callers
# of `ollama_up` need. The lock guards construction only — two threads racing
# here would otherwise each pay the 1.5s and one client would leak.
_PROBE_CLIENT: httpx.Client | None = None
_PROBE_CLIENT_LOCK = threading.Lock()


def _probe_client() -> httpx.Client:
    global _PROBE_CLIENT
    if _PROBE_CLIENT is None:
        with _PROBE_CLIENT_LOCK:
            if _PROBE_CLIENT is None:
                _PROBE_CLIENT = http_client.client()
    return _PROBE_CLIENT


def ollama_up(base_url: str, timeout: float = 2.0) -> bool:
    try:
        r = _probe_client().get(f"{base_url.rstrip('/')}/api/tags", timeout=timeout)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


@dataclass
class MemoryBundle:
    store: MemoryStore
    index: MemoryIndex
    fts_index: FTSIndex
    librarian: Librarian
    embeddings: EmbeddingIndex | None = None
    pipeline: RetrievalPipeline | None = None
    dreaming: DreamingEngine | None = None
    recall_log_path: Path | None = None
    reranker: object | None = None


def build_memory_bundle(
    adapter: ModelAdapter | None = None,
    adapter_options: AdapterOptions | None = None,
) -> MemoryBundle:
    """Store + index + retrieval pipeline are always live (filesystem only).
    Embeddings come online only when the configured Ollama endpoint is
    reachable; the pipeline degrades cleanly to BM25-only retrieval when
    embeddings are absent.

    `adapter` / `adapter_options` flow into `Librarian` so its M2 prefix-
    classifier fallback has something to call. Pass `None` in environments
    without an LLM — unclassifiable sections are then skipped, not written.

    Never returns None — `memory_search` is always available; vector
    search is the only branch gated on embeddings.
    """
    store_dir = TESSERACT_HOME / "memory-store"
    derived_dir = store_dir / "derived"
    derived_dir.mkdir(parents=True, exist_ok=True)

    # Seed the Obsidian color-group config so the operator can
    # open `memory-store/` (and `vault/`) directly in Obsidian and see
    # the unified palette on first launch. Idempotent — operator edits
    # to `.obsidian/graph.json` survive future boots.
    try:
        from tesseract.memory.obsidian_config import (
            VAULT_COLOR_GROUPS,
            ensure_obsidian_config,
        )

        ensure_obsidian_config(store_dir)
        ensure_obsidian_config(
            TESSERACT_HOME / "vault", color_groups=VAULT_COLOR_GROUPS
        )
    except Exception:
        logger.exception("obsidian_config: seeding failed (non-fatal)")

    store = MemoryStore(store_dir=store_dir)
    index = MemoryIndex(store_dir=store_dir)
    fts_index = FTSIndex(db_path=derived_dir / "fts.db")
    recall_log_path = derived_dir / "recall.jsonl"

    embed_cfg = load_embeddings_cfg()
    embeddings: EmbeddingIndex | None = None

    # Built whenever config points at Ollama, WITHOUT probing it first. The
    # probe used to decide this for the life of the process, so a daemon that
    # became reachable one second later left vector search off until the next
    # restart, and boot order decided whether retrieval worked at all: this
    # substrate and the one that starts Ollama are peers in the same parallel
    # layer, and each won on different days. Nothing is risked by building
    # eagerly, because the index already fails at use rather than at
    # construction: `embed_text` returns None on timeout, refusal or
    # connection error, and `search` returns [] when it does, which is the
    # keyword-only path. So an unreachable daemon degrades exactly as before,
    # and a late one now recovers on its own.
    if embed_cfg and embed_cfg.get("provider") == "ollama":
        embeddings = EmbeddingIndex(
            derived_dir=derived_dir,
            provider=embed_cfg["provider"],
            base_url=embed_cfg["base_url"],
            model=embed_cfg["model"],
            dimensions=embed_cfg["dimensions"],
            timeout_seconds=embed_cfg["timeout_seconds"],
            max_retries=embed_cfg["max_retries"],
        )

    # Audit M2 fix (2026-04-29): the pipeline is always constructed —
    # embeddings + fts_index + recall_log_path are all wired in, and
    # any of them being None is a first-class degraded mode rather than
    # a "memory_search disappears" cliff. Before: pipeline only built
    # when Ollama was up, leaving the BM25 path unreachable from the
    # tool surface even though FTSIndex had been live since 2026-04-21.
    # Wire the work-history index so per-turn memory_search can
    # surface session + workshop chunks alongside promoted memory when a
    # caller asks for it (include_work_history=True). The index is the
    # same DB the recall_history tool writes to.
    try:
        from tesseract.memory.work_index import WorkIndex as _WorkIndex

        work_index_db = TESSERACT_HOME / "work_index.sqlite"
        work_index = _WorkIndex(work_index_db)
    except Exception:  # noqa: BLE001
        logger.exception("memory_bundle: work_index init failed; merge disabled")
        work_index = None

    # Cross-encoder reranker — role-wired, best-effort. Constructed even
    # when the model files are absent; it degrades to a no-op and logs the
    # fetch hint once.
    reranker = None
    reranker_cfg = load_reranker_cfg()
    if reranker_cfg:
        from tesseract.memory.reranker import CrossEncoderReranker

        reranker = CrossEncoderReranker(
            model_path=reranker_cfg["model_path"],
            tokenizer_path=reranker_cfg["tokenizer_path"],
            max_seq_len=reranker_cfg["max_seq_len"],
            candidate_cap=reranker_cfg["candidate_cap"],
        )

    pipeline = RetrievalPipeline(
        store=store,
        index=index,
        embeddings=embeddings,
        fts_index=fts_index,
        recall_log_path=recall_log_path,
        work_index=work_index,
        reranker=reranker,
    )

    librarian = Librarian(
        store=store,
        embeddings=embeddings,
        adapter=adapter,
        adapter_options=adapter_options,
    )

    # DreamingEngine consumes the same recall log written by
    # RetrievalPipeline._log_recalls. Always-instantiable (it only needs
    # store + index + a path); the scheduler `dream_cycle` handler in
    # `tesseract.scheduler.tasks.dream_cycle` calls `run_cycle()` daily.
    dreaming = DreamingEngine(
        store=store,
        index=index,
        recall_log_path=recall_log_path,
    )

    return MemoryBundle(
        store=store,
        index=index,
        fts_index=fts_index,
        librarian=librarian,
        embeddings=embeddings,
        pipeline=pipeline,
        dreaming=dreaming,
        recall_log_path=recall_log_path,
        reranker=reranker,
    )


def register_memory_write_tools(registry: ToolRegistry, bundle: MemoryBundle) -> None:
    """Write-side memory tools — always registerable (store is filesystem).

    fts_index (SQLite FTS5) is always live; auto_linker (cosine-neighbor
    discovery) rides on embeddings and is therefore optional. Both were
    dead-param'd at the save site prior to the 2026-04-21 audit; now wired.
    """
    from tesseract.memory.auto_linker import AutoLinker

    auto_linker = (
        AutoLinker(store=bundle.store, embeddings=bundle.embeddings)
        if bundle.embeddings is not None
        else None
    )

    registry.register(MemorySaveTool(
        store=bundle.store,
        index=bundle.index,
        embeddings=bundle.embeddings,
        fts_index=bundle.fts_index,
        auto_linker=auto_linker,
    ))
    registry.register(MemoryUpdateTool(
        store=bundle.store,
        index=bundle.index,
        embeddings=bundle.embeddings,
        fts_index=bundle.fts_index,
    ))
    registry.register(MemoryForgetTool(
        store=bundle.store,
        index=bundle.index,
        embeddings=bundle.embeddings,
    ))
    registry.register(MemoryPromoteTool(
        store=bundle.store,
        index=bundle.index,
        soul_growth_tool=SoulGrowthProposeTool(repo_root=ROOT),
    ))


def register_memory_search(registry: ToolRegistry, bundle: MemoryBundle) -> None:
    """memory_search needs the retrieval pipeline (embeddings-dependent)."""
    if bundle.pipeline is not None and "memory_search" not in registry.tools:
        registry.register(MemorySearchTool(pipeline=bundle.pipeline))


def register_recall_history(registry: ToolRegistry) -> None:
    """recall_history — read-only retrieval over session transcripts +
    workshop artifacts. No embeddings dependency; always registers."""
    if "recall_history" in registry.tools:
        return
    from tesseract.kernel.tools.recall_history import RecallHistoryTool
    from tesseract.memory.work_index import WorkIndex

    # TESSERACT_HOME is resolved at import time in `tesseract.paths`
    # (env-var-or-default). Use that — the env-at-call-time pattern is
    # for hooks that fire from test fixtures with `monkeypatch.setenv`;
    # tool-registry registration happens once at boot, where the import-
    # time constant is canonical.
    db_path = TESSERACT_HOME / "work_index.sqlite"
    try:
        index = WorkIndex(db_path)
    except Exception:  # noqa: BLE001
        logger.exception("recall_history: WorkIndex init failed at %s", db_path)
        return
    registry.register(RecallHistoryTool(index))


def ensure_memory_tools(
    registry: ToolRegistry,
    adapter: ModelAdapter | None = None,
    adapter_options: AdapterOptions | None = None,
) -> MemoryBundle:
    """Build/rebuild the bundle, registering all memory tools.

    Write tools (save/update/forget) and `memory_search` both always land
    — `memory_search` runs BM25-only when Ollama is offline. Idempotent —
    safe to call from /refresh after ollama starts mid-session. `adapter`
    flows to the librarian for the missing-prefix classifier path.
    """
    bundle = build_memory_bundle(adapter=adapter, adapter_options=adapter_options)
    if "memory_save" not in registry.tools:
        register_memory_write_tools(registry, bundle)
    register_memory_search(registry, bundle)
    register_recall_history(registry)
    return bundle


def load_voice_config() -> dict:
    """Materialize ``voice:`` from the typed bundle into a dict the
    runtime consumer (``mirror/server/app.py::_build_voice_runtime``)
    can walk without re-importing the loader.

    Shape returned (2026-05-01 — primary+fallbacks):

        {
          "stt": {
            "mode": "active",
            "chain": [
              {"ref", "adapter", "model", "api_key_env"?, "daily_budget_usd",
               "timeout_seconds", ...catalog fields..., ...settings overrides...},
              ...
            ],
          },
          "tts": { ... same shape ... },
        }

    Each chain entry is the merged view of {connection + catalog model
    fields + per-ref settings overrides}, in that order — settings win.
    The `adapter` key is what the engine branches on (``local_whisper``
    / ``gemini`` / ``kokoro``) to pick the right config object.
    Returns an empty dict when no ``voice:`` block exists.
    """
    bundle = load_bundle()
    if bundle.voice is None:
        return {}

    voice = bundle.voice
    out: dict = {}

    def _materialize_provider(provider) -> dict:  # noqa: ANN001
        conn = provider.ref.connection
        merged: dict = {
            "ref": provider.ref.ref,
            "adapter": conn.adapter,
            # `model` is the actual provider-side model name; `provider`
            # alias keeps the cost-ledger key (= catalog model id) close
            # so STTEngine / TTSEngine debit pricing without extra mapping.
            "provider": provider.ref.model.id,
            "model": provider.ref.model.model,
            "model_id": provider.ref.model.model,
            "timeout_seconds": int(
                provider.settings.get("timeout_seconds", conn.timeout_seconds)
            ),
        }
        if conn.api_key_env:
            merged["api_key_env"] = conn.api_key_env
        for k in (
            "device",
            "compute_type",
            "beam_size",
            "preload",
            "output_format",
            "sample_rate",
            "synthesis_presets",
            "voices_file",
            "mix",
            "lang",
            # Cloud TTS: the catalog names the endpoint (this model family
            # is on the Interactions API, not the connection's chat base),
            # the voice, and the one line of operator-set identity.
            "base_url_override",
            "voice",
            "audio_profile",
        ):
            if k in provider.ref.model.fields:
                merged[k] = provider.ref.model.fields[k]
        merged.update(dict(provider.settings))
        merged["daily_budget_usd"] = provider.daily_budget_usd
        return merged

    def _materialize_chain(chain) -> dict:  # noqa: ANN001
        # Drop providers whose tier or provider switch is off — voice runtime
        # then ignores them as if they weren't listed (no engine built, no
        # cost accrual). An entirely-disabled chain becomes ``chain: []``,
        # which `_build_voice_runtime` already handles ("no `voice:` block"
        # path equivalent — the engine simply isn't constructed).
        kept: list[dict] = []
        if chain.mode != "active":
            # Belt to the loader's brace. The loader drops an inactive lane
            # before it resolves anything, so this is unreachable from
            # `roles.yaml` today — but `_build_voice_runtime` reads `chain`
            # and nothing else, so a lane arriving inactive by any other route
            # would build an engine, warm it up and spend. The empty chain is
            # the shape that path already treats as "no engine".
            logger.info("voice: lane is %s — no chain materialized", chain.mode)
            return {"mode": chain.mode, "chain": kept}
        for p in chain.chain():
            conn = p.ref.connection
            if not conn.tier_enabled or not conn.enabled:
                logger.info(
                    "voice: skipping ref=%s (tier_enabled=%s, enabled=%s)",
                    p.ref.ref, conn.tier_enabled, conn.enabled,
                )
                continue
            kept.append(_materialize_provider(p))
        return {"mode": chain.mode, "chain": kept}

    if voice.stt is not None:
        out["stt"] = _materialize_chain(voice.stt)
    if voice.tts is not None:
        out["tts"] = _materialize_chain(voice.tts)
    return out


def rebuild_adapters(app: Any) -> dict[str, Any]:
    """Re-resolve chat_brain + observer + voice runtime from a
    freshly-edited `providers.yaml` / `roles.yaml`. Returns a summary dict naming the new
    primary chat_brain (`provider/model`) and a flag for the voice
    runtime's presence so the watcher can compose a meaningful toast.

    Live `ChatSession` instances are rewired in place so an operator
    edit to `roles.yaml` (model swap, compact_threshold tweak,
    keep_recent_turns) lands on the next turn of every active session,
    not just on freshly-opened ones. The session dataclass exposes
    every knob as a writable attribute; mid-stream swap is safe because
    `adapter.stream(...)` returns an iterator whose pages don't
    re-resolve `self.adapter` (the in-flight stream finishes on the
    old handle, the next tool-loop iteration uses the new one).

    The dict held by `app["config"].models` is also
    refreshed here. `ServerConfig` is a frozen dataclass, but its `models`
    field is a plain dict; mutating in place keeps every REST surface
    that reads `request.app["config"].models` (e.g. `/api/identity`,
    `/api/voice/providers`) consistent with the on-disk YAML.

    The `roles.yaml::reranker` role is re-resolved and swapped the same
    way: both `app["memory_bundle"].pipeline._reranker` and the live
    `VaultSearchTool._reranker` (reached via `app["tool_registry"]`) are
    updated in place, so an edit lands on the next retrieval call rather
    than requiring a restart. An in-flight retrieval already holds its
    own reference to the old reranker at the point it awaits `rerank()`,
    so the swap does not affect it mid-call — same argument as the
    `ChatSession` case above.
    """
    summary: dict[str, Any] = {}

    # Refresh the REST-facing config snapshot so
    # /api/identity, /api/voice/providers, etc. see the same values the
    # adapters were just rebuilt against. We do this before the adapter
    # rebuild so a downstream failure still leaves the dict and handles
    # in agreement.
    config = app.get("config")
    if config is not None and isinstance(getattr(config, "models", None), dict):
        try:
            from tesseract.config.loader import load_config
            from tesseract.mirror.server.config import synthesize_legacy_models_dict

            bundle = load_config(providers_path=PROVIDERS_YAML, roles_path=ROLES_YAML)
            refreshed = synthesize_legacy_models_dict(bundle)
            config.models.clear()
            config.models.update(refreshed)
            summary["config_models_refreshed"] = True
        except Exception:
            logger.exception("rebuild_adapters: providers/roles in-memory refresh failed")

    # Chat adapters
    #
    # Drop boot's parked resolution first. `_build_chat_infra` consumes it, but
    # a config save can land before STAGE 2 gets there — and what this function
    # is about to resolve is newer than what boot parked, by definition.
    try:
        del app["chat_runtime"]
    except (KeyError, TypeError):
        pass
    try:
        chat_cfg, adapter, options, adapter_chain = resolve_chat_brain_runtime()
    except RuntimeError as exc:
        logger.warning("rebuild_adapters: chat_brain resolution failed (%s)", exc)
        summary["chat_brain_error"] = str(exc)
    else:
        app["adapter"] = adapter
        app["adapter_options"] = options
        app["adapter_entry"] = chat_cfg
        app["adapter_chain"] = adapter_chain
        summary["chat_brain"] = f"{chat_cfg.provider}/{chat_cfg.model}"

        # Heal a registry that booted without a chat adapter: the
        # sub-agent tools are gated on one, and their absence silently
        # kills autonomy dispatch (see `register_agent_session_tools`).
        # Only fires when they are genuinely missing — a healthy registry
        # keeps the handles it already has.
        live_registry = app.get("tool_registry")
        if live_registry is not None and live_registry.get("invoke_agent") is None:
            try:
                config = app.get("config")
                register_agent_session_tools(
                    live_registry,
                    adapter=(
                        build_fallback_adapter(adapter_chain)
                        if adapter_chain else adapter
                    ),
                    options=options,
                    chat_cfg=chat_cfg,
                    policy=getattr(config, "permissions", None),
                    cost_ledger=app.get("cost_ledger"),
                )
                # Attach the two class-default postures to the live policy;
                # boot's own pass ran before these tools existed.
                _wire_tool_defaults(
                    live_registry, getattr(config, "permissions", None)
                )
                summary["agent_tools_registered"] = True
                logger.info(
                    "rebuild_adapters: registered invoke_agent + session_open "
                    "into the live registry (chat_brain resolved after a "
                    "boot without one)"
                )
            except Exception:
                logger.exception(
                    "rebuild_adapters: agent-session tool registration failed"
                )

        # Re-build the system prompt only if the prompt builder closure
        # exists — otherwise the Mirror started without chat infra and we
        # leave system_prompt at its boot value.
        prompt_builder = app.get("prompt_builder")
        if callable(prompt_builder):
            try:
                app["system_prompt"] = prompt_builder()
            except Exception:
                logger.exception("rebuild_adapters: prompt rebuild failed")

        # Propagate the freshly-resolved chat runtime onto every live
        # ChatSession. Without this, an operator edit only lands on
        # sessions created AFTER the edit — sessions opened before
        # the swap keep their captured adapter, threshold, and
        # keep_recent_turns until they reconnect, which contradicts
        # the watcher's "external YAML edits reflect live" goal.
        try:
            live_adapter = build_fallback_adapter(adapter_chain) if adapter_chain else adapter
            sessions = app.get("server_sessions") or {}
            swapped = 0
            for srv_session in list(sessions.values()):
                # Every open chat, not only the one on screen. A background
                # chat holds its own ChatSession and kept the old wiring until
                # it was closed and reopened. One enumeration, shared with the
                # settings writer, because two copies of "which sessions are
                # open" drift.
                for chat_session in open_chat_sessions(srv_session):
                    chat_session.adapter = live_adapter
                    chat_session.options = options
                    chat_session.compact_threshold = chat_cfg.compact_threshold
                    chat_session.headroom_multiplier = chat_cfg.headroom_multiplier
                    chat_session.keep_recent_turns = chat_cfg.keep_recent_turns
                    chat_session.head_anchor_messages = chat_cfg.head_anchor_messages
                    chat_session.summary_char_budget = chat_cfg.summary_char_budget
                    # The ceiling belongs to the MODEL, and this loop exists
                    # because the model just changed. Leaving it behind meant a
                    # swap onto a tighter model kept the looser model's guard,
                    # so the session would assemble a prompt the new one
                    # rejects: codex accepts 1,048,576 characters where luna is
                    # worth 3,481,800.
                    chat_session.prompt_char_budget = chat_cfg.prompt_char_budget
                    chat_session.max_tool_iterations = chat_cfg.tool_iteration_cap
                    chat_session.max_consecutive_adapter_errors = (
                        chat_cfg.consecutive_error_cap
                    )
                    if "system_prompt" in app:
                        chat_session.system_prompt = app["system_prompt"]
                    swapped += 1
            if swapped:
                summary["live_sessions_swapped"] = swapped
        except Exception:
            logger.exception("rebuild_adapters: live session swap failed")

    # Reranker — role-wired cross-encoder (roles.yaml top-level `reranker:`).
    # Re-resolved from the freshly-edited providers.yaml/roles.yaml and
    # swapped into both live consumers that hold the old handle:
    # `RetrievalPipeline._reranker` (memory_search) and
    # `VaultSearchTool._reranker` (vault_search). Construction is cheap
    # (path/int fields only — `CrossEncoderReranker` lazy-loads the ONNX
    # session + tokenizer off the loop on first `rerank()` call, see
    # `reranker.py::_ensure_loaded`), so this runs inline rather than via
    # `asyncio.to_thread`, matching how `build_memory_bundle` constructs it
    # at boot. A resolution failure is contained the same way the config
    # refresh above is — logged + recorded in the summary — and neither
    # consumer is touched, so retrieval keeps its previous handle instead
    # of one consumer swapping while the other doesn't.
    try:
        bundle_cfg = load_bundle()
        reranker_ref = bundle_cfg.reranker
        reranker_cfg = load_reranker_cfg(bundle_cfg)
        new_reranker = None
        if reranker_cfg:
            from tesseract.memory.reranker import CrossEncoderReranker

            new_reranker = CrossEncoderReranker(
                model_path=reranker_cfg["model_path"],
                tokenizer_path=reranker_cfg["tokenizer_path"],
                max_seq_len=reranker_cfg["max_seq_len"],
                candidate_cap=reranker_cfg["candidate_cap"],
            )
    except Exception as exc:
        logger.exception("rebuild_adapters: reranker resolution failed")
        summary["reranker_error"] = str(exc)
    else:
        swapped: list[str] = []
        memory_bundle = app.get("memory_bundle")
        pipeline = getattr(memory_bundle, "pipeline", None)
        if pipeline is not None:
            pipeline._reranker = new_reranker
            swapped.append("memory_search")
        live_registry = app.get("tool_registry")
        vault_search_tool = (
            live_registry.get("vault_search") if live_registry is not None else None
        )
        if vault_search_tool is not None:
            vault_search_tool._reranker = new_reranker
            swapped.append("vault_search")
        if memory_bundle is not None:
            memory_bundle.reranker = new_reranker
        # Report what actually moved, not what was resolved. A boot that
        # never built a memory bundle or never registered `vault_search`
        # leaves nothing holding the new handle, and a toast claiming the
        # swap landed would be the one case where the operator trusts a
        # reload that did not happen.
        if not swapped:
            summary["reranker_error"] = (
                "resolved but not applied — no live consumer "
                "(memory bundle / vault_search) present"
            )
        else:
            summary["reranker"] = reranker_ref.ref if new_reranker is not None else None
            summary["reranker_consumers"] = swapped

    # Observer
    cost_ledger = app.get("cost_ledger")
    try:
        observer = build_observer(cost_ledger=cost_ledger)
    except Exception:
        logger.exception("rebuild_adapters: observer rebuild failed")
        observer = app.get("observer")  # keep old handle on failure
    if observer is not app.get("observer"):
        app["observer"] = observer
        subscriber = app.get("observer_subscriber")
        if subscriber is not None:
            # The subscriber is NOT rebuilt. Every live ChatSession — the
            # cockpit's and a channel's alike — holds a back-reference taken
            # when it was built, so replacing it would orphan every
            # conversation the runtime is holding, and a channel session is
            # rebuilt by no reconnect. Pointing it at the new observer keeps
            # them all. Rebuilding it is what silently killed the observer on
            # every providers/roles edit (found live 2026-07-30: zero fires
            # all evening).
            if observer is None:
                subscriber.disarm()
                app["observer_subscriber"] = None
            else:
                subscriber.set_observer(observer)
        elif observer is not None:
            from tesseract.brain.observer_subscriber import ObserverSubscriber

            subscriber = ObserverSubscriber(observer)
            if app.get("observer_state") in {"armed", "observing"}:
                subscriber.arm()
            app["observer_subscriber"] = subscriber
            # Sessions that already exist were built when there was no
            # subscriber to attach to, so they carry none. Only conversations
            # opened from here on are observed.
            logger.info(
                "rebuild_adapters: observer subscriber created after boot — "
                "conversations already open are not observed"
            )
        summary["observer"] = "rebuilt"

    # Voice runtime — delegate to the Mirror app's existing helper so the
    # cloud-only Gemini wiring stays in one place.
    try:
        from tesseract.mirror.server.app import _build_voice_runtime  # late import; circular if top
    except Exception:
        logger.exception("rebuild_adapters: voice runtime helper unavailable")
    else:
        try:
            _build_voice_runtime(app)
            summary["voice"] = bool(app.get("tts_engine") or app.get("stt_engine"))
        except Exception:
            logger.exception("rebuild_adapters: voice runtime rebuild failed")

    return summary


def register_agent_session_tools(
    registry: ToolRegistry,
    *,
    adapter: ModelAdapter,
    options: AdapterOptions,
    chat_cfg: ChatBrainConfig,
    policy: "PermissionPolicy | None" = None,
    cost_ledger: Any = None,
) -> None:
    """Register the two adapter-gated sub-agent tools into ``registry``.

    Split out of :func:`build_tool_registry` so :func:`rebuild_adapters`
    can heal a registry that booted without a chat adapter — before this,
    a credential/provider blip at boot removed ``invoke_agent`` for the
    life of the process and only a restart brought it back.
    """
    # A sub-agent shares its parent's context window, so it folds on the
    # operator's compaction settings rather than on ChatSession's defaults,
    # which are there for a session built outside boot.
    from tesseract.brain.agent_factory import CompactionSettings

    compaction = CompactionSettings(
        compact_threshold=chat_cfg.compact_threshold,
        headroom_multiplier=chat_cfg.headroom_multiplier,
        keep_recent_turns=chat_cfg.keep_recent_turns,
        head_anchor_messages=chat_cfg.head_anchor_messages,
        summary_char_budget=chat_cfg.summary_char_budget,
        prompt_char_budget=chat_cfg.prompt_char_budget,
    )
    # `None`, not the user root: both tools READ, so they want whichever
    # card wins between the shipped tree and the operator's.
    registry.register(InvokeAgentTool(
        agents_dir=None,
        adapter=adapter,
        options=options,
        parent_registry=registry,
        max_tool_iterations=chat_cfg.tool_iteration_cap,
        max_consecutive_adapter_errors=chat_cfg.consecutive_error_cap,
        policy=policy,
        cost_ledger=cost_ledger,
        compaction=compaction,
    ))
    registry.register(SessionOpenTool(
        agents_dir=None,
        adapter=adapter,
        options=options,
        registry=registry,
        max_tool_iterations=chat_cfg.tool_iteration_cap,
        max_consecutive_adapter_errors=chat_cfg.consecutive_error_cap,
        compaction=compaction,
    ))
    # Both are working-set entries when the operator has kept them. At boot
    # `_apply_tool_tiers` would do this; on the rebuild path nothing else
    # would, and an extended-tier invoke_agent is invisible to the chat prompt.
    core = core_tool_names()
    for name in ("invoke_agent", "session_open"):
        tool = registry.get(name)
        if tool is not None and name in core:
            tool.tier = "core"


def build_tool_registry(
    alarm_registry: AlarmRegistry | None = None,
    policy: "PermissionPolicy | None" = None,
    app: Any = None,
    chat_runtime: ChatBrainRuntime | None = None,
    include_home_tools: bool = False,
) -> tuple[ToolRegistry, MoodState, MemoryBundle, AlarmRegistry]:
    """Register every tool the runtime knows how to execute.

    Returns the registry, MoodState, MemoryBundle, and the
    shared AlarmRegistry. The alarm registry is constructed here (with
    YAML persistence) unless the caller injects one — tests pass a fresh
    instance to isolate state.

    `policy` is forwarded to `InvokeAgentTool` so nested sub-agent sessions
    inherit the parent permission policy in Mirror. The REPL currently
    re-registers `invoke_agent` with its own ask_fn after policy load
    (REPL retired 2026-07-13); Mirror calls this with policy=app["config"]
    .permissions to close the audit C2 inheritance gap. None is allowed for
    test/back-compat callers.

    ``app`` is the Mirror ``web.Application`` (or None for REPL/tests).
    When provided, tools that broadcast WS envelopes (workspace_post /
    workspace_reply / chat_initiate) wire a lazy app_provider closure so
    their writes light up open tabs in realtime without a manual refresh.

    ``chat_runtime`` is an already-resolved :data:`ChatBrainRuntime` for the
    caller that resolved one moments ago. Absent, this resolves its own —
    which builds an SDK client per chain entry and costs seconds, so a caller
    that has the answer already should hand it over rather than let it be
    computed twice. Sharing the adapter instances is safe: they are stateless
    wrappers around HTTP/subprocess (see ``FallbackAdapter.fork``), and the
    per-entry breaker state that is NOT shareable lives on the
    ``FallbackAdapter`` this function builds for itself.
    """
    app_provider = (lambda: app) if app is not None else None
    registry = ToolRegistry()
    mood = MoodState()
    registry.register(SetMoodTool(mood_state=mood))
    registry.register(SetStateTool(affect=EntityAffect()))
    # home_dir() (not TESSERACT_DIR): diary entries are operator/the assistant
    # state, not code — an app update that replaces the code tree must
    # not wipe them.
    # Call-time resolved, matching Tasks 1-4's idiom (`workspace_dir()` /
    # `agents_dir()`), not the TESSERACT_HOME constant frozen at import.
    registry.register(DiaryAppendTool(repo_root=home_dir()))
    registry.register(TasksSetTool())
    registry.register(TasksUpdateTool())
    registry.register(SpawnCheckTool())
    registry.register(SpawnAwaitTool())
    registry.register(SpawnCancelTool())
    registry.register(SoulGrowthProposeTool(repo_root=ROOT))
    registry.register(ProposeChangeTool(repo_root=ROOT))

    from tesseract.workspace_events import EventStore

    workspace_store = EventStore(home_logs_root())
    registry.register(WorkspacePostTool(store=workspace_store, app_provider=app_provider))
    registry.register(WorkspaceReplyTool(store=workspace_store))

    from tesseract.kernel.tools.agenda_comment import AgendaCommentTool
    from tesseract.kernel.tools.task_close import TaskCloseTool
    from tesseract.kernel.tools.task_propose import TaskProposeTool
    from tesseract.kernel.tools.task_work import TaskWorkTool
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore

    agenda_store = AgendaStore()
    registry.register(AgendaCommentTool(store=agenda_store))
    registry.register(TaskProposeTool(store=agenda_store))
    registry.register(TaskWorkTool(store=agenda_store))
    registry.register(TaskCloseTool(store=agenda_store))

    from tesseract.kernel.tools.autonomy_read import AutonomyReadTool
    from tesseract.kernel.tools.channel_notify import ChannelNotifyTool
    from tesseract.kernel.tools.chat_initiate import ChatInitiateTool
    from tesseract.kernel.tools.cockpit_show import CockpitShowTool
    from tesseract.kernel.tools.context_read import ContextReadTool
    from tesseract.kernel.tools.context_set import ContextSetTool
    from tesseract.kernel.tools.health_leave import HealthLeaveTool
    from tesseract.kernel.tools.orb_visibility import OrbVisibilityTool
    from tesseract.kernel.tools.pipeline_run_stage import PipelineRunStageTool
    from tesseract.kernel.tools.retention_set_window import RetentionSetWindowTool
    from tesseract.kernel.tools.session_continue import SessionContinueTool
    from tesseract.kernel.tools.workspace_decide import (
        WorkspaceDecideTool,
        WorkspacePendingTool,
    )
    from tesseract.kernel.tools.workspace_hold import WorkspaceHoldTool
    registry.register(AutonomyReadTool(app_provider=app_provider))
    registry.register(WorkspacePendingTool(app_provider=app_provider))
    registry.register(WorkspaceDecideTool(app_provider=app_provider))
    registry.register(ChannelNotifyTool())
    registry.register(ChatInitiateTool(app_provider=app_provider))
    registry.register(CockpitShowTool(app_provider=app_provider))
    registry.register(ContextReadTool())
    registry.register(ContextSetTool(app_provider=app_provider))
    registry.register(HealthLeaveTool(app_provider=app_provider))
    registry.register(OrbVisibilityTool(app_provider=app_provider))
    registry.register(PipelineRunStageTool(app_provider=app_provider))
    registry.register(RetentionSetWindowTool())
    registry.register(SessionContinueTool())
    registry.register(WorkspaceHoldTool())

    if alarm_registry is None:
        # Call-time resolved (never the frozen import-time constant this used
        # to be) so a relocated TESSERACT_HOME takes effect (distributable-app
        # pre-installer blocker). The one-time legacy-state
        # migration is a separate, explicit `ensure_alarms_state_migrated()`
        # call at the real entry points (mirror/supervisor/agent_controller
        # main()), not here — this function is exercised by unit tests too
        # often to carry a destructive migration side effect safely.
        alarm_registry = AlarmRegistry(state_file=alarms_state_path())
    registry.register(AlarmSetTool(alarm_registry=alarm_registry))
    registry.register(AlarmListTool(alarm_registry=alarm_registry))
    registry.register(AlarmCancelTool(alarm_registry=alarm_registry))
    registry.register(AlarmSnoozeTool(alarm_registry=alarm_registry))

    # The assistant's own accounts. It can see what it has and ask for what
    # it lacks; no tool reads a value, because the runtime puts one into a
    # request at the moment it is sent and never hands it back here.
    registry.register(CredentialListTool())
    registry.register(CredentialRequestTool())
    registry.register(CredentialSetupTool())
    registry.register(ApiRequestTool())
    registry.register(CommandRunTool())

    # Project registry — what "the thing we are working on" is. `project_open`
    # moves the active root, which the prompt block renders and new lanes
    # default their cwd to.
    registry.register(ProjectListTool())
    registry.register(ProjectLinkTool())
    registry.register(ProjectOpenTool())
    registry.register(
        ProjectNewTool(store=workspace_store, app_provider=app_provider)
    )
    # Version control on whatever the active project is, plus any other
    # repo outside the seal. Registered beside the project tools because
    # it acts on the root they decide.
    registry.register(GitTool())

    registry.register(ScheduleCreateTool())
    registry.register(ScheduleListTool())
    registry.register(ScheduleUpdateTool())
    registry.register(ScheduleRunTool())
    registry.register(ScheduleRemoveTool())
    registry.register(
        AskClarificationTool(store=workspace_store, app_provider=app_provider)
    )

    registry.register(MemoryGetTool())
    registry.register(WorkspaceReadTool())

    # atlas_query — the one surface for asking the derived graph how records
    # connect. Read-only: it opens `atlas.json` and returns text. The same
    # `orchestrator/atlas/retrieval.py` answers here and behind the MCP verb,
    # so an outside client and an in-process agent cannot get different
    # answers to the same question.
    registry.register(AtlasQueryTool())

    # Controller-owned lanes (claude / codex). Provider lives on the
    # ToolContext; Mirror's brain hits the "lane manager not wired" error
    # path until the daemon IPC bridge is up.
    registry.register(LaneOpenTool())
    registry.register(LaneSendTool())
    registry.register(LaneTurnTool())
    registry.register(LaneReadTool())
    registry.register(LaneStatusTool())
    registry.register(LaneAttachTool())
    registry.register(LaneCloseTool())
    registry.register(LaneListTool())

    # name→lane_id binding layer over LaneManager. tmux Agent Teams
    # pattern: the assistant holds two stable persistent lanes (coder/claude +
    # auditor/codex) the autonomy paths route through.
    registry.register(LaneNamedGetTool())
    registry.register(LaneNamedListTool())
    registry.register(LaneNamedEnsureTool())

    # trio W3 — unified steer verb over the three steerable substrates
    # (lanes / interactive sessions / controller sessions).
    registry.register(WorkSendTool())

    # Y-2 — Surface Protocol v1. Tools emit canvas UI intents; the
    # SurfaceStore singleton (orchestrator/surfaces) relays them to the
    # Mirror canvas via the `surface` background-bus channel. All AUTO —
    # canvas mutations carry no tool-layer security weight.
    registry.register(SurfaceCreateTool())
    registry.register(SurfaceUpdateTool())
    registry.register(SurfaceFocusTool())
    registry.register(SurfaceCloseTool())
    registry.register(SurfaceListTool())
    registry.register(SurfaceHighlightTool())
    # The only tool that reads PIXELS. Everything else reports structure —
    # which is why a card can be registered, listed, and blank.
    registry.register(ScreenLookTool())

    # `open` resolves a target and dispatches to one of these; nothing selects
    # them directly. The canvas half stays ungated — rendering carries no
    # tool-layer weight — while `os_launch` is ASK, because ShellExecute starts
    # whatever program owns the file and `bash_security` never sees that call.
    registry.register(OsOpenUrlTool())
    registry.register(OsLaunchTool())
    registry.register(OpenTool())
    registry.register(SurfaceBindSessionTool())

    # Operating a card, as against drawing or redrawing one. Tier 2: it
    # only matters once something is already on the canvas.
    from tesseract.kernel.tools.surface_control import SurfaceControlTool
    registry.register(SurfaceControlTool())

    # browser_* cockpit tools (headless Playwright, pc_audit sink). The
    # first seven read a page or drive it one control at a time; the last
    # six operate one, which is why two of those are ASK.
    registry.register(BrowserNavigateTool())
    registry.register(BrowserSnapshotTool())
    registry.register(BrowserClickTool())
    registry.register(BrowserFillFormTool())
    registry.register(BrowserScreenshotTool())
    registry.register(BrowserNetworkRequestsTool())
    registry.register(BrowserCloseTool())
    registry.register(BrowserKeyTool())
    registry.register(BrowserMediaTool())
    registry.register(BrowserScrollTool())
    registry.register(BrowserHoverTool())
    registry.register(BrowserSelectTool())
    registry.register(BrowserWaitForTool())

    registry.register(FileReadTool())
    registry.register(GlobTool())
    registry.register(GrepTool())
    registry.register(LogTriageTool())
    registry.register(SystemDiagnoseTool())
    registry.register(FileWriteTool())
    registry.register(FileCopyTool())
    registry.register(FileMoveTool())
    registry.register(PdfReadTool())
    registry.register(WebSearchTool())
    registry.register(TavilySearchTool())
    registry.register(TavilyExtractTool())

    registry.register(BashTool())

    registry.register(DelegateCoderTool())
    registry.register(DelegateAuditorTool())
    registry.register(DelegateSecondOpinionTool())
    registry.register(AgentAskTool())
    # 2026-05-24 — controller-as-orchestrator dispatch. The autonomy
    # runner picks this up via `_route_for_kind(AGENT_CONTROLLER)`;
    # chat-side surfaces (start_controller_session) and any future
    # caller route through the same tool so behaviour stays uniform.
    registry.register(DelegateAgentControllerTool())
    registry.register(StartControllerSessionTool())

    # Text-to-image via the configured `image_generator` role (primary set
    # in roles.yaml). The tool resolves role + model at call time and posts
    # to the genai endpoint directly — it does NOT flow through `build_adapter`,
    # so there's no chain failover here. Role-disabled / no-key paths return
    # ToolResult(is_error=True) with a clean message instead of crashing.
    registry.register(ImageGenerateTool())

    # Outbound channel media — the assistant calls these to
    # reply with audio / images / files on external channels (Telegram
    # today, future WhatsApp / Signal). Each resolves the live adapter
    # via `integrations.get_channel` and dispatches to its `send_*`
    # methods. Default posture AUTO — replying mid-conversation is the
    # expected behavior; permissions.yaml can flip to ASK per-tool.
    registry.register(ChannelSendVoiceTool())
    registry.register(ChannelSendPhotoTool())
    registry.register(ChannelSendDocumentTool())
    # Full outbound parity. Video / animation /
    # video_note for rich media; sticker / location / poll for
    # conversational range; channel_react for lightweight acks.
    registry.register(ChannelSendVideoTool())
    registry.register(ChannelSendAnimationTool())
    registry.register(ChannelSendVideoNoteTool())
    registry.register(ChannelSendStickerTool())
    registry.register(ChannelSendLocationTool())
    registry.register(ChannelSendPollTool())
    registry.register(ChannelReactTool())
    # 2026-05-17 — direct log reader for when memory/recall come back
    # empty (the bridge restart case where reflection didn't fire).
    registry.register(ChannelHistoryReadTool())

    registry.register(Context7LookupTool())
    registry.register(ConscienceStatusTool())
    registry.register(BreakerStatusTool())
    registry.register(BreakerResetTool())

    # The user root specifically: these two move files through
    # `pending/` and `rejected/`, which only exist on the operator's side.
    agents_dir = user_agents_dir()
    bundle = load_bundle()
    # AgentCreateTool only reads `models_config["roles"]` to validate the role
    # name on a new agent. Hand it the role keys in the legacy shape so that
    # tool stays decoupled from the loader's typed bundle.
    models_cfg = {"roles": {name: {} for name in bundle.roles}}
    # Stage 10 — both tools carry the workspace store: agent_create files
    # the agent_approval proposal card (broadcast live via app_provider);
    # agent_promote settles the open card when promotion happens chat-side.
    registry.register(AgentCreateTool(
        agents_dir=agents_dir,
        models_config=models_cfg,
        event_store=workspace_store,
        app_provider=app_provider,
    ))
    registry.register(AgentPromoteTool(agents_dir=agents_dir, event_store=workspace_store))

    # Skill lifecycle, mirror of the agent
    # Stage-10 flow. skill_create files the skill_approval proposal card
    # (broadcast live via app_provider); skill_promote settles the open card
    # when promotion happens chat-side. Skills live under the workspace tree.
    skills_dir = workspace_dir() / "skills"
    registry.register(SkillCreateTool(
        skills_dir=skills_dir,
        event_store=workspace_store,
        app_provider=app_provider,
        tool_names=lambda: frozenset(registry.names()),
        registry_provider=lambda: registry,
    ))
    registry.register(SkillPromoteTool(skills_dir=skills_dir, event_store=workspace_store))
    registry.register(SkillRefineTool(
        skills_dir=skills_dir,
        event_store=workspace_store,
        app_provider=app_provider,
        tool_names=lambda: frozenset(registry.names()),
    ))

    # The door to a playbook the turn is carrying only as a line. Same shape
    # as `tool_search`: the map names every playbook, this fetches the whole
    # of one. It does NOT read the body — `file_read` does, and that read is
    # what the usage log records.
    registry.register(PlaybookSearchTool(skills_dir=skills_dir))

    # Resolve the chat_brain adapter first so it can be threaded into both
    # the librarian (M2 classifier fallback) and the vault-librarian wiring
    # below. If no provider has credentials, adapter=None and the missing-
    # prefix path in Librarian._write_section skips rather than promotes.
    chat_cfg: ChatBrainConfig | None
    try:
        chat_cfg, chat_adapter, chat_options, chat_chain = (
            chat_runtime if chat_runtime is not None else resolve_chat_brain_runtime()
        )
    except RuntimeError as exc:
        logger.warning("chat_brain adapter unavailable (%s); librarian + vault_librarian will no-op their LLM paths", exc)
        chat_cfg = None
        chat_adapter = None
        chat_options = AdapterOptions()
        chat_chain = []

    # Sub-agent sessions invoked through `invoke_agent` should respect the
    # same failover contract as the top-level ChatSession (W3/W4 reviewer
    # follow-up I1, 2026-04-29). Wrap the resolved chain in FallbackAdapter
    # for the sub-agent path; the librarian / vault_librarian classifier
    # stays on the bare primary because their callers don't need failover.
    invoke_adapter: ModelAdapter | None
    if chat_chain:
        invoke_adapter = build_fallback_adapter(chat_chain)
    else:
        invoke_adapter = chat_adapter

    bundle = ensure_memory_tools(
        registry,
        adapter=chat_adapter,
        adapter_options=chat_options,
    )

    vault_manager = VaultManager(vault_root=TESSERACT_HOME / "vault")
    vault_cfg = load_vault_config()
    registry.register(VaultSearchTool(
        embeddings=bundle.embeddings,
        fts_index=bundle.fts_index,
        vault_manager=vault_manager,
        vault_cfg=vault_cfg,
        reranker=bundle.reranker,
    ))

    # Always constructed — embeddings=None is the BM25-only degraded mode;
    # gating the whole indexer on embeddings would silently drop vault
    # FTS chunks whenever Ollama is down.
    # `log_dir` on both, the same as `VaultLintTool` below: without it their
    # breakers write nothing, so a trip is invisible to the cockpit panel, the
    # watchman, the conscience signal and `system_diagnose`, and is forgotten
    # at the next restart. Three sibling breakers, one answer.
    vault_indexer = VaultIndexer(
        embeddings=bundle.embeddings,
        fts_index=bundle.fts_index,
        log_dir=log_dir("circuit-breakers"),
    )

    vault_librarian = VaultLibrarian(
        vault_manager=vault_manager,
        adapter=chat_adapter,
        adapter_options=chat_options,
        config=vault_cfg,
        agents_dir=agents_dir,
        log_dir=log_dir("circuit-breakers"),
    )

    registry.register(VaultQueryTool(
        vault_manager=vault_manager,
        vault_config=vault_cfg,
        vault_librarian=vault_librarian,
    ))
    registry.register(VaultIngestTool(
        vault_manager=vault_manager,
        vault_indexer=vault_indexer,
        vault_librarian=vault_librarian,
    ))
    registry.register(VaultLintTool(
        vault_manager=vault_manager,
        vault_config=vault_cfg,
        vault_librarian=vault_librarian,
        log_dir=log_dir("circuit-breakers"),
        agents_dir=agents_dir,
    ))

    # invoke_agent ships with the Mirror registry so vault-librarian and
    # other sub-agent sessions are reachable from chat. Per-session
    # ask_fn + tool_context flow through `context` at run() time. The
    # process-wide `policy` is passed in by the Mirror caller so nested
    # sub-agent sessions inherit it (audit C2 fix, 2026-04-29). The REPL
    # site (REPL, retired 2026-07-13) re-registered with its own per-process
    # ask_fn for the single-operator case.
    if invoke_adapter is not None and chat_cfg is not None:
        register_agent_session_tools(
            registry,
            adapter=invoke_adapter,
            options=chat_options,
            chat_cfg=chat_cfg,
            policy=policy,
            cost_ledger=(app.get("cost_ledger") if app is not None else None),
        )
    else:
        # Not cosmetic: with these two absent, every autonomy dispatch of a
        # MARKDOWN_AGENT / AGENT_SELF worker dies on "unknown tool:
        # invoke_agent" (live install, 2026-07-30). Name the consequence so
        # a boot-time credential blip is diagnosable from the log alone.
        logger.warning(
            "chat_brain adapter unavailable — invoke_agent + session_open are "
            "NOT registered; autonomy markdown_agent/agent_self dispatch will "
            "fail until the chain resolves (config edit → rebuild_adapters, "
            "or restart)"
        )
    registry.register(SessionSendTool())
    registry.register(SessionResultTool())
    registry.register(SessionCloseTool())
    registry.register(SessionListTool())
    registry.register(ControllerSessionListTool())

    # brief_render — operator-facing `/brief` slash. Invokes the digester
    # agents in order, writes the brief markdown + a brief-as-memory record.
    # Uses the same chat_brain adapter `InvokeAgentTool` was built with
    # (loop above) so sub-digester completions route through the
    # operator's configured primary model.
    registry.register(BriefRenderTool(
        adapter=invoke_adapter,
        adapter_options=chat_options,
        memory_store=bundle.store,
        event_store=workspace_store,
    ))

    # brief_read — read-only companion to brief_render. Returns
    # today's brief body (frontmatter stripped) so the chat_brain can
    # answer voice "read brief" requests by reading the file back
    # through the normal TTS lane.
    registry.register(BriefReadTool())

    # Lean-agent-os P1 Task 2 — tool_search meta-tool. AUTO, read-only;
    # searches the full registry and enables matching extended tools for
    # the rest of the session. See `core_tool_names()` above.
    registry.register(ToolSearchTool())

    # Tools the operator's own tree carries, before the tier and posture
    # passes so a home tool faces both exactly as a shipped one does. It
    # cannot take a registered name, so this can only add.
    #
    # OFF by default, and the live callers opt in. The generators
    # (`generate_guide`, `guide_facts`, `generate_working_set`,
    # `check_tool_claims`) all build a registry to
    # describe the SHIPPED tree, and a tool from one operator's home directory
    # in `Guide/reference/tools.md` is that operator's machine published to
    # strangers. Defaulting off means a generator added later is safe without
    # knowing this rule; the cost of the opposite mistake is only that a home
    # tool does not load, which is visible in the first session.
    if include_home_tools:
        from tesseract.kernel.home_tools import sync_home_tools

        sync_home_tools(registry)

    # Lean-agent-os P1 Task 2 — mark the pinned core tools before the
    # posture/tier validation pass below.
    _apply_tool_tiers(registry)

    # Wire each tool's class-declared baseline posture into the policy and
    # report drift against `permissions.yaml::tools`. Boot is a hard gate —
    # any tool registered without a valid `default_posture` raises so the
    # parity bug (registry has tool, yaml doesn't, posture silently falls
    # to ASK) cannot recur.
    _wire_tool_defaults(registry, policy)

    # Stash the live vault_librarian on the registry so the Mirror app
    # (and any consumer who already holds a registry reference) can
    # reach `compile_source()` without rebuilding the librarian.
    registry.vault_librarian = vault_librarian  # type: ignore[attr-defined]

    return registry, mood, bundle, alarm_registry


def _apply_tool_tiers(registry: ToolRegistry) -> None:
    """Mark the configured working set as `tier = "core"` on the live instances.

    Instance-attribute assignment shadows the `Tool.tier` ClassVar default
    ("extended") without touching the individual tool source files — the pinned
    set lives in one place (`working_set.yaml::core`) instead of 45 scattered
    class-body edits. Names in `_CONDITIONAL_CORE_TOOL_NAMES` are exempt: they
    legitimately don't register in adapter-less contexts.

    Raises if a listed name isn't registered, and strictly: the person writing
    it is an operator with an editor, and the failure a typo produces is
    silent — that tool stays extended and simply costs a search round trip
    forever. So the message names the file, the key and the name, because the
    reader cannot grep for a constant.
    """
    core = core_tool_names()
    registered = set(registry.names())

    # Custom promotions come from their own file and are checked leniently:
    # a name there is a file the operator may delete at any moment, and a
    # promoted tool they removed must not stop the app from starting. The
    # shipped list below keeps its strict check, because a typo in OUR file is
    # our bug and silently costs a search round trip forever.
    from tesseract.kernel.home_tools import promoted_names, promoted_path

    promoted = promoted_names()
    custom_registered = {
        name
        for name in registered
        if getattr(registry.tools[name], "origin", "shipped") == "custom"
    }
    stale = sorted(promoted - custom_registered)
    if stale:
        logger.warning(
            "home_tools: %s promotes %s, which %s not a loaded custom tool — "
            "ignored",
            promoted_path(),
            ", ".join(stale),
            "is" if len(stale) == 1 else "are",
        )
    # The strict check stays on the RAW yaml set. A name in our file that no
    # tool answers to is our bug and stops the boot; pre-filtering it to what
    # is registered would make that check silently unreachable.
    missing = sorted(core - registered - _CONDITIONAL_CORE_TOOL_NAMES)
    if missing:
        from tesseract.config.working_set import CORE_KEY, config_path

        raise RuntimeError(
            f"{config_path()}::{CORE_KEY} names {len(missing)} tool(s) that do "
            f"not exist: {', '.join(missing)}. Settings -> Tools lists every "
            "real name. Remove or correct these lines; until then the app will "
            "not start."
        )
    # Both directions, not just promotion. At boot every instance starts at the
    # ClassVar default so promoting was enough; the panel re-applies this after
    # a save, and a tool the operator has just REMOVED would otherwise keep the
    # tier it was given the last time round and go on riding every turn. A
    # control that silently does half of what it says is worse than no control.
    # The tier itself comes from the same helper the panel reads, so a custom
    # name hand-added to `working_set.yaml` cannot make boot and a later
    # `tool_search` disagree about that tool's tier.
    from tesseract.kernel.home_tools import effective_core_names

    carried = effective_core_names(registry)
    for name in registered:
        registry.tools[name].tier = "core" if name in carried else "extended"


def _custom_postures(policy: "PermissionPolicy | None") -> dict[str, str]:
    """The operator's own answer for each custom tool, or nothing.

    Read off `permissions.yaml::custom`, which the policy has already loaded
    and validated. It used to be its own file beside the tools; that file was
    ASK to the assistant while this one is DENY, so the record of what a
    self-written tool may do was writable by the thing it governs.

    An empty map leaves every custom tool on the ASK floor, which is the safe
    direction and the answer this had before any record existed.
    """
    return dict(getattr(policy, "custom_defaults", None) or {})


def _wire_tool_defaults(
    registry: ToolRegistry,
    policy: "PermissionPolicy | None",
) -> None:
    """Collect each registered tool's `default_posture`, attach them to the
    policy, and log drift against the yaml-declared `tools:` block.

    Errors:
      * Tool subclass missing a valid posture → RuntimeError. Forces the
        author to declare it at the class level.
    Warnings (logger only — never blocks boot):
      * Yaml lists a tool that isn't registered → orphan entry; log so
        the operator can prune it.
      * Yaml entry diverges from the class default → diverge log so the
        operator knows their override is intentional vs accidental.
      * Registered tool has no yaml entry → falls back to class default
        (the new contract); info-level log only.
    """
    class_defaults: dict[str, str] = {}
    # Custom tools are held out of every yaml comparison below. Their names are
    # machine-local, and `permissions.yaml` is a file the operator owns and dev
    # ships: autosync writing `my_tool: auto` into it would both persist a
    # posture the file granted itself and put one machine's tool name in a
    # tracked config. An operator raising it by hand or in Settings is a
    # different act and still works.
    custom_names: set[str] = set()
    # Once, not once per tool. The file is the same for every name in the loop
    # below, and reading it inside the loop is the shape that was already
    # measured and fixed on the shipped-roster read.
    chosen_postures = _custom_postures(policy)
    valid_risks = VALID_RISK_CLASSES
    for tool in registry.tools.values():
        posture = getattr(type(tool), "default_posture", "")
        if posture not in ("auto", "ask", "deny"):
            raise RuntimeError(
                f"tool '{tool.name}' (class {type(tool).__name__}) declares "
                f"default_posture={posture!r}; expected one of "
                f"('auto','ask','deny'). Set it at the class level so the "
                f"runtime has a single source of truth."
            )
        # Every concrete Tool subclass MUST declare risk_class.
        # The AgendaStore compares this against agenda-item class
        # at admission; an unknown/missing class would silently fall to
        # the default constructor "" and admission would refuse — but
        # better to fail loud at boot than to land a tool that can never
        # be dispatched. See `_shared/risk-class-taxonomy.md`.
        risk = getattr(type(tool), "risk_class", "")
        if risk not in valid_risks:
            raise RuntimeError(
                f"tool '{tool.name}' (class {type(tool).__name__}) declares "
                f"risk_class={risk!r}; expected one of {sorted(valid_risks)}. "
                "Set it at the class level per "
                "the risk-class taxonomy."
            )
        # Lean-agent-os P1 Task 2 — every tool's `tier` (class default or
        # `_apply_tool_tiers` instance override) must resolve to a known
        # value; anything else means the tiering filter in
        # `ToolRegistry.schemas_for_adapter` would silently drop it from
        # every session's schema payload.
        tier = getattr(tool, "tier", "")
        if tier not in ("core", "extended"):
            raise RuntimeError(
                f"tool '{tool.name}' (class {type(tool).__name__}) resolves "
                f"tier={tier!r}; expected one of ('core','extended')."
            )
        # The four contract fields, checked where the postures are. Lives in
        # `kernel/tools/base.py` beside the fields it validates, so a tool
        # author reads the rule in the file that declares it.
        check_tool_contract(tool)
        # A custom tool's declared posture is a claim the operator's own file
        # makes about itself, so it is read for the contract check above and
        # then discarded. `auto` beside `risk_class: autonomous` would
        # otherwise be a file in `<home>/tools/` granting itself unattended
        # execution, and `attach_class_defaults` below is a full replace, so
        # merging ASK anywhere earlier does not survive this loop.
        #
        # **The OPERATOR's answer is a different claim and is not discarded.**
        # It lives in `permissions.yaml::custom`, which Settings and the
        # operator write and which no tool call may, and ASK is the floor for
        # a tool with no entry there. This loop is where that answer
        # survives a restart: `attach_class_defaults` below is a full replace,
        # so a posture merged anywhere earlier does not outlive it, and the
        # control that sets one appeared to work and reverted on the next
        # launch until this read existed.
        if getattr(tool, "origin", "shipped") == "custom":
            class_defaults[tool.name] = chosen_postures.get(tool.name, "ask")
            custom_names.add(tool.name)
            continue
        class_defaults[tool.name] = posture

    # The same question of the agent cards, which are the other thing the
    # runtime dispatches by name. A shipped card naming a role that is not in
    # roles.yaml resolves to an empty chain and does nothing — `vision.md` was
    # in that state, reported by one WARNING nobody reads.
    check_shipped_cards()

    # And of the playbooks, which are the third thing dispatched by name: a
    # step naming a tool that is not registered here is a procedure that
    # cannot run. Reported, never raised, because every skill is the
    # operator's (`playbook_contract` says why).
    check_playbooks(workspace_dir() / "skills", tool_names=frozenset(registry.names()))

    if policy is None:
        # REPL / unit tests sometimes call build_tool_registry without a
        # policy. Skip drift logging — the policy gets wired later or
        # never (in test contexts that don't care about postures).
        return

    policy.attach_class_defaults(class_defaults)
    # And hold those same names out of a security mode's blanket baseline,
    # here rather than only in `home_tools.sync_home_tools`. Boot calls that
    # function BEFORE the policy is stashed two lines below, so its own
    # exempt call has no policy to reach and never runs — the ASK ceiling set
    # in the loop above was then lifted again by `modes.<mode>.baseline`, and
    # a tool the assistant wrote ran unattended under a relaxed mode. The
    # ceiling and the exemption are one decision and are made in one place.
    if custom_names:
        policy.exempt_from_baseline(custom_names)

    # Stash the live policy where a consumer holding only the registry can
    # reach it, the same way `vault_librarian` is stashed below. `tool_search`
    # rescans `<home>/tools/` and needs somewhere to merge a newly-loaded
    # tool's declared posture; its `ToolContext` carries no policy.
    registry.permission_policy = policy  # type: ignore[attr-defined]

    yaml_defaults = dict(policy.tools_defaults)
    registered = set(class_defaults) - custom_names
    yaml_listed = set(yaml_defaults)

    # Conditionally-registered tools (adapter-gated, or late-registering
    # like transcribe_audio) are NOT orphans — flagging them told the
    # operator to prune LIVE tools' postures on every keyless/booting
    # install (2026-07-30).
    # `custom_names` is subtracted here as well as from `registered`. Raising a
    # custom tool above its forced ASK means adding `my_tool: auto` under
    # `tools:`, which is the one supported way to do it. Without this term that
    # name is in `yaml_listed` and not in `registered`, so every boot logged an
    # ERROR telling the operator to prune the override they had just made.
    orphans = sorted(
        yaml_listed - registered - custom_names - _CONDITIONAL_CORE_TOOL_NAMES
    )
    conditional_absent = sorted(
        (yaml_listed - registered) & _CONDITIONAL_CORE_TOOL_NAMES
    )
    if conditional_absent:
        logger.info(
            "permissions.yaml::tools — %d tool(s) not registered in this "
            "configuration (adapter/engine-gated, posture kept): %s",
            len(conditional_absent), ", ".join(conditional_absent),
        )
    if orphans:
        # ERROR-level so the pulse panel surfaces it. Orphans mean a tool
        # was deleted but yaml still references it — operator should prune.
        # We never auto-remove orphans; that's their call.
        logger.error(
            "permissions.yaml::tools lists %d unregistered tool(s) — prune them: %s",
            len(orphans), ", ".join(orphans),
        )

    diverged = []
    for name in sorted(registered & yaml_listed):
        if yaml_defaults[name] != class_defaults[name]:
            diverged.append(f"{name}(yaml={yaml_defaults[name]},class={class_defaults[name]})")
    if diverged:
        logger.info(
            "permissions.yaml::tools overrides %d class default(s): %s",
            len(diverged), ", ".join(diverged),
        )

    using_class_default = sorted(registered - yaml_listed)
    if using_class_default:
        # Drift detected — surface in the pulse panel so the operator sees
        # that yaml is being repaired at boot. Then auto-write the missing
        # entries via round_trip_yaml (preserves comments + key order).
        # Operator overrides already in yaml are never touched. Settings
        # view re-reads yaml on its next request and renders the new state.
        # Disable auto-write with TESSERACT_NO_AUTOSYNC=1.
        logger.error(
            "permissions.yaml drift: %d tool(s) registered but absent from "
            "tools: %s", len(using_class_default), ", ".join(using_class_default),
        )
        if os.getenv("TESSERACT_NO_AUTOSYNC") == "1":
            logger.error(
                "TESSERACT_NO_AUTOSYNC=1 — leaving yaml unmodified; class defaults "
                "still serve at runtime",
            )
        else:
            try:
                from tesseract.lib.yaml_io import round_trip_yaml

                def _add_missing(doc):  # noqa: ANN001
                    tools = doc.setdefault("tools", {})
                    for name in using_class_default:
                        if name not in tools:
                            tools[name] = class_defaults[name]

                round_trip_yaml(PERMISSIONS_YAML, _add_missing)
                logger.error(
                    "permissions.yaml auto-synced %d new entry/entries: %s",
                    len(using_class_default), ", ".join(
                        f"{n}={class_defaults[n]}" for n in using_class_default
                    ),
                )
                # Refresh the live policy so the just-written entries are
                # visible without a hot-reload round-trip.
                policy.tools_defaults.update(
                    {name: class_defaults[name] for name in using_class_default}
                )
            except Exception as exc:
                logger.error(
                    "permissions.yaml auto-sync FAILED (%s) — class defaults "
                    "still active in-memory; %d tool(s) absent from yaml: %s",
                    exc, len(using_class_default), ", ".join(using_class_default),
                )
