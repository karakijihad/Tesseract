"""Quick-wins regression suite for the F1-follow-up memory retune.

Covers:
  * `Librarian._promote_daily` skips bookkeeping-tagged sections
    (`[reflect]` / `[session_end]` / `[auto_compact]` / `[scheduler]`).
  * `Librarian._top_by_importance` filters bookkeeping-titled legacy
    entries and ranks by type priority (user > feedback > project >
    reference), breaking ties by importance then created_at.
  * `Librarian._recent_entries` filters bookkeeping-titled entries.
  * `_is_bookkeeping_title` classifier matches the right prefixes.

Full stream-split / classifier / chat-digest work lives in the memory-retune
multi-session plan — see `Docs/Plan/memory-retune/` for the phased refactor.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tesseract.memory.librarian import (
    Librarian,
    RECENT_WINDOW_DAYS,
    _is_bookkeeping_title,
)
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType


NOW = datetime(2026, 4, 22, 16, 0, tzinfo=timezone.utc)


def _fm(
    *,
    mem_id: str,
    mem_type: MemoryType,
    title: str,
    importance: int = 5,
    created_at: datetime = NOW,
) -> MemoryFrontmatter:
    return MemoryFrontmatter(
        id=mem_id,
        type=mem_type,
        title=title,
        summary=title[:50],
        created_at=created_at,
        updated_at=created_at,
        importance=importance,
        tags=[],
        entities=[],
        links=[],
        auto_links=[],
        source_session="test",
        source_path="",
        source_url="",
        source_type="test",
    )


def _seed_store(tmp_path: Path, entries: list[MemoryFrontmatter]) -> MemoryStore:
    store_dir = tmp_path / "memory-store"
    store = MemoryStore(store_dir=store_dir)
    for fm in entries:
        store.write(fm, body=f"# {fm.title}\n\n" + ("body content long enough to clear the librarian floor — " * 3))
    return store


# ── _is_bookkeeping_title ────────────────────────────────────────────────


@pytest.mark.parametrize("title,expected", [
    ("[reflect] Reflection sess-2 2026-04-22T05:20:36Z", True),
    ("[session_end] Session abcd1234 closed 2026-04-22T13:41:27Z", True),
    ("[auto_compact] Compaction at 2026-04-22T12:00:00Z", True),
    ("[scheduler] Daily rollup 2026-04-21", True),
    ("[user] Operator prefers cmd over PowerShell", False),
    ("[project] TARS-reboot merge freeze 2026-03-05", False),
    ("Real memory without brackets", False),
    ("", False),
    ("   [reflect] leading whitespace", True),
])
def test_is_bookkeeping_title_classifies_correctly(title, expected):
    assert _is_bookkeeping_title(title) is expected


# ── _promote_daily skip path ─────────────────────────────────────────────


async def test_promote_daily_skips_bookkeeping_titles(tmp_path: Path):
    store = _seed_store(tmp_path, [])
    librarian = Librarian(store=store, embeddings=None)

    # Write a daily file from YESTERDAY (today's file is always skipped).
    daily_dir = tmp_path / "memory-store" / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    yesterday = (NOW - timedelta(days=1)).date().isoformat()
    daily = daily_dir / f"{yesterday}.md"
    body = "body content long enough to clear the librarian 80-char floor — " * 3
    daily.write_text(
        "\n\n".join([
            f"## [reflect] Reflection sess-2 {yesterday}T05:20:36Z",
            body,
            f"## [session_end] Session abcd1234 closed {yesterday}T13:41:27Z",
            body,
            f"## [auto_compact] Compaction at {yesterday}T12:00:00Z",
            body,
            f"## [scheduler] Daily rollup {yesterday}",
            body,
            "## [user] Operator prefers terse responses",
            body,
        ]),
        encoding="utf-8",
    )

    promoted, deduped, merged, skipped = await librarian._promote_daily()

    # All 4 bookkeeping-tagged sections + the 1 user-tagged section fall
    # through to the promote path. Bookkeeping are explicitly skipped; the
    # `[user]` section may promote or skip depending on WhatNotToSave — what
    # we assert here is ONLY that the bookkeeping filter counted 4 skips.
    assert skipped >= 4, f"expected >=4 bookkeeping skips, got skipped={skipped}"
    assert merged == 0, f"bookkeeping + fresh user section should not merge, got merged={merged}"
    # Nothing from reflect/session_end/auto_compact/scheduler should land in reference/
    refs = [m for m in store.list_all(type_filter=MemoryType.REFERENCE)]
    for fm in refs:
        assert not _is_bookkeeping_title(fm.title), f"bookkeeping leaked: {fm.title}"


# ── _top_by_importance ranking ───────────────────────────────────────────


def test_top_by_importance_ranks_types(tmp_path: Path):
    entries = [
        _fm(mem_id="mem_user1", mem_type=MemoryType.USER, title="user high", importance=5),
        _fm(mem_id="mem_ref1", mem_type=MemoryType.REFERENCE, title="ref top", importance=10),
        _fm(mem_id="mem_fb1", mem_type=MemoryType.FEEDBACK, title="fb mid", importance=5),
        _fm(mem_id="mem_proj1", mem_type=MemoryType.PROJECT, title="proj low", importance=5),
    ]
    store = _seed_store(tmp_path, entries)
    librarian = Librarian(store=store, embeddings=None)

    top, filtered = librarian._top_by_importance(limit=10)
    titles = [fm.title for fm in top]
    # User outranks Feedback outranks Project outranks Reference,
    # *regardless* of importance numeric value.
    assert titles[0] == "user high"
    assert titles[1] == "fb mid"
    assert titles[2] == "proj low"
    assert titles[3] == "ref top"
    assert filtered == 0


def test_top_by_importance_filters_bookkeeping_titles(tmp_path: Path):
    entries = [
        _fm(mem_id="mem_ref_clean", mem_type=MemoryType.REFERENCE, title="Real research note", importance=5),
        _fm(mem_id="mem_ref_reflect", mem_type=MemoryType.REFERENCE, title="[reflect] Reflection sess-2", importance=5),
        _fm(mem_id="mem_ref_se", mem_type=MemoryType.REFERENCE, title="[session_end] Session closed", importance=5),
        _fm(mem_id="mem_user1", mem_type=MemoryType.USER, title="Operator preference", importance=5),
    ]
    store = _seed_store(tmp_path, entries)
    librarian = Librarian(store=store, embeddings=None)

    top, filtered = librarian._top_by_importance(limit=10)
    titles = [fm.title for fm in top]
    assert "Real research note" in titles
    assert "Operator preference" in titles
    assert "[reflect] Reflection sess-2" not in titles
    assert "[session_end] Session closed" not in titles
    assert filtered == 2


# ── _recent_entries filter ───────────────────────────────────────────────


def test_recent_entries_filters_bookkeeping(tmp_path: Path):
    now = datetime.now(timezone.utc)
    entries = [
        _fm(mem_id="mem_user1", mem_type=MemoryType.USER, title="Good memory", created_at=now),
        _fm(mem_id="mem_ref_r", mem_type=MemoryType.REFERENCE, title="[reflect] Reflection sess-3", created_at=now),
        _fm(mem_id="mem_ref_se", mem_type=MemoryType.REFERENCE, title="[session_end] Session closed", created_at=now),
    ]
    store = _seed_store(tmp_path, entries)
    librarian = Librarian(store=store, embeddings=None)

    recent = librarian._recent_entries(days=RECENT_WINDOW_DAYS, limit=10)
    titles = [fm.title for fm in recent]
    assert "Good memory" in titles
    assert not any(t.startswith("[reflect]") for t in titles)
    assert not any(t.startswith("[session_end]") for t in titles)


# ── cmd_reflect idempotency ──────────────────────────────────────────────


def _read_jsonl(path: Path) -> list[dict]:
    import json
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def test_cmd_reflect_collapses_identical_repeat_runs(tmp_path: Path, monkeypatch):
    """Same session + same saves + same librarian counts → single `[reflect]` entry.

    Regression for the observed bloat: 13 `/reflect` fires on sess-2 wrote
    13 identical sections. Post-M1 the entry lands in `logs/sessions/*.jsonl`;
    idempotency signature is session/saves/soul/librarian-counts.
    """
    import asyncio
    from unittest.mock import patch
    from types import SimpleNamespace

    from tesseract.mirror.server import commands as commands_module

    # Redirect TESSERACT_HOME so the logs writer lands in tmp_path
    # (Phase 18.5 W7-A — log paths now anchor to TESSERACT_HOME).
    monkeypatch.setattr(commands_module, "TESSERACT_HOME", tmp_path)

    class _FakeWS:
        closed = False
        def __init__(self):
            self.sent: list[dict] = []
        async def send_json(self, payload: dict) -> None:
            self.sent.append(payload)

    class _FakeLibrarian:
        async def run_pass(self) -> dict:
            return {"promoted": 3, "deduped": 1, "skipped": 2, "counts": {}, "top": 0, "recent": 0}

        async def distill_personality_candidates(self, soul_path) -> None:
            pass

    fake_app = {"memory_bundle": SimpleNamespace(librarian=_FakeLibrarian())}
    sess = SimpleNamespace(
        session_id="sess-idempot",
        event_log=[],
        ws=_FakeWS(),
        chat_session=SimpleNamespace(history=[]),
    )

    pending: list[asyncio.Task] = []
    saves_list = [
        {"tool": "memory_save", "title": f"item-{i}", "snippet": f"snippet-{i}"}
        for i in range(4)
    ]

    def fake_factory(session, reason, *, on_complete=None, on_error=None):
        async def _run():
            if on_complete is not None:
                await on_complete(saves_list, reason)
        task = asyncio.create_task(_run(), name="fake_reflect_bg")
        pending.append(task)
        return task

    with (
        patch.object(commands_module, "reflect_in_background", new=fake_factory),
        patch.object(
            commands_module, "soul_path",
            lambda: SimpleNamespace(stat=lambda: SimpleNamespace(st_mtime=0.0), exists=lambda: False),
        ),
    ):
        await commands_module.cmd_reflect(fake_app, sess)
        await commands_module.cmd_reflect(fake_app, sess)
        await commands_module.cmd_reflect(fake_app, sess)
        for task in pending:
            await task

    logs_dir = tmp_path / "logs" / "sessions"
    files = list(logs_dir.glob("*.jsonl"))
    assert len(files) == 1, f"expected one logs-stream file, got {files}"
    entries = _read_jsonl(files[0])
    reflect_entries = [e for e in entries if e["type"] == "reflect"]
    # All three runs had identical signature → only one `[reflect]` entry.
    assert len(reflect_entries) == 1, (
        f"expected 1 reflect entry after 3 identical runs, got:\n{entries}"
    )
    # The reflect_result envelope still emits on every call (operator feedback).
    reflect_envs = [e for e in sess.ws.sent if e.get("type") == "reflect_result"]
    assert len(reflect_envs) == 3
    # memory-stream daily/ must NOT receive [reflect] entries after M1.
    assert not (tmp_path / "memory-store" / "daily").exists()


async def test_cmd_reflect_appends_when_signature_changes(tmp_path: Path, monkeypatch):
    """Changed saves count (or librarian counts, or soul edit) → new entry lands."""
    import asyncio
    from unittest.mock import patch
    from types import SimpleNamespace

    from tesseract.mirror.server import commands as commands_module

    monkeypatch.setattr(commands_module, "TESSERACT_HOME", tmp_path)

    class _FakeWS:
        closed = False
        def __init__(self):
            self.sent: list[dict] = []
        async def send_json(self, payload: dict) -> None:
            self.sent.append(payload)

    counts = {"promoted": 3, "deduped": 1, "skipped": 2, "counts": {}, "top": 0, "recent": 0}

    class _FakeLibrarian:
        async def run_pass(self) -> dict:
            return counts

        async def distill_personality_candidates(self, soul_path) -> None:
            pass

    fake_app = {"memory_bundle": SimpleNamespace(librarian=_FakeLibrarian())}
    sess = SimpleNamespace(
        session_id="sess-change",
        event_log=[],
        ws=_FakeWS(),
        chat_session=SimpleNamespace(history=[]),
    )

    def _saves_of(n: int) -> list[dict[str, str]]:
        return [
            {"tool": "memory_save", "title": f"item-{i}", "snippet": f"snippet-{i}"}
            for i in range(n)
        ]

    saves_iter = iter([_saves_of(4), _saves_of(7)])
    pending: list[asyncio.Task] = []

    def fake_factory(session, reason, *, on_complete=None, on_error=None):
        saves = next(saves_iter)
        async def _run():
            if on_complete is not None:
                await on_complete(saves, reason)
        task = asyncio.create_task(_run(), name="fake_reflect_bg")
        pending.append(task)
        return task

    with (
        patch.object(commands_module, "reflect_in_background", new=fake_factory),
        patch.object(
            commands_module, "soul_path",
            lambda: SimpleNamespace(stat=lambda: SimpleNamespace(st_mtime=0.0), exists=lambda: False),
        ),
    ):
        await commands_module.cmd_reflect(fake_app, sess)
        # Drain the first task before second call so each completes in order.
        await pending[-1]
        await commands_module.cmd_reflect(fake_app, sess)
        await pending[-1]

    logs_dir = tmp_path / "logs" / "sessions"
    entries = _read_jsonl(next(logs_dir.glob("*.jsonl")))
    reflect_entries = [e for e in entries if e["type"] == "reflect"]
    assert len(reflect_entries) == 2, (
        f"expected 2 reflect entries after signature change, got:\n{entries}"
    )
