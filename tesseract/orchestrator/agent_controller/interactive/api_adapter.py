"""ApiLaneAdapter — a lane driven by an API model instead of a CLI subprocess.

The delegation seats made the worker a config choice, but only the cli tier
had a lane adapter, so a seat pointed at an `api.*` ref was refused at
resolution. This is the transport that makes the refusal unnecessary: it
speaks the same `LaneAdapter` protocol the CLI adapters do, so everything
above it — turn identity, the event stream, `await_turn`, interrupt,
completion delivery — is unchanged.

**An API lane has no tools, and that is the point rather than a limitation.**
The CLI adapters drive an agent that reads files, runs commands and edits a
repository. This one drives a model with a conversation and nothing else. It
cannot touch the filesystem, so it is read-only *by construction* — more
strongly than codex's sandbox flag, which is a CLI option that could be
passed wrong. What it buys is a reviewer that reasons over material you hand
it, which is exactly the auditor seat's job when the evidence is already in
the brief.

Multi-turn state is the message list held here, mirroring how a CLI lane
keeps its own session across sends. There is no external session id to
resume, so the lane's identity is the only continuity, and a restart starts
the conversation over — the same trade a CLI lane makes when its
`cli_session_id` is gone.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from tesseract.kernel.adapters.base import AdapterOptions, ChunkType

log = logging.getLogger(__name__)


@dataclass
class ApiLaneAdapter:
    """Drives one lane's turns against a `ModelAdapter`.

    `ref` is the resolved `api.*` reference; the adapter is built lazily on
    the first turn so a lane can be opened (and persisted) without paying a
    provider handshake, matching the CLI adapters' spawn-on-first-send shape.
    """

    ref: Any  # ResolvedRef
    system_prompt: str = ""
    _messages: list[dict[str, Any]] = field(default_factory=list, repr=False)
    _adapter: Any = field(default=None, repr=False)

    def _ensure_adapter(self):
        if self._adapter is None:
            from tesseract.brain.boot import build_adapter

            self._adapter = build_adapter(self.ref)
        return self._adapter

    async def run_turn(
        self,
        *,
        message: str,
        on_event: Callable[[dict[str, Any]], None],
        cancel_event: asyncio.Event | None,
    ) -> dict[str, Any]:
        adapter = self._ensure_adapter()
        self._messages.append({"role": "user", "content": message})

        options = AdapterOptions(model=self.ref.model.model)
        chunks: list[str] = []
        usage: dict[str, Any] = {}
        error: str | None = None

        try:
            # No `tools` argument, deliberately — see the module docstring.
            # A tool schema here would let an API lane act, which is the one
            # property this transport is trusted for not having.
            async for chunk in adapter.stream(self._messages, options=options):
                if cancel_event is not None and cancel_event.is_set():
                    # Same contract as the CLI adapters: stop streaming and
                    # report what was gathered rather than raising, so the
                    # lane records a real turn_ended for the waiter.
                    break
                if chunk.type == ChunkType.TEXT and chunk.text:
                    chunks.append(chunk.text)
                elif chunk.type == ChunkType.ERROR:
                    error = chunk.error or "adapter error"
                    break
                elif chunk.type == ChunkType.STOP:
                    usage = dict(chunk.raw.get("usage") or {})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — surfaced as a failed turn
            error = str(exc)

        text = "".join(chunks).strip()
        if text:
            # Recorded before the return so a cancelled or errored turn still
            # leaves its partial reply on the lane — the same reason the CLI
            # path emits as it goes rather than at the end.
            self._messages.append({"role": "assistant", "content": text})
            on_event({"type": "assistant_text", "text": text})
        if error is not None:
            on_event({"type": "error", "message": error})

        return {
            "session_id": None,
            "is_error": error is not None,
            "usage": usage,
        }


__all__ = ["ApiLaneAdapter"]
