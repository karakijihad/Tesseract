"""M1 regression — `related_slugs` must be validated against wiki page slugs,
not INDEX topic headers.

Pre-fix, `vault_librarian.py` filtered LLM-proposed `related_slugs` against
slugified INDEX topic headers (a distinct namespace), so every legitimate
page-to-page edge was dropped at ingest.
"""

from __future__ import annotations

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
class _StubAdapter:
    response_json: str

    async def generate(self, prompt: str, options: AdapterOptions) -> str:
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


def _seed_page(vault_root: Path, slug: str) -> Path:
    dst = vault_root / "wiki" / f"{slug}.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(
        "---\n"
        f"title: {slug}\n"
        "type: Source\n"
        f"slug: {slug}\n"
        "topic: general\n"
        "source_path: raw/sample.md\n"
        "date_added: 2026-04-24\n"
        "related_slugs: []\n"
        "open_questions: []\n"
        "backlinks_from: []\n"
        "---\n"
        f"# {slug}\n",
        encoding="utf-8",
    )
    return dst


def _stage(vault_root: Path, rename: str) -> str:
    dst = vault_root / "raw" / rename
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(_FIXTURE), str(dst))
    return f"raw/{rename}"


async def test_related_slug_is_kept_when_page_exists(tmp_path) -> None:
    """An LLM-proposed `related_slug` that matches a real wiki page survives."""
    vault_root = tmp_path / "vault"
    _seed_page(vault_root, "existing-page")

    librarian = _build_librarian(
        vault_root,
        _StubAdapter(response_json=json.dumps({
            "topic": "memory",
            "summary": "Decay notes.",
            "entities": [],
            "concepts": [],
            "open_questions": [],
            "related_slugs": ["existing-page"],
        })),
    )

    raw = _stage(vault_root, "decay.md")
    page = await librarian.compile_source(raw)

    assert page is not None
    assert page.related_slugs == ["existing-page"], (
        f"valid related slug must be preserved; got {page.related_slugs!r}"
    )


async def test_related_slug_is_dropped_when_page_missing(tmp_path) -> None:
    """An LLM hallucinated slug with no matching wiki page is filtered out."""
    vault_root = tmp_path / "vault"

    librarian = _build_librarian(
        vault_root,
        _StubAdapter(response_json=json.dumps({
            "topic": "memory",
            "summary": "Decay notes.",
            "entities": [],
            "concepts": [],
            "open_questions": [],
            "related_slugs": ["hallucinated-page", "also-not-real"],
        })),
    )

    raw = _stage(vault_root, "decay.md")
    page = await librarian.compile_source(raw)

    assert page is not None
    assert page.related_slugs == [], (
        f"non-existent slugs must be dropped; got {page.related_slugs!r}"
    )
