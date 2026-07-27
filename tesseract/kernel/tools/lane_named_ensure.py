"""lane_named_ensure — get-or-open a named lane in one ASK gate.

X-5 Session A. Idempotent: reuses the bound lane when alive, opens a
fresh one (under the same name) when no binding exists or the bound
lane is dead. ASK-gated by default — `ensure` may spawn a CLI
subprocess on the open-new branch, which is operator-visible work.

`kind` mismatch raises rather than silently swapping; operators
release the binding (future tool) before re-pointing a name at a new
kind."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.tars_controller.lanes.named import (
    InvalidNamedLaneNameError,
    NamedLaneError,
)
from tesseract.orchestrator.tars_controller.lanes.tool_support import (
    resolve_named_lane_manager,
    validate_lane_model,
)


class LaneNamedEnsureInput(BaseModel):
    name: str = Field(
        description=(
            "Named-lane label (e.g. 'coder/claude'). Must match "
            "[a-z0-9_-]+(/[a-z0-9_-]+)?."
        )
    )
    kind: Literal["claude", "codex"] = Field(
        description=(
            "Lane kind: 'claude' or 'codex'. Must match any existing "
            "binding for this name — kind swap requires release first."
        )
    )
    model: str = Field(
        description=(
            "Model id the lane should target. For the trio lanes use the "
            "config-resolved model (lane_named_get / the trio definition) — "
            "never invent one. Recorded on the binding + passed as --model "
            "when the CLI spawns."
        )
    )
    working_dir: str = Field(
        description=(
            "Working directory the CLI runs in. Recorded on the binding "
            "so a brain-restart-driven attach knows where the lane lives."
        )
    )


class LaneNamedEnsureTool(Tool):
    default_posture = "ask"
    risk_class: ClassVar[str] = "operator_gate"

    @property
    def name(self) -> str:
        return "lane_named_ensure"

    @property
    def description(self) -> str:
        return (
            "Get-or-open a named lane in one call. Reuses the bound "
            "lane_id when alive; opens a fresh lane under the same name "
            "when no binding exists or the bound lane is dead. Returns "
            "the binding record. ASK-gated because the open branch "
            "spawns a CLI subprocess."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return LaneNamedEnsureInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: LaneNamedEnsureInput = tool_input  # type: ignore[assignment]
        manager = resolve_named_lane_manager(context)
        if manager is None:
            return ToolResult(
                output="lane_named_ensure unavailable: NamedLaneManager not wired",
                is_error=True,
            )
        model_error = validate_lane_model(inp.kind, inp.model)
        if model_error is not None:
            return ToolResult(
                output=f"lane_named_ensure: {model_error}", is_error=True
            )
        try:
            record = await manager.ensure(
                inp.name,
                kind=inp.kind,
                model=inp.model,
                working_dir=inp.working_dir,
            )
        except InvalidNamedLaneNameError as exc:
            return ToolResult(output=f"lane_named_ensure: {exc}", is_error=True)
        except NamedLaneError as exc:
            return ToolResult(output=f"lane_named_ensure: {exc}", is_error=True)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                output=f"lane_named_ensure failed: {exc}",
                is_error=True,
            )
        return ToolResult(
            output=(
                f"name={record.name} lane_id={record.lane_id} "
                f"kind={record.kind} mode={record.mode}"
            ),
            metadata=record.model_dump(mode="json"),
        )
