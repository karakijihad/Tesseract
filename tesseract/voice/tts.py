"""TTSEngine — synthesis over the ordered lane chain named in config.

The chain comes from `roles.yaml::voice.tts` (primary + fallbacks); the
engine holds one config slot per adapter and tries them in that order.
Two adapters ship by default: a local one first, so a fresh install
speaks with no key and no bill, and a cloud one behind it so a machine
that never downloaded the local model still has a voice. Adding another
is a provider module plus a slot here — the chain shape doesn't change.

When every configured lane is down the engine raises and the caller
degrades the reply to text.

Style/character is **preset-driven**, per provider:

- The chunked-text emitter labels each segment `intent` or `answer`.
- Each catalog entry carries its own per-surface `synthesis_presets`,
  in whatever knobs its provider exposes.
- Tone is fixed per-surface — no per-turn variation, no agent-side
  mutation surface. The operator retunes by editing the catalog.

A lane that raises latches a `disabled_reason` and is skipped until it
is unloaded from Settings; the sentence falls to the next lane in the
chain. Local synthesis still debits the ledger at $0 so the spend rollup
lists it as a zero-row.

Sentence chunking is *not* applied here; callers (Mirror's WS handler)
chunk before calling so envelopes stream in order.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import wave
from dataclasses import dataclass

from tesseract.brain.cost import CostLedger, TtsUsage
from tesseract.voice.providers import (
    gemini_tts as gemini_tts_provider,
    kokoro_tts as kokoro_tts_provider,
)

logger = logging.getLogger(__name__)

_DEFAULT_PRESET = "answer"


def _audio_seconds(audio: bytes) -> float:
    """Duration of a WAV payload, or 0.0 if it cannot be read.

    Every lane hands back WAV, so the duration is in the envelope and
    needs no per-lane sample-rate plumbing. It is only *billed* on a lane
    priced per audio-hour; measuring it on the free local lanes costs a
    header read and keeps the ledger's zero-rows honest about what they
    produced.

    Never raises: a lane that spoke must not fail because its output
    could not be measured. An unreadable envelope bills as zero, which is
    the direction that cannot overcharge.
    """
    if not audio:
        return 0.0
    with contextlib.suppress(Exception):
        with wave.open(io.BytesIO(audio), "rb") as w:
            rate = w.getframerate()
            if rate > 0:
                return w.getnframes() / float(rate)
    return 0.0


class NoTTSLaneAvailable(RuntimeError):
    """Every configured lane is unconfigured or latched off. The caller
    degrades to text rather than retrying — a lane only clears on an
    operator unload."""


@dataclass
class TTSEngine:
    """TTS with an ordered fallback chain.

    `provider_key` is the primary lane's catalog id; the remaining
    configured lanes are tried behind it. A lane is present only when
    `_build_voice_runtime` found its entry in the chain, so a `*_config`
    of `None` means "not in the operator's chain", not "failed"."""

    cost_ledger: CostLedger | None
    kokoro_config: kokoro_tts_provider.KokoroTTSConfig | None = None
    gemini_config: gemini_tts_provider.GeminiTTSConfig | None = None
    provider_key: str = ""
    kokoro_provider_key: str = ""
    gemini_provider_key: str = ""
    kokoro_disabled_reason: str = ""
    gemini_disabled_reason: str = ""

    def kokoro_status(self) -> dict:
        """Mirror Settings shape for the LocalModels panel — same envelope
        as `STTEngine.local_status()`."""
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

    def gemini_status(self) -> dict:
        """Mirror Settings shape — same envelope as `kokoro_status()`.

        There is no `unload_gemini()` counterpart: unload exists to free a
        loaded model and clear a latch, and this lane holds no model. Its
        latch clears on the next `_build_voice_runtime`, which is what a
        config edit already triggers.
        """
        status = gemini_tts_provider.status(self.gemini_config)
        status["disabled"] = bool(self.gemini_disabled_reason)
        status["disabled_reason"] = self.gemini_disabled_reason
        status["provider_key"] = self.gemini_provider_key
        return status

    async def warm_up_kokoro(self) -> None:
        """Eager-load the Kokoro model + blend on boot so the first
        sentence doesn't pay the ONNX init latency. On failure the engine
        latches a `disabled_reason` and the chain falls through to the
        next lane — the next reload through Settings clears the latch."""
        if self.kokoro_config is None:
            return
        timeout = float(self.kokoro_config.timeout_seconds)
        try:
            await asyncio.wait_for(
                kokoro_tts_provider.warm_up(self.kokoro_config),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # Deliberately does NOT latch the lane off. A preload that ran out
            # of wall-clock says nothing about whether the model works — on a
            # busy boot the session had already loaded on CUDA and it was the
            # warm SYNTHESIS that got cancelled. Latching there demoted the
            # operator to the fallback voice for the whole session over a
            # timing accident, and the only clue was a traceback in the log.
            # A real failure still latches, via the handler below.
            logger.warning(
                "Kokoro preload timed out after %.1fs — leaving the lane "
                "available; the first sentence will load it lazily",
                timeout,
            )
            return
        except Exception as exc:
            self.kokoro_disabled_reason = str(exc)[:300]
            raise

    def _lane_order(self) -> list[str]:
        """Primary first, then every other configured lane. Dedup keeps a
        lane from being tried twice when it *is* the primary."""
        order: list[str] = []
        for key in (
            self.provider_key,
            self.kokoro_provider_key,
            self.gemini_provider_key,
        ):
            if key and key not in order:
                order.append(key)
        return order

    def _lane_ready(self, lane: str) -> bool:
        if lane == self.kokoro_provider_key:
            return self.kokoro_config is not None and not self.kokoro_disabled_reason
        if lane == self.gemini_provider_key:
            return self.gemini_config is not None and not self.gemini_disabled_reason
        return False

    async def _synthesize_on(self, lane: str, text: str, preset: str) -> bytes:
        if lane == self.kokoro_provider_key:
            return await kokoro_tts_provider.synthesize(
                text, self.kokoro_config, preset=preset,
            )
        return await gemini_tts_provider.synthesize(
            text, self.gemini_config, preset=preset,
        )

    def _latch_disabled(self, lane: str, exc: Exception) -> None:
        reason = str(exc)[:300]
        if lane == self.kokoro_provider_key:
            self.kokoro_disabled_reason = reason
        elif lane == self.gemini_provider_key:
            self.gemini_disabled_reason = reason

    async def synthesize(
        self,
        text: str,
        *,
        preset: str = _DEFAULT_PRESET,
    ) -> tuple[bytes, str]:
        """Render `text` to audio. Returns `(audio_bytes, provider_key)`.

        Walks the lane chain until one succeeds. Empty / whitespace text
        → empty bytes, no ledger debit. `BudgetExhausted` from a lane's
        preflight propagates rather than falling through: a cap is the
        operator's decision, not a fault to route around.

        Raises `NoTTSLaneAvailable` when the chain is exhausted."""
        if not text.strip():
            return b"", ""

        # Char count covers the transcript only. Local lanes bill $0; the
        # field is recorded so the rollup can show them as zero-rows.
        char_count = len(text)

        for lane in self._lane_order():
            if not self._lane_ready(lane):
                continue
            if self.cost_ledger is not None:
                self.cost_ledger.voice_check_preflight("tts", lane)
            try:
                audio = await self._synthesize_on(lane, text, preset)
            except Exception as exc:
                self._latch_disabled(lane, exc)
                logger.exception(
                    "local TTS lane %s failed and is disabled until unload/restart; "
                    "trying the next lane",
                    lane,
                )
                continue
            if self.cost_ledger is not None:
                self.cost_ledger.record_voice(
                    "tts",
                    lane,
                    TtsUsage(char_count=char_count, seconds=_audio_seconds(audio)),
                )
            return audio, lane

        raise NoTTSLaneAvailable(
            "no TTS lane available — configured lanes: "
            f"{self._lane_order() or ['(none)']}; "
            f"kokoro={self.kokoro_disabled_reason or 'ok'}, "
            f"gemini={self.gemini_disabled_reason or 'ok'}"
        )
