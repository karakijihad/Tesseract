"""Manifest prompt must include the tool-use rule block so TARS stops
over-searching and leaving empty responses when the tool budget is exhausted.
"""

from __future__ import annotations

from pathlib import Path

from tesseract.brain.prompt import assemble_system_prompt


def test_manifest_prompt_carries_tool_use_rule(tmp_path: Path) -> None:
    # Minimal workspace — only AGENTS.md present so the assembly doesn't
    # short-circuit on "no sections". The rule is inlined directly from a
    # constant, so we don't need the real workspace tree.
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("# Agents\n\nplaceholder\n", encoding="utf-8")

    store = tmp_path / "memory-store"
    store.mkdir()

    prompt = assemble_system_prompt(workspace_dir=ws, memory_store_dir=store, mode="manifest")
    assert "# Tool use" in prompt, "tool-use rule section missing from manifest prompt"
    assert "purposefully" in prompt, "tool-use rule body drifted"
    assert "one targeted query" in prompt, "tool-use rule body drifted"
