"""Line-buffered renderer for claude ``--output-format stream-json`` output.

Delegate visibility fix-pass (2026-07-10): ``claude -p --output-format text``
prints nothing until the run finishes, so the Mirror DelegateCard sat on
"waiting for first chunk" for a whole 20-minute delegation. Switching the
delegate to stream-json gives one NDJSON event per message; this module turns
those events into human-readable transcript lines as they arrive and extracts
the final result text at the end.

Result extraction reuses ``ClaudeTurnAccumulator`` (the interactive-session
parser) — one schema implementation, two consumers.
"""

from __future__ import annotations

import json
from typing import Any

from tesseract.orchestrator.tars_controller.interactive.stream_parser import (
    ClaudeTurnAccumulator,
)

_TOOL_INPUT_PREVIEW_CHARS = 160
_RAW_LINE_PREVIEW_CHARS = 400


def _preview(value: Any, limit: int) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _render_event(event: dict[str, Any]) -> str:
    """One readable transcript line (possibly multi-line for text blocks)
    per stream-json event. Returns "" for events with no display value."""
    etype = event.get("type")
    if etype == "system" and event.get("subtype") == "init":
        model = event.get("model") or ""
        suffix = f" ({model})" if model else ""
        return f"[claude session started{suffix}]\n"
    if etype == "assistant":
        msg = event.get("message") or {}
        lines: list[str] = []
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = str(block.get("text") or "").strip()
                if text:
                    lines.append(text)
            elif btype == "tool_use":
                name = block.get("name") or "?"
                lines.append(
                    f"→ {name} {_preview(block.get('input'), _TOOL_INPUT_PREVIEW_CHARS)}"
                )
        return ("\n".join(lines) + "\n") if lines else ""
    if etype == "result":
        subtype = event.get("subtype") or ("error" if event.get("is_error") else "success")
        duration = event.get("duration_ms")
        timing = f" in {duration / 1000:.0f}s" if isinstance(duration, (int, float)) else ""
        return f"[claude finished: {subtype}{timing}]\n"
    # tool_result echoes (type == "user") and unknown event types carry no
    # display value — the tool_use line above already shows the action.
    return ""


class ClaudeDelegateStreamParser:
    """Feed raw stdout chunks; get back readable transcript text.

    ``feed`` buffers partial NDJSON lines across chunk boundaries, parses
    complete lines, folds them into a ``ClaudeTurnAccumulator`` (for the
    final result), and returns the rendered display text for the chunk.
    Unparseable lines pass through raw (truncated) so schema drift degrades
    to noisy-but-visible instead of silent.
    """

    def __init__(self) -> None:
        self._accumulator = ClaudeTurnAccumulator()
        self._carry = ""

    def _consume_line(self, line: str) -> str:
        line = line.strip()
        if not line:
            return ""
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return line[:_RAW_LINE_PREVIEW_CHARS] + "\n"
        if not isinstance(event, dict):
            return ""
        self._accumulator.feed(event)
        return _render_event(event)

    def feed(self, chunk: str) -> str:
        self._carry += chunk
        display: list[str] = []
        while "\n" in self._carry:
            line, self._carry = self._carry.split("\n", 1)
            rendered = self._consume_line(line)
            if rendered:
                display.append(rendered)
        return "".join(display)

    def flush(self) -> str:
        """Consume any trailing partial line after EOF."""
        rest, self._carry = self._carry, ""
        return self._consume_line(rest) if rest.strip() else ""

    def final_output(self) -> str | None:
        """The turn's result text once a ``result`` event has arrived."""
        if not self._accumulator.done:
            return None
        text = self._accumulator.result_text
        return text if text else None

    @property
    def is_error(self) -> bool:
        return self._accumulator.done and self._accumulator.is_error
