"""Layer A — Operator Directives in the system prompt.

Covers `MemoryStore.list_active_directives` (filter + dedup) and the
`_build_directives_section` builder (formatting + budget enforcement).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from tesseract.brain import prompt as prompt_mod
from tesseract.brain.prompt import (
    DIRECTIVES_BODY_PREVIEW_CHARS,
    _build_directives_section,
)
from tesseract.memory.store import DIRECTIVES_IMPORTANCE_FLOOR, MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType, Stability


def _write_feedback(
    store: MemoryStore,
    *,
    mem_id: str,
    title: str,
    summary: str,
    importance: int,
    created_at: datetime,
    stability: Stability = Stability.ACTIVE,
    auto_links: list[str] | None = None,
    mem_type: MemoryType = MemoryType.FEEDBACK,
    slug: str = "",
) -> None:
    """Bypass `MemoryStore.write` (and its WhatNotToSave guard) for tests —
    we exercise the read-side aggregation, not the save-time content filter."""
    fm = MemoryFrontmatter(
        id=mem_id,
        type=mem_type,
        title=title,
        summary=summary,
        importance=importance,
        created_at=created_at,
        stability=stability,
        auto_links=auto_links or [],
        slug=slug,
    )
    target = store.store_dir / mem_type.value / f"{mem_id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    yaml_dict = fm.to_yaml_dict()
    text = "---\n" + yaml.dump(yaml_dict, default_flow_style=False, sort_keys=False) + "---\n\n" + summary
    target.write_text(text, encoding="utf-8")


def test_list_active_directives_filters_and_sorts(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    _write_feedback(store, mem_id="mem_high1", title="High imp", summary="A",
                    importance=9, created_at=base)
    _write_feedback(store, mem_id="mem_med1", title="Mid imp", summary="B",
                    importance=6, created_at=base + timedelta(days=1))
    _write_feedback(store, mem_id="mem_low1", title="Low imp", summary="C",
                    importance=3, created_at=base + timedelta(days=2))
    _write_feedback(store, mem_id="mem_arch", title="Archived", summary="D",
                    importance=8, created_at=base, stability=Stability.ARCHIVED)

    kept = store.list_active_directives()
    ids = [fm.id for fm in kept]
    assert ids == ["mem_high1", "mem_med1"]
    assert kept[0].importance == 9
    assert kept[1].importance == 6


def test_list_active_directives_dedup_via_auto_links(tmp_path: Path) -> None:
    """Higher-importance record wins; later record linking back is dropped."""
    store = MemoryStore(tmp_path)
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    _write_feedback(store, mem_id="mem_keeper", title="Pronoun rule",
                    summary="Use my for private", importance=8, created_at=base)
    _write_feedback(store, mem_id="mem_dupe", title="Pronoun rule restated",
                    summary="Use my for private (variant)", importance=7,
                    created_at=base + timedelta(hours=1),
                    auto_links=["mem_keeper"])
    _write_feedback(store, mem_id="mem_other", title="Different rule",
                    summary="Unrelated", importance=7,
                    created_at=base + timedelta(hours=2))

    kept = store.list_active_directives()
    ids = {fm.id for fm in kept}
    assert "mem_keeper" in ids
    assert "mem_other" in ids
    assert "mem_dupe" not in ids


def test_list_active_directives_dedup_outbound_link(tmp_path: Path) -> None:
    """Higher-importance record carrying auto_links to a duplicate also wins.

    Direction-agnostic check: the librarian doesn't always normalise which
    side of a merged pair carries `auto_links`, so both inbound and
    outbound suppression have to work.
    """
    store = MemoryStore(tmp_path)
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    _write_feedback(store, mem_id="mem_canonical", title="Canonical",
                    summary="x", importance=9, created_at=base,
                    auto_links=["mem_variant"])
    _write_feedback(store, mem_id="mem_variant", title="Variant",
                    summary="y", importance=7,
                    created_at=base + timedelta(hours=1))

    kept = store.list_active_directives()
    ids = {fm.id for fm in kept}
    assert "mem_canonical" in ids
    assert "mem_variant" not in ids


def test_slug_keyed_records_survive_auto_links_dedup(tmp_path: Path) -> None:
    """Slug-keyed records describe distinct decisions; the librarian's
    cosine-merge `auto_links` between two slug-keyed records is a cross-ref,
    not a duplicate marker. Both must survive into the directives block.

    Regression for mem_4b4f133e (PTY-close-whole-tab) being silently dropped
    because it shared auto_links with mem_885d5b59 (brainstorming-2-3-ideas)
    — two unrelated rules linked only by cosine similarity.
    """
    store = MemoryStore(tmp_path)
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    _write_feedback(
        store, mem_id="mem_pty", title="PTY close",
        summary="close whole tab on pty close",
        importance=7, created_at=base + timedelta(days=12),
        slug="pty_close_closes_whole_tab",
        auto_links=["mem_brain", "mem_other"],
    )
    _write_feedback(
        store, mem_id="mem_brain", title="Brainstorming format",
        summary="2-3 ideas plus recommendation",
        importance=7, created_at=base,
        slug="brainstorming_2_3_ideas_recommendation",
        auto_links=["mem_pty", "mem_other"],
    )
    _write_feedback(
        store, mem_id="mem_other", title="Other rule",
        summary="unrelated",
        importance=7, created_at=base + timedelta(days=5),
        slug="other_rule",
    )

    kept = store.list_active_directives()
    ids = {fm.id for fm in kept}
    # All three slug-keyed records must appear — auto_links between distinct
    # decisions must not evict each other.
    assert ids == {"mem_pty", "mem_brain", "mem_other"}


def test_list_active_directives_floor_override(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    _write_feedback(store, mem_id="mem_low", title="Low", summary="x",
                    importance=2, created_at=base)
    assert store.list_active_directives() == []
    assert len(store.list_active_directives(importance_floor=1)) == 1


def test_build_directives_section_renders_block(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    _write_feedback(store, mem_id="mem_a", title="Pronoun precision",
                    summary="Use my for private artifacts; your for operator-facing",
                    importance=8, created_at=base)
    _write_feedback(store, mem_id="mem_b", title="Voice character",
                    summary="Charon timbre, dry observational",
                    importance=7, created_at=base)

    section = _build_directives_section(tmp_path)
    assert section.startswith("# Operator Directives")
    assert "[imp 8] Pronoun precision" in section
    assert "[imp 7] Voice character" in section
    # Higher importance must appear before lower.
    assert section.index("[imp 8]") < section.index("[imp 7]")


def test_build_directives_section_truncates_long_summary(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    long_summary = "x" * (DIRECTIVES_BODY_PREVIEW_CHARS + 50)
    _write_feedback(store, mem_id="mem_long", title="Long rule",
                    summary=long_summary, importance=7, created_at=base)

    section = _build_directives_section(tmp_path)
    assert "…" in section
    # The rendered preview should be at most preview-chars + 1 (the ellipsis).
    rule_line = next(ln for ln in section.splitlines() if ln.startswith("- "))
    body = rule_line.split(": ", 1)[1]
    assert len(body) <= DIRECTIVES_BODY_PREVIEW_CHARS + 1


def test_build_directives_section_budget_drops_lowest(tmp_path: Path, caplog) -> None:
    store = MemoryStore(tmp_path)
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    for idx, imp in enumerate([10, 9, 8, 7, 6]):
        _write_feedback(
            store,
            mem_id=f"mem_{imp}",
            title=f"Rule {imp}",
            summary="x" * 100,
            importance=imp,
            created_at=base + timedelta(hours=idx),
        )

    with caplog.at_level(logging.WARNING, logger=prompt_mod.__name__):
        section = _build_directives_section(tmp_path, char_budget=200)

    assert "Rule 10" in section
    assert "Rule 6" not in section
    assert any("operator-directives" in rec.message for rec in caplog.records)


def test_build_directives_section_empty_returns_blank(tmp_path: Path) -> None:
    MemoryStore(tmp_path)
    assert _build_directives_section(tmp_path) == ""


def test_build_directives_section_missing_dir_returns_blank(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert _build_directives_section(missing) == ""


def test_directives_floor_default_is_six() -> None:
    assert DIRECTIVES_IMPORTANCE_FLOOR == 6


def test_list_active_directives_includes_user_type(tmp_path: Path) -> None:
    """User-type records (e.g. 'inline preview by default') describe operator
    rules just like feedback. The directives helper unions both subdirs."""
    store = MemoryStore(tmp_path)
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    _write_feedback(store, mem_id="mem_fb", title="A correction",
                    summary="don't do X", importance=8, created_at=base)
    _write_feedback(store, mem_id="mem_user", title="Inline preview by default",
                    summary="show artifacts in chat after building",
                    importance=8, created_at=base + timedelta(hours=1),
                    mem_type=MemoryType.USER)
    kept = store.list_active_directives()
    ids = {fm.id for fm in kept}
    assert ids == {"mem_fb", "mem_user"}


def test_list_active_directives_skips_other_types(tmp_path: Path) -> None:
    """Project / reference / conscience records must NOT bleed into the
    directives section even at high importance — they aren't operator rules."""
    store = MemoryStore(tmp_path)
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    _write_feedback(store, mem_id="mem_proj", title="Project state",
                    summary="MO-5 ships next week", importance=9, created_at=base,
                    mem_type=MemoryType.PROJECT)
    _write_feedback(store, mem_id="mem_ref", title="Linear board",
                    summary="ingest pipeline tickets", importance=9, created_at=base,
                    mem_type=MemoryType.REFERENCE)
    _write_feedback(store, mem_id="mem_consc", title="Drift event",
                    summary="repeated apology pattern", importance=9, created_at=base,
                    mem_type=MemoryType.CONSCIENCE)
    assert store.list_active_directives() == []
