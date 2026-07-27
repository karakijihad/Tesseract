"""Vault-librarian rewire (phase 2) — entity schema + ingest contract.

Locks in:
- `load_vault_config()` returns the four expected keys.
- LLM-returned `related_slugs` are filtered to subset of `existing_topics`.
- Empty source text → fallback page is written; no adapter call happens.
- Wiki-page frontmatter includes `type: Source`, `slug:`, `backlinks_from: []`,
  `related_slugs:`, `open_questions:`. `lint_flags:` is absent.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from tesseract.brain.boot import VaultConfig, load_vault_config
from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.memory.vault_librarian import VaultLibrarian
from tesseract.memory.vault_manager import VaultManager


_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "vault" / "sample-article.md"


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


@dataclass
class _StubAdapter:
    """Minimal duck-typed ModelAdapter for the librarian's `_get_adapter` path."""

    response_json: str
    call_count: int = 0

    async def generate(self, prompt: str, options: AdapterOptions) -> str:
        self.call_count += 1
        return self.response_json

    async def stream(self, *args, **kwargs):  # pragma: no cover — unused by librarian
        raise NotImplementedError


def _build_librarian(
    vault_root: Path,
    adapter: ModelAdapter | None,
    *,
    seed_existing_topic: str | None = None,
) -> VaultLibrarian:
    """Construct a librarian against an isolated vault tree."""
    (vault_root / "raw").mkdir(parents=True, exist_ok=True)
    (vault_root / "wiki").mkdir(parents=True, exist_ok=True)
    manager = VaultManager(vault_root=vault_root)
    manager.seed_wiki_skeleton()

    if seed_existing_topic:
        # Add an `## Existing Topic` header so the librarian's
        # `_extract_existing_topics()` has something to compare against.
        index = vault_root / "wiki" / "INDEX.md"
        index.write_text(
            index.read_text(encoding="utf-8")
            + f"\n## {seed_existing_topic.replace('-', ' ').title()}\n",
            encoding="utf-8",
        )

    return VaultLibrarian(
        vault_manager=manager,
        adapter=adapter,
        adapter_options=AdapterOptions(),
        config=_DEFAULT_VAULT_CONFIG,
        agents_dir=None,  # use the real tesseract/agents/vault-librarian.md
    )


def _stage_source(vault_root: Path, fixture: Path) -> str:
    """Copy a fixture into vault/raw/ and return its raw_rel_path."""
    dst = vault_root / "raw" / fixture.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(fixture), str(dst))
    return f"raw/{fixture.name}"


# ── Tests ─────────────────────────────────────────────────


def test_vault_config_loads() -> None:
    """`load_vault_config()` returns the seeded YAML keys with the right types."""
    cfg = load_vault_config()
    assert cfg.max_extract_chars == 3000
    assert cfg.scale_split_threshold == 80
    assert cfg.stale_grace_days == 180
    assert cfg.contradiction_pair_limit == 50
    # audit-1 i3 (2026-04-24) — VaultSearchTool reads RRF + default top_k from config.
    assert cfg.search_rrf_k == 60
    assert cfg.search_default_top_k == 5


async def test_ingest_prompt_rejects_invented_slugs(tmp_path) -> None:
    """LLM-returned `related_slugs` not in INDEX must be dropped before write."""
    response = json.dumps({
        "topic": "memory",
        "summary": "Discusses temporal decay in episodic memory.",
        "entities": ["hippocampus"],
        "concepts": ["temporal decay"],
        "open_questions": ["how long until full forgetting?"],
        "related_slugs": ["nonexistent-slug", "another-fake"],
    })
    adapter = _StubAdapter(response_json=response)
    vault_root = tmp_path / "vault"
    librarian = _build_librarian(vault_root, adapter)

    raw_rel = _stage_source(vault_root, _FIXTURE)
    page = await librarian.compile_source(raw_rel)

    assert page is not None
    assert page.related_slugs == [], (
        f"invented slugs should be filtered out, got {page.related_slugs}"
    )

    written = (vault_root / "wiki" / f"{page.slug}.md").read_text(encoding="utf-8")
    assert "related_slugs: []" in written
    assert "nonexistent-slug" not in written
    assert "another-fake" not in written


