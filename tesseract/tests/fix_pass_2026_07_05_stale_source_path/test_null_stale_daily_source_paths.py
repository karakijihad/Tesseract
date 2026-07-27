"""prune_stale_daily_notes nulls `source_path` on memories whose daily note is gone.

memory_lint has flagged stale `source_path` frontmatter since 2026-06-03:
`prune_stale_daily_notes` archives daily notes but never repaired the
cross-references held by memories promoted from them. The prune now ends
with `_null_stale_daily_source_paths`, an idempotent sweep restricted to
missing `daily/` paths.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from tesseract.memory.dreaming import DreamingEngine
from tesseract.memory.index import MemoryIndex
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType


def _make_memory(store_dir: Path, *, mid: str, source_path: str) -> None:
    folder = store_dir / MemoryType.PROJECT.value
    folder.mkdir(parents=True, exist_ok=True)
    fm = MemoryFrontmatter(
        id=mid,
        type=MemoryType.PROJECT,
        title=f"title-{mid}",
        created_at=datetime.now(timezone.utc),
        source_path=source_path,
    )
    text = (
        "---\n"
        + yaml.dump(fm.to_yaml_dict(), default_flow_style=False, sort_keys=False)
        + "---\n\n"
        + "Body prose about John Doe.\n"
    )
    (folder / f"{mid}.md").write_text(text, encoding="utf-8")


def _engine(store_dir: Path) -> DreamingEngine:
    return DreamingEngine(
        store=MemoryStore(store_dir),
        index=MemoryIndex(store_dir=store_dir),
        recall_log_path=store_dir / "recall.jsonl",
    )


def test_missing_daily_source_path_is_nulled(tmp_path: Path) -> None:
    _make_memory(tmp_path, mid="mem_stale1", source_path="daily/2026-04-29.md#chat_digest-2026-04-29")
    engine = _engine(tmp_path)

    engine.prune_stale_daily_notes()

    fm, _ = engine._store.read("mem_stale1", log_access=False)
    assert fm.source_path == ""


def test_existing_daily_source_path_is_kept(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir(parents=True)
    (daily / "2026-07-05.md").write_text("today\n", encoding="utf-8")
    _make_memory(tmp_path, mid="mem_fresh1", source_path="daily/2026-07-05.md")
    engine = _engine(tmp_path)

    engine.prune_stale_daily_notes(now=datetime(2026, 7, 5, tzinfo=timezone.utc))

    fm, _ = engine._store.read("mem_fresh1", log_access=False)
    assert fm.source_path == "daily/2026-07-05.md"


def test_non_daily_source_path_is_out_of_scope(tmp_path: Path) -> None:
    _make_memory(tmp_path, mid="mem_vault1", source_path="vault/notes/missing.md")
    engine = _engine(tmp_path)

    engine.prune_stale_daily_notes()

    fm, _ = engine._store.read("mem_vault1", log_access=False)
    assert fm.source_path == "vault/notes/missing.md"


def test_prune_archives_note_and_nulls_reference_in_one_pass(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir(parents=True)
    now = datetime(2026, 7, 5, tzinfo=timezone.utc)
    old = (now - timedelta(days=40)).strftime("%Y-%m-%d")
    (daily / f"{old}.md").write_text("old note\n", encoding="utf-8")
    _make_memory(tmp_path, mid="mem_linked1", source_path=f"daily/{old}.md")
    engine = _engine(tmp_path)

    pruned = engine.prune_stale_daily_notes(max_age_days=30, now=now)

    assert pruned == 1
    assert not (daily / f"{old}.md").exists()
    fm, _ = engine._store.read("mem_linked1", log_access=False)
    assert fm.source_path == ""
