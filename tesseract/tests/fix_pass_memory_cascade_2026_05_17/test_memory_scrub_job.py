"""MemoryScrubJob — report mode counts repairs, fix mode applies them.

Covers the two repair classes scrub owns (broken frontmatter links, zero-
byte orphan stubs) and verifies it leaves the operator-attended findings
alone (stale_source_paths, broken_wikilinks of kind ``missing_path``).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tesseract.memory.memory_lint import MemoryLinter
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType
from tesseract.scheduler.tasks.memory_scrub import MemoryScrubJob
from tesseract.scheduler.types import JobContext


def _fm(
    mem_id: str,
    *,
    mem_type: MemoryType = MemoryType.PROJECT,
    auto_links: list[str] | None = None,
    links: list[str] | None = None,
    source_path: str = "",
) -> MemoryFrontmatter:
    return MemoryFrontmatter(
        id=mem_id,
        type=mem_type,
        title=mem_id,
        summary="seed",
        created_at=datetime(2026, 5, 17, tzinfo=timezone.utc),
        auto_links=auto_links or [],
        links=links or [],
        source_path=source_path,
    )


def _make_ctx_with_store(store: MemoryStore, mode: str) -> JobContext:
    # Real MemoryStore so MemoryScrubJob.fix path can write back through it.
    bundle = SimpleNamespace(store=store)
    app = {"memory_bundle": bundle}
    return JobContext(
        job_name="memory_scrub",
        app=app,
        config={"mode": mode},
    )


def test_report_mode_counts_scrubbable_without_mutating(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory-store")
    assert store.write(
        _fm("mem_holder1", auto_links=["mem_ghost9"]), "Body — padded to clear the WhatNotToSave trivial-body filter so the scrub-job seed sticks."
    )
    (store.store_dir / "reference" / "people").mkdir(parents=True, exist_ok=True)
    (store.store_dir / "reference" / "people" / "ghost.md").write_text(
        "", encoding="utf-8"
    )

    ctx = _make_ctx_with_store(store, "report")
    result = asyncio.run(MemoryScrubJob().run(ctx))

    assert result.ok is True
    assert result.payload["mode"] == "report"
    assert result.payload["scrubbable"] == 2

    # Nothing rewritten.
    fm_after, _ = store.read("mem_holder1", log_access=False)
    assert fm_after.auto_links == ["mem_ghost9"]
    assert (store.store_dir / "reference" / "people" / "ghost.md").exists()


def test_fix_mode_strips_dead_frontmatter_links(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory-store")
    assert store.write(
        _fm("mem_holder2", auto_links=["mem_ghost9", "mem_real1"]),
        "Body — padded to clear the WhatNotToSave trivial-body filter so the scrub-job seed sticks.",
    )
    assert store.write(_fm("mem_real1"), "Live neighbor — padded so the scrub-job seed clears the WhatNotToSave trivial-body filter.")

    ctx = _make_ctx_with_store(store, "fix")
    result = asyncio.run(MemoryScrubJob().run(ctx))

    assert result.ok is True, result.detail
    assert result.payload["fixed_frontmatter_links"] == 1
    fm_after, _ = store.read("mem_holder2", log_access=False)
    assert fm_after.auto_links == ["mem_real1"]


def test_fix_mode_unlinks_zero_byte_stubs(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory-store")
    stub_dir = store.store_dir / "reference" / "people"
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / "ghost.md"
    stub.write_text("", encoding="utf-8")

    ctx = _make_ctx_with_store(store, "fix")
    result = asyncio.run(MemoryScrubJob().run(ctx))

    assert result.ok is True, result.detail
    assert result.payload["fixed_orphan_stubs"] == 1
    assert not stub.exists()


def test_fix_mode_idempotent_when_clean(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory-store")
    assert store.write(_fm("mem_clean1"), "Body — padded to clear the WhatNotToSave trivial-body filter so the scrub-job seed sticks.")

    ctx = _make_ctx_with_store(store, "fix")
    first = asyncio.run(MemoryScrubJob().run(ctx))
    second = asyncio.run(MemoryScrubJob().run(ctx))

    assert first.ok is True
    assert second.ok is True
    assert second.payload["fixed_frontmatter_links"] == 0
    assert second.payload["fixed_orphan_stubs"] == 0


def test_fix_mode_leaves_stale_source_paths_alone(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory-store")
    assert store.write(
        _fm("mem_stale", source_path="vault/missing.md"),
        "Body — padded to clear the WhatNotToSave trivial-body filter so the scrub-job seed sticks.",
    )

    ctx = _make_ctx_with_store(store, "fix")
    result = asyncio.run(MemoryScrubJob().run(ctx))

    # ok stays True (fm_links + orphans both at 0), but the stale source
    # path is still detected and the file is untouched.
    assert result.ok is True
    report_after = MemoryLinter(store_dir=store.store_dir).lint()
    assert len(report_after.stale_source_paths) == 1
    fm_after, _ = store.read("mem_stale", log_access=False)
    assert fm_after.source_path == "vault/missing.md"


def test_invalid_mode_returns_error(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory-store")
    ctx = _make_ctx_with_store(store, "destroy_everything")
    result = asyncio.run(MemoryScrubJob().run(ctx))

    assert result.ok is False
    assert "invalid mode" in result.detail


def test_missing_bundle_returns_error(tmp_path: Path) -> None:
    ctx = JobContext(job_name="memory_scrub", app=None, config={"mode": "report"})
    result = asyncio.run(MemoryScrubJob().run(ctx))

    assert result.ok is False
    assert "unavailable" in result.detail
