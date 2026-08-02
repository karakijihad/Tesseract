"""TC-4 entry point: `python -m tesseract.scripts.tars_controller`.

Boots the headless controller daemon as a sibling to the Mirror backend.
On `start()` the daemon writes its TCP port to
`<TESSERACT_HOME>/run/controller.port` and accepts IPC connections after
the token handshake.

Brain wiring (chat brain + tool registry + observer) is attempted on
boot but is **not** load-bearing for daemon liveness. If brain
construction raises, the daemon still comes up and accepts attach /
list_sessions / new_session traffic — user_input messages are persisted
as `user_text` transcript events without an assistant reply. TC-6 lands
the full chat turn integration over IPC.

TC-5 (reload protocol) attaches a :class:`ControllerRuntime` so the
brain wiring is a mutable holder: an inbound ``reload`` IPC message can
rebuild the adapter / tool registry / system prompt in place, without
restarting the OS process.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import signal
import sys
from typing import Any, Awaitable, Callable

from tesseract.config_seed import (
    ensure_agents_seeded,
    ensure_config_seeded,
    ensure_env_seeded,
    ensure_memory_store_seeded,
    ensure_tars_workshop_seeded,
    ensure_vault_seeded,
    ensure_workspace_seeded,
)
from tesseract.orchestrator.tars_controller import (
    ControllerDaemon,
    ControllerSessionRecord,
    SessionRegistry,
    auth as controller_auth_mod,
    register_default_handler,
)
from tesseract.orchestrator.tars_controller import auth  # alias for clarity
from tesseract.orchestrator.tars_controller.events import (
    AssistantTextEvent,
    SessionMetricsEvent,
    ToolResultEvent,
    ToolUseEvent,
    WorkerStatusEvent,
)
from tesseract.paths import CONFIG_DIR
from tesseract.scheduler.alarms import ensure_alarms_state_migrated

log = logging.getLogger(__name__)

# Strong-reference set so GC cannot collect in-flight controller session-emit tasks.
_CONTROLLER_EMIT_TASKS: set[asyncio.Task] = set()

_AGENDA_YAML = CONFIG_DIR / "agenda.yaml"
_DEFAULT_DRAIN_TIMEOUT_SECONDS = 30.0


def _mint_controller_id() -> str:
    return f"ctrl-{secrets.token_hex(6)}"


def _registry_without(registry: "ToolRegistry", exclude: set[str]) -> "ToolRegistry":
    """Return a registry copy minus ``exclude`` names. Returns the input
    unchanged when ``exclude`` is empty or no name matches (no needless
    copy). Mirrors the proven fork pattern in ``brain/chat.py``."""
    from tesseract.brain.tools import ToolRegistry

    if not exclude or not (exclude & set(registry.tools)):
        return registry
    filtered = ToolRegistry()
    for name, tool in registry.tools.items():
        if name in exclude:
            continue
        filtered.register(tool)
    return filtered


def _load_token() -> str:
    token = auth.read_token()
    if token:
        return token
    minted = controller_auth_mod.mint_token()
    auth.write_token(minted)
    return minted


def _load_drain_timeout_seconds() -> float:
    """Read ``controller.drain_timeout_seconds`` from ``agenda.yaml``.

    Falls back to :data:`_DEFAULT_DRAIN_TIMEOUT_SECONDS` only when the
    file is unreadable / the key is missing — a typo'd value logs a
    warning and falls back to the default rather than passing through.
    """
    try:
        import yaml as _yaml

        raw = _yaml.safe_load(_AGENDA_YAML.read_text(encoding="utf-8")) or {}
    except (OSError, Exception):  # noqa: BLE001 — boot must never wedge
        log.warning(
            "controller: agenda.yaml unreadable; drain_timeout default=%.0fs",
            _DEFAULT_DRAIN_TIMEOUT_SECONDS,
        )
        return _DEFAULT_DRAIN_TIMEOUT_SECONDS
    block = raw.get("controller") or {}
    raw_val = block.get("drain_timeout_seconds")
    if raw_val is None:
        return _DEFAULT_DRAIN_TIMEOUT_SECONDS
    try:
        value = float(raw_val)
    except (TypeError, ValueError):
        log.warning(
            "controller: agenda.yaml::controller.drain_timeout_seconds "
            "is non-numeric (%r); falling back to %.0fs",
            raw_val,
            _DEFAULT_DRAIN_TIMEOUT_SECONDS,
        )
        return _DEFAULT_DRAIN_TIMEOUT_SECONDS
    if value <= 0:
        log.warning(
            "controller: agenda.yaml::controller.drain_timeout_seconds "
            "is non-positive (%r); falling back to %.0fs",
            raw_val,
            _DEFAULT_DRAIN_TIMEOUT_SECONDS,
        )
        return _DEFAULT_DRAIN_TIMEOUT_SECONDS
    return value


def _schedule_close_all(sessions: Any) -> None:
    """Schedule close_all() for each session's interactive_sessions registry.

    Mirrors ChatSession.reset()'s pattern: fire-and-forget via
    loop.create_task so the async close_all is not lost to GC. Guard
    against missing attr and no running loop — best-effort only.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no running loop — nothing to schedule
    for session in sessions:
        reg = getattr(session, "interactive_sessions", None)
        if reg is None:
            continue
        try:
            loop.create_task(reg.close_all())
        except Exception:  # noqa: BLE001
            log.debug("_schedule_close_all: create_task raised", exc_info=True)


