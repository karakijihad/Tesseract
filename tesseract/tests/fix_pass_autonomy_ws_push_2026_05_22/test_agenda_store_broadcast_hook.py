"""Phase 2 — AgendaStore.broadcast_hook fires on add/transition.

Closes the kernel-side gap: the autonomy kernel mutates agenda items via
its own ``AgendaStore`` instance (separate from the Mirror route's
instance). Those mutations were silent to WS subscribers. The store now
calls an optional broadcaster hook after every successful disk write so
the Mirror can fan ``agenda_item_*`` envelopes from kernel ticks.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    mint_agenda_id,
)
from tesseract.orchestrator.workers.record import RiskClass


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import tesseract.paths
    importlib.reload(tesseract.paths)
    return tmp_path


def _make_item(goal: str = "kernel mutation visibility") -> AgendaItem:
    now = datetime.now(timezone.utc)
    return AgendaItem(
        id=mint_agenda_id(goal[:40], now=now),
        created_at=now,
        updated_at=now,
        source=AgendaSource.RECOVERY,
        goal=goal,
        rationale="phase-2 test",
        risk_class=RiskClass.AUTONOMOUS,
        status=AgendaStatus.PROPOSED,
    )


def test_add_fires_broadcast_hook() -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def _hook(event_type: str, item: AgendaItem, extras: dict[str, Any]) -> None:
        calls.append((event_type, item.id, dict(extras)))

    store = AgendaStore(broadcast_hook=_hook)
    item = _make_item()
    store.add(item)

    assert len(calls) == 1
    assert calls[0][0] == "agenda_item_added"
    assert calls[0][1] == item.id


def test_transition_fires_broadcast_hook_with_prior_status() -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def _hook(event_type: str, item: AgendaItem, extras: dict[str, Any]) -> None:
        calls.append((event_type, item.id, dict(extras)))

    store = AgendaStore(broadcast_hook=_hook)
    item = _make_item()
    store.add(item)
    calls.clear()  # discard the add broadcast; focus on transition

    store.transition(item, AgendaStatus.SELECTED, reason="kernel_test")

    assert len(calls) == 1
    event_type, item_id, extras = calls[0]
    assert event_type == "agenda_item_transitioned"
    assert item_id == item.id
    assert extras == {"prior_status": "proposed"}


def test_transition_to_same_status_is_noop_no_broadcast() -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def _hook(event_type: str, item: AgendaItem, extras: dict[str, Any]) -> None:
        calls.append((event_type, item.id, dict(extras)))

    store = AgendaStore(broadcast_hook=_hook)
    item = _make_item()
    store.add(item)
    calls.clear()

    # Same-status transition is documented as a no-op in the store; the
    # broadcast must not fire either or the operator's tab churns on
    # idempotent kernel ticks.
    store.transition(item, AgendaStatus.PROPOSED, reason="noop")

    assert calls == []


def test_set_broadcast_hook_after_construction() -> None:
    calls: list[str] = []

    def _hook(event_type: str, item: AgendaItem, extras: dict[str, Any]) -> None:
        calls.append(event_type)

    store = AgendaStore()  # no hook
    item = _make_item()
    store.add(item)
    assert calls == []  # nothing fires when hook is unset

    store.set_broadcast_hook(_hook)
    store.transition(item, AgendaStatus.SELECTED, reason="late_hook")
    assert calls == ["agenda_item_transitioned"]


def test_hook_exception_does_not_break_mutation() -> None:
    def _bad_hook(event_type: str, item: AgendaItem, extras: dict[str, Any]) -> None:
        raise RuntimeError("hook intentionally raises")

    store = AgendaStore(broadcast_hook=_bad_hook)
    item = _make_item()

    # Must not propagate; the mutation must succeed and be on disk.
    store.add(item)
    fetched = store.get(item.id)
    assert fetched is not None
    assert fetched.id == item.id
