# tesseract/tests/fix_pass_tars_conductor/test_lane_tool_proxy_wiring.py
"""Task 3 — verify that lane_* tools handle BOTH sync manager (real in-process
LaneManager / NamedLaneManager) and async proxy (IpcLaneManager / IpcNamedLaneManager)
via the maybe_await guard in tool_support.py."""

import pytest

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.lane_list import LaneListInput, LaneListTool
from tesseract.kernel.tools.lane_named_get import LaneNamedGetInput, LaneNamedGetTool
from tesseract.kernel.tools.lane_named_list import LaneNamedListInput, LaneNamedListTool
from tesseract.kernel.tools.lane_read import LaneReadInput, LaneReadTool
from tesseract.kernel.tools.lane_status import LaneStatusInput, LaneStatusTool
from tesseract.orchestrator.tars_controller.lanes.models import (
    LaneEvent,
    LaneStatus,
)
from tesseract.orchestrator.tars_controller.lanes.named import NamedLaneRecord


# ---------------------------------------------------------------------------
# Async manager fakes (simulate IPC proxy — all methods are coroutines)
# ---------------------------------------------------------------------------


class _AsyncLaneMgr:
    async def read(self, lane_id, since_cursor=None):
        event = LaneEvent.model_validate(
            {
                "lane_id": lane_id,
                "kind": "assistant_text",
                "payload": {"text": "hi"},
                "at_utc": "2026-01-01T00:00:00Z",
                "cursor": "1",
            }
        )
        return [event], "1"

    async def status(self, lane_id):
        return LaneStatus(
            alive=True,
            busy=False,
            queue_depth=0,
            lifecycle="ready",
            last_activity_utc="2026-01-01T00:00:00Z",
        )

    async def list_ids(self):
        return ["lane-claude-x"]


class _AsyncNamedMgr:
    async def get(self, name):
        return NamedLaneRecord(
            name=name,
            lane_id="lane-claude-x",
            kind="claude",
            mode="headless",
            model="test-model",
            working_dir="/tmp",
        )

    async def list(self):
        return [
            NamedLaneRecord(
                name="coder/claude",
                lane_id="lane-claude-x",
                kind="claude",
                mode="headless",
                model="test-model",
                working_dir="/tmp",
            )
        ]


# ---------------------------------------------------------------------------
# Sync manager fakes (real in-process path — methods return values directly)
# ---------------------------------------------------------------------------


class _SyncLaneMgr:
    def read(self, lane_id, since_cursor=None):
        event = LaneEvent.model_validate(
            {
                "lane_id": lane_id,
                "kind": "tool_result",
                "payload": {"content": "done"},
                "at_utc": "2026-01-01T00:00:00Z",
                "cursor": "2",
            }
        )
        return [event], "2"

    def status(self, lane_id):
        return LaneStatus(
            alive=True,
            busy=True,
            queue_depth=1,
            lifecycle="busy",
            last_activity_utc="2026-01-01T00:00:00Z",
        )

    def list_ids(self):
        return ["lane-codex-y"]


