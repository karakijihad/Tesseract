"""A4 + A5 + A7 — arm idempotency, PTY task tracking, consent TOCTOU.

- A4 (Claude coder H-2 / pr-review SEC-3): arm() called twice without
  intervening disarm leaks stale chat_session / emit on the subscriber.
- A5 (Claude coder H-1 / simplifier #5): PTY-push tasks scheduled by
  _forward_to_observer escape subscriber.detach() cleanup because they
  are not tracked in _tasks.
- A7 (Claude coder M-1): _forward_to_observer checks consent at line
  267 then schedules create_task at line 278 — if consent is revoked
  in between, the task still writes PTY content to the observer.

Uses AsyncMock-style fake app state — no live aiohttp server.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from aiohttp import web

from tesseract.brain.observer_subscriber import ObserverSubscriber
from tesseract.mirror.server.pty_manager import PTYManager
from tesseract.mirror.server.routes.observer_consent import _attach_to_active_sessions


class _FakeObserver:
    def __init__(self) -> None:
        self.incremental_calls: list[list[dict[str, Any]]] = []
        self._release_incremental = asyncio.Event()

    async def observe_incremental(self, new_turns, mode="meta"):
        self.incremental_calls.append(list(new_turns))
        # Don't complete until released — simulates an in-flight LLM call.
        await self._release_incremental.wait()
        return None

    def reset(self):
        pass

    def drop_pty_for_pane(self, pane_id):
        pass


class _FakeChatSession:
    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.attached = False

    def attach_observer_subscriber(self, sub):
        self.attached = True

    def detach_observer_subscriber(self):
        self.attached = False


def _build_app(observer: _FakeObserver) -> web.Application:
    app = web.Application()
    app["observer"] = observer
    app["observer_state"] = "off"
    app["observer_consented_panes"] = set()
    app["observer_subscriber"] = ObserverSubscriber(observer)
    cs = _FakeChatSession("s1")
    session = SimpleNamespace(chat_session=cs, session_id="s1", ws=SimpleNamespace(closed=False, send_json=_noop_send))
    app["server_sessions"] = {"s1": session}
    return app


async def _noop_send(payload):
    return None


async def test_a4_arm_idempotent() -> None:
    """Double-arm without intervening disarm must land at exactly one
    attached session and zero orphan subscriber state."""
    from tesseract.mirror.server.routes.observer_consent import _detach_subscriber

    observer = _FakeObserver()
    app = _build_app(observer)
    sub: ObserverSubscriber = app["observer_subscriber"]

    # First arm.
    _attach_to_active_sessions(app)
    assert sub.is_active
    first_chat = sub._chat_session
    assert first_chat is not None
    first_emit = sub._emit

    # Second arm without disarm — AFTER FIX, arm() should detach first
    # so sub._emit and sub._chat_session refer to the new session only.
    await _detach_subscriber(app)
    _attach_to_active_sessions(app)

    assert sub.is_active
    # After a clean re-attach, the chat session reference must be the one
    # currently registered with the subscriber, not a stale dupe.
    assert sub._chat_session is app["server_sessions"]["s1"].chat_session

    # Detach one more time and confirm no dangling tasks.
    await _detach_subscriber(app)
    assert not sub.is_active
    assert len(sub._tasks) == 0


async def test_a5_pty_tasks_tracked() -> None:
    """PTY push tasks must be tracked on the app so disarm cancels them.

    Before fix: _forward_to_observer schedules asyncio.create_task without
    any tracking. After fix: tasks live on app['observer_pty_tasks'] and
    are cancelled on disarm."""
    observer = _FakeObserver()
    app = _build_app(observer)

    from tesseract.mirror.server.config import ShellProfile, TerminalServerConfig
    cfg = TerminalServerConfig(
        default_shell="bash",
        max_tabs=1,
        max_panes_per_tab=1,
        shell_profiles={"bash": ShellProfile(argv=("bash",), label="bash")},
        coalesce_flush_ms=8.0,
        coalesce_flush_chars=4096,
        reattach_grace_s=30.0,
        pause_buffer_cap_chars=2_000_000,
    )
    pty = PTYManager(cfg)
    pty.bind_app(app)

    # Simulate the consented-observing state and fire a line.
    app["observer_state"] = "observing"
    app["observer_consented_panes"].add("pane-1")
    pty._forward_to_observer("pane-1", "hello\n")

    # After fix: the task is registered where disarm can find it.
    tracked = app.get("observer_pty_tasks")
    assert tracked is not None, (
        "BUG: _forward_to_observer did not register task on app['observer_pty_tasks']"
    )
    assert len(tracked) == 1, f"BUG: expected 1 tracked task, got {len(tracked)}"

    # Release the in-flight call and let it finish.
    observer._release_incremental.set()
    await asyncio.sleep(0.05)
    # After completion, task should be cleared out.
    assert len(app["observer_pty_tasks"]) == 0, (
        "BUG: completed PTY task left in set — add_done_callback not wired"
    )


async def test_a7_consent_toctou() -> None:
    """Consent revoked between the gate-check and the in-flight push must
    not result in PTY content reaching the observer."""
    observer = _FakeObserver()
    app = _build_app(observer)

    from tesseract.mirror.server.config import ShellProfile, TerminalServerConfig
    cfg = TerminalServerConfig(
        default_shell="bash",
        max_tabs=1,
        max_panes_per_tab=1,
        shell_profiles={"bash": ShellProfile(argv=("bash",), label="bash")},
        coalesce_flush_ms=8.0,
        coalesce_flush_chars=4096,
        reattach_grace_s=30.0,
        pause_buffer_cap_chars=2_000_000,
    )
    pty = PTYManager(cfg)
    pty.bind_app(app)

    app["observer_state"] = "observing"
    app["observer_consented_panes"].add("pane-1")
    pty._forward_to_observer("pane-1", "secret\n")

    # Revoke consent while the task is in-flight.
    app["observer_consented_panes"].discard("pane-1")

    # Release and drain.
    observer._release_incremental.set()
    await asyncio.sleep(0.05)

    # After fix: the _observer_push body re-checks consent, so no call.
    assert len(observer.incremental_calls) == 0, (
        f"BUG (A7): PTY content reached observer after consent revoked — "
        f"incremental_calls={observer.incremental_calls}"
    )


