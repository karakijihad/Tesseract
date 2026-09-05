"""A sub-agent's turn loop, published on the channel the delegate cards read.

An `invoke_agent` run happens inside this process: no subprocess, so no
stdout, so the Activity card that reads `cliStreams` had nothing to draw and
sat on "waiting for first chunk" for the whole run while the agent worked.

The tap below emits the same `cli_start` / `cli_output` / `cli_end` events a
delegate subprocess emits, with each line shaped the way the card's parser
reads CLI stdout: `● tool(args)` for a call, `⎿ text` for its result,
everything else prose. Feeding the renderer that already exists is the point.
A second renderer for in-process agents would be a second thing to keep in
step with the first.
"""

from __future__ import annotations

import json
from typing import Any

from tesseract.kernel.adapters.base import ChunkType, StreamChunk
from tesseract.kernel.tools.base import CliSink
from tesseract.kernel.tools.cli_stream import emit_cli_event

# A call's arguments and a result's first line are both there to say WHICH
# call this was, not to carry the payload. The card has a width.
_ARG_CHARS = 120
_RESULT_CHARS = 200
# Text arrives as adapter deltas, often one token each. Coalescing keeps the
# card fed without one websocket envelope per token; a boundary event flushes
# whatever is buffered, so a tool call never lands before the prose that
# introduced it.
_PROSE_FLUSH_CHARS = 400


def _one_line(text: str, limit: int) -> str:
    """The first non-empty line of `text`, bounded. The card's parser is
    line-based, so an embedded newline would split one entry into two."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:limit]
    return ""


def _render_args(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(payload)
    text = " ".join(text.split())
    return text[:_ARG_CHARS]


class AgentTranscriptTap:
    """Turns one sub-session's chunks into the operator's live view.

    Every emit is best-effort: a spawn must not fail because nobody was
    watching it. `close` is paired with `start` on every exit path, including
    a cancellation, or the card streams forever for a call that has returned.
    """

    def __init__(
        self, sink: CliSink, stream_key: str, tool: str, origin_call_id: str
    ) -> None:
        self._sink = sink
        self._key = stream_key
        self._tool = tool
        self._origin_call_id = origin_call_id
        self._prose: list[str] = []
        self._prose_chars = 0

    @classmethod
    def for_run(
        cls,
        sink: CliSink | None,
        stream_key: str,
        tool: str,
        origin_call_id: str,
    ) -> "AgentTranscriptTap | None":
        """None when there is nobody to show this to (REPL, tests, autonomy)
        or no key to file it under, so every call site stays unconditional."""
        if sink is None or not stream_key:
            return None
        return cls(sink, stream_key, tool, origin_call_id)

    async def start(self, agent_name: str) -> None:
        # `origin_call_id` names the tool call this stream belongs to. It is
        # not the key when a background spawn's card is bound to its handle,
        # and the rail counts fires by call, so without it one delegation
        # reads as two.
        await emit_cli_event(
            self._sink,
            self._key,
            "cli_start",
            {
                "tool": self._tool,
                "argv": [agent_name],
                "origin_call_id": self._origin_call_id,
            },
        )

    async def on_chunk(self, chunk: StreamChunk) -> None:
        if chunk.type is ChunkType.TEXT:
            if not chunk.text:
                return
            self._prose.append(chunk.text)
            self._prose_chars += len(chunk.text)
            if self._prose_chars >= _PROSE_FLUSH_CHARS:
                await self._flush_prose()
            return
        if chunk.type is ChunkType.TOOL_CALL_END:
            call = chunk.tool_call
            if call is None:
                return
            await self._flush_prose()
            await self._line(f"● {call.name}({_render_args(call.input)})")
            return
        if chunk.type is ChunkType.TOOL_RESULT:
            await self._flush_prose()
            summary = _one_line(chunk.error or chunk.text, _RESULT_CHARS)
            await self._line(f"⎿ {summary or '(no output)'}")
            return
        if chunk.type is ChunkType.ERROR:
            await self._flush_prose()
            await self._line(f"⎿ error: {_one_line(chunk.error, _RESULT_CHARS)}")

    async def close(self, exit_code: int) -> None:
        await self._flush_prose(shielded=True)
        await emit_cli_event(
            self._sink,
            self._key,
            "cli_end",
            {"tool": self._tool, "exit_code": exit_code},
            shielded=True,
        )

    async def _flush_prose(self, *, shielded: bool = False) -> None:
        if not self._prose:
            return
        text = "".join(self._prose).strip()
        self._prose = []
        self._prose_chars = 0
        if text:
            await self._line(text, shielded=shielded)

    async def _line(self, text: str, *, shielded: bool = False) -> None:
        await emit_cli_event(
            self._sink,
            self._key,
            "cli_output",
            {"tool": self._tool, "delta": text + "\n"},
            shielded=shielded,
        )
