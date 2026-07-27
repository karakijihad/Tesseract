"""Regression suite for memory-retune M1 — stream split + source-annotation writers.

Covers:
  * `append_log_entry` writes JSONL with correct schema + idempotency
  * Header `[type]` token parsing (happy path, missing, empty)
  * `cmd_reflect`, `_autosave`, `_maybe_auto_compact`, `DailyWriterJob.run`
    all route to `logs/sessions/` instead of `memory-store/daily/`
  * Pure `_extract_type_prefix` helper in librarian (M2 hook point)

Contract lives in `Docs/Plan/memory-retune/_shared/memory-stream-contract.md`.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _fake_reflect_in_background(saves: int):
    """Stand-in for `reflect_in_background` that fires `on_complete`
    asynchronously and returns the resulting Task.

    `saves` is a count; the helper synthesises that many ``memory_save``
    summaries to match the new ``list[dict]`` callback contract.
    """
    pending: list[asyncio.Task] = []
    saves_list = [
        {"tool": "memory_save", "title": f"item-{i}", "snippet": f"snippet-{i}"}
        for i in range(saves)
    ]

    def factory(session, reason, *, on_complete=None, on_error=None):
        async def _run():
            if on_complete is not None:
                await on_complete(saves_list, reason)
        task = asyncio.create_task(_run(), name="fake_reflect_bg")
        pending.append(task)
        return task

    return factory, pending


NOW = datetime(2026, 4, 22, 16, 0, tzinfo=timezone.utc)


# ── append_log_entry ──────────────────────────────────────────────────────


def test_log_notes_appends_jsonl(tmp_path: Path) -> None:
    from tesseract.memory.log_notes import append_log_entry

    wrote1 = append_log_entry(
        header="## [reflect] Reflection aaaaaaaa 2026-04-22T16:00:00Z",
        body="first body\nprobe=alpha",
        log_dir=tmp_path,
        date=NOW,
        idempotency_probe="probe=alpha",
    )
    wrote2 = append_log_entry(
        header="## [session_end] Session bbbbbbbb closed 2026-04-22T16:05:00Z",
        body="second body\nprobe=beta",
        log_dir=tmp_path,
        date=NOW,
        idempotency_probe="probe=beta",
    )
    wrote3 = append_log_entry(
        header="## [reflect] Repeat",
        body="different body but same probe=alpha",
        log_dir=tmp_path,
        date=NOW,
        idempotency_probe="probe=alpha",
    )

    target = tmp_path / "2026-04-22.jsonl"
    assert wrote1 is True
    assert wrote2 is True
    assert wrote3 is False, "idempotency probe already present — skip"

    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["type"] == "reflect"
    assert first["title"].startswith("Reflection aaaaaaaa")
    assert first["ts"] == "2026-04-22T16:00:00Z"
    assert "probe=alpha" in first["body"]
    assert second["type"] == "session_end"
    assert second["title"].startswith("Session bbbbbbbb closed")


def test_log_notes_type_extraction(tmp_path: Path) -> None:
    from tesseract.memory.log_notes import append_log_entry

    append_log_entry(
        header="## [auto_compact] Compaction 2026-04-22T16:10:00Z",
        body="auto-compact body",
        log_dir=tmp_path,
        date=NOW,
    )
    append_log_entry(
        header="## no-prefix header here",
        body="second body",
        log_dir=tmp_path,
        date=NOW,
    )

    target = tmp_path / "2026-04-22.jsonl"
    lines = [json.loads(l) for l in target.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["type"] == "auto_compact"
    assert lines[0]["title"].startswith("Compaction")
    assert lines[1]["type"] is None
    assert lines[1]["title"] == "no-prefix header here"


# ── _extract_type_prefix (pure helper — M2 hook point) ────────────────────


def test_extract_type_prefix_cases() -> None:
    from tesseract.memory.librarian import _extract_type_prefix

    assert _extract_type_prefix("[reflect] Reflection abc 2026-04-22") == "reflect"
    assert _extract_type_prefix("[chat_digest] 2026-04-22") == "chat_digest"
    assert _extract_type_prefix("  [ user ] Operator prefers async") == "user"
    assert _extract_type_prefix("no bracket title") is None
    assert _extract_type_prefix("[] empty token") is None
    assert _extract_type_prefix("") is None
    assert _extract_type_prefix("[missing-close bracket") is None


# ── cmd_reflect routes to logs/sessions/ ──────────────────────────────────


@pytest.mark.asyncio
async def test_cmd_reflect_writes_to_logs(tmp_path: Path) -> None:
    from tesseract.mirror.server import commands as commands_module

    class _FakeWS:
        closed = False

        async def send_json(self, payload: dict) -> None:
            pass

    class _FakeLibrarian:
        async def run_pass(self) -> dict:
            return {"promoted": 1, "deduped": 0, "skipped": 0, "counts": {}, "top": 0, "recent": 0}

        async def distill_personality_candidates(self, soul_path) -> None:
            pass

    fake_session = SimpleNamespace(
        session_id="sess-reflect-0001",
        event_log=[],
        ws=_FakeWS(),
        chat_session=SimpleNamespace(history=[]),
        memory_saves=0,
    )
    fake_app: dict = {"memory_bundle": SimpleNamespace(librarian=_FakeLibrarian())}

    captured: list[dict] = []

    def _fake_append(**kwargs) -> bool:
        captured.append(kwargs)
        return True

    fake_factory, pending = _fake_reflect_in_background(saves=2)

    with (
        patch.object(commands_module, "reflect_in_background", new=fake_factory),
        patch.object(
            commands_module, "soul_path",
            lambda: SimpleNamespace(stat=lambda: SimpleNamespace(st_mtime=0.0), exists=lambda: False),
        ),
        patch.object(commands_module, "append_log_entry", _fake_append),
    ):
        await commands_module.cmd_reflect(fake_app, fake_session)
        for task in pending:
            await task

    assert len(captured) == 1, f"expected single log write, got {captured}"
    call = captured[0]
    assert call["header"].startswith("## [reflect] Reflection sess-ref")
    # log_dir must end in logs/sessions (OS-agnostic)
    parts = Path(call["log_dir"]).parts[-2:]
    assert parts == ("logs", "sessions"), f"log_dir tail was {parts}"
    # Guard against regression back to daily_dir kwarg
    assert "daily_dir" not in call


# ── _autosave routes to logs/sessions/ ────────────────────────────────────


@pytest.mark.asyncio
async def test_autosave_writes_to_logs() -> None:
    from tesseract.mirror.server import ws as ws_module
    from tesseract.mirror.server import ws_connection

    fake_session = SimpleNamespace(
        session_id="sess-autosave-0001",
        chat_session=SimpleNamespace(history=[]),  # empty → save path skipped
        compact_count=0,
        memory_saves=0,
        save_name=None,
        started_at="2026-04-22T15:00:00+00:00",
    )
    fake_app: dict = {"adapter_options": None}  # no opts → save path skipped

    captured: list[dict] = []

    def _fake_append(**kwargs) -> bool:
        captured.append(kwargs)
        return True

    # `_autosave` lives in ws_connection.py (SDD Task 7.3) and resolves
    # `append_log_entry` from that module's own globals — patch there, not on
    # the ws.py re-export, or the patch is a silent no-op.
    with patch.object(ws_connection, "append_log_entry", _fake_append):
        await ws_module._autosave(fake_app, fake_session)

    assert len(captured) == 1
    call = captured[0]
    assert call["header"].startswith("## [session_end] Session sess-aut")
    parts = Path(call["log_dir"]).parts[-2:]
    assert parts == ("logs", "sessions")
    assert call["idempotency_probe"] == f"id={fake_session.session_id}"
    assert "pad_short" not in call, "pad_short dropped — logs stream has no 80-char floor"


# ── _maybe_auto_compact routes to logs/sessions/ ──────────────────────────


@pytest.mark.asyncio
async def test_auto_compact_writes_to_logs() -> None:
    from tesseract.mirror.server import ws as ws_module
    from tesseract.mirror.server import turn_runner as turn_runner_module

    class _FakeWS:
        closed = False

        async def send_json(self, payload: dict) -> None:
            pass

    fake_session = SimpleNamespace(
        session_id="sess-compact-0001",
        event_log=[],
        ws=_FakeWS(),
        chat_session=SimpleNamespace(history=[]),
        compact_count=0,
        turn_count=5,
    )
    fake_app: dict = {"adapter_options": None}

    captured: list[dict] = []

    def _fake_append(**kwargs) -> bool:
        captured.append(kwargs)
        return True

    with (
        patch.object(turn_runner_module, "auto_compact_if_needed", new=AsyncMock(return_value=(100, 50))),
        patch.object(turn_runner_module, "append_log_entry", _fake_append),
    ):
        await ws_module._maybe_auto_compact(fake_app, fake_session)

    assert len(captured) == 1
    call = captured[0]
    assert call["header"].startswith("## [auto_compact] Compaction ")
    parts = Path(call["log_dir"]).parts[-2:]
    assert parts == ("logs", "sessions")
    assert fake_session.compact_count == 1  # counter still increments


# ── DailyWriterJob routes to logs/sessions/ ───────────────────────────────


@pytest.mark.asyncio
async def test_daily_writer_writes_to_logs(tmp_path: Path) -> None:
    from tesseract.scheduler.tasks.daily_writer import DailyWriterJob
    from tesseract.scheduler.types import JobContext

    # `_resolve_log_dir` prefers `<memory_bundle.store.store_dir>.parent / logs / sessions`
    store_dir = tmp_path / "memory-store"
    store_dir.mkdir()
    fake_app: dict = {
        "memory_bundle": SimpleNamespace(store=SimpleNamespace(store_dir=store_dir)),
    }
    ctx = JobContext(job_name="daily_writer", fired_at=NOW, app=fake_app)

    # `runs.jsonl` missing → `_load_rows_for` returns []; empty aggregates still writes
    # the "No scheduled runs" rollup. Target file: <tmp_path>/logs/sessions/<yesterday>.jsonl.
    result = await DailyWriterJob().run(ctx)

    assert result.ok is True
    yesterday = (NOW.date().replace(day=NOW.day - 1)).isoformat()
    target = tmp_path / "logs" / "sessions" / f"{yesterday}.jsonl"
    assert target.exists(), f"expected {target} to exist"
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["type"] == "scheduler"
    assert entry["title"].startswith(f"Daily rollup {yesterday}")
    assert f"No scheduled runs on {yesterday}" in entry["body"]

    # Confirm the old daily markdown path was NOT written
    assert not (store_dir / "daily").exists(), "DailyWriterJob must not touch memory-store/daily/"


@pytest.mark.asyncio
async def test_daily_writer_is_idempotent(tmp_path: Path) -> None:
    """Second run against the same target-date must skip the write (header-based probe)."""
    from tesseract.scheduler.tasks.daily_writer import DailyWriterJob
    from tesseract.scheduler.types import JobContext

    store_dir = tmp_path / "memory-store"
    store_dir.mkdir()
    fake_app: dict = {
        "memory_bundle": SimpleNamespace(store=SimpleNamespace(store_dir=store_dir)),
    }
    ctx = JobContext(job_name="daily_writer", fired_at=NOW, app=fake_app)

    first = await DailyWriterJob().run(ctx)
    second = await DailyWriterJob().run(ctx)

    assert first.ok is True
    assert second.ok is True
    assert second.payload["skipped"] is True

    yesterday = (NOW.date().replace(day=NOW.day - 1)).isoformat()
    target = tmp_path / "logs" / "sessions" / f"{yesterday}.jsonl"
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, "idempotency probe must prevent second append"
