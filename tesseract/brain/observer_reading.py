"""What the observer reads back from one call, decoded.

The observer sends one request per turn and the reply may carry two
independent things: a memory suggestion, which is what to keep, and a boundary
nudge, which is whether this conversation should stop and hand its work over.
They are parsed together because they arrive together. One call, one billing
row: a second model call for the second signal is what GOVERNANCE §3 exists to
stop.

Either half may be absent, and a malformed half is dropped without taking the
other with it. Nothing here decides anything. The nudge is a recommendation the
agent may refuse, per the owner's document §24 and Invariant 6; the runtime
still owns the hard boundary.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal, get_args

from tesseract.brain.memory_suggestion import (
    MemorySuggestion,
    collapse_to_one_line,
    format_target,
    strip_code_fence,
    suggestion_from_payload,
)

logger = logging.getLogger(__name__)

#: The two answers a boundary can be recommended toward. These are the values
#: of `chat.Continuation`, repeated rather than imported because `chat` imports
#: this module to render a nudge and the reverse import would close a cycle.
#: `tests/cost_and_continuity_CC_10` pins the two vocabularies together so the
#: repetition cannot drift into a disagreement.
NudgeRecommendation = Literal["continue", "reset"]
_VALID_RECOMMENDATIONS: frozenset[str] = frozenset(get_args(NudgeRecommendation))



@dataclass(frozen=True)
class BoundaryNudge:
    """The observer's read on whether a boundary is due, and which answer fits.

    A nudge exists only when one looks due. There is no third value saying
    "carry on": that is the absence of a nudge, the same way carrying on is the
    absence of a decision rather than a mode of consolidating.
    """

    recommendation: NudgeRecommendation
    reason: str
    observation_id: str


@dataclass(frozen=True)
class ObserverReading:
    suggestion: MemorySuggestion | None = None
    nudge: BoundaryNudge | None = None


EMPTY_READING = ObserverReading()


def format_for_injection(signal: MemorySuggestion | BoundaryNudge) -> str:
    """One JSON object per observer signal, whichever role it came from.

    Two fields carry the routing and they are not the same question.
    `observer` names the ROLE, which is what an agent sorts by: a `boundary`
    is about whether this conversation should go on at all and outranks a
    `memory`, which is housekeeping. `tag` names the specific act inside that
    role. An agent that reads only `observer` still knows what to do first.

    JSON rather than a labelled block because these travel together in one
    message and a reader has to tell where one ends and the next begins.
    Every value is machine-written except the reasons, which are model text
    with their line breaks collapsed at parse time so a reason cannot forge
    a sibling object.
    """
    if isinstance(signal, BoundaryNudge):
        payload: dict[str, Any] = {
            "observer": "boundary",
            "id": signal.observation_id,
            "tag": signal.recommendation,
            "reason": signal.reason,
        }
    else:
        payload = {
            "observer": "memory",
            "id": signal.observation_id,
            "tag": signal.kind,
            "target": format_target(signal.target),
            "reason": signal.reason,
            "confidence": round(signal.confidence, 2),
        }
    return json.dumps(payload, ensure_ascii=False)


def to_envelope_data(n: BoundaryNudge) -> dict[str, Any]:
    return {
        "recommendation": n.recommendation,
        "reason": n.reason,
        "observation_id": n.observation_id,
    }


def parse_reading(raw: str, fallback_observation_id: str) -> ObserverReading:
    """Decode the observer's reply into whichever halves survived.

    `NONE`, empty text, undecodable JSON and a non-object payload all yield an
    empty reading. A half that fails validation is dropped and logged at
    WARNING; the other half is still returned, because one bad field is not a
    reason to throw away a good observation.
    """
    text = raw.strip()
    if not text:
        return EMPTY_READING
    if text.upper().rstrip(".!") == "NONE":
        return EMPTY_READING
    text = strip_code_fence(text)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("observer reading JSON parse failed: %s | raw=%r", exc, raw[:200])
        return EMPTY_READING
    if not isinstance(payload, dict):
        logger.warning("observer reading payload not a dict: %r", payload)
        return EMPTY_READING

    # Two accepted shapes. The envelope carries both halves under their own
    # keys; a bare object is a suggestion on its own, which is what the prompt
    # asked for before this phase and what a model falls back to when it
    # answers from habit rather than from the schema in front of it.
    if "suggestion" in payload or "nudge" in payload:
        suggestion_payload = payload.get("suggestion")
        nudge_payload = payload.get("nudge")
    else:
        suggestion_payload = payload
        nudge_payload = None
        if "recommendation" in payload:
            # A reply that meant to nudge and put the fields at the top level
            # instead of under `nudge`. The suggestion half still parses, so
            # this would otherwise be a silent loss with nothing to find later.
            logger.warning(
                "observer reply carried a recommendation outside the nudge "
                "envelope, so the nudge was dropped: %r",
                payload.get("recommendation"),
            )

    return ObserverReading(
        suggestion=_suggestion_or_none(suggestion_payload, fallback_observation_id),
        nudge=_nudge_or_none(nudge_payload, fallback_observation_id),
    )


READING_SCHEMA_FOR_PROMPT = """{
  "suggestion": null | {
    "kind": "remember" | "consolidate" | "reread",
    "target":
      | { "kind": "memory_path", "path": "<path/to/memory.md>" }
      | { "kind": "topic_slug",  "slug": "<short-kebab-slug>" }
      | { "kind": "quote",       "turn_index": <int>, "text": "<verbatim snippet>" },
    "reason": "<= 180 chars, one sentence",
    "confidence": 0.0-1.0,
    "observation_id": "obs_YYYYMMDD_HHMMSS_<4hex>"
  },
  "nudge": null | {
    "recommendation": "continue" | "reset",
    "reason": "<= 180 chars, one sentence naming what you saw"
  }
}"""


def _suggestion_or_none(payload: Any, fallback_observation_id: str) -> MemorySuggestion | None:
    if payload is None:
        return None
    try:
        return suggestion_from_payload(payload, fallback_observation_id)
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("observer suggestion invalid, dropped: %s | payload=%r", exc, payload)
        return None


def _nudge_or_none(payload: Any, fallback_observation_id: str) -> BoundaryNudge | None:
    if payload is None:
        return None
    try:
        if not isinstance(payload, dict):
            raise TypeError(f"nudge must be an object, got {type(payload).__name__}")
        recommendation = payload["recommendation"]
        if recommendation not in _VALID_RECOMMENDATIONS:
            raise ValueError(
                f"recommendation must be one of {sorted(_VALID_RECOMMENDATIONS)}, "
                f"got {recommendation!r}"
            )
        reason = collapse_to_one_line(payload["reason"])
        if not reason:
            raise ValueError("reason is empty")
        observation_id = str(
            payload.get("observation_id") or fallback_observation_id
        ).strip() or fallback_observation_id
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("observer nudge invalid, dropped: %s | payload=%r", exc, payload)
        return None

    return BoundaryNudge(
        recommendation=recommendation,
        reason=reason,
        observation_id=observation_id,
    )
