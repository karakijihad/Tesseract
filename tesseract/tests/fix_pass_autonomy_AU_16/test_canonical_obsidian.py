"""AU-16 — canonical-store Obsidian readiness.

The wiki/memory/ mirror surface was removed 2026-05-19. The canonical
stores (``memory-store/`` and ``vault/``) are now the single source of
truth Obsidian opens. This module covers:

- ``MemoryStore.write()`` always injects ``kind=type.value`` into the
  leading tag slot.
- The three tree writers (source / topic / global) emit AU-16
  frontmatter at the head of every tree file.
- ``daily_notes.append_section`` writes a ``kind: daily-note``
  frontmatter on the first append of the day.
- ``obsidian_config.ensure_obsidian_config(root)`` ships graph.json at
  ``<root>/.obsidian/graph.json``; idempotent on operator edits.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import yaml

from tesseract.memory.daily_notes import append_section
from tesseract.memory.leaf_seals import Seal, mint_seal_id
from tesseract.memory.obsidian_config import (
    GRAPH_JSON_DEFAULT,
    ensure_obsidian_config,
    graph_json_path,
)
from tesseract.memory.store import MemoryStore, _inject_kind_tag
from tesseract.memory.trees.global_tree import write_daily_digest
from tesseract.memory.trees.source_tree import (
    read_source_tree,
    source_tree_path,
    write_seal_section,
)
from tesseract.memory.trees.topic_tree import (
    activate_topic,
    topic_tree_path,
)
from tesseract.memory.types import MemoryFrontmatter, MemoryType, Stability


_LONG_BODY = (
    "Operator rule body content with enough length to clear the "
    "WhatNotToSave 80-char trivial-body floor for the test fixture."
)


def _make_fm(
    mem_type: MemoryType = MemoryType.FEEDBACK,
    *,
    tags: list[str] | None = None,
    stability: Stability = Stability.ACTIVE,
) -> MemoryFrontmatter:
    now = datetime.now(timezone.utc)
    return MemoryFrontmatter(
        id=MemoryFrontmatter.generate_id(),
        type=mem_type,
        title=f"Test {mem_type.value}",
        summary="",
        created_at=now,
        updated_at=now,
        importance=7,
        tags=tags or [],
        stability=stability,
    )


# ---- _inject_kind_tag helper ----------------------------------------


def test_inject_kind_tag_prepends_kind() -> None:
    fm = _make_fm(MemoryType.FEEDBACK, tags=["voice-first", "tooling"])
    out = _inject_kind_tag(fm)
    assert out.tags == ["feedback", "voice-first", "tooling"]


def test_inject_kind_tag_idempotent() -> None:
    fm = _make_fm(MemoryType.FEEDBACK, tags=["feedback", "voice-first"])
    out = _inject_kind_tag(fm)
    assert out.tags == ["feedback", "voice-first"]
    # And re-running returns equivalent output.
    again = _inject_kind_tag(out)
    assert again.tags == out.tags


def test_inject_kind_tag_dedups_collisions() -> None:
    """Operator-set tag list already contains the kind; we don't duplicate."""
    fm = _make_fm(MemoryType.USER, tags=["unrelated", "user", "again"])
    out = _inject_kind_tag(fm)
    assert out.tags == ["user", "unrelated", "again"]


# ---- MemoryStore.write injects on persistence -----------------------


def test_store_write_injects_kind_tag(isolated_home: Path) -> None:
    store_dir = isolated_home / "memory-store"
    store = MemoryStore(store_dir=store_dir)
    fm = _make_fm(MemoryType.FEEDBACK, tags=["voice-first"])
    assert store.write(fm, _LONG_BODY)
    read = store.read(fm.id, log_access=False)
    assert read is not None
    refetched_fm, _body = read
    assert refetched_fm.tags == ["feedback", "voice-first"]


def test_store_write_injection_runs_for_every_type(isolated_home: Path) -> None:
    store = MemoryStore(store_dir=isolated_home / "memory-store")
    for mt in MemoryType:
        fm = _make_fm(mt)
        assert store.write(fm, _LONG_BODY), f"write blocked for {mt.value}"
        read = store.read(fm.id, log_access=False)
        assert read is not None
        assert read[0].tags[0] == mt.value


# ---- Tree files carry AU-16 frontmatter -----------------------------


def _make_seal(slug: str = "chat-x", sealed_at: datetime | None = None) -> Seal:
    when = sealed_at or datetime.now(timezone.utc)
    return Seal(
        seal_id=mint_seal_id(),
        source_slug=slug,
        sealed_at=when,
        leaf_ids=[f"leaf_{i:08x}" for i in range(2)],
        leaf_count=2,
        summary_title=f"Seal for {slug}",
        summary_body="# header\n\nbody",
    )


def _split_frontmatter(text: str) -> tuple[dict, str]:
    assert text.startswith("---\n"), f"missing frontmatter: {text[:80]!r}"
    end = text.index("\n---\n", 4)
    fm = yaml.safe_load(text[4:end])
    body = text[end + 5:]
    return fm, body


def test_source_tree_emits_au16_frontmatter(isolated_home: Path) -> None:
    seal = _make_seal("chat-y")
    write_seal_section(seal)
    text = read_source_tree("chat-y")
    assert text is not None
    fm, body = _split_frontmatter(text)
    assert fm["kind"] == "source-summary"
    assert fm["parent_tree"] == "source"
    assert "source-summary" in fm["tags"]
    assert seal.seal_id in body


