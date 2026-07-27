"""CV-1 — named-lane IPC message contract + daemon handler routing."""

from __future__ import annotations

import pytest

from tesseract.orchestrator.tars_controller.protocol import (
    LaneNamedEnsureMessage,
    LaneNamedGetMessage,
    LaneNamedListMessage,
    parse_client_message,
)


def test_named_ensure_message_parses():
    m = parse_client_message(
        {
            "msg": "lane_named_ensure",
            "request_id": "r1",
            "name": "coder/claude",
            "kind": "claude",
            "model": "claude-sonnet-4-6",
            "working_dir": ".",
        }
    )
    assert isinstance(m, LaneNamedEnsureMessage)
    assert m.name == "coder/claude"
    assert m.mode == "headless"  # default


def test_named_get_and_list_parse():
    g = parse_client_message({"msg": "lane_named_get", "request_id": "r2", "name": "auditor/codex"})
    assert isinstance(g, LaneNamedGetMessage)
    lst = parse_client_message({"msg": "lane_named_list", "request_id": "r3"})
    assert isinstance(lst, LaneNamedListMessage)


def test_named_ensure_rejects_bad_kind():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LaneNamedEnsureMessage(
            request_id="r", name="x", kind="gemini", model="m", working_dir="."
        )


@pytest.mark.asyncio
async def test_daemon_named_ensure_handler_routes_to_manager():
    # Exercise the daemon handler against a fake conn + fake NamedLaneManager,
    # asserting it pushes a LaneResultPush carrying the record.
    from tesseract.orchestrator.tars_controller.daemon import ControllerDaemon

    pushed: list[dict] = []

    class _FakeConn:
        class _Out:
            async def put(self, item):
                pushed.append(item)  # _push_lane_result already model_dumps

        outbound = _Out()

    class _FakeRecord:
        def model_dump(self, mode="json"):
            return {"name": "coder/claude", "lane_id": "lane-1", "kind": "claude"}

    class _FakeNamedManager:
        async def ensure(self, name, *, kind, model, working_dir, mode):
            assert name == "coder/claude"
            return _FakeRecord()

    daemon = ControllerDaemon(
        controller_id="c1", token="t", named_lane_manager=_FakeNamedManager()
    )
    msg = LaneNamedEnsureMessage(
        request_id="r9", name="coder/claude", kind="claude",
        model="claude-sonnet-4-6", working_dir=".",
    )
    await daemon._on_lane_named_ensure(_FakeConn(), msg)
    assert len(pushed) == 1
    assert pushed[0]["verb"] == "named_ensure"
    assert pushed[0]["ok"] is True
    assert pushed[0]["result"]["lane_id"] == "lane-1"


@pytest.mark.asyncio
async def test_daemon_named_unwired_errors_cleanly():
    from tesseract.orchestrator.tars_controller.daemon import ControllerDaemon

    pushed: list[dict] = []

    class _FakeConn:
        class _Out:
            async def put(self, item):
                pushed.append(item)  # _push_lane_result already model_dumps

        outbound = _Out()

    daemon = ControllerDaemon(controller_id="c1", token="t")  # no named manager
    await daemon._on_lane_named_get(
        _FakeConn(), LaneNamedGetMessage(request_id="r", name="x")
    )
    assert pushed[0]["ok"] is False
    assert pushed[0]["error"] == "named_lane_manager_unwired"
