"""Daemon-side ``lane_named_*`` IPC handlers (CV-1).

Extracted from ``daemon.py`` (lane-cleanup Batch 4) as a mixin. The named-lane
layer (``NamedLaneManager``) resolves a human name (``coder``/``claude``,
``auditor``/``codex``, …) to a live ``lane_id``, opening one on demand.
``ControllerDaemon`` inherits this mixin; each method resolves
``self._named_lane_manager`` and the shared ``self._push_lane_result`` /
``self._push_unwired`` helpers via the MRO.

As with ``lane_handlers``, references to ``daemon`` types are annotations only
(``TYPE_CHECKING`` + ``from __future__ import annotations``) — no import cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .daemon import _ClientConn
    from .protocol import (
        LaneNamedEnsureMessage,
        LaneNamedGetMessage,
        LaneNamedListMessage,
    )


class _NamedLaneHandlersMixin:
    """The three ``lane_named_*`` verb handlers. Mixed into ``ControllerDaemon``."""

    async def _on_lane_named_ensure(
        self, conn: _ClientConn, msg: LaneNamedEnsureMessage
    ) -> None:
        manager = self._named_lane_manager
        if manager is None:
            await self._push_unwired(
                conn, msg.request_id, "named_ensure",
                error="named_lane_manager_unwired",
            )
            return
        try:
            record = await manager.ensure(
                msg.name,
                kind=msg.kind,
                model=msg.model,
                working_dir=msg.working_dir,
                mode=msg.mode,
            )
        except Exception as exc:  # noqa: BLE001 — surface as result error
            await self._push_lane_result(
                conn,
                request_id=msg.request_id,
                verb="named_ensure",
                ok=False,
                error=str(exc),
            )
            return
        await self._push_lane_result(
            conn,
            request_id=msg.request_id,
            verb="named_ensure",
            ok=True,
            result=record.model_dump(mode="json"),
        )

    async def _on_lane_named_get(
        self, conn: _ClientConn, msg: LaneNamedGetMessage
    ) -> None:
        manager = self._named_lane_manager
        if manager is None:
            await self._push_unwired(
                conn, msg.request_id, "named_get",
                error="named_lane_manager_unwired",
            )
            return
        try:
            record = manager.get(msg.name)
        except Exception as exc:  # noqa: BLE001
            await self._push_lane_result(
                conn,
                request_id=msg.request_id,
                verb="named_get",
                ok=False,
                error=str(exc),
            )
            return
        await self._push_lane_result(
            conn,
            request_id=msg.request_id,
            verb="named_get",
            ok=True,
            result={"record": record.model_dump(mode="json") if record else None},
        )

    async def _on_lane_named_list(
        self, conn: _ClientConn, msg: LaneNamedListMessage
    ) -> None:
        manager = self._named_lane_manager
        if manager is None:
            await self._push_unwired(
                conn, msg.request_id, "named_list",
                error="named_lane_manager_unwired",
            )
            return
        try:
            records = manager.list()
        except Exception as exc:  # noqa: BLE001
            await self._push_lane_result(
                conn,
                request_id=msg.request_id,
                verb="named_list",
                ok=False,
                error=str(exc),
            )
            return
        await self._push_lane_result(
            conn,
            request_id=msg.request_id,
            verb="named_list",
            ok=True,
            result={"records": [r.model_dump(mode="json") for r in records]},
        )
