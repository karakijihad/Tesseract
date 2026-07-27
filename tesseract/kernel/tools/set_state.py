"""set_state — TARS-driven orb state.

Lets TARS choose discrete affective states the Mirror orb renders with
visually distinct presets (`mirror/src/lib/entity/states.ts`). Pairs
with `set_mood`: mood is continuous shading, state is a discrete mode.

Allowed values: `happy`, `deep_focus`, `dreaming`, `idle`. Reactive
states (`thinking`, `speaking`, `listening`, `error`, `spawning`) stay
loop-driven — they fire often enough that a TARS-set value would be
overwritten within milliseconds, so this tool refuses them rather
than letting TARS create noise.

State is sticky frontend-side until either TARS calls again or the
loop fires its own setState (e.g. `loop_start` → `thinking`). Boot
default: `idle`.

Low-risk (no I/O, no network, no state outside the in-process holder).
AUTO permission.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)


ALLOWED_STATES = frozenset({"happy", "deep_focus", "dreaming", "idle"})


@dataclass
class EntityAffect:
    """In-memory holder for the operator-visible orb state. One per Mirror
    process; Mirror's WS handler reads this after `set_state` TOOL_RESULT
    fires the `entity_state_set` envelope."""

    state: str = "idle"

    def set(self, state: str) -> None:
        self.state = state


class SetStateInput(BaseModel):
    state: str = Field(
        description=(
            "Discrete orb state. Allowed: 'happy', 'deep_focus', 'dreaming', 'idle'. "
            "Loop-driven reactive states (thinking/speaking/listening/error/spawning) "
            "are not settable here — they're driven by what's actually happening."
        ),
    )


class SetStateTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"
    def __init__(self, affect: EntityAffect) -> None:
        self._affect = affect

    @property
    def name(self) -> str:
        return "set_state"

    @property
    def description(self) -> str:
        return (
            "Set the orb's discrete state. Use 'happy' after a real "
            "breakthrough, 'deep_focus' for long debugging stretches, "
            "'dreaming' when reflecting between turns, 'idle' to "
            "settle back. Sticky until you call again or the loop "
            "transitions on its own. Reactive states (thinking, "
            "speaking) are loop-driven; this tool can't set them. "
            "Use sparingly — mood (set_mood) does most expressive work."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return SetStateInput

    def is_concurrency_safe(self) -> bool:
        # EntityAffect lives on the tool instance in the shared ToolRegistry —
        # two concurrent set() calls last-write-wins on the entity state.
        # WP-2 synthetic registry will omit this tool entirely.
        return False

    def is_read_only(self) -> bool:
        return False

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: SetStateInput = tool_input  # type: ignore[assignment]
        state = inp.state.strip().lower()
        if state not in ALLOWED_STATES:
            allowed = ", ".join(sorted(ALLOWED_STATES))
            return ToolResult(
                output=(
                    f"set_state: unknown or non-settable state {inp.state!r}; "
                    f"allowed: {allowed}"
                ),
                is_error=True,
            )
        self._affect.set(state)
        return ToolResult(
            output=f"state set: {state}",
            metadata={"state": state},
        )
