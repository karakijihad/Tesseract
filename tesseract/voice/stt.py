"""STTEngine — cloud transcription via Gemini 2.5 Flash audio.

The engine takes a buffered audio blob (raw 16-bit/16 kHz mono PCM from
the Mirror frontend, or a WAV-wrapped equivalent), forwards it to the
Gemini STT provider, and yields a single `(text, True)` pair so the WS
handler stays on the same streaming-iterator contract used in earlier
phases.

Cost: the engine debits `cost_ledger.record_voice("stt", provider, ...)`
with the audio duration in seconds. Gemini Flash audio is priced per
audio-token but the ledger normalises to per-audio-hour for visibility
in `cost-tracking.jsonl`.

Budget: when the engine is constructed with a ledger, it preflights the
voice budget. Hitting the per-provider or global daily cap raises
`BudgetExhausted` — there is no STT fallback, so the WS handler
catches it and emits an empty `voice_final` with a `voice_instruction`
note so the operator sees why nothing was transcribed.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass
from typing import AsyncIterator

from tesseract.brain.cost import BudgetExhausted, CostLedger, SttUsage
from tesseract.voice.providers import gemini as gemini_provider
from tesseract.voice.providers import local_whisper

logger = logging.getLogger(__name__)


_FALLBACK_BYTES_PER_SECOND = 32_000  # 16 kHz · 16-bit · mono


def _audio_seconds(audio_bytes: bytes) -> float:
    """Best-effort duration estimate. RIFF/WAVE inputs read the `fmt `
    chunk; everything else (raw PCM, foreign containers) falls back to a
    flat 32 kB/s assumption (16 kHz mono 16-bit). Used for cost-ledger
    debits — exact second-counts matter for billing accuracy."""
    if len(audio_bytes) < 44 or audio_bytes[:4] != b"RIFF" or audio_bytes[8:12] != b"WAVE":
        return len(audio_bytes) / _FALLBACK_BYTES_PER_SECOND
    try:
        sample_rate: int | None = None
        bits_per_sample: int | None = None
        channels: int | None = None
        data_size: int | None = None
        i = 12
        while i + 8 <= len(audio_bytes):
            chunk_id = audio_bytes[i : i + 4]
            (chunk_size,) = struct.unpack("<I", audio_bytes[i + 4 : i + 8])
            payload_off = i + 8
            if chunk_id == b"fmt " and chunk_size >= 16:
                channels = struct.unpack("<H", audio_bytes[payload_off + 2 : payload_off + 4])[0]
                sample_rate = struct.unpack("<I", audio_bytes[payload_off + 4 : payload_off + 8])[0]
                bits_per_sample = struct.unpack("<H", audio_bytes[payload_off + 14 : payload_off + 16])[0]
            elif chunk_id == b"data":
                data_size = chunk_size
                break
            i = payload_off + chunk_size + (chunk_size & 1)
        if (
            sample_rate
            and bits_per_sample
            and channels
            and data_size is not None
        ):
            byte_rate = sample_rate * channels * (bits_per_sample // 8)
            if byte_rate > 0:
                return data_size / byte_rate
    except (struct.error, IndexError):
        pass
    return len(audio_bytes) / _FALLBACK_BYTES_PER_SECOND


@dataclass
class STTEngine:
    """Local-first transcription with Gemini fallback.

    Construct once at boot from `roles.yaml voice.stt`. Local
    faster-whisper is attempted first when configured; Gemini is used
    only when local STT is absent or fails. Pass `cost_ledger=None` to
    skip cloud metering in tests."""

    cloud_config: gemini_provider.GeminiSTTConfig | None
    cost_ledger: CostLedger | None
    local_config: local_whisper.LocalWhisperConfig | None = None
    cloud_provider_key: str = "gemini_flash_audio"
    local_provider_key: str = "local_whisper"
    local_disabled_reason: str = ""
    _pending_fallback_notice: str = ""

    def local_status(self) -> dict:
        status = local_whisper.status(self.local_config)
        status["disabled"] = bool(self.local_disabled_reason)
        status["disabled_reason"] = self.local_disabled_reason
        return status

    def consume_fallback_notice(self) -> str:
        """One-shot notice that local STT just latched off and the next
        utterances will bill Gemini. Returned exactly once per latch so
        the WS handler can surface a toast without re-notifying every
        turn. Cleared on `unload_local()` (operator reset)."""
        notice = self._pending_fallback_notice
        self._pending_fallback_notice = ""
        return notice

    def unload_local(self) -> None:
        local_whisper.unload_models()
        self.local_disabled_reason = ""
        self._pending_fallback_notice = ""

    async def warm_up_local(self) -> None:
        if self.local_config is None:
            return
        try:
            await asyncio.wait_for(
                local_whisper.warm_up(self.local_config),
                timeout=max(self.local_config.timeout_seconds, 60.0),
            )
        except asyncio.TimeoutError:
            # Deliberately does NOT latch local STT off. This lane's fallback
            # is a PAID cloud provider, so latching on a timeout means one
            # slow boot silently starts billing the operator for every
            # utterance until they notice and reset it in Settings — over a
            # preload that ran out of wall-clock, which says nothing about
            # whether the model works. A real load failure still latches,
            # through the handler below.
            logger.warning(
                "local Whisper preload timed out after %.1fs — leaving the "
                "lane available; the first utterance will load it lazily",
                max(self.local_config.timeout_seconds, 60.0),
            )
            return
        except Exception as exc:
            self.local_disabled_reason = str(exc)[:300]
            self._pending_fallback_notice = (
                f"Local STT disabled ({self.local_disabled_reason}); "
                "falling back to paid Gemini cloud STT until reset in Settings."
            )
            raise
        self._pending_fallback_notice = ""

    async def transcribe_stream(
        self,
        audio_bytes: bytes,
        *,
        language: str | None = None,  # accepted for shape parity; ignored
    ) -> AsyncIterator[tuple[str, bool]]:
        """Yield `(text, is_final)` pairs from the configured STT path.

        Always exactly one pair (`text, True`). Local STT is primary;
        cloud STT remains the metered fallback."""
        seconds = _audio_seconds(audio_bytes)

        if self.local_config is not None and not self.local_disabled_reason:
            try:
                text = await asyncio.wait_for(
                    local_whisper.transcribe(audio_bytes, self.local_config),
                    timeout=self.local_config.timeout_seconds,
                )
                text = (text or "").strip()
                if text:
                    yield text, True
                    return
            except asyncio.TimeoutError:
                self.local_disabled_reason = (
                    f"local Whisper timed out after {self.local_config.timeout_seconds:.1f}s"
                )
                self._pending_fallback_notice = (
                    f"Local STT disabled ({self.local_disabled_reason}); "
                    "falling back to paid Gemini cloud STT until reset in Settings."
                )
                logger.exception("%s; falling back to cloud STT", self.local_disabled_reason)
            except Exception as exc:
                self.local_disabled_reason = str(exc)[:300]
                self._pending_fallback_notice = (
                    f"Local STT disabled ({self.local_disabled_reason}); "
                    "falling back to paid Gemini cloud STT until reset in Settings."
                )
                logger.exception(
                    "local STT failed and is disabled until unload/restart; falling back to cloud STT"
                )

        if self.cloud_config is None:
            yield "", True
            return

        if self.cost_ledger is not None:
            # Preflight the voice budget *before* the network call so a
            # capped operator pays nothing and the WS handler can flip a
            # `voice_instruction` toast instead of silently empty.
            self.cost_ledger.voice_check_preflight("stt", self.cloud_provider_key)

        text = await asyncio.wait_for(
            gemini_provider.transcribe(audio_bytes, self.cloud_config),
            timeout=self.cloud_config.timeout_seconds,
        )

        if self.cost_ledger is not None:
            self.cost_ledger.record_voice(
                "stt",
                self.cloud_provider_key,
                SttUsage(seconds=seconds),
            )

        yield text, True


__all__ = ["STTEngine", "BudgetExhausted"]
