"""Batched vet-prompt builder — the assistant's proposal quality gate."""

from __future__ import annotations

from typing import Any

_GOAL_CAP = 200
_RATIONALE_CAP = 300

_SCHEMA = (
    '{"verdicts": ['
    '{"id": "<id>", "verdict": "promote|reject|merge", '
    '"score": <0-1>, "reason": "<short>", "merge_into": "<id or null>"}'
    "]}"
)


def build_vet_prompt(items: list[dict[str, Any]]) -> str:
    """Build the batched vet prompt for ``items`` (each
    ``{id, source, goal, rationale}``). Returns a single prompt string."""
    parts: list[str] = [
        "You are the assistant's proposal quality gate.",
        "",
        "For each candidate agenda item below, decide exactly one verdict:",
        "- promote: clearly useful and actionable — worth the operator's time.",
        "- reject: vague, noise, or not worth operator time.",
        "- merge: a near-duplicate of another item IN THIS BATCH — set",
        "  merge_into to that other item's id.",
        "",
        "Return one verdict per input id. Return STRICT JSON, no preamble:",
        _SCHEMA,
        "score is 0-1 usefulness.",
        "",
        "--- CANDIDATES ---",
    ]
    for item in items:
        goal = str(item.get("goal", ""))[:_GOAL_CAP]
        rationale = str(item.get("rationale", ""))[:_RATIONALE_CAP]
        parts.append(
            f"- id={item.get('id', '')} source={item.get('source', '')}\n"
            f"    goal: {goal}\n"
            f"    rationale: {rationale}"
        )
    parts.extend(["", "Return the JSON object now."])
    return "\n".join(parts)


__all__ = ["build_vet_prompt"]
