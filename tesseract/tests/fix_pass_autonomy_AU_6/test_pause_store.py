"""PauseStore — durable pause registry round-trip + JSONL audit."""

from __future__ import annotations

import json

from tesseract.orchestrator.autonomy import PauseStore
from tesseract.orchestrator.autonomy.governor import (
    DETECTOR_LOOP,
    REASON_LOOP_DETECTED,
    REASON_OPERATOR_UNPAUSE,
)
from tesseract.orchestrator.autonomy.models import AgendaSource
from tesseract.orchestrator.autonomy.paths import (
    governor_log_path,
    source_pauses_path,
)


def test_add_persists_to_disk(pause_store: PauseStore) -> None:
    pause = pause_store.add(
        AgendaSource.SELF_REFLECTION,
        detector=DETECTOR_LOOP,
        reason=REASON_LOOP_DETECTED,
        evidence={"count": 4},
    )
    assert pause is not None
    assert pause.source == AgendaSource.SELF_REFLECTION
    payload = json.loads(source_pauses_path().read_text(encoding="utf-8"))
    assert payload["pauses"][0]["source"] == "self_reflection"
    assert payload["pauses"][0]["detector"] == DETECTOR_LOOP


def test_add_is_idempotent(pause_store: PauseStore) -> None:
    first = pause_store.add(
        AgendaSource.SELF_REFLECTION, detector=DETECTOR_LOOP, reason=REASON_LOOP_DETECTED,
    )
    second = pause_store.add(
        AgendaSource.SELF_REFLECTION, detector=DETECTOR_LOOP, reason=REASON_LOOP_DETECTED,
    )
    assert first is not None
    assert second is None
    assert pause_store.is_paused(AgendaSource.SELF_REFLECTION)


def test_remove_clears_pause(pause_store: PauseStore) -> None:
    pause_store.add(
        AgendaSource.SELF_REFLECTION, detector=DETECTOR_LOOP, reason=REASON_LOOP_DETECTED,
    )
    cleared = pause_store.remove(
        AgendaSource.SELF_REFLECTION, by="operator", reason=REASON_OPERATOR_UNPAUSE,
    )
    assert cleared is not None
    assert not pause_store.is_paused(AgendaSource.SELF_REFLECTION)
    payload = json.loads(source_pauses_path().read_text(encoding="utf-8"))
    assert payload["pauses"] == []


def test_remove_returns_none_when_not_paused(pause_store: PauseStore) -> None:
    assert pause_store.remove(AgendaSource.SELF_REFLECTION) is None


def test_pause_survives_fresh_instance(pause_store: PauseStore) -> None:
    """A pause persisted from one process is visible to a fresh
    PauseStore — the kernel reloads on boot."""
    pause_store.add(
        AgendaSource.PROVIDER_WATCH, detector=DETECTOR_LOOP, reason=REASON_LOOP_DETECTED,
    )

    other = PauseStore()
    assert other.is_paused(AgendaSource.PROVIDER_WATCH)


def test_jsonl_log_appends_pause_and_unpause(pause_store: PauseStore) -> None:
    pause_store.add(
        AgendaSource.SELF_REFLECTION, detector=DETECTOR_LOOP, reason=REASON_LOOP_DETECTED,
    )
    pause_store.remove(AgendaSource.SELF_REFLECTION, by="operator")
    lines = governor_log_path().read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines]
    events = [r["event"] for r in rows]
    assert events == ["pause", "unpause"]


def test_malformed_file_treated_as_empty(
    pause_store: PauseStore,
) -> None:
    """A corrupt source-pauses.json is logged + treated as empty so a
    bad write does not brick the governor."""
    source_pauses_path().parent.mkdir(parents=True, exist_ok=True)
    source_pauses_path().write_text("{ this is not json", encoding="utf-8")
    fresh = PauseStore()
    assert fresh.all_paused() == {}
