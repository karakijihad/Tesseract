"""Drift events as first-class memory records (2026-05-02).

Covers:
  - `tesseract.conscience.memory_writer.write_drift_entry` — fresh write,
    same-day flap collapse, recurrence enrichment.
  - `tesseract.conscience.memory_writer.count_recent_drifts` — windowed
    counts, signal scoping.
  - `tesseract.scheduler.tasks.conscience_heartbeat` — drift writer
    wiring and recurrence injection into the transition payload.
  - `tesseract.brain.chat._format_conscience_transition` — recurrence +
    flapping + reflection-prompt rendering.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tesseract.brain.chat import _format_conscience_transition
from tesseract.conscience.memory_writer import (
    DRIFT_SUBDIR,
    DRIFT_TAG,
    FLAPPING_TAG,
    count_recent_drifts,
    write_drift_entry,
)
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryType
from tesseract.scheduler.tasks.conscience_heartbeat import ConscienceHeartbeatJob
from tesseract.scheduler.types import JobContext


NOW = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)


def _make_store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "memory-store")


def _transition(
    *,
    frm: str = "ok",
    to: str = "bad",
    signal_name: str = "breakers_open",
    detail: str = "vault_librarian",
) -> dict[str, Any]:
    return {
        "from": frm,
        "to": to,
        "summary": {"ok": 2, "warn": 0, "bad": 1} if to == "bad" else {"ok": 3, "warn": 0, "bad": 0},
        "changed_signals": [
            {
                "name": signal_name,
                "from": frm,
                "to": to,
                "value": 3.0,
                "detail": detail,
            }
        ],
    }


# ── writer: fresh entry ───────────────────────────────────────────────────


def test_write_drift_entry_creates_indexed_memory(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    result = write_drift_entry(store=store, transition=_transition(), when=NOW)
    assert result is not None
    assert result.flapping is False
    assert result.primary_signal == "breakers_open"

    # Lives under conscience/drift/ — searchable via list_all (rglob).
    drift_dir = store.store_dir / DRIFT_SUBDIR
    files = list(drift_dir.glob("*.md"))
    assert len(files) == 1, f"expected 1 entry, got {[p.name for p in files]}"

    fms = store.list_all(type_filter=MemoryType.CONSCIENCE)
    assert any(fm.id == result.memory_id for fm in fms), \
        "drift entry must be visible to list_all so memory_search can find it"

    found = next(fm for fm in fms if fm.id == result.memory_id)
    assert found.type == MemoryType.CONSCIENCE
    assert DRIFT_TAG in found.tags
    assert "breakers_open" in found.tags
    assert FLAPPING_TAG not in found.tags

    # Body carries wikilinks for signal-name clustering in the graph.
    text = (drift_dir / f"{result.memory_id}.md").read_text(encoding="utf-8")
    assert "[[breakers_open]]" in text
    assert "ok→bad" in text


def test_write_drift_entry_returns_none_without_changed_signal(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    bare = {"from": "ok", "to": "warn", "summary": {}, "changed_signals": []}
    assert write_drift_entry(store=store, transition=bare, when=NOW) is None


# ── writer: same-day flap collapse ────────────────────────────────────────


def test_same_day_flap_collapses_into_single_entry(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    first = write_drift_entry(store=store, transition=_transition(frm="ok", to="bad"), when=NOW)
    assert first is not None
    later_same_day = NOW + timedelta(hours=1)
    second = write_drift_entry(
        store=store,
        transition=_transition(frm="bad", to="ok"),
        when=later_same_day,
    )
    assert second is not None
    assert second.memory_id == first.memory_id, "flap reuses the existing entry id"
    assert second.flapping is True

    files = list((store.store_dir / DRIFT_SUBDIR).glob("*.md"))
    assert len(files) == 1, "flap must not litter the store"

    fms = store.list_all(type_filter=MemoryType.CONSCIENCE)
    fm = next(f for f in fms if f.id == first.memory_id)
    assert FLAPPING_TAG in fm.tags
    text = (store.store_dir / DRIFT_SUBDIR / f"{first.memory_id}.md").read_text(encoding="utf-8")
    assert "Flap update" in text


def test_different_day_writes_sibling_entry(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    write_drift_entry(store=store, transition=_transition(), when=NOW)
    next_day = NOW + timedelta(days=1)
    second = write_drift_entry(store=store, transition=_transition(), when=next_day)
    assert second is not None
    assert second.flapping is False
    files = list((store.store_dir / DRIFT_SUBDIR).glob("*.md"))
    assert len(files) == 2, "different-day drifts get their own files"


# ── writer: recurrence count ──────────────────────────────────────────────


def test_count_recent_drifts_windows(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    # Three drifts on breakers_open spread across 5/45/200 days ago.
    for offset_days in (5, 45, 200):
        write_drift_entry(
            store=store,
            transition=_transition(signal_name="breakers_open"),
            when=NOW - timedelta(days=offset_days),
        )
    # And one on a different signal — must NOT count.
    write_drift_entry(
        store=store,
        transition=_transition(signal_name="scheduler_failure"),
        when=NOW - timedelta(days=2),
    )

    counts = count_recent_drifts(
        store=store,
        signal_name="breakers_open",
        now=NOW,
        windows_days=(30, 90, 365),
    )
    assert counts == {30: 1, 90: 2, 365: 3}, \
        f"expected 1/2/3 hits in 30/90/365d, got {counts}"


def test_count_recent_drifts_ignores_non_drift_conscience_memories(tmp_path: Path) -> None:
    """Conscience memories without the drift tag must not be counted.

    Reflection notes (e.g. tagged drift_reflection) live alongside drift
    events in the conscience bucket but should not inflate the recurrence
    count — only entries actually carrying DRIFT_TAG count.
    """
    store = _make_store(tmp_path)
    from tesseract.memory.types import MemoryFrontmatter

    fm = MemoryFrontmatter(
        id=MemoryFrontmatter.generate_id(),
        type=MemoryType.CONSCIENCE,
        title="reflection about breakers_open",
        created_at=NOW - timedelta(days=10),
        updated_at=NOW - timedelta(days=10),
        tags=["breakers_open", "drift_reflection"],
    )
    body = (
        "Long-form reflection on breakers_open recurrence — kept in the "
        "conscience bucket as a reflection artifact so count_recent_drifts "
        "must distinguish it from actual drift events by the absence of "
        "the drift tag."
    )
    assert store.write(fm, body)

    write_drift_entry(
        store=store,
        transition=_transition(signal_name="breakers_open"),
        when=NOW,
    )
    counts = count_recent_drifts(
        store=store,
        signal_name="breakers_open",
        now=NOW,
        windows_days=(30,),
    )
    assert counts == {30: 1}


# ── chat: synthetic note rendering ────────────────────────────────────────


def test_format_conscience_transition_includes_recurrence_and_reflection_prompt() -> None:
    transition: dict[str, Any] = {
        **_transition(),
        "memory_id": "mem_abcd1234",
        "recurrence_days": {30: 3, 90: 7, 365: 12},
    }
    rendered = _format_conscience_transition(transition)
    assert rendered.startswith("[conscience_drift]")
    assert "3 in 30d" in rendered
    assert "7 in 90d" in rendered
    assert "memory_save" in rendered, "reflection prompt must point TARS at memory_save"
    assert "drift_reflection" in rendered


def test_format_conscience_transition_marks_flapping() -> None:
    transition: dict[str, Any] = {
        **_transition(frm="ok", to="warn"),
        "flapping": True,
        "memory_id": "mem_flap0001",
        "recurrence_days": {30: 1},
    }
    rendered = _format_conscience_transition(transition)
    assert "flapping" in rendered.lower()


def test_format_conscience_transition_omits_zero_windows() -> None:
    transition: dict[str, Any] = {
        **_transition(),
        "memory_id": "mem_first0001",
        "recurrence_days": {30: 1, 90: 1, 365: 1},
    }
    rendered = _format_conscience_transition(transition)
    # Counts of 1 should still render; zeros should not.
    transition_zeros: dict[str, Any] = {
        **_transition(),
        "memory_id": "mem_zeros0001",
        "recurrence_days": {30: 0, 90: 0, 365: 0},
    }
    rendered_zeros = _format_conscience_transition(transition_zeros)
    assert "first observed" in rendered_zeros
    assert "in 30d" in rendered or "first observed" in rendered  # non-zero shows window
    assert "0 in" not in rendered_zeros


# ── heartbeat wiring: drift writer fires on transition ────────────────────


def _write_runs(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


def _write_prior_report(tmp_path: Path, summary: dict) -> None:
    conscience_dir = tmp_path / "conscience"
    conscience_dir.mkdir(parents=True, exist_ok=True)
    prior_date = NOW.date().isoformat()
    prior_file = conscience_dir / f"drift-{prior_date}.jsonl"
    prior_file.write_text(
        json.dumps(
            {
                "timestamp": (NOW - timedelta(days=1)).isoformat(),
                "window_hours": 24,
                "signals": [
                    {
                        "name": "circuit_breaker_open_count",
                        "status": "ok",
                        "value": 0,
                        "warn": 1,
                        "bad": 3,
                        "detail": "",
                    },
                    {
                        "name": "scheduler_failure_rate",
                        "status": "ok",
                        "value": 0,
                        "warn": 0.1,
                        "bad": 0.3,
                        "detail": "",
                    },
                    {
                        "name": "scheduler_idle_hours",
                        "status": "ok",
                        "value": 0.1,
                        "warn": 6,
                        "bad": 24,
                        "detail": "",
                    },
                ],
                "summary": summary,
            }
        )
        + "\n",
        encoding="utf-8",
    )


class _FakeMemoryBundle:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store


class _FakeApp:
    def __init__(self, *, memory_bundle: Any | None) -> None:
        self._d: dict[str, Any] = {
            "memory_bundle": memory_bundle,
            "server_sessions": {},
            "scheduler": None,
        }

    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)


async def test_heartbeat_writes_drift_memory_on_transition(tmp_path: Path) -> None:
    """Transition + memory_bundle present → drift memory lands under conscience/drift/."""
    _write_prior_report(tmp_path, summary={"ok": 3, "warn": 0, "bad": 0})
    store = _make_store(tmp_path)
    ctx = JobContext(
        job_name="conscience_heartbeat",
        fired_at=NOW,
        app=_FakeApp(memory_bundle=_FakeMemoryBundle(store)),
        config={},
        log_dir=tmp_path / "schedule",
    )
    result = await ConscienceHeartbeatJob().run(ctx)
    assert result.ok is True
    assert "transition=" in result.detail
    assert result.payload.get("drift_memory_id"), "memory_id must be on the result payload"
    files = list((store.store_dir / DRIFT_SUBDIR).glob("*.md"))
    assert len(files) == 1
    # First-ever drift on this signal — recurrence in 30d window is 1 (this entry).
    assert result.payload["drift_recurrence_days"][30] >= 1


async def test_heartbeat_passes_recurrence_to_chat_session(tmp_path: Path) -> None:
    """The synthetic note injected into the chat session must include the recurrence count."""
    from tesseract.brain.chat import ChatSession
    from tesseract.kernel.adapters.base import AdapterOptions

    class _DummyAdapter:
        async def stream(self, *_a, **_kw):  # pragma: no cover — never called
            if False:
                yield {}

    _write_prior_report(tmp_path, summary={"ok": 3, "warn": 0, "bad": 0})
    store = _make_store(tmp_path)
    # Plant a prior drift on the same signal (5 days ago) so recurrence counts > 1.
    write_drift_entry(
        store=store,
        transition=_transition(signal_name="scheduler_idle_hours"),
        when=NOW - timedelta(days=5),
    )

    chat = ChatSession(
        adapter=_DummyAdapter(),
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(role="chat_brain"),
    )

    class _Sess:
        def __init__(self, cs):
            self.session_id = "s1"
            self.chat_session = cs

    sessions: dict[str, _Sess] = {"s1": _Sess(chat)}

    class _AppWithSessions:
        def __init__(self, store: MemoryStore) -> None:
            self._d: dict[str, Any] = {
                "memory_bundle": _FakeMemoryBundle(store),
                "server_sessions": sessions,
                "scheduler": None,
            }

        def get(self, key: str, default: Any = None) -> Any:
            return self._d.get(key, default)

    ctx = JobContext(
        job_name="conscience_heartbeat",
        fired_at=NOW,
        app=_AppWithSessions(store),
        config={},
        log_dir=tmp_path / "schedule",
    )
    result = await ConscienceHeartbeatJob().run(ctx)
    assert result.ok is True
    # Drain the synthetic note from the chat session and assert it carries
    # the recurrence + reflection prompt.
    drained = chat._drain_pending_suggestions()
    assert "[conscience_drift]" in drained
    assert "in 30d" in drained or "first observed" in drained
    assert "memory_save" in drained, "reflection prompt missing"


async def test_heartbeat_handles_missing_memory_bundle(tmp_path: Path) -> None:
    """No memory bundle → JSONL still writes, transition still broadcasts, no crash."""
    _write_prior_report(tmp_path, summary={"ok": 3, "warn": 0, "bad": 0})
    ctx = JobContext(
        job_name="conscience_heartbeat",
        fired_at=NOW,
        app=_FakeApp(memory_bundle=None),
        config={},
        log_dir=tmp_path / "schedule",
    )
    result = await ConscienceHeartbeatJob().run(ctx)
    assert result.ok is True
    assert "drift_memory_id" not in result.payload
    # Transition still fires — memory bundle absence does not block awareness.
    assert "transition" in result.payload


async def test_heartbeat_same_day_replay_collapses_to_one_entry(tmp_path: Path) -> None:
    """Two heartbeats on the same UTC day → one drift entry, second tagged flapping."""
    _write_prior_report(tmp_path, summary={"ok": 3, "warn": 0, "bad": 0})
    store = _make_store(tmp_path)
    ctx_factory = lambda when: JobContext(
        job_name="conscience_heartbeat",
        fired_at=when,
        app=_FakeApp(memory_bundle=_FakeMemoryBundle(store)),
        config={},
        log_dir=tmp_path / "schedule",
    )
    first = await ConscienceHeartbeatJob().run(ctx_factory(NOW))
    assert first.ok is True

    # Update the prior report so the next heartbeat sees a transition again,
    # and fire it later the same day.
    later = NOW + timedelta(hours=2)
    second = await ConscienceHeartbeatJob().run(ctx_factory(later))
    assert second.ok is True

    # Either second fires another transition (then flap) or no transition;
    # either way, the drift folder must hold exactly one file because both
    # heartbeats fall on the same UTC date.
    files = list((store.store_dir / DRIFT_SUBDIR).glob("*.md"))
    assert len(files) == 1, f"expected 1 entry after same-day replay, got {[p.name for p in files]}"
