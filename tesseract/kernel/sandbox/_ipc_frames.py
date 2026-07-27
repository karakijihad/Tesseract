"""Length-prefixed JSON frame helpers for the MO-8-3 IPC bridge.

Wire format per ``Docs/Plan/mission-orchestrator/MO-8/_shared/ipc-bridge.md``:
``<4-byte little-endian uint32 length><utf-8 JSON bytes>``. Length excludes
the 4-byte prefix itself. Frames cap at 8 MiB (``MAX_FRAME_BYTES``); over-size
frames raise so a runaway candidate cannot drown the main process.
"""

from __future__ import annotations

import asyncio
import json
import struct
from typing import Any, Final


# Frame-type constants — used in the ``frame`` field of every payload.
FRAME_TOOL_CALL_REQUEST: Final[str] = "tool_call_request"
FRAME_TOOL_CALL_RESPONSE: Final[str] = "tool_call_response"
FRAME_KPI_RESULT: Final[str] = "kpi_result"
FRAME_ERROR: Final[str] = "error"
FRAME_TERMINATE: Final[str] = "terminate"

# 8 MiB ceiling — generous for JSON tracebacks, tight enough to refuse abuse.
MAX_FRAME_BYTES: Final[int] = 8 * 1024 * 1024

_LEN_PREFIX = struct.Struct("<I")


def encode_frame(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise ValueError(f"frame body {len(body)} exceeds MAX_FRAME_BYTES {MAX_FRAME_BYTES}")
    return _LEN_PREFIX.pack(len(body)) + body


async def decode_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Read one frame; raises ``ConnectionError`` on EOF mid-frame."""
    prefix = await reader.readexactly(_LEN_PREFIX.size)
    (length,) = _LEN_PREFIX.unpack(prefix)
    if length > MAX_FRAME_BYTES:
        raise ValueError(f"frame length {length} exceeds MAX_FRAME_BYTES {MAX_FRAME_BYTES}")
    body = await reader.readexactly(length)
    return json.loads(body.decode("utf-8"))


__all__ = [
    "FRAME_TOOL_CALL_REQUEST",
    "FRAME_TOOL_CALL_RESPONSE",
    "FRAME_KPI_RESULT",
    "FRAME_ERROR",
    "FRAME_TERMINATE",
    "MAX_FRAME_BYTES",
    "encode_frame",
    "decode_frame",
]
