"""2026-07-13 incident regression guards — lane send/wait wiring.

Incident: `LaneManager.send` ran the whole turn inline, the daemon acked
`lane_send` only after `send` returned, and the IPC client waited for
that ack with a hardcoded 30 s — so every lane turn longer than 30 s was
falsely reported "failed" to the brain, which then span up redundant
controller-session fallbacks.

Pins:
- `send` is fire-and-queue: the ack returns while the turn is still
  running (submit → accept → stream → turn_ended, the CLI-agent shape).
- Every accepted turn gets a `turn_ended` event, even when the adapter
  raises — stream waiters must never hang on a crashed turn.
- The lane survives a crashed turn (next send runs normally).
- `send_and_await` blocks in-process until turn_ended (lane_send
  wait=True parity with the IPC proxy).
- The IPC accept-ack ceiling comes from cockpit.yaml, not a literal.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

import pytest

from tesseract.orchestrator.tars_controller.lanes import Lane, LaneManager
from tesseract.orchestrator.tars_controller.lanes.manager import LaneRuntime


class _GatedAdapter:
    """Blocks mid-turn until the test releases it — a long CLI turn."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run_turn(
        self,
        *,
        message: str,
        on_event: Callable[[dict[str, Any]], None],
        cancel_event: asyncio.Event | None,
    ) -> dict[str, Any]:
        self.started.set()
        await self.release.wait()
        on_event({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "done late"}]},
        })
        return {"session_id": "sess-gated", "is_error": False, "usage": {}}


class _RaisingAdapter:
    async def run_turn(self, *, message, on_event, cancel_event):
        raise RuntimeError("CLI exploded mid-turn")


class _EchoAdapter:
    async def run_turn(self, *, message, on_event, cancel_event):
        on_event({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": f"echo: {message}"}]},
        })
        return {"session_id": "sess-echo", "is_error": False, "usage": {}}


def _factory_for(adapter: Any) -> Callable[[Lane, LaneRuntime], Any]:
    return lambda lane, runtime: adapter


async def _open(mgr: LaneManager, home: Path) -> str:
    return await mgr.open(
        kind="claude", mode="headless", model="claude-sonnet-4-6",
        working_dir=str(home),
    )


def test_send_acks_while_turn_still_running(isolated_home: Path) -> None:
    """The incident pin: the ack means 'queued', never 'completed'."""
    adapter = _GatedAdapter()
    mgr = LaneManager(adapter_factory=_factory_for(adapter))

    async def _run() -> None:
        lane_id = await _open(mgr, isolated_home)
        result = await mgr.send(lane_id, "long turn")
        assert result.accepted  # returned immediately — turn not finished
        await asyncio.wait_for(adapter.started.wait(), timeout=2.0)
        status = mgr.status(lane_id)
        assert status.busy or status.queue_depth > 0
        events, _ = mgr.read(lane_id, None)
        assert not any(e.kind == "turn_ended" for e in events)

        adapter.release.set()
        await asyncio.wait_for(mgr.drain(lane_id), timeout=2.0)
        events, _ = mgr.read(lane_id, None)
        assert any(e.kind == "turn_ended" for e in events)
        assert mgr.status(lane_id).busy is False

    asyncio.run(_run())


def test_adapter_crash_still_emits_turn_ended(isolated_home: Path) -> None:
    """Completion contract: waiters key off turn_ended and must never
    hang on a crashed turn."""
    mgr = LaneManager(adapter_factory=_factory_for(_RaisingAdapter()))

    async def _run() -> None:
        lane_id = await _open(mgr, isolated_home)
        result = await mgr.send(lane_id, "boom")
        assert result.accepted
        await asyncio.wait_for(mgr.drain(lane_id), timeout=2.0)
        events, _ = mgr.read(lane_id, None)
        ended = [e for e in events if e.kind == "turn_ended"]
        assert len(ended) == 1
        assert ended[0].payload["is_error"] is True
        assert "CLI exploded" in ended[0].payload["error"]
        assert any(e.kind == "error" for e in events)
        assert mgr.status(lane_id).busy is False

    asyncio.run(_run())


def test_lane_survives_crashed_turn(isolated_home: Path) -> None:
    crash_then_echo = [_RaisingAdapter(), _EchoAdapter()]

    class _FlakyAdapter:
        async def run_turn(self, **kwargs):
            return await crash_then_echo.pop(0).run_turn(**kwargs)

    mgr = LaneManager(adapter_factory=_factory_for(_FlakyAdapter()))

    async def _run() -> None:
        lane_id = await _open(mgr, isolated_home)
        await mgr.send(lane_id, "boom")
        await asyncio.wait_for(mgr.drain(lane_id), timeout=2.0)
        await mgr.send(lane_id, "recover")
        await asyncio.wait_for(mgr.drain(lane_id), timeout=2.0)
        events, _ = mgr.read(lane_id, None)
        texts = [e for e in events if e.kind == "assistant_text"]
        assert any("echo: recover" in e.payload["text"] for e in texts)
        ended = [e for e in events if e.kind == "turn_ended"]
        assert [bool(e.payload["is_error"]) for e in ended] == [True, False]

    asyncio.run(_run())


def test_send_and_await_blocks_until_turn_ended(isolated_home: Path) -> None:
    """lane_send(wait=True) parity: in-process manager must block the
    same way IpcLaneManager.send_and_await does."""
    mgr = LaneManager(adapter_factory=_factory_for(_EchoAdapter()))

    async def _run() -> None:
        lane_id = await _open(mgr, isolated_home)
        result = await mgr.send_and_await(
            lane_id, "wait for me", timeout=2.0, poll_s=0.01
        )
        assert result.accepted
        events, _ = mgr.read(lane_id, None)
        assert any(e.kind == "turn_ended" for e in events)

    asyncio.run(_run())


def test_close_settles_inflight_turn_before_archive(isolated_home: Path) -> None:
    """A straggler task appending after the archive move would recreate
    the live lane dir next to the archive."""
    adapter = _GatedAdapter()
    mgr = LaneManager(adapter_factory=_factory_for(adapter))

    async def _run() -> None:
        lane_id = await _open(mgr, isolated_home)
        await mgr.send(lane_id, "long turn")
        await asyncio.wait_for(adapter.started.wait(), timeout=2.0)
        result = await asyncio.wait_for(
            mgr.close(lane_id, reason="test-close"), timeout=2.0
        )
        assert result["final_status"] == "closed"
        live_dir = isolated_home / "controller" / "lanes" / lane_id
        assert not live_dir.exists()

    asyncio.run(_run())


def test_lane_ack_timeout_loader_reads_cockpit_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tesseract.config import cockpit

    yaml_path = tmp_path / "cockpit.yaml"
    yaml_path.write_text(
        "conductor:\n  lane_ack_timeout_s: 12.5\n", encoding="utf-8"
    )
    monkeypatch.setattr(cockpit, "_COCKPIT_YAML", yaml_path)
    assert cockpit.load_lane_ack_timeout_s() == 12.5

    yaml_path.write_text("conductor: {}\n", encoding="utf-8")
    with pytest.raises(KeyError):
        cockpit.load_lane_ack_timeout_s()
