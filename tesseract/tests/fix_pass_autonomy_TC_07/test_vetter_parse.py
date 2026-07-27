"""Task 2B Part B — vetter response parsing."""

from __future__ import annotations

import json

from tesseract.orchestrator.autonomy.vetter.parse import (
    VetVerdict,
    parse_vet_response,
)


def test_valid_batch_parses() -> None:
    raw = json.dumps(
        {"verdicts": [{"id": "ag-1", "verdict": "promote", "score": 0.9, "reason": "useful"}]}
    )
    result = parse_vet_response(raw)
    assert len(result.verdicts) == 1
    v = result.verdicts[0]
    assert v.id == "ag-1"
    assert v.verdict == VetVerdict.PROMOTE
    assert v.score == 0.9


def test_prose_wrapped_json_parses() -> None:
    raw = (
        "Sure, here you go:\n"
        '{"verdicts": [{"id": "ag-2", "verdict": "reject", "score": 0.1}]}\n'
        "Hope that helps."
    )
    result = parse_vet_response(raw)
    assert len(result.verdicts) == 1
    assert result.verdicts[0].id == "ag-2"
    assert result.verdicts[0].verdict == VetVerdict.REJECT


def test_bad_verdict_dropped_rest_kept() -> None:
    raw = json.dumps(
        {
            "verdicts": [
                {"id": "ag-3", "verdict": "not_a_real_verdict", "score": 0.5},
                {"id": "ag-4", "verdict": "reject", "score": 0.2},
            ]
        }
    )
    result = parse_vet_response(raw)
    assert [v.id for v in result.verdicts] == ["ag-4"]


def test_garbage_returns_empty() -> None:
    assert parse_vet_response("not json at all").verdicts == []
    assert parse_vet_response("").verdicts == []
    assert parse_vet_response("   ").verdicts == []


def test_merge_without_target_downgraded_to_reject() -> None:
    raw = json.dumps(
        {"verdicts": [{"id": "ag-5", "verdict": "merge", "score": 0.5, "reason": "dup"}]}
    )
    result = parse_vet_response(raw)
    assert len(result.verdicts) == 1
    assert result.verdicts[0].verdict == VetVerdict.REJECT
    assert result.verdicts[0].merge_into is None
