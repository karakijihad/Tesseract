"""CR-3: per-chunk fidelity for consumers and observer push.

Coalescing happens between the PTY reader and the WebSocket — only
the WS frame count drops. Mission transcript consumers and the
observer push pipeline still receive EACH chunk as it is read, so
end-of-turn detection, transcript capture, and per-chunk observation
all behave exactly as before CR-3.
"""

from __future__ import annotations

import pytest

from .conftest import make_manager, make_scripted_entry


@pytest.mark.asyncio
async def test_output_consumers_see_every_chunk_individually() -> None:
    chunks = ["one", "two", "three", "four", "five", ""]
    manager = make_manager()
    entry, sent = make_scripted_entry(manager, "pty_consumers", chunks)
    entry.proc.isalive = lambda: False  # type: ignore[method-assign]

    received: list[str] = []
    entry.output_consumers.append(received.append)

    await manager._reader_loop(entry)  # noqa: SLF001

    # Consumers see exactly the 5 non-empty chunks, in order.
    assert received == ["one", "two", "three", "four", "five"], (
        f"per-chunk consumer fidelity broken: {received}"
    )


@pytest.mark.asyncio
async def test_observer_push_invoked_per_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_forward_to_observer`` is called once per chunk when observer
    is enabled, regardless of coalescing on the WS side."""
    chunks = ["a", "b", "c", "d", ""]
    manager = make_manager()
    entry, sent = make_scripted_entry(manager, "pty_obs", chunks)
    entry.observer_enabled = True
    entry.proc.isalive = lambda: False  # type: ignore[method-assign]

    pushes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        manager,
        "_forward_to_observer",
        lambda pane_id, chunk: pushes.append((pane_id, chunk)),
    )

    await manager._reader_loop(entry)  # noqa: SLF001

    chunk_texts = [c for _, c in pushes]
    assert chunk_texts == ["a", "b", "c", "d"], (
        f"observer push not per-chunk: {chunk_texts}"
    )


@pytest.mark.asyncio
async def test_observer_push_skipped_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Observer respects the consent gate even under coalescing."""
    chunks = ["x", "y", ""]
    manager = make_manager()
    entry, sent = make_scripted_entry(manager, "pty_obs_off", chunks)
    entry.observer_enabled = False
    entry.proc.isalive = lambda: False  # type: ignore[method-assign]

    pushes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        manager,
        "_forward_to_observer",
        lambda pane_id, chunk: pushes.append((pane_id, chunk)),
    )

    await manager._reader_loop(entry)  # noqa: SLF001

    assert pushes == [], (
        f"observer push fired despite consent gate: {pushes}"
    )
