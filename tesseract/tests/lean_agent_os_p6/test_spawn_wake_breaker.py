"""P6 Task 2 §G1 — global `spawn-wake` circuit breaker.

Design: Docs/Plan/lean-agent-os/idle-wake-design.md §G1. Uses the existing
`tesseract/context/circuit_breaker.py` class (threshold sourced from
`providers.yaml::availability.max_consecutive_failures`, never a literal).

Check point: `on_spawn_complete` — breaker open → skip the wake (floor still
runs). Accounting: `_wake_turn` — exception from the turn driver →
record_failure; clean completion → record_success (reset).

Fakes only — no live brain/turn-driver. `TESSERACT_HOME` monkeypatched to
`tmp_path` BEFORE the breaker singleton is (re)constructed, so nothing
lands under `tesseract/logs/`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.mirror.server import spawn_wake


@pytest.fixture(autouse=True)
def _isolated_breaker(tmp_path: Path, monkeypatch):
    """Point the process-global spawn-wake breaker at a fresh tmp dir and
    reset the singleton so each test starts with a closed breaker."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    monkeypatch.setattr(spawn_wake, "_wake_breaker", None)
    yield
    monkeypatch.setattr(spawn_wake, "_wake_breaker", None)


class _Task:
    def __init__(self, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


class _Session:
    def __init__(self) -> None:
        self.session_id = "sess-breaker-test"
        self.current_turn_tasks: dict = {}
        self.spawn_wake_pending: set[str] = set()
        self.chats: dict = {}


class _Handle:
    handle_id = "del-claude-1"
    kind = "delegate_claude"

    def status(self) -> str:
        return "done"


class _CS:
    def __init__(self, pending_after: bool = False) -> None:
        self._pending = pending_after

    def has_pending_spawn_completions(self) -> bool:
        return self._pending


@pytest.fixture
def scheduled(monkeypatch):
    """Record schedule_wake(chat_id) calls instead of spawning a real turn."""
    calls: list[str] = []
    monkeypatch.setattr(
        spawn_wake, "schedule_wake",
        lambda app, session, chat_id: calls.append(chat_id),
    )
    return calls


# --- G1 check point: on_spawn_complete gates on breaker.is_tripped -------


def test_breaker_open_skips_wake_but_floor_still_runs(scheduled) -> None:
    breaker = spawn_wake._get_wake_breaker()
    breaker.record_failure("boom-1")
    breaker.record_failure("boom-2")
    breaker.record_failure("boom-3")
    assert breaker.is_tripped is True

    session = _Session()
    ingested: list = []
    spawn_wake.on_spawn_complete(
        None, session, cs=object(), chat_id="A", handle=_Handle(),
        floor=ingested.append,
    )

    assert len(ingested) == 1          # floor always runs
    assert scheduled == []             # breaker open → no wake scheduled
    assert "A" not in session.spawn_wake_pending  # no stale pending flag


def test_breaker_closed_schedules_wake_normally(scheduled) -> None:
    session = _Session()
    spawn_wake.on_spawn_complete(
        None, session, cs=object(), chat_id="A", handle=_Handle(),
        floor=lambda h: None,
    )
    assert scheduled == ["A"]


# --- accounting: _wake_turn exception → record_failure -------------------


@pytest.mark.asyncio
async def test_wake_turn_exception_increments_breaker_failure(monkeypatch) -> None:
    session = _Session()
    session.chats["A"] = _CS(pending_after=False)

    async def _boom(app, sess, text, *, chat_id, **kwargs):
        raise RuntimeError("adapter exploded")

    monkeypatch.setattr("tesseract.mirror.server.turn_runner._run_chat_turn", _boom)

    with pytest.raises(RuntimeError):
        await spawn_wake._wake_turn(None, session, "A")

    breaker = spawn_wake._get_wake_breaker()
    assert breaker.failure_count == 1
    assert breaker.is_tripped is False


@pytest.mark.asyncio
async def test_third_consecutive_wake_failure_trips_breaker(monkeypatch) -> None:
    session = _Session()
    session.chats["A"] = _CS(pending_after=False)

    async def _boom(app, sess, text, *, chat_id, **kwargs):
        raise RuntimeError("adapter exploded")

    monkeypatch.setattr("tesseract.mirror.server.turn_runner._run_chat_turn", _boom)

    for _ in range(3):
        session.spawn_wake_pending.add("A")
        with pytest.raises(RuntimeError):
            await spawn_wake._wake_turn(None, session, "A")

    breaker = spawn_wake._get_wake_breaker()
    assert breaker.failure_count == 3
    assert breaker.is_tripped is True


# --- accounting: clean wake → record_success (reset) ----------------------


@pytest.mark.asyncio
async def test_clean_wake_resets_breaker(monkeypatch) -> None:
    breaker = spawn_wake._get_wake_breaker()
    breaker.record_failure("boom-1")
    breaker.record_failure("boom-2")
    assert breaker.failure_count == 2

    session = _Session()
    session.chats["A"] = _CS(pending_after=False)

    async def _ok(app, sess, text, *, chat_id, **kwargs):
        return None

    monkeypatch.setattr("tesseract.mirror.server.turn_runner._run_chat_turn", _ok)

    await spawn_wake._wake_turn(None, session, "A")

    breaker = spawn_wake._get_wake_breaker()
    assert breaker.failure_count == 0
    assert breaker.is_tripped is False


# --- fix pass 1: outcome-based accounting (both drivers can swallow an ----
# --- ordinary turn failure internally without raising) --------------------


@pytest.mark.asyncio
async def test_cockpit_wake_turn_swallowed_error_increments_breaker(monkeypatch) -> None:
    """``ws._run_turn`` catches Exception/CancelledError internally and
    emits a ``stream_error`` envelope instead of re-raising (ws.py
    ~1440-1449), so the pre-fix except-Exception-only accounting never saw
    this failure. The real ``_run_chat_turn``/``_run_turn`` now accept an
    optional ``outcome`` dict populated with ``{"ok": stream_ok}`` — this
    fake mirrors a swallowed failure (no exception, ``ok=False``)."""
    session = _Session()
    session.chats["A"] = _CS(pending_after=False)

    async def _swallowed_error(app, sess, text, *, chat_id, outcome=None, **kwargs):
        if outcome is not None:
            outcome["ok"] = False
        return None

    monkeypatch.setattr("tesseract.mirror.server.turn_runner._run_chat_turn", _swallowed_error)

    await spawn_wake._wake_turn(None, session, "A")

    breaker = spawn_wake._get_wake_breaker()
    assert breaker.failure_count == 1
    assert breaker.is_tripped is False


@pytest.mark.asyncio
async def test_channel_wake_turn_error_outcome_increments_breaker() -> None:
    """Channel turn drivers (Telegram's ``_wake_turn_driver``) return
    ``str | None`` — a non-``None`` string is a turn-level error observed
    from ``_start_channel_turn``'s ``error_out`` without an exception ever
    propagating (ws.py's broad except in ``_drive`` swallows it into
    ``error_holder``)."""
    session = _Session()
    session.chats["A"] = _CS(pending_after=False)

    async def _channel_driver_with_error(app, sess, chat_id):
        return "channel turn crashed: RuntimeError: boom"

    await spawn_wake._wake_turn(None, session, "A", _channel_driver_with_error)

    breaker = spawn_wake._get_wake_breaker()
    assert breaker.failure_count == 1
    assert breaker.is_tripped is False


@pytest.mark.asyncio
async def test_channel_wake_turn_clean_outcome_resets_breaker() -> None:
    breaker = spawn_wake._get_wake_breaker()
    breaker.record_failure("boom-1")
    assert breaker.failure_count == 1

    session = _Session()
    session.chats["A"] = _CS(pending_after=False)

    async def _channel_driver_clean(app, sess, chat_id):
        return None

    await spawn_wake._wake_turn(None, session, "A", _channel_driver_clean)

    breaker = spawn_wake._get_wake_breaker()
    assert breaker.failure_count == 0
    assert breaker.is_tripped is False


# --- fix pass 2: cancelled cockpit wake turn is neutral (2026-07-06) ------


@pytest.mark.asyncio
async def test_cancelled_cockpit_wake_turn_leaves_breaker_untouched(monkeypatch) -> None:
    """A Stop-button cancel during a cockpit wake turn must count as neither
    a failure nor a success — the breaker's failure_count must be unchanged
    from before the call (see idle-wake-design.md §G1 fix pass 2)."""
    session = _Session()
    session.chats["A"] = _CS(pending_after=False)

    async def _cancelled(app, sess, text, *, chat_id, outcome=None, **kwargs):
        if outcome is not None:
            outcome["ok"] = False
            outcome["cancelled"] = True
        return None

    monkeypatch.setattr("tesseract.mirror.server.turn_runner._run_chat_turn", _cancelled)

    breaker_before = spawn_wake._get_wake_breaker()
    breaker_before.record_failure("boom-1")
    assert breaker_before.failure_count == 1

    await spawn_wake._wake_turn(None, session, "A")

    breaker = spawn_wake._get_wake_breaker()
    assert breaker.failure_count == 1  # untouched — neither incremented nor reset
    assert breaker.is_tripped is False


@pytest.mark.asyncio
async def test_cancelled_cockpit_wake_turn_never_trips_breaker_on_repeat(monkeypatch) -> None:
    """Repeated operator cancellations of wakes must never trip the breaker
    (neutral, not a failure) — three cancellations in a row stay at zero."""
    session = _Session()
    session.chats["A"] = _CS(pending_after=False)

    async def _cancelled(app, sess, text, *, chat_id, outcome=None, **kwargs):
        if outcome is not None:
            outcome["ok"] = False
            outcome["cancelled"] = True
        return None

    monkeypatch.setattr("tesseract.mirror.server.turn_runner._run_chat_turn", _cancelled)

    for _ in range(3):
        session.spawn_wake_pending.add("A")
        await spawn_wake._wake_turn(None, session, "A")

    breaker = spawn_wake._get_wake_breaker()
    assert breaker.failure_count == 0
    assert breaker.is_tripped is False


# --- persistence: rehydrates from logs/circuit-breakers/spawn-wake.jsonl --


def test_breaker_persists_trip_under_configured_log_dir(tmp_path: Path) -> None:
    breaker = spawn_wake._get_wake_breaker()
    breaker.record_failure("boom-1")
    breaker.record_failure("boom-2")
    breaker.record_failure("boom-3")
    assert breaker.is_tripped is True

    log_path = tmp_path / "logs" / "circuit-breakers" / "spawn-wake.jsonl"
    assert log_path.exists()
    assert '"tripped"' in log_path.read_text(encoding="utf-8")
