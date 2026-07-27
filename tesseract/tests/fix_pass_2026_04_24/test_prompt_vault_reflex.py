"""Manifest prompt must carry the vault-reflex rule so TARS reaches for
`vault_query` / `vault_search` before answering from memory on recall
questions. The rule ships every turn in both manifest and full modes.
"""

from __future__ import annotations

from pathlib import Path

from tesseract.brain.prompt import assemble_system_prompt


def _minimal_workspace(tmp_path: Path) -> tuple[Path, Path]:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("# Agents\n\nplaceholder\n", encoding="utf-8")
    store = tmp_path / "memory-store"
    store.mkdir()
    return ws, store


def test_manifest_prompt_carries_vault_reflex(tmp_path: Path) -> None:
    ws, store = _minimal_workspace(tmp_path)
    prompt = assemble_system_prompt(workspace_dir=ws, memory_store_dir=store, mode="manifest")
    assert "# Vault reflex" in prompt, "vault-reflex section missing from manifest prompt"
    assert "vault_query" in prompt, "vault_query not mentioned in reflex rule"
    assert "vault_search" in prompt, "vault_search not mentioned in reflex rule"
    assert "authoritative recall surface" in prompt, "reflex rule body drifted"


def test_full_prompt_carries_vault_reflex(tmp_path: Path) -> None:
    ws, store = _minimal_workspace(tmp_path)
    prompt = assemble_system_prompt(workspace_dir=ws, memory_store_dir=store, mode="full")
    assert "# Vault reflex" in prompt, "vault-reflex section missing from full prompt"
    assert "vault_query" in prompt
    assert "vault_search" in prompt