async def test_partial_overlap_keeps_real_slugs_only(tmp_path) -> None:
    """When LLM mixes real + invented slugs, only the real ones survive the filter.

    "Real" now means an existing wiki page slug (`vault/wiki/<slug>.md`), not
    an INDEX topic header — see M1 fix 2026-04-24 (audit-1.md).
    """
    response = json.dumps({
        "topic": "memory",
        "summary": "Discusses temporal decay in episodic memory.",
        "entities": ["hippocampus"],
        "concepts": ["temporal decay"],
        "open_questions": [],
        "related_slugs": ["memory", "fake-slug"],
    })
    adapter = _StubAdapter(response_json=response)
    vault_root = tmp_path / "vault"
    librarian = _build_librarian(vault_root, adapter)

    # Seed a real wiki page so the `memory` related_slug has something to match.
    (vault_root / "wiki" / "memory.md").write_text(
        "---\ntitle: memory\nslug: memory\ntype: Source\n"
        "topic: general\nsource_path: raw/memory.md\ndate_added: 2026-04-24\n"
        "related_slugs: []\nopen_questions: []\nbacklinks_from: []\n---\n# memory\n",
        encoding="utf-8",
    )

    raw_rel = _stage_source(vault_root, _FIXTURE)
    page = await librarian.compile_source(raw_rel)

    assert page is not None
    assert page.related_slugs == ["memory"], (
        f"real slugs should survive, fake should drop, got {page.related_slugs}"
    )


async def test_empty_source_fallback(tmp_path) -> None:
    """Whitespace-only source → page lands with `topic: general` + sentinel summary; no adapter call."""
    adapter = _StubAdapter(response_json="{}")
    vault_root = tmp_path / "vault"
    librarian = _build_librarian(vault_root, adapter)

    empty = vault_root / "raw" / "blank.md"
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_text("   \n\n  \t  \n", encoding="utf-8")

    page = await librarian.compile_source("raw/blank.md")

    assert page is not None
    assert page.topic == "general"
    assert page.summary == "(no extractable text)"
    assert adapter.call_count == 0, "empty-source path must skip the adapter call"

    written = (vault_root / "wiki" / "blank.md").read_text(encoding="utf-8")
    assert "topic: general" in written
    assert "(no extractable text)" in written


async def test_wiki_page_frontmatter_has_slug_and_type(tmp_path) -> None:
    """Rendered frontmatter must include type: Source, slug:, related_slugs:, open_questions:, backlinks_from: []. lint_flags: must be absent."""
    response = json.dumps({
        "topic": "memory-decay",
        "summary": "Temporal decay in episodic memory.",
        "entities": ["hippocampus"],
        "concepts": ["consolidation", "rehearsal"],
        "open_questions": ["is decay rate constant across modalities?"],
        "related_slugs": [],
    })
    adapter = _StubAdapter(response_json=response)
    vault_root = tmp_path / "vault"
    librarian = _build_librarian(vault_root, adapter)

    raw_rel = _stage_source(vault_root, _FIXTURE)
    page = await librarian.compile_source(raw_rel)
    assert page is not None

    written = (vault_root / "wiki" / f"{page.slug}.md").read_text(encoding="utf-8")
    frontmatter, _, _body = written.partition("\n---\n")
    # frontmatter starts with "---" then key/value lines
    assert "type: Source" in frontmatter
    assert f"slug: {page.slug}" in frontmatter
    assert "related_slugs:" in frontmatter
    assert "open_questions:" in frontmatter
    assert "backlinks_from: []" in frontmatter
    assert "lint_flags" not in frontmatter, "lint_flags is Phase 5; must not appear in Phase 2 output"
