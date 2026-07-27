"""work_send — one verb to steer any running, steerable work (trio W3).

Resolves the target across the three steerable substrates and routes:

* interactive session handle → the ``session_send`` path (background turn),
* named lane or raw ``lane-*`` id → the ``lane_turn`` path (background turn,
  completion note carries the reply),
* controller session id (``YYYY-MM-DD-xxxxxxxx``) → daemon ``user_input``
  (fire-and-forget; attach with ``tars --session <id>`` to watch).

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
from tesseract.orchestrator.tars_controller.lanes.tool_support import (
    maybe_await,
    resolve_named_lane_manager,
)
from tesseract.orchestrator.tars_controller.paths import SESSION_ID_RE


class WorkSendInput(BaseModel):
    target: str = Field(
        description=(
            "Where to send: a named lane (e.g. 'coder/claude'), a raw lane "
            "id ('lane-…'), an interactive session handle (session_open), "
            "or a controller session id (YYYY-MM-DD-xxxxxxxx)."
        )
    )
    message: str = Field(description="Instruction / course-correction to send.")


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

        # 1) One-shot spawn handle → reject with guidance (checked first so
        #    a spawn handle can never fall through to a lane/session guess).
        spawns = getattr(context, "spawns", None)
        if spawns is not None and spawns.get(target) is not None:
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
            from tesseract.orchestrator.tars_controller.lanes.tool_support import (
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
            # the trio's raw lane_turn path too — no duplicate retry here.
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
        from tesseract.orchestrator.tars_controller.ipc_client import (
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
                f"`tars --session {session_id}` or agent_review."
            ),
            metadata={"target": session_id, "route": "controller_user_input"},
        )


__all__ = ["WorkSendInput", "WorkSendTool"]
