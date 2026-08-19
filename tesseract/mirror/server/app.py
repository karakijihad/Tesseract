from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Any, Callable

from aiohttp import web
from dotenv import load_dotenv

from tesseract.kernel.adapters.base import AdapterOptions
from tesseract.mirror.server.config import ServerConfig
from tesseract.mirror.server.cors import build_cors_middleware, resolve_allowed_origins
from tesseract.mirror.server.routes import agents as agents_route
from tesseract.mirror.server.routes import brief as brief_route
from tesseract.mirror.server.routes import channels as channels_route
from tesseract.mirror.server.routes import chains as chains_route
from tesseract.mirror.server.routes import chats as chats_route
from tesseract.mirror.server.routes import commands as commands_route
from tesseract.mirror.server.routes import conscience as conscience_route
from tesseract.mirror.server.routes import cost as cost_route
from tesseract.mirror.server.routes import downloads as downloads_route
from tesseract.mirror.server.routes import events as events_route
from tesseract.mirror.server.routes import asset_files as asset_files_route
from tesseract.mirror.server.routes import home_files as home_files_route
from tesseract.mirror.server.routes import local_models as local_models_route
from tesseract.mirror.server.routes import observe as observe_route
from tesseract.mirror.server.routes import ollama as ollama_route
from tesseract.mirror.server.routes import observer_consent as observer_consent_route
from tesseract.mirror.server.routes import observer_stats as observer_stats_route
from tesseract.mirror.server.routes import providers as providers_route
from tesseract.mirror.server.routes import alarms as alarms_route
from tesseract.mirror.server.routes import schedule as schedule_route
from tesseract.mirror.server.routes import settings as settings_route
from tesseract.mirror.server.routes import system as system_route
from tesseract.mirror.server.routes import uploads as uploads_route
from tesseract.mirror.server.routes import voice as voice_route
from tesseract.mirror.server.routes import workspace as workspace_route
from tesseract.mirror.server.routes.controller_sessions import (
    controller_session_status_handler,
    controller_sessions_handler,
)
from tesseract.mirror.server.routes.health import health
from tesseract.mirror.server.log_forwarder import (
    install_log_forwarder,
    uninstall_log_forwarder,
)
from tesseract.mirror.server.pty_manager import PTYManager
from tesseract.mirror.server.ws import websocket_handler
from tesseract.mirror.server.controller_ws import controller_ws_handler
from tesseract.paths import CONFIG_DIR, TESSERACT_HOME, home_logs_root

log = logging.getLogger(__name__)

TESSERACT_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = TESSERACT_DIR.parent
_LOOP_LAG_INTERVAL_S = 1.0
_LOOP_LAG_WARN_S = 2.0
# How often the watchdog samples the loop thread's stack, and how many
# samples it keeps. 0.25s over 240 samples is a minute of history for a few
# hundred KB — enough to cover any stall worth chasing.
_LAG_SAMPLE_INTERVAL_S = 0.25
_LAG_SAMPLE_HISTORY = 240
# (monotonic, formatted top frames) for the event-loop thread only.
_lag_samples: deque[tuple[float, str]] = deque(maxlen=_LAG_SAMPLE_HISTORY)
_lag_sampler_started = False


#: Request-body ceiling. aiohttp defaults to 1 MiB, which is *just* under what
#: wake-word calibration legitimately sends: five phrase takes plus a few
#: seconds of ordinary speech is ~730 KB of audio, and base64 costs 4 bytes per
#: 3. A payload past the default is refused by the framework with a bare,
#: non-JSON 413 before any handler runs — during a recording session that reads
#: as the feature being broken rather than as a limit.
#:
#: Set explicitly so the two limits are chosen together: this is the outer
#: bound, and `routes/voice.py::MAX_CALIBRATION_AUDIO_BYTES` is the inner one
#: that can still explain itself in seconds. Raised only enough to put the
#: friendly refusal in front of the framework's, not enough to make large
#: bodies cheap.
MAX_REQUEST_BYTES = 4 * 1024 * 1024


def create_app(config: ServerConfig) -> web.Application:
    app = web.Application(
        middlewares=[build_cors_middleware(config.cors_origins)],
        client_max_size=MAX_REQUEST_BYTES,
    )
    app["config"] = config
    # The WebSocket handshake is not CORS-gated by the middleware — CORS only
    # decorates responses, it never refuses one. `/ws` reaches the PTY, which
    # is operator-direct and deliberately outside the permission engine, so the
    # handshake enforces this allowlist itself.
    app["allowed_origins"] = resolve_allowed_origins(config.cors_origins)
    # Writable user-state root — settings.py's yaml read/write helpers key
    # off this to resolve config under TESSERACT_HOME, not the source tree.
    app["tesseract_dir"] = TESSERACT_HOME
    app["repo_root"] = REPO_ROOT
    app["sessions"] = {}  # {session_id: aiohttp.web.WebSocketResponse} — populated by Phase 2
    app["event_logs"] = {}  # {session_id: EventLog} — populated by Phase 2
    app["server_sessions"] = {}  # {session_id: ServerSession} — populated by Phase 2
    app["tool_registry"] = None
    app["mood"] = None  # MoodState | None — populated by _on_startup; bridged to frontend via entity_signals
    app["observer"] = None
    app["observer_subscriber"] = None  # ObserverSubscriber | None — built alongside observer; attaches on arm
    app["observer_state"] = "off"  # off | armed | observing — set via /api/observer/{arm,disarm}
    app["observer_consented_panes"] = set()  # pane_ids granted PTY observation; cleared on disarm/pane close/disconnect
    app["adapter"] = None
    app["adapter_options"] = None
    app["adapter_entry"] = None
    # None while chat infra is still booting or resolved cleanly; a
    # plain-language reason string once every chat_brain candidate has been
    # tried and failed (no key / disabled) — read by the capabilities route
    # and by NullChatAdapter's in-chat error message. See _build_chat_infra.
    app["chat_infra_error"] = None
    # mirror-multi-chat P2 inc.C2 — per-provider chat-turn concurrency budget.
    # `_chat_turn_provider_slot` (ws.py) lazily fills the dict with a
    # Semaphore(cap) per provider so parallel background chats can't collide on
    # one provider's rate limit. Cap from runtime.yaml (raise-loudly).
    from tesseract.config.runtime_limits import (
        default_runtime_config_path,
        load_max_concurrent_chat_turns_per_provider,
        load_max_concurrent_spawns_per_session,
        load_max_spawn_depth,
        load_spawn_stall_seconds,
    )
    app["chat_turn_semaphores"] = {}
    app["max_concurrent_chat_turns_per_provider"] = (
        load_max_concurrent_chat_turns_per_provider(default_runtime_config_path())
    )
    # Spawn halt-watchdog bound (Stage 2B) — passed to every ChatSession so the
    # per-turn sweep flags spawns stuck `running` past it.
    app["spawn_stall_seconds"] = load_spawn_stall_seconds(default_runtime_config_path())
    # per-session cap on concurrent background spawns.
    app["max_concurrent_spawns_per_session"] = (
        load_max_concurrent_spawns_per_session(default_runtime_config_path())
    )
    # trio W3 — structural cap on spawn nesting depth.
    app["max_spawn_depth"] = load_max_spawn_depth(default_runtime_config_path())
    app["system_prompt"] = ""
    app["pty_manager"] = PTYManager(config.terminal)
    app["pty_manager"].bind_app(app)
    app["scheduler"] = None  # SchedulerEngine | None — populated by _on_startup (S0)
    app["autonomy_kernel"] = None  # AutonomyKernel | None — populated by _on_startup (AU-5)
    app["autonomy_governor"] = None  # Governor | None — populated by _on_startup (AU-6)
    app["autonomy_pause_store"] = None  # PauseStore | None — populated by routes/agenda::register
    app["alarm_registry"] = None  # AlarmRegistry | None — populated by _on_startup (S4)
    app["stt_engine"] = None  # STTEngine | None — populated when voice config is present
    app["tts_engine"] = None  # TTSEngine | None — populated when voice config is present
    app["vault_config"] = None  # VaultConfig | None — populated when watcher reloads
    app["config_watcher"] = None  # ConfigWatcher | None — Phase 18 auto-config-reflection
    app["config_reload_toasts_enabled"] = _config_reload_toasts_enabled(config)
    app["_warmup_tasks"] = []  # list[asyncio.Task] — fire-and-forget model warm-ups; drained on shutdown
    # Created here, not lazily in the appender: `_build_voice_runtime` queues
    # into it from a worker thread while the drainer consumes it on the loop,
    # and two racing `setdefault` calls could each install a list, silently
    # dropping one lane's warm-up.
    app["_serial_warmups"] = []  # list[(name, coro)] — GIL-bound, drained one at a time
    app["log_forwarder"] = None  # MirrorLogHandler | None — installed by _on_startup, removed on shutdown
    app["workspace_event_store"] = _build_workspace_event_store()
    app["conversation_store"] = _build_conversation_store()
    app["channels_config"] = _load_channels_config()  # CR-1: typed channels.yaml; live-reloaded by config_watcher
    app["telegram_bridge"] = None  # TelegramBridge | None — started on boot if TELEGRAM_BOT_TOKEN is set
    app["command_registry"] = None  # CommandRegistry | None — built on startup once tool_registry is ready
    app["activity_subscriber"] = None  # ActivitySubscriber | None — AS-1 controller→Mirror activity push; started in _init_background
    app["controller_parked_asks"] = {}  # dict[approval_id, dict] — Option B (2026-07-13) view of the controller daemon's parked asks; mutated in place by the ActivitySubscriber
    app["mcp_server"] = None  # MCPServer | None — mcp-control-plane P2; populated in _on_startup
    app["mcp_approvals"] = None  # MCPApprovalRegistry | None — P3 ASK-over-MCP; populated in _on_startup
    app["mcp_clients"] = None  # MCPClientManager | None — capability-growth Phase 2 OUTBOUND client; connected in _init_background STAGE 2

    app["loop_lag_task"] = None
    app["brief_delivery_task"] = None  # Task | None — brief delivery service; started in _init_background

    _register_routes(app)
    app.on_startup.append(_on_startup)
    app.on_shutdown.append(_on_shutdown)
    return app


