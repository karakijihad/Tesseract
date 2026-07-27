"""Vault-librarian rewire (phase 1) — wiring regression tests.

Scope:
- `vault_ingest` is registered by `build_tool_registry()`.
- `VaultLibrarian` is constructed with an adapter (when a chat_brain provider's
  API key is present) or `None` (graceful degrade).
- `compile_source` on a None-adapter librarian is a no-op (no crash, returns None).

Network calls are not exercised here — this is pure plumbing.
"""

from __future__ import annotations

import os

import pytest

from tesseract.brain.boot import VaultConfig, build_tool_registry
from tesseract.kernel.adapters.base import AdapterOptions
from tesseract.memory.vault_librarian import VaultLibrarian
from tesseract.memory.vault_manager import VaultManager


_TEST_VAULT_CONFIG = VaultConfig(
    max_extract_chars=3000,
    scale_split_threshold=80,
    stale_grace_days=180,
    contradiction_pair_limit=50,
    max_seed_slugs=6,
    max_expanded_slugs=12,
    search_rrf_k=60,
    search_default_top_k=5,
)


def _has_chat_brain_key() -> bool:
    if os.environ.get("OPENAI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return True
    # CLI tier (codex / claude) is also a valid chat_brain provider — the
    # librarian's adapter resolves through it when no API key is present
    # but the operator has codex.cmd / claude on PATH.
    import shutil
    if (
        shutil.which("codex")
        or shutil.which("codex.cmd")
        or shutil.which("claude")
        or shutil.which("claude.cmd")
    ):
        return True
    return False


def test_vault_ingest_registered() -> None:
    registry, _mood, _voice, _bundle, _alarms = build_tool_registry()
    assert "vault_ingest" in registry.tools
    assert "vault_query" in registry.tools
    assert "vault_search" in registry.tools


def test_vault_librarian_adapter_reflects_env() -> None:
    """Librarian adapter is wired when any chat_brain provider has a key in env."""
    registry, _mood, _voice, _bundle, _alarms = build_tool_registry()
    ingest = registry.tools["vault_ingest"]
    librarian = ingest._librarian  # type: ignore[attr-defined]
    assert librarian is not None
    if _has_chat_brain_key():
        assert librarian._adapter is not None, "adapter should be live with API key present"  # type: ignore[attr-defined]
    else:
        assert librarian._adapter is None, "adapter should be None without any chat_brain key"  # type: ignore[attr-defined]


async def test_compile_source_noop_on_missing_adapter(tmp_path) -> None:
    """A librarian with adapter=None must not crash on compile_source."""
    vault_root = tmp_path / "vault"
    (vault_root / "raw").mkdir(parents=True)
    (vault_root / "wiki").mkdir()
    manager = VaultManager(vault_root=vault_root)
    librarian = VaultLibrarian(
        vault_manager=manager,
        adapter=None,
        adapter_options=AdapterOptions(),
        config=_TEST_VAULT_CONFIG,
        agents_dir=None,
    )
    result = await librarian.compile_source("raw/does-not-exist.md")
    assert result is None
