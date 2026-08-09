"""Channel-agnostic progress throttler + event types.

CR-4 — the placeholder-edit progress narrative for channel turns.
``_start_channel_turn`` fires a ``ProgressEvent`` on three triggers
(elapsed-time pulses at 15/30/60/120s, ``tool_start``, ``tool_end``);
each channel adapter passes its own ``on_progress`` callback that
formats the event and pumps it through :class:`ProgressThrottler`.
The throttler enforces a 1 Hz floor per chat so we never trip
Telegram's ~20/s editMessageText rate limit (we self-cap to leave
headroom).

Pure-asyncio. No Telegram-specific knowledge — Telegram + future
adapters share this module so the 1 Hz rule lives in exactly one
place.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

log = logging.getLogger(__name__)

ProgressKind = Literal["elapsed", "tool_start", "tool_end"]

# Per-tool emoji prefix for the rendered one-liner. Anything missing
# from this map falls through to ``default`` so a brand-new tool name
# still surfaces as a recognisable "working on it" line.
TOOL_EMOJI: dict[str, str] = {
    "web_search": "🔍",
    "vault_query": "📂",
    "vault_search": "📂",
    "memory_search": "🧠",
    "brief_render": "📰",
    "default": "🛠",
}

DELEGATE_PREFIX = "delegate_"
DELEGATE_EMOJI = "🤝"

# 1 Hz per-chat cap. Telegram's editMessageText hard rate limit is
# ~20/s; we self-cap to 1/s so two-back-to-back tool_start chunks
# (very common — chat_brain often fans out a search + a memory probe
# in the same iteration) collapse into a single edit.
DEFAULT_COOLDOWN_S = 1.0

# Elapsed-tick ladder. The first nudge fires at 4s — inside the
# Telegram typing-indicator's ~5s decay window — so text-only turns
# (chat_brain streams text with no tool calls, the placeholder edit
# path never fires `tool_start`/`tool_end`) still show "something is
# happening" before the operator perceives the reply as stuck.
# Subsequent ticks back off so a 5-minute turn doesn't surface ten
# "still working" lines.
ELAPSED_TICKS_S: tuple[float, ...] = (4.0, 15.0, 30.0, 60.0, 120.0)


@dataclass(frozen=True)
class ProgressEvent:
    kind: ProgressKind
    tool_name: str = ""
    tool_args: dict | None = None
    elapsed_s: float = 0.0


def emoji_for_tool(tool_name: str) -> str:
    """Map a tool name to its progress emoji.

    ``delegate_*`` is folded to a single 🤝 since every delegate call
    surfaces the same "asked a subagent" semantics from the operator's
    point of view. Unknown names get the generic 🛠 default.
    """
    if not tool_name:
        return TOOL_EMOJI["default"]
    if tool_name.startswith(DELEGATE_PREFIX):
        return DELEGATE_EMOJI
    return TOOL_EMOJI.get(tool_name, TOOL_EMOJI["default"])


def format_progress_line(event: ProgressEvent) -> str:
    """Render a one-line progress string for a chat edit.

    Lines are <60 chars so they fit comfortably in a Telegram bubble
    and read as "the assistant is working" rather than as content. The final
    answer replaces them entirely when the turn lands; these strings
    are scaffolding, not transcript.
    """
    if event.kind == "elapsed":
        elapsed = max(0, int(round(event.elapsed_s)))
        return f"🛠 still working on this — {elapsed}s in"
    if event.kind == "tool_start":
        emoji = emoji_for_tool(event.tool_name)
        if event.tool_name.startswith(DELEGATE_PREFIX):
            return f"{emoji} {event.tool_name}"
        if event.tool_name == "web_search":
            query = _stringify_query(event.tool_args)
            if query:
                return f"{emoji} web_search: {query[:60]}"
        return f"{emoji} {event.tool_name or 'tool'}"
    if event.kind == "tool_end":
        emoji = emoji_for_tool(event.tool_name)
        return f"{emoji} {event.tool_name or 'tool'} ✓"
    return ""


def _stringify_query(tool_args: dict | None) -> str:
    """Best-effort extraction of a 'query'-shaped arg for web_search."""
    if not isinstance(tool_args, dict):
        return ""
    for key in ("query", "q", "text", "prompt"):
        val = tool_args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


EditFn = Callable[[str], Awaitable[None]]


class ProgressThrottler:
    """1 Hz coalescing throttler for chat-edit progress lines.

    One instance per chat (Telegram chat_id, etc.). Rapid emits within
    the cooldown window are folded — the latest text wins, no stale-
    text leakage. A pending-flush task delivers the buffered text
    after the cooldown elapses; cancelling :meth:`stop` cleans the
    task so a turn that ends fast cannot leak a ghost edit.

    The throttler does not know about Telegram — the ``edit_fn``
    callback owns the channel-specific send/edit. The throttler only
    enforces "at most one call per cooldown window".
    """

    def __init__(
        self,
        edit_fn: EditFn,
        *,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
    ) -> None:
        self._edit_fn = edit_fn
        self._cooldown_s = max(0.0, float(cooldown_s))
        self._last_edit_at: float | None = None
        self._pending_text: str | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def emit(self, text: str) -> None:
        """Request an edit to ``text``.

        Coalesces with any in-flight pending text: only the most
        recent string is delivered when the cooldown elapses.
        """
        text = (text or "").strip()
        if not text:
            return
        async with self._lock:
            if self._closed:
                return
            now = _loop_time()
            since = (
                now - self._last_edit_at if self._last_edit_at is not None else None
            )
            if since is None or since >= self._cooldown_s:
                self._last_edit_at = now
                self._pending_text = None
                fire_text = text
            else:
                # Within cooldown — buffer the latest text and let
                # the pending-flush task deliver it. New calls
                # overwrite the buffer so the operator only sees the
                # latest state.
                self._pending_text = text
                if self._flush_task is None or self._flush_task.done():
                    delay = max(0.0, self._cooldown_s - since)
                    self._flush_task = asyncio.create_task(
                        self._flush_after(delay),
                        name="channel_progress_flush",
                    )
                return
        await self._invoke_edit(fire_text)

    async def stop(self) -> None:
        """Flush any buffered text, then stop accepting emits.

        Called on turn completion. Short turns commonly buffer a
        ``tool_start``/``tool_end`` edit inside the 1Hz cooldown and
        finish before the pending flush task can fire — without an
        end-of-turn flush, the placeholder stays on ``thinking…`` for
        the whole turn and the operator never sees which tool ran.
        We deliver the buffered text once (best-effort) before the
        final-reply path overwrites the placeholder with the answer.
        Ordering on Telegram is per-chat sequential, so the brief
        progress line lands strictly before the final reply edit.
        """
        async with self._lock:
            task = self._flush_task
            self._flush_task = None
            pending = self._pending_text
            self._pending_text = None
            self._closed = True
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("channel progress: flush task exit raised")
        if pending:
            try:
                await self._edit_fn(pending)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("channel progress: final flush edit raised")

    async def _flush_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        async with self._lock:
            if self._closed:
                return
            text = self._pending_text
            self._pending_text = None
            if text is None:
                return
            self._last_edit_at = _loop_time()
        # Re-check `_closed` outside the lock: ``stop()`` may have set it
        # in the window between lock release here and the ``_invoke_edit``
        # below. Without this guard, ``_flush_after`` could fire an edit
        # after ``stop()`` returned — a ghost line landing after the
        # final reply.
        if self._closed:
            return
        await self._invoke_edit(text)

    async def _invoke_edit(self, text: str) -> None:
        if self._closed:
            return
        try:
            await self._edit_fn(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("channel progress: edit_fn raised")


def _loop_time() -> float:
    """Hook seam — tests can monkeypatch ``time.monotonic`` via the
    event loop, but using the loop clock keeps the throttler honest
    under ``loop.run_until_complete`` fixtures that don't stub time.
    """
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:
        import time

        return time.monotonic()
