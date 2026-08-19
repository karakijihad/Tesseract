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
from tesseract.orchestrator.agent_controller.lanes.named import (
    InvalidNamedLaneNameError,
    NamedLaneError,
)
from tesseract.orchestrator.agent_controller.lanes.tool_support import (
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
    kind: Literal["claude", "codex", "api"] = Field(
        description=(
            "Lane kind: 'claude' or 'codex'. Must match any existing "
            "binding for this name — kind swap requires release first."
        )
    )
    model: str = Field(
        description=(
            "Model id the lane should target. Use the config-resolved model "
            "(lane_named_get, or the seat's roles.yaml primary) — "
            "never invent one. Recorded on the binding + passed as --model "
            "when the CLI spawns."
        )
    )
    working_dir: str | None = Field(
        default=None,
        description=(
            "Working directory the CLI runs in. Omit to use the active "
            "project's root — that is the normal case, and passing a path is "
            "how you deliberately send a lane somewhere else. Recorded on the "
            "binding so a brain-restart-driven attach knows where the lane "
            "lives."
        ),
    )


class LaneNamedEnsureTool(Tool):
    default_posture = "ask"
    risk_class: ClassVar[str] = "operator_gate"

    group: ClassVar[str] = "long-running-collaborators"
    summary: ClassVar[str] = (
        "Get-or-open a named, persistent lane in one call — reuses it if alive, opens fresh if not."
    )
    use_when: ClassVar[str] = (
        "Use for a standing collaborator you address by name across turns and sessions, not a throwaway."
    )
    not_when: ClassVar[str] = (
        "an unnamed one-off lane, which is `lane_open`; looking up an existing binding without "
        "opening, which is `lane_named_get`."
    )

    @property
    def name(self) -> str:
        return "lane_named_ensure"

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
