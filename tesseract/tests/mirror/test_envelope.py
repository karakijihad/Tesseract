"""mirror-multi-chat P2 inc.A — envelope chat_id protocol.

`make_envelope` stamps `chat_id` on turn-scoped envelopes so the frontend can
route each event to its chat's slice: explicit arg wins, else the running
turn's `current_chat_id` ContextVar. Voice / session / broadcast types stay
session-scoped (no chat_id).
"""

from __future__ import annotations

from tesseract.mirror.server.envelope import make_envelope
from tesseract.mirror.server.turn_context import current_chat_id, current_turn_id

_CID = "0123456789abcdef0123456789abcdef"


def test_explicit_chat_id_stamped_on_turn_scoped() -> None:
    env = make_envelope("stream_text", "loop", "test-session", {"text": "hi"}, chat_id=_CID)
    assert env["chat_id"] == _CID


def test_chat_id_autostamped_from_contextvar_on_turn_scoped() -> None:
    token = current_chat_id.set(_CID)
    try:
        env = make_envelope("loop_start", "loop", "test-session", {"turn": 1})
    finally:
        current_chat_id.reset(token)
    assert env["chat_id"] == _CID


def test_explicit_chat_id_wins_over_contextvar() -> None:
    other = "f" * 32
    token = current_chat_id.set(_CID)
    try:
        env = make_envelope("stream_stop", "loop", "test-session", {}, chat_id=other)
    finally:
        current_chat_id.reset(token)
    assert env["chat_id"] == other


def test_non_turn_scoped_stays_session_scoped() -> None:
    # session_created is NOT in the turn-scoped set — even with the ContextVar
    # set (e.g. a voice turn in flight), it must not inherit a chat_id.
    token = current_chat_id.set(_CID)
    try:
        env = make_envelope("session_created", "session", "test-session", {})
    finally:
        current_chat_id.reset(token)
    assert "chat_id" not in env


def test_no_chat_id_when_absent() -> None:
    env = make_envelope("stream_text", "loop", "test-session", {"text": "hi"})
    assert "chat_id" not in env


def test_turn_id_still_stamped_alongside_chat_id() -> None:
    # Regression: the chat_id addition must not break turn_id stamping.
    tid_token = current_turn_id.set("turn-abc")
    cid_token = current_chat_id.set(_CID)
    try:
        env = make_envelope("stream_text", "loop", "test-session", {"text": "hi"})
    finally:
        current_turn_id.reset(tid_token)
        current_chat_id.reset(cid_token)
    assert env["turn_id"] == "turn-abc"
    assert env["chat_id"] == _CID
