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

from .lanes.principals import is_known_principal

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
        LaneTurnReadMessage,
    )


class _LaneHandlersMixin:
    """The seven ``lane.*`` verb handlers. Mixed into ``ControllerDaemon``."""

    async def _attested_caller(self, conn, msg, verb: str) -> str | None:
        """The message's caller principal, or None once a refusal is pushed.

        Enforcement lives here rather than in the Mirror because the daemon
        has its own loopback endpoint — Mirror-side filtering is usability,
        not a boundary. Two refusals, both fail-closed:

        - **Unattested.** A message naming no caller is not treated as the
          operator. Defaulting would make every message that forgot to carry
          the field an administrative one.
        - **Self-asserted.** A principal no MCP client is configured as is
          refused, so an identity cannot be acquired by spelling it.

        What the transport proves is narrower than it looks and is worth
        stating: the controller token proves *a gateway* is speaking, not
        which principal it speaks for. Attested-and-known is as far as this
        goes; a token holder that lies is out of scope by the phase's own
        framing — the goal is no accidental cross-client reach.
        """
        caller = (getattr(msg, "caller_principal", None) or "").strip()
        if not caller:
            await self._push_lane_result(
                conn,
                request_id=msg.request_id,
                verb=verb,
                ok=False,
                error=(
                    "unattested lane request: no caller principal. Every "
                    "lane.* message must carry the gateway-resolved MCP "
                    "client identity."
                ),
            )
            return None
        if not is_known_principal(caller):
            await self._push_lane_result(
                conn,
                request_id=msg.request_id,
                verb=verb,
                ok=False,
                error=(
                    f"unknown caller principal {caller!r}: no MCP client is "
                    f"configured under that name"
                ),
            )
            return None
        return caller

    async def _on_lane_open(
        self, conn: _ClientConn, msg: LaneOpenMessage
    ) -> None:
        manager = self._lane_manager
        if manager is None:
            await self._push_unwired(conn, msg.request_id, "open")
            return
        caller = await self._attested_caller(conn, msg, "open")
        if caller is None:
            return
        try:
            lane_id = await manager.open(
                kind=msg.kind,
                mode=msg.mode,
                model=msg.model,
                working_dir=msg.working_dir,
                env=msg.env,
                read_only=msg.read_only,
                owner_principal=caller,
                shared_with=msg.shared_with,
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
        caller = await self._attested_caller(conn, msg, "send")
        if caller is None:
            return
        try:
            send_result = await manager.send(
                msg.lane_id, msg.message, caller=caller
            )
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
                await manager.attach(msg.lane_id, caller=caller)
                send_result = await manager.send(
                    msg.lane_id, msg.message, caller=caller
                )
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

    async def _on_lane_turn_read(
        self, conn: _ClientConn, msg: LaneTurnReadMessage
    ) -> None:
        """Turn-scoped poll: the correlation rule runs here, once.

        The remote waiter polls this rather than the daemon holding a reply
        open for the length of a turn — an inline wait behind the lane_result
        ack timeout failed every lane turn longer than 30 s (2026-07-13)."""
        from .lanes.manager import LaneTurnNotFoundError
        from .lanes.turn_wait import TurnPoll

        manager = self._lane_manager
        if manager is None:
            await self._push_unwired(conn, msg.request_id, "turn_read")
            return
        caller = await self._attested_caller(conn, msg, "turn_read")
        if caller is None:
            return
        try:
            poll = manager.poll_turn(
                msg.lane_id, msg.turn_id, msg.since_cursor, caller=caller
            )
        except LaneTurnNotFoundError:
            # Carried as a value, not an error string: "this lane never
            # issued that turn" is a verdict the remote waiter has to act on
            # (fail closed), and parsing it back out of prose is how such
            # checks quietly stop firing.
            poll = TurnPoll(known=False)
        except Exception as exc:  # noqa: BLE001
            await self._push_lane_result(
                conn,
                request_id=msg.request_id,
                verb="turn_read",
                ok=False,
                error=str(exc),
            )
            return
        await self._push_lane_result(
            conn,
            request_id=msg.request_id,
            verb="turn_read",
            ok=True,
            result=poll.model_dump(mode="json"),
        )

    async def _on_lane_read(
        self, conn: _ClientConn, msg: LaneReadMessage
    ) -> None:
        manager = self._lane_manager
        if manager is None:
            await self._push_unwired(conn, msg.request_id, "read")
            return
        caller = await self._attested_caller(conn, msg, "read")
        if caller is None:
            return
        try:
            events, next_cursor = manager.read(
                msg.lane_id, msg.since_cursor, caller=caller
            )
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
        caller = await self._attested_caller(conn, msg, "status")
        if caller is None:
            return
        try:
            status = manager.status(msg.lane_id, caller=caller)
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
        caller = await self._attested_caller(conn, msg, "attach")
        if caller is None:
            return
        try:
            snapshot = await manager.attach(msg.lane_id, caller=caller)
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
        caller = await self._attested_caller(conn, msg, "close")
        if caller is None:
            return
        try:
            close_result = await manager.close(
                msg.lane_id, msg.reason, caller=caller
            )
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
        caller = await self._attested_caller(conn, msg, "interrupt")
        if caller is None:
            return
        try:
            interrupted = await manager.interrupt(
                msg.lane_id, msg.turn_id, caller=caller
            )
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
        caller = await self._attested_caller(conn, msg, "list")
        if caller is None:
            return
        try:
            ids = manager.list_ids(caller=caller)
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
