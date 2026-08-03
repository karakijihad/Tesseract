"""Daemon-side ``lane.*`` IPC handlers (X-4 Session C).

Extracted from ``daemon.py`` (lane-cleanup Batch 4) as a mixin so the
controller daemon stays a dispatch + lifecycle shell. ``ControllerDaemon``
inherits this mixin; every method below resolves ``self._lane_manager`` and
the shared ``self._push_lane_result`` / ``self._push_unwired`` helpers via the
MRO. The mixin holds no state of its own.

Runtime has no dependency on ``daemon`` — the only references to its types
(``_ClientConn``, the ``Lane*Message`` protocol types) are annotations, kept
as strings by ``from __future__ import annotations`` and imported under
``TYPE_CHECKING`` only, so there is no import cycle with ``daemon``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .daemon import _ClientConn
    from .protocol import (
        LaneAttachMessage,
        LaneCloseMessage,
        LaneInterruptMessage,
        LaneListMessage,
        LaneOpenMessage,
        LaneReadMessage,
        LaneSendMessage,
        LaneStatusMessage,
    )


class _LaneHandlersMixin:
    """The seven ``lane.*`` verb handlers. Mixed into ``ControllerDaemon``."""

    async def _on_lane_open(
        self, conn: _ClientConn, msg: LaneOpenMessage
    ) -> None:
        manager = self._lane_manager
        if manager is None:
            await self._push_unwired(conn, msg.request_id, "open")
            return
        try:
            lane_id = await manager.open(
                kind=msg.kind,
                mode=msg.mode,
                model=msg.model,
                working_dir=msg.working_dir,
                env=msg.env,
                read_only=msg.read_only,
            )
        except Exception as exc:  # noqa: BLE001 — surface as result error
            await self._push_lane_result(
                conn,
                request_id=msg.request_id,
                verb="open",
                ok=False,
                error=str(exc),
            )
            return
        await self._push_lane_result(
            conn,
            request_id=msg.request_id,
            verb="open",
            ok=True,
            result={"lane_id": lane_id},
        )

    async def _on_lane_send(
        self, conn: _ClientConn, msg: LaneSendMessage
    ) -> None:
        manager = self._lane_manager
        if manager is None:
            await self._push_unwired(conn, msg.request_id, "send")
            return
        try:
            send_result = await manager.send(msg.lane_id, msg.message)
        except Exception as exc:  # noqa: BLE001
            # Root fix for the M6 class (Deferred 2026-07-12): a daemon
            # restart leaves disk-alive lanes detached — attach once and
            # retry here so EVERY client path recovers, not just the
            # kernel tools' own self-heals.
            if "not attached" not in str(exc):
                await self._push_lane_result(
                    conn,
                    request_id=msg.request_id,
                    verb="send",
                    ok=False,
                    error=str(exc),
                )
                return
            try:
                await manager.attach(msg.lane_id)
                send_result = await manager.send(msg.lane_id, msg.message)
            except Exception as retry_exc:  # noqa: BLE001
                await self._push_lane_result(
                    conn,
                    request_id=msg.request_id,
                    verb="send",
                    ok=False,
                    error=str(retry_exc),
                )
                return
        await self._push_lane_result(
            conn,
            request_id=msg.request_id,
            verb="send",
            ok=send_result.accepted,
            result=send_result.model_dump(mode="json"),
            error=None if send_result.accepted else send_result.reason,
        )

    async def _on_lane_read(
        self, conn: _ClientConn, msg: LaneReadMessage
    ) -> None:
        manager = self._lane_manager
        if manager is None:
            await self._push_unwired(conn, msg.request_id, "read")
            return
        try:
            events, next_cursor = manager.read(msg.lane_id, msg.since_cursor)
        except Exception as exc:  # noqa: BLE001
            await self._push_lane_result(
                conn,
                request_id=msg.request_id,
                verb="read",
                ok=False,
                error=str(exc),
            )
            return
        await self._push_lane_result(
            conn,
            request_id=msg.request_id,
            verb="read",
            ok=True,
            result={
                "events": [e.model_dump(mode="json") for e in events],
                "next_cursor": next_cursor,
                "count": len(events),
            },
        )

    async def _on_lane_status(
        self, conn: _ClientConn, msg: LaneStatusMessage
    ) -> None:
        manager = self._lane_manager
        if manager is None:
            await self._push_unwired(conn, msg.request_id, "status")
            return
        try:
            status = manager.status(msg.lane_id)
        except Exception as exc:  # noqa: BLE001
            await self._push_lane_result(
                conn,
                request_id=msg.request_id,
                verb="status",
                ok=False,
                error=str(exc),
            )
            return
        await self._push_lane_result(
            conn,
            request_id=msg.request_id,
            verb="status",
            ok=True,
            result=status.model_dump(mode="json"),
        )

    async def _on_lane_attach(
        self, conn: _ClientConn, msg: LaneAttachMessage
    ) -> None:
        manager = self._lane_manager
        if manager is None:
            await self._push_unwired(conn, msg.request_id, "attach")
            return
        try:
            snapshot = await manager.attach(msg.lane_id)
        except Exception as exc:  # noqa: BLE001
            await self._push_lane_result(
                conn,
                request_id=msg.request_id,
                verb="attach",
                ok=False,
                error=str(exc),
            )
            return
        await self._push_lane_result(
            conn,
            request_id=msg.request_id,
            verb="attach",
            ok=True,
            result=snapshot.model_dump(mode="json"),
        )

    async def _on_lane_close(
        self, conn: _ClientConn, msg: LaneCloseMessage
    ) -> None:
        manager = self._lane_manager
        if manager is None:
            await self._push_unwired(conn, msg.request_id, "close")
            return
        try:
            close_result = await manager.close(msg.lane_id, msg.reason)
        except Exception as exc:  # noqa: BLE001
            await self._push_lane_result(
                conn,
                request_id=msg.request_id,
                verb="close",
                ok=False,
                error=str(exc),
            )
            return
        await self._push_lane_result(
            conn,
            request_id=msg.request_id,
            verb="close",
            ok=True,
            result=close_result,
        )

    async def _on_lane_interrupt(
        self, conn: _ClientConn, msg: "LaneInterruptMessage"
    ) -> None:
        manager = self._lane_manager
        if manager is None:
            await self._push_unwired(conn, msg.request_id, "interrupt")
            return
        try:
            interrupted = await manager.interrupt(msg.lane_id)
        except Exception as exc:  # noqa: BLE001
            await self._push_lane_result(
                conn,
                request_id=msg.request_id,
                verb="interrupt",
                ok=False,
                error=str(exc),
            )
            return
        await self._push_lane_result(
            conn,
            request_id=msg.request_id,
            verb="interrupt",
            ok=True,
            result={"interrupted": bool(interrupted)},
        )

    async def _on_lane_list(
        self, conn: _ClientConn, msg: LaneListMessage
    ) -> None:
        manager = self._lane_manager
        if manager is None:
            await self._push_unwired(conn, msg.request_id, "list")
            return
        try:
            ids = manager.list_ids()
        except Exception as exc:  # noqa: BLE001
            await self._push_lane_result(
                conn,
                request_id=msg.request_id,
                verb="list",
                ok=False,
                error=str(exc),
            )
            return
        await self._push_lane_result(
            conn,
            request_id=msg.request_id,
            verb="list",
            ok=True,
            result={"ids": ids, "count": len(ids)},
        )
