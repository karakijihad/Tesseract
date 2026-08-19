"""lane_open — spawn a new persistent controller-owned lane.

X-4 Session B. ASK-gated by default — opening a lane spawns a CLI
subprocess (claude / codex) which is operator-visible work. The
`LaneManager` owns the process; this tool just records intent and
returns the new `lane_id`."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.agent_controller.lanes.tool_support import (
    resolve_lane_manager,
    validate_lane_model,
)


class LaneOpenInput(BaseModel):
    kind: str = Field(
        description=(
            "Lane kind: 'claude' or 'codex'. Selects the CLI binary + "
            "stream-JSON detector the lane uses."
        )
    )
    model: str = Field(
        description=(
            "Model id the lane should target. Use the config-resolved model "
            "(the seat's roles.yaml primary) — never invent one. "
            "Recorded in `lane.json` and passed as --model when the CLI spawns."
        )
    )
    working_dir: str = Field(
        description=(
            "Working directory the CLI runs in. Tools and file paths "
            "the lane emits resolve relative to this."
        )
    )
    shared_with: list[str] = Field(
        default_factory=list,
        description=(
            "MCP client identities allowed to operate on this lane alongside "
            "the one opening it (e.g. ['lane-codex']). Empty means the lane "
            "answers only to its owner and the operator. Naming a "
            "collaborator here is the deliberate way two workers share a "
            "lane; it is never inferred."
        ),
    )


class LaneOpenTool(Tool):
    default_posture = "ask"
    risk_class: ClassVar[str] = "operator_gate"

    group: ClassVar[str] = "long-running-collaborators"
    summary: ClassVar[str] = "Spawn a new unnamed CLI lane (claude or codex) that persists across turns."
    use_when: ClassVar[str] = (
        "Use to start a fresh, unnamed collaborator lane for one-off multi-turn work you drive "
        "yourself with lane_send/lane_turn."
    )
    not_when: ClassVar[str] = (
        "a lane bound to a reusable name, which is `lane_named_ensure`; a one-shot worker, which "
        "is `delegate_coder`/`delegate_auditor`."
    )

    @property
    def name(self) -> str:
        return "lane_open"

    @property
    def input_schema(self) -> type[BaseModel]:
        return LaneOpenInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: LaneOpenInput = tool_input  # type: ignore[assignment]
        manager = resolve_lane_manager(context)
        if manager is None:
            return ToolResult(
                output="lane_open unavailable: LaneManager not wired in this runtime",
                is_error=True,
            )
        model_error = validate_lane_model(inp.kind, inp.model)
        if model_error is not None:
            return ToolResult(output=f"lane_open: {model_error}", is_error=True)
        try:
            lane_id = await manager.open(
                kind=inp.kind,  # type: ignore[arg-type]
                model=inp.model,
                working_dir=inp.working_dir,
                shared_with=inp.shared_with,
            )
        except Exception as exc:  # noqa: BLE001 — surface as clean tool error
            return ToolResult(
                output=f"lane_open failed: {exc}",
                is_error=True,
            )
        return ToolResult(
            output=f"lane_id={lane_id}",
            metadata={
                "lane_id": lane_id,
                "kind": inp.kind,
                "model": inp.model,
            },
        )