def _register_routes(app: web.Application) -> None:
    # AU-1 — supervisor visibility + operator clean-shutdown route.
    from tesseract.mirror.server.routes import runtime as runtime_route
    runtime_route.register(app)
    # 2026-07-30 — frontend error intake (webview console is invisible in
    # the packaged app; UI crashes must land in a file on disk).
    from tesseract.mirror.server.routes import client_log as client_log_route
    client_log_route.register(app)
    # AU-4 S2 — AgendaStore REST routes (list/get/create/patch/cancel/approve).
    from tesseract.mirror.server.routes import agenda as agenda_route
    agenda_route.register(app)
    # AU-7 S1 — Autonomy Dashboard read-only feeds.
    from tesseract.mirror.server.routes import autonomy as autonomy_route
    autonomy_route.register(app)
    # AU-10 — outbound notification settings (mute UI + rate inspection).
    from tesseract.mirror.server.routes import notifications as notifications_route
    notifications_route.register(app)
    # AU-21 — operator presence (viewSnapshot WS handler is wired in ws.py).
    from tesseract.mirror.server.routes import operator_view as operator_view_route
    operator_view_route.register(app)
    # Y-1 — per-view canvas state persistence (GET/POST /api/canvas/<view>).
    from tesseract.mirror.server.routes import canvas_state as canvas_state_route
    canvas_state_route.register(app)
    # Y-2 — Surface Protocol REST (list + operator emit_event).
    from tesseract.mirror.server.routes import surfaces as surfaces_route
    surfaces_route.register(app)
    # CV-1 — lane bridge (Mirror → controller daemon IPC) for canvas lane cards.
    from tesseract.mirror.server.routes import lanes as lanes_route
    lanes_route.register(app)
    # AS-1 — Unified Activity Registry REST (snapshot hydration; deltas stream
    # over the `activity` WS channel in ws.py).
    from tesseract.mirror.server.routes import activity as activity_route
    activity_route.register(app)
    # P4-2 — serve browser screenshots captured by BrowserManager.
    from tesseract.mirror.server.routes import browser_assets as browser_assets_route
    browser_assets_route.register(app)
    # mcp-control-plane P4 — TESSERACT-as-MCP-server (Streamable-HTTP): the
    # spec-compliant JSON-RPC endpoint POST/GET/DELETE /mcp. Handlers resolve
    # the live MCPServer off app["mcp_server"] (built in _on_startup); until
    # then they answer 503.
    from tesseract.mirror.server.mcp import MCPServer
    MCPServer.register_routes(app)
    # mcp-control-plane P3 — operator-facing ASK-over-MCP approval routes
    # (list + decide). Operator-trusted (no MCP bearer); resolves a held
    # tools/call awaiting operator approval.
    from tesseract.mirror.server.routes import mcp_approvals as mcp_approvals_route
    mcp_approvals_route.register(app)
    # trio W4 — parked background-spawn asks (ask-instead-of-die): list +
    # decide. Settles the same future the original chat card holds.
    from tesseract.mirror.server.routes import asks_parked as asks_parked_route
    asks_parked_route.register(app)
    # Operator bulk activity control — POST /api/activity/close-all cancels every
    # cancellable running unit (lanes/mcp_sessions/delegates) in one action.
    from tesseract.mirror.server.routes import activity_control as activity_control_route
    activity_control_route.register(app)
    # Capability report — which providers/keys are live, off, or missing a
    # key, and why. Must be reachable with zero keys set (fresh install), so
    # it stays a plain providers.yaml + os.environ read with no substrate
    # dependency.
    from tesseract.mirror.server.routes import capabilities as capabilities_route
    capabilities_route.register(app)
    # The editing half of the same picture: the keys themselves, read from
    # .env.example and written back to <TESSERACT_HOME>/.env. Same zero-key
    # constraint — this is the route a fresh install opens to stop being one.
    from tesseract.mirror.server.routes import env_keys as env_keys_route
    env_keys_route.register(app)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/controller/sessions", controller_sessions_handler)
    app.router.add_get(
        "/api/controller_sessions/{session_id}", controller_session_status_handler
    )
    # Session-agnostic chat library (chats persist across WS connections;
    # create is WS-only). `{chat_id}/archive|restore` are more specific than
    # `{chat_id}` — aiohttp matches by exact path, no conflict.
    #
    # Path order matters for `days`: aiohttp matches top-down, so a literal
    # segment MUST be registered before the `{chat_id}` placeholder or the
    # placeholder swallows it.
    app.router.add_get("/api/chats", chats_route.list_chats_handler)
    app.router.add_get("/api/chats/days", chats_route.list_chats_by_day_handler)
    app.router.add_get("/api/chats/{chat_id}", chats_route.get_chat_handler)
    app.router.add_get("/api/chats/{chat_id}/preview", chats_route.preview_chat_handler)
    app.router.add_patch("/api/chats/{chat_id}", chats_route.rename_chat_handler)
    app.router.add_post("/api/chats/{chat_id}/archive", chats_route.archive_chat_handler)
    app.router.add_post("/api/chats/{chat_id}/restore", chats_route.restore_chat_handler)
    app.router.add_delete("/api/chats/{chat_id}", chats_route.delete_chat_handler)
    app.router.add_post("/api/observe", observe_route.observe)
    app.router.add_post("/api/observer/arm", observer_consent_route.arm)
    app.router.add_post("/api/observer/disarm", observer_consent_route.disarm)
    app.router.add_get("/api/observer/status", observer_consent_route.status)
    app.router.add_get("/api/observer/stats", observer_stats_route.stats)
    app.router.add_get("/api/schedule", schedule_route.list_jobs)
    app.router.add_get("/api/schedule/handlers", schedule_route.list_handlers)
    app.router.add_get("/api/schedule/roles", schedule_route.list_roles)
    app.router.add_post("/api/schedule/create", schedule_route.create_job)
    app.router.add_delete("/api/schedule/{name}", schedule_route.remove_job)
    app.router.add_get("/api/alarms", alarms_route.list_alarms)
    app.router.add_post("/api/alarms", alarms_route.create_alarm)
    app.router.add_delete("/api/alarms/{handle}", alarms_route.cancel_alarm)
    app.router.add_post("/api/alarms/{handle}/snooze", alarms_route.snooze_alarm)
    app.router.add_get("/api/providers/catalog", providers_route.list_chat_models)
    app.router.add_get("/api/agents", agents_route.list_agents_handler)
    app.router.add_get("/api/agents/pending", agents_route.list_pending_handler)
    app.router.add_get("/api/agents/{name}", agents_route.get_agent_handler)
    app.router.add_get("/api/agents/{name}/source", agents_route.get_agent_source_handler)
    app.router.add_post("/api/agents/{name}/source", agents_route.save_agent_source_handler)
    app.router.add_post("/api/agents/{name}/toggle", agents_route.toggle_agent_disabled_handler)
    # MO-9-9 — Brief tab. `/api/brief/dates` MUST be registered before
    # `/api/brief/{date}` or the placeholder would swallow the literal.
    app.router.add_get("/api/brief/dates", brief_route.get_brief_dates)
    # MO-9-14 — `/api/brief/feedback` placed before `/{date}` for the
    # same aiohttp top-down match reason.
    app.router.add_post("/api/brief/feedback", brief_route.brief_feedback)
    app.router.add_get("/api/brief/{date}", brief_route.get_brief)
    app.router.add_post("/api/brief/refresh", brief_route.refresh_brief)
    # MO-9-11 — Mirror Channels tab. Registry-backed; `/restart` and
    # `/telegram/status` are ASK-gated via the operator's chat session.
    # MO-9-12 — added /users (read), /users/{user_id}/conversation (read),
    # /approve, /revoke, /block (all ASK-gated; share posture_source
    # 'channel_mutation'). The literal `/telegram/status` path stays
    # registered BEFORE `/{name}/restart` so aiohttp's top-down match does
    # not swallow it; the same rule applies to MO-9-12's /users and
    # /users/{user_id}/conversation — they precede the verbed mutation
    # routes since they themselves carry no verb segment.
    app.router.add_get("/api/channels", channels_route.list_channels_handler)
    app.router.add_post(
        "/api/channels/telegram/status", channels_route.set_telegram_status_handler
    )
    app.router.add_get(
        "/api/channels/{name}/users", channels_route.list_channel_users_handler
    )
    app.router.add_get(
        "/api/channels/{name}/users/{user_id}/conversation",
        channels_route.get_channel_conversation_handler,
    )
    app.router.add_post(
        "/api/channels/{name}/approve", channels_route.approve_channel_user_handler
    )
    app.router.add_post(
        "/api/channels/{name}/revoke", channels_route.revoke_channel_user_handler
    )
    app.router.add_post(
        "/api/channels/{name}/block", channels_route.block_channel_user_handler
    )
    app.router.add_post(
        "/api/channels/{name}/restart", channels_route.restart_channel_handler
    )
    # Offline-inbox surface (audit fix M1) — read missed messages and
    # manually trigger a replay drain.
    app.router.add_get(
        "/api/channels/{name}/users/{user_id}/missed",
        channels_route.list_channel_missed_handler,
    )
    app.router.add_post(
        "/api/channels/{name}/users/{user_id}/missed/replay",
        channels_route.replay_channel_missed_handler,
    )
    app.router.add_get("/api/conscience/drift", conscience_route.drift)
    app.router.add_get("/api/conscience/tool-usage", conscience_route.tool_usage)
    app.router.add_get("/api/soul", system_route.soul)
    app.router.add_get("/api/breakers", system_route.breakers)
    app.router.add_get("/api/identity", system_route.identity)
    app.router.add_post("/api/identity", system_route.set_identity)
    app.router.add_post("/api/mode", system_route.set_mode)
    app.router.add_post("/api/settings/compact-threshold", settings_route.set_compact_threshold)
    app.router.add_post("/api/settings/cost", settings_route.set_cost)
    app.router.add_post("/api/settings/voice-cost", settings_route.set_voice_cost)
    app.router.add_get("/api/settings/config-files", settings_route.get_config_files)
    app.router.add_post("/api/settings/tool-permission", settings_route.set_tool_permission)
    app.router.add_post("/api/settings/reset-defaults", settings_route.reset_defaults)
    app.router.add_post("/api/settings/role-models", settings_route.set_role_models)
    app.router.add_get("/api/settings/catalog", settings_route.get_catalog)
    app.router.add_post("/api/settings/model-ref", settings_route.set_model_ref)
    chains_route.register(app)
    app.router.add_get("/api/settings/voice", settings_route.get_voice)
    app.router.add_post("/api/settings/voice", settings_route.set_voice)
    app.router.add_post("/api/settings/voice/preset", settings_route.set_voice_preset)
    app.router.add_get("/api/settings/system", settings_route.get_system)
    app.router.add_get("/api/settings/session-policy", settings_route.get_session_policy)
    app.router.add_post("/api/settings/session-policy", settings_route.set_session_policy)
    app.router.add_get("/api/settings/session-caps", settings_route.get_session_caps)
    app.router.add_post("/api/settings/session-caps", settings_route.set_session_caps)
    app.router.add_get("/api/tools", system_route.tools)
    app.router.add_get("/api/voice/providers", voice_route.get_providers)
    app.router.add_get("/api/voice/catalog", voice_route.get_catalog)
    app.router.add_post("/api/voice/primary", voice_route.set_primary)
    app.router.add_post("/api/voice/test", voice_route.post_test)
    # Wake word. `GET` separates permission (`enabled`) from readiness
    # (`armed`) — reporting one number for both is how an operator ends up
    # believing a gate is live while it passes everything through. `DELETE` is
    # the rollback: the gate arms on the calibration existing.
    app.router.add_get("/api/voice/wake", voice_route.get_wake)
    app.router.add_post("/api/voice/wake/calibrate", voice_route.post_wake_calibrate)
    app.router.add_delete("/api/voice/wake", voice_route.delete_wake_calibration)
    app.router.add_get("/api/cost/state", cost_route.get_state)
    app.router.add_get("/api/cost/windows", cost_route.get_windows)
    app.router.add_get("/api/system/ollama", ollama_route.status)
    app.router.add_post("/api/system/ollama", ollama_route.action)
    app.router.add_get("/api/system/whisper", local_models_route.whisper_status)
    app.router.add_post("/api/system/whisper", local_models_route.whisper_action)
    app.router.add_get("/api/system/kokoro", local_models_route.kokoro_status)
    app.router.add_post("/api/system/kokoro", local_models_route.kokoro_action)
    app.router.add_post("/api/system/models/download", local_models_route.model_download)
    app.router.add_get("/api/uploads/chat/config", uploads_route.get_chat_upload_config)
    app.router.add_post("/api/uploads/chat/{session_id}", uploads_route.upload_chat_attachment)
    app.router.add_get(
        "/api/uploads/chat/{session_id}/{attachment_id}/{filename}",
        uploads_route.get_chat_attachment,
    )
    app.router.add_delete(
        "/api/uploads/chat/{session_id}/{attachment_id}",
        uploads_route.delete_chat_attachment,
    )
    app.router.add_post(
        "/api/uploads/chat/{session_id}/{attachment_id}/promote-to-vault",
        uploads_route.promote_chat_attachment_to_vault,
    )
    app.router.add_get(
        "/api/downloads/chat/{session_id}/{artifact_id}/{filename}",
        downloads_route.get_chat_download,
    )
    # Read-only serving of the operator's own trees. Distinct from the
    # chat-scoped route above, which is artifact-indexed.
    home_files_route.register(app)
    asset_files_route.register(app)
    app.router.add_get("/api/terminal/config", system_route.terminal_config)
    app.router.add_get("/api/events", events_route.events)
    app.router.add_get("/api/workspace/inbox", workspace_route.list_inbox)
    app.router.add_get("/api/workspace/event/{event_id}", workspace_route.get_event)
    app.router.add_post(
        "/api/workspace/event/{event_id}/decision", workspace_route.post_decision,
    )
    app.router.add_post(
        "/api/workspace/event/{event_id}/comment", workspace_route.post_comment,
    )
    app.router.add_post(
        "/api/workspace/operator-post", workspace_route.post_operator_post,
    )
    app.router.add_get("/api/workspace/seen", workspace_route.get_seen)
    app.router.add_post("/api/workspace/seen", workspace_route.post_seen)
    app.router.add_get("/api/workspace/docs", workspace_route.list_docs)
    app.router.add_get("/api/workspace/doc", workspace_route.get_doc)
    app.router.add_post("/api/workspace/doc", workspace_route.save_doc)
    app.router.add_get("/api/commands", commands_route.list_commands)
    app.router.add_get("/ws", websocket_handler)
    # Audit-1 M-2 — Mirror observer bridge to a controller session. Opens
    # a fresh ControllerClient per WS connection, attaches as observer,
    # and forwards typed transcript events as ``controller_event``
    # envelopes. See ``controller_ws.py``.
    app.router.add_get("/ws/controller/{session_id}", controller_ws_handler)


def _start_lag_sampler() -> None:
    """Sample the event-loop thread's stack from a watchdog thread.

    A lag warning says the loop was late; it never said WHY, and every
    attempt to find out from outside the process failed for the same reason
    — by the time a stack dump was requested, serviced and written, the stall
    was over and the loop was back to idling in `select`. The evidence has to
    be collected DURING the window, which means sampling continuously and
    keeping the recent past.

    Runs on a plain daemon thread, deliberately: it must be able to observe a
    loop that is not running. `sys._current_frames()` gives that thread's
    frames without cooperation from the loop, so a stall shows up as the same
    stack repeating across consecutive samples — which is the signature that
    names the culprit.

    If the sampler ITSELF stops producing samples across a stall, that is not
    a failure of the instrument; it is the finding. It means the interpreter
    could not schedule a plain thread either, so nothing Python-level was
    holding the loop and the cause is below us: the GIL held by a native call
    that never releases it, or the whole process descheduled by the OS.
    """
    global _lag_sampler_started
    if _lag_sampler_started:
        return
    _lag_sampler_started = True
    loop_thread_id = threading.get_ident()

    def _run() -> None:
        while True:
            try:
                frame = sys._current_frames().get(loop_thread_id)
                if frame is not None:
                    # Formatted immediately — holding frame objects would pin
                    # every local in the stall's stack for the ring's lifetime.
                    stack = traceback.extract_stack(frame)[-6:]
                    summary = " <- ".join(
                        f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in reversed(stack)
                    )
                    _lag_samples.append((time.monotonic(), summary))
            except Exception:  # noqa: BLE001 — a sampler must never take the process down
                pass
            time.sleep(_LAG_SAMPLE_INTERVAL_S)

    threading.Thread(target=_run, name="mirror-lag-sampler", daemon=True).start()


def _lag_window_report(lag: float) -> str:
    """What the loop thread was doing across the stall that just ended."""
    now = time.monotonic()
    window = [(t, s) for t, s in list(_lag_samples) if now - t <= lag + _LAG_SAMPLE_INTERVAL_S]
    if not window:
        return (
            "no stack samples cover the window — the sampler thread was not "
            "scheduled either, so the cause is below Python: a native call "
            "holding the GIL, or the process descheduled by the OS"
        )
    counts: dict[str, int] = {}
    for _t, summary in window:
        counts[summary] = counts.get(summary, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:3]
    span = f"{len(window)} samples over {window[-1][0] - window[0][0]:.1f}s"
    return " | ".join(f"{n}x {summary}" for summary, n in ranked) + f"  ({span})"


async def _monitor_loop_lag() -> None:
    """Log when the aiohttp loop is blocked long enough to threaten liveness."""
    loop = asyncio.get_running_loop()
    _start_lag_sampler()
    expected = loop.time() + _LOOP_LAG_INTERVAL_S
    while True:
        await asyncio.sleep(_LOOP_LAG_INTERVAL_S)
        now = loop.time()
        lag = now - expected
        if lag >= _LOOP_LAG_WARN_S:
            log.warning("mirror: loop was doing: %s", _lag_window_report(lag))
            log.warning(
                "mirror: event loop lag %.3fs exceeds %.3fs heartbeat-risk threshold",
                lag,
                _LOOP_LAG_WARN_S,
            )
        expected = now + _LOOP_LAG_INTERVAL_S


