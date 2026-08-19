"""``tool_search`` — meta-tool for the tool-schema tiering system.

Chat sessions start with only the "core" working set visible to the model
(`_CORE_TOOL_NAMES` in `brain/boot.py`; see `Tool.tier` in
`kernel/tools/base.py` and `ToolRegistry.schemas_for_adapter` in
`brain/tools.py`) so the schemas riding every turn are a fraction of the
registry. Extended tools stay fully executable by name — registry lookup
and `decide.evaluate` are unaffected by tier — they are just not
advertised up front.

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

    group: ClassVar[str] = "finding-a-tool"
    summary: ClassVar[str] = "Search the full tool registry for tools not currently visible."
    use_when: ClassVar[str] = (
        "Most tools are not in this turn's schema — they still exist and "
        "still run, just unlisted. Search by keyword against tool name and "
        "description whenever the task needs a capability you cannot see; "
        "matches are returned AND enabled for the rest of the session. "
        "Search here before concluding a tool does not exist."
    )
    not_when: ClassVar[str] = (
        "a tool already visible in this turn's schema needs no search — call "
        "it directly."
    )

    @property
    def name(self) -> str:
        return "tool_search"

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

        # Ranked, because the glossary changed what a search IS. It used to be
        # a guess against a registry the model could not see, so any hit was
        # progress and order did not matter. Now the model reads a name off the
        # map and comes here to make it callable — so an exact name must be
        # first, or the one result that is certainly right arrives behind six
        # that merely mention it.
        query = inp.query.strip().lower()
        scored: list[tuple[int, str, object]] = []
        for tool in registry.tools.values():
            if getattr(tool, "tier", "extended") != "extended":
                continue
            name = tool.name.lower()
            if name == query:
                rank = 0
            elif all(t in name for t in terms):
                rank = 1
            elif any(t in name for t in terms):
                rank = 2
            elif any(t in tool.description.lower() for t in terms):
                rank = 3
            else:
                continue
            scored.append((rank, name, tool))
        scored.sort(key=lambda row: (row[0], row[1]))
        # An exact name is not a search, it is a request. Returning the six
        # tools that merely mention it alongside costs six schemas to answer a
        # question that had one answer.
        if scored and scored[0][0] == 0:
            scored = scored[:1]
        matches = [tool for _rank, _name, tool in scored]

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
