"""Body `## Related` block sync + dreaming wikilink-folder skip.

Two related fixes for Obsidian-graph hygiene:
1. `replace_related_block` mirrors `auto_links` into a delimited body
   block so Obsidian's graph view sees inter-memory edges. Idempotent.
2. `dreaming.sweep_missing_wikilinks` no longer injects `[[folder/]]`
   wikilinks for `source_path` values that aren't `.md` files — those
   produced ghost nodes in the graph.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from tesseract.memory.dreaming import DreamingEngine
from tesseract.memory.index import MemoryIndex
from tesseract.memory.related_block import (
    END_MARKER,
    START_MARKER,
    render_related_block,
    replace_related_block,
)
from tesseract.memory.store import MemoryStore, extract_wikilinks
from tesseract.memory.types import MemoryFrontmatter, MemoryType


def _make(store_dir: Path, *, mid: str, mem_type: MemoryType, body: str,
          source_path: str | None = None, auto_links: list[str] | None = None) -> None:
    folder = store_dir / mem_type.value
    folder.mkdir(parents=True, exist_ok=True)
    kwargs: dict = dict(
        id=mid,
        type=mem_type,
        title=f"title-{mid}",
        created_at=datetime.now(timezone.utc),
        auto_links=auto_links or [],
    )
    if source_path is not None:
        kwargs["source_path"] = source_path
    fm = MemoryFrontmatter(**kwargs)
    text = (
        "---\n"
        + yaml.dump(fm.to_yaml_dict(), default_flow_style=False, sort_keys=False)
        + "---\n\n"
        + body
    )
    (folder / f"{mid}.md").write_text(text, encoding="utf-8")


def test_render_related_block_empty_returns_empty() -> None:
    assert render_related_block([]) == ""


def test_render_related_block_includes_markers_and_wikilinks() -> None:
    out = render_related_block(["mem_a", "mem_b"])
    assert out.startswith(START_MARKER)
    assert out.endswith(END_MARKER)
    assert "## Related" in out
    assert "[[mem_a]]" in out
    assert "[[mem_b]]" in out


def test_replace_related_block_appends_when_absent() -> None:
    body = "Some body prose.\n"
    out = replace_related_block(body, ["mem_a"])
    assert "Some body prose." in out
    assert "[[mem_a]]" in out
    assert out.count(START_MARKER) == 1


def test_replace_related_block_is_idempotent() -> None:
    body = "Body."
    once = replace_related_block(body, ["mem_a", "mem_b"])
    twice = replace_related_block(once, ["mem_a", "mem_b"])
    assert once == twice


def test_replace_related_block_swaps_existing_block() -> None:
    body = "Body."
    first = replace_related_block(body, ["mem_a"])
    swapped = replace_related_block(first, ["mem_c", "mem_d"])
    assert "[[mem_a]]" not in swapped
    assert "[[mem_c]]" in swapped and "[[mem_d]]" in swapped
    assert swapped.count(START_MARKER) == 1


def test_replace_related_block_drops_block_when_links_empty() -> None:
    body = "Body."
    with_block = replace_related_block(body, ["mem_a"])
    cleared = replace_related_block(with_block, [])
    assert START_MARKER not in cleared
    assert END_MARKER not in cleared
    assert "Body." in cleared


def test_replace_related_block_preserves_operator_related_section() -> None:
    """Operator-written `## Related` (without our markers) is left alone."""
    body = "Prose.\n\n## Related\n\nManual notes only.\n"
    out = replace_related_block(body, ["mem_a"])
    assert "Manual notes only." in out
    assert "[[mem_a]]" in out


def test_dreaming_skips_folder_source_path(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    _make(tmp_path, mid="mem_folder1", mem_type=MemoryType.PROJECT,
          body="Body.", source_path="tesseract/memory-store/reference/people/")
    dreaming = DreamingEngine(
        store=store,
        index=MemoryIndex(store_dir=tmp_path),
        recall_log_path=tmp_path / "recall.jsonl",
    )
    dreaming.sweep_missing_wikilinks()

    _, body = store.read("mem_folder1", log_access=False)
    assert extract_wikilinks(body) == [], "folder-shaped source_path must not seed a wikilink"


def test_dreaming_skips_non_md_source_path(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    _make(tmp_path, mid="mem_nonmd1", mem_type=MemoryType.PROJECT,
          body="Body.", source_path="tesseract/config/providers.yaml")
    dreaming = DreamingEngine(
        store=store,
        index=MemoryIndex(store_dir=tmp_path),
        recall_log_path=tmp_path / "recall.jsonl",
    )
    dreaming.sweep_missing_wikilinks()

    _, body = store.read("mem_nonmd1", log_access=False)
    assert extract_wikilinks(body) == [], ".yaml source_path must not seed a wikilink"


def test_auto_linker_writes_related_block_to_body(tmp_path: Path) -> None:
    """Integration: _add_auto_links must refresh the body block, not just the YAML.

    Pins the load-bearing wiring — if `replace_related_block` ever drops out
    of `_add_auto_links`, this test catches it. Embeddings aren't needed for
    `_add_auto_links` (it only uses the store).
    """
    from tesseract.memory.auto_linker import AutoLinker

    store = MemoryStore(tmp_path)
    long_body = "Long enough body to clear the WhatNotToSave trivial-content threshold without effort."
    _make(tmp_path, mid="mem_int1", mem_type=MemoryType.PROJECT, body=long_body)
    _make(tmp_path, mid="mem_int2", mem_type=MemoryType.PROJECT, body=long_body)

    linker = AutoLinker(store=store, embeddings=None)  # type: ignore[arg-type]
    linker._add_auto_links("mem_int1", ["mem_int2"])

    fm_after, body_after = store.read("mem_int1", log_access=False)
    assert fm_after.auto_links == ["mem_int2"]
    assert START_MARKER in body_after
    # Title-rendering fix (M3, audit 2026-05-01): _add_auto_links resolves
    # the neighbor's title and renders the Obsidian alias form. Match either
    # `[[mem_int2]]` or `[[mem_int2|title-mem_int2]]` so the assertion stays
    # honest if the title-resolution path ever short-circuits.
    assert ("[[mem_int2]]" in body_after) or ("[[mem_int2|" in body_after)


def test_replace_related_block_preserves_paragraph_break_around_block() -> None:
    """When a block sits mid-body, removal must keep a blank line between
    surrounding paragraphs. Pins the regex `\\n\\n` substitute (was `\\n`)."""
    body = (
        "Para A.\n\n"
        f"{START_MARKER}\n## Related\n\n- [[mem_x]]\n{END_MARKER}\n\n"
        "Para B.\n"
    )
    out = replace_related_block(body, [])
    assert "Para A." in out and "Para B." in out
    assert "Para A.Para B." not in out
    assert "Para A.\nPara B." not in out


def test_dreaming_still_seeds_md_source_path(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    long_body = "Long enough body to clear the WhatNotToSave trivial-content threshold without effort, just words."
    _make(tmp_path, mid="mem_md1", mem_type=MemoryType.PROJECT,
          body=long_body, source_path="daily/2026-04-29.md#some-section")
    dreaming = DreamingEngine(
        store=store,
        index=MemoryIndex(store_dir=tmp_path),
        recall_log_path=tmp_path / "recall.jsonl",
    )
    dreaming.sweep_missing_wikilinks()

    _, body = store.read("mem_md1", log_access=False)
    assert extract_wikilinks(body) != [], ".md source_path should still seed a wikilink"
