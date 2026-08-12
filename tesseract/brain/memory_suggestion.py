"""Typed `memory_suggestion` payload produced by the stateful observer.

The observer parses its LLM output into a `MemorySuggestion`; the server
serialises it via `to_envelope_data()` and streams it as the `data` field
of a `memory_suggestion` WS envelope. `ChatSession` re-uses
`format_for_injection()` to render the same payload as a synthetic
user-message at the top of the next turn.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Union, get_args

logger = logging.getLogger(__name__)

SuggestionKind = Literal["remember", "consolidate", "reread"]
_VALID_KINDS: frozenset[str] = frozenset(get_args(SuggestionKind))
_REASON_MAX_CHARS = 180


@dataclass(frozen=True)
class MemoryPath:
    path: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "memory_path", "path": self.path}


@dataclass(frozen=True)
class TopicSlug:
    slug: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "topic_slug", "slug": self.slug}


@dataclass(frozen=True)
class Quote:
    turn_index: int
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "quote", "turn_index": self.turn_index, "text": self.text}


MemoryTarget = Union[MemoryPath, TopicSlug, Quote]


@dataclass(frozen=True)
class MemorySuggestion:
    kind: SuggestionKind
    target: MemoryTarget
    reason: str
    confidence: float
    observation_id: str


def next_observation_id() -> str:
    """`obs_YYYYMMDD_HHMMSS_<4hex>` — stable, monotonic-ish, cheap."""
    now = datetime.now(timezone.utc)
    suffix = secrets.token_hex(2)
    return f"obs_{now.strftime('%Y%m%d_%H%M%S')}_{suffix}"


def to_envelope_data(s: MemorySuggestion) -> dict[str, Any]:
    return {
        "kind": s.kind,
        "target": s.target.to_dict(),
        "reason": s.reason,
        "confidence": s.confidence,
        "observation_id": s.observation_id,
    }


def format_for_injection(s: MemorySuggestion) -> str:
    """Render a suggestion as the `[observer_suggestion]` text block
    ChatSession injects as a synthetic user message. Format is the one
    fixed in `_shared/memory-suggestion-envelope.md` § "Injection into
    The assistant turn loop"."""
    target_line = _format_target(s.target)
    return (
        "[observer_suggestion]\n"
        f"kind: {s.kind}\n"
        f"target: {target_line}\n"
        f"reason: {s.reason}\n"
        f"confidence: {s.confidence:.2f}\n"
        f"observation_id: {s.observation_id}"
    )


def parse_suggestion(raw: str, fallback_observation_id: str) -> MemorySuggestion | None:
    """Parse adapter output into a `MemorySuggestion`.

    `NONE` (with optional trailing punctuation) yields `None`. JSON parse
    or schema-validation failure yields `None` and logs at WARNING.
    """
    text = raw.strip()
    if not text:
        return None
    if text.upper().rstrip(".!") == "NONE":
        return None
    text = _strip_code_fence(text)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("memory_suggestion JSON parse failed: %s | raw=%r", exc, raw[:200])
        return None
    if not isinstance(payload, dict):
        logger.warning("memory_suggestion payload not a dict: %r", payload)
        return None

    try:
        kind = payload["kind"]
        if kind not in _VALID_KINDS:
            raise ValueError(f"kind must be one of {sorted(_VALID_KINDS)}, got {kind!r}")
        target = _parse_target(payload["target"])
        reason = str(payload["reason"]).strip()
        if not reason:
            raise ValueError("reason is empty")
        if len(reason) > _REASON_MAX_CHARS:
            reason = reason[:_REASON_MAX_CHARS].rstrip()
        confidence = float(payload["confidence"])
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"confidence out of range: {confidence}")
        observation_id = str(payload.get("observation_id") or fallback_observation_id).strip() or fallback_observation_id
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("memory_suggestion schema invalid: %s | payload=%r", exc, payload)
        return None

    return MemorySuggestion(
        kind=kind,
        target=target,
        reason=reason,
        confidence=confidence,
        observation_id=observation_id,
    )


SCHEMA_FOR_PROMPT = """{
  "kind": "remember" | "consolidate" | "reread",
  "target":
    | { "kind": "memory_path", "path": "<path/to/memory.md>" }
    | { "kind": "topic_slug",  "slug": "<short-kebab-slug>" }
    | { "kind": "quote",       "turn_index": <int>, "text": "<verbatim snippet>" },
  "reason": "<= 180 chars, one sentence",
  "confidence": 0.0-1.0,
  "observation_id": "obs_YYYYMMDD_HHMMSS_<4hex>"
}"""


def _parse_target(raw: Any) -> MemoryTarget:
    if not isinstance(raw, dict):
        raise ValueError(f"target must be an object, got {type(raw).__name__}")
    kind = raw.get("kind")
    if kind == "memory_path":
        path = str(raw["path"]).strip()
        if not path:
            raise ValueError("memory_path.path is empty")
        return MemoryPath(path=path)
    if kind == "topic_slug":
        slug = str(raw["slug"]).strip()
        if not slug:
            raise ValueError("topic_slug.slug is empty")
        return TopicSlug(slug=slug)
    if kind == "quote":
        text = str(raw["text"])
        if not text.strip():
            raise ValueError("quote.text is empty")
        return Quote(turn_index=int(raw["turn_index"]), text=text)
    raise ValueError(f"unknown target.kind: {kind!r}")


def _format_target(t: MemoryTarget) -> str:
    if isinstance(t, MemoryPath):
        return f'memory_path = "{t.path}"'
    if isinstance(t, TopicSlug):
        return f'topic_slug = "{t.slug}"'
    if isinstance(t, Quote):
        preview = t.text if len(t.text) <= 120 else t.text[:117] + "..."
        return f'quote @ turn {t.turn_index}: "{preview}"'
    raise TypeError(f"unknown MemoryTarget: {type(t).__name__}")


def _strip_code_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()
