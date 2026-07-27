"""Vault-librarian rewire (phase 4) — deterministic two-pass query scope.

Locks in:
- Seed keyword match on INDEX.md + per-page frontmatter title/concepts.
- Expansion pass unions `related_slugs:` + `backlinks_from:` from each seed's
  frontmatter, dedupes against seeds, and respects the cap.
- Header line reports seed + expanded as separate lists.
- Cold vault (empty INDEX.md) returns the existing sentinel message.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.brain.boot import VaultConfig
from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.vault_query import VaultQueryInput, VaultQueryTool
from tesseract.memory.vault_manager import VaultManager


_DEFAULT_VAULT_CONFIG = VaultConfig(
    max_extract_chars=3000,
    scale_split_threshold=80,
    stale_grace_days=180,
    contradiction_pair_limit=50,
    max_seed_slugs=6,
    max_expanded_slugs=12,
    search_rrf_k=60,
    search_default_top_k=5,
)


def _config_with(**overrides) -> VaultConfig:
    base = _DEFAULT_VAULT_CONFIG
    return VaultConfig(
        max_extract_chars=overrides.get("max_extract_chars", base.max_extract_chars),
        scale_split_threshold=overrides.get("scale_split_threshold", base.scale_split_threshold),
        stale_grace_days=overrides.get("stale_grace_days", base.stale_grace_days),
        contradiction_pair_limit=overrides.get("contradiction_pair_limit", base.contradiction_pair_limit),
        max_seed_slugs=overrides.get("max_seed_slugs", base.max_seed_slugs),
        max_expanded_slugs=overrides.get("max_expanded_slugs", base.max_expanded_slugs),
        search_rrf_k=overrides.get("search_rrf_k", base.search_rrf_k),
        search_default_top_k=overrides.get("search_default_top_k", base.search_default_top_k),
    )


def _write_page(
    vault_root: Path,
    slug: str,
    *,
    title: str | None = None,
    topic: str = "general",
    concepts: list[str] | None = None,
    related: list[str] | None = None,
    backlinks: list[str] | None = None,
) -> Path:
    page = vault_root / "wiki" / f"{slug}.md"
    page.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "---",
        f"title: {title or slug.replace('-', ' ').title()}",
        "type: Source",
        f"slug: {slug}",
        f"topic: {topic}",
        f"source_path: raw/{slug}.md",
        "date_added: 2026-04-22",
    ]
    if concepts:
        lines.append("concepts:")
        lines.extend(f"  - {c}" for c in concepts)
    else:
        lines.append("concepts: []")
    if related:
        lines.append("related_slugs:")
        lines.extend(f"  - {s}" for s in related)
    else:
        lines.append("related_slugs: []")
    if backlinks:
        lines.append("backlinks_from:")
        lines.extend(f"  - {s}" for s in backlinks)
    else:
        lines.append("backlinks_from: []")
    lines.append("---")
    lines.append("")
    lines.append(f"# {title or slug.replace('-', ' ').title()}")
    lines.append("")
    lines.append("Body.")

    page.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return page


def _seed_index(vault_root: Path, sections: dict[str, list[str]]) -> Path:
    """Write vault/wiki/INDEX.md with topic sections and slug entries."""
    (vault_root / "wiki").mkdir(parents=True, exist_ok=True)
    index = vault_root / "wiki" / "INDEX.md"
    parts = ["# Vault Wiki Index", ""]
    for topic, slugs in sections.items():
        parts.append(f"## {topic}")
        for slug in slugs:
            parts.append(f"- [[{slug}]] — summary for {slug}")
        parts.append("")
    index.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return index


def _build_tool(vault_root: Path, config: VaultConfig | None = None) -> VaultQueryTool:
    manager = VaultManager(vault_root=vault_root)
    return VaultQueryTool(
        vault_manager=manager,
        vault_config=config or _DEFAULT_VAULT_CONFIG,
        vault_librarian=None,  # raw-page rendering — asserts header line directly
    )


async def _run(tool: VaultQueryTool, query: str, topic_filter: str | None = None) -> str:
    result = await tool.run(
        VaultQueryInput(query=query, topic_filter=topic_filter),
        ToolContext(),
    )
    return result.output


# ── Tests ─────────────────────────────────────────────────


async def test_vault_query_expands_via_related_slugs(tmp_path) -> None:
    """Seed matches A only; B appears via A's related_slugs."""
    vault = tmp_path / "vault"
    _seed_index(vault, {"Memory Systems": ["alpha", "beta"]})
    _write_page(vault, "alpha", title="Alpha Paper", topic="memory-systems", related=["beta"])
    _write_page(vault, "beta", title="Beta Note", topic="memory-systems", backlinks=["alpha"])

    tool = _build_tool(vault)
    # "Alpha" matches only A's title via INDEX.md entry + frontmatter title.
    output = await _run(tool, "alpha paper")

    assert "seed=[alpha]" in output
    assert "expanded=[beta]" in output
    assert "=== alpha ===" in output
    assert "=== beta ===" in output


