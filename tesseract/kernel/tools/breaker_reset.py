"""breaker_reset tool — start trying again now, rather than at the end of the wait.

A breaker closes on its own once its cooldown is up, so this is not how an
outage ends. It is how the operator says the cause is gone and the wait is no
longer worth serving, from wherever they happen to be.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult


class BreakerResetInput(BaseModel):
    name: str = Field(description="Which breaker to clear, as `breaker_status` names it (for example 'spawn-wake', or a model's ref like 'api.openai.gpt56_luna').")


class BreakerResetTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "propose"

    group: ClassVar[str] = "checking-your-state"
    summary: ClassVar[str] = "Clears one open circuit breaker so the runtime tries that capability again now."
    use_when: ClassVar[str] = (
        "Use when the operator has fixed what a breaker tripped on and does "
        "not want to wait out the cooldown. Call `breaker_status` first: it "
        "names what is open and what the breaker said. It covers a model the "
        "runtime has stopped calling too, so an account that has just been "
        "topped up need not sit out its hour."
    )
    not_when: ClassVar[str] = (
        "the cause is still there, since clearing it only spends one attempt and re-opens it for longer; "
        "a crash storm that stopped the supervisor, which is cleared with "
        "`python -m tesseract.scripts.clear_crash_storm` and is deliberately not a tool."
    )
    depends_on: ClassVar[str] = ""

    @property
    def name(self) -> str:
        return "breaker_reset"

    @property
    def input_schema(self) -> type[BaseModel]:
        return BreakerResetInput

    def is_concurrency_safe(self) -> bool:
        return False

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        from tesseract.context.circuit_breaker import breaker_report, clear_breaker
        from tesseract.paths import log_dir

        inp: BreakerResetInput = tool_input  # type: ignore[assignment]
        breakers = log_dir("circuit-breakers")
        said = clear_breaker(inp.name, breakers)
        if said is None:
            known = ", ".join(r["name"] for r in breaker_report(breakers))
            msg = f"there is no breaker called {inp.name!r}"
            if known:
                msg += f". There is: {known}"
            return ToolResult(output=msg, is_error=True)
        return ToolResult(output=said, metadata={"name": inp.name})
