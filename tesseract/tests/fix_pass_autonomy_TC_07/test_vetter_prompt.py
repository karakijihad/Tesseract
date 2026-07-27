"""Task 2B Part C — vet-prompt builder."""

from __future__ import annotations

from tesseract.orchestrator.autonomy.vetter.prompt import build_vet_prompt


def test_prompt_contains_ids_and_goals() -> None:
    items = [
        {"id": "ag-1", "source": "observer", "goal": "doe goal one", "rationale": "doe rationale one"},
        {"id": "ag-2", "source": "channel", "goal": "doe goal two", "rationale": "doe rationale two"},
    ]
    prompt = build_vet_prompt(items)
    assert isinstance(prompt, str)
    for item in items:
        assert item["id"] in prompt
        assert item["goal"] in prompt


def test_prompt_contains_json_schema() -> None:
    prompt = build_vet_prompt([{"id": "ag-1", "source": "observer", "goal": "g", "rationale": "r"}])
    assert '"verdicts"' in prompt
    assert '"merge_into"' in prompt
    assert "promote" in prompt and "reject" in prompt and "merge" in prompt