async def _on_startup(app: web.Application) -> None:
    """Fast path — minimum wiring so aiohttp binds the listener quickly.

    Splits into two phases so the supervisor's heartbeat sees a live
    backend within seconds of spawn instead of waiting 30-60s for every
    substrate to come up serially:

    * **Synchronous (this function)** — env load, log forwarder, app
      keys initialised, recovery_state stamped ``initializing``. Cheap;
      completes in well under 1s. aiohttp binds the listener as soon
      as this returns. ``/api/health`` answers 200 immediately with
      ``state=initializing`` so the supervisor stops flagging the
      backend as dead.
    * **Background (``_init_background`` task)** — the heavy chain:
      tool registry, ollama warmup, voice runtime, observer, chat infra,
      telegram bridge, recovery, scheduler, autonomy kernel, config
      watcher. Runs sequentially (substrates depend on one another) but
      does NOT block the listener.

    Routes that hit a substrate before it's ready use the existing
    "not wired yet → 503" shape (e.g. ``alarms.py``, ``schedule.py``).
    ``/api/health`` body's ``state`` field surfaces progress through
    ``initializing → recovering → ready``.
    """
    app["started_at"] = time.monotonic()
    app["recovery_state"] = "initializing"
    app["ollama_supervisor"] = None
    # Capture the main event loop so work that runs in a worker thread
    # (e.g. `_build_voice_runtime` under `asyncio.to_thread`) can still
    # schedule loop-bound tasks via `_schedule_warmup`.
    app["_main_loop"] = asyncio.get_running_loop()
    # Log forwarder first so any background-init exception reaches the
    # pulse of the next session that connects.
    app["log_forwarder"] = install_log_forwarder(app, asyncio.get_running_loop())
    app["loop_lag_task"] = asyncio.create_task(
        _monitor_loop_lag(), name="mirror-loop-lag-monitor",
    )
    _load_env()
    # Y-1 — ensure the per-view canvas-state dir exists at boot so the
    # gitignored tree is visible before the first PUT lands.
    from tesseract.mirror.server.routes.canvas_state import canvas_state_dir
    canvas_state_dir().mkdir(parents=True, exist_ok=True)
    _regenerate_kb_roles_summary()
    await _prepare_mcp_server(app)
    # The boot graph is read and checked against the registry HERE, not inside
    # the background task. `_init_background` swallows its own exceptions so a
    # broken substrate cannot take the backend down, and a malformed graph
    # would inherit that swallow — producing a backend that answers, reports
    # warm, and has prepared nothing. Config is authority and a contradiction
    # raises loudly, so it raises where it stops the start.
    from tesseract.boot_graph import load_graph, validate

    app["boot_registry"] = build_substrate_registry(app)
    app["boot_layers"] = load_graph()
    validate(app["boot_layers"], app["boot_registry"])
    # Fire the heavy boot chain WITHOUT awaiting it. aiohttp will bind
    # the listener as soon as this on_startup hook returns; the
    # supervisor's heartbeat sees /api/health = 200 within seconds.
    task = asyncio.create_task(
        _init_background(app), name="mirror-init-background",
    )
    app["init_background_task"] = task


_BOOT_TIMING_ENV = "TESSERACT_BOOT_TIMING"


def _enable_boot_timing_loop_debug() -> None:
    """Turn on asyncio's own slow-callback reporting for this boot.

    Deliberately not a configured value: it is a debug override for one
    boot, not a setting, and asyncio's own default threshold applies unless
    the env var carries one.
    """
    raw = os.environ.get(_BOOT_TIMING_ENV, "")
    try:
        threshold = float(raw)
    except ValueError:
        return
    if threshold <= 0 or threshold == 1.0:
        return  # `1` means "on", not "a 1-second threshold"
    loop = asyncio.get_running_loop()
    loop.set_debug(True)
    loop.slow_callback_duration = threshold
    log.info(
        "boot timing: loop debug on, reporting callbacks slower than %.2fs",
        threshold,
    )


def _needs_tool_registry(app: web.Application):
    """Skip reason for substrates that are meaningless without the registry.

    Routes to the state the registry substrate itself installs rather than to
    a second flag: there is one answer to "did the tool registry build", and it
    is whether `app["tool_registry"]` is there.
    """
    def _reason() -> str | None:
        if app.get("tool_registry") is None:
            return "the tool registry did not build"
        return None

    return _reason


async def _prepare_activity_rebuild(app: web.Application) -> None:
    from tesseract.orchestrator.activity.rebuild import rebuild_from_disk

    seeded = await asyncio.to_thread(rebuild_from_disk)
    log.info("activity: seeded %d persistent record(s) from disk", seeded)


async def _prepare_tool_registry(app: web.Application) -> None:
    """Build the registry off-loop, then publish what it produced on the loop.

    The heavy half is threaded and the assignments are not, which is the same
    split the hand-written chain had: it built inside `to_thread` and unpacked
    after the gather.
    """
    result = await asyncio.to_thread(
        _try_build_tool_registry, policy=app["config"].permissions, app=app,
    )
    if not isinstance(result, tuple):
        return
    registry, mood, bundle, alarm_registry = result
    app["tool_registry"] = registry
    app["mood"] = mood
    app["memory_bundle"] = bundle
    app["alarm_registry"] = alarm_registry
    # Daily-brief auto-promote reads ``compile_source`` off this; cron jobs
    # find it via app context.
    app["vault_librarian"] = (
        getattr(registry, "vault_librarian", None) if registry else None
    )
    set_state_tool = registry.tools.get("set_state") if registry is not None else None
    app["entity_affect"] = getattr(set_state_tool, "_affect", None)
    if alarm_registry is not None:
        log.info("alarm_registry: ready (%d restored)", len(alarm_registry.list_pending()))
    _schedule_warmup(app, _warm_embeddings(bundle), name="embeddings")
    # Armed BEFORE the warm-up is queued, because the thing it guards against
    # runs in between: the autonomy kernel starts below the warm line and its
    # resume sweep can retrieve, which would build the session on the request
    # path and block the loop for seconds.
    reranker = getattr(bundle, "reranker", None) if bundle is not None else None
    if reranker is not None and hasattr(reranker, "defer_until_warm"):
        reranker.defer_until_warm()
    _queue_serial_warmup(app, _warm_reranker(bundle), name="reranker")


async def _prepare_cost_ledger(app: web.Application) -> None:
    app["cost_ledger"] = await asyncio.to_thread(_try_build_cost_ledger)
    _wire_cost_broadcast(app)


async def _prepare_voice_runtime(app: web.Application) -> None:
    await asyncio.to_thread(_build_voice_runtime, app)


async def _prepare_observer(app: web.Application) -> None:
    observer = await asyncio.to_thread(_try_build_observer, app.get("cost_ledger"))
    app["observer"] = observer
    if observer is None:
        return
    from tesseract.brain.observer_subscriber import ObserverSubscriber

    app["observer_subscriber"] = ObserverSubscriber(observer)
    # Owner request 2026-04-29 — observer arms by default on boot.
    app["observer_state"] = "armed"
    log.info("observer: armed-by-default at boot")


async def _prepare_chat_infra(app: web.Application) -> None:
    await asyncio.to_thread(_build_chat_infra, app)


async def _prepare_command_registry(app: web.Application) -> None:
    # Late import — commands_registry pulls in envelope/session helpers that
    # need the chat infra, which the layer above this one built.
    from tesseract.mirror.server.commands_registry import build_command_registry

    app["command_registry"] = await asyncio.to_thread(
        build_command_registry, app["tool_registry"],
    )
    log.info(
        "command_registry: %d specs ready (mirror+kernel)",
        len(app["command_registry"].specs()),
    )


async def _prepare_voice_dependent_tools(app: web.Application) -> None:
    await asyncio.to_thread(_register_voice_dependent_tools, app)


async def _prepare_outbound_notifier(app: web.Application) -> None:
    """Prime the shared notifier before any scheduler job can want it.

    Without it, a job that notifies fires with reason="no_notifier" whenever it
    beats every other path that would have warmed the cache. The getters
    resolve channel state at notify-time, so priming now is safe even when the
    telegram bridge was a no-op for want of a token.
    """
    _get_outbound_notifier(app)
    log.info("outbound_notifier: initialized (shared cache primed)")
    if app.get("pty_manager") is not None:
        log.info("pty_manager: ready (max %d pty(s))", app["pty_manager"].max_ptys)


async def _prepare_serial_warmups(app: web.Application) -> None:
    """Drain the GIL-bound queue, and wait for it.

    The queue is still how these arrive: `_build_voice_runtime` fills it at
    boot AND on every voice hot reload, and the single-drainer rule is what
    stops a reload's pair from being awaited concurrently with a boot's. This
    layer starts that drainer and waits for it, which is the difference
    between the splash meaning something and not — these are the only boot
    work that holds the GIL, so they are the only boot work that can still
    stall a turn after the window opens.

    The `_schedule_warmup` set is deliberately NOT waited on: it releases the
    GIL, so it costs a first-use latency rather than a stall.
    """
    app["_serial_warmups_started"] = True
    _ensure_serial_drainer(app)
    drain = app.get("serial_warmup_task")
    if drain is not None:
        await drain


async def _prepare_mcp_clients(app: web.Application) -> None:
    await _connect_mcp_clients(app, app.get("tool_registry"))


async def _start_brief_delivery(app: web.Application) -> None:
    """The delivery half of the brief. Generation is `brief_render`, a stage of
    the nightly row; this only decides when what it wrote reaches the operator,
    at the hour in `mirror.yaml::brief`."""
    from tesseract.mirror.server.brief_delivery import delivery_loop

    app["brief_delivery_task"] = asyncio.create_task(delivery_loop(app))


async def _prepare_activity_subscriber(app: web.Application) -> None:
    from tesseract.mirror.server.activity_subscriber import ActivitySubscriber

    subscriber = ActivitySubscriber(parked_store=app["controller_parked_asks"])
    await subscriber.start()
    app["activity_subscriber"] = subscriber
    log.info("activity: controller push subscriber started")


async def _prepare_operator_panes(app: web.Application) -> None:
    """Re-open operator-facing controller session viewer panes that died with
    the previous backend process. Background sessions stay headless."""
    from tesseract.mirror.server.routes.controller_sessions import (
        _list_active_sessions,
    )

    pty_manager = app.get("pty_manager")
    if pty_manager is None:
        return
    await reattach_operator_panes(
        list_fn=_list_active_sessions,
        pty_open_fn=pty_manager.dispatch_for_agent,
    )


async def _prepare_wake_spotter(app: web.Application) -> None:
    await asyncio.to_thread(_warm_wake_spotter, app)


async def _prepare_cli_auth(app: web.Application) -> None:
    from tesseract.brain import cli_auth

    await cli_auth.refresh()


async def _prepare_browser_provision(app: web.Application) -> None:
    from tesseract.orchestrator.browser.provision import ensure_browsers_if_wanted

    await ensure_browsers_if_wanted()


def build_substrate_registry(app: web.Application):
    """Every substrate the Mirror backend knows how to prepare.

    Each entry states three things about its own code that `boot.yaml` cannot
    override: whether preparing it holds the interpreter, what the app does
    when it was never prepared, and whether this machine has any use for it.
    A blank `degrade` is legal only above the warm line, where there is no
    cold behaviour to describe because the window waits.
    """
    from tesseract.boot_graph import SubstrateRegistry

    reg = SubstrateRegistry()

    # ── above the line ────────────────────────────────────
    reg.add(
        "tls_trust_store", lambda: _warm_tls_trust_store(),
        # The CA-bundle load is OpenSSL's, and it runs under `to_thread`.
        holds_gil=False, degrade="",
    )
    reg.add(
        "ollama", lambda: _ensure_ollama_ready(app),
        holds_gil=False, degrade="",
    )
    reg.add(
        "tool_registry", lambda: _prepare_tool_registry(app),
        holds_gil=False, degrade="",
    )
    reg.add(
        "cost_ledger", lambda: _prepare_cost_ledger(app),
        holds_gil=False, degrade="",
    )
    reg.add(
        "recovery", lambda: _run_recovery(app),
        holds_gil=False, degrade="",
    )
    reg.add(
        "activity_rebuild", lambda: _prepare_activity_rebuild(app),
        holds_gil=False, degrade="",
    )
    reg.add(
        "voice_runtime", lambda: _prepare_voice_runtime(app),
        holds_gil=False, degrade="",
    )
    reg.add(
        "observer", lambda: _prepare_observer(app),
        holds_gil=False, degrade="",
    )
    reg.add(
        "chat_infra", lambda: _prepare_chat_infra(app),
        holds_gil=False, degrade="",
    )
    reg.add(
        "command_registry", lambda: _prepare_command_registry(app),
        holds_gil=False, degrade="", requires=_needs_tool_registry(app),
    )
    reg.add(
        "voice_dependent_tools", lambda: _prepare_voice_dependent_tools(app),
        holds_gil=False, degrade="", requires=_needs_tool_registry(app),
    )
    reg.add(
        "telegram_bridge", lambda: _start_telegram_bridge(app),
        holds_gil=False, degrade="",
    )
    reg.add(
        "outbound_notifier", lambda: _prepare_outbound_notifier(app),
        holds_gil=False, degrade="",
    )
    reg.add(
        "scheduler", lambda: _start_scheduler(app),
        holds_gil=False, degrade="",
    )
    reg.add(
        "serial_warmups", lambda: _prepare_serial_warmups(app),
        # onnxruntime's session constructor does not release the GIL for its
        # duration, so a worker thread buys nothing — measured at 4.6s of a
        # 4.7s Kokoro build. This is the fact that keeps it above the line.
        holds_gil=True, degrade="",
    )

    # ── below the line ────────────────────────────────────
    reg.add(
        "autonomy_kernel", lambda: _start_autonomy_kernel(app),
        holds_gil=False,
        degrade=(
            "no agenda ticks and no dispatch; jobs still run and what they "
            "publish is discarded, with the first loss per source warned and "
            "the rest counted"
        ),
    )
    reg.add(
        "config_watcher", lambda: _start_config_watcher(app),
        holds_gil=False,
        degrade=(
            "a yaml edit reaches anything that re-reads the file; anything "
            "needing a rebuild waits for the next start"
        ),
    )
    reg.add(
        "activity_subscriber", lambda: _prepare_activity_subscriber(app),
        holds_gil=False,
        degrade=(
            "controller-origin parked asks refuse with 503 naming the missing "
            "half; chat-origin asks still settle, and delegates plus the disk "
            "seed still populate the activity registry"
        ),
    )
    reg.add(
        "mcp_clients", lambda: _prepare_mcp_clients(app),
        holds_gil=False,
        degrade=(
            "no remote MCP tools in the registry; local kernel tools are "
            "unaffected and an mcp_servers.yaml save reports that it reached "
            "nothing"
        ),
        requires=_needs_tool_registry(app),
    )
    reg.add(
        "brief_delivery", lambda: _start_brief_delivery(app),
        holds_gil=False,
        degrade=(
            "the brief is still written at the anchor and readable in the "
            "Brief tab; it just is not pushed at your hour"
        ),
    )
    reg.add(
        "operator_panes", lambda: _prepare_operator_panes(app),
        holds_gil=False,
        degrade=(
            "viewer panes are not reattached; the operator's next connect and "
            "session list cover it"
        ),
    )
    reg.add(
        "cli_auth", lambda: _prepare_cli_auth(app),
        holds_gil=False,
        degrade="the capability report shows cli providers as unverified rather than unavailable",
    )
    reg.add(
        "browser_provision", lambda: _prepare_browser_provision(app),
        holds_gil=False,
        degrade=(
            "the first browser_* call returns an ordinary tool error naming "
            "the missing engine"
        ),
    )
    reg.add(
        "wake_spotter", lambda: _prepare_wake_spotter(app),
        holds_gil=False,
        degrade="the wake gate builds its decoder lazily on the first gated utterance",
    )
    return reg


