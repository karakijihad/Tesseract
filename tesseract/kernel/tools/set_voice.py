"""set_voice tool — operator/agent-controlled voice settings.

One real knob after the 2026-05-04 simplification:

- `voice_id`  — prebuilt Gemini voice (Charon / Algieba / Achird /
  Iapetus / Kore / etc.). Picks **timbre** only.

Style/character is **config-only** now — `roles.yaml` carries per-surface
`synthesis_presets` (intent / answer) for each TTS provider. The agent
cannot mutate tone mid-session; the operator can change it via
`POST /api/settings/voice` or by editing `roles.yaml` (config_watcher
picks it up). Rationale: per-turn tone drift broke the "always sounds
like the same character" contract.

`speaking_rate` and `pitch_semitones` are **deprecated** — Gemini TTS
ignores SSML prosody knobs. They are kept on the dataclass and tool
input for one release with deprecation warnings, dropped in G3.

AUTO permission: tweaking voice has no destructive blast radius.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)


@dataclass
class VoiceState:
    """In-memory voice settings for the current process.

    Defaults seeded from `roles.yaml voice.default_voice_id` and
    `voice.default_tone_prompt`. `set_voice` is idempotent.
    """

    voice_id: str
    tone_prompt: str = ""
    # Deprecated — Gemini TTS doesn't honour SSML prosody. Kept on the
    # dataclass so any older caller still constructs without errors.
    speaking_rate: float = 1.0
    pitch_semitones: float = 0.0

    def set(
        self,
        voice_id: str,
        tone_prompt: str,
        *,
        speaking_rate: float = 1.0,
        pitch_semitones: float = 0.0,
    ) -> None:
        self.voice_id = voice_id
        self.tone_prompt = tone_prompt
        self.speaking_rate = speaking_rate
        self.pitch_semitones = pitch_semitones


class SetVoiceInput(BaseModel):
    voice_id: str = Field(
        description="Prebuilt Gemini voice name (e.g. 'Charon', 'Algieba'). "
        "Picks timbre only. Style/character is config-only "
        "(roles.yaml::synthesis_presets) — not adjustable from this tool.",
    )
    speaking_rate: float = Field(
        default=1.0,
        description="Deprecated — Gemini TTS ignores SSML prosody. "
        "Field will be removed in G3.",
    )
    pitch_semitones: float = Field(
        default=0.0,
        description="Deprecated — Gemini TTS ignores SSML prosody. "
        "Field will be removed in G3.",
    )


class SetVoiceTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"
    def __init__(self, voice_state: VoiceState) -> None:
        self._voice_state = voice_state

    @property
    def name(self) -> str:
        return "set_voice"

    @property
    def description(self) -> str:
        return (
            "Set your spoken voice timbre. voice_id picks the prebuilt voice "
            "(Charon / Algieba / Achird / Iapetus / Kore from the Gemini "
            "catalogue). Persists for the session lifetime. Style/character "
            "is config-only — not adjustable from this tool."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return SetVoiceInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return False

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: SetVoiceInput = tool_input  # type: ignore[assignment]
        deprecated_used: list[str] = []
        if inp.speaking_rate != 1.0:
            deprecated_used.append(f"speaking_rate={inp.speaking_rate:.2f}")
        if inp.pitch_semitones != 0.0:
            deprecated_used.append(f"pitch_semitones={inp.pitch_semitones:+.2f}")
        if deprecated_used:
            logger.warning(
                "set_voice received deprecated SSML prosody fields (%s) — "
                "Gemini TTS ignores them. Pacing/character is config-only "
                "now: edit `synthesis_presets` in roles.yaml.",
                ", ".join(deprecated_used),
            )
        self._voice_state.set(
            voice_id=inp.voice_id,
            tone_prompt=self._voice_state.tone_prompt,
            speaking_rate=inp.speaking_rate,
            pitch_semitones=inp.pitch_semitones,
        )
        suffix = " (deprecated rate/pitch ignored)" if deprecated_used else ""
        return ToolResult(
            output=f"voice set: id={self._voice_state.voice_id}{suffix}",
            metadata={"voice_id": self._voice_state.voice_id},
        )
