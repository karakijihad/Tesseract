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
import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import yaml

if TYPE_CHECKING:
    from tesseract.permissions.policy import PermissionPolicy

from tesseract.agents.loader import load_agent
from tesseract.brain.cost import CostLedger
from tesseract.brain.observer import Observer, build_observer_from_config
from tesseract.brain.tools import ToolRegistry
from tesseract.config.loader import (
    PROVIDERS_YAML,
    ROLES_YAML,
    ConfigBundle,
    ConfigError,
    ResolvedRef,
    RoleConfig,
    load_config,
)
from tesseract.kernel.adapters.anthropic import AnthropicAdapter
from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.kernel.adapters.gemini import GeminiAdapter
from tesseract.kernel.adapters.openai import OpenAIAdapter
from tesseract.kernel.tools.agent_create import AgentCreateTool
from tesseract.kernel.tools.agent_promote import AgentPromoteTool
from tesseract.kernel.tools.skill_create import SkillCreateTool
from tesseract.kernel.tools.skill_promote import SkillPromoteTool
from tesseract.kernel.tools.skill_refine import SkillRefineTool
from tesseract.kernel.tools.bash_tool import BashTool
from tesseract.kernel.tools.conscience import ConscienceStatusTool
from tesseract.kernel.tools.context7 import Context7LookupTool
from tesseract.kernel.tools.delegate_claude import DelegateClaudeTool
from tesseract.kernel.tools.delegate_codex import DelegateCodexTool
from tesseract.kernel.tools.delegate_codex_exec import DelegateCodexExecTool
from tesseract.kernel.tools.delegate_tars_controller import (
    DelegateTarsControllerTool,
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
from tesseract.kernel.tools.glob_tool import GlobTool
from tesseract.kernel.tools.grep_tool import GrepTool
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
from tesseract.kernel.tools.brief_read import BriefReadTool
from tesseract.kernel.tools.brief_render import BriefRenderTool
from tesseract.kernel.tools.ask_clarification import AskClarificationTool
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
from tesseract.kernel.tools.doodle_open import DoodleOpenTool
from tesseract.kernel.tools.lane_read import LaneReadTool
from tesseract.kernel.tools.surface_bind_session import SurfaceBindSessionTool
from tesseract.kernel.tools.surface_close import SurfaceCloseTool
from tesseract.kernel.tools.surface_create import SurfaceCreateTool
from tesseract.kernel.tools.surface_focus import SurfaceFocusTool
from tesseract.kernel.tools.surface_highlight import SurfaceHighlightTool
from tesseract.kernel.tools.surface_list import SurfaceListTool
from tesseract.kernel.tools.surface_update import SurfaceUpdateTool
from tesseract.kernel.tools.lane_send import LaneSendTool
from tesseract.kernel.tools.lane_turn import LaneTurnTool
from tesseract.kernel.tools.work_send import WorkSendTool
from tesseract.kernel.tools.lane_status import LaneStatusTool
from tesseract.kernel.tools.set_mood import SetMoodTool
from tesseract.kernel.tools.set_state import EntityAffect, SetStateTool
from tesseract.kernel.tools.set_voice import SetVoiceTool, VoiceState
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

from tesseract.paths import CONFIG_DIR, ROOT, TESSERACT_HOME, home_dir, workspace_dir
from tesseract.paths import agents_dir as _home_agents_dir

ENV_PATH = TESSERACT_HOME / ".env"
PERMISSIONS_YAML = CONFIG_DIR / "permissions.yaml"
VAULT_YAML = CONFIG_DIR / "vault.yaml"
SESSIONS_DIR = TESSERACT_HOME / "sessions"

# Lean-agent-os P1 Task 2 — tool-schema tiering. Every registered tool
# defaults to `Tool.tier == "extended"` (schema hidden from the chat
# model until `tool_search` surfaces it); the names below are marked
# `tier = "core"` at the end of `build_tool_registry` so their schemas
# are always in the per-turn payload. ~45 of ~125 registered tools —
# "~40" per `Docs/Plan/lean-agent-os/phase-1-unmuzzle.md` Task 2, sized
# up slightly to keep each named category (memory/delegate/lane/file/
# web/schedule/alarm/vault/surface) usable without a search round trip.
# Pin by literal registered name (`Tool.name`, not class name) — verify
# with `registry.names()` before adding an entry, a typo here is a
# silent no-op (see the RuntimeError guard in `_wire_tool_defaults`).
_CORE_TOOL_NAMES: frozenset[str] = frozenset({
    # memory
    "memory_save", "memory_get", "memory_search",
    "memory_update", "memory_promote", "memory_forget",
    # delegate
    "delegate_claude", "delegate_codex", "delegate_codex_exec",
    "delegate_tars_controller",
    # lane
    "lane_open", "lane_send", "lane_turn", "lane_read", "lane_status",
    "lane_attach", "lane_close", "lane_list", "lane_named_ensure",
    # controller / interactive session
    "start_controller_session", "controller_session_list",
    "session_open", "session_send", "session_result",
    "session_close", "session_list",
    # file
    "file_read", "file_write", "glob", "grep",
    # shell
    "bash",
    # web + docs
    "web_search", "tavily_search", "tavily_extract",
    "context7_lookup", "pdf_read",
    # schedule
    "schedule_create", "schedule_list", "schedule_update", "schedule_remove",
    # alarm
    "alarm_set", "alarm_cancel",
    # channel
    "channel_notify",
    # surface — create/update/list/close are the coherent-loop minimum:
    # spawn, mutate, see what exists, clean up. (focus/highlight/bind stay
    # extended — reachable via tool_search when actually needed.)
    "surface_create", "surface_update", "surface_list", "surface_close",
    # browser — TARS's own headless-Playwright eyes. The read-only observe
    # verbs are core so "look at what I just rendered" is a reflex, not a
    # tool_search away. (click/fill/close stay extended.)
    "browser_navigate", "browser_snapshot", "browser_screenshot",
    # vault
    "vault_query", "vault_search",
    # 2026-07-12 operator directive ("give TARS all the access"): every tool
    # an always-inlined rule card instructs is core — the prompt must never
    # name a tool the model can't see. The long tail (channel_send_*, vault
    # admin, browser extras) stays extended behind tool_search.
    # spawns + delegation follow-through (05-parallel-delegation.md)
    "spawn_check", "spawn_await", "spawn_cancel", "work_send",
    "invoke_agent",
    # workspace threads (07-workspace-thread-isolation.md)
    "workspace_post", "workspace_reply", "ask_clarification",
    # agenda comment auto-reply (Option-B durability, mirrors workspace_reply)
    "agenda_comment",
    # identity / state (13-state.md) + reflection write-backs
    "set_mood", "set_state", "set_voice",
    "diary_append", "soul_growth_propose",
    # tasks (04-tasks.md)
    "tasks_set", "tasks_update",
    # recall + proactive surfaces
    "recall_history", "chat_initiate", "image_generate",
    "alarm_list", "alarm_snooze",
    "lane_named_get", "lane_named_list",
    # meta
    "tool_search",
})

# Core-pinned tools that only register when a chat adapter resolves (the
# `invoke_adapter is not None and chat_cfg is not None` guard in
# `build_tool_registry`). Their absence is legitimate in adapter-less
# contexts — capability-matrix script, credential-less CI, minimal test
# boots — so `_apply_tool_tiers` tolerates it instead of raising. Every
# other `_CORE_TOOL_NAMES` entry still hard-fails when missing (typo net).
_CONDITIONAL_CORE_TOOL_NAMES: frozenset[str] = frozenset({
    "session_open",
    "invoke_agent",  # same adapter guard as session_open
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
    temperature: float
    max_output_tokens: int
    context_window: int
    reasoning_effort: str
    knowledge_cutoff: str
    use_responses_api: bool
    compact_threshold: float
    keep_recent_turns: int
    # CR-0 (2026-05-22) sliding-window knobs. See
    # tesseract/brain/chat.py module docstring + Docs/Plan/context-recall/.
    head_anchor_messages: int
    active_window_tokens: int | None
    summary_char_budget: int
    provider_cfg: dict
    ref: ResolvedRef
    tool_iteration_cap: int
    consecutive_error_cap: int


def _require(d: dict, key: str, where: str):
    """Fetch `key` from `d`; raise RuntimeError naming the location on miss."""
    if key not in d:
        raise RuntimeError(f"missing required key '{key}' in {where}")
    return d[key]


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


def _chat_brain_from_ref(
    ref: ResolvedRef, role_overrides: dict, where: str,
) -> ChatBrainConfig:
    """Build a ChatBrainConfig from a resolved ref, layering role overrides.

    Required model fields (temperature, max_output_tokens, context_window,
    reasoning_effort, knowledge_cutoff, use_responses_api) come from the
    catalog. Role-level *_override keys (e.g. ``reasoning_effort_override``,
    ``max_output_tokens_override``) replace the catalog default. Compact
    knobs (``compact_threshold``, ``keep_recent_turns``) live only on the
    role.
    """
    fields = ref.model.fields
    eff_reasoning = role_overrides.get(
        "reasoning_effort_override",
        fields.get("reasoning_effort", "none"),
    )
    eff_max_out = int(role_overrides.get(
        "max_output_tokens_override",
        _require(fields, "max_output_tokens", f"{where} {ref.ref}"),
    ))
    return ChatBrainConfig(
        provider=ref.connection.name,
        model=ref.model.model,
        tier=ref.connection.tier,
        temperature=float(_require(fields, "temperature", f"{where} {ref.ref}")),
        max_output_tokens=eff_max_out,
        context_window=int(_require(fields, "context_window", f"{where} {ref.ref}")),
        reasoning_effort=str(eff_reasoning),
        knowledge_cutoff=str(fields.get("knowledge_cutoff", "")),
        use_responses_api=bool(fields.get("use_responses_api", False)),
        compact_threshold=float(role_overrides.get("compact_threshold", 0.5)),
        keep_recent_turns=int(role_overrides.get("keep_recent_turns", 10)),
        head_anchor_messages=int(role_overrides.get("head_anchor_messages", 3)),
        active_window_tokens=(
            int(role_overrides["active_window_tokens"])
            if "active_window_tokens" in role_overrides
            and role_overrides["active_window_tokens"] is not None
            else None
        ),
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
    keys are required — missing keys raise loudly per CLAUDE.md
    §"No hardcoded model IDs, URLs, timeouts, or get(..., 'default')
    patterns for infrastructure values anywhere in Python".
    """

    transient_retries: int
    transient_backoff_ms: int
    cooldown_max_failures: int
    cooldown_seconds: float


def load_chain_config(bundle: ConfigBundle | None = None) -> ChainConfig:
    """Read the `chain:` block from providers.yaml — required."""
    bundle = bundle or load_bundle()
    chain_raw = _require(dict(bundle.providers_raw), "chain", "providers.yaml")
    return ChainConfig(
        transient_retries=int(_require(chain_raw, "transient_retries", "providers.yaml chain")),
        transient_backoff_ms=int(_require(chain_raw, "transient_backoff_ms", "providers.yaml chain")),
        cooldown_max_failures=int(_require(chain_raw, "cooldown_max_failures", "providers.yaml chain")),
        cooldown_seconds=float(_require(chain_raw, "cooldown_seconds", "providers.yaml chain")),
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
    )


def load_chat_brain_config() -> ChatBrainConfig:
    """Return the typed primary chat_brain config (the role's `primary` ref)."""
    return load_chat_brain_chain()[0]


def load_chat_brain_chain() -> list[ChatBrainConfig]:
    """Return the full ordered chat_brain fallback chain.

    Walks ``roles.chat_brain.primary`` then each of ``roles.chat_brain.fallbacks``,
    parses each into a typed ChatBrainConfig, and returns the list. Raises
    on missing role / missing required field — config errors fail loudly.
    Runtime availability (API keys, live endpoints) is a later concern.
    """
    bundle = load_bundle()
    try:
        role = bundle.role("chat_brain")
    except ConfigError as exc:
        raise RuntimeError(str(exc)) from exc
    overrides = dict(role.overrides)
    refs: list[ResolvedRef] = [role.primary, *role.fallbacks]
    return [
        _chat_brain_from_ref(ref, overrides, "roles.yaml roles.chat_brain")
        for ref in refs
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

    Codex audit-2 2026-05-19 P2: ``agent-writer.md`` documents ``model_role``
    as accepting either a role name OR a direct provider ref, but
    ``invoke_agent._resolve_sub_adapter`` previously fell through provider
    refs to the parent adapter. This helper closes the contract gap.

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
        cfg = _chat_brain_from_ref(resolved_ref, overrides, f"agent.model_role={ref}")
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
    # are required keys on roles.yaml::chat_brain per CLAUDE.md hard rule.
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
                ref, merged_overrides, f"roles.yaml roles.{role_name}",
            )
        except (ConfigError, ValueError, KeyError) as exc:
            logger.info(
                "resolve_role_runtime: role=%r idx=%d ref=%s unbuildable: %s",
                role_name, idx, ref.ref, exc,
            )
            continue
        try:
            adapter = build_adapter(ref)
        except RuntimeError as exc:
            logger.info(
                "resolve_role_runtime: role=%r idx=%d (%s/%s) unavailable: %s",
                role_name, idx, cfg.provider, cfg.model, exc,
            )
            continue
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
            f"TARS can't answer yet — no API key is set for any configured "
            f"chat provider. Add one to {env_path}, then restart TARS."
        )
    if disabled and not no_key:
        return (
            "TARS can't answer yet — every configured chat provider is "
            "switched off in providers.yaml. Enable one, then restart TARS."
        )
    return (
        f"TARS can't answer yet — no chat provider is available (no API "
        f"key set, or a provider switched off in providers.yaml). Add a "
        f"key to {env_path}, then restart TARS."
    )


def resolve_chat_brain_runtime() -> tuple[
    ChatBrainConfig,
    ModelAdapter,
    AdapterOptions,
    list[tuple[ModelAdapter, AdapterOptions]],
]:
    """Return the first available chat_brain entry plus the full live chain.

    `load_chat_brain_chain()` validates config shape; adapter construction is
    a separate concern. The runtime should use the first entry whose adapter
    can actually be built, not blindly pin itself to `resolution[0]`.

    Nothing in the chain is a hard requirement (no API key/provider is
    mandatory) — if every candidate fails, raises `ChatBrainUnavailable`
    carrying both the full technical breakdown (for the log) and a short
    plain-language `summary` (for the caller — `_build_chat_infra` — to hand
    to a degraded placeholder adapter instead of leaving chat silently dead).
    """
    chain_cfgs = load_chat_brain_chain()
    built_chain: list[tuple[ChatBrainConfig, ModelAdapter, AdapterOptions]] = []
    failures: list[str] = []
    for idx, cfg in enumerate(chain_cfgs):
        try:
            adapter = build_chat_brain_adapter(cfg)
        except RuntimeError as exc:
            logger.info(
                "chat_brain chain idx=%d (%s/%s) unavailable: %s",
                idx, cfg.provider, cfg.model, exc,
            )
            failures.append(f"{cfg.provider} ({cfg.model}): {exc}")
            continue
        options = adapter_options_from_chat_brain(cfg)
        built_chain.append((cfg, adapter, options))
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
        extra=extra,
    )


@dataclass(frozen=True)
class VaultConfig:
    """Typed view of `tesseract/config/vault.yaml`.

    Every field is required. `load_vault_config()` raises if any key is
    missing — no silent fallbacks (CLAUDE.md §Hard Rules).
    """
    max_extract_chars: int
    scale_split_threshold: int
    stale_grace_days: int
    contradiction_pair_limit: int
    max_seed_slugs: int
    max_expanded_slugs: int
    search_rrf_k: int
    search_default_top_k: int


def load_vault_config() -> VaultConfig:
    """Read vault.yaml; return a typed VaultConfig or raise on missing keys."""
    raw = yaml.safe_load(VAULT_YAML.read_text(encoding="utf-8"))
    ingest = _require(raw, "ingest", "vault.yaml")
    lint = _require(raw, "lint", "vault.yaml")
    query = _require(raw, "query", "vault.yaml")
    search = _require(raw, "search", "vault.yaml")
    return VaultConfig(
        max_extract_chars=int(_require(ingest, "max_extract_chars", "vault.yaml ingest")),
        scale_split_threshold=int(_require(lint, "scale_split_threshold", "vault.yaml lint")),
        stale_grace_days=int(_require(lint, "stale_grace_days", "vault.yaml lint")),
        contradiction_pair_limit=int(_require(lint, "contradiction_pair_limit", "vault.yaml lint")),
        max_seed_slugs=int(_require(query, "max_seed_slugs", "vault.yaml query")),
        max_expanded_slugs=int(_require(query, "max_expanded_slugs", "vault.yaml query")),
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
        "base_url": conn.base_url or "http://localhost:11434",
        "model": ref.model.model,
        "dimensions": int(ref.model.fields.get("dimensions", 768)),
        "timeout_seconds": int(ref.model.fields.get("timeout_seconds", conn.timeout_seconds)),
        "max_retries": int(ref.model.fields.get("max_retries", conn.max_retries)),
        "host": str(conn.extra.get("host", "this_pc")),
        "auto_start_ollama": bool(conn.extra.get("auto_start", False)),
    }
    return cfg


# ── Adapter + observer builders ──────────────────────────

def build_adapter(ref: ResolvedRef) -> ModelAdapter:
    """Construct a ModelAdapter from a resolved provider/model reference.

    Dispatch key is ``ref.connection.adapter`` (declared in providers.yaml).
    Each branch reads exactly the connection fields it needs and raises on
    missing API keys — no silent fallbacks. Disabled tier or provider
    (``providers.yaml`` ``enabled: false``) raises before any network work
    so the fallback chain skips this entry the same way it skips a missing
    API key.
    """
    conn = ref.connection
    if not conn.tier_enabled:
        raise RuntimeError(
            f"tier '{conn.tier}' is disabled in providers.yaml ({conn.tier}.enabled=false)"
        )
    if not conn.enabled:
        raise RuntimeError(
            f"provider '{conn.tier}.{conn.name}' is disabled in providers.yaml "
            f"({conn.tier}.{conn.name}.enabled=false)"
        )
    adapter = conn.adapter
    if adapter == "gemini":
        api_key = os.environ.get(conn.api_key_env or "GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError(f"{conn.api_key_env or 'GOOGLE_API_KEY'} missing from .env")
        return GeminiAdapter(
            api_key=api_key,
            timeout=conn.timeout_seconds,
            max_retries=conn.max_retries,
        )
    if adapter == "openai":
        api_key = os.environ.get(conn.api_key_env or "OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(f"{conn.api_key_env or 'OPENAI_API_KEY'} missing from .env")
        return OpenAIAdapter(
            api_key=api_key,
            base_url=conn.base_url or "https://api.openai.com/v1",
            timeout=conn.timeout_seconds,
            max_retries=conn.max_retries,
            supports_prompt_cache_key=conn.supports_prompt_cache_key,
            supports_stream_usage=conn.supports_stream_usage,
            cache_routing_header=conn.cache_routing_header,
        )
    if adapter == "anthropic":
        api_key = os.environ.get(conn.api_key_env or "ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(f"{conn.api_key_env or 'ANTHROPIC_API_KEY'} missing from .env")
        return AnthropicAdapter(
            api_key=api_key,
            timeout=conn.timeout_seconds,
            max_retries=conn.max_retries,
            base_url=conn.base_url,
            health_check_model=ref.model.model,
        )
    if adapter == "cli":
        # Subprocess-backed adapter — uses the operator's CLI subscription
        # auth (no API key). `command` selects the binary; today only
        # `codex` is fully wired (parses `--json` event stream). `claude`
        # falls back to plain-text mode.
        if not conn.command:
            raise RuntimeError(
                f"cli provider '{conn.tier}.{conn.name}' missing 'command' in providers.yaml"
            )
        # Mirror the API-key gate: if the binary isn't on PATH the chain
        # should skip this entry instead of returning a dead adapter that
        # fails at first stream() call. `.cmd` shim covers Windows. Same
        # pair check lives in `CLIAdapter.check_available` for runtime
        # health probes — keep both in sync if either lookup rule changes.
        if shutil.which(conn.command) is None and shutil.which(f"{conn.command}.cmd") is None:
            raise RuntimeError(
                f"cli binary '{conn.command}' not on PATH "
                f"(provider '{conn.tier}.{conn.name}')"
            )
        from tesseract.kernel.adapters.cli import CLIAdapter
        return CLIAdapter(
            command=conn.command,
            model_id=ref.model.model,
            timeout=conn.timeout_seconds,
            stream_json=conn.stream_json_capable,
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
        entry = {
            "provider": ref.connection.name,
            "tier": ref.connection.tier,
            "model": ref.model.model,
            "use_responses_api": bool(ref.model.fields.get("use_responses_api", False)),
            "context_window": ref.model.fields.get("context_window"),
            "max_output_tokens": int(overrides.get(
                "max_output_tokens_override",
                ref.model.fields.get("max_output_tokens", 2048),
            )),
            "temperature": ref.model.fields.get("temperature", 0.7),
            "reasoning_effort": str(overrides.get(
                "reasoning_effort_override",
                ref.model.fields.get("reasoning_effort", "low"),
            )),
        }
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

def ollama_up(base_url: str, timeout: float = 2.0) -> bool:
    try:
        r = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=timeout)
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


def build_memory_bundle(
    adapter: ModelAdapter | None = None,
    adapter_options: AdapterOptions | None = None,
) -> MemoryBundle:
    """Store + index + retrieval pipeline are always live (filesystem only).
    Embeddings come online only when the configured Ollama endpoint is
    reachable; the pipeline degrades cleanly to BM25-only retrieval when
    embeddings are absent (audit M2 fix, 2026-04-29).

    `adapter` / `adapter_options` flow into `Librarian` so its M2 prefix-
    classifier fallback has something to call. Pass `None` in environments
    without an LLM — unclassifiable sections are then skipped, not written.

    Never returns None — `memory_search` is always available; vector
    search is the only branch gated on embeddings.
    """
    store_dir = TESSERACT_HOME / "memory-store"
    derived_dir = store_dir / "derived"
    derived_dir.mkdir(parents=True, exist_ok=True)

    # AU-16 — seed the Obsidian color-group config so the operator can
    # open `memory-store/` (and `vault/`) directly in Obsidian and see
    # the unified palette on first launch. Idempotent — operator edits
    # to `.obsidian/graph.json` survive future boots.
    try:
        from tesseract.memory.obsidian_config import ensure_obsidian_config

        ensure_obsidian_config(store_dir)
        ensure_obsidian_config(TESSERACT_HOME / "vault")
    except Exception:
        logger.exception("obsidian_config: seeding failed (non-fatal)")

    store = MemoryStore(store_dir=store_dir)
    index = MemoryIndex(store_dir=store_dir)
    fts_index = FTSIndex(db_path=derived_dir / "fts.db")
    recall_log_path = derived_dir / "recall.jsonl"

    embed_cfg = load_embeddings_cfg()
    embeddings: EmbeddingIndex | None = None

    if embed_cfg and embed_cfg.get("provider") == "ollama" and ollama_up(embed_cfg["base_url"]):
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
    # CR-1 M3 — wire the work-history index so per-turn memory_search can
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

    pipeline = RetrievalPipeline(
        store=store,
        index=index,
        embeddings=embeddings,
        fts_index=fts_index,
        recall_log_path=recall_log_path,
        work_index=work_index,
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
    """CR-1 recall_history — read-only retrieval over session transcripts +
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
    — `memory_search` runs BM25-only when Ollama is offline (audit M2 fix,
    2026-04-29). Idempotent — safe to call from /refresh after ollama
    starts mid-session. `adapter` flows to the librarian for M2's
    missing-prefix classifier path.
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
          "default_voice_id": "Charon",
          "default_tone_prompt": "...",
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
    / ``gemini`` / ``piper``) to pick the right config object.
    Returns an empty dict when no ``voice:`` block exists.
    """
    bundle = load_bundle()
    if bundle.voice is None:
        return {}

    voice = bundle.voice
    out: dict = {
        "default_voice_id": voice.default_voice_id,
        "default_tone_prompt": voice.default_tone_prompt,
    }

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
    """Phase 18 — re-resolve chat_brain + observer + voice runtime from a
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

    Phase 18 audit M2 — the dict held by `app["config"].models` is also
    refreshed here. `ServerConfig` is a frozen dataclass, but its `models`
    field is a plain dict; mutating in place keeps every REST surface
    that reads `request.app["config"].models` (e.g. `/api/identity`,
    `/api/voice/providers`) consistent with the on-disk YAML.
    """
    summary: dict[str, Any] = {}

    # Phase 18 audit M2 — refresh the REST-facing config snapshot so
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
                chat_session = getattr(srv_session, "chat_session", None)
                if chat_session is None:
                    continue
                chat_session.adapter = live_adapter
                chat_session.options = options
                chat_session.compact_threshold = chat_cfg.compact_threshold
                chat_session.keep_recent_turns = chat_cfg.keep_recent_turns
                chat_session.head_anchor_messages = chat_cfg.head_anchor_messages
                chat_session.active_window_tokens = chat_cfg.active_window_tokens
                chat_session.summary_char_budget = chat_cfg.summary_char_budget
                chat_session.max_tool_iterations = chat_cfg.tool_iteration_cap
                chat_session.max_consecutive_adapter_errors = chat_cfg.consecutive_error_cap
                if "system_prompt" in app:
                    chat_session.system_prompt = app["system_prompt"]
                swapped += 1
            if swapped:
                summary["live_sessions_swapped"] = swapped
        except Exception:
            logger.exception("rebuild_adapters: live session swap failed")

    # Observer
    cost_ledger = app.get("cost_ledger")
    try:
        observer = build_observer(cost_ledger=cost_ledger)
    except Exception:
        logger.exception("rebuild_adapters: observer rebuild failed")
        observer = app.get("observer")  # keep old handle on failure
    if observer is not app.get("observer"):
        # Detach the old subscriber before swapping so the new observer
        # can be attached on the next /observe arm. Failure to detach is
        # not fatal — we log and continue.
        old_sub = app.get("observer_subscriber")
        if old_sub is not None:
            try:
                # ObserverSubscriber.detach is async — we cannot await
                # here without making this whole helper async. Schedule
                # the detach if a loop is running; otherwise drop.
                import asyncio

                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(old_sub.detach())
                except RuntimeError:
                    pass
            except Exception:
                logger.exception("rebuild_adapters: observer detach scheduling failed")
        app["observer"] = observer
        if observer is not None:
            from tesseract.brain.observer_subscriber import ObserverSubscriber

            app["observer_subscriber"] = ObserverSubscriber(observer)
        else:
            app["observer_subscriber"] = None
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

    # Phase 18 audit M1 — refresh the live VoiceState used by ws.py
    # `_synth_one_sentence` and `routes/voice.py::post_test`. Without
    # this, a Settings → Voice save updates roles.yaml + the engines'
    # GeminiTTSConfig but the per-utterance VoiceParams still pull
    # `voice_id` / `tone_prompt` from the boot-time VoiceState.
    voice_state = app.get("voice_state")
    if voice_state is not None:
        try:
            voice_cfg = load_voice_config()
            new_voice_id = str(voice_cfg.get("default_voice_id") or voice_state.voice_id)
            new_tone = str(voice_cfg.get("default_tone_prompt") or "").strip()
            voice_state.voice_id = new_voice_id
            voice_state.tone_prompt = new_tone
            summary["voice_state_refreshed"] = True
        except Exception:
            logger.exception("rebuild_adapters: voice_state refresh failed")

    return summary


def build_tool_registry(
    alarm_registry: AlarmRegistry | None = None,
    policy: "PermissionPolicy | None" = None,
    app: Any = None,
) -> tuple[ToolRegistry, MoodState, VoiceState, MemoryBundle, AlarmRegistry]:
    """Register every tool the runtime knows how to execute.

    Returns the registry, MoodState, VoiceState, MemoryBundle, and the
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
    """
    app_provider = (lambda: app) if app is not None else None
    registry = ToolRegistry()
    mood = MoodState()
    voice_cfg = load_voice_config()
    default_voice_id = str(voice_cfg.get("default_voice_id", "Charon"))
    default_tone_prompt = str(voice_cfg.get("default_tone_prompt") or "").strip()
    voice_state = VoiceState(voice_id=default_voice_id, tone_prompt=default_tone_prompt)
    registry.register(SetMoodTool(mood_state=mood))
    registry.register(SetVoiceTool(voice_state=voice_state))
    registry.register(SetStateTool(affect=EntityAffect()))
    # home_dir() (not TESSERACT_DIR): diary entries are operator/TARS
    # state, not code — an app update that replaces the code tree must
    # not wipe them (distributable-app Phase 1, Task 5 exit-gate finding).
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

    workspace_store = EventStore(Path(os.environ.get("TESSERACT_HOME") or TESSERACT_HOME) / "logs")
    registry.register(WorkspacePostTool(store=workspace_store, app_provider=app_provider))
    registry.register(WorkspaceReplyTool(store=workspace_store))

    from tesseract.kernel.tools.agenda_comment import AgendaCommentTool
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore

    registry.register(AgendaCommentTool(store=AgendaStore()))

    from tesseract.kernel.tools.channel_notify import ChannelNotifyTool
    from tesseract.kernel.tools.chat_initiate import ChatInitiateTool
    from tesseract.kernel.tools.orb_visibility import OrbVisibilityTool
    registry.register(ChannelNotifyTool())
    registry.register(ChatInitiateTool(app_provider=app_provider))
    registry.register(OrbVisibilityTool(app_provider=app_provider))

    if alarm_registry is None:
        # Call-time resolved (never the frozen import-time constant this used
        # to be) so a relocated TESSERACT_HOME takes effect (distributable-app
        # pre-installer blocker, Docs/Deferred.md). The one-time legacy-state
        # migration is a separate, explicit `ensure_alarms_state_migrated()`
        # call at the real entry points (mirror/supervisor/tars_controller
        # main()), not here — this function is exercised by unit tests too
        # often to carry a destructive migration side effect safely.
        alarm_registry = AlarmRegistry(state_file=alarms_state_path())
    registry.register(AlarmSetTool(alarm_registry=alarm_registry))
    registry.register(AlarmListTool(alarm_registry=alarm_registry))
    registry.register(AlarmCancelTool(alarm_registry=alarm_registry))
    registry.register(AlarmSnoozeTool(alarm_registry=alarm_registry))

    registry.register(ScheduleCreateTool())
    registry.register(ScheduleListTool())
    registry.register(ScheduleUpdateTool())
    registry.register(ScheduleRunTool())
    registry.register(ScheduleRemoveTool())
    registry.register(
        AskClarificationTool(store=workspace_store, app_provider=app_provider)
    )

    registry.register(MemoryGetTool())

    # X-4 — controller-owned lanes (claude / codex). Provider lives on the
    # ToolContext; Mirror's brain hits the "lane manager not wired" error
    # path until Session C lands the daemon IPC bridge.
    registry.register(LaneOpenTool())
    registry.register(LaneSendTool())
    registry.register(LaneTurnTool())
    registry.register(LaneReadTool())
    registry.register(LaneStatusTool())
    registry.register(LaneAttachTool())
    registry.register(LaneCloseTool())
    registry.register(LaneListTool())

    # X-5 — name→lane_id binding layer over LaneManager. tmux Agent Teams
    # pattern: TARS holds two stable persistent lanes (coder/claude +
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
    registry.register(SurfaceBindSessionTool())
    registry.register(DoodleOpenTool())

    # P4-2 — browser_* cockpit tools (headless Playwright, pc_audit sink).
    registry.register(BrowserNavigateTool())
    registry.register(BrowserSnapshotTool())
    registry.register(BrowserClickTool())
    registry.register(BrowserFillFormTool())
    registry.register(BrowserScreenshotTool())
    registry.register(BrowserNetworkRequestsTool())
    registry.register(BrowserCloseTool())

    registry.register(FileReadTool())
    registry.register(GlobTool())
    registry.register(GrepTool())
    registry.register(FileWriteTool())
    registry.register(FileCopyTool())
    registry.register(FileMoveTool())
    registry.register(PdfReadTool())
    registry.register(WebSearchTool())
    registry.register(TavilySearchTool())
    registry.register(TavilyExtractTool())

    registry.register(BashTool())

    registry.register(DelegateClaudeTool())
    registry.register(DelegateCodexTool())
    registry.register(DelegateCodexExecTool())
    # 2026-05-24 — controller-as-orchestrator dispatch. The autonomy
    # runner picks this up via `_route_for_kind(TARS_CONTROLLER)`;
    # chat-side surfaces (start_controller_session) and any future
    # caller route through the same tool so behaviour stays uniform.
    registry.register(DelegateTarsControllerTool())
    registry.register(StartControllerSessionTool())

    # Text-to-image via the configured `image_generator` role (primary set
    # in roles.yaml). The tool resolves role + model at call time and posts
    # to the genai endpoint directly — it does NOT flow through `build_adapter`,
    # so there's no chain failover here. Role-disabled / no-key paths return
    # ToolResult(is_error=True) with a clean message instead of crashing.
    registry.register(ImageGenerateTool())

    # Outbound channel media (Session 2 2026-05-16) — TARS calls these to
    # reply with audio / images / files on external channels (Telegram
    # today, future WhatsApp / Signal). Each resolves the live adapter
    # via `integrations.get_channel` and dispatches to its `send_*`
    # methods. Default posture AUTO — replying mid-conversation is the
    # expected behavior; permissions.yaml can flip to ASK per-tool.
    registry.register(ChannelSendVoiceTool())
    registry.register(ChannelSendPhotoTool())
    registry.register(ChannelSendDocumentTool())
    # Session 3 (2026-05-16) — full outbound parity. Video / animation /
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

    agents_dir = _home_agents_dir()
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

    # Phase 4 (capability-growth) — skill lifecycle, mirror of the agent
    # Stage-10 flow. skill_create files the skill_approval proposal card
    # (broadcast live via app_provider); skill_promote settles the open card
    # when promotion happens chat-side. Skills live under the workspace tree.
    skills_dir = workspace_dir() / "skills"
    registry.register(SkillCreateTool(
        skills_dir=skills_dir,
        event_store=workspace_store,
        app_provider=app_provider,
    ))
    registry.register(SkillPromoteTool(skills_dir=skills_dir, event_store=workspace_store))
    registry.register(SkillRefineTool(
        skills_dir=skills_dir,
        event_store=workspace_store,
        app_provider=app_provider,
    ))

    # Resolve the chat_brain adapter first so it can be threaded into both
    # the librarian (M2 classifier fallback) and the vault-librarian wiring
    # below. If no provider has credentials, adapter=None and the missing-
    # prefix path in Librarian._write_section skips rather than promotes.
    chat_cfg: ChatBrainConfig | None
    try:
        chat_cfg, chat_adapter, chat_options, chat_chain = resolve_chat_brain_runtime()
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
    ))

    vault_indexer = (
        VaultIndexer(embeddings=bundle.embeddings, fts_index=bundle.fts_index)
        if bundle.embeddings is not None
        else None
    )

    vault_librarian = VaultLibrarian(
        vault_manager=vault_manager,
        adapter=chat_adapter,
        adapter_options=chat_options,
        config=vault_cfg,
        agents_dir=agents_dir,
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
        log_dir=TESSERACT_HOME / "logs" / "circuit-breakers",
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
        registry.register(InvokeAgentTool(
            agents_dir=agents_dir,
            adapter=invoke_adapter,
            options=chat_options,
            parent_registry=registry,
            max_tool_iterations=chat_cfg.tool_iteration_cap,
            max_consecutive_adapter_errors=chat_cfg.consecutive_error_cap,
            policy=policy,
            cost_ledger=(app.get("cost_ledger") if app is not None else None),
        ))
        registry.register(SessionOpenTool(
            agents_dir=agents_dir,
            adapter=invoke_adapter,
            options=chat_options,
            registry=registry,
            max_tool_iterations=chat_cfg.tool_iteration_cap,
            max_consecutive_adapter_errors=chat_cfg.consecutive_error_cap,
        ))
    registry.register(SessionSendTool())
    registry.register(SessionResultTool())
    registry.register(SessionCloseTool())
    registry.register(SessionListTool())
    registry.register(ControllerSessionListTool())

    # MO-9-8: brief_render — operator-facing `/brief` slash. Pre-fetches
    # Tavily under loop_cost_caps, invokes the 5 digester agents in
    # order, writes the brief markdown + a brief-as-memory record.
    # Uses the same chat_brain adapter `InvokeAgentTool` was built with
    # (loop above) so sub-digester completions route through the
    # operator's configured primary model.
    registry.register(BriefRenderTool(
        adapter=invoke_adapter,
        adapter_options=chat_options,
        memory_store=bundle.store,
        event_store=workspace_store,
        vault_librarian=vault_librarian,
    ))

    # MO-9-9: brief_read — read-only companion to brief_render. Returns
    # today's brief body (frontmatter stripped) so the chat_brain can
    # answer voice "read brief" requests by reading the file back
    # through the normal TTS lane.
    registry.register(BriefReadTool())

    # Lean-agent-os P1 Task 2 — tool_search meta-tool. AUTO, read-only;
    # searches the full registry and enables matching extended tools for
    # the rest of the session. See `_CORE_TOOL_NAMES` above.
    registry.register(ToolSearchTool())

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
    # Daily-brief uses this for auto-promote of world cards.
    registry.vault_librarian = vault_librarian  # type: ignore[attr-defined]

    return registry, mood, voice_state, bundle, alarm_registry


def _apply_tool_tiers(registry: ToolRegistry) -> None:
    """Mark `_CORE_TOOL_NAMES` as `tier = "core"` on the live instances.

    Instance-attribute assignment shadows the `Tool.tier` ClassVar default
    ("extended") without touching the individual tool source files — the
    pinned set lives in one place (`_CORE_TOOL_NAMES` above) instead of
    45 scattered class-body edits. Raises if a pinned name isn't actually
    registered — a typo here would otherwise silently leave that tool
    extended (visibility bug, not caught by any type checker). Names in
    `_CONDITIONAL_CORE_TOOL_NAMES` are exempt: they legitimately don't
    register in adapter-less contexts.
    """
    registered = set(registry.names())
    missing = sorted(_CORE_TOOL_NAMES - registered - _CONDITIONAL_CORE_TOOL_NAMES)
    if missing:
        raise RuntimeError(
            f"_CORE_TOOL_NAMES references unregistered tool name(s): {missing}. "
            "Check tesseract/brain/boot.py::_CORE_TOOL_NAMES against "
            "registry.names() — likely a stale/typo'd tool name."
        )
    for name in _CORE_TOOL_NAMES & registered:
        registry.tools[name].tier = "core"


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
    valid_risks = {"autonomous", "propose", "operator_gate", "absolute_deny"}
    for tool in registry.tools.values():
        posture = getattr(type(tool), "default_posture", "")
        if posture not in ("auto", "ask", "deny"):
            raise RuntimeError(
                f"tool '{tool.name}' (class {type(tool).__name__}) declares "
                f"default_posture={posture!r}; expected one of "
                f"('auto','ask','deny'). Set it at the class level so the "
                f"runtime has a single source of truth."
            )
        # AU-3 — every concrete Tool subclass MUST declare risk_class.
        # The AgendaStore (AU-4) compares this against agenda-item class
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
                "Docs/Plan/autonomy/_shared/risk-class-taxonomy.md."
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
        class_defaults[tool.name] = posture

    if policy is None:
        # REPL / unit tests sometimes call build_tool_registry without a
        # policy. Skip drift logging — the policy gets wired later or
        # never (in test contexts that don't care about postures).
        return

    policy.attach_class_defaults(class_defaults)

    yaml_defaults = dict(policy.tools_defaults)
    registered = set(class_defaults)
    yaml_listed = set(yaml_defaults)

    orphans = sorted(yaml_listed - registered)
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
        # Disable auto-write with TARS_NO_AUTOSYNC=1.
        logger.error(
            "permissions.yaml drift: %d tool(s) registered but absent from "
            "tools: %s", len(using_class_default), ", ".join(using_class_default),
        )
        if os.getenv("TARS_NO_AUTOSYNC") == "1":
            logger.error(
                "TARS_NO_AUTOSYNC=1 — leaving yaml unmodified; class defaults "
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
