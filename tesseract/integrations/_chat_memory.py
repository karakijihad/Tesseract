"""Per-chat memory tier for external channels (Telegram, future WhatsApp/Signal).

Two layers sit on top of the existing 20-turn verbatim ``ChatSession.history``:

1. **Rolling summary** — when a turn pair drops out of the sliding window,
   :meth:`ChatMemoryService.append_evictions` folds it into
   ``logs/channels/<channel>/<chat_id>/summary.md`` as bullets, newest-first,
   capped at ~80 entries. Operator-readable; injected above the verbatim
   window on every turn so long-running threads stay coherent.
2. **Auto recall** — :meth:`recall_for_inbound` queries the memory pipeline
   scoped to this chat's tags before ``chat_brain`` runs, so the assistant picks up
   yesterday's promises automatically rather than relying on a proactive
   ``memory_search`` call.

End-of-conversation reflection used to be a third layer here, and a channel is
not the place for it: every entry point earns the same recap, so the writer is
``tesseract/capture/`` and the `conversation_reflect` stage drives it for the
Mirror's chats and this one alike. The tag it scopes by — ``chat:<chat_id>`` —
is unchanged, which is what :meth:`recall_for_inbound` reads.

This is deliberately filesystem-first: every write is atomic and operator-
visible. ``MemoryBundle`` may be ``None`` (minimal harness, test fixtures);
the recall path degrades cleanly in that case and the summary path still works.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from tesseract.paths import TESSERACT_HOME, log_dir

if TYPE_CHECKING:  # avoid heavy imports at module load
    from tesseract.brain.boot import MemoryBundle
    from tesseract.integrations._conversation_store import ConversationStore

log = logging.getLogger(__name__)


# Keep the summary visible in one screen — when this many bullets accumulate
# we drop the oldest before adding the new one. A future LLM compaction pass
# can replace the trim with a real digest; for now the file IS the digest.
_SUMMARY_MAX_BULLETS = 80
# Hard length cap per bullet so a long voice transcript doesn't blow up the
# whole summary file. The verbatim text is still on disk in conversations.jsonl;
# the summary is a pointer, not a copy.
_BULLET_MAX_CHARS = 280
# 2026-05-17 — total char cap on the recall_context returned by
# `recall_for_inbound`. Defense-in-depth: even if `read_summary` returns
# an unexpectedly large file (e.g. legacy uncapped bullets from before
# `_BULLET_MAX_CHARS` was enforced) or 5 memory bodies each carry a long
# tool output, we keep the block under a sane budget. The chat.py-side
# trim catches anything larger; this cap keeps the typical hot path
# small. Cap chosen to leave room for the rest of the prompt assembly.
_RECALL_TOTAL_MAX_CHARS = 60_000
_RECALL_MEMORY_BODY_MAX_CHARS = 4_000
# Top-K recalled memories injected at the head of each inbound turn. Five is
# the same default the global memory_search uses (see
# ``RetrievalPipeline.retrieve``).
_RECALL_TOP_K = 5
# Log-fallback tail cap (2026-05-17). When memory + summary come back empty
# we tail this many turns from the per-day log so the assistant has *some* grounding.
# 30 ≈ 15 user/assistant pairs, enough to cover a short conversation that
# happened before the capture funnel had a chance to recap it.
_RECALL_LOG_TAIL_TURNS = 30


def _channels_logs_root() -> Path:
    """Mirror the resolution pattern in
    :func:`tesseract.integrations._conversation_store._channels_root` so test
    fixtures that ``monkeypatch.setenv('TESSERACT_HOME', tmp_path)`` after
    import still land their writes under the temp dir."""
    home_env = os.environ.get("TESSERACT_HOME")
    base = Path(home_env) if home_env else TESSERACT_HOME
    return log_dir("channels")


def _safe_segment(value: str) -> str:
    cleaned = "".join(ch for ch in str(value) if ch.isalnum() or ch in {"-", "_"})
    return cleaned or "unknown"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ChatMemoryService:
    """Per-chat memory layer for channel adapters.

    Construct once at adapter boot; share across chats. All public methods
    are best-effort: failures log + swallow rather than abort the turn or
    block the long-poll loop. It holds no state between calls — what a chat
    has already left behind is on disk, in the store and in the funnel's own
    position file.
    """

    def __init__(
        self,
        *,
        conversation_store: "ConversationStore",
        memory_bundle: "MemoryBundle | None" = None,
    ) -> None:
        self._convo = conversation_store
        self._bundle = memory_bundle

    # -- summary --------------------------------------------------------

    def summary_path(self, channel: str, chat_id: str) -> Path:
        return (
            _channels_logs_root()
            / _safe_segment(channel)
            / _safe_segment(str(chat_id))
            / "summary.md"
        )

    def read_summary(self, channel: str, chat_id: str) -> str:
        path = self.summary_path(channel, chat_id)
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("chat-memory: read summary failed for %s/%s (%s)", channel, chat_id, exc)
            return ""

    def append_evictions(
        self,
        channel: str,
        chat_id: str,
        evicted: Iterable[dict[str, Any]],
    ) -> None:
        """Fold evicted turns into the per-chat summary file.

        ``evicted`` is a list of ``ChatSession.history`` rows (dicts with
        ``role`` + ``content`` shape). Each row becomes one bullet
        prefixed with ``role:``. Newest-first; we drop the oldest entries
        when the file grows beyond :data:`_SUMMARY_MAX_BULLETS`.
        """
        rows = list(evicted)
        if not rows:
            return
        path = self.summary_path(channel, chat_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.read_summary(channel, chat_id)
        existing_lines = existing.splitlines() if existing else []
        # Preserve any header (lines starting with '#') at the top.
        header: list[str] = []
        bullets: list[str] = []
        for line in existing_lines:
            if not bullets and (line.startswith("#") or not line.strip()):
                header.append(line)
            else:
                bullets.append(line)

        if not header:
            header = [
                f"# Rolling summary — {channel}:{chat_id}",
                "",
                "_Bullets are evicted turns, newest-first. Maintained automatically by ChatMemoryService._",
                "",
            ]

        new_bullets: list[str] = []
        for row in rows:
            new_bullets.append(_format_bullet(row))

        bullets = new_bullets + [b for b in bullets if b.strip().startswith("- ")]
        # Trim oldest beyond the cap.
        bullets = bullets[:_SUMMARY_MAX_BULLETS]

        body = "\n".join(header + bullets) + "\n"
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(body, encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            log.warning(
                "chat-memory: write summary failed for %s/%s (%s)",
                channel, chat_id, exc,
            )

    # -- recall ---------------------------------------------------------

    async def recall_for_inbound(
        self,
        channel: str,
        chat_id: str,
        query: str,
        *,
        top_k: int = _RECALL_TOP_K,
    ) -> str:
        """Build a one-shot "what we know about this chat" context block.

        Combines:
        1. The rolling summary (verbatim, capped).
        2. Top-K memory hits filtered to this chat's tags.

        Returns an empty string when nothing is available so the caller
        can append unconditionally. Best-effort: pipeline errors degrade
        to summary-only.
        """
        parts: list[str] = []
        summary = self.read_summary(channel, str(chat_id))
        if summary.strip():
            parts.append("--- ROLLING CHAT SUMMARY ---\n" + summary.strip())

        if self._bundle is None or self._bundle.pipeline is None:
            return _cap_recall_block("\n\n".join(parts))

        scoped_query = (
            f"channel:{channel} chat:{chat_id} {query}".strip()
        )
        try:
            packet = await self._bundle.pipeline.retrieve(
                scoped_query, type_filter=None, top_k=top_k,
            )
        except Exception:
            log.exception(
                "chat-memory: recall pipeline failed for %s/%s",
                channel, chat_id,
            )
            return _cap_recall_block("\n\n".join(parts))

        chat_tag = f"chat:{chat_id}"
        scoped_results = [
            r for r in (packet.results or [])
            if chat_tag in (getattr(r, "tags", None) or [])
        ]
        if not scoped_results:
            # 2026-05-17 — fallback: when memory has nothing for this
            # chat AND no rolling summary, tail the per-day conversation
            # logs directly so the assistant still has *some* context to ground
            # on instead of hallucinating. Caps the tail at
            # ``_RECALL_LOG_TAIL_TURNS`` (~30) so the prompt budget stays
            # reasonable. The capture funnel usually populates memory within
            # `capture.config.conversation_reflect.idle_minutes` of the last
            # turn; this is the rescue path for the window before it does.
            if not parts:
                tail_block = self._tail_log_for_recall(channel, str(chat_id))
                if tail_block:
                    parts.append(tail_block)
            return _cap_recall_block("\n\n".join(parts))

        parts.append("--- PRIOR CHAT MEMORIES ---")
        for r in scoped_results:
            title = getattr(r, "title", "") or getattr(r, "memory_id", "")
            body = getattr(r, "body", "") or ""
            body = body.strip()
            if len(body) > _RECALL_MEMORY_BODY_MAX_CHARS:
                body = body[: _RECALL_MEMORY_BODY_MAX_CHARS - 1].rstrip() + "…"
            parts.append(f"### {title}\n{body}")
        return _cap_recall_block("\n\n".join(parts))

    @staticmethod
    def _cap(block: str) -> str:
        # Indirection so tests can validate the helper without rebuilding
        # the full retrieve path. See module-level `_cap_recall_block`.
        return _cap_recall_block(block)

    def _tail_log_for_recall(self, channel: str, chat_id: str) -> str:
        """Read the most recent ``_RECALL_LOG_TAIL_TURNS`` turns from the
        per-day conversation log and format them as a context block.

        Used by :meth:`recall_for_inbound` when both the memory store
        and the rolling summary come back empty — typical after a
        the funnel recapped the chat. Caps each
        row's body at ``_BULLET_MAX_CHARS`` so a long voice transcript
        can't dominate the context. Returns empty string when the chat
        has no log at all (truly fresh chat).
        """
        try:
            rows = self._convo.tail(channel, chat_id, limit=_RECALL_LOG_TAIL_TURNS)
        except Exception:
            log.exception(
                "chat-memory: log-tail recall failed for %s/%s",
                channel, chat_id,
            )
            return ""
        if not rows:
            return ""
        # ``tail`` returns newest-first; flip to chronological for read flow.
        rows = list(reversed(rows))
        lines = ["--- RECENT CONVERSATION (log fallback — no summary or memory yet) ---"]
        for row in rows:
            ts = str(row.get("ts") or "")
            direction = str(row.get("direction") or "?")
            body = str(row.get("body") or "").replace("\n", " ").strip()
            if not body:
                continue
            if len(body) > _BULLET_MAX_CHARS:
                body = body[: _BULLET_MAX_CHARS - 1] + "…"
            marker = "←" if direction == "inbound" else "→"
            lines.append(f"{ts} {marker} {body}")
        return "\n".join(lines)

    # -- session seeding -----------------------------------------------

    def seed_new_session_context(self, channel: str, chat_id: str) -> str:
        """Synchronous helper used when a fresh ChatSession is built.

        Returns just the rolling summary (no memory pipeline call — that
        would block the bridge's lock-protected ``_session_for`` path).
        :meth:`recall_for_inbound` adds prior-memory context per turn.
        """
        summary = self.read_summary(channel, str(chat_id))
        if not summary.strip():
            return ""
        return "--- CONTINUING THIS CHAT ---\n" + summary.strip()

# -- formatting helpers --------------------------------------------------


def _cap_recall_block(text: str) -> str:
    """Trim a recall_context block to ``_RECALL_TOTAL_MAX_CHARS``.

    Truncates from the end with a clear marker so the model sees that
    older recall content was dropped. The chat.py-side ``_trim_to_budget``
    is the absolute backstop; this cap keeps the typical case small so
    nothing blows the prompt budget under normal load.
    """
    if not text or len(text) <= _RECALL_TOTAL_MAX_CHARS:
        return text
    keep = _RECALL_TOTAL_MAX_CHARS - 64  # room for the marker
    return (
        text[:keep].rstrip()
        + f"\n\n[recall_context capped at {_RECALL_TOTAL_MAX_CHARS} chars]"
    )


def _format_bullet(row: dict[str, Any]) -> str:
    """Turn one ChatSession.history row into a single summary bullet."""
    role = str(row.get("role") or "?").lower()
    content = row.get("content") or ""
    if isinstance(content, list):
        # Multimodal content blocks — collapse to a text-only digest.
        text_parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                value = block.get("text") or block.get("type") or ""
                if value:
                    text_parts.append(str(value))
        content = " ".join(text_parts)
    text = str(content).replace("\n", " ").strip()
    if len(text) > _BULLET_MAX_CHARS:
        text = text[: _BULLET_MAX_CHARS - 1].rstrip() + "…"
    return f"- **{role}**: {text}" if text else f"- **{role}**: _(empty)_"


__all__ = [
    "ChatMemoryService",
]
