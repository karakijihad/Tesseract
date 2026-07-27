"""Voice + mood wiring sanity checks.

2026-05-01: voice was decoupled from MoodState. `set_mood` drives the orb
only — no `tone_modifier`, no prosody table. The synthesis path reads
`voice_state.tone_prompt` directly. This file pins:

- `set_mood` writes intensity/valence to MoodState (and clamps).
- The voice block has the chain shape after the providers/roles split.
- Cost-delta envelopes carry the right `kind` for voice vs chat events.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import yaml

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.set_mood import SetMoodInput, SetMoodTool
from tesseract.mirror.server.envelope import make_cost_delta
from tesseract.orchestrator.mood_state import MoodState


async def test_set_mood_writes_to_mood_state():
    mood = MoodState()
    tool = SetMoodTool(mood_state=mood)
    await tool.run(SetMoodInput(intensity=0.9, valence=0.9), ToolContext())
    assert mood.intensity == 0.9
    assert mood.valence == 0.9


async def test_set_mood_clamps_out_of_range():
    mood = MoodState()
    tool = SetMoodTool(mood_state=mood)
    await tool.run(SetMoodInput(intensity=1.5, valence=-2.0), ToolContext())
    assert mood.intensity == 1.0
    assert mood.valence == -1.0


async def test_set_mood_metadata_does_not_carry_tone_modifier():
    """Voice is decoupled from mood — set_mood metadata is intensity/valence
    only. A `tone_modifier` key would resurrect the dead prosody coupling."""
    mood = MoodState()
    tool = SetMoodTool(mood_state=mood)
    result = await tool.run(SetMoodInput(intensity=0.5, valence=0.0), ToolContext())
    assert result.metadata is not None
    assert "tone_modifier" not in result.metadata
    assert result.metadata["intensity"] == 0.5
    assert result.metadata["valence"] == 0.0


def test_mood_state_reset_returns_to_neutral():
    mood = MoodState()
    mood.set(0.9, -0.7)
    assert mood.intensity == 0.9
    assert mood.valence == -0.7
    mood.reset()
    assert mood.intensity == 0.5
    assert mood.valence == 0.0


def test_make_cost_delta_kind_is_voice_for_voice_events():
    event = MagicMock()
    event.role = "voice_tts"
    event.model = "gemini_flash_tts"
    event.cost_usd = 0.001
    event.daily_total_usd = 0.001
    event.role_total_usd = 0.001
    state = MagicMock()
    state.spent_usd = 0.001
    state.warning_usd = 1.0
    state.cap_usd = 5.0
    state.role_spent_usd = 0.001
    state.role_cap_usd = 0.20
    state.warning = False
    state.blocked = False
    env = make_cost_delta("sess-1", event, state)
    assert env["data"]["kind"] == "voice_tts"


def test_make_cost_delta_kind_is_chat_for_chat_events():
    event = MagicMock()
    event.role = "chat_brain"
    event.model = "gpt-5.4-nano"
    event.cost_usd = 0.001
    event.daily_total_usd = 0.001
    event.role_total_usd = 0.001
    state = MagicMock()
    state.spent_usd = 0.001
    state.warning_usd = 1.0
    state.cap_usd = 3.0
    state.role_spent_usd = 0.001
    state.role_cap_usd = 3.0
    state.warning = False
    state.blocked = False
    env = make_cost_delta("sess-1", event, state)
    assert env["data"]["kind"] == "chat"


def test_voice_config_has_required_lanes():
    """Smoke test: ensure the shipping providers.yaml + roles.yaml carry the
    chain-shaped STT + TTS blocks. After the 2026-05-01 refactor, voice
    runtime config follows the same primary+fallbacks shape as chat-brain
    roles — each lane has `mode` + `chain[]` of `{ref, adapter, provider,
    model, ...}` entries."""
    from tesseract.brain.boot import load_voice_config
    from tesseract.brain.cost.ledger import CostLedger
    from tesseract.config.loader import load_config

    voice = load_voice_config()
    assert voice, "load_voice_config returned empty — voice block missing"
    assert "stt" in voice and "tts" in voice

    # STT: local-whisper primary + Gemini Flash audio fallback.
    stt = voice["stt"]
    assert stt.get("mode") == "active"
    stt_chain = stt.get("chain") or []
    assert stt_chain, "stt.chain empty"
    stt_adapters = [e.get("adapter") for e in stt_chain]
    assert "local_whisper" in stt_adapters
    local_stt = next(e for e in stt_chain if e.get("adapter") == "local_whisper")
    assert local_stt.get("model")
    assert local_stt.get("device")
    assert local_stt.get("compute_type")
    assert "timeout_seconds" in local_stt
    cloud_stt = next((e for e in stt_chain if e.get("adapter") == "gemini"), None)
    if cloud_stt is not None:
        assert cloud_stt.get("api_key_env") == "GOOGLE_API_KEY"

    # TTS: Gemini default + ElevenLabs fallback.
    tts = voice["tts"]
    assert tts.get("mode") == "active"
    tts_chain = tts.get("chain") or []
    assert tts_chain, "tts.chain empty"
    cloud_tts = next((e for e in tts_chain if e.get("adapter") == "gemini"), None)
    assert cloud_tts is not None, "expected Gemini TTS entry in tts.chain"
    assert cloud_tts.get("api_key_env") == "GOOGLE_API_KEY"
    assert cloud_tts.get("model") == "gemini-2.5-flash-preview-tts"
    el = next((e for e in tts_chain if e.get("adapter") == "elevenlabs"), None)
    if el is not None:
        assert el.get("api_key_env")
        assert el.get("model") or el.get("model_id")

    assert voice.get("default_voice_id") == "Charon"
    assert voice.get("default_tone_prompt"), "voice default_tone_prompt required"
    # Voice was decoupled from MoodState 2026-05-01 — prosody block is gone.
    assert "prosody" not in voice

    # Pricing now lives in providers.yaml — surfaced via the cost ledger.
    bundle = load_config()
    ledger = CostLedger.from_bundle(bundle)
    assert "gemini_flash_tts" in ledger.voice_tts_pricing
    assert "gemini_flash_audio" in ledger.voice_stt_pricing
    assert "local_whisper" in ledger.voice_stt_pricing
    # No legacy provider keys remain.
    assert "google_neural2" not in ledger.voice_tts_pricing
    assert "piper" not in ledger.voice_tts_pricing


def test_permissions_yaml_has_set_voice_auto():
    from tesseract.brain.boot import PERMISSIONS_YAML
    raw = yaml.safe_load(PERMISSIONS_YAML.read_text(encoding="utf-8"))
    assert raw["tools"]["set_voice"] == "auto"


def test_set_voice_registered_in_boot():
    from tesseract.brain.boot import build_tool_registry
    registry, _mood, voice_state, _bundle, _alarms = build_tool_registry()
    assert "set_voice" in registry.tools
    assert voice_state is not None
    # voice_state seeded from `voice.default_voice_id` + `default_tone_prompt`.
    assert voice_state.voice_id == "Charon"
    assert voice_state.tone_prompt  # non-empty seed