class _SyncNamedMgr:
    def get(self, name):
        return NamedLaneRecord(
            name=name,
            lane_id="lane-codex-y",
            kind="codex",
            mode="headless",
            model="test-codex",
            working_dir="/tmp",
        )

    def list(self):
        return [
            NamedLaneRecord(
                name="auditor/codex",
                lane_id="lane-codex-y",
                kind="codex",
                mode="headless",
                model="test-codex",
                working_dir="/tmp",
            )
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lane_ctx(tmp_path, mgr):
    return ToolContext(
        workspace_root=str(tmp_path),
        session_id="test-lane-proxy-wiring",
        lane_manager_provider=lambda: mgr,
    )


def _named_ctx(tmp_path, mgr):
    return ToolContext(
        workspace_root=str(tmp_path),
        session_id="test-lane-proxy-wiring",
        named_lane_manager_provider=lambda: mgr,
    )


# ---------------------------------------------------------------------------
# lane_read — async proxy
# ---------------------------------------------------------------------------


async def test_lane_read_awaits_async_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    ctx = _lane_ctx(tmp_path, _AsyncLaneMgr())
    res = await LaneReadTool().run(LaneReadInput(lane_id="lane-claude-x"), ctx)
    assert not res.is_error
    assert res.metadata.get("count") == 1
    assert res.metadata["events"][0]["kind"] == "assistant_text"
    assert res.metadata["events"][0]["payload"]["text"] == "hi"


# ---------------------------------------------------------------------------
# lane_read — sync manager (maybe_await passes non-awaitables through)
# ---------------------------------------------------------------------------


async def test_lane_read_sync_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    ctx = _lane_ctx(tmp_path, _SyncLaneMgr())
    res = await LaneReadTool().run(LaneReadInput(lane_id="lane-codex-y"), ctx)
    assert not res.is_error
    assert res.metadata.get("count") == 1
    assert res.metadata["next_cursor"] == "2"


# ---------------------------------------------------------------------------
# lane_status — async proxy + sync manager
# ---------------------------------------------------------------------------


async def test_lane_status_awaits_async_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    ctx = _lane_ctx(tmp_path, _AsyncLaneMgr())
    res = await LaneStatusTool().run(LaneStatusInput(lane_id="lane-claude-x"), ctx)
    assert not res.is_error
    assert res.metadata["alive"] is True
    assert res.metadata["lifecycle"] == "ready"


async def test_lane_status_sync_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    ctx = _lane_ctx(tmp_path, _SyncLaneMgr())
    res = await LaneStatusTool().run(LaneStatusInput(lane_id="lane-codex-y"), ctx)
    assert not res.is_error
    assert res.metadata["busy"] is True


# ---------------------------------------------------------------------------
# lane_list — async proxy + sync manager
# ---------------------------------------------------------------------------


async def test_lane_list_awaits_async_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    ctx = _lane_ctx(tmp_path, _AsyncLaneMgr())
    res = await LaneListTool().run(LaneListInput(), ctx)
    assert not res.is_error
    assert res.metadata["count"] == 1
    assert "lane-claude-x" in res.metadata["ids"]


async def test_lane_list_sync_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    ctx = _lane_ctx(tmp_path, _SyncLaneMgr())
    res = await LaneListTool().run(LaneListInput(), ctx)
    assert not res.is_error
    assert "lane-codex-y" in res.metadata["ids"]


# ---------------------------------------------------------------------------
# lane_named_get — async proxy + sync manager
# ---------------------------------------------------------------------------


async def test_lane_named_get_awaits_async_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    ctx = _named_ctx(tmp_path, _AsyncNamedMgr())
    res = await LaneNamedGetTool().run(LaneNamedGetInput(name="coder/claude"), ctx)
    assert not res.is_error
    assert res.metadata["bound"] is True
    assert res.metadata["lane_id"] == "lane-claude-x"


async def test_lane_named_get_sync_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    ctx = _named_ctx(tmp_path, _SyncNamedMgr())
    res = await LaneNamedGetTool().run(LaneNamedGetInput(name="auditor/codex"), ctx)
    assert not res.is_error
    assert res.metadata["kind"] == "codex"


# ---------------------------------------------------------------------------
# lane_named_list — async proxy + sync manager
# ---------------------------------------------------------------------------


async def test_lane_named_list_awaits_async_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    ctx = _named_ctx(tmp_path, _AsyncNamedMgr())
    res = await LaneNamedListTool().run(LaneNamedListInput(), ctx)
    assert not res.is_error
    assert res.metadata["count"] == 1
    assert res.metadata["records"][0]["name"] == "coder/claude"


async def test_lane_named_list_sync_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    ctx = _named_ctx(tmp_path, _SyncNamedMgr())
    res = await LaneNamedListTool().run(LaneNamedListInput(), ctx)
    assert not res.is_error
    assert res.metadata["records"][0]["name"] == "auditor/codex"


# ---------------------------------------------------------------------------
# Task 4 — Mirror session wiring: _lane_manager_provider / _named_lane_manager_provider
# ---------------------------------------------------------------------------


def test_mirror_lane_provider_returns_ipc_proxy():
    from tesseract.orchestrator.tars_controller.lanes.ipc_proxy import IpcLaneManager
    from tesseract.mirror.server.session import _lane_manager_provider
    assert isinstance(_lane_manager_provider(), IpcLaneManager)


def test_mirror_named_lane_provider_returns_ipc_proxy():
    from tesseract.orchestrator.tars_controller.lanes.ipc_proxy import IpcNamedLaneManager
    from tesseract.mirror.server.session import _named_lane_manager_provider
    assert isinstance(_named_lane_manager_provider(), IpcNamedLaneManager)
