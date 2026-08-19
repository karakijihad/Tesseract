"""Five interactive-session tools: session_open / send / result / close / list.

Design: Option B — all operations take the session handle minted by
`session_open`. Background turns register a coro on `context.spawns` and
store the returned spawn-handle-id on the session object
(`session._pending_spawn_id`). `session_result` collects via that spawn
handle, then clears it.

Ask gate: only agent backends go through ASK (`context.ask_fn`). CLI
backends (claude / codex) run unconditionally — they are operator-approved
by posture.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, ClassVar, Optional

from pydantic import BaseModel, Field

from tesseract.brain.agent_factory import AgentBuildError, build_agent_session
from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.agent_controller.interactive.agent_backend import (
    AgentSessionBackend,
)
from tesseract.orchestrator.agent_controller.interactive.cli_adapter import (
    ClaudeStreamAdapter,
    CodexStreamAdapter,
)
from tesseract.orchestrator.agent_controller.interactive.cli_backend import (
    CliSessionBackend,
)
from tesseract.orchestrator.agent_controller.interactive.types import TurnResult

logger = logging.getLogger(__name__)

# ─────────────────────────── helpers ────────────────────────────────────────

# Strong-reference set so GC cannot collect in-flight emit tasks.
_EMIT_TASKS: set[asyncio.Task] = set()


def _load_session_thresholds() -> dict[str, object]:
    """Read pty_thresholds from permissions.yaml (the CLI-backend session
    turn-timeout knob). Errors are non-fatal; returns {}."""
    try:
        import yaml
        from tesseract.paths import config_dir
        path = config_dir() / "permissions.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}
    thresholds = raw.get("pty_thresholds")
    return dict(thresholds) if isinstance(thresholds, dict) else {}


def _make_emit(context: ToolContext, handle: str = ""):
    """Best-effort emit closure, stamped with the originating session handle.

    Every event forwarded by this closure carries ``"handle": handle`` so the
    controller bridge can attribute streamed text to the correct parallel
    sub-session without a shared mutable cell.  The stamp is a non-destructive
    merge: ``event.get("handle") or handle`` — an event that already carries
    its own handle is never overwritten.

    Priority: ``session_emit`` (controller bridge) → ``cli_sink`` → no-op.
    Failures are swallowed — emit must never break the calling tool.
    """
    session_emit = context.session_emit
    sink = context.cli_sink

    def _emit(event: dict[str, Any]) -> None:
        stamped = {**event, "handle": event.get("handle") or handle}
        if session_emit is not None:
            try:
                session_emit(stamped)
            except Exception:  # noqa: BLE001
                logger.debug("session emit: session_emit call failed", exc_info=True)
            return
        if sink is None:
            return
        try:
            coro = sink("session", "event", stamped)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return  # not inside a running loop — emit is best-effort
            task = loop.create_task(coro)
            _EMIT_TASKS.add(task)
            task.add_done_callback(_EMIT_TASKS.discard)
        except Exception:  # noqa: BLE001
            logger.debug("session emit: cli_sink call failed", exc_info=True)

    return _emit


def _turn_to_toolresult(result: TurnResult, handle: str) -> ToolResult:
    return ToolResult(
        output=result.result_text or f"session={handle} turn={result.turn_index} done",
        is_error=result.is_error,
        metadata={
            "handle": handle,
            "target": result.target,
            "turn_index": result.turn_index,
            "status": result.status.value,
        },
    )


def _resolve_registry(context: ToolContext) -> Any | None:
    reg = getattr(context, "interactive_sessions", None)
    return reg


def _resolve_spawns(context: ToolContext) -> Any | None:
    return getattr(context, "spawns", None)


_ALREADY_PENDING = object()  # sentinel returned when a bg turn is already in flight


async def _register_background(
    context: ToolContext,
    kind: str,
    coro,
    session: Any,
    *,
    goal: str | None = None,
) -> ToolResult:
    """Register coro on spawns, store spawn id on session, return ToolResult.

    Returns the sentinel ``_ALREADY_PENDING`` (not a ToolResult) when the
    session already has an uncollected background turn, so callers can
    produce an appropriate error ToolResult.
    """
    if getattr(session, "_pending_spawn_id", None) is not None:
        return _ALREADY_PENDING  # type: ignore[return-value]

    spawns = _resolve_spawns(context)
    if spawns is None:
        return ToolResult(
            output=(
                "session background mode unavailable: spawn registry not wired. "
                "Retry with background=false."
            ),
            is_error=True,
        )
    spawn_handle = spawns.register(kind=kind, coro=coro, goal=goal)
    session._pending_spawn_id = spawn_handle.handle_id
    return ToolResult(
        output=f"{kind} spawned in background: handle={session.handle}, spawn={spawn_handle.handle_id}.",
        metadata={
            "handle": session.handle,
            "spawn_handle": spawn_handle.handle_id,
            "status": "running",
        },
    )


async def _collect_background(
    context: ToolContext,
    session: Any,
    wait: bool,
    timeout: float | None,
) -> ToolResult:
    """Collect a pending background spawn from a session, using spawns registry."""
    spawn_id = getattr(session, "_pending_spawn_id", None)
    if spawn_id is None:
        return ToolResult(
            output=f"session handle={session.handle}: no pending background turn.",
            metadata={"handle": session.handle, "status": "idle"},
        )

    spawns = _resolve_spawns(context)
    if spawns is None:
        return ToolResult(
            output="spawn registry not wired — cannot collect result.",
            is_error=True,
        )

    spawn_handle = spawns.get(spawn_id)
    if spawn_handle is None:
        session._pending_spawn_id = None
        return ToolResult(
            output=f"spawn handle {spawn_id!r} not found (may have already been collected).",
            is_error=True,
        )

    if not wait and spawn_handle.is_running():
        return ToolResult(
            output=f"session handle={session.handle} spawn still running.",
            metadata={"handle": session.handle, "spawn_handle": spawn_id, "status": "running"},
        )

    try:
        if timeout is None:
            result = await asyncio.shield(spawn_handle.task)
        else:
            result = await asyncio.wait_for(asyncio.shield(spawn_handle.task), timeout=timeout)
    except asyncio.TimeoutError:
        return ToolResult(
            output=f"session_result timed out after {timeout}s — spawn still running.",
            is_error=True,
        )
    except asyncio.CancelledError:
        return ToolResult(
            output=f"session_result cancelled for spawn={spawn_id}.",
            is_error=True,
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            output=f"session_result spawn failed: {exc!r}",
            is_error=True,
        )
    finally:
        session._pending_spawn_id = None

    if not isinstance(result, ToolResult):
        return ToolResult(
            output=f"session_result unexpected result type {type(result).__name__}",
            is_error=True,
        )
    return result


# ─────────────────────────── input models ───────────────────────────────────


class SessionOpenInput(BaseModel):
    target: str = Field(description='Backend target: "claude", "codex", or an agent slug.')
    task: str = Field(description="Opening task prompt for this session.")
    background: bool = Field(default=False, description="Open the session in the background.")
    title: Optional[str] = Field(default=None, description="Human-readable session label.")


class SessionSendInput(BaseModel):
    handle: str = Field(description="Session handle returned by session_open.")
    message: str = Field(description="Message to send to the session.")
    background: bool = Field(default=False, description="Send in the background.")


class SessionResultInput(BaseModel):
    handle: str = Field(description="Session handle to collect a result for.")
    wait: bool = Field(default=True, description="Block until the background turn completes.")
    timeout: Optional[float] = Field(
        default=None, ge=1, le=1800,
        description="Max seconds to wait. Omit to block indefinitely.",
    )


class SessionCloseInput(BaseModel):
    handle: str = Field(description="Session handle to close.")


class SessionListInput(BaseModel):
    pass


# ─────────────────────────── tools ──────────────────────────────────────────


class SessionOpenTool(Tool):
    """Open an interactive multi-turn session against claude, codex, or an agent."""

    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "propose"

    group: ClassVar[str] = "long-running-collaborators"
    summary: ClassVar[str] = "Open a new interactive session against claude, codex, or a named agent."
    use_when: ClassVar[str] = (
        "Use to start a scoped per-chat conversation you drive with session_send, distinct from "
        "a controller-owned lane."
    )
    not_when: ClassVar[str] = (
        "a standing named collaborator surviving restarts, which is `lane_named_ensure`; a "
        "one-shot worker, which is `delegate_coder`/`delegate_auditor`."
    )

    def __init__(
        self,
        agents_dir=None,
        adapter=None,
        options=None,
        registry=None,
        max_tool_iterations: int = 10,
        max_consecutive_adapter_errors: int = 3,
    ) -> None:
        self._agents_dir = agents_dir
        self._adapter = adapter
        self._options = options
        self._registry = registry  # ToolRegistry (parent), for agent sessions
        self._max_tool_iterations = max_tool_iterations
        self._max_consecutive_adapter_errors = max_consecutive_adapter_errors

    @property
    def name(self) -> str:
        return "session_open"

    @property
    def input_schema(self) -> type[BaseModel]:
        return SessionOpenInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, SessionOpenInput)
            else SessionOpenInput(**tool_input.model_dump())
        )

        reg = _resolve_registry(context)
        if reg is None:
            return ToolResult(
                output="session_open: interactive_sessions not wired in this context.",
                is_error=True,
            )

        handle = reg.mint_handle(inp.target)
        emit = _make_emit(context, handle)

        if inp.target in ("claude", "codex"):
            from tesseract.kernel.tools._delegate_runner import resolve_cli_model

            adapter_cls = ClaudeStreamAdapter if inp.target == "claude" else CodexStreamAdapter
            try:
                # Explicit --model from roles.yaml — without it the CLI runs
                # its own account default, not the configured primary.
                model = resolve_cli_model(
                    "claude_cli" if inp.target == "claude" else "codex_cli"
                )
            except Exception as exc:  # noqa: BLE001 — config is authoritative
                return ToolResult(
                    output=f"session_open: model resolution failed: {exc}",
                    is_error=True,
                )
            _thresholds = _load_session_thresholds()
            _raw_timeout = _thresholds.get("total_turn_timeout_s")
            _turn_timeout: float | None = None
            try:
                if _raw_timeout is not None:
                    _turn_timeout = float(_raw_timeout)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pass
            session = CliSessionBackend(
                handle=handle,
                target=inp.target,
                adapter=adapter_cls(model=model),
                cwd=context.workspace_root or ".",
                emit=emit,
                cancel_event=context.cancel_event,
                turn_timeout=_turn_timeout,
            )
        else:
            # Agent backend — gate via ASK.
            ask_fn = context.ask_fn
            if ask_fn is None:
                return ToolResult(
                    output=(
                        f"session_open: agent backend {inp.target!r} requires operator approval, "
                        "but no approval channel is wired. Cannot open agent session."
                    ),
                    is_error=True,
                    denied_hard=True,
                    deny_reason="ASK posture with no approval channel",
                )
            approved = await ask_fn(self, inp, context)
            if not approved:
                return ToolResult(
                    output=f"operator declined session_open for agent {inp.target!r}.",
                    denied_hard=True,
                    deny_reason="operator declined",
                )

            try:
                chat_session = build_agent_session(
                    name=inp.target,
                    agents_dir=self._agents_dir,
                    parent_adapter=self._adapter,
                    parent_options=self._options,
                    parent_registry=self._registry,
                    max_tool_iterations=self._max_tool_iterations,
                    max_consecutive_adapter_errors=self._max_consecutive_adapter_errors,
                    tool_context=context,
                    policy=context.policy if hasattr(context, "policy") else None,
                    ask_fn=ask_fn,
                )
            except AgentBuildError as exc:
                return ToolResult(output=f"session_open: AgentBuildError: {exc}", is_error=True)

            session = AgentSessionBackend(
                handle=handle, target=inp.target, chat_session=chat_session, emit=emit
            )

        reg.add(session)

        if inp.background:
            coro = _open_and_wrap(session, inp.task, handle)
            bg_result = await _register_background(
                context, f"session_open:{inp.target}", coro, session, goal=inp.task
            )
            if bg_result is _ALREADY_PENDING:
                # Background turns on a freshly opened session cannot be
                # pending — this is a defensive guard that should never fire.
                reg.remove(handle)
                return ToolResult(
                    output=f"a background turn is already in flight for session {handle}; "
                           "call session_result(handle) to collect it before starting another.",
                    is_error=True,
                )
            # Lifecycle marker — notify the TUI rail that this background
            # session is registered and running (same signal as foreground).
            emit({"type": "session_status", "handle": handle, "target": inp.target, "status": "running"})
            return bg_result

        result = await session.open(inp.task)
        if result.is_error:
            reg.remove(handle)
        else:
            # Lifecycle marker — notify the TUI rail that this session is running.
            emit({"type": "session_status", "handle": handle, "target": inp.target, "status": "running"})
        return _turn_to_toolresult(result, handle)


async def _open_and_wrap(session: Any, task: str, handle: str) -> ToolResult:
    result = await session.open(task)
    return _turn_to_toolresult(result, handle)


class SessionSendTool(Tool):
    """Send a message to an existing interactive session."""

    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "propose"

    group: ClassVar[str] = "long-running-collaborators"
    summary: ClassVar[str] = "Send a follow-up message to an open session and return its reply."
    use_when: ClassVar[str] = (
        "Use for each turn of an open session; background=true returns a spawn handle instead "
        "of blocking."
    )
    not_when: ClassVar[str] = (
        "the lane equivalent, which is `lane_send`/`lane_turn`; re-fetching a background turn's "
        "result later, which is `session_result`."
    )

    @property
    def name(self) -> str:
        return "session_send"

    @property
    def input_schema(self) -> type[BaseModel]:
        return SessionSendInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, SessionSendInput)
            else SessionSendInput(**tool_input.model_dump())
        )

        reg = _resolve_registry(context)
        if reg is None:
            return ToolResult(
                output="session_send: interactive_sessions not wired.", is_error=True
            )

        session = reg.get(inp.handle)
        if session is None:
            return ToolResult(
                output=f"session_send: unknown handle {inp.handle!r}.", is_error=True
            )

        if inp.background:
            if getattr(session, "_pending_spawn_id", None) is not None:
                return ToolResult(
                    output=f"a background turn is already in flight for session {inp.handle}; "
                           "call session_result(handle) to collect it before starting another.",
                    is_error=True,
                )
            coro = _send_and_wrap(session, inp.message, inp.handle)
            bg_result = await _register_background(
                context, f"session_send:{session.target}", coro, session, goal=inp.message
            )
            if bg_result is _ALREADY_PENDING:  # defensive — should not reach here
                return ToolResult(
                    output=f"a background turn is already in flight for session {inp.handle}; "
                           "call session_result(handle) to collect it before starting another.",
                    is_error=True,
                )
            return bg_result

        result = await session.send(inp.message)
        return _turn_to_toolresult(result, inp.handle)


async def _send_and_wrap(session: Any, message: str, handle: str) -> ToolResult:
    result = await session.send(message)
    return _turn_to_toolresult(result, handle)


class SessionResultTool(Tool):
    """Collect the latest background turn result for a session handle."""

    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "long-running-collaborators"
    summary: ClassVar[str] = "Collect the result of a session's most recent background turn."
    use_when: ClassVar[str] = "Use only after a session_send/session_open call made with background=true."
    not_when: ClassVar[str] = (
        "a foreground call's reply, already in that call's own return value; a lane's reply, "
        "which is `lane_read`/`lane_turn`."
    )

    @property
    def name(self) -> str:
        return "session_result"

    @property
    def input_schema(self) -> type[BaseModel]:
        return SessionResultInput

    def is_read_only(self) -> bool:
        return False  # consumes the pending spawn

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, SessionResultInput)
            else SessionResultInput(**tool_input.model_dump())
        )

        reg = _resolve_registry(context)
        if reg is None:
            return ToolResult(
                output="session_result: interactive_sessions not wired.", is_error=True
            )

        session = reg.get(inp.handle)
        if session is None:
            return ToolResult(
                output=f"session_result: unknown handle {inp.handle!r}.", is_error=True
            )

        return await _collect_background(context, session, inp.wait, inp.timeout)


class SessionCloseTool(Tool):
    """Close an interactive session and remove it from the registry."""

    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "long-running-collaborators"
    summary: ClassVar[str] = "Close a session and free its resources. Idempotent on an already-closed handle."
    use_when: ClassVar[str] = "Use when a session's conversation is finished."
    not_when: ClassVar[str] = "terminating a lane's CLI process, which is `lane_close`."

    @property
    def name(self) -> str:
        return "session_close"

    @property
    def input_schema(self) -> type[BaseModel]:
        return SessionCloseInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, SessionCloseInput)
            else SessionCloseInput(**tool_input.model_dump())
        )

        reg = _resolve_registry(context)
        if reg is None:
            return ToolResult(output='{"ok": true}', metadata={"ok": True})

        session = reg.get(inp.handle)
        if session is not None:
            # Cancel any in-flight background turn before closing so the
            # subprocess doesn't become orphaned once the handle is gone.
            spawn_id = getattr(session, "_pending_spawn_id", None)
            if spawn_id is not None:
                spawns = _resolve_spawns(context)
                if spawns is not None:
                    try:
                        await spawns.cancel(spawn_id)
                    except Exception:  # noqa: BLE001
                        logger.debug("session_close: spawn cancel raised", exc_info=True)
                session._pending_spawn_id = None
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                logger.debug("session_close: close() raised", exc_info=True)
            reg.remove(inp.handle)
            # Lifecycle marker — notify the TUI rail that this session is done.
            emit = _make_emit(context, inp.handle)
            emit({"type": "session_status", "handle": inp.handle, "status": "done"})

        return ToolResult(output='{"ok": true}', metadata={"ok": True})


class SessionListTool(Tool):
    """List all open interactive sessions for this chat session."""

    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "long-running-collaborators"
    summary: ClassVar[str] = "List every open interactive session with its handle and target."
    use_when: ClassVar[str] = "Use to see what sessions are open before sending to or closing one."
    not_when: ClassVar[str] = (
        "lanes, which is `lane_list`/`lane_named_list`; controller sessions, which is "
        "`controller_session_list`."
    )

    @property
    def name(self) -> str:
        return "session_list"

    @property
    def input_schema(self) -> type[BaseModel]:
        return SessionListInput

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        reg = _resolve_registry(context)
        if reg is None:
            return ToolResult(output="[]", metadata={"sessions": []})

        sessions = reg.list()
        rows = [{"handle": s.handle, "target": s.target} for s in sessions]
        return ToolResult(output=json.dumps(rows), metadata={"sessions": rows})
