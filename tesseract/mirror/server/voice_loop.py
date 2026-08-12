"""SC-5 — server-side voice-input state machine.

The full voice loop spans frontend + backend. This module owns the
BACKEND (speech-in) half and produces the `voice_state` wire values that
drive the HUD mic button + orb:

    IDLE --audio--> LISTENING --commit--> TRANSCRIBING --done--> IDLE

The output (speech-back) half is owned downstream and is deliberately NOT
emitted here, so the two halves never race over the frontend's
`voice.state`:

  - RESPONDING — the assistant is thinking. The orb's `thinking` state, driven by
    the chat stream, is the indicator. No `voice_state` for it.
  - SPEAKING — the assistant is talking. The frontend derives `speaking_back`
    from the `tts_chunk` envelopes it actually plays (`stores/dispatch.ts`),
    which is authoritative (tied to real audio) — re-emitting it here
    would only race that.

Fast partials are client-side: the browser Web Speech API surfaces an
interim transcript (`lib/voice/stt-stream.ts`); the backend `voice_partial`
envelope was removed in the Phase-16-S1 simplification and is not
reintroduced.

The machine is pure + synchronous: each transition method mutates the
state and returns the `voice_state` wire value to emit (or `None` when no
envelope should be sent). The caller owns the actual `send_envelope`, so
this stays trivially unit-testable with no async and no session.
"""

from __future__ import annotations

import logging
from enum import Enum

log = logging.getLogger(__name__)


class VoiceLoopState(str, Enum):
    """The backend (speech-in) states. Values are the `voice_state` wire
    strings the frontend already understands (`lib/types.ts::VoiceState`)."""

    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"


class VoiceLoop:
    """Per-session backend voice-input state machine.

    `begin_transcribe` / `finish` / `cancel` are authoritative resets that
    ALWAYS emit their wire value (the frontend relies on the `idle` /
    `transcribing` signal to clear a local VAD state on commit). `note_audio`
    and `barge_in` are deduplicated — they only emit on a real change so the
    hot PCM path never floods the wire.
    """

    def __init__(self) -> None:
        self._state = VoiceLoopState.IDLE

    @property
    def state(self) -> VoiceLoopState:
        return self._state

    def note_audio(self, *, turn_active: bool) -> str | None:
        """A PCM frame arrived. Enter LISTENING from IDLE only — and only
        when no chat turn is in flight, so ambient mic audio during the assistant's
        reply can't flip the mic UI out of `speaking_back`. Deduped: returns
        `None` once already listening (or when gated)."""
        if turn_active or self._state is not VoiceLoopState.IDLE:
            return None
        return self._set(VoiceLoopState.LISTENING)

    def begin_transcribe(self) -> str:
        """`voice_commit` — STT is now running. Always emits."""
        self._state = VoiceLoopState.TRANSCRIBING
        return VoiceLoopState.TRANSCRIBING.value

    def finish(self) -> str:
        """Transcript delivered (or empty / engine-down) — the input half is
        done and ready for the next utterance; the reply is downstream.
        Always emits `idle` so a local VAD `transcribing` state is cleared
        even when the commit produced no audio."""
        self._state = VoiceLoopState.IDLE
        return VoiceLoopState.IDLE.value

    def barge_in(self) -> str | None:
        """Operator started speaking over the assistant — the input half is live
        again (TTS teardown is the caller's job). Deduped."""
        if self._state is VoiceLoopState.LISTENING:
            return None
        return self._set(VoiceLoopState.LISTENING)

    def cancel(self) -> str:
        """Full operator cancel / reset — back to IDLE. Always emits."""
        self._state = VoiceLoopState.IDLE
        return VoiceLoopState.IDLE.value

    def _set(self, target: VoiceLoopState) -> str:
        log.debug("voice loop: %s -> %s", self._state.value, target.value)
        self._state = target
        return target.value
