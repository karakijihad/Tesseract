"""Per-chat memory tier for external channels (Telegram, future WhatsApp/Signal).

Three layers sit on top of the existing 20-turn verbatim ``ChatSession.history``:

1. **Rolling summary** — when a turn pair drops out of the sliding window,
   :meth:`ChatMemoryService.append_evictions` folds it into
   ``logs/channels/<channel>/<chat_id>/summary.md`` as bullets, newest-first,
   capped at ~80 entries. Operator-readable; injected above the verbatim
   window on every turn so long-running threads stay coherent.
2. **End-of-conversation reflection** — after a chat goes idle for
   ``reflection_delay_s`` (default 30 min), :meth:`_write_reflection` writes
   a single :class:`MemoryFrontmatter` entry tagged
   ``["channel", channel, f"chat:{chat_id}"]`` so the next day's first message
   surfaces it via the auto-recall path. Long-term continuity.
3. **Auto recall** — :meth:`recall_for_inbound` queries the memory pipeline
   scoped to this chat's tags before ``chat_brain`` runs, so TARS picks up
   yesterday's promises automatically rather than relying on a proactive
   ``memory_search`` call.

This is deliberately filesystem-first: every write is atomic and operator-
visible. ``MemoryBundle`` may be ``None`` (minimal harness, test fixtures);
the reflection path no-ops cleanly in that case and the summary path still
works.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from tesseract.paths import TESSERACT_HOME

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
# Default idle window before a reflection writes. 30 min matches the cockpit's
# notion of "conversation ended" and gives the operator time to send a
# follow-up without churning a new memory entry every minute.
_DEFAULT_REFLECTION_DELAY_S = 1800
# Tail size for the reflection body. Bigger than the 20-turn ChatSession window
# so a long burst still summarises in one memory; smaller than the full chat
# log so the body stays scannable.
_REFLECTION_TAIL_TURNS = 40
# Top-K recalled memories injected at the head of each inbound turn. Five is
# the same default the global memory_search uses (see
# ``RetrievalPipeline.retrieve``).
_RECALL_TOP_K = 5
# Log-fallback tail cap (2026-05-17). When memory + summary come back empty
# we tail this many turns from the per-day log so TARS has *some* grounding.
# 30 ≈ 15 user/assistant pairs, enough to cover a short conversation that
# happened before the reflection sweep had a chance to fire.
_RECALL_LOG_TAIL_TURNS = 30


def _channels_logs_root() -> Path:
    """Mirror the resolution pattern in
    :func:`tesseract.integrations._conversation_store._channels_root` so test
    fixtures that ``monkeypatch.setenv('TESSERACT_HOME', tmp_path)`` after
    import still land their writes under the temp dir."""
    home_env = os.environ.get("TESSERACT_HOME")
    base = Path(home_env) if home_env else TESSERACT_HOME
    return base / "logs" / "channels"


def _safe_segment(value: str) -> str:
    cleaned = "".join(ch for ch in str(value) if ch.isalnum() or ch in {"-", "_"})
    return cleaned or "unknown"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class _ReflectionState:
    """Last-written marker so we don't double-reflect on idle that already
    fired. The reflection writer compares ``last_turn_at`` against this to
    decide whether new turns have landed since the previous reflection."""

    last_reflected_at: datetime
    last_reflected_turn_ts: str


class ChatMemoryService:
    """Per-chat memory layer for channel adapters.

    Construct once at adapter boot; share across chats. All public methods
    are best-effort: failures log + swallow rather than abort the turn or
    block the long-poll loop. The service owns its own scheduling for
    deferred reflection tasks; :meth:`shutdown` cancels them cleanly.
    """

    def __init__(
        self,
        *,
        conversation_store: "ConversationStore",
        memory_bundle: "MemoryBundle | None" = None,
        reflection_delay_s: int = _DEFAULT_REFLECTION_DELAY_S,
    ) -> None:
        self._convo = conversation_store
        self._bundle = memory_bundle
        self._reflection_delay_s = max(60, int(reflection_delay_s))
        self._last_turn: dict[tuple[str, str], datetime] = {}
        self._reflection_state: dict[tuple[str, str], _ReflectionState] = {}
        self._reflect_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._lock = asyncio.Lock()

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

    # -- reflection scheduling -----------------------------------------

    async def on_turn_completed(self, channel: str, chat_id: str) -> None:
        """Mark a turn done — last-turn timestamp is the canonical signal.

        2026-05-17: the in-process ``asyncio.sleep(1800)`` timer pattern
        is gone. ``ChannelReflectionSweepJob`` (cron */5) walks
        ``poll_state.last_message_ts`` and fires :meth:`_write_reflection`
        when a chat is idle ≥ ``reflection_delay_s`` — restart-resilient
        because the source-of-truth lives on disk (state.json) not in
        a cancellable RAM task. This method still updates the in-memory
        ``_last_turn`` map for tests and for any caller introspecting
        the service directly.
        """
        key = (channel, str(chat_id))
        self._last_turn[key] = _now()

    async def _write_reflection(self, channel: str, chat_id: str) -> None:
        """Persist a conversation-recap memory if anything new happened.

        Idempotent: a no-op when no turns have landed since the last
        reflection or when no :class:`MemoryBundle` is wired (test
        harness / minimal boot). Tags the memory so :meth:`recall_for_inbound`
        can find it on the next inbound.
        """
        if self._bundle is None:
            log.debug("chat-memory: no MemoryBundle wired; skipping reflection")
            return
        rows = self._convo.tail(channel, str(chat_id), limit=_REFLECTION_TAIL_TURNS)
        if not rows:
            return
        # rows are newest-first per ConversationStore.tail; we want oldest-
        # first in the body so the memory reads chronologically.
        rows = list(reversed(rows))
        last_ts = str(rows[-1].get("ts") or "")
        state = self._reflection_state.get((channel, str(chat_id)))
        if state is not None and state.last_reflected_turn_ts == last_ts:
            log.debug(
                "chat-memory: reflection up-to-date for %s/%s (last_ts=%s)",
                channel, chat_id, last_ts,
            )
            return

        from tesseract.memory.types import MemoryFrontmatter, MemoryType

        body = _format_reflection_body(channel, chat_id, rows)
        title = _format_reflection_title(channel, chat_id, rows)
        now = _now()
        frontmatter = MemoryFrontmatter(
            id=MemoryFrontmatter.generate_id(),
            type=MemoryType.PROJECT,
            title=title,
            summary=_reflection_summary_blurb(rows),
            created_at=now,
            importance=6,
            tags=["channel", channel, f"chat:{chat_id}", "conversation-recap"],
            source_type="chat",
        )
        try:
            ok = self._bundle.store.write(frontmatter, body)
        except Exception:
            log.exception("chat-memory: store.write raised for %s/%s", channel, chat_id)
            return
        if not ok:
            # Policy-blocked write (WhatNotToSave). Deterministic for this
            # tail — advance the marker so the */5 sweep doesn't retry the
            # identical blocked recap forever (live loop, 2026-07-12). A new
            # turn changes last_ts and re-arms reflection naturally.
            self._reflection_state[(channel, str(chat_id))] = _ReflectionState(
                last_reflected_at=now,
                last_reflected_turn_ts=last_ts,
            )
            log.info(
                "chat-memory: reflection blocked by policy for %s/%s; "
                "marked reflected through last_ts=%s (no retry)",
                channel, chat_id, last_ts,
            )
            return
        # Best-effort index/embedding refresh — the same pattern memory_save
        # uses. Failures are non-fatal; the next /rebuild picks them up.
        try:
            self._bundle.index.add_or_update(frontmatter)
        except Exception:
            log.debug("chat-memory: index update skipped", exc_info=True)
        if self._bundle.embeddings is not None:
            try:
                await self._bundle.embeddings.embed_and_add(frontmatter, body)
            except Exception:
                log.debug("chat-memory: embedding skipped", exc_info=True)

        self._reflection_state[(channel, str(chat_id))] = _ReflectionState(
            last_reflected_at=now,
            last_reflected_turn_ts=last_ts,
        )
        log.info(
            "chat-memory: wrote reflection for %s/%s (%d turns, last_ts=%s, mem=%s)",
            channel, chat_id, len(rows), last_ts, frontmatter.id,
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
            # logs directly so TARS still has *some* context to ground
            # on instead of hallucinating. Caps the tail at
            # ``_RECALL_LOG_TAIL_TURNS`` (~30) so the prompt budget stays
            # reasonable. The reflection writer should usually populate
            # memory within ``reflection_delay_s`` of the last turn; this
            # is the rescue path for the "bridge restart before reflect
            # fired" case.
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
        bridge restart before the reflection writer fired. Caps each
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

    # -- shutdown -------------------------------------------------------

    async def shutdown(self) -> None:
        """Cancel every legacy in-process reflection task (2026-05-17:
        the in-process timer is gone; the scheduler-driven
        ``ChannelReflectionSweepJob`` is the new path. This method
        survives to drain any legacy tasks a long-running process
        might still hold, and to satisfy the bridge's stop() contract).
        """
        async with self._lock:
            tasks = list(self._reflect_tasks.values())
            self._reflect_tasks.clear()
        for t in tasks:
            if t.done():
                continue
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:
                log.debug("chat-memory: shutdown task error", exc_info=True)


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


def _format_reflection_title(channel: str, chat_id: str, rows: list[dict[str, Any]]) -> str:
    head = rows[0] if rows else {}
    ts = str(head.get("ts") or "")
    date_part = ts.split("T", 1)[0] if ts else _now().date().isoformat()
    return f"Chat recap — {channel}:{chat_id} ({date_part})"


def _reflection_summary_blurb(rows: list[dict[str, Any]]) -> str:
    inbound = sum(1 for r in rows if r.get("direction") == "inbound")
    outbound = sum(1 for r in rows if r.get("direction") == "outbound")
    span_first = str(rows[0].get("ts") or "") if rows else ""
    span_last = str(rows[-1].get("ts") or "") if rows else ""
    return (
        f"{len(rows)} turns ({inbound} in / {outbound} out) "
        f"between {span_first} and {span_last}"
    )


def _format_reflection_body(
    channel: str, chat_id: str, rows: list[dict[str, Any]]
) -> str:
    """Heuristic body — one bullet per turn. A future pass can replace this
    with an LLM-driven thematic digest; v1 keeps the raw record so TARS can
    quote it back exactly. Body is markdown; embeddings index the text body."""
    lines = [
        f"# Chat recap — {channel}:{chat_id}",
        "",
        _reflection_summary_blurb(rows),
        "",
        "## Transcript (oldest → newest)",
        "",
    ]
    for row in rows:
        ts = str(row.get("ts") or "")
        direction = str(row.get("direction") or "?")
        body = str(row.get("body") or "").strip()
        if not body:
            continue
        snippet = body if len(body) <= _BULLET_MAX_CHARS else body[: _BULLET_MAX_CHARS - 1] + "…"
        lines.append(f"- **{direction}** @ {ts} — {snippet}")
    return "\n".join(lines) + "\n"


__all__ = [
    "ChatMemoryService",
]
