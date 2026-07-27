"""Regression suite for scheduler S1 — daily writer + WS logs-stream hooks.

Covers: DailyWriterJob happy path, idempotency, empty-rows rollup;
daily_notes append helper idempotency + pad + new-file (memory-stream
writers still use the markdown helper); ws._autosave zero-turn
[session_end] entry.

Updated 2026-04-23 for memory-retune M1 — daily_writer + autosave route
logs-stream prefixes to `tesseract/logs/sessions/YYYY-MM-DD.jsonl` instead
of `memory-store/daily/*.md`. `daily_notes.append_section` itself is
unchanged and remains the shared helper for memory-stream writers.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tesseract.memory.daily_notes import _LIBRARIAN_MIN_BODY, append_section
from tesseract.scheduler import log as scheduler_log
from tesseract.scheduler.tasks.daily_writer import DailyWriterJob
from tesseract.scheduler.types import JobContext


# ── daily_notes helper ────────────────────────────────────────────────────

def test_daily_notes_append_new_file_creates_dir(tmp_path: Path):
    daily_dir = tmp_path / "daily"
    wrote = append_section(
        header="## [test] First",
        body="Body long enough to survive the librarian floor — padding is not required for this one.",
        daily_dir=daily_dir,
        date=datetime(2026, 4, 21, tzinfo=timezone.utc),
    )
    assert wrote is True
    target = daily_dir / "2026-04-21.md"
    assert target.exists()
    content = target.read_text(encoding="utf-8")
    # AU-16 (2026-05-19) — daily files now open with a `kind: daily-note`
    # frontmatter block so Obsidian's graph view color-groups them as
    # rollups. The header still lands right after the frontmatter.
    assert content.startswith("---\n")
    assert "kind: daily-note" in content
    assert "## [test] First\n" in content


def test_daily_notes_idempotent_probe_hit(tmp_path: Path):
    daily_dir = tmp_path / "daily"
    header = "## [session_end] Session abcd1234 closed 2026-04-21T00:00:00Z"
    body = "Mirror session closed (id=abcd1234-full-uuid).\nTurns: 1  |  Compactions: 0  |  Memory saves: 0\nClose reason: normal"
    assert append_section(
        header=header, body=body, daily_dir=daily_dir,
        date=datetime(2026, 4, 21, tzinfo=timezone.utc),
        idempotency_probe="id=abcd1234-full-uuid",
    ) is True
    assert append_section(
        header=header, body=body, daily_dir=daily_dir,
        date=datetime(2026, 4, 21, tzinfo=timezone.utc),
        idempotency_probe="id=abcd1234-full-uuid",
    ) is False
    content = (daily_dir / "2026-04-21.md").read_text(encoding="utf-8")
    assert content.count("[session_end]") == 1


def test_daily_notes_pad_short_body(tmp_path: Path):
    daily_dir = tmp_path / "daily"
    body = "tiny body"
    assert len(body) < _LIBRARIAN_MIN_BODY
    append_section(
        header="## [test] Pad",
        body=body,
        daily_dir=daily_dir,
        date=datetime(2026, 4, 21, tzinfo=timezone.utc),
        pad_short=True,
    )
    content = (daily_dir / "2026-04-21.md").read_text(encoding="utf-8")
    _, section_body = content.split("## [test] Pad\n", 1)
    assert len(section_body.rstrip()) >= _LIBRARIAN_MIN_BODY


# ── DailyWriterJob ────────────────────────────────────────────────────────

def _write_runs_jsonl(log_dir: Path, rows: list[dict]) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "runs.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _build_app_ctx(memory_store_dir: Path) -> SimpleNamespace:
    store = SimpleNamespace(store_dir=memory_store_dir)
    bundle = SimpleNamespace(store=store)
    return {"memory_bundle": bundle}


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def test_daily_writer_happy_path(tmp_path: Path, monkeypatch):
    log_dir = tmp_path / "logs" / "schedule"
    monkeypatch.setattr(scheduler_log, "_DEFAULT_LOG_DIR", log_dir)
    yesterday = datetime(2026, 4, 20, 15, 0, tzinfo=timezone.utc)
    rows = [
        {"job_name": "librarian_heartbeat", "fired_at": yesterday.isoformat(),
         "ok": True, "duration_ms": 4000.0, "run_id": "r1", "detail": "", "payload": {}},
        {"job_name": "librarian_heartbeat", "fired_at": (yesterday + timedelta(seconds=1)).isoformat(),
         "ok": True, "duration_ms": 4400.0, "run_id": "r2", "detail": "", "payload": {}},
        {"job_name": "index_rebuild", "fired_at": yesterday.isoformat(),
         "ok": False, "duration_ms": 100.0, "run_id": "r3", "detail": "err", "payload": {}},
    ]
    _write_runs_jsonl(log_dir, rows)

    store_dir = tmp_path / "memory-store"
    ctx = JobContext(
        job_name="daily_writer",
        fired_at=datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
        app=_build_app_ctx(store_dir),
    )
    result = await DailyWriterJob().run(ctx)
    assert result.ok is True
    assert result.payload["jobs"] == 2
    assert result.payload["runs"] == 3

    entries = _read_jsonl(tmp_path / "logs" / "sessions" / "2026-04-20.jsonl")
    assert len(entries) == 1
    entry = entries[0]
    assert entry["type"] == "scheduler"
    assert entry["title"] == "Daily rollup 2026-04-20"
    assert "| librarian_heartbeat | 2 | 2 | 0 | 4200 |" in entry["body"]
    assert "| index_rebuild | 1 | 0 | 1 | 100 |" in entry["body"]
    # memory-stream daily/ must NOT receive scheduler rollups after M1.
    assert not (store_dir / "daily").exists()


async def test_daily_writer_idempotent(tmp_path: Path, monkeypatch):
    log_dir = tmp_path / "logs" / "schedule"
    monkeypatch.setattr(scheduler_log, "_DEFAULT_LOG_DIR", log_dir)
    yesterday = datetime(2026, 4, 20, 15, 0, tzinfo=timezone.utc)
    _write_runs_jsonl(log_dir, [
        {"job_name": "j", "fired_at": yesterday.isoformat(),
         "ok": True, "duration_ms": 10.0, "run_id": "r", "detail": "", "payload": {}},
    ])
    store_dir = tmp_path / "memory-store"
    ctx = JobContext(
        job_name="daily_writer",
        fired_at=datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
        app=_build_app_ctx(store_dir),
    )
    first = await DailyWriterJob().run(ctx)
    second = await DailyWriterJob().run(ctx)
    assert first.ok and second.ok
    assert second.payload["skipped"] is True
    entries = _read_jsonl(tmp_path / "logs" / "sessions" / "2026-04-20.jsonl")
    assert len(entries) == 1


async def test_daily_writer_no_rows_writes_empty_rollup(tmp_path: Path, monkeypatch):
    log_dir = tmp_path / "logs" / "schedule"
    monkeypatch.setattr(scheduler_log, "_DEFAULT_LOG_DIR", log_dir)
    # No runs.jsonl at all — simulates first day after install. Post-M1 the
    # rollup lands in the logs stream, which has no librarian floor, so the
    # old `pad_short=not aggregates` path is gone — we just assert the
    # "no runs" body made it through.
    store_dir = tmp_path / "memory-store"
    ctx = JobContext(
        job_name="daily_writer",
        fired_at=datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
        app=_build_app_ctx(store_dir),
    )
    result = await DailyWriterJob().run(ctx)
    assert result.ok is True
    assert result.payload["jobs"] == 0
    entry = _read_jsonl(tmp_path / "logs" / "sessions" / "2026-04-20.jsonl")[0]
    assert entry["type"] == "scheduler"
    assert "No scheduled runs on 2026-04-20." in entry["body"]


async def test_daily_writer_app_missing_falls_back(tmp_path: Path, monkeypatch):
    """ctx.app=None path — job must still succeed via the TESSERACT_DIR fallback."""
    log_dir = tmp_path / "logs" / "schedule"
    monkeypatch.setattr(scheduler_log, "_DEFAULT_LOG_DIR", log_dir)
    from tesseract.scheduler.tasks import daily_writer as dw
    monkeypatch.setattr(dw, "TESSERACT_HOME", tmp_path)
    ctx = JobContext(
        job_name="daily_writer",
        fired_at=datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
        app=None,
    )
    result = await DailyWriterJob().run(ctx)
    assert result.ok is True
    assert (tmp_path / "logs" / "sessions" / "2026-04-20.jsonl").exists()


# ── ws._autosave zero-turn hook ───────────────────────────────────────────

async def test_ws_autosave_zero_turn_writes_session_end(tmp_path: Path, monkeypatch):
    from tesseract.mirror.server import ws as ws_module
    from tesseract.mirror.server import ws_connection

    # `_autosave` lives in ws_connection.py (SDD Task 7.3) and resolves
    # `TESSERACT_HOME` from that module's own globals — patch there, not on
    # the ws.py re-export, or the patch is a silent no-op.
    monkeypatch.setattr(ws_connection, "TESSERACT_HOME", tmp_path)

    # Minimal fake session. ws._autosave touches:
    #  - session.chat_session.history (for turn count)
    #  - session.session_id, session.compact_count, session.memory_saves
    #  - session.chats (multi-chat persist + recall index — empty here)
    #  - app["adapter_options"] (None → short-circuits the disk save path)
    chat = SimpleNamespace(history=[])
    fake_session = SimpleNamespace(
        chat_session=chat,
        chats={},
        session_id="deadbeefcafef00d0000000000000000",
        compact_count=0,
        memory_saves=0,
        save_name=None,
        started_at="2026-04-21T00:00:00+00:00",
    )
    fake_app = {"adapter_options": None}

    await ws_module._autosave(fake_app, fake_session)

    today = datetime.now(timezone.utc).date().isoformat()
    jsonl = _read_jsonl(tmp_path / "logs" / "sessions" / f"{today}.jsonl")
    assert len(jsonl) == 1
    entry = jsonl[0]
    assert entry["type"] == "session_end"
    assert entry["title"].startswith("Session deadbeef closed")
    # Close-log format is multi-chat-aware since 25e2929 (chat-aware persistence).
    assert "Turns (all chats): 0" in entry["body"]
    # Post-M1 the logs stream has no librarian floor — the old `pad_short`
    # invariant no longer applies.
    assert not (tmp_path / "memory-store" / "daily").exists()
