"""Fix 2: ControllerRuntime.reload() and drop_session() must schedule
close_all() on the evicted ChatSession's interactive_sessions registry,
to avoid leaking CLI subprocesses when sessions are cleared.
"""

from __future__ import annotations

import asyncio

import pytest


# ─── Fakes ────────────────────────────────────────────────────────────────────


class _FakeInteractiveSessions:
    def __init__(self) -> None:
        self.close_all_called = False

    async def close_all(self) -> None:
        self.close_all_called = True

    def list(self) -> list:
        return []


class _FakeChatSession:
    def __init__(self) -> None:
        self.interactive_sessions = _FakeInteractiveSessions()


# ─── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drop_session_schedules_close_all():
    """drop_session must schedule close_all on the evicted ChatSession's
    interactive_sessions so CLI subprocesses are not orphaned."""
    from tesseract.scripts.tars_controller import _schedule_close_all

    session = _FakeChatSession()
    reg = session.interactive_sessions

    # _schedule_close_all runs inside a running event loop
    _schedule_close_all([session])

    # Drain pending tasks so close_all actually runs
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert reg.close_all_called, "close_all must be scheduled and awaited"


@pytest.mark.asyncio
async def test_drop_session_multiple_sessions_schedules_all():
    """reload() evicts multiple sessions; close_all must fire for each."""
    from tesseract.scripts.tars_controller import _schedule_close_all

    sessions = [_FakeChatSession() for _ in range(3)]
    regs = [s.interactive_sessions for s in sessions]

    _schedule_close_all(sessions)

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    for i, reg in enumerate(regs):
        assert reg.close_all_called, f"session[{i}].close_all not called"


@pytest.mark.asyncio
async def test_schedule_close_all_tolerates_missing_attr():
    """Sessions without interactive_sessions attr must be silently skipped."""
    from tesseract.scripts.tars_controller import _schedule_close_all

    class _Bare:
        pass  # no interactive_sessions

    # Must not raise
    _schedule_close_all([_Bare()])
    await asyncio.sleep(0)
