"""Shared text-quality predicates for autonomy proposal goals — fragment
detection + actionable-directive extraction. Single source of truth (was
duplicated in follow_up_mapper + ApprovalsPane.tsx)."""

from __future__ import annotations

import re

_LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*•]+|\d+[.)])\s*")
_MARKDOWN_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_DASH_RE = re.compile(r"\s+[–—]\s+")
_DEGENERATE_RE = re.compile(r"^[\W\d_]+$")
_ACTIONABLE_STARTS = frozenset(
    {
        "add",
        "analyze",
        "audit",
        "build",
        "confirm",
        "create",
        "document",
        "extract",
        "fix",
        "generate",
        "implement",
        "inspect",
        "investigate",
        "merge",
        "migrate",
        "move",
        "patch",
        "refactor",
        "rename",
        "resolve",
        "review",
        "rewrite",
        "run",
        "split",
        "synthesize",
        "update",
        "verify",
    }
)


def first_sentence(text: str, *, cap: int = 240) -> str:
    """Title for the new draft — first sentence or ``cap`` chars.

    Splits on terminator + whitespace OR terminator + uppercase letter
    so advisor output without spacing between sentences ("…cache.Then
    test…") still segments cleanly rather than bleeding into the next
    sentence (capped, but visibly wrong).
    """
    stripped = text.strip()
    if not stripped:
        return ""
    match = re.search(r"(?<=[.!?])(?:\s|(?=[A-Z]))", stripped)
    candidate = stripped[: match.start()].strip() if match else stripped
    if len(candidate) > cap:
        candidate = candidate[: cap - 1].rstrip() + "…"
    return candidate


def _clean_candidate(text: str) -> str:
    cleaned = _LIST_PREFIX_RE.sub("", text.strip())
    cleaned = _MARKDOWN_BOLD_RE.sub(r"\1", cleaned)
    cleaned = cleaned.replace("`", "").replace("**", "")
    cleaned = _DASH_RE.sub(": ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—")
    return cleaned


def looks_like_fragment(text: str) -> bool:
    stripped = text.strip()
    if re.fullmatch(r"[\W\d_]+", stripped):
        return True
    lowered_full = stripped.lower()
    if lowered_full.startswith(("we should ", "please ")):
        return False
    first = stripped.split(maxsplit=1)[0].strip(".,:;()[]{}\"'")
    if not first:
        return True
    lowered = first.lower()
    if len(stripped) < 12 and lowered not in _ACTIONABLE_STARTS:
        return True
    if lowered in {"and", "or", "for", "from", "to", "with", "of", "the", "this"}:
        return True
    if stripped[0].islower() and lowered not in _ACTIONABLE_STARTS:
        return True
    return False


def is_degenerate_goal(text: str) -> bool:
    """True only for STRUCTURALLY broken goals safe to prune at admission
    regardless of source: empty/whitespace, punctuation/digit-only (e.g. a
    lone ``}``), or shorter than 3 chars. Intentionally does NOT judge
    prose quality — that is the agent-vet's job (Phase 2). Must never fire
    on a real English directive like ``act on heartbeat observation: ...``."""
    s = (text or "").strip()
    if len(s) < 3:
        return True
    if _DEGENERATE_RE.fullmatch(s):
        return True
    return False


def _candidate_lines(summary: str) -> list[str]:
    candidates: list[str] = []
    for raw in summary.splitlines():
        line = _clean_candidate(raw)
        if not line:
            continue
        if line.lower() in {"suggested next steps", "next steps", "steps"}:
            continue
        candidates.append(line)
    if candidates:
        return candidates
    return [_clean_candidate(summary)]


def actionable_goal(summary: str, keywords: tuple[str, ...], *, cap: int = 240) -> str:
    """Extract a coherent follow-up directive from a possibly truncated tail."""
    lowered_keywords = tuple(k.lower() for k in keywords)
    for line in _candidate_lines(summary):
        if not any(keyword in line.lower() for keyword in lowered_keywords):
            continue
        sentence = _clean_candidate(first_sentence(line, cap=cap))
        if looks_like_fragment(sentence):
            continue
        if len(sentence) > cap:
            sentence = sentence[: cap - 1].rstrip() + "…"
        return sentence
    return ""


__all__ = [
    "actionable_goal",
    "first_sentence",
    "is_degenerate_goal",
    "looks_like_fragment",
]
