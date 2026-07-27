"""``ControllerClient._await_event_or_error`` surfaces error pushes.

The new request/response methods (``delete_session``, ``rename_session``,
``reload``) await this helper so a daemon-side refusal raises
:class:`ControllerClientError` instead of timing out waiting for a
success event that will never arrive.

2026-05-24 — these were rewritten when the client gained a reply
demultiplexer (`_resolve_reply`): request/reply calls now resolve via a
Future the reader fulfils, so a concurrent ``pushes()`` consumer can no
longer swallow the reply (that bug broke `/sessions` in the TUI). The
tests now drive a scripted reader rather than stuffing the inbox.
"""

from __future__ import annotations

import asyncio

import pytest

from tesseract.kernel.sandbox._ipc_frames import encode_frame
from tesseract.orchestrator.tars_controller.ipc_client import (
    ControllerClient,
    ControllerClientError,
)


class _ScriptedReader:
    """Delivers pre-scripted length-prefixed frames, then blocks (open socket)."""

    def __init__(self, payloads: list[dict]) -> None:
        self._buf = bytearray()
        for p in payloads:
            self._buf += encode_frame(p)

    async def readexactly(self, n: int) -> bytes:
        while len(self._buf) < n:
            await asyncio.sleep(3600)  # open socket, nothing more to send
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk


class _ClosingReader:
    """Delivers nothing and immediately EOFs (socket closed)."""

    async def readexactly(self, n: int) -> bytes:
        raise asyncio.IncompleteReadError(b"", n)


class _FakeWriter:
    def write(self, _data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


def _client(reader) -> ControllerClient:
    client = ControllerClient(reader, _FakeWriter(), "tok")  # type: ignore[arg-type]
    client._start_reader_task()
    return client


@pytest.mark.asyncio
async def test_await_or_error_returns_matching_event() -> None:
    client = _client(
        _ScriptedReader([{"event": "session_deleted", "session_id": "sid"}])
    )
    result = await client._await_event_or_error("session_deleted")
    assert result["session_id"] == "sid"
    await client.close()


@pytest.mark.asyncio
async def test_await_or_error_raises_on_error_push() -> None:
    client = _client(
        _ScriptedReader(
            [{"event": "error", "code": "session_attached", "detail": "x"}]
        )
    )
    with pytest.raises(ControllerClientError) as info:
        await client._await_event_or_error("session_deleted")
    assert "session_attached" in str(info.value)
    await client.close()


@pytest.mark.asyncio
async def test_await_or_error_ignores_unrelated_pushes() -> None:
    """A transcript push must NOT resolve a session_deleted waiter; it
    flows to the pushes() consumer while the request keeps waiting."""
    client = _client(
        _ScriptedReader(
            [
                {"event": "transcript_event", "transcript_event": {}},
                {"event": "session_deleted", "session_id": "sid"},
            ]
        )
    )
    result = await client._await_event_or_error("session_deleted")
    assert result["event"] == "session_deleted"
    # The unrelated transcript push reached the inbox for pushes().
    pushed = await asyncio.wait_for(client._inbox.get(), timeout=1.0)
    assert pushed["event"] == "transcript_event"
    await client.close()


@pytest.mark.asyncio
async def test_await_or_error_raises_on_disconnect() -> None:
    client = _client(_ClosingReader())
    with pytest.raises(ControllerClientError, match="disconnect"):
        await client._await_event_or_error("session_deleted")
    await client.close()


@pytest.mark.asyncio
async def test_await_or_error_times_out() -> None:
    client = _client(_ScriptedReader([]))  # nothing matching arrives
    with pytest.raises(ControllerClientError, match="timed out"):
        await client._await_event_or_error("session_deleted", timeout=0.1)
    await client.close()
