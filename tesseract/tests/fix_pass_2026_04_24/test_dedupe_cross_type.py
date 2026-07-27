"""M4 regression — `check_with_title` must not collapse distinct memory types.

Pre-fix, `_normalize_title` stripped the `[type]` prefix, so
`[user] Preferences` and `[project] Preferences` both normalized to
`"preferences"` and `title_exact` silently blocked the second write.
Post-fix, `new_type` scopes the title-exact / title-fuzzy passes to the
same memory type.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from tesseract.memory import dedupe
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType


_BODY = (
    "Long-enough body to clear the WhatNotToSave trivial-content threshold "
    "(>= 80 chars). Distinct per entry for cosine separation.\n"
)


def _fm(store: MemoryStore, mem_type: MemoryType, title: str, body_suffix: str = "") -> MemoryFrontmatter:
    now = datetime.now(timezone.utc)
    fm = MemoryFrontmatter(
        id=MemoryFrontmatter.generate_id(),
        type=mem_type,
        title=title,
        summary=title,
        created_at=now,
        updated_at=now,
        importance=5,
        tags=[],
        source_session="librarian",
        source_path="",
        source_url="",
        source_type="consolidation",
    )
    ok = store.write(fm, body=_BODY + body_suffix)
    assert ok, "store.write rejected the seed memory — fixture body too short or blocked"
    return fm


async def test_same_title_different_type_proceeds(tmp_path: Path) -> None:
    """`[user] Preferences` and `[project] Preferences` must not collide."""
    store = MemoryStore(store_dir=tmp_path / "memory-store")
    _fm(store, MemoryType.USER, "[user] Preferences", body_suffix="user-scoped variant")

    proceed, matched, reason = await dedupe.check_with_title(
        "[project] Preferences",
        body="different project-scoped content",
        store=store,
        embeddings=None,
        new_type=MemoryType.PROJECT,
    )

    assert proceed is True
    assert matched is None
    assert reason is None


async def test_same_title_same_type_blocks(tmp_path: Path) -> None:
    """Same title + same type still short-circuits on title_exact."""
    store = MemoryStore(store_dir=tmp_path / "memory-store")
    existing = _fm(store, MemoryType.USER, "[user] Preferences", body_suffix="original")

    proceed, matched, reason = await dedupe.check_with_title(
        "[user] Preferences",
        body="second user-type entry with same title",
        store=store,
        embeddings=None,
        new_type=MemoryType.USER,
    )

    assert proceed is False
    assert matched == existing.id
    assert reason == "title_exact"
