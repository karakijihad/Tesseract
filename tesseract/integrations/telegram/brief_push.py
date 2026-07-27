"""Daily-brief Telegram push (MO-10-3).

When ``daily_brief_ready`` fires (cron or operator-driven), pushes an
operator-phone exec summary to operator-tier Telegram chat_ids.

Source of truth for content: the matching ``daily_brief`` workspace
event's ``payload.sections`` (built by the brief renderer). The event
is appended right before ``daily_brief_ready`` broadcasts.

Disabled by default via ``channels.yaml::telegram.brief_push: false``.
"""

from __future__ import annotations

import logging
import re
from html import escape as html_escape
from typing import Any

from tesseract.integrations.telegram.format import markdown_to_telegram_html

log = logging.getLogger(__name__)


HARD_LIMIT_CHARS = 2500
SECTION_LINE_LIMIT = 220
MAX_WORLD_CARDS_PER_PILLAR = 2

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _clip_sentences(text: str, *, max_sentences: int = 2, max_chars: int | None = None) -> str:
    if not text:
        return ""
    raw = " ".join(str(text).strip().split())
    if not raw:
        return ""

    parts = _SENTENCE_SPLIT_RE.split(raw)
    if max_sentences and len(parts) > max_sentences:
        clipped = " ".join(parts[:max_sentences]).strip()
    else:
        clipped = raw

    if max_chars is not None and len(clipped) > max_chars:
        clipped = clipped[: max_chars - 1].rstrip() + "…"

    return clipped


def _vault_top(entries: list[Any]) -> str:
    if not isinstance(entries, list) or not entries:
        return ""
    first = entries[0]
    if isinstance(first, dict):
        title = str(first.get("title") or "").strip()
        if title:
            return title[:SECTION_LINE_LIMIT]
        body = str(first.get("body") or "").strip()
        return body[:120] if body else ""
    if isinstance(first, str):
        return first.strip()[:SECTION_LINE_LIMIT]
    return ""


def _world_card_line(card: dict[str, Any]) -> str:
    title = str(card.get("title") or "").strip()
    url = str(card.get("url") or "").strip()
    summary = str(card.get("summary") or "").strip()
    source = str(card.get("source") or "").strip()
    published_at = str(card.get("published_at") or "").strip()

    if not title:
        return ""

    # Prefer markdown links; the markdown→Telegram converter will
    # consistently escape anything risky.
    if url:
        headline = f"[{title}]({url})"
    else:
        headline = title

    meta: list[str] = []
    if source:
        meta.append(source)
    if published_at:
        meta.append(published_at)

    meta_suffix = f" ({', '.join(meta)})" if meta else ""

    s = _clip_sentences(summary, max_sentences=2, max_chars=140) if summary else ""
    if s:
        return f"{headline} — {s}{meta_suffix}".strip()
    return f"{headline}{meta_suffix}".strip()


def _format_world(world: Any) -> list[str]:
    if not isinstance(world, dict):
        return []
    out: list[str] = []
    for label, key in (("Tech", "tech"), ("Science", "science"), ("Politics", "politics")):
        cards = world.get(key) or []
        if not isinstance(cards, list) or not cards:
            continue

        # Render up to N cards inline after the label.
        parts: list[str] = []
        for card in cards[:MAX_WORLD_CARDS_PER_PILLAR]:
            if not isinstance(card, dict):
                continue
            line = _world_card_line(card)
            if line:
                parts.append(line)

        if not parts:
            continue
        out.append(f"• <b>{label}:</b> {_inline_html(' • '.join(parts))}")
    return out


def format_exec_summary(workspace_payload: dict[str, Any] | None) -> str:
    """Render the operator-phone exec summary from a workspace payload.

    Drops empty sections (header AND body). Hard-truncates at
    ``HARD_LIMIT_CHARS`` with an ellipsis. Returns Telegram-HTML.
    """
    if not isinstance(workspace_payload, dict):
        return ""

    sections = workspace_payload.get("sections") or {}
    if not isinstance(sections, dict) or not sections:
        return ""

    date = str(workspace_payload.get("date") or "").strip()

    lines: list[str] = []
    header = (
        f"<b>TESSERACT — {html_escape(date)}</b>" if date else "<b>TESSERACT</b>"
    )
    lines.append(header)
    initial_length = len(lines)

    # Voice-prose sections: keep the first chunk, but not so short that
    # we drop the operator-liked "Suggested:" sentence.
    yest_tess = _clip_sentences(
        str(sections.get("yesterday_in_tesseract") or ""),
        max_sentences=2,
        max_chars=SECTION_LINE_LIMIT,
    )
    if yest_tess:
        lines.append("")
        lines.append("<i>Yesterday in TESSERACT</i>")
        lines.append(_inline_html(yest_tess))

    yest_you = _clip_sentences(
        str(sections.get("yesterday_with_you") or ""),
        max_sentences=2,
        max_chars=SECTION_LINE_LIMIT,
    )
    if yest_you:
        lines.append("")
        lines.append("<i>With you</i>")
        lines.append(_inline_html(yest_you))

    learned = _clip_sentences(
        str(sections.get("what_i_learned") or ""),
        max_sentences=2,
        max_chars=SECTION_LINE_LIMIT,
    )
    if learned:
        lines.append("")
        lines.append("<i>What I learned</i>")
        lines.append(_inline_html(learned))

    vault_top = _vault_top(sections.get("vault") or [])
    if vault_top:
        lines.append("")
        lines.append("<i>Vault</i>")
        lines.append(f"• {_inline_html(vault_top)}")

    # World: structured cards per pillar.
    world_lines = _format_world(sections.get("world") or {})
    if world_lines:
        lines.append("")
        lines.append("<i>World</i>")
        lines.extend(world_lines)

    # Ecosystem: voice-prose paragraphs; keep the "Suggested:" clause.
    ecosystem = sections.get("ecosystem") or ""
    if isinstance(ecosystem, str) and ecosystem.strip():
        paras = [p.strip() for p in ecosystem.split("\n\n") if p.strip()]
        if paras:
            lines.append("")
            lines.append("<i>Ecosystem</i>")
            for p in paras[:4]:
                clipped = _clip_sentences(p, max_sentences=6, max_chars=520)
                if clipped:
                    lines.append(f"• {_inline_html(clipped)}")

    initiatives = sections.get("initiatives") or []
    if isinstance(initiatives, list) and initiatives:
        lines.append("")
        lines.append("<i>Initiatives</i>")
        for item in initiatives[:4]:
            if not item:
                continue
            lines.append(f"• {_inline_html(str(item))}")

    if len(lines) == initial_length:
        # Nothing rendered beyond the header — drop the header too so
        # the operator never gets a payload-shaped "empty brief" ping.
        return ""

    text = "\n".join(lines)
    if len(text) > HARD_LIMIT_CHARS:
        text = text[: HARD_LIMIT_CHARS - 1].rstrip() + "…"
    return text


