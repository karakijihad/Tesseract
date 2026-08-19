"""spawn_await — block on a background spawn's result.

Phase 4 of the assistant reboot CLI-parity plan. The "I'm done with other
work, give me the result" call. Returns whatever the foreground tool
would have returned, or an error string on cancellation / timeout.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar, Optional

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import (
    PermissionResult,
    Tool,
    ToolContext,
    ToolResult,
)


class SpawnAwaitInput(BaseModel):
    handle: str = Field(min_length=1, max_length=120)
    timeout: Optional[float] = Field(
        default=None,
        ge=1,
        le=1800,
        description=(
            "Optional max seconds to wait. Omit to block indefinitely "
            "(the spawn's own timeout caps total wall-clock anyway)."
        ),
    )


class SpawnAwaitTool(Tool):
    default_posture: ClassVar[str] = "auto"

    risk_class: ClassVar[str] = "autonomous"

    # A CLI's reply is whatever it read out of a repository, a web page, or
    # a compromised model — untrusted by origin, however trusted the tool
    # that fetched it. Set here so the foreground result and `spawn_await`'s
    # retrieval get the same envelope the background completion delivery
    # applies in `chat.py::_format_spawn_completion`; wrapping only the one
    # path left the other two open.
    untrusted_source: ClassVar[bool] = True

    group: ClassVar[str] = "tracking-spawned-work"
    summary: ClassVar[str] = (
        "Blocks until a background spawn finishes and returns its full result."
    )
    use_when: ClassVar[str] = (
        "Use only when a completed spawn's result said it was too large to "
        "deliver whole and told you to fetch it here."
    )
    not_when: ClassVar[str] = (
        "Routine retrieval — a finished spawn's output is delivered to you "
        "automatically on your next turn without polling; don't call this or "
        "`spawn_check` to wait for it."
    )

    @property
    def name(self) -> str:
        return "spawn_await"

    @property
    def input_schema(self) -> type[BaseModel]:
        return SpawnAwaitInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return False  # consumes the spawn task

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, SpawnAwaitInput)
            else SpawnAwaitInput(**tool_input.model_dump())
        )
        registry = getattr(context, "spawns", None)
        if registry is None:
            return ToolResult(
                output="spawn_await unavailable: registry not wired",
                is_error=True,
            )
        handle = registry.get(inp.handle)
        if handle is None:
            # M4-p2 — a reconnected chat's OWN registry starts empty; a spawn
            # that survived reconnect (still owned by the stale, orphaned
            # registry) resolves through the process-global, registry-
            # independent index instead (same mechanism the ask_fn
            # already relies on for cross-registry handle lookup).
            from tesseract.brain.spawns import find_handle

            handle = find_handle(inp.handle)
        if handle is None:
            return ToolResult(
                output=f"No spawn with handle={inp.handle!r}.",
                is_error=True,
            )
        try:
            # Shield on BOTH paths so a foreground cancellation
            # (operator Ctrl+C while spawn_await is blocked) does NOT
            # kill the underlying spawn task. The contract is
            # "background spawn survives foreground turn cancel" —
            # without the shield on the no-timeout path, an outer
            # CancelledError would propagate into handle.task and
            # cancel it. Operator can retry spawn_await later.
            if inp.timeout is None:
                result = await asyncio.shield(handle.task)
            else:
                result = await asyncio.wait_for(
                    asyncio.shield(handle.task), timeout=inp.timeout,
                )
        except asyncio.TimeoutError:
            return ToolResult(
                output=(
                    f"spawn_await({inp.handle}) timed out after "
                    f"{inp.timeout}s — task still running, retry "
                    f"spawn_check to confirm."
                ),
                is_error=True,
            )
        except asyncio.CancelledError:
            return ToolResult(
                output=f"spawn_await({inp.handle}) cancelled.",
                is_error=True,
            )
        except Exception as exc:  # noqa: BLE001 — surface as tool error
            return ToolResult(
                output=f"spawn_await({inp.handle}) failed: {exc!r}",
                is_error=True,
            )

        if not isinstance(result, ToolResult):
            return ToolResult(
                output=f"spawn_await unexpected result type {type(result).__name__}",
                is_error=True,
            )
        # Pass the original result through verbatim — output, is_error,
        # metadata. The assistant sees the same payload it would have seen from
        # a foreground call.
        return result
