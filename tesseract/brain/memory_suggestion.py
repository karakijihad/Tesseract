"""Typed `memory_suggestion` payload produced by the stateful observer.

`observer_reading` decodes the observer's reply, hands the suggestion half here
to be validated, and renders it for injection. The server serialises the result
via `to_envelope_data()` and streams it as the `data` field of a
`memory_suggestion` WS envelope.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Union, get_args

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


def collapse_to_one_line(raw: Any) -> str:
    """A model-written reason, flattened to one line and bounded.

    Every observer reason is rendered into a message that carries other
    objects beside it. Line breaks left in it are how model text stops being
    a value and starts looking like structure, so they are removed here, at
    the one point both roles pass through, rather than trusted to whatever
    renders them later.
    """
    return " ".join(str(raw).split())[:_REASON_MAX_CHARS].rstrip()


def suggestion_from_payload(payload: Any, fallback_observation_id: str) -> MemorySuggestion:
    """Validate one already-decoded suggestion object.

    Raises `KeyError`/`TypeError`/`ValueError` on anything malformed. The
    caller decides what a malformed one costs; `observer_reading` drops it
    and logs, which is the only policy in the runtime.
    """
    if not isinstance(payload, dict):
        raise TypeError(f"suggestion must be an object, got {type(payload).__name__}")
    kind = payload["kind"]
    if kind not in _VALID_KINDS:
        raise ValueError(f"kind must be one of {sorted(_VALID_KINDS)}, got {kind!r}")
    target = _parse_target(payload["target"])
    reason = collapse_to_one_line(payload["reason"])
    if not reason:
        raise ValueError("reason is empty")
    confidence = float(payload["confidence"])
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence out of range: {confidence}")
    observation_id = str(payload.get("observation_id") or fallback_observation_id).strip() or fallback_observation_id

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


def format_target(t: MemoryTarget) -> str:
    if isinstance(t, MemoryPath):
        return f'memory_path = "{t.path}"'
    if isinstance(t, TopicSlug):
        return f'topic_slug = "{t.slug}"'
    if isinstance(t, Quote):
        preview = t.text if len(t.text) <= 120 else t.text[:117] + "..."
        return f'quote @ turn {t.turn_index}: "{preview}"'
    raise TypeError(f"unknown MemoryTarget: {type(t).__name__}")


def strip_code_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()
