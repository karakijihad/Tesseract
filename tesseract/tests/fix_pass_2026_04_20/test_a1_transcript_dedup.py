"""A1 — incremental observer stops after first fire.

Codex Finding #1. Confirmed bug in ObservationTranscript.append_chat_turns:
dedup uses enumerate(new_turns) offsets, which are slice-local (always
start at 0). The caller already passes a delta, so the offset-based
dedup rejects every subsequent call.

Run BEFORE fix: both asserts fail (turn-2 adds 0, not 2).
Run AFTER fix: both asserts pass.
"""

from __future__ import annotations

from tesseract.brain.observation_transcript import ObservationTranscript


def test_a1_transcript_dedup() -> None:
    t = ObservationTranscript()

    # Turn 1: caller (ChatSession._notify_observer_turn_end) sends the new delta.
    delta_1 = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    added_1 = t.append_chat_turns(delta_1)
    assert added_1 == 2, f"turn 1: expected 2 appended, got {added_1}"
    assert len(t.chat_turns) == 2, f"turn 1: chat_turns len {len(t.chat_turns)}"

    # Turn 2: caller sends the NEXT delta (only new turns).
    delta_2 = [
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]
    added_2 = t.append_chat_turns(delta_2)
    assert added_2 == 2, f"turn 2: expected 2 appended, got {added_2} (BUG: dedup rejects new turns)"
    assert len(t.chat_turns) == 4, f"turn 2: chat_turns len {len(t.chat_turns)}"

    # Turn 3: same delta-shape again — must keep appending.
    delta_3 = [
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
    ]
    added_3 = t.append_chat_turns(delta_3)
    assert added_3 == 2, f"turn 3: expected 2 appended, got {added_3}"
    assert len(t.chat_turns) == 6, f"turn 3: chat_turns len {len(t.chat_turns)}"

