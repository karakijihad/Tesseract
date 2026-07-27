"""Phase 5 (2026-07-05): idle-flush replaces the old fixed-window hold-back.

Pre-Phase-5, every chunk — including a single keystroke's echo — waited
up to ``coalesce_flush_ms`` before reaching the WS. That's the "glitchy
typing" root cause: the coalescer was built for high-throughput bulk
output but applied its window uniformly to interactive echo too.

New spec: when the asyncio queue is empty right after a chunk is
processed (nothing else immediately available) and the pending payload
hasn't hit the size cap, flush immediately — don't wait for the window.
The window remains a backstop for sustained output that never idles.

Both tests below configure a deliberately huge ``coalesce_flush_ms``
(seconds, not the prod 8ms) so the assertion isn't a tight race against
Windows' ~15ms timer granularity — if the idle-flush path regressed to
the old window-only behavior, these tests would need to wait out that
multi-second window instead of flushing near-instantly, making the
regression unmistakable rather than a timing coin-flip.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from tesseract.mirror.server.config import ShellProfile, TerminalServerConfig
from tesseract.mirror.server.pty_manager import PTYEntry, PTYManager

_HUGE_WINDOW_MS = 5_000.0
_PROOF_DEADLINE_S = 1.0  # generous vs. thread-scheduling jitter, tiny vs. the window


def _make_manager_with_huge_window() -> PTYManager:
    cfg = TerminalServerConfig(
        default_shell="cmd",
        max_tabs=4,
        max_panes_per_tab=4,
        shell_profiles={"cmd": ShellProfile(argv=("cmd.exe",), label="cmd")},
        coalesce_flush_ms=_HUGE_WINDOW_MS,
        coalesce_flush_chars=4096,
        reattach_grace_s=30.0,
        pause_buffer_cap_chars=2_000_000,
    )
    return PTYManager(cfg)


@pytest.mark.asyncio
async def test_lone_small_chunk_flushes_without_waiting_for_window() -> None:
    """Drive the reader thread by hand so the first chunk arrives, THEN
    the thread blocks (simulating a quiet PTY) well past the (huge,
    5s) coalesce window before EOF. Under the OLD spec the chunk would
    sit in the coalesce buffer until the window elapsed; under the NEW
    spec the queue goes idle immediately after the chunk lands, so it
    must reach the WS within ~1s — nowhere near the 5s window."""
    block_release = threading.Event()
    read_calls: list[int] = []

    def _read(_n: int) -> str:
        read_calls.append(len(read_calls))
        if len(read_calls) == 1:
            return "tiny "
        block_release.wait(timeout=10.0)
        raise EOFError()

    sent: list[dict[str, Any]] = []

    async def _send_json(payload: dict[str, Any]) -> None:
        sent.append(payload)

    proc = SimpleNamespace(
        isalive=lambda: True,
        write=lambda *a, **k: None,
        terminate=lambda *a, **k: None,
        read=_read,
    )
    ws = SimpleNamespace(closed=False, send_json=_send_json)
    manager = _make_manager_with_huge_window()
    entry = PTYEntry(
        pane_id="pty_idle_flush",
        shell="cmd",
        proc=proc,  # type: ignore[arg-type]
        ws=ws,  # type: ignore[arg-type]
    )
    manager._ptys["pty_idle_flush"] = entry  # noqa: SLF001

    loop_task = asyncio.create_task(manager._reader_loop(entry))  # noqa: SLF001
    started_at = time.monotonic()

    deadline = started_at + _PROOF_DEADLINE_S
    while time.monotonic() < deadline:
        if any("tiny " in f.get("bytes", "") for f in sent):
            break
        await asyncio.sleep(0.001)
    else:
        block_release.set()
        await loop_task
        pytest.fail(
            f"tiny chunk did not flush within {_PROOF_DEADLINE_S}s — "
            f"idle-flush path broken (would need the full "
            f"{_HUGE_WINDOW_MS}ms window under the old spec)"
        )

    elapsed_s = time.monotonic() - started_at
    assert elapsed_s < _PROOF_DEADLINE_S, (
        f"chunk took {elapsed_s:.3f}s — nowhere near instant, and the window "
        f"is {_HUGE_WINDOW_MS / 1000.0}s, so this wasn't a window-timeout flush either"
    )
    assert any("tiny " in f.get("bytes", "") for f in sent)

    block_release.set()
    await loop_task


@pytest.mark.asyncio
async def test_trickling_small_chunks_flush_individually_not_batched_at_window() -> None:
    """Two small chunks separated by a short gap (milliseconds — tiny
    next to the 5s window) must arrive as two SEPARATE WS frames — proof
    the coalescer isn't batching them together and waiting for the
    window to elapse before sending. Under the OLD spec both would ride
    out in one frame only once the 5s window tripped; under the NEW spec
    each flushes as soon as the queue idles after it lands."""
    gap_s = 0.01
    block_release = threading.Event()
    read_calls: list[int] = []

    def _read(_n: int) -> str:
        read_calls.append(len(read_calls))
        if len(read_calls) == 1:
            return "a"
        if len(read_calls) == 2:
            time.sleep(gap_s)
            return "b"
        block_release.wait(timeout=10.0)
        raise EOFError()

    sent: list[dict[str, Any]] = []

    async def _send_json(payload: dict[str, Any]) -> None:
        sent.append(payload)

    proc = SimpleNamespace(
        isalive=lambda: True,
        write=lambda *a, **k: None,
        terminate=lambda *a, **k: None,
        read=_read,
    )
    ws = SimpleNamespace(closed=False, send_json=_send_json)
    manager = _make_manager_with_huge_window()
    entry = PTYEntry(
        pane_id="pty_trickle",
        shell="cmd",
        proc=proc,  # type: ignore[arg-type]
        ws=ws,  # type: ignore[arg-type]
    )
    manager._ptys["pty_trickle"] = entry  # noqa: SLF001

    loop_task = asyncio.create_task(manager._reader_loop(entry))  # noqa: SLF001

    deadline = time.monotonic() + _PROOF_DEADLINE_S
    while time.monotonic() < deadline:
        output_frames = [f for f in sent if f.get("type") == "terminal_output_chunk"]
        if len(output_frames) >= 2:
            break
        await asyncio.sleep(0.002)

    block_release.set()
    await loop_task

    output_frames = [f for f in sent if f.get("type") == "terminal_output_chunk"]
    assert len(output_frames) == 2, (
        f"expected 2 separate frames (one per idle-flushed chunk) within "
        f"{_PROOF_DEADLINE_S}s (the window is {_HUGE_WINDOW_MS / 1000.0}s), "
        f"got {len(output_frames)}: {output_frames}"
    )
    assert output_frames[0]["bytes"] == "a"
    assert output_frames[1]["bytes"] == "b"
