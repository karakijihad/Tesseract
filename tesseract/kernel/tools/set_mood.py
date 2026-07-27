"""set_mood tool — entity-driven affect control.

Two scalars:
- intensity (0..1): affective energy (couples to particle motion intensity)
- valence (-1..+1): cool→warm tone (couples to hue shift)

Out-of-range values are clamped (not rejected) per phase-2-entity.md:221-224:
a misbehaving model should degrade to the nearest valid mood, not fail the
tool call and break the turn.

2026-05-01: voice is decoupled from mood. `set_mood` drives the orb only.
Mood auto-decays to neutral at the end of every turn (`ws._run_turn`), so
TARS sets the mood for the current turn — no carryover between prompts.

Low-risk (no file I/O, no network, no state outside session MoodState).
No ask.rules entry → permission engine auto-approves via PASSTHROUGH.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.mood_state import MoodState

logger = logging.getLogger(__name__)


class SetMoodInput(BaseModel):
    intensity: float = Field(
        description="Affective energy, 0..1. Higher = more motion, faster pulse. Values outside [0,1] are clamped.",
    )
    valence: float = Field(
        description="Tone, -1..+1. Negative = cool, positive = warm (hue shift). Values outside [-1,1] are clamped.",
    )


class SetMoodTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"
    def __init__(self, mood_state: MoodState) -> None:
        self._mood_state = mood_state

    @property
    def name(self) -> str:
        return "set_mood"

    @property
    def description(self) -> str:
        return (
            "Set your expressive mood for this turn. Two knobs: intensity "
            "(0..1 energy) and valence (-1..+1 cool→warm). Drives the orb's "
            "color and motion. Auto-resets to neutral at the end of every "
            "turn — call again next turn if the mood should persist."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return SetMoodInput

    def is_concurrency_safe(self) -> bool:
        # MoodState lives on the tool instance in the shared ToolRegistry —
        # two concurrent set() calls last-write-wins on intensity/valence.
        # WP-2 synthetic registry will omit this tool entirely.
        return False

    def is_read_only(self) -> bool:
        return False

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: SetMoodInput = tool_input  # type: ignore[assignment]
        clamped = []
        if not (0.0 <= inp.intensity <= 1.0):
            clamped.append(f"intensity={inp.intensity:.2f}")
        if not (-1.0 <= inp.valence <= 1.0):
            clamped.append(f"valence={inp.valence:+.2f}")
        if clamped:
            logger.warning("set_mood clamped out-of-range input: %s", ", ".join(clamped))
        self._mood_state.set(inp.intensity, inp.valence)
        suffix = " (clamped)" if clamped else ""
        metadata: dict[str, Any] = {
            "intensity": self._mood_state.intensity,
            "valence": self._mood_state.valence,
        }
        return ToolResult(
            output=(
                f"mood set: intensity={self._mood_state.intensity:.2f} "
                f"valence={self._mood_state.valence:+.2f}{suffix}"
            ),
            metadata=metadata,
        )
