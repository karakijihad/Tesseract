"""C2 regression — concurrent `compile_source` must not drop hub backlinks.

Pre-fix, two `compile_source` calls targeting the same hub raced on the
read-modify-write of `backlinks_from`: the second atomic swap clobbered
the first's append. The fix serializes via `VaultLibrarian._backlinks_lock`.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from tesseract.brain.boot import VaultConfig
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
class _SlowStubAdapter:
    """Duck-typed adapter that yields control mid-call so two ingests can
    interleave their read-modify-write on the shared hub."""

    response_json: str

    async def generate(self, prompt: str, options: AdapterOptions) -> str:
        await asyncio.sleep(0)  # force a scheduling point for the race
        return self.response_json

    async def stream(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError


def _build_librarian(vault_root: Path, adapter: ModelAdapter) -> VaultLibrarian:
    (vault_root / "raw").mkdir(parents=True, exist_ok=True)
    (vault_root / "wiki").mkdir(parents=True, exist_ok=True)
    manager = VaultManager(vault_root=vault_root)
    manager.seed_wiki_skeleton()
    return VaultLibrarian(
        vault_manager=manager,
        adapter=adapter,
        adapter_options=AdapterOptions(),
        config=_DEFAULT_VAULT_CONFIG,
        agents_dir=None,
    )


def _seed_hub(vault_root: Path, slug: str) -> Path:
    dst = vault_root / "wiki" / f"{slug}.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(
        "---\n"
        f"title: {slug.replace('-', ' ').title()}\n"
        "type: Concept\n"
        f"slug: {slug}\n"
        "entity_types:\n"
        "  - Concept\n"
        "date_added: 2026-04-24\n"
        "backlinks_from: []\n"
        "---\n\n"
        f"# {slug.replace('-', ' ').title()}\n",
        encoding="utf-8",
    )
    return dst


def _stage_as(vault_root: Path, fixture: Path, rename_to: str) -> str:
    dst = vault_root / "raw" / rename_to
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(fixture), str(dst))
    return f"raw/{rename_to}"


async def test_concurrent_compile_source_preserves_both_backlinks(tmp_path) -> None:
    """Two raw files ingested concurrently into the same hub both land in
    `backlinks_from`."""
    vault_root = tmp_path / "vault"
    hub_path = _seed_hub(vault_root, "system-dynamics")

    librarian = _build_librarian(
        vault_root,
        _SlowStubAdapter(response_json=json.dumps({
            "topic": "memory",
            "summary": "Discusses temporal decay.",
            "entities": [],
            "concepts": ["system dynamics"],
            "open_questions": [],
            "related_slugs": [],
        })),
    )

    raw_a = _stage_as(vault_root, _FIXTURE, "decay-a.md")
    raw_b = _stage_as(vault_root, _FIXTURE, "decay-b.md")

    page_a, page_b = await asyncio.gather(
        librarian.compile_source(raw_a),
        librarian.compile_source(raw_b),
    )

    assert page_a is not None and page_b is not None
    fm = VaultManager(vault_root).read_wiki_page_frontmatter("system-dynamics")
    backlinks = list(fm.get("backlinks_from") or [])
    assert sorted(backlinks) == sorted([page_a.slug, page_b.slug]), (
        f"both source slugs must land; got {backlinks!r}"
    )
