# tesseract/tests/fix_pass_tars_conductor/test_ipc_proxy.py
import pytest
from tesseract.orchestrator.tars_controller.lanes.ipc_proxy import (
    IpcLaneManager,
    IpcNamedLaneManager,
)
from tesseract.orchestrator.tars_controller.lanes.models import LaneEvent, LaneStatus
from tesseract.orchestrator.tars_controller.lanes.named import NamedLaneRecord


class _FakeClient:
    def __init__(self): self.closed = False

    async def lane_open(self, *, kind, mode, model, working_dir, env=None):
        return f"lane-{kind}-fake"

    async def lane_read(self, lane_id, since_cursor=None):
        return {
            "events": [
                {
                    "lane_id": lane_id,
                    "kind": "assistant_text",
                    "payload": {"text": "hi"},
                    "at_utc": "2026-01-01T00:00:00Z",
                    "cursor": "1",
                }
            ],
            "next_cursor": "1",
            "count": 1,
        }

    async def lane_status(self, lane_id):
        return {
            "alive": True,
            "busy": False,
            "queue_depth": 0,
            "lifecycle": "ready",
            "last_activity_utc": "2026-01-01T00:00:00Z",
        }

    async def lane_list(self):
        return ["lane-claude-fake"]

    async def close(self): self.closed = True


@pytest.fixture
def proxy():
    fake = _FakeClient()

    async def _connect(): return fake

    return IpcLaneManager(connect_factory=_connect), fake


async def test_open_forwards_and_closes(proxy):
    mgr, fake = proxy
    lane_id = await mgr.open(
        kind="claude", mode="headless",
        model="test-model", working_dir="/tmp",
    )
    assert lane_id == "lane-claude-fake"
    assert fake.closed is True


async def test_read_reconstructs_typed_events(proxy):
    mgr, _ = proxy
    events, cursor = await mgr.read("lane-claude-fake")
    assert cursor == "1"
    assert isinstance(events[0], LaneEvent)
    assert events[0].kind == "assistant_text"


async def test_status_reconstructs_typed(proxy):
    mgr, _ = proxy
    st = await mgr.status("lane-claude-fake")
    assert isinstance(st, LaneStatus)
    assert st.alive is True


# ---------------------------------------------------------------------------
# IpcNamedLaneManager tests (Task 2)
# ---------------------------------------------------------------------------


class _FakeNamedClient:
    # Strict signature matching ControllerClient — no **kwargs, no env param —
    # so a forwarded env kwarg would immediately raise TypeError.
    async def lane_named_ensure(self, *, name, kind, model, working_dir, mode="headless"):
        return {
            "name": name,
            "lane_id": "lane-claude-x",
            "kind": kind,
            "model": model,
            "working_dir": working_dir,
            "mode": mode,
            "created_at_utc": "2026-01-01T00:00:00Z",
            "last_bound_at_utc": "2026-01-01T00:00:00Z",
        }

    async def lane_named_get(self, name):
        return {
            "name": name,
            "lane_id": "lane-claude-x",
            "kind": "claude",
            "model": "test-model",
            "working_dir": "/tmp",
            "mode": "headless",
            "created_at_utc": "2026-01-01T00:00:00Z",
            "last_bound_at_utc": "2026-01-01T00:00:00Z",
        }

    async def close(self):
        pass


async def test_named_get_reconstructs_record():
    fake = _FakeNamedClient()

    async def _c():
        return fake

    mgr = IpcNamedLaneManager(connect_factory=_c)
    rec = await mgr.get("coder")
    assert isinstance(rec, NamedLaneRecord)
    assert rec.name == "coder"


async def test_named_get_none_when_absent():
    class _NoneClient:
        async def lane_named_get(self, name):
            return None

        async def close(self):
            pass

    async def _c():
        return _NoneClient()

    mgr = IpcNamedLaneManager(connect_factory=_c)
    assert await mgr.get("missing") is None


async def test_named_ensure_accepts_env_but_does_not_forward_it():
    """env is accepted by IpcNamedLaneManager.ensure for signature parity but must NOT
    be forwarded to the client (ControllerClient.lane_named_ensure has no env param).
    The strict fake has no **kwargs, so a forwarded env would TypeError immediately."""
    fake = _FakeNamedClient()

    async def _c():
        return fake

    mgr = IpcNamedLaneManager(connect_factory=_c)
    rec = await mgr.ensure(
        "coder",
        kind="claude",
        model="test-model",
        working_dir="/tmp",
        env={"X": "1"},
    )
    assert isinstance(rec, NamedLaneRecord)
    assert rec.name == "coder"
