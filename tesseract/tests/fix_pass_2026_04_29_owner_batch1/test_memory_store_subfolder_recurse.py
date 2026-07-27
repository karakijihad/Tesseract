"""Memory store must walk operator sub-buckets under canonical type dirs.

Operators curate folders like `reference/people/`, `project/sprints/`
to keep large stores navigable. Before this fix, `_find_file` and
`list_all` only looked at the top level of each canonical subdir, so a
file at `reference/people/jane.md` was unreachable through `read()` /
`memory_search` — even though the frontmatter said `type: reference`.

Recursion uses `rglob('*.md')` so any new sub-bucket the operator
creates is discoverable without code changes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType


def _make_fm(memory_id: str, type_: MemoryType, title: str = "t") -> MemoryFrontmatter:
    now = datetime.now(timezone.utc)
    return MemoryFrontmatter(
        id=memory_id,
        type=type_,
        title=title,
        created_at=now,
    )


def test_find_file_recurses_into_subbuckets(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    bucket = tmp_path / "reference" / "people"
    bucket.mkdir(parents=True, exist_ok=True)
    fm = _make_fm("mem_subdir01", MemoryType.REFERENCE, title="Profile: Test")
    body = "Profile body."
    content = "---\n" + _yaml_dump(fm) + "---\n\n" + body
    (bucket / "mem_subdir01.md").write_text(content, encoding="utf-8")

    hit = store.read("mem_subdir01", log_access=False)
    assert hit is not None, "read() must walk reference/people/ sub-bucket"
    assert hit[0].id == "mem_subdir01"


def test_list_all_includes_subbucket_files(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    nested = tmp_path / "project" / "sprints" / "Q1"
    nested.mkdir(parents=True, exist_ok=True)
    fm = _make_fm("mem_nested02", MemoryType.PROJECT, title="Sprint")
    content = "---\n" + _yaml_dump(fm) + "---\n\nSprint body."
    (nested / "mem_nested02.md").write_text(content, encoding="utf-8")

    ids = {m.id for m in store.list_all()}
    assert "mem_nested02" in ids, "list_all() must rglob each canonical type dir"


def test_top_level_files_still_resolved(tmp_path: Path) -> None:
    """Recursion is additive — flat layout still works."""
    store = MemoryStore(tmp_path)
    fm = _make_fm("mem_flat03", MemoryType.USER, title="Flat")
    content = "---\n" + _yaml_dump(fm) + "---\n\nFlat body."
    (tmp_path / "user" / "mem_flat03.md").write_text(content, encoding="utf-8")

    hit = store.read("mem_flat03", log_access=False)
    assert hit is not None
    ids = {m.id for m in store.list_all()}
    assert "mem_flat03" in ids


def _yaml_dump(fm: MemoryFrontmatter) -> str:
    import yaml

    return yaml.dump(fm.to_yaml_dict(), default_flow_style=False, sort_keys=False)