def _make_controller_ask_fn(
    daemon: ControllerDaemon, session_id: str
) -> Callable[[Any, Any, Any], Awaitable[bool]]:
    """Build the ``AskFn`` the ChatSession plumbs into tool execution.

    ChatSession calls ``ask_fn(tool, tool_input, context)`` whenever a
    tool resolves to ASK posture. We forward to
    :meth:`ControllerDaemon.request_permission` so the prompt fans out to
    every interactive client attached to ``session_id``; headless sessions
    surface as BLOCKED automatically inside the daemon.

    ``tool_use_id`` is the call id pinned on the per-call ``ToolContext``
    by ChatSession's safe/unsafe split (``current_call_id``); without it
    the daemon's pending-approval map cannot route the operator's
    ``approval`` IPC back to the awaiting future.
    """

    async def _ask_fn(tool: Any, tool_input: Any, context: Any) -> bool:
        tool_name = getattr(tool, "name", "unknown")
        tool_use_id = getattr(context, "current_call_id", None) or "unknown"
        try:
            summary = (
                tool_input.model_dump_json()
                if hasattr(tool_input, "model_dump_json")
                else str(tool_input)
            )
        except Exception:  # noqa: BLE001 — summary is operator-visible only
            summary = "<unreadable input>"
        if len(summary) > 240:
            summary = summary[:240] + "…"
        return await daemon.request_permission(
            session_id,
            tool=tool_name,
            summary=summary,
            tool_use_id=str(tool_use_id),
            posture="ask",
        )

    return _ask_fn


def _make_controller_cli_sink(
    daemon: ControllerDaemon, session_id: str
) -> Callable[[str, str, dict[str, Any]], Awaitable[None]]:
    """Build the ``CliSink`` chat tools call when streaming subprocess
    output. The chat brain's tool layer invokes
    ``sink(kind, call_id, payload)`` with ``kind`` in
    ``{"cli_start", "cli_output", "cli_end"}``; we wrap each call in a
    :class:`CliChunkEvent` so the TUI renders the live output indented
    under the parent tool_use line — the "see what claude is doing"
    affordance you'd get from the bare ``claude`` CLI.
    """
    from tesseract.orchestrator.tars_controller.events import CliChunkEvent

    async def _cli_sink(
        kind: str, call_id: str, payload: dict[str, Any]
    ) -> None:
        # The chat-brain's tool layer can call this concurrently from
        # multiple tool tasks (the safe-tool fan-out). Each event is
        # independently small + the daemon's append_event already locks
        # the writer, so no extra serialization is needed here.
        phase = (
            "start"
            if kind == "cli_start"
            else "end"
            if kind == "cli_end"
            else "chunk"
        )
        text = ""
        exit_code = None
        if phase == "chunk":
            # Audit-3 M3 — canonical chunk key is ``delta`` (see
            # ``cli_stream.CliSinkChunkPayload``). The previous lookup
            # read ``text`` / ``output`` which the producer never sets,
            # so every chunk landed empty and the live-stdout
            # affordance was invisible. ``text`` / ``output`` stay as
            # fallbacks so a third-party sink that names its key
            # differently still surfaces something rather than nothing.
            raw = (
                payload.get("delta")
                or payload.get("text")
                or payload.get("output")
                or ""
            )
            text = raw if isinstance(raw, str) else str(raw)
        elif phase == "end":
            ec = payload.get("exit_code")
            exit_code = int(ec) if isinstance(ec, int) else None
        try:
            await daemon.append_event(
                session_id,
                CliChunkEvent(
                    session_id=session_id,
                    origin="chat",
                    tool=str(payload.get("tool") or ""),
                    tool_use_id=str(call_id or ""),
                    text=text,
                    phase=phase,  # type: ignore[arg-type]
                    exit_code=exit_code,
                ),
            )
        except Exception:  # noqa: BLE001 — never let the sink kill the tool
            log.debug(
                "controller: cli_sink append_event raised", exc_info=True,
            )

    return _cli_sink


