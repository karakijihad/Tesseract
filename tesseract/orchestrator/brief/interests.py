"""Operator interest-affinity profile for the daily-brief world section.

The profile is a per-pillar map of ``topic_keyword -> weight``. Weights
drift up when the operator engages with a card (INTERESTED / DIG_DEEPER
/ COMMENTED) and down when they dismiss it (NOT_FOR_ME). World-digest
calls :func:`score_url` to rank candidate Tavily results by summed
overlap of the keyword vocabulary against the result's title + summary.

File layout::

    <TESSERACT_HOME>/memory-store/interests/profile.yaml

Schema (Pydantic v2)::

    pillars:
      tech:
        "local-first software": 2.5
        "AI safety": -1.0
      science: {}
      politics: {}
    last_decay_at: "2026-05-14T00:00:00+00:00"

Decay runs once per day from a scheduler tick (added in MO-9-14 once
feedback signals exist); the implementation is here so MO-9-13's
substrate ships complete.

Bounded weights — every signal is clamped to ``[-WEIGHT_CLAMP,
+WEIGHT_CLAMP]`` so a runaway signal stream cannot make one topic
dominate the brief forever. Cumulative score behaviour is therefore
asymptotic, not unbounded.
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import ClassVar

import yaml
from pydantic import BaseModel, Field

from tesseract.paths import TESSERACT_DIR, TESSERACT_HOME


WEIGHT_CLAMP: float = 10.0
DEFAULT_HALF_LIFE_DAYS: int = 30


class Signal(str, Enum):
    INTERESTED = "INTERESTED"
    NOT_FOR_ME = "NOT_FOR_ME"
    DIG_DEEPER = "DIG_DEEPER"
    COMMENTED = "COMMENTED"


SIGNAL_WEIGHTS: dict[Signal, float] = {
    Signal.INTERESTED: +1.0,
    Signal.NOT_FOR_ME: -1.0,
    Signal.DIG_DEEPER: +0.5,
    Signal.COMMENTED: +0.25,
}


class InterestsProfile(BaseModel):
    """Operator's per-pillar topic affinity table.

    Keys in ``pillars`` are pillar names (``tech`` / ``science`` /
    ``politics``); values are flat ``topic -> weight`` maps. Topics are
    free-form short strings — typically a 2-4 word phrase the operator
    cared about (the source is either an explicit signal payload from
    the brief UI or, in a future phase, an LLM tag pass over engaged
    cards). Substring matching in :func:`score_url` means a topic of
    ``"local-first"`` matches any title containing the literal phrase
    case-insensitively.

    Empty-defaults on first read — :func:`load_profile` creates the
    file with zero state if it is missing so callers do not branch on
    "first-run" vs "loaded".
    """

    DEFAULT_PILLAR_NAMES: ClassVar[tuple[str, ...]] = ("tech", "science", "politics")

    pillars: dict[str, dict[str, float]] = Field(default_factory=dict)
    last_decay_at: str = ""

    def ensure_pillars(self, names: tuple[str, ...]) -> "InterestsProfile":
        """Return a profile with every pillar in ``names`` present.

        Idempotent — existing pillar maps are preserved untouched; only
        missing keys get an empty dict. Used by :func:`load_profile` to
        guarantee callers always see the full pillar set even if the
        on-disk file predates a new pillar.
        """
        merged = dict(self.pillars)
        for name in names:
            merged.setdefault(name, {})
        if merged == self.pillars:
            return self
        return self.model_copy(update={"pillars": merged})


def _profile_path_default() -> Path:
    """Resolve the profile path at call time.

    Resolves ``TESSERACT_HOME`` via the env var first and falls back to
    the import-time constant only when unset — matches the
    ``_person_record`` pattern (see MO-9-12 audit memo-1).
    """
    home = Path(os.environ.get("TESSERACT_HOME") or TESSERACT_HOME).resolve()
    if not home or str(home) == "":
        home = TESSERACT_DIR.resolve()
    return home / "memory-store" / "interests" / "profile.yaml"


def load_profile(path: Path | None = None) -> InterestsProfile:
    """Load the interest profile from ``path`` (or the default location).

    First-read semantics: if the file does not exist, return a
    zero-state ``InterestsProfile`` with every default pillar present.
    Malformed YAML or wrong-shape contents fall back to zero-state too
    — the world-digester must never crash because the operator hand-
    edited the file into an invalid state.
    """
    target = Path(path) if path is not None else _profile_path_default()
    if not target.exists():
        return InterestsProfile().ensure_pillars(InterestsProfile.DEFAULT_PILLAR_NAMES)
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return InterestsProfile().ensure_pillars(InterestsProfile.DEFAULT_PILLAR_NAMES)
    if not isinstance(raw, dict):
        return InterestsProfile().ensure_pillars(InterestsProfile.DEFAULT_PILLAR_NAMES)
    pillars_raw = raw.get("pillars") or {}
    pillars: dict[str, dict[str, float]] = {}
    if isinstance(pillars_raw, dict):
        for pillar, topics in pillars_raw.items():
            if not isinstance(topics, dict):
                continue
            kept: dict[str, float] = {}
            for topic, weight in topics.items():
                if not isinstance(topic, str) or not topic.strip():
                    continue
                try:
                    kept[topic.strip()] = float(weight)
                except (TypeError, ValueError):
                    continue
            pillars[str(pillar)] = kept
    last_decay = raw.get("last_decay_at")
    return InterestsProfile(
        pillars=pillars,
        last_decay_at=last_decay if isinstance(last_decay, str) else "",
    ).ensure_pillars(InterestsProfile.DEFAULT_PILLAR_NAMES)


def save_profile(profile: InterestsProfile, path: Path | None = None) -> None:
    """Atomic write to ``path`` (or the default location)."""
    target = Path(path) if path is not None else _profile_path_default()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pillars": {
            pillar: {topic: float(round(weight, 4)) for topic, weight in topics.items()}
            for pillar, topics in profile.pillars.items()
        },
        "last_decay_at": profile.last_decay_at,
    }
    text = yaml.safe_dump(payload, sort_keys=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(target))


def record_signal(
    profile: InterestsProfile,
    pillar: str,
    topic: str,
    signal: Signal,
) -> InterestsProfile:
    """Return a copy of ``profile`` with ``topic`` weight updated.

    Weight is clamped to ``[-WEIGHT_CLAMP, +WEIGHT_CLAMP]`` so a long
    sequence of one-direction signals saturates rather than running
    away. Empty topic strings are no-ops (returns the input unchanged).
    """
    topic_key = topic.strip()
    if not topic_key:
        return profile
    pillars = {p: dict(topics) for p, topics in profile.pillars.items()}
    pillars.setdefault(pillar, {})
    current = float(pillars[pillar].get(topic_key, 0.0))
    delta = SIGNAL_WEIGHTS[signal]
    new_weight = max(-WEIGHT_CLAMP, min(WEIGHT_CLAMP, current + delta))
    pillars[pillar][topic_key] = new_weight
    return profile.model_copy(update={"pillars": pillars})


def decay(
    profile: InterestsProfile,
    *,
    days: int = 1,
    half_life_days: int = DEFAULT_HALF_LIFE_DAYS,
) -> InterestsProfile:
    """Apply exponential half-life decay across all topic weights.

    Each weight ``w`` becomes ``w * 0.5 ** (days / half_life_days)``.
    Weights with magnitude < 0.05 after decay are pruned to keep the
    profile from accumulating noise from forgotten one-off signals.
    Updates ``last_decay_at`` to the current UTC timestamp.
    """
    if days <= 0 or half_life_days <= 0:
        return profile
    factor = math.pow(0.5, days / half_life_days)
    pillars: dict[str, dict[str, float]] = {}
    for pillar, topics in profile.pillars.items():
        kept: dict[str, float] = {}
        for topic, weight in topics.items():
            decayed = weight * factor
            if abs(decayed) < 0.05:
                continue
            kept[topic] = decayed
        pillars[pillar] = kept
    return profile.model_copy(update={
        "pillars": pillars,
        "last_decay_at": datetime.now(timezone.utc).isoformat(),
    })


def score_url(
    profile: InterestsProfile,
    pillar: str,
    title: str,
    summary: str,
) -> float:
    """Score a candidate URL by summing weights of matching topics.

    A topic counts as matching when its casefolded form appears as a
    substring of the casefolded ``title + " " + summary``. The summed
    score is the world-digest's read-side ranking signal — higher
    weighted topic overlap → higher score → renderer keeps it.

    Returns ``0.0`` when the pillar is unknown or has no topics.
    """
    topics = profile.pillars.get(pillar) or {}
    if not topics:
        return 0.0
    haystack = f"{title}\n{summary}".casefold()
    score = 0.0
    for topic, weight in topics.items():
        if topic.casefold() in haystack:
            score += float(weight)
    return score


__all__ = [
    "InterestsProfile",
    "Signal",
    "SIGNAL_WEIGHTS",
    "WEIGHT_CLAMP",
    "DEFAULT_HALF_LIFE_DAYS",
    "load_profile",
    "save_profile",
    "record_signal",
    "decay",
    "score_url",
]
