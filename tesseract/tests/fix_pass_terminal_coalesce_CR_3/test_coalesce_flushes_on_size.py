"""CR-3 / Phase 5 (2026-07-05): size-trigger coalescing still holds.

The adaptive coalescer (Phase 5) flushes immediately when the queue is
idle and the payload is small (interactive echo), but a chunk that by
itself meets or exceeds ``coalesce_flush_chars`` still flushes on the
size trigger — unaffected by the idle-flush change since the size
check runs first.
"""

from __future__ import annotations

import pytest

from .conftest import COALESCE_FLUSH_CHARS, make_manager, make_scripted_entry


@pytest.mark.asyncio
async def test_multi_chunk_bytes_preserved_in_order() -> None:
    """Byte fidelity invariant: regardless of how the coalescer groups
    the chunks into WS frames, the concatenated `bytes` payloads
    reproduce the original input in order. (The exact frame count
    depends on real wall-clock timing of `asyncio.to_thread` dispatches
    relative to the 8 ms coalesce window — tested separately by the
    single-large-chunk size-trigger case below.)"""
    chunks: list[str] = [f"chunk-{i:02d} " for i in range(8)]
    chunks.append("")
    manager = make_manager()
    entry, sent = make_scripted_entry(manager, "pty_multi", chunks)
    entry.proc.isalive = lambda: False  # type: ignore[method-assign]

    await manager._reader_loop(entry)  # noqa: SLF001

    output_frames = [f for f in sent if f.get("type") == "terminal_output_chunk"]
    total_bytes = "".join(f["bytes"] for f in output_frames)
    expected = "".join(f"chunk-{i:02d} " for i in range(8))
    assert total_bytes == expected, (
        f"byte ordering / fidelity broken under coalescing.\n"
        f"got:      {total_bytes!r}\nexpected: {expected!r}"
    )
    # And: at most one frame per chunk — no spurious extra frames.
    assert len(output_frames) <= 8


@pytest.mark.asyncio
async def test_single_large_chunk_flushes_immediately() -> None:
    """A chunk that meets or exceeds COALESCE_FLUSH_CHARS by itself
    flushes on the same iteration — no waiting for more data."""
    big = "Y" * (COALESCE_FLUSH_CHARS + 100)
    manager = make_manager()
    entry, sent = make_scripted_entry(manager, "pty_big", [big, ""])
    entry.proc.isalive = lambda: False  # type: ignore[method-assign]

    await manager._reader_loop(entry)  # noqa: SLF001

    output_frames = [f for f in sent if f.get("type") == "terminal_output_chunk"]
    assert len(output_frames) == 1
    assert output_frames[0]["bytes"] == big
