from __future__ import annotations

import pytest
from tesseract.mirror.server.app import reattach_operator_panes


class _Rec:
    def __init__(self, sid, origin):
        self.session_id, self.origin = sid, origin


@pytest.mark.asyncio
async def test_only_operator_facing_panes_reopen():
    opened = []

    async def fake_pty(action, payload):
        opened.append((action, payload))

    await reattach_operator_panes(
        list_fn=lambda: [_Rec("s1", "mirror"), _Rec("s2", "autonomy"), _Rec("s3", "cli")],
        pty_open_fn=fake_pty,
    )
    cmds = [p["command"] for (a, p) in opened]
    assert cmds == [["tars", "--session", "s1"], ["tars", "--session", "s3"]]
    assert all(a == "open" for (a, p) in opened)
    assert all("end_of_turn_mode" not in p for (a, p) in opened)
    assert all(p["name"] == f"ctrl-{p['command'][2]}" for (a, p) in opened)


@pytest.mark.asyncio
async def test_per_pane_error_does_not_stop_reattach():
    """Design (a): per-pane errors are swallowed; remaining panes still open."""
    opened = []
    call_count = 0

    async def flaky_pty(action, payload):
        nonlocal call_count
        call_count += 1
        # The second call (s-middle) raises; s1 and s3 must still succeed.
        if call_count == 2:
            raise RuntimeError("pty open failed for s-mid")
        opened.append((action, payload))

    # s1=mirror, s-mid=mirror (will raise), s3=cli — s1 and s3 should appear.
    await reattach_operator_panes(
        list_fn=lambda: [_Rec("s1", "mirror"), _Rec("s-mid", "mirror"), _Rec("s3", "cli")],
        pty_open_fn=flaky_pty,
    )
    ids = [p["command"][2] for (_, p) in opened]
    assert ids == ["s1", "s3"], f"expected s1+s3 only, got {ids}"


@pytest.mark.asyncio
async def test_empty_sessions_no_error():
    """No sessions → no calls, no exception."""
    async def fake_pty(action, payload):
        raise AssertionError("should not be called")

    await reattach_operator_panes(list_fn=lambda: [], pty_open_fn=fake_pty)


@pytest.mark.asyncio
async def test_background_only_no_panes():
    """Only background-origin sessions → nothing opened."""
    opened = []

    async def fake_pty(action, payload):
        opened.append((action, payload))

    await reattach_operator_panes(
        list_fn=lambda: [_Rec("bg1", "autonomy"), _Rec("bg2", "scheduler")],
        pty_open_fn=fake_pty,
    )
    assert opened == []
