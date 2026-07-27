"""`stream_tool_call_delta` must not cross the WS wire.

Per-chunk tool-call argument fragments carry no operator-visible signal and,
when forwarded, dominated both the WS stream and the 500-slot `event_log`
ring buffer. `chunk_to_envelope` now returns `None` for `TOOL_CALL_DELTA`
alongside the existing `REASONING_ITEM` skip so the event never enters
`send_envelope`.
"""

from __future__ import annotations

from tesseract.kernel.adapters.base import ChunkType, StreamChunk
from tesseract.mirror.server.envelope import chunk_to_envelope


def test_tool_call_delta_returns_none() -> None:
    chunk = StreamChunk(
        type=ChunkType.TOOL_CALL_DELTA,
        tool_call_id="call_1",
        text='{"pattern":"foo"',
    )
    assert chunk_to_envelope(chunk, session_id="s1") is None


def test_tool_call_start_still_forwarded() -> None:
    # Defensive: the drop must not accidentally suppress the bracketing
    # start/end/result envelopes — those are the operator-visible signal.
    chunk = StreamChunk(
        type=ChunkType.TOOL_CALL_START,
        tool_call_id="call_1",
    )
    env = chunk_to_envelope(chunk, session_id="s1")
    assert env is not None
    assert env["type"] == "stream_tool_call_start"