def _make_controller_session_emit(
    daemon: ControllerDaemon, session_id: str
) -> Any:
    """Build the ``session_emit`` callable wired onto ``ToolContext``.

    A10 — interactive-session tools call this synchronously (no await)
    with raw event dicts.  We map the three relevant shapes to typed
    transcript events and schedule ``daemon.append_event`` as a
    fire-and-forget task so the synchronous caller is not blocked.

    Mapped shapes:
    - ``{"type":"session_status","handle":h,"target":t,"status":"running"|"done"}``
      → ``WorkerStatusEvent`` (rail job row keyed by handle)
    - ``{"type":"assistant","text":t,"handle":h}``               (agent backend)
    - ``{"type":"assistant","message":{"content":[{"text":t}]},"handle":h}`` (claude backend)
    - ``{"type":"item.completed","item":{"type":"agent_message","text":t},"handle":h}``
      → ``AssistantTextEvent`` (detail pane keyed by worker_id=handle)

    The ``handle`` field on text events is stamped at source by
    ``_make_emit(context, handle)`` in ``session_tools.py``, which performs a
    non-destructive merge so every event already carries its originating handle
    before it arrives here.  There is no shared mutable cell — each event is
    attributed to the handle it declares, making this closure safe for parallel
    background sessions that stream concurrently through the same parent context.

    Unknown event types are silently dropped; errors never propagate.
    """

    def _session_emit(event: dict[str, Any]) -> None:
        ev_type = event.get("type", "")
        typed_event = None

        if ev_type == "session_status":
            handle = str(event.get("handle") or "")
            status = str(event.get("status") or "")
            target = str(event.get("target") or "")
            if handle and status:
                typed_event = WorkerStatusEvent(
                    session_id=session_id,
                    origin="chat",
                    worker_id=handle,
                    worker_kind=target or "session",
                    status=status,
                    progress=None,
                )
        elif ev_type == "assistant":
            # Agent backend: {"type":"assistant","text":"...","handle":"..."}
            text = event.get("text")
            if not isinstance(text, str):
                # Claude backend: {"type":"assistant","message":{"content":[{"text":"..."}]},"handle":"..."}
                msg = event.get("message") or {}
                content = msg.get("content") if isinstance(msg, dict) else None
                if isinstance(content, list) and content:
                    first = content[0]
                    text = first.get("text") if isinstance(first, dict) else None
            if isinstance(text, str) and text:
                handle = str(event.get("handle") or "")
                typed_event = AssistantTextEvent(
                    session_id=session_id,
                    origin="chat",
                    text=text,
                    partial=False,
                    worker_id=handle or None,
                )
        elif ev_type == "item.completed":
            # Codex backend: {"type":"item.completed","item":{"type":"agent_message","text":"..."},"handle":"..."}
            item = event.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text:
                    handle = str(event.get("handle") or "")
                    typed_event = AssistantTextEvent(
                        session_id=session_id,
                        origin="chat",
                        text=text,
                        partial=False,
                        worker_id=handle or None,
                    )

        if typed_event is None:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # not inside a running loop — best-effort
        try:
            task = loop.create_task(daemon.append_event(session_id, typed_event))
            _CONTROLLER_EMIT_TASKS.add(task)
            task.add_done_callback(_CONTROLLER_EMIT_TASKS.discard)
        except Exception:  # noqa: BLE001 — never let emit break the tool
            log.debug("controller: session_emit task creation failed", exc_info=True)

    return _session_emit