def _mark_warm(app: web.Application) -> None:
    """Every substrate the operator can immediately touch is prepared.

    The launch splash waits on this via `/api/health`. Below-the-line layers
    are still running when it fires, and that is the whole point of the line.
    """
    if app.get("recovery_state") == "initializing":
        app["recovery_state"] = "ready"
    app["warm"] = True
    log.info("mirror: warm — the window may open; background layers continue")


async def _init_background(app: web.Application) -> None:
    """Walk the boot graph — after aiohttp binds the listener.

    What runs, in what order, and what the operator waits for is declared in
    `config/boot.yaml` and checked against the registry in `_on_startup`, so a
    contradiction stops the backend there rather than surfacing later as a
    substrate nobody prepared. Three invariants survive from the hand-written
    chain this replaces:

    1. **The event loop stays free.** `_on_startup` returns in under a second
       and `/api/health` answers throughout. CPU-bound preparations go through
       `asyncio.to_thread`; the one substrate that holds the GIL regardless is
       declared as doing so and cannot be placed below the line.
    2. **Independent substrates fan out.** A `fires: parallel` layer gathers
       its members; `serial` is for genuine dependency chains and for work
       that contends on one interpreter.
    3. **Failure isolation.** One broken substrate leaves the rest prepared,
       and the window still opens — a window that never appears is worse than
       a partial one.
    """
    try:
        _enable_boot_timing_loop_debug()
        from tesseract.boot_graph import run_layers

        await run_layers(
            app["boot_layers"], app["boot_registry"],
            on_window_open=lambda: _mark_warm(app),
        )
        log.info("mirror: background init complete; every layer prepared")
    except Exception:
        log.exception("mirror: background init crashed — leaving backend in partial-ready state")
    finally:
        # The safety net, not the signal: `_mark_warm` fires at the warm line
        # and this catches a boot that crashed before reaching it. A crashed
        # boot is as warm as this process will get.
        _mark_warm(app)


async def _on_shutdown(app: web.Application) -> None:
    """Tear down everything Mirror owns: WS sessions, scheduler tasks,
    observer subscriber, in-flight warmups, PTY processes, and any
    Mirror-spawned local daemons (Ollama). Order matters: close the
    operator-facing surfaces (WS) first so in-flight turns don't keep
    pulling on subsystems we're about to tear down, then drain background
    work, then kill child processes.

    AU-1: write the shutdown intent FIRST. The supervisor reads this
    file after backend exit to distinguish operator_quit from crash.
    Writing before the (long, error-prone) teardown means a teardown
    error doesn't blank the intent file and re-route as crash.
    """
    from tesseract.mirror.server.lifecycle import on_aiohttp_shutdown
    on_aiohttp_shutdown(app)
    # If background init is still running, cancel it so the heavy chain
    # doesn't fight teardown for substrates it's still building.
    init_task = app.get("init_background_task")
    if init_task is not None and not init_task.done():
        init_task.cancel()
        try:
            await init_task
        except (asyncio.CancelledError, Exception):
            pass
    lag_task = app.get("loop_lag_task")
    if lag_task is not None and not lag_task.done():
        lag_task.cancel()
        try:
            await lag_task
        except (asyncio.CancelledError, Exception):
            pass
    brief_task = app.get("brief_delivery_task")
    if brief_task is not None:
        brief_task.cancel()
        try:
            await brief_task
        except (asyncio.CancelledError, Exception):
            pass
    # mcp-control-plane P2 — signal open MCP SSE streams to close.
    mcp_server = app.get("mcp_server")
    if mcp_server is not None:
        try:
            await mcp_server.stop(app)
        except Exception:
            log.exception("mcp_server.stop on shutdown failed")
    # capability-growth Phase 2 — close outbound MCP client sessions + child
    # processes (each closed in its own owning task; janitor-friendly).
    mcp_clients = app.get("mcp_clients")
    if mcp_clients is not None:
        try:
            await mcp_clients.shutdown()
        except Exception:
            log.exception("mcp_clients.shutdown on shutdown failed")
    # AS-1 — tear down the always-on controller activity subscriber.
    activity_subscriber = app.get("activity_subscriber")
    if activity_subscriber is not None:
        try:
            await activity_subscriber.stop()
        except Exception:
            log.exception("activity subscriber stop on shutdown failed")
    await _close_all_websockets(app)
    # Phase 4 follow-up (2026-05-11): cancel any background spawns
    # (delegate_coder/codex/invoke_agent fired with background=true)
    # before the process exits. cleanup_session schedules cancel_all
    # fire-and-forget per-session; awaiting here guarantees subprocess
    # children (claude/codex CLIs) are SIGTERMed rather than left as
    # orphans when the parent Python exits.
    for sess in list((app.get("server_sessions") or {}).values()):
        chat_session = getattr(sess, "chat_session", None)
        spawns = getattr(chat_session, "spawns", None)
        if spawns is None:
            continue
        try:
            n = await spawns.cancel_all()
            if n:
                log.info("shutdown: cancelled %d background spawn(s) for session %s", n, sess.session_id)
        except Exception:
            log.exception("spawn cancel_all on shutdown failed for session %s", getattr(sess, "session_id", "?"))
    bridge = app.get("telegram_bridge")
    if bridge is not None:
        try:
            await bridge.stop()
        except Exception:
            log.exception("telegram bridge stop on shutdown failed")
        try:
            from tesseract.integrations import unregister_channel

            unregister_channel(bridge.name)
        except Exception:
            log.exception("telegram bridge unregister on shutdown failed")
    watcher = app.get("config_watcher")
    if watcher is not None:
        try:
            await watcher.stop()
        except Exception:
            log.exception("config_watcher.stop on shutdown failed")
    governor = app.get("autonomy_governor")
    if governor is not None:
        try:
            await governor.stop()
        except Exception:
            log.exception("autonomy_governor.stop on shutdown failed")
    kernel = app.get("autonomy_kernel")
    if kernel is not None:
        try:
            from tesseract.orchestrator.autonomy.publishers import set_active_bus
            set_active_bus(None)
            await kernel.stop()
        except Exception:
            log.exception("autonomy_kernel.stop on shutdown failed")
    # Phase 3 — release the process-global worker broadcast hook so a
    # subsequent app lifecycle (test runner, hot-reload) doesn't broadcast
    # through a closure that still references this dead `app`.
    try:
        from tesseract.orchestrator.workers.broadcast import set_worker_broadcast_hook
        set_worker_broadcast_hook(None)
    except Exception:
        log.exception("set_worker_broadcast_hook(None) on shutdown failed")
    scheduler = app.get("scheduler")
    if scheduler is not None:
        try:
            await scheduler.stop()
        except Exception:
            log.exception("scheduler.stop on shutdown failed")
    subscriber = app.get("observer_subscriber")
    if subscriber is not None:
        try:
            await subscriber.detach()
        except Exception:
            log.exception("observer_subscriber.detach on shutdown failed")
    await _drain_warmup_tasks(app)
    _unload_local_whisper(app)
    _unload_kokoro(app)
    pty_manager = app.get("pty_manager")
    if pty_manager is not None:
        try:
            await pty_manager.cleanup_all()
        except Exception:
            log.exception("pty_manager.cleanup_all on shutdown failed")
    try:
        from tesseract.orchestrator.browser.manager import get_browser_manager
        await get_browser_manager().shutdown()
    except Exception:
        log.exception("browser_manager shutdown failed")
    await _stop_owned_ollama(app)
    # Close the supervisor's keepalive client after we're done with it
    # (status probes inside _stop_owned_ollama still need it). External
    # Ollama instances stay running; the close only unwinds Mirror's
    # client-side connection pool to localhost:11434.
    sup = app.get("ollama_supervisor")
    if sup is not None and hasattr(sup, "aclose"):
        try:
            await sup.aclose()
        except Exception:
            log.exception("ollama supervisor aclose on shutdown failed")
    bundle = app.get("memory_bundle")
    embeddings = getattr(bundle, "embeddings", None)
    if embeddings is not None and hasattr(embeddings, "aclose"):
        try:
            await embeddings.aclose()
        except Exception:
            log.exception("embeddings client aclose on shutdown failed")
    uninstall_log_forwarder(app.get("log_forwarder"))


async def reattach_operator_panes(*, list_fn, pty_open_fn) -> None:
    """Re-open viewer panes for operator-facing controller sessions
    after a backend restart. Background (autonomy/scheduler) sessions are
    listed but not re-paned.

    Design (a): each pane open is individually guarded so one bad session
    never prevents the remaining sessions from being reattached.

    Note: ``_open_for_agent`` requires a primary operator WS to be connected.
    On a fresh boot before the operator has connected, each call may
    fail gracefully (PTYManager logs + returns an error dict). The outer
    call site in ``_init_background`` wraps the whole call in try/except
    as an additional safety net; the operator's next connect + the
    session-list endpoint covers any sessions that didn't reattach.
    """
    from tesseract.mirror.server.routes.controller_sessions import OPERATOR_FACING_ORIGINS

    for rec in list_fn():
        if str(getattr(rec, "origin", "")) in OPERATOR_FACING_ORIGINS:
            try:
                await pty_open_fn("open", {
                    "name": f"ctrl-{rec.session_id}",
                    "command": ["agent", "--session", rec.session_id],
                })
            except Exception:
                log.exception(
                    "reattach_operator_panes: failed to open pane for session %s",
                    rec.session_id,
                )


async def _close_all_websockets(app: web.Application) -> None:
    """Close every open Mirror WS so the per-session `websocket_handler`
    finally-block can run autosave + cleanup before the process exits.
    Without this, aiohttp's own teardown closes sockets but per-session
    cleanup may race with `loop.close()` and lose in-flight autosaves."""
    sessions = list((app.get("server_sessions") or {}).values())
    if not sessions:
        return
    for sess in sessions:
        ws = getattr(sess, "ws", None)
        if ws is None or ws.closed:
            continue
        try:
            await ws.close(code=1001, message=b"mirror shutting down")
        except Exception:
            log.exception(
                "ws.close on shutdown failed for %s",
                getattr(sess, "session_id", "?"),
            )


async def _stop_owned_ollama(app: web.Application) -> None:
    """Terminate the Ollama daemon if Mirror itself spawned it. External
    instances (operator started Ollama in a terminal or system tray) are
    left running — we only stop what we own. Soft contract: shutdown
    cleans up Mirror-side state; processes outside that boundary stay."""
    sup = app.get("ollama_supervisor")
    if sup is None:
        return
    try:
        status = await sup.status()
    except Exception:
        log.exception("ollama status probe failed during shutdown")
        return
    if not status.owned_by_mirror:
        return
    try:
        ok, msg = await sup.stop()
        if ok:
            log.info("ollama: stopped Mirror-owned daemon (%s)", msg)
        else:
            log.warning("ollama: failed to stop owned daemon: %s", msg)
    except Exception:
        log.exception("ollama supervisor stop on shutdown failed")


def _unload_kokoro(app: web.Application) -> None:
    """Release the Kokoro ORT session + cached style vectors at Mirror
    shutdown. The CUDA arena is released on GC of the underlying
    InferenceSession; clearing the module cache breaks the reference
    cycle so it actually collects."""
    engine = app.get("tts_engine")
    if engine is None or not hasattr(engine, "unload_kokoro"):
        return
    try:
        engine.unload_kokoro()
    except Exception:
        log.exception("voice: Kokoro unload failed")


def _unload_local_whisper(app: web.Application) -> None:
    engine = app.get("stt_engine")
    if engine is None or not hasattr(engine, "unload_local"):
        return
    try:
        engine.unload_local()
        log.info("voice: local Whisper model cache cleared")
    except Exception:
        log.exception("voice: local Whisper unload failed")


async def _drain_warmup_tasks(app: web.Application) -> None:
    """Cancel any in-flight model warm-ups so Mirror shutdown is clean.

    Threads spawned by `asyncio.to_thread` cannot be interrupted mid-load —
    cancelling the wrapping task only discards its result; the thread keeps
    running until the load completes or process exit kills it. That's
    acceptable: the operator has already asked for shutdown, so we just
    don't block on long-running model loads."""
    pending = [t for t in app.get("_warmup_tasks", []) if not t.done()]
    # The serial drainer is tracked separately (only one may exist — see
    # `_ensure_serial_drainer`) and so is missed by the list above. Left
    # running, shutdown tears down the voice engines while it is still
    # building an ONNX session against them.
    serial = app.get("serial_warmup_task")
    if serial is not None and not serial.done():
        pending.append(serial)
    # Anything still queued is dropped rather than run: the operator has
    # asked to exit, and closing the coroutines keeps "never awaited"
    # warnings out of the shutdown log they would be read in.
    #
    # Emptied IN PLACE and taken before the cancel below, for two reasons.
    # The drainer captured this exact list object when it started, so
    # rebinding the key would leave it popping from the old one; and taking
    # the entries first means the drainer cannot pop one in the same tick
    # this loop closes it, which would raise "cannot close a running
    # coroutine" in the middle of an otherwise clean shutdown.
    queue = app.get("_serial_warmups") or []
    queued = list(queue)
    queue.clear()
    for _name, coro in queued:
        coro.close()
    if not pending:
        return
    for t in pending:
        t.cancel()
    try:
        await asyncio.wait(pending, timeout=2.0)
    except Exception:
        log.exception("warmup drain failed — continuing shutdown")


def _load_env() -> None:
    # Call-time resolution: `boot.ENV_PATH` is frozen at first import of
    # `tesseract.brain.boot`, which happens before an app-update-relocated
    # `TESSERACT_HOME` is guaranteed to be visible. `home_dir()` re-resolves
    # the env var on every call instead.
    from tesseract.paths import home_dir

    # `interpolate=False` (env_file.INTERPOLATE): dotenv's default expands
    # `${NAME}` inside a value, so a credential containing that sequence
    # would silently load as something else — or as nothing, when the name it
    # names is unset. This file holds secrets, not shell, and the settings
    # view reads it back through the same switch so what it shows as set is
    # what the process is running on.
    from tesseract.env_file import INTERPOLATE

    load_dotenv(home_dir() / ".env", interpolate=INTERPOLATE)


def _regenerate_kb_roles_summary() -> None:
    """Ensure ``vault/knowledge-base/roles/SUMMARY.md`` reflects the
    current catalog at boot. Fail-soft: a broken catalog YAML must not
    block Mirror startup — the catalog loader downstream will surface
    the same error with a precise message.
    """
    try:
        from tesseract.scripts.regenerate_roles_summary import regenerate

        regenerate()
    except Exception:
        log.exception("kb roles SUMMARY regenerate failed at boot")


