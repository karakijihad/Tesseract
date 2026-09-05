"""TTSEngine — synthesis over the ordered lane chain named in config.

The chain comes from `roles.yaml::voice.tts` (primary + fallbacks); the
engine holds one `TTSLane` per adapter in the chain and tries them in that
order. Two adapters ship by default: a local one first, so a fresh install
speaks with no key and no bill, and a cloud one behind it so a machine
that never downloaded the local model still has a voice. Adding another
is a provider module, a name in `LANE_PROVIDERS`, and a lane in the dict
the builder passes — the engine itself does not change.

When every configured lane is down the engine raises and the caller
degrades the reply to text.

Style/character is **preset-driven**, per provider:

- The chunked-text emitter labels each segment `intent` or `answer`.
- Each catalog entry carries its own per-surface `synthesis_presets`,
  in whatever knobs its provider exposes.
- Tone is fixed per-surface — no per-turn variation, no agent-side
  mutation surface. The operator retunes by editing the catalog.

A lane that raises does NOT go down on the first failure, and does not
stay down forever. It takes `MAX_CONSECUTIVE_FAILURES` in a row to latch
a `disabled_reason`, a success anywhere in between clears the count, and
the latch expires after the lane's `lane_cooldown_seconds` so the next
sentence tries it again. Without both halves, one bad minute costs the
voice for the life of the process and only the operator noticing the
silence brings it back. Either way the sentence in hand falls to the next
lane in the chain, and `lane_down_hook` fires when a lane latches so
something can tell a person. Local synthesis still debits the ledger at $0
so the spend rollup lists it as a zero-row.

Sentence chunking is *not* applied here; callers (Mirror's WS handler)
chunk before calling so envelopes stream in order.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import time
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from tesseract.brain.cost import CostLedger, TtsUsage
from tesseract.voice.providers import (
    gemini_tts as gemini_tts_provider,
    kokoro_tts as kokoro_tts_provider,
)

logger = logging.getLogger(__name__)

_DEFAULT_PRESET = "answer"

#: Adapter name → the provider module that implements it. Every module here
#: exposes the same two callables, `status(config)` and
#: `synthesize(text, config, preset=...)`, which is what lets the engine hold
#: lanes in a dict instead of a branch per lane.
LANE_PROVIDERS = {
    "kokoro": kokoro_tts_provider,
    "gemini": gemini_tts_provider,
}

#: Consecutive failures on one lane before it latches off. The house circuit
#: breaker: a `503` on the way to a working provider is not a broken lane, and
#: treating it as one is how a whole day went by with nothing speaking.
MAX_CONSECUTIVE_FAILURES = 3


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
    degrades to text rather than retrying — a latched lane clears on its own
    cooldown or on an operator unload, not inside one call."""


@dataclass
class TTSLane:
    """One lane of the chain: which adapter, its config, and why it is off.

    `adapter` names an entry in `LANE_PROVIDERS`; the engine calls that
    module and never learns which one it got. A `config` of `None` means the
    operator's chain named the lane but the builder could not construct it,
    which the engine skips the same way it skips a latched lane.
    """

    adapter: str
    config: Any = None
    disabled_reason: str = ""

    def __post_init__(self) -> None:
        if self.adapter not in LANE_PROVIDERS:
            raise ValueError(
                f"unknown TTS adapter {self.adapter!r}; "
                f"known: {sorted(LANE_PROVIDERS)}"
            )

    @property
    def provider(self) -> Any:
        return LANE_PROVIDERS[self.adapter]

    @property
    def ready(self) -> bool:
        return self.config is not None and not self.disabled_reason