class ControllerRuntime:
    """Mutable holder for the controller's brain wiring.

    The daemon's ``dispatch_turn`` callback reads :attr:`adapter`,
    :attr:`tool_registry`, and :attr:`system_prompt` on every turn —
    never captures them at construction. :meth:`reload` rebuilds these
    in place when a TC-5 ``reload`` IPC message arrives.

    All three handles may be ``None`` when the underlying boot path
    raises; in that case ``dispatch_turn`` is a no-op (the user_text
    event still lands in the transcript). This keeps the daemon
    liveness invariant from TC-4: brain wiring is best-effort.
    """

    def __init__(self) -> None:
        self.adapter: Any | None = None
        self.tool_registry: Any | None = None
        self.system_prompt: str | None = None
        self.tool_iteration_cap: int = 8
        # ChatSession requires both caps as explicit kwargs; canonical
        # source is ``roles.yaml::chat_brain.{tool_iteration_cap,
        # consecutive_error_cap}`` via ``ChatBrainConfig``. The init value
        # is a safe boot floor; ``_rebuild_adapter`` replaces it on every
        # adapter rebuild so reload picks up YAML edits.
        self.consecutive_error_cap: int = 3
        # PermissionPolicy resolved during tool-registry rebuild; passed
        # into ChatSession so ASK-posture tools flow through ask_fn.
        self.policy: Any | None = None
        # Audit-2 M3 — adapter options (max tokens, temperature,
        # thinking_budget, etc) and the live chat brain config, both
        # resolved by ``resolve_chat_brain_runtime``. Discarding them
        # left controller turns running at adapter defaults while
        # Mirror/REPL ran with YAML-driven knobs.
        self.adapter_options: Any | None = None
        self.chat_brain_config: Any | None = None
        # Audit-2 M2 — persistent ChatSession per controller session_id.
        # ``ChatSession.history`` is mutable per-turn; recreating the
        # session for each ``user_input`` discards conversation context
        # and breaks tool-loop continuity (spawn handles, todos, pending
        # suggestions). ``reload()`` clears this map so the next turn
        # picks up the rebuilt adapter / tool registry / prompt without
        # importing stale history.
        self._chat_sessions: dict[str, Any] = {}
        # X-3 — real providers; scheduler is built without .start() (Mirror
        # owns the tick loop).
        self.scheduler: Any | None = None
        # X-4 Session A — controller-owned LaneManager. Long-lived; survives
        # brain restarts because lane state is file-canonical under
        # <TESSERACT_HOME>/controller/lanes/. Built once at boot; not
        # rebuilt on reload (lanes outlive the brain's adapter config).
        self.lane_manager: Any | None = None
        # X-5 — name→lane_id binding layer over `lane_manager`. Rebuilt
        # whenever the lane manager is rebuilt so both holders stay in
        # lockstep (the binding wraps the underlying manager directly).
        self.named_lane_manager: Any | None = None

    def _get_scheduler(self) -> Any | None:
        return self.scheduler

    def _get_lane_manager(self) -> Any | None:
        return self.lane_manager

    def _get_named_lane_manager(self) -> Any | None:
        return self.named_lane_manager

    def _rebuild_adapter(self) -> tuple[list[str], list[str]]:
        reloaded: list[str] = []
        failed: list[str] = []
        try:
            from dotenv import load_dotenv
            from tesseract.brain.boot import (
                build_fallback_adapter,
                resolve_chat_brain_runtime,
            )
            from tesseract.paths import home_dir

            # Call-time resolution — `boot.ENV_PATH` is frozen at first
            # import, before a relocated `TESSERACT_HOME` is guaranteed
            # visible; `home_dir()` re-resolves the env var on every call.
            load_dotenv(home_dir() / ".env")
            chat_cfg, adapter, options, adapter_chain = (
                resolve_chat_brain_runtime()
            )
            self.adapter = (
                build_fallback_adapter(adapter_chain) if adapter_chain else adapter
            )
            self.tool_iteration_cap = chat_cfg.tool_iteration_cap
            self.consecutive_error_cap = chat_cfg.consecutive_error_cap
            # Audit-2 M3 — keep the YAML-resolved adapter options + cfg so
            # dispatch_turn passes them into ChatSession (mirrors the
            # Mirror/REPL wiring path).
            self.adapter_options = options
            self.chat_brain_config = chat_cfg
            reloaded.append("adapter")
        except Exception as exc:  # noqa: BLE001
            log.exception("controller: adapter rebuild failed")
            failed.append(f"adapter: {exc}")
        return reloaded, failed

    def _rebuild_tools(self) -> tuple[list[str], list[str]]:
        reloaded: list[str] = []
        failed: list[str] = []
        try:
            from tesseract.brain.boot import (
                PERMISSIONS_YAML,
                ROOT,
                build_tool_registry,
            )
            from tesseract.paths import home_dir
            from tesseract.permissions.policy import load_permission_policy

            # State root, not the repo — `path_overrides` prefixes are
            # home-relative (see `mirror/server/config.py` for the full note).
            policy = load_permission_policy(
                PERMISSIONS_YAML, workspace_root=str(home_dir())
            )
            registry, *_ = build_tool_registry(policy=policy)
            self.tool_registry = registry
            self.policy = policy
            reloaded.append("tool_registry")
        except Exception as exc:  # noqa: BLE001
            log.exception("controller: tool registry rebuild failed")
            failed.append(f"tool_registry: {exc}")
        return reloaded, failed

    def _rebuild_scheduler(self) -> tuple[list[str], list[str]]:
        """X-3 — unstarted engine (Mirror owns the tick loop); provides
        create/list/remove persistence."""
        reloaded: list[str] = []
        failed: list[str] = []
        try:
            from tesseract.paths import CONFIG_DIR
            from tesseract.scheduler.engine import SchedulerEngine

            self.scheduler = SchedulerEngine(config_dir=CONFIG_DIR)
            reloaded.append("scheduler")
        except Exception as exc:  # noqa: BLE001
            log.exception("controller: scheduler rebuild failed")
            failed.append(f"scheduler: {exc}")
        return reloaded, failed

    def _rebuild_lane_manager(self) -> tuple[list[str], list[str]]:
        """Construct the controller's single `LaneManager` instance plus
        the `NamedLaneManager` binding layer that wraps it."""
        reloaded: list[str] = []
        failed: list[str] = []
        try:
            from tesseract.orchestrator.tars_controller.lanes import (
                LaneManager,
                NamedLaneManager,
            )

            self.lane_manager = LaneManager()
            self.named_lane_manager = NamedLaneManager(
                lane_manager=self.lane_manager
            )
            reloaded.append("lane_manager")
            reloaded.append("named_lane_manager")
        except Exception as exc:  # noqa: BLE001
            log.exception("controller: lane manager rebuild failed")
            failed.append(f"lane_manager: {exc}")
        return reloaded, failed

    def _rebuild_prompt(self) -> tuple[list[str], list[str]]:
        reloaded: list[str] = []
        failed: list[str] = []
        try:
            from tesseract.brain.prompt import assemble_system_prompt

            self.system_prompt = assemble_system_prompt(mode="manifest")
            reloaded.append("system_prompt")
        except Exception as exc:  # noqa: BLE001
            log.exception("controller: system prompt rebuild failed")
            failed.append(f"system_prompt: {exc}")
        return reloaded, failed

    def initial_build(self) -> None:
        """Best-effort build at boot. Failures are logged and leave the
        holders at ``None``; the daemon stays alive (TC-4 invariant)."""
        try:
            self._rebuild_adapter()
            self._rebuild_tools()
            self._rebuild_scheduler()
            self._rebuild_lane_manager()
            self._rebuild_prompt()
        except Exception:  # noqa: BLE001
            log.exception("controller: initial brain wiring failed")

    async def reload(self, target: str) -> dict[str, list[str]]:
        """TC-5 reload callback invoked by the daemon after drain.

        Targets follow :class:`ReloadMessage`:
        - ``config`` / ``roles`` → rebuild adapter (providers.yaml /
          roles.yaml feed the same ``resolve_chat_brain_runtime`` path)
        - ``tools`` → rebuild tool registry (e.g. permissions.yaml)
        - ``all`` → both, plus the system prompt
        """
        reloaded: list[str] = []
        failed: list[str] = []
        if target in ("config", "roles", "all"):
            r, f = await asyncio.to_thread(self._rebuild_adapter)
            reloaded.extend(r)
            failed.extend(f)
        if target in ("tools", "all"):
            r, f = await asyncio.to_thread(self._rebuild_tools)
            reloaded.extend(r)
            failed.extend(f)
            r, f = await asyncio.to_thread(self._rebuild_scheduler)
            reloaded.extend(r)
            failed.extend(f)
        if target == "all":
            r, f = await asyncio.to_thread(self._rebuild_prompt)
            reloaded.extend(r)
            failed.extend(f)
        # X-4 — DO NOT rebuild self.lane_manager on reload. Lanes outlive
        # any reload target (config/roles/tools): the lane processes are
        # live workers, lane state is file-canonical, and the brain's
        # restart-recovery path is `lane.attach`, not "rebuild manager".
        # A rebuild here would orphan every in-flight lane.
        # Audit-2 M2 — drop cached ChatSession instances so the next turn
        # picks up the rebuilt adapter / tool registry / system prompt.
        # History is discarded with them; that matches Mirror's
        # rebuild_adapters semantics where existing sessions also get a
        # fresh chain on the next ``send()``.
        # Close interactive sessions before clearing so CLI subprocesses
        # aren't left orphaned (close_all is async; GC won't run it).
        _schedule_close_all(self._chat_sessions.values())
        self._chat_sessions.clear()
        return {"reloaded": reloaded, "failed": failed}

    async def drop_session(self, session_id: str) -> None:
        """Audit-2 A1 — wired as the daemon's ``on_session_deleted``
        callback so a deleted session's cached :class:`ChatSession`
        gets reclaimed instead of leaking until the next ``reload``.

        Async signature matches the daemon's :class:`OnSessionDeleted`
        type even though the work is purely synchronous — keeps the
        contract uniform with ``reload`` / ``dispatch_turn`` callbacks
        and leaves room for a future hook that needs to await
        teardown of out-of-process state.
        """
        evicted = self._chat_sessions.pop(session_id, None)
        if evicted is not None:
            _schedule_close_all([evicted])

    def _build_chat_session(
        self,
        record: ControllerSessionRecord,
        daemon: ControllerDaemon,
    ) -> Any:
        """Audit-2 M3 — controller-side equivalent of Mirror's
        ``_build_chat_session`` factory.

        Mirror's path lives in ``tesseract/mirror/server/session.py``
        and seeds ChatSession with adapter options, a prompt builder,
        and a ``ToolContext`` populated with workspace_root + session_id
        + scheduler / registry providers. Until this method landed the
        controller passed only the bare minimum, leaving registry-aware
        tools to run with default ``ToolContext()``."""
        from tesseract.brain.chat_session_factory import (
            ChatSessionWiring,
            build_chat_session,
        )
        from tesseract.brain.prompt import assemble_system_prompt
        from tesseract.brain.boot import ROOT

        ask_fn = _make_controller_ask_fn(daemon, record.session_id)

        # WS-3 — enforce the operator's coder choice two ways: physically
        # remove the opposing delegate_* tool from this session's registry
        # AND append a HARD-RULE directive so the brain can't route around
        # it in prose. ``None`` → no constraint (registry unchanged).
        coder = getattr(record, "preferred_coder", None)
        exclude: set[str] = set()
        if coder == "claude":
            exclude = {"delegate_codex"}
        elif coder == "codex":
            exclude = {"delegate_claude"}
        session_registry = _registry_without(self.tool_registry, exclude)

        def _prompt_builder() -> str:
            # Mirror reassembles the system prompt per turn so SOUL.md /
            # IDENTITY.md edits land inside the active session. The
            # controller follows the same pattern; cached `system_prompt`
            # acts as the boot-time fallback when assembly fails.
            try:
                base = assemble_system_prompt(mode="manifest")
            except Exception:  # noqa: BLE001 — keep the turn alive
                base = self.system_prompt or ""
            if coder:
                other = "codex" if coder == "claude" else "claude"
                base += (
                    f"\n\n# Session coder constraint\n\n"
                    f"HARD RULE for this session: use `delegate_{coder}` for ALL "
                    f"coding and auditing work. `delegate_{other}` is unavailable "
                    f"in this session — do not attempt to call it."
                )
            return base

        # trio W3 — controller sessions are roots of their own process
        # (cross-process depth is not threaded); each enforces the same
        # nesting cap for its in-process sub-agents.
        from tesseract.config.runtime_limits import (
            default_runtime_config_path,
            load_max_spawn_depth,
        )

        cfg = self.chat_brain_config
        # ChatSession's compact_threshold / keep_recent_turns have safe
        # dataclass defaults; pull cfg-driven values only when the
        # config object actually exposes them so older boot paths still
        # work in tests.
        compact_threshold = None
        keep_recent_turns = None
        if cfg is not None:
            compact_threshold = getattr(cfg, "compact_threshold", None)
            keep_recent_turns = getattr(cfg, "keep_recent_turns", None)

        wiring = ChatSessionWiring(
            adapter=self.adapter,
            system_prompt=self.system_prompt or "",
            max_tool_iterations=self.tool_iteration_cap,
            max_consecutive_adapter_errors=self.consecutive_error_cap,
            workspace_root=str(ROOT),
            session_id=record.session_id,
            cli_sink=_make_controller_cli_sink(daemon, record.session_id),
            scheduler_provider=self._get_scheduler,
            tool_registry_provider=lambda: session_registry,
            lane_manager_provider=self._get_lane_manager,
            named_lane_manager_provider=self._get_named_lane_manager,
            ask_fn=ask_fn,
            policy=self.policy,
            prompt_builder=_prompt_builder,
            registry=session_registry,
            spawn_depth_cap=load_max_spawn_depth(default_runtime_config_path()),
            session_emit=_make_controller_session_emit(daemon, record.session_id),
            options=self.adapter_options,
            compact_threshold=compact_threshold,
            keep_recent_turns=keep_recent_turns,
        )
        return build_chat_session(wiring)

    def make_dispatch_turn(
        self,
    ) -> Callable[
        [ControllerSessionRecord, str, ControllerDaemon], Awaitable[None]
    ]:
        """Returns the dispatch_turn closure the daemon calls per
        user_input.

        Audit-2 M2 — ChatSession instances are cached per
        ``session_id`` on the runtime so conversation history persists
        across inputs. ``reload()`` invalidates the cache; the next
        turn rebuilds with the new adapter / tools / prompt.

        Audit-2 M3 — sessions are built via :meth:`_build_chat_session`
        which mirrors the Mirror/REPL wiring (full ToolContext, adapter
        options, prompt builder).

        Audit-3 C1/C2 — the stream is mapped chunk-by-chunk to typed
        transcript events so the TUI sees live intent / tool / result
        separation instead of one batched ``assistant_text`` at the end.
        Branching is on ``chunk.type`` because the previous
        ``hasattr(chunk, "text")`` test matched ``TOOL_RESULT`` chunks
        too and folded raw tool output into assistant prose.

        Audit-3 M7 — adapter / usage / context snapshots ride a
        :class:`SessionMetricsEvent` so the statusline can show
        model + tokens + context capacity instead of just elapsed-time
        for the active tool.
        """
        from tesseract.kernel.adapters.base import ChunkType

        async def dispatch_turn(
            record: ControllerSessionRecord,
            text: str,
            daemon: ControllerDaemon,
        ) -> None:
            sid = record.session_id
            if self.adapter is None or self.system_prompt is None:
                log.debug(
                    "controller: dispatch_turn skipped — brain wiring not ready"
                )
                return

            session = self._chat_sessions.get(sid)
            if session is None:
                session = self._build_chat_session(record, daemon)
                self._chat_sessions[sid] = session

            # Per-turn streaming state. ``in_assistant_text`` is True
            # while we're mid-stream of model prose; flipping to a tool
            # call or finishing the turn flushes a final
            # ``partial=False`` so the renderer closes the bubble.
            in_assistant_text = False
            last_model_meta: dict[str, str] = {}
            options = self.adapter_options

            async def _close_assistant_text() -> None:
                nonlocal in_assistant_text
                if in_assistant_text:
                    await daemon.append_event(
                        sid,
                        AssistantTextEvent(
                            session_id=sid,
                            origin="chat",
                            text="",
                            partial=False,
                        ),
                    )
                    in_assistant_text = False

            async def _emit_metrics(
                *,
                turn_state: str,
                usage: dict[str, Any] | None = None,
            ) -> None:
                # Best-effort metrics snapshot. ``model`` / ``role`` etc.
                # come from the most recent ``MODEL_SELECTED`` chunk OR
                # the adapter options when no MODEL_SELECTED has fired
                # yet (single-entry chains, local providers).
                opt_model = getattr(options, "model", "") if options else ""
                opt_role = getattr(options, "role", "") if options else ""
                opt_provider = getattr(options, "provider", "") if options else ""
                opt_tier = getattr(options, "tier", "") if options else ""
                opt_window = getattr(options, "context_window", 0) if options else 0
                payload: dict[str, Any] = {
                    "session_id": sid,
                    "origin": "chat",
                    "turn_state": turn_state,  # type: ignore[arg-type]
                    "model": last_model_meta.get("model") or opt_model or None,
                    "provider": last_model_meta.get("provider") or opt_provider or None,
                    "role": last_model_meta.get("role") or opt_role or None,
                    "tier": last_model_meta.get("tier") or opt_tier or None,
                    "context_window": int(opt_window) if opt_window else None,
                }
                if usage:
                    payload["input_tokens"] = int(usage.get("input_tokens") or 0)
                    payload["output_tokens"] = int(usage.get("output_tokens") or 0)
                    payload["cached_tokens"] = int(usage.get("cached_tokens") or 0)
                    in_t = payload["input_tokens"]
                    out_t = payload["output_tokens"]
                    payload["context_used"] = in_t + out_t
                try:
                    await daemon.append_event(
                        sid, SessionMetricsEvent(**payload)
                    )
                except Exception:  # noqa: BLE001 — metrics never break a turn
                    log.debug("controller: metrics emit raised", exc_info=True)

            await _emit_metrics(turn_state="thinking")
            try:
                async for chunk in session.send(text):
                    ct = chunk.type
                    if ct == ChunkType.TEXT:
                        if chunk.text:
                            await daemon.append_event(
                                sid,
                                AssistantTextEvent(
                                    session_id=sid,
                                    origin="chat",
                                    text=chunk.text,
                                    partial=True,
                                ),
                            )
                            in_assistant_text = True
                    elif ct == ChunkType.TOOL_CALL_END:
                        await _close_assistant_text()
                        tc = chunk.tool_call
                        if tc is not None:
                            await daemon.append_event(
                                sid,
                                ToolUseEvent(
                                    session_id=sid,
                                    origin="chat",
                                    tool=tc.name,
                                    input=dict(tc.input or {}),
                                    tool_use_id=tc.id,
                                ),
                            )
                            await _emit_metrics(turn_state="tool")
                    elif ct == ChunkType.TOOL_RESULT:
                        # Tool output. NEVER feed into assistant prose —
                        # that was the C2 regression. ``chunk.text`` is
                        # the tool's stdout/return; route it through the
                        # typed result event so the renderer can paint a
                        # distinct, untrusted-content panel.
                        tc_id = chunk.tool_call_id or ""
                        is_error = bool(chunk.error)
                        raw = chunk.raw if isinstance(chunk.raw, dict) else {}
                        await daemon.append_event(
                            sid,
                            ToolResultEvent(
                                session_id=sid,
                                origin="chat",
                                tool_use_id=tc_id,
                                success=not is_error,
                                output=chunk.text or "",
                                timed_out=bool(raw.get("timed_out")),
                            ),
                        )
                        await _emit_metrics(turn_state="streaming")
                    elif ct == ChunkType.MODEL_SELECTED:
                        raw = chunk.raw if isinstance(chunk.raw, dict) else {}
                        last_model_meta = {
                            "role": str(raw.get("role") or ""),
                            "provider": str(raw.get("provider") or ""),
                            "model": str(raw.get("model") or ""),
                            "tier": str(raw.get("tier") or ""),
                        }
                        await _emit_metrics(turn_state="streaming")
                    elif ct == ChunkType.STOP:
                        await _close_assistant_text()
                        usage_raw = (
                            chunk.raw.get("usage")
                            if isinstance(chunk.raw, dict)
                            else None
                        )
                        await _emit_metrics(
                            turn_state="done",
                            usage=usage_raw if isinstance(usage_raw, dict) else None,
                        )
                    elif ct == ChunkType.ERROR:
                        await _close_assistant_text()
                        err = chunk.error or "unknown adapter error"
                        await daemon.append_event(
                            sid,
                            AssistantTextEvent(
                                session_id=sid,
                                origin="chat",
                                text=f"[error: {err}]",
                                partial=False,
                            ),
                        )
                        await _emit_metrics(turn_state="error")
                    elif ct == ChunkType.SPAWN_DONE:
                        # Background spawn (delegate_* with background=True)
                        # finished — surface it on the background-agent
                        # rail keyed by the spawn handle so the operator
                        # sees the process complete + can inspect it.
                        raw = chunk.raw if isinstance(chunk.raw, dict) else {}
                        handle = str(raw.get("handle") or "")
                        if handle:
                            await daemon.append_event(
                                sid,
                                WorkerStatusEvent(
                                    session_id=sid,
                                    origin="chat",
                                    worker_id=handle,
                                    worker_kind=str(raw.get("kind") or "spawn"),
                                    status=str(raw.get("status") or "done"),
                                    progress=str(raw.get("summary") or ""),
                                ),
                            )
                    # REASONING_ITEM / USER_INJECT are ChatSession-internal
                    # — not surfaced to the TUI transcript.
            except Exception:
                log.exception("controller: dispatch_turn failed")
                await _close_assistant_text()
                await daemon.append_event(
                    sid,
                    AssistantTextEvent(
                        session_id=sid,
                        origin="chat",
                        text="[turn failed — see controller logs]",
                        partial=False,
                    ),
                )
                await _emit_metrics(turn_state="error")
                return
            # Stream exhausted without a STOP (rare — generator returned
            # cleanly via a circuit-breaker yield or similar). Make sure
            # the renderer closes its open bubble either way.
            await _close_assistant_text()

        return dispatch_turn