def _build_workspace_event_store():
    from tesseract.paths import TESSERACT_HOME
    from tesseract.workspace_events import EventStore

    # TESSERACT_HOME is the user-state root (runtime logs, memory-store,
    # sessions) — same place the consolidator/sweep jobs and chat.py drain
    # use. Sharing the dir means all three see the same events.jsonl.
    return EventStore(home_logs_root())


def _build_conversation_store():
    from tesseract.integrations._conversation_store import ConversationStore

    # Shared per-channel JSONL writer (MO-9-10). Path resolution happens
    # at call time inside the store, so TESSERACT_HOME env changes
    # reflect on the next append without rebuilding the singleton.
    return ConversationStore()


def _load_channels_config():
    """Load the typed ``channels.yaml`` at boot.

    Boot-time validation failure raises ``RuntimeError`` per the hard
    rule against silent infrastructure defaults; an absent file
    legitimately falls back to the built-in defaults inside the loader.
    """
    from tesseract.integrations._channels_config import load_channels_config

    return load_channels_config()


def _try_build_tool_registry(policy=None, app=None):
    """STAGE 1's registry build — and the boot's single chat_brain resolution.

    Resolved here rather than inside `build_tool_registry`, and parked on the
    app so STAGE 2's `_build_chat_infra` consumes it instead of resolving
    again. The resolve builds one SDK client per chain entry and cost ~3.6s
    twice per boot for two copies of the same answer.
    """
    chat_runtime = None
    try:
        from tesseract.brain.boot import resolve_chat_brain_runtime

        chat_runtime = resolve_chat_brain_runtime()
    except Exception as exc:  # noqa: BLE001
        # Not a failure here: `build_tool_registry` and `_build_chat_infra`
        # each own a degraded path for an unresolvable chain, and both are
        # reached by leaving this None so they re-ask and get their own
        # exception. Re-asking is cheap — a chain nobody can build constructs
        # no clients.
        log.info("chat_brain: boot resolve failed (%s) — substrates will degrade", exc)
    if app is not None:
        app["chat_runtime"] = chat_runtime
    try:
        from tesseract.brain.boot import build_tool_registry

        registry, mood, bundle, alarm_registry = build_tool_registry(
            policy=policy, app=app, chat_runtime=chat_runtime,
        )
        log.info("tool_registry loaded: %d tools", len(registry.tools))
        return registry, mood, bundle, alarm_registry
    except Exception:
        log.exception("tool_registry unavailable — /api/tools will return []")
        return None, None, None, None


async def _connect_mcp_clients(app: web.Application, registry) -> None:
    """capability-growth Phase 2 — connect curated outbound MCP servers and
    register their tools into the live registry. No-op when the registry never
    built (STAGE 1 failure) or when ``mcp_servers.yaml`` enables no servers.
    Never raises: the caller's ``gather(return_exceptions=True)`` logs, but a
    swallow here keeps a config typo from taking down STAGE 2.
    """
    if registry is None:
        log.info("mcp_client: tool_registry unavailable — skipping outbound connect")
        return
    try:
        from tesseract.mcp_client import MCPClientManager

        policy = app["config"].permissions
        manager = MCPClientManager.from_yaml(registry, policy)
        await manager.connect_all()
        app["mcp_clients"] = manager
        log.info(
            "mcp_client: outbound connect complete — %d external tool(s) registered",
            len(manager.connected_tool_names()),
        )
    except Exception:
        log.exception("mcp_client: outbound connect failed — no external tools registered")


def _ollama_slots_safe() -> list[tuple[str, Any]]:
    from tesseract.brain.boot import ollama_slots

    try:
        return ollama_slots()
    except Exception:  # noqa: BLE001 — a broken catalog surfaces on its own path
        return []


async def _report_ollama_state(base_url: str, running: bool) -> None:
    """Say out loud what an absent Ollama costs, once, at boot.

    Ollama is ours — provisioning installs it — so its absence is not the
    operator forgetting a login, it is something that broke. Two different
    failures with two different remedies: the daemon is gone (reinstall or
    start it), or the daemon is up but a model was never pulled (fetch it).
    Both log at ERROR because that is the threshold `log_forwarder.py`
    forwards to the pulse; at WARNING this reaches a terminal nobody is
    reading. Neither stops boot.
    """
    slots = _ollama_slots_safe()
    if not slots:
        return

    if not running:
        wired = ", ".join(f"{slot} ({ref.model.model})" for slot, ref in slots)
        log.error(
            "Ollama is not running at %s and could not be started — %s will not "
            "be served. Semantic search falls back to keyword matching and any "
            "role wired to Ollama falls through to its fallbacks; memory writes "
            "are unaffected. Reinstall or start it from Settings → Local models.",
            base_url, wired,
        )
        return

    from tesseract.memory.ollama_boot import _model_present, fetch_tags

    fetched = await fetch_tags(base_url)
    if not fetched.ok:
        # NOT "models are not pulled". The daemon answered the liveness probe
        # and then failed to list its tags, so which models exist is unknown —
        # and a slow daemon under load is the common case. Saying "missing"
        # here sent the operator hunting for a model that was installed.
        wired = ", ".join(f"{slot} ({ref.model.model})" for slot, ref in slots)
        log.error(
            "Ollama is running at %s but its model list could not be read (%s). "
            "Which of %s are pulled is unknown — this is a daemon that is up "
            "but not answering, not a missing model. Check it from Settings → "
            "Local models; retrieval falls back to keyword matching until it "
            "responds.",
            base_url, fetched.error, wired,
        )
        return

    missing = [
        f"{slot} ({ref.model.model})"
        for slot, ref in slots
        if not _model_present(fetched.tags, ref.model.model)
    ]
    if missing:
        log.error(
            "Ollama is running at %s but these models are not pulled: %s. "
            "Fetch them from Settings → Local models, or run "
            "`python -m tesseract.scripts.ensure_ollama`.",
            base_url, ", ".join(missing),
        )


async def _ensure_ollama_ready(app: web.Application) -> None:
    """Probe Ollama + optionally auto-start; report on failure, never raise.

    Must run before `build_memory_bundle()` so the embeddings index is wired
    when the daemon is reachable. Fail-open: Mirror starts regardless.

    A failure logs at ERROR, not WARNING, because that is the threshold
    `log_forwarder.py` forwards to the pulse — below it the operator learns
    that retrieval went keyword-only only by tailing a terminal.

    Also constructs the `OllamaSupervisor` so the Settings panel start/stop
    toggle has a stable handle. When this path auto-starts Ollama itself,
    the supervisor records the spawned Popen so a later UI "stop" knows
    it owns the process. When Ollama is already up (operator started it
    externally), the supervisor records no Popen and the stop action
    refuses with a 409 — Mirror won't kill processes it doesn't own.
    """
    supervisor = None
    try:
        from tesseract.brain.boot import load_embeddings_cfg
        from tesseract.memory.ollama_boot import _probe
        from tesseract.mirror.server.ollama_supervisor import OllamaSupervisor

        # Any slot, not just embeddings. Keying this on the embeddings role
        # meant a config that served embeddings elsewhere and pointed a chat
        # role at Ollama got no auto-start, no report, and — because the
        # supervisor is built here and nowhere else — a Settings → Local
        # models panel that 503s, which is the panel the failure message
        # tells the operator to go and use.
        slots = _ollama_slots_safe()
        if not slots:
            return
        conn = slots[0][1].connection
        base_url = conn.base_url
        if not base_url:
            return
        auto_start = bool(conn.extra.get("auto_start", True))

        # The panel's row is embedding-specific, so it gets the embedding
        # model only when Ollama is the one serving it.
        embed_cfg = load_embeddings_cfg()
        embedding_model = (
            embed_cfg["model"]
            if embed_cfg and embed_cfg.get("provider") == "ollama"
            else ""
        )
        supervisor = OllamaSupervisor(base_url=base_url, embedding_model=embedding_model)
        if auto_start:
            running, _reason = await supervisor.start()
        else:
            running = await _probe(base_url, timeout_s=2.0)
        app["ollama_supervisor"] = supervisor
        await _report_ollama_state(base_url, running)
    except Exception:
        log.exception("ollama readiness probe failed — continuing fail-open")
        # Supervisor was constructed but never reached `app[...]`. Close
        # its httpx client so a startup-path failure doesn't leak a
        # keepalive pool with no shutdown owner. Identity, not key presence:
        # `_on_startup` seeds `app["ollama_supervisor"] = None`, so the key
        # is always there and a presence check never fired.
        if supervisor is not None and app.get("ollama_supervisor") is not supervisor:
            try:
                await supervisor.aclose()
            except Exception:
                log.exception("ollama supervisor aclose on startup-fail failed")


async def _prepare_mcp_server(app: web.Application) -> None:
    """Construct the MCP server from ``mcp.yaml`` — if it is switched on.

    Cheap disk read; config-as-authority means a malformed ``mcp.yaml`` raises
    loudly here (same contract as ``load_server_config``). Routes were
    registered in ``_register_routes``; this gives their handlers a live
    instance.

    ``server.enabled: false`` is the shipped default and builds nothing: no
    approval registry, no ask timer, no session sweep. ``app["mcp_server"]``
    stays ``None``, which the ``/mcp`` handlers already read as nothing to
    serve. The CONFIG is still loaded and kept, because Settings renders the
    address and the verb list from it whether the door is open or not — what
    the surface would be is not a secret, and hiding it would leave the
    operator turning on something undescribed.
    """
    from tesseract.config.mcp import load_mcp_config
    from tesseract.mirror.server.mcp import MCPServer
    from tesseract.mirror.server.mcp.approvals import (
        MCPApprovalRegistry,
        build_verb_ask_fn,
    )
    from tesseract.orchestrator.agent_controller.lanes import mcp_provision
    from tesseract.orchestrator.background_event_bus import get_background_bus

    mcp_cfg = load_mcp_config()
    app["mcp_config"] = mcp_cfg
    # Before the switch, not after: the runtime identities are what a lane and
    # a terminal are given when they spawn, and a boot that skipped minting
    # them would leave the operator turning MCP on and still finding it dead.
    await asyncio.to_thread(mcp_provision.ensure_runtime_tokens, mcp_cfg)
    if not mcp_cfg.server.enabled:
        log.info("mcp: off (mcp.yaml server.enabled) — /mcp serves nothing")
        return

    approvals = MCPApprovalRegistry()
    app["mcp_approvals"] = approvals

    def _emit_mcp_approval(approval_id, verb, client):  # noqa: ANN001,ANN202
        get_background_bus().publish(
            "mcp_approval_requested",
            {
                "channel": "mcp_approval",
                "approval_id": approval_id,
                "verb": verb,
                "client": client.name,
                "trust_tier": client.trust_tier,
            },
        )

    verb_ask_fn = build_verb_ask_fn(
        approvals, _emit_mcp_approval, mcp_cfg.server.ask_hold_timeout_s
    )
    mcp_server = MCPServer(mcp_cfg, verb_ask_fn=verb_ask_fn)
    await mcp_server.start(app)
    app["mcp_server"] = mcp_server


async def _warm_tls_trust_store() -> None:
    """Build the process-wide TLS context before anything needs it.

    `http_client.ssl_context()` loads a CA bundle — ~0.8s of synchronous CPU
    on this machine, and it is the same 0.8s whoever pays it. Left to the
    first caller it lands inside a request handler, on the loop, which is
    exactly the >50ms rule. Here it lands in a thread during boot, where it
    costs nothing and every client built afterwards is free.
    """
    from tesseract import http_client

    try:
        await asyncio.to_thread(http_client.ssl_context)
        log.info("tls: trust store built and shared")
    except Exception:
        log.exception("tls: trust store build failed — clients will build their own")


async def _warm_embeddings(bundle) -> None:
    """Pre-warm the embedding model so the first dedupe doesn't pay cold-load tax.

    Embeddings only. This awaits an HTTP call to Ollama, which yields the
    loop properly, so it belongs on the concurrent path. The reranker does
    NOT — it builds an ONNX session, which holds the GIL — and is warmed by
    `_warm_reranker`, queued with the other ONNX loads.
    """
    if bundle is None or bundle.embeddings is None:
        return
    try:
        await bundle.embeddings.warm_up()
        log.info("embedding model warmed and pinned")
    except Exception:
        log.exception("warm_up failed — continuing without warm embeddings")


async def _warm_reranker(bundle) -> None:
    """Load the reranker's ONNX session. Queued, never raced.

    Left on the concurrent path this was the last blocking stretch at boot:
    measured after the voice lanes were serialised, its warm-up was the only
    remaining event-loop breach, at 5.34s. It is the same defect as Kokoro's —
    an onnxruntime session constructor that does not release the GIL — so it
    gets the same treatment.

    Warms regardless of whether embeddings came online: it is built from its
    own local config, so BM25-only retrieval still gets a warm reranker.

    Note it can still lose the race to a LAZY load: `retrieval.py`'s
    `_apply_reranker` builds the session on first use, and the autonomy
    kernel's resume sweep runs a retrieval during boot. When that happens
    this is a no-op and the blocking load has already been paid on the
    concurrent path — shaping what autonomy may do before the warm-ups
    finish is a separate problem from where the warm-ups are scheduled.
    """
    reranker = getattr(bundle, "reranker", None) if bundle is not None else None
    if reranker is None:
        return
    try:
        await reranker.warm_up()
    except Exception:
        log.exception("reranker warm_up failed — first retrieval loads lazily")


def _queue_serial_warmup(app: web.Application, coro, *, name: str) -> None:
    """Hold a GIL-BOUND warm-up back, to be run one at a time after boot.

    `asyncio.to_thread` only buys concurrency when the native call releases
    the GIL, and onnxruntime's session constructor does not: measured on an
    8 GB laptop, building the Kokoro session blocks the event loop for 4.6s
    of its 4.7s. CTranslate2 DOES release it — whisper's 13.9s load costs
    0.25s of lag — which is why it still goes through `_schedule_warmup` and
    is not queued here.

    So these cannot overlap each other, and running them as concurrent tasks
    never made boot shorter; it only stacked the blocked stretches into one
    long outage. A live boot showed 36.5s in a single window, during which
    the backend answered no HTTP at all and the UI reported "failed to fetch".
    Worse, the lost wall-clock ran Kokoro's own preload past its timeout, and
    the timeout latched the lane off — so a slow boot silently demoted the
    operator to the fallback voice for the rest of the session.

    Draining them serially and only after `background init complete` keeps the
    window where the UI connects answerable, at no cost to total boot time.
    """
    app.setdefault("_serial_warmups", []).append((name, coro))
    # Boot queues these before the drain is started, so nothing to kick. A
    # LATER caller — a voice-config hot reload rebuilding the runtime — has
    # missed that drain, and its warm-ups would otherwise never run.
    #
    # Exactly ONE drainer may exist. A reload queues two lanes, and kicking a
    # drainer per queued item gives two drainers that pop different entries
    # and await them at the same time — which is precisely the concurrent
    # GIL-bound session construction this whole mechanism exists to prevent,
    # reintroduced by the fix for the reload gap.
    if app.get("_serial_warmups_started"):
        _ensure_serial_drainer(app)


