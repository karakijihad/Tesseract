"""chat.py drain helper picks up undelivered operator_post events and
emits `[workspace_post_on_<event_id>]` blocks. Codex 2026-05-06 M2:
the drain no longer marks delivered itself — it returns the IDs and
the caller commits via `_mark_workspace_delivered` only after the
synthetic turn's reply has succeeded. Codex 2026-05-07 M1: the drain
is also pinned to a single ``target_event_id`` so unrelated pending
posts cannot piggyback on this turn's delivery commit."""

from __future__ import annotations

from pathlib import Path

from tesseract.brain import chat as chat_mod
from tesseract.workspace_events import EventStore, WorkspaceEvent


def _seed_post(store: EventStore, *, body: str = "Hello TARS") -> WorkspaceEvent:
    ev = WorkspaceEvent.new(
        kind="operator_post",
        source="operator",
        title="Quick note",
        summary=body[:400],
        payload={"body": body, "source": "scratchpad"},
    )
    store.append_event(ev)
    return ev


def test_drain_emits_post_block_and_marks_delivered(
    tmp_path: Path, monkeypatch,
) -> None:
    """First drain returns the formatted block + the IDs it pulled.
    Marking delivered is a SEPARATE caller-driven step — after that, a
    second drain returns nothing (M2)."""
    logs_dir = tmp_path / "logs"
    store = EventStore(logs_dir)
    ev = _seed_post(store, body="look at vault search latency")

    monkeypatch.setattr(
        "tesseract.kernel.workspace_changes.workspace_events_dir",
        lambda: logs_dir,
    )

    blocks, ids = chat_mod._drain_operator_posts(ev.event_id)
    assert len(blocks) == 1
    assert f"[workspace_post_on_{ev.event_id}]" in blocks[0]
    assert "look at vault search latency" in blocks[0]
    assert ids == [ev.event_id]

    # Without an explicit mark, the drain is idempotent — the ID is
    # still undelivered, so a second pull returns the same item.
    blocks2, ids2 = chat_mod._drain_operator_posts(ev.event_id)
    assert blocks2 == blocks
    assert ids2 == ids

    # Caller commits delivery after a successful workspace_reply.
    chat_mod._mark_workspace_delivered([], ids)
    blocks3, ids3 = chat_mod._drain_operator_posts(ev.event_id)
    assert blocks3 == [] and ids3 == []

    persisted = next(e for e in store.list_events() if e.event_id == ev.event_id)
    assert persisted.delivered_to_tars is True


def test_drain_only_returns_targeted_post(
    tmp_path: Path, monkeypatch,
) -> None:
    """Codex 2026-05-07 M1: when two operator_posts are pending, the
    drain returns ONLY the one matching ``target_event_id``. Previously
    the drain returned all undelivered posts, which let a successful
    reply on turn A mark unrelated post B delivered before B's own
    queued turn ran."""
    logs_dir = tmp_path / "logs"
    store = EventStore(logs_dir)
    ev_a = _seed_post(store, body="first")
    ev_b = _seed_post(store, body="second")

    monkeypatch.setattr(
        "tesseract.kernel.workspace_changes.workspace_events_dir",
        lambda: logs_dir,
    )

    blocks_a, ids_a = chat_mod._drain_operator_posts(ev_a.event_id)
    assert len(blocks_a) == 1
    assert ids_a == [ev_a.event_id]
    assert ev_b.event_id not in blocks_a[0]

    blocks_b, ids_b = chat_mod._drain_operator_posts(ev_b.event_id)
    assert len(blocks_b) == 1
    assert ids_b == [ev_b.event_id]

    # No-target form drains nothing — it is no longer used as a global
    # flush after the M1 fix.
    assert chat_mod._drain_operator_posts(None) == ([], [])
    assert chat_mod._drain_operator_posts("") == ([], [])
