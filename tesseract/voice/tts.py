"""TTSEngine — Piper-primary local TTS, Gemini fallback.

Default chain (set in `roles.yaml`): Piper (local, $0) →
Gemini Flash TTS (cloud fallback) → Kokoro. The engine
preflights `cost_ledger.voice_check_preflight("tts", ...)` before any
network call and debits `record_voice("tts", provider, TtsUsage(...))`
after a successful synthesis. Local Piper synth still debits the ledger
at $0 so the rollup table includes it as a zero-row.

Style/character is **preset-driven** across all providers (2026-05-04):

- The chunked-text emitter labels each segment `intent` or `answer`.
- Each provider carries its own per-surface `synthesis_presets`:
  Piper/Kokoro → length_scale / noise_scale / sentence_silence,
  Gemini → a Director's-Notes `style_prompt` (e.g. "Read aloud as
  Jarvis from Iron Man — composed, helpful, lightly wry"). Gemini
  prepends the prompt to the transcript with a colon separator so the
  model interprets it as instruction, not transcript.
- Tone is fixed per-surface — no per-turn variation, no agent-side
  mutation surface. Operator edits `roles.yaml` to retune.

Sentence chunking is *not* applied here; callers (Mirror's WS handler)
chunk before calling so envelopes stream in order.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from tesseract.brain.cost import CostLedger, TtsUsage
from tesseract.voice.providers import (
    gemini_tts as gemini_tts_provider,
    kokoro_tts as kokoro_tts_provider,
    piper_tts as piper_tts_provider,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VoiceParams:
    """TARS-controlled voice settings, read off `VoiceState` per call.

    - `voice_id`: prebuilt voice name (Charon / Algieba / …). Picks
      timbre only on the Gemini path; Piper ignores it (the ONNX file
      IS the voice).
    - `preset`: `intent` / `answer` segment label from the chunked
      text emitter. Drives per-surface preset selection across every
      provider (Piper/Kokoro length_scale, Gemini style_prompt).

    `tone_prompt` is retained as a vestigial field for one release —
    the synth path no longer reads it. `speaking_rate` and
    `pitch_semitones` are deprecated SSML prosody knobs Gemini ignores;
    both are dropped in G3."""

    voice_id: str
    tone_prompt: str = ""            # vestigial — no longer threaded
    preset: str = "answer"
    speaking_rate: float = 1.0       # deprecated — Gemini TTS ignores
    pitch_semitones: float = 0.0     # deprecated — Gemini TTS ignores


@dataclass
class TTSEngine:
    """Provider-selectable TTS synthesis. `provider_key` selects the
    primary lane (Piper / Gemini); the corresponding `*_config` slot
    must be populated. Other slots stay seeded for a future fallback
    engine."""

    cloud_config: gemini_tts_provider.GeminiTTSConfig | None
    cost_ledger: CostLedger | None
    piper_config: piper_tts_provider.PiperTTSConfig | None = None
    kokoro_config: kokoro_tts_provider.KokoroTTSConfig | None = None
    provider_key: str = "gemini_flash_tts"
    cloud_provider_key: str = "gemini_flash_tts"
    piper_provider_key: str = "piper_northern_english_male"
    kokoro_provider_key: str = "charon"
    piper_disabled_reason: str = ""
    kokoro_disabled_reason: str = ""

    def piper_status(self) -> dict:
        """Mirror Settings shape for the LocalModels panel — same envelope
        as `STTEngine.local_status()`."""
        status = piper_tts_provider.status(self.piper_config)
        status["disabled"] = bool(self.piper_disabled_reason)
        status["disabled_reason"] = self.piper_disabled_reason
        status["provider_key"] = self.piper_provider_key
        return status

    def unload_piper(self) -> None:
        """Clear the cached PiperVoice handle and any latched failure
        reason. Operator-driven from Settings."""
        piper_tts_provider.unload_models()
        self.piper_disabled_reason = ""

    def kokoro_status(self) -> dict:
        """Mirror Settings shape for the LocalModels panel — same envelope
        as `piper_status()`."""
        status = kokoro_tts_provider.status(self.kokoro_config)
        status["disabled"] = bool(self.kokoro_disabled_reason)
        status["disabled_reason"] = self.kokoro_disabled_reason
        status["provider_key"] = self.kokoro_provider_key
        return status

    def unload_kokoro(self) -> None:
        """Clear the cached Kokoro+session handles and any latched
        failure reason. Operator-driven from Settings; called from
        Mirror shutdown to release the GPU arena cleanly."""
        kokoro_tts_provider.unload_models()
        self.kokoro_disabled_reason = ""

    async def warm_up_kokoro(self) -> None:
        """Eager-load the Kokoro model + blend on boot so the first
        sentence doesn't pay the ONNX init latency. On failure the engine
        latches a `disabled_reason` and the chain falls through to the
        next provider — the next reload through Settings clears the latch."""
        if self.kokoro_config is None:
            return
        timeout = float(self.kokoro_config.timeout_seconds)
        try:
            await asyncio.wait_for(
                kokoro_tts_provider.warm_up(self.kokoro_config),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            self.kokoro_disabled_reason = f"Kokoro preload timed out after {timeout:.1f}s"
            raise
        except Exception as exc:
            self.kokoro_disabled_reason = str(exc)[:300]
            raise

    async def warm_up_piper(self) -> None:
        """Eager-load the Piper voice on boot so the first sentence
        doesn't pay the ONNX init latency. On failure the engine latches
        a `disabled_reason` and the cloud fallback (Gemini) takes over —
        the next reload through Settings clears the latch."""
        if self.piper_config is None:
            return
        try:
            await asyncio.wait_for(
                piper_tts_provider.warm_up(self.piper_config),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            self.piper_disabled_reason = "Piper preload timed out after 60.0s"
            raise
        except Exception as exc:
            self.piper_disabled_reason = str(exc)[:300]
            raise

    async def synthesize(
        self,
        text: str,
        params: VoiceParams,
    ) -> tuple[bytes, str]:
        """Render `text` to audio. Returns `(audio_bytes, provider_key)`.
        Empty / whitespace text → empty bytes, no ledger debit."""
        if not text.strip():
            return b"", ""

        # Char count covers transcript only — style prompts come from
        # config and add a constant per call we don't track per-debit.
        # Piper/Kokoro bill $0; the field is recorded for the rollup.
        char_count = len(text)

        provider_key = self.provider_key
        if self.cost_ledger is not None:
            self.cost_ledger.voice_check_preflight("tts", provider_key)

        if params.speaking_rate != 1.0 or params.pitch_semitones != 0.0:
            logger.warning(
                "TTSEngine: speaking_rate/pitch_semitones are deprecated — "
                "Gemini TTS ignores SSML prosody. Edit "
                "`voice.tts.settings.<ref>.synthesis_presets` in roles.yaml "
                "to shape pacing/character per surface."
            )

        if provider_key == self.kokoro_provider_key:
            if self.kokoro_config is None:
                raise RuntimeError("Kokoro TTS selected but no config was loaded")
            if not self.kokoro_disabled_reason:
                try:
                    audio = await kokoro_tts_provider.synthesize(
                        text,
                        self.kokoro_config,
                        preset=params.preset,
                    )
                    if self.cost_ledger is not None:
                        self.cost_ledger.record_voice(
                            "tts",
                            provider_key,
                            TtsUsage(char_count=char_count),
                        )
                    return audio, provider_key
                except Exception as exc:
                    self.kokoro_disabled_reason = str(exc)[:300]
                    logger.exception(
                        "local Kokoro TTS failed and is disabled until unload/restart; "
                        "falling back to next provider"
                    )
            # Kokoro latched off — fall through to Piper (if configured) then cloud.
            if self.piper_config is not None and not self.piper_disabled_reason:
                try:
                    audio = await piper_tts_provider.synthesize(
                        text,
                        self.piper_config,
                        preset=params.preset,
                    )
                    if self.cost_ledger is not None:
                        self.cost_ledger.record_voice(
                            "tts",
                            self.piper_provider_key,
                            TtsUsage(char_count=char_count),
                        )
                    return audio, self.piper_provider_key
                except Exception as exc:
                    self.piper_disabled_reason = str(exc)[:300]
                    logger.exception(
                        "Piper fallback failed after Kokoro; falling through to cloud"
                    )
            if self.cloud_config is None:
                raise RuntimeError("Kokoro TTS disabled and no Gemini fallback configured")
            if self.cost_ledger is not None:
                self.cost_ledger.voice_check_preflight("tts", self.cloud_provider_key)
            audio = await gemini_tts_provider.synthesize(
                text,
                self.cloud_config,
                voice_id=params.voice_id,
                preset=params.preset,
            )
            if self.cost_ledger is not None:
                self.cost_ledger.record_voice(
                    "tts",
                    self.cloud_provider_key,
                    TtsUsage(char_count=char_count),
                )
            return audio, self.cloud_provider_key
        elif provider_key == self.piper_provider_key:
            if self.piper_config is None:
                raise RuntimeError("Piper TTS selected but no config was loaded")
            if not self.piper_disabled_reason:
                try:
                    audio = await piper_tts_provider.synthesize(
                        text,
                        self.piper_config,
                        preset=params.preset,
                    )
                    if self.cost_ledger is not None:
                        self.cost_ledger.record_voice(
                            "tts",
                            provider_key,
                            TtsUsage(char_count=char_count),
                        )
                    return audio, provider_key
                except Exception as exc:
                    self.piper_disabled_reason = str(exc)[:300]
                    logger.exception(
                        "local Piper TTS failed and is disabled until unload/restart; "
                        "falling back to Gemini cloud TTS"
                    )
            # Piper latched off — fall through to the cloud fallback.
            if self.cloud_config is None:
                raise RuntimeError("Piper TTS disabled and no Gemini fallback configured")
            if self.cost_ledger is not None:
                self.cost_ledger.voice_check_preflight("tts", self.cloud_provider_key)
            audio = await gemini_tts_provider.synthesize(
                text,
                self.cloud_config,
                voice_id=params.voice_id,
                preset=params.preset,
            )
            if self.cost_ledger is not None:
                self.cost_ledger.record_voice(
                    "tts",
                    self.cloud_provider_key,
                    TtsUsage(char_count=char_count),
                )
            return audio, self.cloud_provider_key
        else:
            if self.cloud_config is None:
                raise RuntimeError("Gemini TTS selected but no config was loaded")
            audio = await gemini_tts_provider.synthesize(
                text,
                self.cloud_config,
                voice_id=params.voice_id,
                preset=params.preset,
            )
        if self.cost_ledger is not None:
            self.cost_ledger.record_voice(
                "tts",
                provider_key,
                TtsUsage(char_count=char_count),
            )
        return audio, provider_key
