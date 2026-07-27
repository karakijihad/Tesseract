"""Phase 4 — governor_pause_* and governor_tick WS envelopes.

PauseStore.add / .remove now fire a sync broadcast hook so source pauses
surface on the operator's Autonomy tab immediately. Governor.run_once
fires a tick broadcast at the end of every detector pass so the tab
clock + recent-pauses list refresh on cadence without polling.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tesseract.orchestrator.autonomy.governor import (
    Governor,
    GovernorConfig,
    PauseStore,
)
from tesseract.orchestrator.autonomy.models import AgendaSource


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import tesseract.paths
    importlib.reload(tesseract.paths)
    return tmp_path


def test_pause_add_fires_broadcast() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _hook(event_type: str, payload: dict[str, Any]) -> None:
        calls.append((event_type, payload))

    store = PauseStore(broadcast_hook=_hook)
    store.add(AgendaSource.PROVIDER_WATCH, detector="loop", reason="three_in_window")

    assert len(calls) == 1
    event_type, payload = calls[0]
    assert event_type == "governor_pause_added"
    assert payload["source"] == "provider_watch"
    assert payload["detector"] == "loop"
    assert payload["reason"] == "three_in_window"


def test_pause_add_idempotent_second_add_no_broadcast() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _hook(event_type: str, payload: dict[str, Any]) -> None:
        calls.append((event_type, payload))

    store = PauseStore(broadcast_hook=_hook)
    store.add(AgendaSource.PROVIDER_WATCH, detector="loop", reason="first")
    store.add(AgendaSource.PROVIDER_WATCH, detector="loop", reason="duplicate")

    # Second add returns None and must not fire a broadcast.
    assert len(calls) == 1


def test_pause_remove_fires_broadcast() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _hook(event_type: str, payload: dict[str, Any]) -> None:
        calls.append((event_type, payload))

    store = PauseStore(broadcast_hook=_hook)
    store.add(AgendaSource.PROVIDER_WATCH, detector="loop", reason="x")
    calls.clear()

    store.remove(AgendaSource.PROVIDER_WATCH, by="operator", reason="all_clear")
    assert len(calls) == 1
    event_type, payload = calls[0]
    assert event_type == "governor_pause_removed"
    assert payload["source"] == "provider_watch"
    assert payload["by"] == "operator"


def test_pause_remove_missing_source_no_broadcast() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _hook(event_type: str, payload: dict[str, Any]) -> None:
        calls.append((event_type, payload))

    store = PauseStore(broadcast_hook=_hook)
    # remove() against a source that isn't paused returns None and must
    # not broadcast (no state change).
    store.remove(AgendaSource.PROVIDER_WATCH, by="operator", reason="noop")
    assert calls == []


def test_set_broadcast_hook_after_construction() -> None:
    calls: list[str] = []

    def _hook(event_type: str, payload: dict[str, Any]) -> None:
        calls.append(event_type)

    store = PauseStore()  # no hook
    store.add(AgendaSource.PROVIDER_WATCH, detector="loop", reason="silent")
    assert calls == []

    store.set_broadcast_hook(_hook)
    store.remove(AgendaSource.PROVIDER_WATCH, reason="now_visible")
    assert calls == ["governor_pause_removed"]


def test_hook_exception_does_not_break_mutation() -> None:
    def _bad_hook(event_type: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("hook intentionally raises")

    store = PauseStore(broadcast_hook=_bad_hook)
    # Must not propagate.
    pause = store.add(AgendaSource.PROVIDER_WATCH, detector="loop", reason="x")
    assert pause is not None
    assert store.is_paused(AgendaSource.PROVIDER_WATCH)


@pytest.mark.asyncio
async def test_governor_run_once_fires_tick_broadcast() -> None:
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore

    calls: list[dict[str, Any]] = []

    def _hook(payload: dict[str, Any]) -> None:
        calls.append(payload)

    pause_store = PauseStore()
    governor = Governor(
        agenda_store=AgendaStore(),
        pause_store=pause_store,
        config=GovernorConfig(),
    )
    governor.set_tick_broadcast_hook(_hook)

    await governor.run_once()

    # Idle ticks still broadcast — the operator's clock needs a heartbeat
    # even on no-change passes.
    assert len(calls) == 1
    assert "at" in calls[0]
    assert calls[0]["pauses_added"] == []


@pytest.mark.asyncio
async def test_governor_tick_hook_exception_does_not_break_run() -> None:
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore

    def _bad_hook(payload: dict[str, Any]) -> None:
        raise RuntimeError("tick hook intentionally raises")

    governor = Governor(
        agenda_store=AgendaStore(),
        pause_store=PauseStore(),
        config=GovernorConfig(),
    )
    governor.set_tick_broadcast_hook(_bad_hook)

    # Must complete without raising even though the hook explodes.
    result = await governor.run_once()
    assert result is not None