@dataclass
class TTSEngine:
    """TTS with an ordered fallback chain.

    `provider_key` is the primary lane's catalog id; the remaining
    configured lanes are tried behind it. `lanes` is keyed by catalog id,
    so everything about a lane — its adapter, its config, whether it is
    latched off, how long its cooldown runs — is reached by that one key."""

    cost_ledger: CostLedger | None
    lanes: dict[str, TTSLane] = field(default_factory=dict)
    provider_key: str = ""
    #: How long a latched lane stays down before it is tried again, keyed by
    #: provider key. Comes off each lane's `settings:` block in `roles.yaml`,
    #: because how long to wait out a local model's failure and a cloud
    #: provider's are not the same number. A lane missing from here never
    #: retries on its own, which is the pre-cooldown behaviour.
    lane_cooldown_seconds: dict[str, float] = field(default_factory=dict)
    #: Awaited once when a lane latches, with `(provider_key, reason)`. The
    #: engine has no route to a person; whoever builds it does.
    lane_down_hook: Callable[[str, str], Awaitable[None]] | None = None
    _lane_failures: dict[str, int] = field(default_factory=dict)
    _lane_retry_at: dict[str, float] = field(default_factory=dict)
    #: The last thing each lane raised, latched or not. Kept so an exhausted
    #: chain can still say WHY: with a breaker in front of the latch, every
    #: lane can fail a sentence while none of them is off yet, and "no lane
    #: available, all lanes ok" is not a sentence anyone can act on.
    _lane_last_error: dict[str, str] = field(default_factory=dict)

    # ---- lane lookup ----

    def key_for_adapter(self, adapter: str) -> str:
        """The catalog id of the lane running `adapter`, or `""`.

        The two named surfaces below ask for a lane by what it IS — the local
        model that can be unloaded — while everything else addresses lanes by
        the catalog id the operator chose in Settings.
        """
        for key, lane in self.lanes.items():
            if lane.adapter == adapter:
                return key
        return ""

    def adapter_status(self, adapter: str) -> dict:
        """Mirror Settings shape for the LocalModels panel — same envelope
        as `STTEngine.local_status()`.

        Asked of the ADAPTER rather than of a lane, because the panel renders
        whether or not the operator's chain names one: a cloud-only chain has
        no local lane and the panel still has to say so, in the same envelope
        with the same keys. The provider answers that for a `None` config.
        """
        key = self.key_for_adapter(adapter)
        lane = self.lanes.get(key)
        status = LANE_PROVIDERS[adapter].status(lane.config if lane else None)
        status["disabled"] = bool(lane.disabled_reason) if lane else False
        status["disabled_reason"] = lane.disabled_reason if lane else ""
        status["provider_key"] = key
        return status

    def disabled_reason(self, key: str) -> str:
        lane = self.lanes.get(key)
        return lane.disabled_reason if lane else ""

    # ---- the two surfaces that name a lane ----

    def kokoro_status(self) -> dict:
        return self.adapter_status("kokoro")

    def unload_kokoro(self) -> None:
        """Clear the cached Kokoro+session handles and any latched
        failure reason. Operator-driven from Settings; called from
        Mirror shutdown to release the GPU arena cleanly."""
        kokoro_tts_provider.unload_models()
        self._clear_lane(self.key_for_adapter("kokoro"))

    def gemini_status(self) -> dict:
        """There is no `unload_gemini()` counterpart: unload exists to free a
        loaded model and clear a latch, and this lane holds no model. Its
        latch clears on the next `_build_voice_runtime`, which is what a
        config edit already triggers.
        """
        return self.adapter_status("gemini")

    async def warm_up_kokoro(self) -> None:
        """Eager-load the Kokoro model + blend on boot so the first
        sentence doesn't pay the ONNX init latency. On failure the engine
        latches a `disabled_reason` and the chain falls through to the
        next lane — the next reload through Settings clears the latch."""
        key = self.key_for_adapter("kokoro")
        lane = self.lanes.get(key)
        if lane is None or lane.config is None:
            return
        timeout = float(lane.config.timeout_seconds)
        try:
            await asyncio.wait_for(
                kokoro_tts_provider.warm_up(lane.config),
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
            # `_set_disabled` adds the cooldown, so a preload failure decays
            # the same way a synthesis failure does rather than holding the
            # lane down for the life of the process.
            self._set_disabled(key, str(exc)[:300])
            raise

    # ---- the chain ----

    def _lane_order(self) -> list[str]:
        """Primary first, then every other configured lane. Dedup keeps a
        lane from being tried twice when it *is* the primary."""
        order: list[str] = []
        for key in (self.provider_key, *self.lanes):
            if key and key in self.lanes and key not in order:
                order.append(key)
        return order

    def _lane_ready(self, lane: str) -> bool:
        """Configured, and not latched off. Asks; changes nothing.

        A latch that has outlived its cooldown is cleared by `_expire_cooldowns`,
        which `synthesize` runs first. Keeping that out of here means a caller
        asking whether a lane is available cannot re-arm one by asking.
        """
        entry = self.lanes.get(lane)
        return entry is not None and entry.ready

    def _expire_cooldowns(self) -> None:
        """Clear every latch that has served its cooldown.

        Lazily, at the top of a turn rather than on a timer: nothing has to run
        while the machine is quiet, and the first sentence after the cooldown is
        the retry.
        """
        now = time.monotonic()
        for lane, retry_at in list(self._lane_retry_at.items()):
            if now < retry_at:
                continue
            logger.info("TTS lane %s is out of its cooldown — trying it again", lane)
            self._clear_lane(lane)

    async def _synthesize_on(self, lane: str, text: str, preset: str) -> bytes:
        entry = self.lanes[lane]
        return await entry.provider.synthesize(text, entry.config, preset=preset)

    def _set_disabled(self, lane: str, reason: str) -> None:
        """Latch `lane` off with `reason`, and stamp when it may be tried again.

        A lane with no configured cooldown gets no expiry, so it stays down
        until an unload or a rebuild clears it.
        """
        entry = self.lanes.get(lane)
        if entry is None:
            return
        entry.disabled_reason = reason
        cooldown = self.lane_cooldown_seconds.get(lane)
        if cooldown:
            self._lane_retry_at[lane] = time.monotonic() + float(cooldown)

    def _clear_lane(self, lane: str) -> None:
        """Forget everything that would keep `lane` from being tried."""
        self._lane_failures.pop(lane, None)
        self._lane_retry_at.pop(lane, None)
        self._lane_last_error.pop(lane, None)
        entry = self.lanes.get(lane)
        if entry is not None:
            entry.disabled_reason = ""

    def _record_failure(self, lane: str, exc: Exception) -> str:
        """Count one failure on `lane`. Returns the latch reason if this one
        took it down, or `""` if the lane is still in the chain.

        The count is CONSECUTIVE: a sentence that succeeds clears it, so three
        scattered failures across a good hour never latch anything.
        """
        count = self._lane_failures.get(lane, 0) + 1
        self._lane_failures[lane] = count
        reason = str(exc)[:300]
        self._lane_last_error[lane] = reason
        if count < MAX_CONSECUTIVE_FAILURES:
            return ""
        self._set_disabled(lane, reason)
        return reason

    def _lane_note(self, lane: str) -> str:
        """What to say about `lane` when the whole chain came up empty."""
        return (
            self.disabled_reason(lane) or self._lane_last_error.get(lane, "") or "ok"
        )

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

        # Before the walk, so a lane whose cooldown ran out during the silence
        # is back in the chain for this sentence rather than the next one.
        self._expire_cooldowns()

        for lane in self._lane_order():
            if not self._lane_ready(lane):
                continue
            if self.cost_ledger is not None:
                self.cost_ledger.voice_check_preflight("tts", lane)
            try:
                audio = await self._synthesize_on(lane, text, preset)
            except Exception as exc:
                reason = self._record_failure(lane, exc)
                if not reason:
                    logger.warning(
                        "TTS lane %s failed (%d of %d before it is set aside); "
                        "trying the next lane: %s",
                        lane, self._lane_failures[lane], MAX_CONSECUTIVE_FAILURES,
                        str(exc)[:200],
                    )
                    continue
                logger.exception(
                    "TTS lane %s failed %d times in a row and is set aside for "
                    "%.0fs; trying the next lane",
                    lane, MAX_CONSECUTIVE_FAILURES,
                    self.lane_cooldown_seconds.get(lane, 0.0),
                )
                if self.lane_down_hook is not None:
                    with contextlib.suppress(Exception):
                        await self.lane_down_hook(lane, reason)
                continue
            self._clear_lane(lane)
            if self.cost_ledger is not None:
                self.cost_ledger.record_voice(
                    "tts",
                    lane,
                    TtsUsage(char_count=char_count, seconds=_audio_seconds(audio)),
                )
            return audio, lane

        notes = ", ".join(f"{key}={self._lane_note(key)}" for key in self.lanes)
        raise NoTTSLaneAvailable(
            "no TTS lane available — configured lanes: "
            f"{self._lane_order() or ['(none)']}; {notes or '(none)'}"
        )
