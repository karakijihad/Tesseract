"""Pure helpers shared across the librarian's promotion / distillation /
summary stages — slugging, clipping, title parsing, JSON candidate parsing,
and the atomic file write. No I/O beyond `_atomic_write`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from tesseract.memory.librarian.constants import (
    _ANCHOR_FRAGILE_CHARS,
    _BOOKKEEPING_TITLE_PREFIXES,
    _DISTILL_BULLET_MAX_CHARS,
)
from tesseract.memory.types import MemoryFrontmatter


def _is_bookkeeping_title(title: str) -> bool:
    t = (title or "").strip()
    return any(t.startswith(prefix) for prefix in _BOOKKEEPING_TITLE_PREFIXES)


def _is_bookkeeping_entry(fm: MemoryFrontmatter) -> bool:
    """True if `fm` is a legacy logs-stream stub that should be filtered
    from MEMORY.md ranking.

    Scoped to title-prefix matches (pre-M1 `[reflect]` / `[session_end]` /
    `[auto_compact]` / `[scheduler]` stubs). An explicit `bookkeeping` tag
    is honoured as a second signal so the one-shot prune script can mark
    entries it migrates instead of deletes.

    Important: this must NOT blanket-exclude all librarian-promoted
    REFERENCE entries — audit-1 (2026-04-24) M5 showed the prior
    `daily:* + source_session + type` branch silently suppressed every
    `[chat_digest]` and `[reference]` promotion from MEMORY.md.
    """
    if _is_bookkeeping_title(fm.title):
        return True
    return "bookkeeping" in (fm.tags or [])


def _clip_words(text: str, max_len: int) -> str:
    """Clip `text` to ≤max_len chars on a word boundary, appending '…' if cut.

    Hard-slices when no whitespace fits in range. Trailing '…' is included
    in the budget — the rendered string is never longer than max_len.
    """
    if max_len <= 0:
        return ""
    if not text or len(text) <= max_len:
        return text
    head = text[: max_len - 1]
    cut = head.rfind(" ")
    if cut <= 0:
        return head.rstrip() + "…"
    return head[:cut].rstrip() + "…"


def _anchor_slug(title: str) -> str:
    """Stable slug for `daily/…#<slug>` idempotency anchors.

    audit-1 m12 (2026-04-24): raw `.replace(" ", "-")` left characters like
    `[`, `]`, `/`, `#` in the slug — YAML round-trips could alter them and
    cause the same section to re-promote. Strip the fragile set and collapse
    consecutive dashes so the stored `source_path` survives re-serialization.
    """
    cleaned = "".join("-" if c in _ANCHOR_FRAGILE_CHARS else c for c in title.strip().lower())
    cleaned = cleaned.replace(" ", "-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    cleaned = cleaned.strip("-")
    return cleaned or "anon"


def _extract_type_prefix(title: str) -> str | None:
    """Return the `[token]` string from a daily section title, else None.

    Pure — zero I/O. Wired by M2's classifier routing; M1 only lands the
    helper.
    """
    t = (title or "").strip()
    if not t.startswith("[") or "]" not in t:
        return None
    token = t[1 : t.index("]")].strip()
    return token or None


def _parse_candidates(raw: str, *, max_candidates: int) -> list[str]:
    """Extract `candidates` from the model's JSON, clipped to `max_candidates`
    and `_DISTILL_BULLET_MAX_CHARS`. Tolerates leading prose around the JSON.
    """
    if not raw:
        return []
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return []
    try:
        parsed = json.loads(match.group())
    except json.JSONDecodeError:
        return []
    items = parsed.get("candidates")
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if not cleaned:
            continue
        if len(cleaned) > _DISTILL_BULLET_MAX_CHARS:
            cleaned = cleaned[:_DISTILL_BULLET_MAX_CHARS].rstrip()
        out.append(cleaned)
        if len(out) >= max_candidates:
            break
    return out


def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace + drop trailing punctuation, for
    near-equivalence checks between proposed bullets and existing Growth.
    """
    cleaned = re.sub(r"\s+", " ", (text or "").strip().lower())
    return cleaned.rstrip(".!?,;:")


def _parse_daily_sections(text: str) -> list[tuple[str, str]]:
    """Split a daily markdown file into `(title, body)` pairs.

    A section begins at each `## ` heading; everything before the first
    `## ` (including a top-level `# ` date heading) is one anonymous section.
    Trailing whitespace is stripped and empty bodies are dropped.
    """
    current_title = ""
    current_body: list[str] = []
    sections: list[tuple[str, str]] = []

    def _flush() -> None:
        joined = "\n".join(current_body).strip()
        if joined:
            sections.append((current_title, joined))

    for line in text.split("\n"):
        if line.startswith("## "):
            _flush()
            current_title = line[3:].strip()
            current_body = []
        else:
            current_body.append(line)

    _flush()
    return sections


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
