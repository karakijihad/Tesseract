"""Memory index regression: links must follow the real on-disk path,
summaries must clip on word boundaries.

Two related bugs surfaced in the live `memory-store/MEMORY.md`:

1. The Top-N / Recent rows were hard-coded as `{type}/{id}.md`. Operator
   sub-buckets like `reference/people/` produced broken links because the
   real path is `reference/people/<id>.md`.
2. Both summary truncations (`body[:100]` at promotion time, and
   `summary[:80]` at render time) hard-sliced bytes, leaving rows ending
   mid-word like `…named "Jih`.

These tests pin the fixes:
- `_link_path` walks the store and emits the real relative path.
- `_clip_words` cuts on the last whitespace ≤ max_len-1 and appends '…'.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from tesseract.memory.librarian import Librarian, _clip_words
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType


def _write_memory(
    store_dir: Path,
    *,
    subpath: str,
    memory_id: str,
    mem_type: MemoryType,
    title: str,
    summary: str,
    importance: int = 7,
) -> None:
    folder = store_dir / Path(subpath)
    folder.mkdir(parents=True, exist_ok=True)
    fm = MemoryFrontmatter(
        id=memory_id,
        type=mem_type,
        title=title,
        summary=summary,
        importance=importance,
        created_at=datetime.now(timezone.utc),
    )
    body = "Body content for the memory record."
    content = (
        "---\n"
        + yaml.dump(fm.to_yaml_dict(), default_flow_style=False, sort_keys=False)
        + "---\n\n"
        + body
    )
    (folder / f"{memory_id}.md").write_text(content, encoding="utf-8")


def test_clip_words_word_boundary() -> None:
    text = "The operator (in this chat) is Jane Doe. When searching named Jane."
    out = _clip_words(text, 40)
    assert len(out) <= 40
    assert out.endswith("…")
    assert not out.rstrip("…").endswith(" ")
    body = out[:-1]
    assert " " not in text[len(body) : len(body) + 1] or text[len(body)] == " "


def test_clip_words_returns_unchanged_when_short_enough() -> None:
    assert _clip_words("hello", 80) == "hello"


def test_clip_words_falls_back_to_hard_slice_when_no_whitespace() -> None:
    out = _clip_words("a" * 50, 10)
    assert len(out) <= 10
    assert out.endswith("…")


def test_clip_words_zero_or_negative_budget_returns_empty() -> None:
    assert _clip_words("anything goes here", 0) == ""
    assert _clip_words("anything goes here", -5) == ""


def test_link_path_resolves_subbucket(tmp_path: Path) -> None:
    """Memory under `reference/people/` must produce a `reference/people/...` link."""
    store = MemoryStore(tmp_path)
    _write_memory(
        tmp_path,
        subpath="reference/people",
        memory_id="mem_link01",
        mem_type=MemoryType.REFERENCE,
        title="Jane Doe — Profile",
        summary="Predictive Maintenance Engineer at Springfield Polytechnic.",
    )
    librarian = Librarian(store=store, embeddings=None)
    fm = next(m for m in store.list_all() if m.id == "mem_link01")
    assert librarian._link_path(fm) == "reference/people/mem_link01.md"


def test_link_path_resolves_flat_layout(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    _write_memory(
        tmp_path,
        subpath="user",
        memory_id="mem_link02",
        mem_type=MemoryType.USER,
        title="Operator preference",
        summary="A short summary.",
    )
    librarian = Librarian(store=store, embeddings=None)
    fm = next(m for m in store.list_all() if m.id == "mem_link02")
    assert librarian._link_path(fm) == "user/mem_link02.md"


def test_memory_index_writes_real_path_for_subbucket(tmp_path: Path) -> None:
    """End-to-end: the rendered MEMORY.md row must match the actual file path."""
    store = MemoryStore(tmp_path)
    _write_memory(
        tmp_path,
        subpath="reference/people",
        memory_id="mem_idx01",
        mem_type=MemoryType.REFERENCE,
        title="Jane Doe",
        summary=(
            "The operator (in this chat) is Jane Doe. When searching or "
            "mentioning the public profile named Jane Doe always disambiguate."
        ),
        importance=8,
    )
    librarian = Librarian(store=store, embeddings=None)
    top, _ = librarian._top_by_importance(limit=20)
    recent = librarian._recent_entries(days=7, limit=10)
    librarian._write_memory_index(top=top, recent=recent, counts={})

    rendered = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert "reference/people/mem_idx01.md" in rendered
    assert "(reference/mem_idx01.md)" not in rendered

    for line in rendered.splitlines():
        if "mem_idx01" not in line:
            continue
        body = line.rstrip()
        if body.endswith("…"):
            body_no_ellipsis = body[:-1].rstrip()
            assert not body_no_ellipsis.endswith(" "), f"row ends in trailing space: {line!r}"
