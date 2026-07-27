"""``tool_search`` — meta-tool for the tool-schema tiering system.

Lean-agent-os P1 Task 2. Chat sessions start with only "core" tool
schemas visible to the model (see `Tool.tier` in `kernel/tools/base.py`
and `ToolRegistry.schemas_for_adapter` in `brain/tools.py`) to cut
per-turn schema noise from ~125 tools to ~40. Extended tools stay fully
executable by name — registry lookup and `decide.evaluate` are
unaffected by tier — they are just not advertised up front.

This tool searches the FULL registry (every tool, any tier) by simple
substring match against name + description. Only the "extended" matches
are interesting to report (core tools are already visible) — matching
extended tools are returned with their schema AND added to the
session's `enabled_extended_tools` set (threaded via `ToolContext`,
owned by `ChatSession._enabled_extended_tools`), so the very next
`schemas_for_adapter()` call includes them without another round trip.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult


class ToolSearchInput(BaseModel):
    query: str = Field(
        ...,
        min_length=1,
        description=(
            "Keyword(s) to match against extended-tool names + "
            "descriptions, e.g. 'mission' or 'surface highlight'."
        ),
    )


class ToolSearchTool(Tool):
    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"
    tier: ClassVar[str] = "core"

    @property
    def name(self) -> str:
        return "tool_search"

    @property
    def description(self) -> str:
        return (
            "Search the full tool registry (beyond the core set always "
            "visible) by keyword against tool name + description. "
            "Matching tools' schemas are returned AND enabled for the "
            "rest of this session, so a later turn can call them "
            "directly without searching again."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return ToolSearchInput

    def is_read_only(self) -> bool:
        return True

    def is_concurrency_safe(self) -> bool:
        return True

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, ToolSearchInput)
            else ToolSearchInput.model_validate(tool_input.model_dump())
        )
        if context.tool_registry_provider is None:
            return ToolResult(
                output="tool_search unavailable: registry not wired in this runtime",
                is_error=True,
            )
        registry = context.tool_registry_provider()
        terms = [t for t in inp.query.lower().split() if t]
        if not terms:
            return ToolResult(output=f"tool_search({inp.query!r}): empty query", is_error=True)

        matches = []
        for tool in registry.tools.values():
            if getattr(tool, "tier", "extended") != "extended":
                continue
            haystack = f"{tool.name} {tool.description}".lower()
            if any(term in haystack for term in terms):
                matches.append(tool)

        if context.enabled_extended_tools is not None:
            for tool in matches:
                context.enabled_extended_tools.add(tool.name)

        if not matches:
            return ToolResult(output=f"tool_search({inp.query!r}): no extended tools matched")

        lines = [f"- {t.name}: {t.description}" for t in matches]
        return ToolResult(
            output=(
                f"tool_search({inp.query!r}) matched {len(matches)} tool(s), "
                "now enabled for this session:\n" + "\n".join(lines)
            ),
            metadata={"matches": [t.to_schema() for t in matches]},
        )


__all__ = ["ToolSearchTool", "ToolSearchInput"]