def _ensure_serial_drainer(app: web.Application) -> None:
    """Start the drainer if and only if one is not already running.

    The single-drainer rule is the mechanism. Two drainers pop different
    entries and await them concurrently, which is the GIL contention this
    exists to prevent — so the identity of the running task is tracked in one
    place (`serial_warmup_task`) rather than through the generic warm-up task
    list, where a second caller could not see it.

    Callable from off the loop: `_build_voice_runtime` runs under
    `asyncio.to_thread` on a hot reload, where `create_task` would raise.
    """
    if app.get("_serial_drain_active"):
        return  # it drains until empty, so it will reach the new entry

    def _spawn() -> None:
        if app.get("_serial_drain_active"):
            return  # won the race to schedule, lost the race to be first
        # Set BEFORE creating the task: the coroutine does not start running
        # until the loop schedules it, and a second caller in between must
        # not conclude that no drainer is coming.
        app["_serial_drain_active"] = True
        app["serial_warmup_task"] = asyncio.get_running_loop().create_task(
            _drain_serial_warmups(app), name="mirror-serial-warmups",
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        main_loop = app.get("_main_loop")
        if main_loop is None:
            log.warning("serial warm-ups queued with no loop to drain them")
            return
        main_loop.call_soon_threadsafe(_spawn)
        return
    _spawn()


async def _drain_serial_warmups(app: web.Application) -> None:
    """Run the queued GIL-bound warm-ups one at a time, until none are left.

    Sequential on purpose — see `_queue_serial_warmup`. Each failure is
    logged and the next still runs: these are optimisations, and one lane
    that could not preload must not stop the others from doing so.

    Drains in a loop rather than taking one snapshot, because
    `_build_voice_runtime` runs again on a voice-config hot reload and queues
    a fresh pair. A single-shot drain would leave those sitting unexecuted
    for the life of the process, and the symptom — lanes silently never
    preloading again after any Settings change — would look like nothing at
    all until the first sentence after a reload came back slow.
    """
    queue = app.setdefault("_serial_warmups", [])
    try:
        while True:
            if not queue:
                # Stand down, then look ONCE more. `task.done()` is not a safe
                # signal for the queuer: this coroutine can decide the queue is
                # empty and return while its Task is still not marked done, so
                # a reload appending right then would see a live drainer, defer
                # to it, and be stranded forever. Clearing the flag before the
                # re-check closes that window — both halves run on the loop
                # with no await between them, so the ordering is decided, not
                # hoped for.
                app["_serial_drain_active"] = False
                if not queue:
                    return
                app["_serial_drain_active"] = True
                continue
            name, coro = queue.pop(0)
            started = time.perf_counter()
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("warmup %r failed — continuing fail-open", name)
            else:
                log.info("warmup %r ready in %.2fs", name, time.perf_counter() - started)
    finally:
        app["_serial_drain_active"] = False


def _schedule_warmup(app: web.Application, coro, *, name: str) -> None:
    """Fire `coro` as a background task tracked on `app['_warmup_tasks']`.

    Failures inside the coroutine are swallowed and logged — boot must
    never crash because a model warm-up failed. Cancellation propagates
    normally (Mirror shutdown drains the list).

    Thread-safe: `_build_voice_runtime` runs under `asyncio.to_thread`,
    where there is no running loop. Calling `asyncio.create_task` there
    raised `RuntimeError: no running event loop`, which aborted the whole
    voice-runtime build BEFORE the TTS engine was constructed — leaving
    The assistant with no voice. When called off-loop we schedule the task on the
    captured main loop via `call_soon_threadsafe` instead.
    """
    async def _run() -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("warmup task %r failed — continuing fail-open", name)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        app["_warmup_tasks"].append(loop.create_task(_run(), name=f"warmup:{name}"))
        return

    main_loop = app.get("_main_loop")
    if main_loop is None:
        log.warning("warmup %r skipped — no main loop captured", name)
        coro.close()  # close the un-awaited coroutine (silences GC warning)
        return

    def _spawn() -> None:
        app["_warmup_tasks"].append(main_loop.create_task(_run(), name=f"warmup:{name}"))

    main_loop.call_soon_threadsafe(_spawn)


async def _start_telegram_bridge(app: web.Application) -> None:
    """Start the Telegram bridge if the bot token is configured.

    CR-1: also honors ``channels.yaml::telegram.enabled``. Toggling that
    key to ``false`` and reloading still requires a Mirror restart to
    *stop* an already-running bridge — live ``enabled`` cycling is
    deferred to a later phase.
    """
    channels_config = app.get("channels_config")
    if channels_config is not None:
        telegram_block = channels_config.channel_block("telegram")
        if telegram_block is not None and not telegram_block.enabled:
            log.info("telegram: channels.yaml::telegram.enabled=false; bridge disabled")
            return
    try:
        from tesseract.integrations import register_channel
        from tesseract.integrations.telegram.bridge import build_telegram_bridge

        bridge = build_telegram_bridge(app)
        if bridge is None:
            return
        await bridge.start()
        app["telegram_bridge"] = bridge
        register_channel(bridge)
        _wire_brief_push_subscriber(app, bridge)
    except Exception:
        log.exception("telegram bridge unavailable — continuing without Telegram")


def _wire_brief_push_subscriber(app: web.Application, bridge: Any) -> None:
    """MO-10-3 — install the daily-brief Telegram push subscriber.

    The subscriber reads ``channels.yaml::telegram.brief_push`` at call
    time (live-reload-safe) and pushes the exec summary to operator-tier
    chat_ids when ``broadcast_daily_brief_ready`` fires. Fail-soft on
    construction — a missing dependency here cannot block bridge startup.
    """
    try:
        from tesseract.integrations.telegram.brief_push import TelegramBriefPushSubscriber
        from tesseract.integrations.telegram.state import load_allowlist
    except Exception:
        log.exception("brief_push subscriber import failed; skipping")
        return

    bridge_state = getattr(bridge, "_state", None)
    if bridge_state is None:
        log.info("brief_push: bridge state unavailable; subscriber not wired")
        return

    def _allowlist_loader():
        try:
            return load_allowlist(bridge_state.allowlist_path)
        except Exception:
            log.exception("brief_push: allowlist load failed")
            return None

    def _config_loader():
        return app.get("channels_config")

    def _user_tier_loader():
        try:
            return dict(bridge_state.poll_state.user_tier)
        except Exception:
            return {}

    app["brief_push_subscriber"] = TelegramBriefPushSubscriber(
        bridge=bridge,
        event_store=app.get("workspace_event_store"),
        config_loader=_config_loader,
        allowlist_loader=_allowlist_loader,
        user_tier_loader=_user_tier_loader,
    )


async def _run_recovery(app: web.Application) -> None:
    """AU-2 broad reconciler — runs in STAGE 1.

    Broad, mostly-read scan over durable state — worker records,
    PTY leases, scheduler runs, agenda.
    Writes are confined to worker records (terminal-state transitions
    via `recover_worker_sync`) and PTY-lease cleanup. Emits one
    `recovery_summary` workspace event the Mirror UI consumes via the
    existing inbox.

    Sets ``app["recovery_state"]="recovering"`` for the duration so
    ``/api/health`` returns 503 + ``{state: "recovering"}`` until the
    pass completes (supervisor heartbeat tolerates ~90s of 503s). On
    success the state flips to ``"ready"`` and the route returns 200.

    Fail-open per `_start_scheduler` convention: any exception is
    logged and Mirror continues booting (recovery_state still flips to
    ready so the supervisor doesn't loop a crash).
    """
    app["recovery_state"] = "recovering"
    summary = None
    try:
        from tesseract.orchestrator.recovery import new_recovery_manager

        rm = new_recovery_manager()
        summary = await rm.run()
        app["last_recovery_summary"] = summary
        log.info("recovery: %s", summary.to_payload())
    except Exception:
        log.exception("recovery: pass failed — continuing boot")
    finally:
        app["recovery_state"] = "ready"

    if summary is not None:
        await _send_recovery_nudge(app, summary)

    # AU-4 S2 — seed the AgendaStore on first boot so the dashboard's
    # empty state renders end-to-end. Idempotent via sentinel.
    try:
        from tesseract.orchestrator.autonomy import bootstrap_agenda
        bootstrap_agenda()
    except Exception:
        log.exception("agenda: bootstrap_agenda failed — continuing boot")


async def _send_recovery_nudge(app: web.Application, summary: Any) -> None:
    """Telegram outbound — rate-cap-exempt one-line nudge so the
    operator sees the boot summary without opening Mirror.

    Fires only when there's something worth saying: any
    operator_attention items. A clean boot with no in-flight state
    stays silent. Routes through the AU-10 :class:`OutboundNotifier`
    so the mute toggle covers recovery too; ``recovery_summary`` is in
    :data:`EXEMPT_CATEGORIES` so the rate cap never blocks it.
    """
    if not summary.operator_attention:
        return
    text = _format_recovery_telegram(summary)
    try:
        notifier = _get_outbound_notifier(app)
        result = await notifier.notify("recovery_summary", {"text": text})
        log.info("recovery: telegram nudge result=%s", result)
    except Exception:
        log.exception("recovery: telegram nudge failed (best-effort)")


def _format_recovery_telegram(summary: Any) -> str:
    """One-line Telegram body. Mirrors the summary.text formatter but
    front-loads operator-attention count for phone visibility."""
    line = (
        f"<b>Recovery</b> · boot {summary.boot_id[-9:]}"
    )
    attn = len(summary.operator_attention)
    if attn:
        line += f" · {attn} need{'s' if attn == 1 else ''} operator"
    return line


async def _start_scheduler(app: web.Application) -> None:
    """Build and start the SchedulerEngine; fail-open so Mirror always starts."""
    try:
        from tesseract.scheduler.engine import SchedulerEngine

        scheduler = SchedulerEngine(config_dir=CONFIG_DIR)
        await scheduler.start(app)
        app["scheduler"] = scheduler
    except Exception:
        log.exception("scheduler unavailable — continuing without heartbeat")


async def _start_autonomy_kernel(app: web.Application) -> None:
    """AU-5 — long-lived AutonomyKernel + AU-6 Governor.

    Starts after RecoveryManager + bootstrap_agenda so the worker
    lanes are already configured. Fail-open: a missing config file or
    import error logs and continues (Mirror still boots; the kernel is
    non-essential to chat-turn IO).
    """
    try:
        from tesseract.orchestrator.autonomy import (
            Governor,
            GovernorConfig,
            PauseStore,
            build_kernel_from_configs,
        )
        from tesseract.orchestrator.autonomy.kernel_worker_runner import (
            KernelWorkerRunner,
        )
        from tesseract.orchestrator.workers.kinds import WorkerKind
        from tesseract.orchestrator.workers.lane import WorkerLane
        from tesseract.mirror.server.config import MIRROR_YAML
        import yaml as _yaml

        agenda_yaml = CONFIG_DIR / "agenda.yaml"
        mappers_yaml = CONFIG_DIR / "agenda-mappers.yaml"
        raw = _yaml.safe_load(MIRROR_YAML.read_text(encoding="utf-8")) or {}
        lanes_block = ((raw.get("mission") or {}).get("lanes") or {}).get("worker") or {}
        lane = WorkerLane.from_mission_lanes_block(lanes_block)

        # AU-6 — share one PauseStore across kernel + governor + REST routes
        # so kernel's in-memory cache, the durable file, and the operator
        # unpause endpoint all agree. ``routes/agenda::register`` already
        # populated a default; replace it with the same instance the kernel
        # holds so reads after boot return the live state.
        pause_store = app.get("autonomy_pause_store") or PauseStore()
        app["autonomy_pause_store"] = pause_store

        agenda_raw = _yaml.safe_load(agenda_yaml.read_text(encoding="utf-8")) or {}
        # AU-20 follow-up — wire the live tool registry into the
        # autonomy runner so selected agenda items dispatch to real
        # delegate_coder / delegate_auditor / invoke_agent calls. Falls
        # back to the noop runner if the registry isn't ready yet
        # (e.g. tool_registry boot raised earlier and we're still
        # booting fail-open).
        tool_registry = app.get("tool_registry")
        worker_timeouts_raw = agenda_raw.get("worker_timeouts") or {}
        worker_timeouts: dict[WorkerKind, float] = {}
        # delegate_coder / delegate_auditor Pydantic schemas constrain
        # timeout to 10–1800. Clamp at load time with a warning so a
        # typo'd 18000 surfaces as a startup log line, not a per-call
        # validation failure that wastes a dispatch slot.
        _TIMEOUT_MIN = 10.0
        _TIMEOUT_MAX = 1800.0
        for kind_str, seconds in worker_timeouts_raw.items():
            try:
                kind = WorkerKind(kind_str)
            except ValueError:
                log.warning("agenda.yaml worker_timeouts: unknown kind %r", kind_str)
                continue
            try:
                raw_val = float(seconds)
            except (TypeError, ValueError):
                log.warning(
                    "agenda.yaml worker_timeouts: non-numeric value for %s: %r",
                    kind_str, seconds,
                )
                continue
            clamped = max(_TIMEOUT_MIN, min(_TIMEOUT_MAX, raw_val))
            if clamped != raw_val:
                log.warning(
                    "agenda.yaml worker_timeouts: %s value %s out of "
                    "%.0f-%.0fs range; clamped to %.0f",
                    kind_str, raw_val, _TIMEOUT_MIN, _TIMEOUT_MAX, clamped,
                )
            worker_timeouts[kind] = clamped

        async def _on_worker_timeout(record):  # type: ignore[no-untyped-def]
            """Telegram-ping the operator when an autonomy worker exhausts
            its wallclock budget. The worker is BLOCKED (terminal) — the
            agenda item is parked and the operator must re-queue it
            manually after extending the budget. No auto-resume today."""
            notifier = _get_outbound_notifier(app)
            if notifier is None:
                return
            try:
                await notifier.notify("awaiting_operator", {
                    "agenda_id": record.agenda_item_id,
                    "goal": (record.prompt or "")[:200],
                    "rationale": (
                        f"worker {record.id} hit its {record.duration_seconds:.0f}s "
                        f"wallclock budget and stopped. Item is BLOCKED — to retry: "
                        f"raise agenda.yaml::worker_timeouts.{record.kind.value} "
                        f"and re-add the agenda item via POST /api/agenda, "
                        f"or cancel."
                    ),
                })
            except Exception:  # noqa: BLE001
                log.exception("autonomy: worker-timeout notify failed")

        worker_runner = (
            KernelWorkerRunner(
                tool_registry,
                worker_timeouts=worker_timeouts,
                timeout_notifier=_on_worker_timeout,
            )
            if tool_registry is not None
            else None
        )
        # F6 — global daily USD ceiling for autonomous dispatch. The kernel
        # reads today's total system spend from the cost ledger; None when the
        # ledger failed to build (fail-open boot) so the USD cap is skipped
        # rather than crashing the kernel.
        _cost_ledger = app.get("cost_ledger")

        def _daily_usd_spent() -> float:
            return float(_cost_ledger.snapshot()["global"]["spent_usd"])

        kernel = build_kernel_from_configs(
            agenda_yaml=agenda_yaml,
            mappers_yaml=mappers_yaml,
            worker_lane=lane,
            pause_store=pause_store,
            worker_runner=worker_runner,
            daily_usd_spent=_daily_usd_spent if _cost_ledger is not None else None,
        )
        await kernel.start()
        app["autonomy_kernel"] = kernel
        from tesseract.orchestrator.autonomy.broadcast import broadcast_agenda_event
        from tesseract.orchestrator.autonomy.publishers import set_active_bus
        set_active_bus(kernel.bus)

        # Phase 2 — fan kernel-internal agenda mutations to WS. Each transition
        # / add inside the kernel tick (selection, completion, repair) was
        # previously invisible to the operator until they refreshed manually.
        # Route handlers keep their own manual broadcast calls (different
        # AgendaStore instance), so this hook does not double-fire.
        def _agenda_broadcast_hook(event_type, item, extras):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return  # no running loop → no WS broadcast possible
            prior_status = extras.get("prior_status") if extras else None
            try:
                loop.create_task(
                    broadcast_agenda_event(
                        app, event_type, item, prior_status=prior_status
                    ),
                    name=f"agenda_broadcast:{event_type}:{item.id}",
                )
            except RuntimeError:
                # Loop is stopping/closed during shutdown — drop the broadcast
                # silently. Matches the cost-ledger broadcast hook pattern.
                return

        kernel._agenda.set_broadcast_hook(_agenda_broadcast_hook)

        # Phase 3 — fan worker_record_* envelopes from every write_record /
        # archive_record callsite (kernel, governor, cancel, recovery,
        # worker_dispatch, kernel_worker_runner). The hook is process-global
        # because workers/record.py uses module-level write functions.
        from tesseract.orchestrator.workers.broadcast import (
            broadcast_worker_event,
            set_worker_broadcast_hook,
        )

        def _worker_broadcast_hook(event_type, record):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            try:
                loop.create_task(
                    broadcast_worker_event(app, event_type, record),
                    name=f"worker_broadcast:{event_type}:{record.id}",
                )
            except RuntimeError:
                return

        set_worker_broadcast_hook(_worker_broadcast_hook)

        # Phase 4 — fan governor pause + tick envelopes to WS.
        from tesseract.orchestrator.autonomy.broadcast import (
            broadcast_governor_event,
        )

        def _governor_pause_hook(event_type, payload):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            try:
                loop.create_task(
                    broadcast_governor_event(app, event_type, payload),
                    name=f"governor_broadcast:{event_type}",
                )
            except RuntimeError:
                return

        def _governor_tick_hook(payload):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            try:
                loop.create_task(
                    broadcast_governor_event(app, "governor_tick", payload),
                    name="governor_broadcast:governor_tick",
                )
            except RuntimeError:
                return

        # `pause_store` is in scope here; `governor` is constructed below.
        # Wire pause_store now; tick hook waits until after Governor exists.
        pause_store.set_broadcast_hook(_governor_pause_hook)
        log.info(
            "autonomy_kernel: started (fallback wake=%.1fs top_k=%d)",
            kernel.config.tick_interval_seconds,
            kernel.config.top_k,
        )

        # AU-6 — Governor runs alongside the kernel. Detectors operate on
        # disk state (AgendaStore + worker records) so the governor needs
        # no event bus subscription; cadence + on-demand are enough.
        governor = Governor(
            agenda_store=kernel._agenda,
            pause_store=pause_store,
            config=GovernorConfig.from_yaml_dict(agenda_raw),
            notify_fn=_make_governor_notify(app),
            kernel_pause_hook=lambda src, reason: kernel._paused_sources.add(src),
        )
        await governor.start()
        # Phase 4 — wire the tick hook now that Governor is constructed.
        governor.set_tick_broadcast_hook(_governor_tick_hook)
        app["autonomy_governor"] = governor

    except Exception:
        log.exception("autonomy_kernel: failed to start — continuing boot")


def _get_outbound_notifier(app: web.Application):
    """Lazy-build the shared :class:`OutboundNotifier` on first access.

    AU-10 — every autonomous Telegram path (governor pause, upgrade
    restart, agenda transition, recovery summary, crash-storm latch)
    routes through this notifier so rate caps + mute toggles stay in
    one place. Exempt categories still bypass the cap inside
    :meth:`OutboundNotifier.notify`.
    """
    existing = app.get("outbound_notifier")
    if existing is not None:
        return existing
    from tesseract.orchestrator.autonomy.outbound import OutboundNotifier

    notifier = OutboundNotifier(
        bridge_getter=lambda: app.get("telegram_bridge"),
        channels_config_getter=lambda: app.get("channels_config"),
    )
    app["outbound_notifier"] = notifier
    return notifier


def _make_governor_notify(app: web.Application):
    """Build the outbound notify closure handed to the Governor.

    Routes through :class:`OutboundNotifier` so the dashboard's mute
    toggle covers governor pauses; ``governor_pause`` is NOT exempt per
    GOVERNANCE §9 + the AU-10 phase doc, so the per-hour cap applies.
    """

    async def notify(pause) -> None:
        notifier = _get_outbound_notifier(app)
        try:
            result = await notifier.notify(
                "governor_pause",
                {
                    "source": pause.source.value,
                    "detector": pause.detector,
                    "reason": pause.reason,
                },
            )
            log.info(
                "governor: telegram nudge for %s result=%s",
                pause.source.value, result,
            )
        except Exception:
            log.exception("governor: telegram nudge failed (best-effort)")

    return notify


async def _start_config_watcher(app: web.Application) -> None:
    """Phase 18 — observe `tesseract/config/*.yaml` and dispatch reloaders.

    Initialises `app['vault_config']` so live consumers can read it
    without nesting `if app.get(...) is None`. Fail-open: a missing
    `watchdog` install or filesystem-watch error logs and continues.
    """
    try:
        from tesseract.brain.boot import load_vault_config
        app["vault_config"] = load_vault_config()
    except Exception:
        log.exception("config_watcher: initial vault_config load failed (continuing)")

    try:
        from tesseract.mirror.server.config_watcher import ConfigWatcher, default_reloaders

        watcher = ConfigWatcher(
            app=app,
            config_dir=CONFIG_DIR,
            reloaders=default_reloaders(),
        )
        await watcher.start()
        app["config_watcher"] = watcher
    except Exception:
        log.exception("config_watcher unavailable — external yaml edits will not hot-reload")


def _config_reload_toasts_enabled(config: ServerConfig) -> bool:
    """Read `mirror.yaml ui.show_config_reload_toasts`. Default True."""
    ui = (config.models.get("ui") if hasattr(config, "models") else None) or {}
    # `ui` lives under `mirror.yaml`; ServerConfig only carries the
    # synthesized models view today, so re-read mirror.yaml here.
    try:
        from tesseract.mirror.server.config import MIRROR_YAML
        import yaml as _yaml

        raw = _yaml.safe_load(MIRROR_YAML.read_text(encoding="utf-8")) or {}
        ui_block = raw.get("ui") or {}
        flag = ui_block.get("show_config_reload_toasts", True)
        return bool(flag)
    except Exception:
        return True




def _try_build_cost_ledger():
    try:
        from tesseract.brain.boot import build_cost_ledger

        ledger = build_cost_ledger()
        log.info("cost_ledger loaded: cap=$%.2f warning=$%.2f per_role=%s",
                 ledger.cap_usd, ledger.warning_usd,
                 ", ".join(f"{r}=${c:.2f}" for r, c in ledger.per_role_caps.items()) or "(none)")
        return ledger
    except Exception:
        log.exception(
            "cost_ledger unavailable — HUD cost chips will stay empty and chat/voice "
            "turns will run UNBILLED. Check providers.yaml `cost_tracking` block.",
        )
        return None


def _wire_cost_broadcast(app: web.Application) -> None:
    """Subscribe a WS fan-out callback to the CostLedger. Fires a `cost_delta`
    envelope to every connected Mirror session after each recorded turn.

    The ledger fires subscribers synchronously from the caller's thread — a
    chat/observer turn running inside the aiohttp event loop. We schedule the
    async fan-out via `loop.create_task(...)`; if no running loop is found
    (e.g. CLI / REPL call stack), the callback logs and drops. Subscriber
    exceptions are already swallowed by the ledger itself.
    """
    ledger = app.get("cost_ledger")
    if ledger is None:
        return

    def _broadcast(event: Any, state: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # not inside the server loop — e.g. unit test, REPL
        try:
            loop.create_task(_async_broadcast_cost(app, event, state))
        except RuntimeError:
            # Loop stopping / closed during shutdown — drop silently.
            return

    ledger.subscribe(_broadcast)


async def _async_broadcast_cost(app: web.Application, event: Any, state: Any) -> None:
    from tesseract.mirror.server.envelope import (
        make_cost_delta,
        make_cost_warning,
    )
    from tesseract.mirror.server.session import send_envelope

    sessions = app.get("server_sessions") or {}
    if not sessions:
        return

    # Cost UX overhaul: after each `record()` / `record_voice()`, check
    # whether spend just crossed 75% of any cap (global / role / voice
    # provider) and fire a one-shot `cost_warning` envelope per scope
    # per day. `ledger.check_warning` is idempotent — repeated calls
    # the same day return False after the first True.
    ledger = app.get("cost_ledger")
    warnings_to_emit: list[dict[str, Any]] = []
    if ledger is not None:
        # Global ceiling: applies to chat AND voice spend (rolled-up).
        if ledger.check_warning("global", state.spent_usd, state.cap_usd):
            warnings_to_emit.append({
                "scope_key": "global",
                "scope_label": "global daily budget",
                "spent_usd": state.spent_usd,
                "cap_usd": state.cap_usd,
            })
        # Per-role / per-voice-provider sub-cap. The role identifier lives
        # on the CostEvent (BudgetState has no `role` field). Older code
        # read it from BudgetState and raised AttributeError on every
        # chat record, which silently dropped the entire cost_delta
        # broadcast and froze the HUD chips at their last persisted
        # value; owner caught this 2026-04-29. `event.role` carries
        # `chat_brain`/`observer_agent` for chat events and
        # `voice_tts`/`voice_stt` for voice events.
        if state.role_cap_usd is not None:
            role = event.role
            scope_key = role if role.startswith("voice_") else f"role:{role}"
            scope_label = role
            if ledger.check_warning(scope_key, state.role_spent_usd, state.role_cap_usd):
                warnings_to_emit.append({
                    "scope_key": scope_key,
                    "scope_label": scope_label,
                    "spent_usd": state.role_spent_usd,
                    "cap_usd": state.role_cap_usd,
                })

    for sess in sessions.values():
        sid = getattr(sess, "session_id", "")
        env = make_cost_delta(sid, event, state)
        try:
            await send_envelope(sess, env)
        except Exception:
            log.exception("cost_delta send_envelope failed for %s", sid or "?")
        for w in warnings_to_emit:
            wenv = make_cost_warning(sid, **w)
            try:
                await send_envelope(sess, wenv)
            except Exception:
                log.exception("cost_warning send_envelope failed for %s", sid or "?")


def _register_voice_dependent_tools(app: web.Application) -> None:
    """Register tools that require the voice runtime to be built first.

    `transcribe_audio` reads through the `STTEngine` instance constructed by
    `_build_voice_runtime`. Called once at startup AFTER the engine is on
    `app["stt_engine"]`. Idempotent — re-registering replaces the prior
    instance so a `_build_voice_runtime` rebuild on hot-reload picks up
    a newly constructed engine on the next config-watch cycle.
    """
    registry = app.get("tool_registry")
    stt_engine = app.get("stt_engine")
    if registry is None:
        log.info("transcribe_audio: tool_registry unavailable, skipping")
        return
    if stt_engine is None:
        log.info("transcribe_audio: STTEngine not built (no voice block?), skipping")
        return
    try:
        from tesseract.kernel.tools.transcribe_audio import TranscribeAudioTool
        registry.register(TranscribeAudioTool(stt_engine=stt_engine))
        log.info("transcribe_audio: registered against local STT engine")
    except Exception:
        log.exception("transcribe_audio: registration failed")


def _build_voice_runtime(app: web.Application) -> None:
    """Construct STTEngine + TTSEngine from ``roles.yaml voice:``.

    The voice block follows the same primary+fallbacks shape as the
    chat-brain roles. For each side (STT / TTS) the runtime walks the
    chain, branches on ``connection.adapter`` to decide which provider
    config to populate, and picks the primary by chain[0]. An adapter
    with no branch here is simply not built — the operator's chain can
    name refs this build doesn't know how to drive, and the remaining
    lanes still serve. Engine semantics:

    - STTEngine: local-first whenever a ``local_whisper`` entry exists
      anywhere in the chain. To force cloud-only, omit the
      ``local.whisper.*`` entry from the chain entirely.
    - TTSEngine: tries ``provider_key`` (the chain[0] catalog id) first,
      then every other configured lane in turn; a lane that raises is
      latched off until the operator unloads it.
    """
    try:
        from pathlib import Path

        from tesseract.brain.boot import load_voice_config
        from tesseract.voice import STTEngine, TTSEngine
        from tesseract.voice import model_files as voice_model_files
        from tesseract.voice.providers.gemini import GeminiSTTConfig
        from tesseract.voice.providers.local_whisper import LocalWhisperConfig
        from tesseract.voice.providers.gemini_tts import GeminiPreset, GeminiTTSConfig
        from tesseract.voice.providers.kokoro_tts import KokoroPreset, KokoroTTSConfig

        cfg = load_voice_config()

        # Drop prior handles BEFORE reading the new config. An operator
        # who trims the `voice:` block must not keep the old engines
        # alive — otherwise the runtime keeps debiting providers that
        # the new config already retired (Phase 18 audit M3 contract).
        app["stt_engine"] = None
        app["tts_engine"] = None

        if not cfg:
            log.info("voice: no `voice:` block in roles.yaml — voice subsystem disabled")
            return

        stt_chain = (cfg.get("stt") or {}).get("chain") or []
        tts_chain = (cfg.get("tts") or {}).get("chain") or []
        ledger = app.get("cost_ledger")

        # ── STT ─────────────────────────────────────────────────
        local_entry = next((e for e in stt_chain if e.get("adapter") == "local_whisper"), None)
        cloud_stt_entry = next((e for e in stt_chain if e.get("adapter") == "gemini"), None)

        if local_entry or cloud_stt_entry:
            local_config = None
            cloud_config = None
            if local_entry:
                local_config = LocalWhisperConfig(
                    provider=local_entry["provider"],
                    model=local_entry["model"],
                    device=local_entry["device"],
                    compute_type=local_entry["compute_type"],
                    language=local_entry.get("language"),
                    beam_size=int(local_entry.get("beam_size", 1)),
                    timeout_seconds=float(local_entry.get("timeout_seconds", 20)),
                    preload=bool(local_entry.get("preload", False)),
                )
            if cloud_stt_entry:
                cloud_config = GeminiSTTConfig(
                    model=cloud_stt_entry["model"],
                    api_key_env=cloud_stt_entry["api_key_env"],
                    prompt=cloud_stt_entry["prompt"],
                    timeout_seconds=float(cloud_stt_entry["timeout_seconds"]),
                )
            app["stt_engine"] = STTEngine(
                cloud_config=cloud_config,
                cost_ledger=ledger,
                local_config=local_config,
                cloud_provider_key=(cloud_stt_entry or {}).get("provider", "gemini_flash_audio"),
                local_provider_key=(local_entry or {}).get("provider", "local_whisper"),
            )
            primary_stt_ref = stt_chain[0]["ref"] if stt_chain else None
            log.info(
                "voice: STTEngine ready (primary=%s, local=%s, cloud=%s)",
                primary_stt_ref,
                (local_entry or {}).get("provider"),
                (cloud_stt_entry or {}).get("provider"),
            )
            # Warm whisper at boot only when it is the role primary (or
            # operator explicitly opted in via catalog preload). A local
            # provider sitting in the chain as a fallback stays cold —
            # CUDA + model load is ~30s and pollutes startup for no win
            # if cloud STT is primary. First-use will load it lazily.
            local_is_primary = bool(stt_chain) and stt_chain[0].get("adapter") == "local_whisper"
            if (
                app["stt_engine"] is not None
                and local_config is not None
                and (local_is_primary or local_config.preload)
            ):
                _schedule_warmup(app, app["stt_engine"].warm_up_local(), name="whisper")

        # ── TTS ─────────────────────────────────────────────────
        kokoro_entry = next((e for e in tts_chain if e.get("adapter") == "kokoro"), None)
        # `adapter` is a CONNECTION field, so Google's TTS and STT entries
        # both report `gemini`. What identifies a cloud SPEAKING lane is
        # the pair of fields only a TTS entry carries: its own endpoint
        # (this model family is on the Interactions API, not the chat
        # base) and the voice to speak in.
        #
        # Matching on those rather than on the adapter alone is also what
        # keeps a half-written catalog entry from taking down the whole
        # subsystem: everything below runs under one `try`, so a KeyError
        # here would leave the machine with no voice AND no ears. An entry
        # this build cannot drive is skipped and the remaining lanes
        # serve — the contract this function's docstring already states.
        gemini_tts_entry = next(
            (
                e for e in tts_chain
                if e.get("adapter") == "gemini"
                and e.get("base_url_override")
                and e.get("voice")
            ),
            None,
        )
        unbuildable = [
            e for e in tts_chain
            if e.get("adapter") == "gemini" and e is not gemini_tts_entry
        ]
        for entry in unbuildable:
            log.warning(
                "voice: TTS ref=%s names the gemini adapter but carries no "
                "`base_url_override`/`voice` — lane not built",
                entry.get("ref"),
            )

        if kokoro_entry or gemini_tts_entry:
            kokoro_config = None
            gemini_config = None
            if kokoro_entry:
                kokoro_models_dir = voice_model_files.lane_dir("kokoro")
                model_filename = kokoro_entry["model"]
                voices_filename = kokoro_entry.get("voices_file", "voices-v1.0.bin")
                k_model_path = kokoro_models_dir / model_filename
                k_voices_path = kokoro_models_dir / voices_filename
                k_presets_raw = kokoro_entry.get("synthesis_presets") or {}
                k_presets = {
                    name: KokoroPreset(
                        speed=float(spec.get("speed", 1.0)),
                        sentence_silence=float(spec.get("sentence_silence", 0.2)),
                    )
                    for name, spec in k_presets_raw.items()
                }
                k_mix_raw = kokoro_entry.get("mix") or {}
                k_mix = {str(vid): float(weight) for vid, weight in k_mix_raw.items()}
                if not k_mix:
                    log.warning("voice: Kokoro entry has empty `mix` — synthesis will fail")
                kokoro_config = KokoroTTSConfig(
                    model_path=k_model_path,
                    voices_path=k_voices_path,
                    mix=k_mix,
                    lang=str(kokoro_entry.get("lang", "en-gb")),
                    device=str(kokoro_entry.get("device", "cuda")),
                    sample_rate=int(kokoro_entry.get("sample_rate", 24000)),
                    presets=k_presets,
                    preload=bool(kokoro_entry.get("preload", False)),
                    timeout_seconds=float(kokoro_entry.get("timeout_seconds", 60.0)),
                )
            if gemini_tts_entry:
                g_presets_raw = gemini_tts_entry.get("synthesis_presets") or {}
                g_presets = {
                    name: GeminiPreset(
                        style=str(spec.get("style", "")),
                        pace=str(spec.get("pace", "")),
                    )
                    for name, spec in g_presets_raw.items()
                }
                gemini_config = GeminiTTSConfig(
                    model=gemini_tts_entry["model"],
                    api_key_env=gemini_tts_entry["api_key_env"],
                    base_url=gemini_tts_entry["base_url_override"],
                    voice=gemini_tts_entry["voice"],
                    timeout_seconds=float(gemini_tts_entry["timeout_seconds"]),
                    sample_rate=int(gemini_tts_entry["sample_rate"]),
                    audio_profile=str(gemini_tts_entry.get("audio_profile", "")),
                    presets=g_presets,
                )
                if not os.environ.get(gemini_config.api_key_env):
                    # Built anyway. The lane is the operator's stated chain
                    # and a key can arrive without a restart, so refusing to
                    # construct it would make the setup form's "add a key
                    # later" path a lie. Said once at boot, because a cloud
                    # voice that cannot authenticate is the difference
                    # between a quiet machine and a broken one.
                    log.warning(
                        "voice: cloud TTS lane %s is configured but %s is not "
                        "set — it will fail at the first sentence",
                        gemini_tts_entry.get("ref"),
                        gemini_config.api_key_env,
                    )
            # Every key comes off the chain entry that produced the
            # config — no defaults. A lane with no entry has no config
            # and no key, so the engine skips it by construction.
            tts_primary_provider = tts_chain[0]["provider"]
            app["tts_engine"] = TTSEngine(
                cost_ledger=ledger,
                kokoro_config=kokoro_config,
                gemini_config=gemini_config,
                provider_key=tts_primary_provider,
                kokoro_provider_key=(kokoro_entry or {}).get("provider", ""),
                gemini_provider_key=(gemini_tts_entry or {}).get("provider", ""),
            )
            log.info("voice: TTSEngine ready (primary=%s)", tts_primary_provider)
            # Warm a lane at boot only when it is the role primary (or
            # the operator explicitly opted in via catalog `preload`).
            # A fallback lane stays cold so a cloud-primary loadout doesn't
            # pay Kokoro's CUDA-DLL preload cost for a model it may never
            # use. First use loads the cold lane lazily.
            primary_tts_adapter = tts_chain[0].get("adapter")
            if (
                app["tts_engine"] is not None
                and kokoro_config is not None
                and (primary_tts_adapter == "kokoro" or kokoro_config.preload)
            ):
                _queue_serial_warmup(app, app["tts_engine"].warm_up_kokoro(), name="kokoro")
    except Exception:
        log.exception("voice runtime unavailable — /api/voice/* will report disabled")


def _warm_wake_spotter(app: web.Application) -> None:
    """Load the wake decoder into the gate's cache, off the loop.

    Only when the gate would actually use it: the wake word on, a confirmed
    calibration present, and the phrase it was confirmed for still the phrase.
    Warming outside those conditions would spend 9-12 s and a few hundred MB
    of resident graph on an install whose gate is open and stays open.

    Never raises. This is an optimisation, and every fault it could hit is one
    `evaluate_wake_gate` already resolves to dispatch.
    """
    try:
        from tesseract.mirror.server.wake_word import (
            WakeWordConfig,
            spotter_blocking,
            wake_phrase,
        )
        from tesseract.voice import wake_calibration
        from tesseract.voice.wake_spotter import SpotterKey

        config = app.get("config")
        wake = getattr(config, "wake_word", None)
        if not isinstance(wake, WakeWordConfig) or not wake.enabled:
            return
        calibration = wake_calibration.load()
        phrase = wake_phrase(config)
        if calibration is None or not phrase or not calibration.matches_phrase(phrase):
            return
        started = time.monotonic()
        if spotter_blocking(
            app,
            SpotterKey(
                phrase=phrase,
                threshold=max(calibration.threshold, wake.min_threshold),
                boost=wake.boost,
            ),
        ) is not None:
            log.info(
                "wake_word: decoder warm in %.1fs — the first utterance does "
                "not pay for it", time.monotonic() - started,
            )
    except Exception as exc:  # noqa: BLE001 — a warm-up that fails is just a cold gate
        log.info("wake_word: could not warm the decoder (%s) — it will load on demand", exc)


def _try_build_observer(cost_ledger=None):
    try:
        from tesseract.brain.boot import build_observer

        observer = build_observer(cost_ledger=cost_ledger)
        log.info("observer: %s", "ready" if observer else "none_configured")
        return observer
    except Exception:
        log.exception("observer unavailable — /api/observe will return 503")
        return None


def _build_chat_infra(app: web.Application) -> None:
    try:
        from tesseract.brain.boot import (
            resolve_chat_brain_runtime,
        )
        from tesseract.brain.prompt import assemble_system_prompt

        t0 = time.monotonic()
        # Popped, not read: STAGE 1 resolved this seconds ago and nothing else
        # consumes it. Leaving it on the app would be a cache with no
        # invalidation rule — `rebuild_adapters` re-resolves on every config
        # save and would have to remember to refresh a second copy.
        chat_cfg, adapter, options, adapter_chain = (
            app.pop("chat_runtime", None) or resolve_chat_brain_runtime()
        )
        t1 = time.monotonic()

        def _build_prompt() -> str:
            # Resolved at call time, not captured: STAGE ordering does not
            # guarantee the registry is on the app yet when this closure is
            # built, and a prompt rebuilt after `rebuild_adapters` must see
            # the registry that healing produced rather than the one before it.
            return assemble_system_prompt(
                tool_registry_provider=lambda: app.get("tool_registry"),
            )

        system_prompt = _build_prompt()
        t2 = time.monotonic()

        app["adapter"] = adapter
        app["adapter_options"] = options
        app["adapter_entry"] = chat_cfg
        app["adapter_chain"] = adapter_chain
        app["system_prompt"] = system_prompt
        app["prompt_builder"] = _build_prompt
        app["chat_infra_error"] = None
        log.info(
            "chat infra ready: provider=%s model=%s chain_len=%d prompt_chars=%d "
            "resolve_s=%.2f assemble_s=%.2f total_s=%.2f",
            chat_cfg.provider, chat_cfg.model, len(adapter_chain), len(system_prompt),
            t1 - t0, t2 - t1, t2 - t0,
        )
    except RuntimeError as exc:
        # No chat_brain candidate resolved (no API key set for any of them,
        # or every candidate disabled in providers.yaml) — not a hard
        # failure. Nothing in TESSERACT requires an API key; degrade chat
        # to a placeholder so the WS still connects and every non-chat
        # capability keeps working, and let the operator find out why only
        # when they actually try to send a chat message. The full
        # per-candidate technical breakdown (model ids, providers.yaml flag
        # names) goes to this log line only — `str(exc)` is
        # `ChatBrainUnavailable.detail` when raised by
        # `resolve_chat_brain_runtime`. The short `summary` (falling back to
        # `str(exc)` for any other RuntimeError shape) is what actually
        # reaches the operator via NullChatAdapter's in-chat error.
        log.warning("chat infra degraded — no chat provider resolved: %s", exc)
        reason = getattr(exc, "summary", None) or str(exc)
        _build_degraded_chat_infra(app, reason)
    except Exception:
        log.exception("chat infra unavailable — /ws chat_message will fail")


def _build_degraded_chat_infra(app: web.Application, reason: str) -> None:
    """Populate the same app-dict slots `_build_chat_infra` would on success,
    but backed by a `NullChatAdapter` that raises `reason` the moment a turn
    actually calls it. `load_chat_brain_chain()` is a pure YAML parse (no
    adapter construction), so it succeeds even with zero keys — the primary
    entry's config (tool caps, compact thresholds, etc.) is real, only the
    live adapter is a placeholder. Falls through to the old "adapter_entry
    stays None" behavior if even this can't be built (e.g. malformed
    roles.yaml) — that is a genuine boot problem, not a "no key" one.
    """
    try:
        from tesseract.brain.boot import (
            adapter_options_from_chat_brain,
            load_chat_brain_chain,
        )
        from tesseract.brain.prompt import assemble_system_prompt
        from tesseract.kernel.adapters.null_adapter import NullChatAdapter

        chat_cfg = load_chat_brain_chain()[0]

        def _build_prompt() -> str:
            return assemble_system_prompt(
                tool_registry_provider=lambda: app.get("tool_registry"),
            )

        app["adapter"] = NullChatAdapter(reason)
        app["adapter_options"] = adapter_options_from_chat_brain(chat_cfg)
        app["adapter_entry"] = chat_cfg
        app["adapter_chain"] = []
        app["system_prompt"] = _build_prompt()
        app["prompt_builder"] = _build_prompt
        app["chat_infra_error"] = reason
    except Exception:
        log.exception("chat infra degraded-build also failed — /ws chat_message will fail")
