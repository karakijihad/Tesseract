"""audit-1 i4 — vault containment + empty-source fallback regression tests.

Two code paths that previously had zero coverage:

  1. `VaultContainmentError` — `compile_source` must reject any
     `raw_rel_path` that escapes `vault/raw/` (path traversal guard).
  2. Empty-source fallback — when `VaultIndexer.extract_text()` yields
     nothing, the librarian writes a wiki page with a sentinel
     topic/summary *without* calling the adapter, and updates INDEX.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from tesseract.brain.boot import VaultConfig
from tesseract.kernel.adapters.base import AdapterOptions
from tesseract.memory.vault_librarian import VaultContainmentError, VaultLibrarian
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


@dataclass
class _NeverCalledAdapter:
    """Adapter that fails the test if the empty-source path ever invokes it."""

    calls: int = 0

    async def generate(self, prompt: str, options: AdapterOptions) -> str:
        self.calls += 1
        raise AssertionError("empty-source fallback must not call the adapter")

    async def stream(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError


def _build_librarian(vault_root: Path, adapter) -> VaultLibrarian:
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


# ── VaultContainmentError ─────────────────────────────────


async def test_compile_source_rejects_path_traversal(tmp_path: Path) -> None:
    """A raw_rel_path that escapes vault/raw/ via `..` must raise containment."""
    vault = tmp_path / "vault"
    librarian = _build_librarian(vault, adapter=_NeverCalledAdapter())

    # Stage a sentinel file *outside* vault/raw/ to ensure the guard fires on
    # the path math, not on a missing-file short-circuit.
    outside = tmp_path / "secret.md"
    outside.write_text("# leaked", encoding="utf-8")

    with pytest.raises(VaultContainmentError):
        await librarian.compile_source("raw/../secret.md")


async def test_compile_source_rejects_absolute_path(tmp_path: Path) -> None:
    """Absolute paths are rejected — `vault/raw/` is the only permitted root."""
    vault = tmp_path / "vault"
    librarian = _build_librarian(vault, adapter=_NeverCalledAdapter())

    with pytest.raises(VaultContainmentError):
        await librarian.compile_source(str(tmp_path / "outside.md"))


# ── Empty-source fallback ────────────────────────────────


async def test_compile_source_empty_file_writes_fallback_page(tmp_path: Path) -> None:
    """Zero-byte raw file → fallback wiki page with sentinel summary, no LLM call."""
    vault = tmp_path / "vault"
    adapter = _NeverCalledAdapter()
    librarian = _build_librarian(vault, adapter=adapter)

    raw_path = vault / "raw" / "empty-file.md"
    raw_path.write_text("", encoding="utf-8")

    page = await librarian.compile_source("raw/empty-file.md")

    assert page is not None, "empty-source fallback must return a page, not None"
    assert page.slug == "empty-file"
    assert page.topic == "general"
    # Sentinel summary — the librarian uses a fixed "(no extractable text)" body.
    assert "no extractable text" in page.summary.lower()
    assert adapter.calls == 0

    wiki_path = vault / "wiki" / "empty-file.md"
    assert wiki_path.exists(), "empty-source fallback must still persist the wiki page"
    content = wiki_path.read_text(encoding="utf-8")
    assert "topic: general" in content

    # INDEX.md must have the new slug listed so lint and query both see it.
    index = (vault / "wiki" / "INDEX.md").read_text(encoding="utf-8")
    assert "empty-file" in index


async def test_compile_source_whitespace_only_source_uses_fallback(tmp_path: Path) -> None:
    """A file with only whitespace must route through the same fallback path."""
    vault = tmp_path / "vault"
    adapter = _NeverCalledAdapter()
    librarian = _build_librarian(vault, adapter=adapter)

    raw_path = vault / "raw" / "whitespace-only.md"
    raw_path.write_text("   \n\t\n   ", encoding="utf-8")

    page = await librarian.compile_source("raw/whitespace-only.md")

    assert page is not None
    assert page.topic == "general"
    assert adapter.calls == 0
