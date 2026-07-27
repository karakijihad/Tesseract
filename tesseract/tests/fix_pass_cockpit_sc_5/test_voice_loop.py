"""SC-5 — backend voice-input state machine.

Pure unit tests: the machine has no async, no session, no I/O. They lock
the transition table + the wire-emit / dedup contract the `voice_io`
caller depends on.
"""

from tesseract.mirror.server.voice_loop import VoiceLoop, VoiceLoopState


def test_starts_idle():
    assert VoiceLoop().state is VoiceLoopState.IDLE


def test_note_audio_idle_to_listening_emits_once():
    loop = VoiceLoop()
    # First frame: idle -> listening, emits the wire value.
    assert loop.note_audio(turn_active=False) == "listening"
    assert loop.state is VoiceLoopState.LISTENING
    # Subsequent frames in the same utterance: no re-emit (deduped).
    assert loop.note_audio(turn_active=False) is None
    assert loop.state is VoiceLoopState.LISTENING


def test_note_audio_gated_while_turn_active():
    # Ambient mic audio during TARS's reply must NOT flip the UI to listening.
    loop = VoiceLoop()
    assert loop.note_audio(turn_active=True) is None
    assert loop.state is VoiceLoopState.IDLE


def test_note_audio_noop_mid_transcribe():
    # Frames arriving while STT is running don't transition (only IDLE→LISTENING).
    loop = VoiceLoop()
    loop.begin_transcribe()
    assert loop.note_audio(turn_active=False) is None
    assert loop.state is VoiceLoopState.TRANSCRIBING


def test_full_input_cycle():
    loop = VoiceLoop()
    assert loop.note_audio(turn_active=False) == "listening"
    assert loop.begin_transcribe() == "transcribing"
    assert loop.state is VoiceLoopState.TRANSCRIBING
    assert loop.finish() == "idle"
    assert loop.state is VoiceLoopState.IDLE
    # Ready for the next utterance — listening fires again.
    assert loop.note_audio(turn_active=False) == "listening"


def test_begin_transcribe_always_emits():
    # Commit may arrive without a prior listening (buffered audio); still
    # authoritative — always emits transcribing.
    loop = VoiceLoop()
    assert loop.begin_transcribe() == "transcribing"


def test_finish_always_emits_idle_even_from_idle():
    # An empty / engine-down commit calls finish() while still idle; the
    # frontend relies on the idle reset to clear a local VAD transcribing
    # state, so finish() must emit unconditionally.
    loop = VoiceLoop()
    assert loop.finish() == "idle"
    assert loop.state is VoiceLoopState.IDLE


def test_barge_in_from_idle_goes_listening():
    # Operator speaks over TARS (loop is idle after dispatch) — input half
    # is live again.
    loop = VoiceLoop()
    assert loop.barge_in() == "listening"
    assert loop.state is VoiceLoopState.LISTENING


def test_barge_in_deduped_when_already_listening():
    loop = VoiceLoop()
    loop.note_audio(turn_active=False)
    assert loop.state is VoiceLoopState.LISTENING
    assert loop.barge_in() is None


def test_cancel_always_returns_to_idle():
    loop = VoiceLoop()
    loop.begin_transcribe()
    assert loop.cancel() == "idle"
    assert loop.state is VoiceLoopState.IDLE


def test_cancel_from_idle_still_emits_idle():
    loop = VoiceLoop()
    assert loop.cancel() == "idle"


def test_wire_values_match_frontend_vocabulary():
    # The enum values ARE the `voice_state` wire strings the frontend's
    # applyBackendState understands (idle / listening / transcribing).
    assert VoiceLoopState.IDLE.value == "idle"
    assert VoiceLoopState.LISTENING.value == "listening"
    assert VoiceLoopState.TRANSCRIBING.value == "transcribing"
