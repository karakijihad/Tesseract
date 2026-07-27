"""Tests for IpcLaneManager.send_and_await (Task 5 — poll until turn_ended)."""
import pytest
from tesseract.orchestrator.tars_controller.lanes.ipc_proxy import IpcLaneManager


class _TurnClient:
    def __init__(self) -> None:
        self.reads = 0

    async def lane_send(self, lane_id: str, message: str) -> dict:
        return {"accepted": True, "queue_depth": 0}

    async def lane_read(self, lane_id: str, since_cursor=None) -> dict:
        self.reads += 1
        # read 1 is the pre-send tail capture (cursor=None); reads 2+ are poll reads.
        # Poll read 2 returns assistant_text; poll read 3 returns turn_ended.
        kind = "turn_ended" if self.reads >= 3 else "assistant_text"
        return {
            "events": [
                {
                    "lane_id": lane_id,
                    "kind": kind,
                    "payload": {},
                    "at_utc": "2026-01-01T00:00:00Z",
                    "cursor": str(self.reads),
                }
            ],
            "next_cursor": str(self.reads),
            "count": 1,
        }

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_send_and_await_returns_on_turn_ended() -> None:
    fake = _TurnClient()

    async def _c():
        return fake

    mgr = IpcLaneManager(connect_factory=_c)
    res = await mgr.send_and_await("lane-claude-x", "go", timeout=5.0, poll_s=0.0)
    assert res.accepted is True
    # read 1 = tail capture, read 2 = first poll (assistant_text), read 3 = second poll (turn_ended)
    assert fake.reads >= 3


@pytest.mark.asyncio
async def test_send_and_await_returns_immediately_if_send_rejected() -> None:
    class _Reject:
        def __init__(self) -> None:
            self.reads = 0

        async def lane_send(self, lane_id: str, message: str) -> dict:
            return {"accepted": False, "queue_depth": 0, "reason": "busy"}

        async def lane_read(self, lane_id: str, since_cursor=None) -> dict:
            self.reads += 1
            if since_cursor is None:
                # pre-send tail capture — allowed
                return {"events": [], "next_cursor": "0", "count": 0}
            raise AssertionError("must not poll after a rejected send")

        async def close(self) -> None:
            pass

    fake = _Reject()

    async def _c():
        return fake

    mgr = IpcLaneManager(connect_factory=_c)
    res = await mgr.send_and_await("lane-x", "go", timeout=5.0, poll_s=0.0)
    assert res.accepted is False
    assert fake.reads == 1  # exactly the pre-send tail capture, no poll reads


@pytest.mark.asyncio
async def test_send_and_await_does_not_return_on_historical_turn_ended() -> None:
    """Hardening: a lane ALREADY containing a turn_ended at offset-0 must NOT
    cause send_and_await to return before the current turn completes.

    The fix captures the tail cursor before send(), so polls only see events
    appended AFTER the send. This fake simulates a reused lane:
      - lane_read(lane_id, None)  → historical turn_ended (pre-send tail capture)
      - lane_read(lane_id, "1")   → assistant_text  (first poll, post-send)
      - lane_read(lane_id, "2")   → new turn_ended  (second poll, post-send)
    Without the fix the method would return after the first read (cursor=None)
    because it sees the historical turn_ended and returns immediately.
    With the fix it polls past it and returns only after the post-send turn_ended.
    """

    class _HistoricalClient:
        def __init__(self) -> None:
            self.reads = 0

        async def lane_send(self, lane_id: str, message: str) -> dict:
            return {"accepted": True, "queue_depth": 0}

        async def lane_read(self, lane_id: str, since_cursor=None) -> dict:
            self.reads += 1
            if since_cursor is None:
                # pre-send tail capture — returns historical turn_ended at offset 0
                kind = "turn_ended"
                cursor = "1"
            elif since_cursor == "1":
                # first poll after send — assistant_text (current turn in progress)
                kind = "assistant_text"
                cursor = "2"
            else:
                # second poll — new turn_ended from current turn
                kind = "turn_ended"
                cursor = "3"
            return {
                "events": [
                    {
                        "lane_id": lane_id,
                        "kind": kind,
                        "payload": {},
                        "at_utc": "2026-01-01T00:00:00Z",
                        "cursor": cursor,
                    }
                ],
                "next_cursor": cursor,
                "count": 1,
            }

        async def close(self) -> None:
            pass

    fake = _HistoricalClient()

    async def _c():
        return fake

    mgr = IpcLaneManager(connect_factory=_c)
    res = await mgr.send_and_await("lane-claude-x", "hello", timeout=5.0, poll_s=0.0)
    assert res.accepted is True
    # Must have done: 1 tail capture + at least 2 poll reads (assistant_text then turn_ended)
    assert fake.reads >= 3
