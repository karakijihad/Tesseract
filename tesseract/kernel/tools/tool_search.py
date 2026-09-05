"""``tool_search`` — meta-tool for the tool-schema tiering system.

Chat sessions start with only the "core" working set visible to the model
(`working_set.yaml::core`; see `Tool.tier` in
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

import asyncio
import logging
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.home_tools import sync_home_tools
from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult


#: The registry leaves this one out of a deferred payload — the provider's own
#: server-side search does the same job inside the turn, and two search tools
#: in one request is a model choosing between doors into the same room.
TOOL_SEARCH_NAME = "tool_search"

logger = logging.getLogger(__name__)


# The highest rank that counts as the query NAMING a tool: exact name, every
# term in the name, any term in the name. Rank 3 is a description mention,
# which is a different question — "which of these did you mean" rather than
# "make this one callable".
#
# It has to stay at 2, and the reason is not obvious enough to leave unsaid.
# Naming SEVERAL tools at once lands every one of them at rank 2, not rank 1:
# two exact names cannot both be substrings of a single tool's name, so `all`
# fails and `any` is what matches. Narrowing this to 1 to stop a phrase from
# unlocking a family would enable nothing at all when the model asks for the
# three tools it actually wants. Pinned by
# `instruction_surface_IS_3::test_naming_several_tools_enables_exactly_those`.
#
# Measured against the live registry, 83 extended tools: an exact name enables
# 1, a list of names enables exactly those, and a bare word enables its whole
# family (10 for `browser`, 9 for `send`). The family case is the one that
# costs, so `use_when` tells the model to name what it wants.
_NAMED = 2


class ToolSearchInput(BaseModel):
    query: str = Field(
        ...,
        min_length=1,
        description=(
            "The tool names you want, space separated, for example "
            "'browser_navigate browser_click'. Plain words when you do not "
            "know the name."
        ),
    )


class ToolSearchTool(Tool):
    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"
    tier: ClassVar[str] = "core"

    group: ClassVar[str] = "finding-a-tool"
    summary: ClassVar[str] = "Make a tool callable that is not in this turn's schema."
    use_when: ClassVar[str] = (
        "Most tools are not in this turn's schema. They still exist. Your "
        "tool map names every one, so name the ones you want and they become "
        "callable for the rest of the session. Several at once is fine. Use "
        "plain words only when you do not know the name."
    )
    not_when: ClassVar[str] = (
        "a tool already visible in this turn's schema needs no search. Call "
        "it directly."
    )
    depends_on: ClassVar[str] = ""

    @property
    def name(self) -> str:
        return TOOL_SEARCH_NAME

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

        # Pick up anything new under `<home>/tools/` before searching. This is
        # what makes a tool the assistant just wrote callable in the same
        # session: the registry is live, and looking for a tool is the act
        # that would follow writing one.
        #
        # Off the loop: it reads every changed file and imports it, and an
        # import runs arbitrary operator code of unknown duration. Health
        # checks, WS heartbeats and other turns keep running while it does.
        home = await asyncio.to_thread(sync_home_tools, registry)
        terms = [t for t in inp.query.lower().split() if t]
        if not terms:
            return ToolResult(output=f"tool_search({inp.query!r}): empty query", is_error=True)

        # Ranked, because the glossary decides what a search IS. The model
        # reads a name off the map and comes here to make it callable, so an
        # exact name must be first, or the one result that is certainly right
        # arrives behind six that merely mention it.
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
        # Unlock what the query NAMED. Not what merely mentions it.
        #
        # Enabling is the expensive half and searching is the cheap one. Tool
        # schemas are serialised ahead of the system prompt and the whole
        # conversation, so every change to that array re-reads the entire
        # prompt at full price and adds its schemas to every request after.
        # Measured 2026-08-31: one search on an ordinary word matched most of
        # the 91 extended descriptions and unlocked all of them at once, for
        # an 85,595-token re-read and ~24,000 tokens of schema on every later
        # call, in a conversation that went on to call none of them.
        #
        # A description hit is DISCOVERY, and discovery has already happened
        # somewhere cheaper: the tool map in the system prompt lists all 150
        # tools with a line each for 3,531 tokens, against 54,000 for their
        # schemas. The map is what the model reads to learn a name; this tool
        # is where it comes to make that name callable. So a phrase query
        # still answers "which one do you mean", and the answer costs nothing
        # until the model asks for one by name — which is rank 0, returns one
        # tool, and enables it in the same call. The round trip the working
        # set is built around, and no more than that.
        # `enabled_extended_tools` is `None` on a context nobody wired one
        # into — `scheduler/tasks/tool_call.py` builds exactly that. Nothing is
        # enabled there, so nothing may be REPORTED as enabled: a result that
        # says a tool is callable when it is not is the loop this wording was
        # added to prevent, told by the line that was meant to prevent it.
        named = [tool for rank, _name, tool in scored if rank <= _NAMED]
        if context.enabled_extended_tools is None:
            newly_enabled: list = []
        else:
            newly_enabled = named
            for tool in newly_enabled:
                context.enabled_extended_tools.add(tool.name)
        enabled_names = {t.name for t in newly_enabled}
        # What this call actually unlocked, by name and by the rank that
        # earned it. Enabling is the expensive half: the tool array sits ahead
        # of the whole conversation with nowhere to put a cache breakpoint, so
        # every unlock re-reads the entire prompt at full price and every
        # schema rides on every request afterwards. Whether that buys anything
        # is the question of whether the model then CALLS what it unlocked,
        # and until this line existed there was no way to answer it: the
        # request logs show the tool count stepping up and nothing about what
        # the step was for.
        if enabled_names:
            by_rank = ", ".join(
                f"{tool.name}(r{rank})"
                for rank, _name, tool in scored
                if tool.name in enabled_names
            )
            logger.info(
                "tool_search: query=%r matched=%d enabled=%d [%s]",
                inp.query, len(matches), len(enabled_names), by_rank,
            )

        # A tool the assistant just wrote and that failed to load is the one
        # case where "no match" would be a lie. Say what broke, every time,
        # on both paths: silence here is how a session concludes a capability
        # does not exist when the truth is that its file has a typo.
        notes = ""
        if home.unreadable:
            # Not a load failure: nothing was read and nothing was lost. Saying
            # "did not load" here would tell the operator their tools are gone
            # while every one of them is still registered and callable.
            notes = "\n\n" + home.unreadable
        elif home.errors:
            broken = "\n".join(f"- {message}" for message in home.errors)
            notes = (
                "\n\nSome files in the tools folder did not load, so nothing "
                f"they define is callable yet:\n{broken}"
            )
        elif home.loaded:
            notes = f"\n\nNewly loaded from the tools folder: {', '.join(home.loaded)}."

        if not matches:
            return ToolResult(
                output=f"tool_search({inp.query!r}): no extended tools matched{notes}"
            )

        # Say which half is which, in the result the model reads. A tool
        # listed as found but not enabled and no way to tell them apart is
        # how a model calls a name it cannot call, gets an unknown-tool
        # error, re-runs the same search and loops. The line says the exact
        # recovery: ask for it by name.
        lines = [
            f"- {t.name}: {t.description}"
            + ("" if t.name in enabled_names else "  (found, not enabled)")
            for t in matches
        ]
        found_only = len(matches) - len(newly_enabled)
        summary = (
            f"tool_search({inp.query!r}) matched {len(matches)} tool(s); "
            f"{len(newly_enabled)} now enabled for this session"
        )
        if found_only:
            summary += (
                f", {found_only} matched on description only. To use one of "
                "those, run tool_search again with its exact name"
            )
        return ToolResult(
            output=summary + ":\n" + "\n".join(lines) + notes,
            metadata={"matches": [t.to_schema() for t in newly_enabled]},
        )


__all__ = ["ToolSearchTool", "ToolSearchInput"]
