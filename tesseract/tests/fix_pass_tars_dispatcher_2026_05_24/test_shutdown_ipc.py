"""``shutdown`` IPC + tars_cli ``--shutdown`` / ``--restart`` / ``--keep`` flags.

The 2026-05-24 UX shift makes Ctrl+C / ``:quit`` in the TUI tear the
daemon down by default so the next ``tars`` invocation picks up code
edits without a manual taskkill. ``--keep`` preserves the original
detach-without-kill semantics for the headless / multi-attach case.

Tests directly drive the daemon dispatcher with a synthesized
``ShutdownMessage`` (no real client TCP needed) and assert that the
operator-shutdown event fires + the ACK lands.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tesseract.orchestrator.tars_controller.daemon import (
    ControllerDaemon,
    _ClientConn,
)
from tesseract.orchestrator.tars_controller.protocol import (
    ShutdownMessage,
    parse_client_message,
)
from tesseract.orchestrator.tars_controller.sessions import SessionRegistry


def _make_conn() -> _ClientConn:
    return _ClientConn(writer_id=1, outbound=asyncio.Queue())


async def _drain(conn: _ClientConn) -> list[dict[str, Any]]:
    pushes: list[dict[str, Any]] = []
    while not conn.outbound.empty():
        item = conn.outbound.get_nowait()
        if item is None:
            break
        pushes.append(item)
    return pushes


def test_protocol_parses_shutdown_message(isolated_home: Path) -> None:
    msg = parse_client_message({"msg": "shutdown"})
    assert isinstance(msg, ShutdownMessage)


@pytest.mark.asyncio
async def test_on_shutdown_sets_operator_event_and_acks(
    isolated_home: Path,
) -> None:
    daemon = ControllerDaemon(
        controller_id="ctrl-test-shutdown",
        token="t",
        registry=SessionRegistry(),
    )
    assert daemon.operator_shutdown_event.is_set() is False

    conn = _make_conn()
    await daemon._on_shutdown(conn, ShutdownMessage())

    assert daemon.operator_shutdown_event.is_set() is True
    pushes = await _drain(conn)
    ack = next((p for p in pushes if p.get("event") == "ack"), None)
    assert ack is not None, pushes
    assert ack["msg"] == "shutdown"


@pytest.mark.asyncio
async def test_shutdown_event_unblocks_concurrent_waiter(
    isolated_home: Path,
) -> None:
    """run_controller awaits ``operator_shutdown_event`` alongside the
    OS-signal stop event. Verify that an IPC shutdown fires the event
    fast enough that the waiter wakes within one loop turn."""
    daemon = ControllerDaemon(
        controller_id="ctrl-test-shutdown-race",
        token="t",
        registry=SessionRegistry(),
    )

    async def _waiter() -> bool:
        try:
            await asyncio.wait_for(
                daemon.operator_shutdown_event.wait(), timeout=2.0
            )
            return True
        except asyncio.TimeoutError:
            return False

    waiter_task = asyncio.create_task(_waiter())
    # Yield once so waiter_task actually enters the wait.
    await asyncio.sleep(0)

    conn = _make_conn()
    await daemon._on_shutdown(conn, ShutdownMessage())

    woke = await waiter_task
    assert woke is True


def test_cli_parser_has_shutdown_keep_restart_flags(
    isolated_home: Path,
) -> None:
    from tesseract.scripts.tars_cli import _build_parser

    parser = _build_parser()
    ns = parser.parse_args(["--shutdown"])
    assert ns.shutdown is True
    ns = parser.parse_args(["--keep"])
    assert ns.keep is True
    ns = parser.parse_args(["--restart"])
    assert ns.restart is True
    # Default: all three flags off.
    ns = parser.parse_args([])
    assert ns.shutdown is False
    assert ns.keep is False
    assert ns.restart is False
