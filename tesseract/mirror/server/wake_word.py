"""Wake-word gate for the voice-input path.

The gate answers one question: did this utterance address the assistant by name?
It runs after STT, on the transcript, for the mic modes that dispatch a
turn (``command`` / ``speak``). ``transcribe`` and ``terminal`` are never
gated — those modes hand the text to the operator, not to the brain.

The phrase is ``<prefix> <name>``, and both halves come off the same
``mirror.yaml::identity`` block, read through ``ServerConfig`` on every
utterance. Nothing is compiled at boot, so a rename re-teaches the gate
as soon as the config watcher swaps the config in.

Matching is a normalized edit distance over the *prefix* of the
transcript, never a substring scan: a name spoken mid-sentence must not
wake anything. Whitespace inside the window is discarded before
comparison because STT splits and merges names unpredictably — a
one-word name comes back as two tokens, or with an apostrophe or
periods inserted — and the window is tried at three widths so a name
rendered as one token or two both land.

Both halves are required: the phrase is always two words. A bare name
is not a wake word — the widening that makes this matcher tolerant of
STT is exactly what makes a lone short name unsafe, since a two-token
window is free to absorb an unrelated word and score against it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

log = logging.getLogger(__name__)

# Word runs, unicode-aware, underscores excluded. Matched against the
# original string (not a lowered copy) so the spans stay valid for
# slicing the remainder back out with its original casing.
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

# Leading punctuation an STT engine puts between the wake phrase and the
# request ("Hey the assistant, what's the time"). Stripped off the remainder so
# the dispatched text reads as a clean sentence.
_LEADING_PUNCT = " \t,.:;!?-–—…"


@dataclass(frozen=True)
class WakeWordConfig:
    enabled: bool
    prefix: str
    match_threshold: float


@dataclass(frozen=True)
class WakeWordDecision:
    """`matched` is the gate verdict; `text` is what should be dispatched
    when it passed (wake phrase stripped). `score` is the best window's
    similarity — carried on the discard event so the operator can see how
    close a rejected utterance came."""

    matched: bool
    score: float
    text: str


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,               # deletion
                    current[j - 1] + 1,            # insertion
                    previous[j - 1] + (ca != cb),  # substitution
                )
            )
        previous = current
    return previous[-1]


def _similarity(a: str, b: str) -> float:
    longest = max(len(a), len(b))
    if longest == 0:
        return 0.0
    return 1.0 - (_levenshtein(a, b) / longest)


def _fold(tokens: list[str]) -> str:
    return "".join(t.lower() for t in tokens)


def match_wake_phrase(
    transcript: str,
    name: str,
    *,
    prefix: str,
    threshold: float,
) -> WakeWordDecision:
    """Fuzzy-match ``<prefix> <name>`` against the head of ``transcript``.

    On a match the remainder is returned with its original casing and
    punctuation. An utterance that is *only* the wake phrase dispatches
    verbatim rather than as an empty turn — addressing the assistant by name is
    itself something to answer.
    """
    phrase_tokens = _TOKEN_RE.findall(f"{prefix} {name}")
    if not phrase_tokens:
        # No phrase to match against (blank name/prefix). Refusing every
        # utterance here would be a silent mute; the caller's config
        # validation is what keeps this unreachable in practice.
        return WakeWordDecision(matched=False, score=0.0, text=transcript)
    phrase = _fold(phrase_tokens)

    spans = [(m.group(0), m.end()) for m in _TOKEN_RE.finditer(transcript)]
    if not spans:
        return WakeWordDecision(matched=False, score=0.0, text=transcript)

    width = len(phrase_tokens)
    best_score = 0.0
    best_end: int | None = None
    # One token either side of the expected width: STT renders a name as
    # one token or two and the phrase has to survive both without the
    # window sliding off its prefix anchor.
    for candidate_width in {width - 1, width, width + 1}:
        if candidate_width < 1 or candidate_width > len(spans):
            continue
        window = spans[:candidate_width]
        score = _similarity(_fold([tok for tok, _ in window]), phrase)
        if score > best_score:
            best_score = score
            best_end = window[-1][1]

    if best_end is None or best_score < threshold:
        return WakeWordDecision(matched=False, score=best_score, text=transcript)

    remainder = transcript[best_end:].lstrip(_LEADING_PUNCT).strip()
    return WakeWordDecision(
        matched=True,
        score=best_score,
        text=remainder or transcript.strip(),
    )


# ── Config ───────────────────────────────────────────────────────────


def parse_wake_word_config(identity: Mapping[str, Any], path: Path) -> WakeWordConfig:
    """Read ``identity.wake_word`` out of a parsed mirror.yaml.

    Every key is required — the threshold in particular has no in-source
    default, so a config missing it fails at load with the file named
    rather than silently gating on a number nobody chose.
    """
    where = f"{path} identity.wake_word"
    block = identity.get("wake_word")
    if not isinstance(block, dict):
        raise RuntimeError(f"{path} missing required 'identity.wake_word' block")
    for key in ("enabled", "prefix", "match_threshold"):
        if key not in block:
            raise RuntimeError(f"{where} missing required key: {key}")
    # Typed, not coerced. Every one of these three is a YAML scalar an
    # operator can get subtly wrong, and coercion turns each mistake into
    # a silent behaviour change instead of an error:
    #   enabled: "false"       -> bool("false") is True  (gate silently ON)
    #   prefix: null           -> str(None) is "None"    (phrase becomes "None …")
    #   match_threshold: true  -> float(True) is 1.0     (fuzzy match becomes exact)
    # The last two pass every downstream check, so nothing else would catch them.
    if not isinstance(block["enabled"], bool):
        raise RuntimeError(
            f"{where}.enabled must be a boolean (true/false, unquoted), "
            f"got {block['enabled']!r}"
        )
    raw_prefix = block["prefix"]
    if not isinstance(raw_prefix, str):
        raise RuntimeError(
            f"{where}.prefix must be a string, got {raw_prefix!r}"
        )
    prefix = raw_prefix.strip()
    if not prefix:
        raise RuntimeError(f"{where}.prefix must be a non-empty string")
    raw_threshold = block["match_threshold"]
    # bool is an int subclass, so it must be refused before the numeric check.
    if isinstance(raw_threshold, bool) or not isinstance(raw_threshold, (int, float)):
        raise RuntimeError(
            f"{where}.match_threshold must be a number, got {raw_threshold!r}"
        )
    threshold = float(raw_threshold)
    if not (0.0 < threshold <= 1.0):
        raise RuntimeError(f"{where}.match_threshold must be in (0.0, 1.0], got {threshold}")
    return WakeWordConfig(
        enabled=block["enabled"],
        prefix=prefix,
        match_threshold=threshold,
    )


_fail_open_logged: set[str] = set()


def _log_fail_open(reason: str) -> None:
    """One ERROR per distinct fault, not one per utterance.

    The gate runs on every utterance, so an unlatched log turns a standing
    misconfiguration into a repeating pulse row — the log forwarder's
    one-second dedupe only collapses utterances spoken back to back. A
    fault that repeats forever reads as noise; a fault stated once reads
    as a fault."""
    if reason in _fail_open_logged:
        return
    _fail_open_logged.add(reason)
    log.error(
        "wake_word: %s — gate disabled until this is fixed (logged once)", reason
    )


def reset_fail_open_log() -> None:
    """Test hook — the latch is process-wide by design, which would
    otherwise make the second test asserting on it order-dependent."""
    _fail_open_logged.clear()


def evaluate_wake_gate(app: Any, transcript: str) -> WakeWordDecision:
    """Gate one transcript for the dispatching mic modes.

    Returns ``matched=True`` with the text untouched whenever the gate is
    off — and also when the config it needs is missing. A wake word that
    cannot be read must not silently swallow every utterance; it fails
    open to the take-all behaviour that predates the gate, loudly (the
    ERROR reaches the pulse feed through the log forwarder).
    """
    config = app.get("config") if hasattr(app, "get") else None
    wake = getattr(config, "wake_word", None)
    if not isinstance(wake, WakeWordConfig):
        _log_fail_open("no config on ServerConfig")
        return WakeWordDecision(matched=True, score=0.0, text=transcript)
    name = str(getattr(config, "entity_name", "") or "").strip()
    if name:
        # The config reads cleanly again, so re-arm the report. Latched for
        # the life of the process instead would mean a fault that is fixed
        # and then recurs never gets said a second time.
        _fail_open_logged.clear()
    if not wake.enabled:
        return WakeWordDecision(matched=True, score=0.0, text=transcript)

    if not name:
        _log_fail_open("no entity name on ServerConfig")
        return WakeWordDecision(matched=True, score=0.0, text=transcript)

    return match_wake_phrase(
        transcript,
        name,
        prefix=wake.prefix,
        threshold=wake.match_threshold,
    )