async def test_vault_query_expands_via_backlinks_from(tmp_path) -> None:
    """Seed matches source S; hub H surfaces through S's backlinks_from."""
    vault = tmp_path / "vault"
    _seed_index(vault, {"Concepts": ["source-paper", "system-dynamics"]})
    _write_page(
        vault,
        "source-paper",
        title="Source Paper",
        topic="concepts",
        concepts=["feedback loop"],
        backlinks=["system-dynamics"],
    )
    _write_page(vault, "system-dynamics", title="System Dynamics", topic="concepts")

    tool = _build_tool(vault)
    output = await _run(tool, "feedback loop")

    assert "seed=[source-paper]" in output
    assert "expanded=[system-dynamics]" in output


async def test_vault_query_caps_at_max_expanded(tmp_path) -> None:
    """One seed with 30 related slugs is capped at max_expanded_slugs."""
    vault = tmp_path / "vault"
    related = [f"linked-{i:02d}" for i in range(30)]
    # Topic "Research Papers" shares no tokens with the query, so only
    # `hub-page` seeds (its slug contains both "hub" and "page").
    _seed_index(vault, {"Research Papers": ["hub-page", *related]})
    _write_page(vault, "hub-page", title="Hub Page", topic="research-papers", related=related)
    for slug in related:
        _write_page(vault, slug, title=slug, topic="research-papers")

    tool = _build_tool(vault, _config_with(max_expanded_slugs=12))
    output = await _run(tool, "hub page")

    assert "seed=[hub-page]" in output
    expanded_segment = output.split("expanded=", 1)[1].split("\n", 1)[0]
    expanded = [s.strip() for s in expanded_segment.strip("[]").split(",") if s.strip()]
    assert len(expanded) == 12
    # Dedup check: every expanded slug is distinct and not the seed.
    assert len(set(expanded)) == 12
    assert "hub-page" not in expanded


async def test_vault_query_cold_vault_behaves_like_phase3(tmp_path) -> None:
    """Empty wiki (no INDEX.md or header-only INDEX.md) → sentinel message."""
    vault = tmp_path / "vault"
    manager = VaultManager(vault_root=vault)
    manager.seed_wiki_skeleton()  # writes header-only INDEX.md

    tool = _build_tool(vault)
    output = await _run(tool, "anything at all")

    assert "vault wiki is empty" in output.lower()
    assert "seed=" not in output  # no scoping header when sentinel fires


async def test_vault_query_seed_cap_enforced(tmp_path) -> None:
    """max_seed_slugs=2 caps seeds even when 5 pages match the query."""
    vault = tmp_path / "vault"
    slugs = [f"match-{i}" for i in range(5)]
    _seed_index(vault, {"Matches": slugs})
    for s in slugs:
        _write_page(vault, s, title=s, topic="matches")

    tool = _build_tool(vault, _config_with(max_seed_slugs=2))
    output = await _run(tool, "match")

    seed_segment = output.split("seed=", 1)[1].split(",", 1)[0]  # first bracketed list
    # Parse up to the closing bracket for the seeds list.
    seed_segment = output.split("seed=", 1)[1]
    seed_bracket = seed_segment.split("]", 1)[0] + "]"
    seeds = [s.strip() for s in seed_bracket.strip("[]").split(",") if s.strip()]
    assert len(seeds) == 2
