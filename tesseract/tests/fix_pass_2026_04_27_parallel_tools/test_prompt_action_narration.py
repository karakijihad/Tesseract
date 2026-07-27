"""Output contract rule (2026-04-29 follow-up): TARS must wrap every
text emission in `<intent>...</intent>` (status before/between tools) or
`<answer>...</answer>` (final reply). Replaces the prior
"action-narration" prose rule with a structured contract that the WS
parser can decode deterministically — see
`tesseract/mirror/server/ws.py::_parse_tagged_stream` and the
`fix_pass_2026_04_29_tagged_stream` suite.
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


def test_manifest_prompt_carries_output_contract(tmp_path: Path) -> None:
    ws, store = _minimal_workspace(tmp_path)
    prompt = assemble_system_prompt(workspace_dir=ws, memory_store_dir=store, mode="manifest")
    assert "# Output contract" in prompt, "output-contract section missing from manifest prompt"
    # Both tag pairs must appear so the model knows what to emit.
    assert "<intent>" in prompt and "</intent>" in prompt
    assert "<answer>" in prompt and "</answer>" in prompt
    # Chronological-order rule is load-bearing: an intent must precede every
    # operator-visible action — tool call, multi-step reasoning, or brainstorm.
    assert "before every action" in prompt.lower()
    assert "intent → action" in prompt
    assert "voice mode" in prompt.lower(), "voice-mode wording load-bearing — both surfaces are spoken aloud"


def test_full_prompt_carries_output_contract(tmp_path: Path) -> None:
    ws, store = _minimal_workspace(tmp_path)
    prompt = assemble_system_prompt(workspace_dir=ws, memory_store_dir=store, mode="full")
    assert "# Output contract" in prompt
    assert "<intent>" in prompt and "<answer>" in prompt
