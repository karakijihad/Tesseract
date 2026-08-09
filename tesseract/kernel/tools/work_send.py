"""work_send — one verb to steer any running, steerable work.

Resolves the target across the three steerable substrates and routes:

* interactive session handle → the ``session_send`` path (background turn),
* named lane or raw ``lane-*`` id → the ``lane_turn`` path (background turn,
  completion note carries the reply),
* controller session id (``YYYY-MM-DD-xxxxxxxx``) → daemon ``user_input``
  (fire-and-forget; attach with ``agent --session <id>`` to watch).

One-shot spawn handles are REJECTED with guidance — a one-shot subprocess has
no input channel by construction (SUBSTRATE §4): cancel + re-dispatch, or put
steerable work on a lane/session next time.

Routing delegates to the underlying tools' ``run`` directly — ``work_send``
itself is the gated surface for this call; the inner tool's own posture gate
was already considered by the operator when they allowed this verb.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.agent_controller.lanes.tool_support import (
    maybe_await,
    resolve_named_lane_manager,
)
from tesseract.orchestrator.agent_controller.paths import SESSION_ID_RE


class WorkSendInput(BaseModel):
    target: str = Field(
        description=(
            "Where to send: a named lane (e.g. 'coder/claude'), a raw lane "
            "id ('lane-…'), an interactive session handle (session_open), "
            "or a controller session id (YYYY-MM-DD-xxxxxxxx)."
        )
    )
    message: str = Field(description="Instruction / course-correction to send.")


def _caller_owns(handle, context, spawns) -> bool:
    """True when this caller may steer ``handle`` from outside its registry.

    Ownership is read off the HANDLE, stamped by the registry that minted it —
    never off the caller's own registry, which would only ever agree with
    itself. Fails CLOSED: a handle with no recorded owner is not steerable
    cross-registry, because an unattested caller and the real owner must never
    resolve to the same answer.
    """
    caller_principal = getattr(context, "caller_principal", None)
    owner_principal = getattr(handle, "owner_principal", None)
    if owner_principal and caller_principal and owner_principal == caller_principal:
        return True
    owner_session = getattr(handle, "owner_session_id", None)
    caller_session = getattr(context, "session_id", None)
    return bool(owner_session and caller_session and owner_session == caller_session)


class WorkSendTool(Tool):
    default_posture: ClassVar[str] = "ask"
    risk_class: ClassVar[str] = "operator_gate"

    @property
    def name(self) -> str:
        return "work_send"

    @property
    def description(self) -> str:
        return (
            "Steer running background work: send a message into a named "
            "lane, an interactive session, or a controller session — one "
            "verb, target auto-resolved. One-shot spawn handles are not "
            "steerable (no input channel): cancel + re-dispatch instead."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return WorkSendInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: WorkSendInput = tool_input  # type: ignore[assignment]
        target = inp.target.strip()

        # 1) Spawn handle. A sub-agent spawn runs a turn loop and carries a
        #    steer channel; a one-shot subprocess has none by construction and
        #    is refused. Checked first so a handle can never fall through to a
        #    lane/session guess. Resolved process-wide, not per-registry: a
        #    sub-agent's handle was minted by its parent's registry, so the
        #    caller's own registry would miss it.
        from tesseract.brain.spawns import find_handle

        spawns = getattr(context, "spawns", None)
        handle = spawns.get(target) if spawns is not None else None
        if handle is None:
            # Cross-registry reach exists for one legitimate case: a parent
            # answering a child it spawned. Gate the process-global index on
            # the caller owning the handle — `work_send` is AUTO posture, so
            # an unchecked global lookup would let any session inject
            # instructions into another session's background work.
            candidate = find_handle(target)
            if candidate is not None and _caller_owns(candidate, context, spawns):
                handle = candidate
        if handle is not None:
            if handle.steer_fn is None:
                return ToolResult(
                    output=(
                        f"{target} is a one-shot background spawn — it has no "
                        "input channel and cannot be steered. spawn_cancel it "
                        "and re-dispatch with the new instruction, or run "
                        "steerable work on a lane (lane_turn) or an interactive "
                        "session (session_open) next time."
                    ),
                    is_error=True,
                    metadata={"reason": "unsteerable_one_shot", "target": target},
                )
            answered = handle.question
            if not handle.steer(inp.message):
                return ToolResult(
                    output=(
                        f"{target} is no longer running — nothing to steer. "
                        "Use spawn_check for its result."
                    ),
                    is_error=True,
                    metadata={"reason": "spawn_not_running", "target": target},
                )
            return ToolResult(
                output=(
                    f"delivered to {target}; it will apply this and continue."
                    + (" This answers its pending question." if answered else "")
                ),
                metadata={
                    "target": target,
                    "route": "spawn_steer",
                    "answered_question": bool(answered),
                },
            )

        # 2) Interactive session handle.
        sessions = getattr(context, "interactive_sessions", None)
        session = sessions.get(target) if sessions is not None else None
        if session is not None:
            from tesseract.kernel.tools.session_tools import (
                SessionSendInput,
                SessionSendTool,
            )

            # M2 — cancel + resend: if a turn is in flight, cancel its spawn
            # (task-cancel kills the CLI subprocess via the turn loop's finally)
            # so the correction runs now instead of being rejected as "already
            # in flight". Clearing _pending_spawn_id lets the resend proceed.
            interrupted = False
            spawn_id = getattr(session, "_pending_spawn_id", None)
            if spawn_id is not None:
                spawns = getattr(context, "spawns", None)
                if spawns is not None:
                    interrupted = bool(await spawns.cancel(spawn_id))
                session._pending_spawn_id = None

            result = await SessionSendTool().run(
                SessionSendInput(handle=target, message=inp.message, background=True),
                context,
            )
            if interrupted and result.metadata is not None:
                result.metadata["interrupted_prior_turn"] = True
            return result

        # 3) Controller session id → daemon user_input (fire-and-forget).
        if SESSION_ID_RE.fullmatch(target):
            return await self._steer_controller_session(target, inp.message)

        # 4) Named lane binding or raw lane id → background lane turn.
        named_mgr = resolve_named_lane_manager(context)
        named_record = None
        if named_mgr is not None:
            try:
                named_record = await maybe_await(named_mgr.get(target))
            except Exception:  # noqa: BLE001 — fall through to shape checks
                named_record = None
        if named_record is not None or target.startswith("lane-"):
            from tesseract.kernel.tools.lane_turn import LaneTurnInput, LaneTurnTool
            from tesseract.orchestrator.agent_controller.lanes.tool_support import (
                resolve_lane_manager,
            )

            lane_id = named_record.lane_id if named_record is not None else target
            # M2 — real steer: cancel any in-flight lane turn so the correction
            # runs now (cancel + resend) instead of queuing behind the current
            # turn's lock. interrupt() is a no-op on an idle lane; best-effort
            # (a manager without interrupt degrades to queued-next-turn).
            manager = resolve_lane_manager(context)
            interrupted = False
            interrupt_fn = getattr(manager, "interrupt", None) if manager else None
            if interrupt_fn is not None:
                try:
                    interrupted = bool(await maybe_await(interrupt_fn(lane_id)))
                except Exception:  # noqa: BLE001 — steer is best-effort
                    interrupted = False

            result = await LaneTurnTool().run(
                LaneTurnInput(name_or_id=target, message=inp.message),
                context,
            )
            if interrupted and result.metadata is not None:
                result.metadata["interrupted_prior_turn"] = True
            # 'not attached' self-heal now lives in lane_turn (M6), so it covers
            # the raw lane_turn path too — no duplicate retry here.
            return result

        return ToolResult(
            output=(
                f"work_send: no steerable target {target!r}. Accepted: a "
                "named lane, a raw lane-* id, an interactive session handle, "
                "or a controller session id (YYYY-MM-DD-xxxxxxxx)."
            ),
            is_error=True,
            metadata={"reason": "unknown_target", "target": target},
        )

    async def _steer_controller_session(
        self, session_id: str, message: str
    ) -> ToolResult:
        from tesseract.orchestrator.agent_controller.ipc_client import (
            ControllerClient,
            ControllerClientError,
        )

        try:
            client = await ControllerClient.connect()
        except ControllerClientError as exc:
            return ToolResult(
                output=f"work_send: controller offline ({exc})",
                is_error=True,
                metadata={"reason": "controller_offline", "target": session_id},
            )
        try:
            # M9 — await the daemon ack so a stale/unknown session id surfaces
            # as an error instead of a false "sent" success.
            await client.user_input(session_id, message, await_ack=True)
        except ControllerClientError as exc:
            return ToolResult(
                output=f"work_send: user_input failed for {session_id}: {exc}",
                is_error=True,
                metadata={"reason": "user_input_failed", "target": session_id},
            )
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
        return ToolResult(
            output=(
                f"sent into controller session {session_id}. Watch with "
                f"`agent --session {session_id}` or agent_review."
            ),
            metadata={"target": session_id, "route": "controller_user_input"},
        )


__all__ = ["WorkSendInput", "WorkSendTool"]
