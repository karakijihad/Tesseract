"""controller_session_list — list agent controller sessions on disk.

The chat brain spawns controller sessions via start_controller_session /
delegate_agent_controller. Those live in the controller registry (separate
from brain.session_store). This tool reads them so the brain can poll a
detached session's status. Read-only — AUTO tier.
"""
from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult

# Mirror of `agent_controller.sessions.SessionStatus`, declared inline so the
# tool schema does not pull the heavy orchestrator package at registration —
# the runtime call still imports SessionRegistry lazily inside `run`.
ControllerSessionStatus = Literal["active", "idle", "detached", "closed"]


class ControllerSessionListInput(BaseModel):
    status: ControllerSessionStatus | None = Field(
        default=None,
        description="Optional filter: active | idle | detached | closed.",
    )
    limit: int = Field(default=20, ge=1, le=100)


class ControllerSessionListTool(Tool):
    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "long-running-collaborators"
    summary: ClassVar[str] = "List agent controller sessions on disk, newest first, with status."
    use_when: ClassVar[str] = "Use to check whether a detached session from start_controller_session finished."
    not_when: ClassVar[str] = "lanes or interactive sessions, which are `lane_list`/`session_list`."

    @property
    def name(self) -> str:
        return "controller_session_list"

    @property
    def input_schema(self) -> type[BaseModel]:
        return ControllerSessionListInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        from tesseract.orchestrator.agent_controller.sessions import SessionRegistry

        inp: ControllerSessionListInput = tool_input  # type: ignore[assignment]
        records = SessionRegistry().list_sessions(status=inp.status)
        records = sorted(records, key=lambda r: r.last_active_at, reverse=True)[: inp.limit]
        if not records:
            return ToolResult(output="no controller sessions", metadata={"count": 0, "sessions": []})
        lines, entries = [], []
        for r in records:
            lines.append(
                f"- {r.session_id}  ({r.mode} · {r.status} · {r.last_active_at}) {r.title or ''}".rstrip()
            )
            entries.append({
                "session_id": r.session_id, "title": r.title, "mode": r.mode,
                "origin": r.origin, "status": r.status, "last_active_at": r.last_active_at,
            })
        return ToolResult(output="\n".join(lines), metadata={"count": len(records), "sessions": entries})