async def run_controller(*, host: str = "127.0.0.1", port: int = 0) -> int:
    register_default_handler()
    controller_id = _mint_controller_id()
    token = _load_token()
    drain_timeout = _load_drain_timeout_seconds()
    runtime = ControllerRuntime()
    runtime.initial_build()

    daemon = ControllerDaemon(
        controller_id=controller_id,
        token=token,
        registry=SessionRegistry(),
        dispatch_turn=runtime.make_dispatch_turn(),
        reload_callback=runtime.reload,
        on_session_deleted=runtime.drop_session,
        # X-4 Session C — daemon exposes lane.* IPC for external brains
        # (Mirror, ad-hoc TUI clients). The runtime owns the manager so
        # in-process callers (this brain) and IPC callers see one instance.
        lane_manager=runtime.lane_manager,
        # CV-1 — named-lane binding layer over the same manager, so Mirror's
        # lane bridge can resolve + ensure the trio's named lanes via IPC.
        named_lane_manager=runtime.named_lane_manager,
        drain_timeout_seconds=drain_timeout,
    )
    await daemon.start(host=host, port=port)
    print(
        f"controller: listening on {daemon.address[0]}:{daemon.address[1]} "
        f"(id={controller_id}, drain={drain_timeout:.0f}s)",
        flush=True,
    )

    stop_event = asyncio.Event()

    def _handle_signal(*_: object) -> None:
        log.info("controller: shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except (NotImplementedError, RuntimeError):
            # Windows: SIGTERM is not deliverable; SIGINT handled by KeyboardInterrupt.
            pass

    try:
        # 2026-05-24: race the OS signal handler against the daemon's
        # ``operator_shutdown_event`` so a TUI ``shutdown`` IPC tears
        # the process down without waiting for a SIGTERM.
        signal_task = asyncio.create_task(stop_event.wait())
        operator_task = asyncio.create_task(daemon.operator_shutdown_event.wait())
        try:
            await asyncio.wait(
                {signal_task, operator_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in (signal_task, operator_task):
                if not t.done():
                    t.cancel()
    except KeyboardInterrupt:
        pass
    finally:
        if daemon.operator_shutdown_event.is_set():
            # Deliberate IPC-triggered shutdown — tear down all controller sessions
            # so they are not offered as reattach candidates on next boot.
            # SIGTERM / SIGINT (crash / respawn) only sets stop_event, never this
            # event, so crash paths are never reached here.
            try:
                from tesseract.orchestrator.tars_controller.shutdown import (
                    teardown_all_controller_sessions,
                )

                # SessionRegistry is imported at module scope — re-importing it
                # here as a function-local made it local for ALL of
                # run_controller, so the daemon-construction use above raised
                # UnboundLocalError on every cold boot (2026-05-25).
                _reg = SessionRegistry()
                teardown_all_controller_sessions(
                    list_fn=lambda: _reg.list_sessions(status="active"),
                    delete_fn=_reg.delete_session,
                )
            except Exception:  # noqa: BLE001
                log.exception("run_controller: teardown_all_controller_sessions raised — continuing")
        await daemon.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    # Idempotent (each `ensure_*_seeded` copies only templates the install is
    # missing) — safe whether this process was spawned by the supervisor
    # (already seeded on disk) or run directly per this module's own
    # `python -m tesseract.scripts.tars_controller` entry point. Same
    # seed-before-boot order as `mirror/server/__main__.py::main` and
    # `supervisor/__main__.py::main` — without this, `run_controller` ->
    # `ControllerRuntime.initial_build` -> `build_tool_registry` ->
    # `agents_dir()` hits an empty TESSERACT_HOME/agents on a fresh
    # relocated home and every built-in agent load hard-fails.
    ensure_config_seeded()
    ensure_workspace_seeded()
    ensure_agents_seeded()
    ensure_env_seeded()
    ensure_memory_store_seeded()
    ensure_vault_seeded()
    ensure_tars_workshop_seeded()
    parser = argparse.ArgumentParser(prog="tesseract.scripts.tars_controller")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--log-level",
        default=os.environ.get("TESSERACT_LOG_LEVEL", "INFO"),
    )
    args = parser.parse_args(argv)  # --help exits here, before ever reaching migration
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Durable rotating file — same pipeline as the Mirror backend
    # (tesseract/logsetup.py); the inherited console dies with the supervisor.
    from tesseract.logsetup import attach_file_logging

    attach_file_logging("tars-controller")
    # After logging is attached AND after arg-parsing (so --help's SystemExit
    # short-circuits before this ever runs) — see the identical logging-order
    # comment in `mirror/server/__main__.py::main`.
    ensure_alarms_state_migrated()
    try:
        return asyncio.run(run_controller(host=args.host, port=args.port))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
