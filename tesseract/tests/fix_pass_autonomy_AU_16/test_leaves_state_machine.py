"""AU-16 S1 — leaf state machine + atomic LeafStore IO."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tesseract.memory.leaves import (
    LeafState,
    LeafStore,
    LeafTransition,
    MemoryLeaf,
    TERMINAL_STATES,
    mint_leaf_id,
)


def _make_leaf(
    *,
    body: str = "first observation",
    source: str = "chat:test-session",
    state: LeafState = LeafState.PENDING_EXTRACTION,
) -> MemoryLeaf:
    now = datetime.now(timezone.utc)
    return MemoryLeaf(
        id=mint_leaf_id(),
        source=source,
        created_at=now,
        updated_at=now,
        state=state,
        body=body,
        title="",
        entities=[],
        importance=5,
    )


def test_terminal_set_is_dropped_and_sealed() -> None:
    assert TERMINAL_STATES == frozenset({LeafState.DROPPED, LeafState.SEALED})


def test_id_must_start_with_leaf_prefix(isolated_home: Path) -> None:
    store = LeafStore()
    leaf = _make_leaf()
    leaf.id = "wrong_prefix_12345"
    with pytest.raises(ValueError):
        store.add(leaf)


def test_pending_can_admit_or_drop_but_not_skip_to_buffered(isolated_home: Path) -> None:
    leaf = _make_leaf()
    with pytest.raises(ValueError):
        leaf.transition_to(LeafState.BUFFERED, reason="skip-attempt")
    leaf.transition_to(LeafState.ADMITTED, reason="ok")
    assert leaf.state is LeafState.ADMITTED


def test_terminal_state_refuses_further_transitions(isolated_home: Path) -> None:
    leaf = _make_leaf()
    leaf.transition_to(LeafState.DROPPED, reason="too_short")
    assert leaf.is_terminal()
    with pytest.raises(ValueError):
        leaf.transition_to(LeafState.ADMITTED, reason="reanimate")


def test_drop_reason_captures_transition_reason() -> None:
    leaf = _make_leaf()
    leaf.transition_to(LeafState.DROPPED, reason="too_short:5<40")
    assert leaf.drop_reason == "too_short:5<40"


def test_store_persists_and_reads_round_trip(isolated_home: Path) -> None:
    store = LeafStore()
    leaf = _make_leaf(body="round trip body")
    store.add(leaf)
    got = store.get(leaf.id)
    assert got is not None
    assert got.body == "round trip body"
    assert got.source == leaf.source


def test_store_refuses_duplicate_add(isolated_home: Path) -> None:
    store = LeafStore()
    leaf = _make_leaf()
    store.add(leaf)
    dup = _make_leaf()
    dup.id = leaf.id  # forced collision
    with pytest.raises(ValueError):
        store.add(dup)


def test_store_archives_on_terminal_transition(isolated_home: Path) -> None:
    store = LeafStore()
    leaf = _make_leaf()
    store.add(leaf)
    store.transition(leaf, LeafState.DROPPED, reason="too_short")
    # Active file gone, archive bucket holds the leaf.
    active = isolated_home / "memory-store" / "leaves" / "active" / f"{leaf.id}.json"
    assert not active.exists()
    archived = store.get(leaf.id)
    assert archived is not None
    assert archived.state is LeafState.DROPPED
    bucket = leaf.updated_at.strftime("%Y-%m")
    archived_path = (
        isolated_home
        / "memory-store"
        / "leaves"
        / "archive"
        / bucket
        / f"{leaf.id}.json"
    )
    assert archived_path.exists()


def test_list_in_state_isolates_by_state(isolated_home: Path) -> None:
    store = LeafStore()
    pending = _make_leaf(body="pending leaf body that's long enough", source="src:a")
    admitted = _make_leaf(body="admitted leaf body that's long enough", source="src:a")
    store.add(pending)
    store.add(admitted)
    admitted.transition_to(LeafState.ADMITTED, reason="manual")
    store.save(admitted)

    pending_set = {lf.id for lf in store.list_in_state(LeafState.PENDING_EXTRACTION)}
    admitted_set = {lf.id for lf in store.list_in_state(LeafState.ADMITTED)}
    assert pending.id in pending_set
    assert admitted.id in admitted_set
    assert pending.id not in admitted_set


def test_state_history_records_every_step(isolated_home: Path) -> None:
    store = LeafStore()
    leaf = _make_leaf()
    store.add(leaf)
    leaf.transition_to(LeafState.ADMITTED, reason="extract")
    store.save(leaf)
    leaf.transition_to(LeafState.BUFFERED, reason="append")
    store.save(leaf)
    leaf.transition_to(LeafState.SEALED, reason="seal_abc")
    store.save(leaf)
    refetched = store.get(leaf.id)
    assert refetched is not None
    states = [t.to_state for t in refetched.state_history]
    assert states[0] is LeafState.PENDING_EXTRACTION  # initial-seed transition
    assert states[1:] == [
        LeafState.ADMITTED,
        LeafState.BUFFERED,
        LeafState.SEALED,
    ]


def test_atomic_write_leaves_no_tmp_dropping(isolated_home: Path) -> None:
    store = LeafStore()
    leaf = _make_leaf()
    store.add(leaf)
    leaf.body = "updated body that's also long enough to admit"
    store.save(leaf)
    active_dir = isolated_home / "memory-store" / "leaves" / "active"
    tmps = list(active_dir.glob("*.tmp"))
    assert tmps == []
