"""MemoryStore.delete() cascades into entries that referenced the deleted id.

Closes the root cause of recurring `memory_lint` failures: deleting a memory
used to leave stale `auto_links`/`links` and broken wikilinks inside the
auto-managed `## Related` block of every entry that pointed at it. The
cascade strips the dead id from the frontmatter lists and re-renders the
Related block from the trimmed `auto_links` so the next lint run is clean.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from tesseract.memory.memory_lint import MemoryLinter
from tesseract.memory.related_block import (
    END_MARKER,
    START_MARKER,
    render_related_block,
)
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType


def _fm(
    mem_id: str,
    *,
    mem_type: MemoryType = MemoryType.PROJECT,
    title: str | None = None,
    auto_links: list[str] | None = None,
    links: list[str] | None = None,
) -> MemoryFrontmatter:
    return MemoryFrontmatter(
        id=mem_id,
        type=mem_type,
        title=title or mem_id,
        summary="seed",
        created_at=datetime(2026, 5, 17, tzinfo=timezone.utc),
        auto_links=auto_links or [],
        links=links or [],
    )


def _seed(store: MemoryStore, fm: MemoryFrontmatter, body: str) -> None:
    assert store.write(fm, body), f"seed failed for {fm.id}"


def test_delete_removes_id_from_other_entries_auto_links(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory-store")

    _seed(store, _fm("mem_target01", title="Target"), "Target body — long enough to clear the WhatNotToSave trivial-body filter so the seed sticks for the cascade test below.")
    related_block = render_related_block([("mem_target01", "Target")])
    _seed(
        store,
        _fm("mem_holder01", auto_links=["mem_target01"]),
        "Holder body that references the target — long enough to clear the WhatNotToSave trivial-body filter.\n\n" + related_block + "\n",
    )

    assert store.delete("mem_target01") is True

    fm_after, body_after = store.read("mem_holder01", log_access=False)
    assert fm_after.auto_links == []
    # Related block re-rendered to empty — markers gone with the last entry.
    assert START_MARKER not in body_after
    assert END_MARKER not in body_after
    assert "mem_target01" not in body_after

    report = MemoryLinter(store_dir=store.store_dir).lint()
    assert report.broken_frontmatter_links == []
    assert report.broken_wikilinks == []


def test_delete_removes_id_from_links_field_too(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory-store")

    _seed(store, _fm("mem_target02"), "Target body — long enough to clear the WhatNotToSave trivial-body filter so the seed sticks for the cascade test below.")
    _seed(
        store,
        _fm("mem_holder02", links=["mem_target02", "mem_other999"]),
        "Holder body that names the target so the cascade has something to scrub — padded to clear the trivial-body filter.",
    )

    store.delete("mem_target02")

    fm_after, _ = store.read("mem_holder02", log_access=False)
    assert fm_after.links == ["mem_other999"]


def test_delete_preserves_surviving_auto_links(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory-store")

    _seed(store, _fm("mem_keep001", title="Keep"), "Keep body — padded to clear the WhatNotToSave trivial-body filter so the cascade test has a real neighbor.")
    _seed(store, _fm("mem_drop001", title="Drop"), "Drop body — padded to clear the WhatNotToSave trivial-body filter for the test seed.")
    _seed(
        store,
        _fm("mem_holder03", auto_links=["mem_drop001", "mem_keep001"]),
        "Holder body that links to both Drop and Keep — padded to clear the WhatNotToSave trivial-body filter for this multi-neighbor cascade test.\n\n"
        + render_related_block([("mem_drop001", "Drop"), ("mem_keep001", "Keep")])
        + "\n",
    )

    store.delete("mem_drop001")

    fm_after, body_after = store.read("mem_holder03", log_access=False)
    assert fm_after.auto_links == ["mem_keep001"]
    assert "mem_keep001" in body_after
    assert "mem_drop001" not in body_after


def test_delete_leaves_unrelated_entries_alone(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory-store")

    _seed(store, _fm("mem_target04"), "Target body — padded to clear the WhatNotToSave trivial-body filter for the bystander test.")
    _seed(store, _fm("mem_bystander", auto_links=["mem_unrelated"]), "Bystander body — padded to clear the WhatNotToSave trivial-body filter for this isolated test.")

    store.delete("mem_target04")

    fm_after, _ = store.read("mem_bystander", log_access=False)
    assert fm_after.auto_links == ["mem_unrelated"]


def test_cascade_bypasses_trivial_body_filter(tmp_path: Path) -> None:
    """When the cascade strips the only Related entry and the operator
    prose was already terse, the leftover body falls below the
    WhatNotToSave trivial-body floor. The cascade must still rewrite the
    frontmatter so the deleted id stops appearing in `auto_links` /
    `links` — otherwise the next `memory_lint` flags exactly the kind of
    dangling reference this fix is meant to prevent."""
    store = MemoryStore(tmp_path / "memory-store")

    target_body = (
        "Target body — padded with enough content to clear the WhatNotToSave "
        "trivial-body filter so the seed sticks for this cascade test."
    )
    _seed(store, _fm("mem_target05", title="Target"), target_body)

    short_body = "tiny."  # well under the 80-char trivial-body floor
    related = render_related_block([("mem_target05", "Target")])
    _seed(
        store,
        _fm("mem_terse001", auto_links=["mem_target05"]),
        short_body + "\n\n" + related + "\n",
    )

    assert store.delete("mem_target05") is True

    fm_after, body_after = store.read("mem_terse001", log_access=False)
    assert fm_after.auto_links == []
    assert "mem_target05" not in body_after

    report = MemoryLinter(store_dir=store.store_dir).lint()
    assert report.broken_frontmatter_links == []
    assert report.broken_wikilinks == []


def test_delete_returns_false_for_missing_id_and_skips_cascade(
    tmp_path: Path,
) -> None:
    store = MemoryStore(tmp_path / "memory-store")
    _seed(store, _fm("mem_only001", auto_links=["mem_ghost"]), "Bystander body — padded to clear the WhatNotToSave trivial-body filter for this isolated test.")

    assert store.delete("mem_ghost") is False

    # Bystander untouched because the missing id never existed and delete
    # short-circuits before cascading.
    fm_after, _ = store.read("mem_only001", log_access=False)
    assert fm_after.auto_links == ["mem_ghost"]