def _inline_html(text: str) -> str:
    """Run prose through the markdown→HTML helper used by the chat path."""
    return markdown_to_telegram_html(text)


async def send_to_operators(
    text: str,
    *,
    bridge: Any,
    allowlist: Any,
    user_tier: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Fan ``text`` out to operator-tier chat_ids, skipping pending/blocked.

    Per-recipient failures are logged and isolated — one chat_id can't
    block the rest. Returns a small summary for log + tests.
    """
    if not text:
        return {"sent": 0, "skipped": 0, "errors": 0}

    sent = 0
    errors = 0
    skipped = 0
    if allowlist is None:
        return {"sent": 0, "skipped": 0, "errors": 0}

    chat_ids = set(getattr(allowlist, "chat_ids", set()))
    blocked = set(getattr(allowlist, "blocked", set()))
    pending = set((getattr(allowlist, "pending", {}) or {}).keys())
    tiers = user_tier or {}

    for cid in sorted(chat_ids):
        key = str(cid)
        if cid in blocked or cid in pending:
            skipped += 1
            continue
        tier = tiers.get(key, "operator")
        if tier != "operator":
            skipped += 1
            continue
        try:
            await bridge.send_text(chat_ref=key, text=text)
            sent += 1
        except Exception:  # noqa: BLE001
            log.exception("brief_push: send_text failed for chat=%s", key)
            errors += 1
    return {"sent": sent, "skipped": skipped, "errors": errors}


class TelegramBriefPushSubscriber:
    """Listens for ``daily_brief_ready`` and pushes the exec summary."""

    def __init__(
        self,
        *,
        bridge: Any,
        event_store: Any,
        config_loader: Any,
        allowlist_loader: Any,
        user_tier_loader: Any | None = None,
    ) -> None:
        self._bridge = bridge
        self._event_store = event_store
        self._config_loader = config_loader
        self._allowlist_loader = allowlist_loader
        self._user_tier_loader = user_tier_loader

    def _push_enabled(self) -> bool:
        cfg = self._config_loader() if callable(self._config_loader) else self._config_loader
        if cfg is None:
            return False
        telegram_block = getattr(cfg, "telegram", None)
        if telegram_block is None:
            return False
        return bool(getattr(telegram_block, "brief_push", False))

    def _latest_brief_payload(self) -> dict[str, Any] | None:
        if self._event_store is None:
            return None
        try:
            events = self._event_store.list_events(kinds=("daily_brief",), limit=5)
        except Exception:
            log.exception("brief_push: list_events failed")
            return None
        for ev in events:
            payload = ev.payload or {}
            if isinstance(payload, dict) and payload.get("sections"):
                return payload
        return None

    async def handle(self) -> dict[str, Any]:
        if self._bridge is None:
            return {"sent": 0, "skipped": 0, "errors": 0, "reason": "no_bridge"}
        if not self._push_enabled():
            return {"sent": 0, "skipped": 0, "errors": 0, "reason": "disabled"}
        payload = self._latest_brief_payload()
        if payload is None:
            return {"sent": 0, "skipped": 0, "errors": 0, "reason": "no_payload"}
        text = format_exec_summary(payload)
        if not text:
            return {"sent": 0, "skipped": 0, "errors": 0, "reason": "empty_text"}
        allowlist = self._allowlist_loader() if callable(self._allowlist_loader) else self._allowlist_loader
        tiers = (
            self._user_tier_loader()
            if callable(self._user_tier_loader)
            else self._user_tier_loader
        )
        return await send_to_operators(
            text,
            bridge=self._bridge,
            allowlist=allowlist,
            user_tier=tiers if isinstance(tiers, dict) else None,
        )


__all__ = [
    "HARD_LIMIT_CHARS",
    "SECTION_LINE_LIMIT",
    "TelegramBriefPushSubscriber",
    "format_exec_summary",
    "send_to_operators",
]