def test_topic_tree_activate_emits_frontmatter(isolated_home: Path) -> None:
    activate_topic("ProjectX")
    text = topic_tree_path("ProjectX").read_text(encoding="utf-8")
    fm, _ = _split_frontmatter(text)
    assert fm["kind"] == "topic-summary"
    assert fm["parent_tree"] == "topic"
    assert "topic-summary" in fm["tags"]


def test_global_digest_emits_frontmatter(isolated_home: Path) -> None:
    seal = _make_seal()
    path = write_daily_digest(date.today(), [seal])
    text = path.read_text(encoding="utf-8")
    fm, body = _split_frontmatter(text)
    assert fm["kind"] == "global-digest"
    assert fm["parent_tree"] == "global"
    assert "global-digest" in fm["tags"]
    assert seal.seal_id in body


# ---- Daily notes get frontmatter once ---------------------------------


def test_daily_notes_frontmatter_on_first_append(isolated_home: Path) -> None:
    daily_dir = isolated_home / "daily"
    append_section(
        header="## session_end",
        body="x" * 120,
        daily_dir=daily_dir,
        date=datetime(2026, 5, 19, 10, tzinfo=timezone.utc),
    )
    target = daily_dir / "2026-05-19.md"
    fm, body = _split_frontmatter(target.read_text(encoding="utf-8"))
    assert fm["kind"] == "daily-note"
    assert "daily-note" in fm["tags"]


def test_daily_notes_second_append_does_not_duplicate_frontmatter(
    isolated_home: Path,
) -> None:
    daily_dir = isolated_home / "daily"
    when = datetime(2026, 5, 19, 10, tzinfo=timezone.utc)
    append_section(header="## first", body="x" * 120, daily_dir=daily_dir, date=when)
    append_section(header="## second", body="y" * 120, daily_dir=daily_dir, date=when)
    text = (daily_dir / "2026-05-19.md").read_text(encoding="utf-8")
    # Exactly one frontmatter block (one `---` opening at offset 0, one closing).
    assert text.startswith("---\n")
    assert text.count("\n---\n") == 1


# ---- obsidian_config.ensure_obsidian_config --------------------------


def test_ensure_obsidian_config_writes_default(isolated_home: Path) -> None:
    root = isolated_home / "memory-store"
    root.mkdir(parents=True, exist_ok=True)
    path = ensure_obsidian_config(root)
    assert path == graph_json_path(root)
    data = json.loads(path.read_text(encoding="utf-8"))
    queries = {g["query"] for g in data["colorGroups"]}
    assert {
        "tag:#topic-summary", "tag:#feedback",
        "tag:#source-summary", "tag:#global-digest",
        "tag:#user", "tag:#project", "tag:#daily-note",
        "tag:#pending", "tag:#buffered", "tag:#conscience",
    } <= queries


def test_ensure_obsidian_config_preserves_operator_edits(isolated_home: Path) -> None:
    root = isolated_home / "memory-store"
    root.mkdir(parents=True, exist_ok=True)
    path = ensure_obsidian_config(root)
    custom = {"colorGroups": [{"query": "tag:#operator-custom", "color": {"rgb": 0}}]}
    path.write_text(json.dumps(custom), encoding="utf-8")
    ensure_obsidian_config(root)
    assert json.loads(path.read_text(encoding="utf-8")) == custom


def test_ensure_obsidian_config_merges_into_obsidian_stub(isolated_home: Path) -> None:
    """Obsidian creates `.obsidian/graph.json` with empty colorGroups the
    first time the operator opens a vault. The helper must merge the
    default palette in rather than skip — otherwise the operator never
    sees colours."""
    root = isolated_home / "memory-store"
    cfg_path = graph_json_path(root)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    # Simulate Obsidian's empty stub — has other fields, empty colorGroups.
    stub = {"colorGroups": [], "showTags": True, "scale": 1.5, "search": ""}
    cfg_path.write_text(json.dumps(stub), encoding="utf-8")

    ensure_obsidian_config(root)
    merged = json.loads(cfg_path.read_text(encoding="utf-8"))
    queries = {g["query"] for g in merged["colorGroups"]}
    assert "tag:#feedback" in queries
    assert "tag:#topic-summary" in queries
    # Obsidian-set fields survived.
    assert merged["showTags"] is True
    assert merged["scale"] == 1.5


def test_obsidian_config_palette_three_tiers_distinct() -> None:
    by_query = {g["query"]: g["color"]["rgb"] for g in GRAPH_JSON_DEFAULT["colorGroups"]}
    red = {by_query[q] for q in ("tag:#topic-summary", "tag:#feedback")}
    yellow = {by_query[q] for q in (
        "tag:#source-summary", "tag:#global-digest",
        "tag:#user", "tag:#project", "tag:#daily-note",
    )}
    orange = {by_query[q] for q in (
        "tag:#pending", "tag:#buffered", "tag:#conscience",
    )}
    assert len(red) == 1
    assert len(yellow) == 1
    assert len(orange) == 1
    assert len(red | yellow | orange) == 3  # tiers are distinct
