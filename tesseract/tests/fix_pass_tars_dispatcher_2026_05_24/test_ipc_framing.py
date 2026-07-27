import asyncio

import pytest

from tesseract.kernel.sandbox._ipc_frames import (
    MAX_FRAME_BYTES,
    decode_frame,
    encode_frame,
)


@pytest.mark.asyncio
async def test_large_frame_round_trips_over_loopback():
    big = {"event": "transcript_event", "transcript_event": {"text": "x" * 200_000}}
    received: dict | None = None

    async def handle(reader, writer):
        nonlocal received
        received = await decode_frame(reader)
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(encode_frame(big))
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.close()
    assert received == big  # 200 KB line would have raised LimitOverrunError under readline()


@pytest.mark.asyncio
async def test_oversize_frame_rejected_at_encode():
    with pytest.raises(ValueError):
        encode_frame({"blob": "y" * (MAX_FRAME_BYTES + 1)})
