"""TUI renderer — Claude-CLI-style polish over `rich`.

The old renderer wrote bare ANSI strings to stdout. This rewrite uses
:class:`rich.console.Console` so we inherit the same affordances the
Claude CLI gives:

* **Markdown** — ``**bold**`` / ``*italic*`` / fenced code blocks /
  bullet lists / blockquotes all render in the assistant's text.
* **Clickable links** — ``rich`` emits OSC-8 escape sequences for any
  ``[text](url)`` link so modern terminals (Windows Terminal, iTerm2,
  Ghostty) make them ctrl-clickable.
* **Panels** — tool blocks render as Claude-CLI-style cards with a
  coloured ``●`` marker, a bold tool name, and a collapsed input
  summary. Sub-process output (``cli_chunk``) streams indented under
  the card so the operator can see ``claude``/``codex`` working live.
* **Persona tags** — ``<answer>`` / ``</answer>`` get stripped (the
  text inside renders as the normal reply). ``<intent>`` ... ``</intent>``
  is dimmed so "the assistant thinking out loud" is visually distinct from the
  actual answer.

The renderer still exposes the same ``render(event)`` / ``render_header``
contract so callers (agent_cli, replay paths, tests) don't have to
change. Tests pass ``record=True`` so they can read back the recorded
buffer instead of poking the real terminal.
"""

from __future__ import annotations

import base64
import logging
import sys
from typing import Any, Callable, TextIO

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

log = logging.getLogger(__name__)


# Persona-tag set the chat brain emits inside ``assistant_text``. The
# renderer strips ``<answer>``/``</answer>`` (text inside renders as the
# normal reply) and recolors ``<intent>`` ... ``</intent>`` blocks dim
# so the operator can tell "the assistant thinking" from "the assistant answering" at a
# glance. Unknown tags pass through unchanged so a future persona-tag
# addition doesn't disappear silently.
_PERSONA_TAGS_STRIP = ("<answer>", "</answer>")
_PERSONA_TAGS_INTENT_OPEN = "<intent>"
_PERSONA_TAGS_INTENT_CLOSE = "</intent>"
_MAX_TAG_LEN = max(
    len(t)
    for t in (
        *_PERSONA_TAGS_STRIP,
        _PERSONA_TAGS_INTENT_OPEN,
        _PERSONA_TAGS_INTENT_CLOSE,
    )
)


# Maximum visible characters for a tool input summary on the panel
# subtitle. Tool inputs can be huge (a multi-kilobyte ``task`` prompt
# for ``delegate_coder``); the panel shows a one-line preview and the
# transcript file holds the full payload.
_TOOL_INPUT_PREVIEW_CAP = 100
# Maximum lines of tool_result output rendered inline. Above this we
# show ``… (N more lines)`` so a 500-line markdown dump doesn't shred
# the operator's screen. Full output is still on disk in the transcript.
_TOOL_RESULT_LINE_CAP = 8


HEADER = """\
╔══════════════════════════════════════╗
║   TESSERACT · the assistant terminal client   ║
╚══════════════════════════════════════╝
"""


class _AssistantStreamParser:
    """Streaming parser that strips persona tags and emits ``rich``
    markup segments. Each ``feed(chunk)`` returns a list of
    ``(style, text)`` pairs the caller appends to the live console.
    Partial tags split across chunks buffer until the closing ``>``
    so streaming chunks don't break the parser.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._in_intent = False

    def feed(self, chunk: str) -> list[tuple[str | None, str]]:
        self._buffer += chunk
        out: list[tuple[str | None, str]] = []
        i = 0
        while i < len(self._buffer):
            lt = self._buffer.find("<", i)
            if lt == -1:
                out.append((self._style(), self._buffer[i:]))
                i = len(self._buffer)
                break
            if lt > i:
                out.append((self._style(), self._buffer[i:lt]))
            gt = self._buffer.find(">", lt)
            if gt == -1:
                tail = self._buffer[lt:]
                if len(tail) > _MAX_TAG_LEN + 1:
                    # Definitely not a known tag — flush the `<` as text
                    # and keep scanning so a stray `<` (e.g. `price < 10`)
                    # doesn't hold the buffer forever.
                    out.append((self._style(), tail[:1]))
                    self._buffer = tail[1:]
                else:
                    self._buffer = tail
                return out
            tag = self._buffer[lt : gt + 1]
            if tag in _PERSONA_TAGS_STRIP:
                pass  # strip silently
            elif tag == _PERSONA_TAGS_INTENT_OPEN:
                self._in_intent = True
            elif tag == _PERSONA_TAGS_INTENT_CLOSE:
                self._in_intent = False
            else:
                out.append((self._style(), tag))
            i = gt + 1
        self._buffer = self._buffer[i:] if i < len(self._buffer) else ""
        return out

    def flush(self) -> list[tuple[str | None, str]]:
        if not self._buffer:
            return []
        out = [(self._style(), self._buffer)]
        self._buffer = ""
        return out

    def _style(self) -> str | None:
        return "grey50" if self._in_intent else None


def _summarize_input(payload: dict[str, Any]) -> str:
    """Single-line preview of a tool_use input dict. Long string values
    get an ellipsis; nested dicts/lists collapse to the type name so the
    panel subtitle stays one line."""
    if not payload:
        return ""
    parts: list[str] = []
    for k, v in payload.items():
        if isinstance(v, str):
            v_str = v if len(v) <= 40 else v[:39] + "…"
        elif isinstance(v, (int, float, bool)) or v is None:
            v_str = repr(v)
        else:
            v_str = type(v).__name__
        parts.append(f"{k}={v_str}")
    joined = ", ".join(parts)
    if len(joined) > _TOOL_INPUT_PREVIEW_CAP:
        joined = joined[: _TOOL_INPUT_PREVIEW_CAP - 1] + "…"
    return joined


def _truncate_lines(text: str, cap: int = _TOOL_RESULT_LINE_CAP) -> str:
    """Cap a multi-line block at ``cap`` lines with a ``… (N more lines)``
    footer. Used for tool_result outputs so a huge markdown dump from
    ``vault_query`` / ``brief_render`` doesn't shred the screen."""
    lines = text.splitlines()
    if len(lines) <= cap:
        return text
    head = "\n".join(lines[:cap])
    return f"{head}\n… ({len(lines) - cap} more lines — full output in transcript)"


def _tail(id_or_path: str, *, n: int = 8) -> str:
    return id_or_path[-n:] if len(id_or_path) > n else id_or_path


class TuiRenderer:
    """Rich-backed renderer. Writes to ``stream`` (default stdout) when
    ``record=False``; tests pass ``record=True`` to capture output.

    Construction:

    * ``color=True`` (default) — full ANSI + OSC-8 clickable links.
    * ``color=False`` — strips styles for hermetic tests / plain pipes.
    * ``record=True`` — captures all output to an in-memory buffer
      readable via :meth:`recorded_text`.
    """

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        color: bool = True,
        record: bool = False,
    ) -> None:
        self._record = record
        self._color = color
        # ``rich.Console`` decides on color/links/etc. based on the
        # file's tty + force_terminal flags. We force the colour decision
        # via no_color/force_terminal so the renderer's behaviour is
        # deterministic across test + production.
        self._console = Console(
            file=stream or sys.stdout,
            no_color=not color,
            force_terminal=color,
            highlight=False,  # we control colors ourselves
            record=record,
            soft_wrap=True,
        )
        # Streaming-text state for the current assistant turn.
        self._assistant_streaming = False
        self._assistant_parser: _AssistantStreamParser | None = None
        # Holds the accumulated text of the current streaming reply so
        # we can re-render it as Markdown on close (rich Markdown can't
        # be incrementally appended — we buffer + re-render at flush).
        self._assistant_buffer = ""

    def recorded_text(self) -> str:
        """Return everything Console has written when ``record=True``.

        ``export_text`` strips ANSI by default. We want the raw escape
        codes so tests can assert on them; ``styles=True`` preserves the
        markup as ANSI escape sequences.
        """
        if not self._record:
            raise RuntimeError("renderer not in record mode")
        return self._console.export_text(clear=False, styles=True)

    # ── header ─────────────────────────────────────────────────────────

    def render_header(self, *, session_id: str | None = None) -> None:
        self._console.print(Text(HEADER, style="bold"))
        if session_id:
            self._console.print(
                Text(f"session: {session_id}", style="grey50")
            )

    # ── dispatcher ────────────────────────────────────────────────────

    def render(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        handler: Callable[[dict[str, Any]], None] | None = _HANDLERS.get(
            str(kind)
        )
        if handler is None:
            self._unknown(event)
            return
        handler(self, event)

    # ── per-kind handlers ─────────────────────────────────────────────

    def _user_text(self, event: dict[str, Any]) -> None:
        """User-text events from the transcript. Operator-typed input
        is normally already visible in the input box (prompt_toolkit
        echoes it), so we render this dimmer than before — it's primarily
        useful on transcript REPLAY (re-attach to past session) and for
        observer-mode windows watching someone else's session.
        """
        self._flush_assistant_line()
        text = (event.get("text") or "").rstrip()
        self._console.print(
            Text("› ", style="cyan") + Text(text, style="grey50")
        )

    def _assistant_text(self, event: dict[str, Any]) -> None:
        """Stream assistant text. We collect chunks into a buffer so a
        ``partial=False`` close can re-render the FULL reply as Markdown
        (bold/italic/links/code blocks). Persona-tag parsing happens on
        each chunk so ``<intent>`` blocks stream dim immediately rather
        than waiting for the whole reply.

        When ``worker_id`` is set the text originated in a sub-session
        (interactive session tool).  Render it indented + grey in the
        worker detail pane rather than as a main-transcript the assistant bubble
        so sub-session output never pollutes the primary conversation.
        """
        worker_id = event.get("worker_id")
        if worker_id:
            self._flush_assistant_line()
            text = (event.get("text") or "").rstrip()
            for raw_line in (text or " ").splitlines():
                self._console.print(Text(f"    {raw_line}", style="grey50"))
            return
        partial = bool(event.get("partial"))
        text = event.get("text") or ""
        if not self._assistant_streaming:
            self._console.print(Text("▍agent ", style="bold magenta"), end="")
            self._assistant_streaming = True
            self._assistant_parser = _AssistantStreamParser()
            self._assistant_buffer = ""
        if self._assistant_parser is None:  # pragma: no cover — defensive
            self._assistant_parser = _AssistantStreamParser()
        # Stream the chunk live (so the operator sees text as it arrives)
        # with persona-tag handling; once `partial=False` we ALSO emit a
        # full Markdown re-render so links/bold/italic actually render.
        for style, segment in self._assistant_parser.feed(text):
            self._console.print(Text(segment, style=style or "magenta"), end="")
        self._assistant_buffer += text
        if not partial:
            for style, segment in self._assistant_parser.flush():
                self._console.print(
                    Text(segment, style=style or "magenta"), end=""
                )
            self._console.print()  # newline to close the streaming line
            self._assistant_streaming = False
            self._assistant_parser = None
            # Markdown re-render: the operator sees a tidy formatted
            # block UNDER the streamed plaintext. The duplication is
            # intentional — streaming gives "it's working" feedback,
            # the Markdown render gives "here's the finished answer"
            # with proper bold/italic/links/code.
            cleaned = self._strip_persona_tags(self._assistant_buffer)
            if cleaned.strip():
                try:
                    self._console.print(
                        Markdown(cleaned, code_theme="monokai"),
                        soft_wrap=True,
                    )
                except Exception:  # noqa: BLE001 — defensive
                    log.debug("renderer: markdown render failed", exc_info=True)
            self._assistant_buffer = ""

    def _strip_persona_tags(self, text: str) -> str:
        """Single-pass strip of ``<answer>``/``</answer>`` and unwrap
        ``<intent>``...``</intent>`` for the post-stream Markdown render.
        The streaming path uses :class:`_AssistantStreamParser` for the
        same job but on chunks; this is the bulk-text equivalent.
        """
        out = text
        for tag in _PERSONA_TAGS_STRIP:
            out = out.replace(tag, "")
        # Markdown can't dim text natively; convert intent blocks to
        # blockquotes (`>`) so they render as visually de-emphasized.
        out = out.replace(_PERSONA_TAGS_INTENT_OPEN, "\n> ")
        out = out.replace(_PERSONA_TAGS_INTENT_CLOSE, "\n")
        return out

    def _tool_use(self, event: dict[str, Any]) -> None:
        """Render a tool call as a Claude-CLI-style panel:

        ``● delegate_coder``
        ``  task: patch the auth middleware …``

        Bullet is yellow (in-progress). Tool-result will print a
        following ``● done`` or ``● failed`` line so the eye groups
        them as one block.
        """
        self._flush_assistant_line()
        name = event.get("tool") or "<tool>"
        summary = _summarize_input(event.get("input") or {})
        # Compact one-liner — Claude CLI uses a similar shape.
        text = Text()
        text.append("● ", style="yellow")
        text.append(name, style="bold")
        if summary:
            text.append(f"  {summary}", style="grey50")
        self._console.print(text)

    def _tool_result(self, event: dict[str, Any]) -> None:
        """Tool result — green ``● done`` or red ``● failed`` followed
        by a truncated output preview so the operator sees results
        inline without huge JSON / markdown dumps.
        """
        self._flush_assistant_line()
        success = bool(event.get("success"))
        timed_out = bool(event.get("timed_out"))
        out = event.get("output")
        # Normalize to a string preview.
        if isinstance(out, dict):
            preview_full = "\n".join(f"{k}: {v}" for k, v in out.items())
        elif isinstance(out, str):
            preview_full = out
        else:
            preview_full = ""
        preview = _truncate_lines(preview_full)

        marker_style = "green" if success else "red"
        status = "done" if success else ("timed out" if timed_out else "failed")
        text = Text()
        text.append("  ● ", style=marker_style)
        text.append(status, style=marker_style)
        if timed_out and success:
            text.append(" · timed_out", style="yellow")
        self._console.print(text)
        if preview.strip():
            self._console.print(
                Panel(
                    preview,
                    border_style="grey42",
                    padding=(0, 1),
                    expand=False,
                )
            )

    def _permission_request(self, event: dict[str, Any]) -> None:
        self._flush_assistant_line()
        tool = event.get("tool") or "<tool>"
        summary = event.get("summary") or ""
        posture = event.get("posture") or "ask"
        text = Text()
        text.append("‼ ", style="yellow")
        text.append("permission", style="bold yellow")
        text.append(f" · {tool} · [{posture}]", style="yellow")
        if summary:
            text.append(f"  {summary}", style="grey50")
        self._console.print(text)
        if event.get("resolved"):
            resolution = event.get("resolution") or "resolved"
            self._console.print(
                Text(f"  → {resolution}", style="grey50")
            )

    def _worker_status(self, event: dict[str, Any]) -> None:
        self._flush_assistant_line()
        worker_id = event.get("worker_id") or "<worker>"
        kind = event.get("worker_kind") or "<kind>"
        status = event.get("status") or "<status>"
        progress = event.get("progress")
        suffix = f" · {progress}" if progress else ""
        self._console.print(
            Text(
                f"· worker {kind}#{_tail(worker_id)} {status}{suffix}",
                style="cyan",
            )
        )

    def _artifact(self, event: dict[str, Any]) -> None:
        self._flush_assistant_line()
        kind = event.get("artifact_type") or "<type>"
        path = event.get("path") or "?"
        self._console.print(
            Text(f"+ artifact {kind} @ {path}", style="green")
        )

    def _child_transcript_ref(self, event: dict[str, Any]) -> None:
        self._flush_assistant_line()
        child_sid = event.get("child_session_id") or "<sid>"
        path = event.get("child_transcript_path") or "?"
        self._console.print(
            Text(f"→ child {_tail(child_sid)} @ {path}", style="grey50")
        )

    def _journal_entry(self, event: dict[str, Any]) -> None:
        self._flush_assistant_line()
        entry_type = event.get("entry_type") or "<type>"
        self._console.print(
            Text(f"· journal {entry_type}", style="grey50")
        )

    def _pty_chunk(self, event: dict[str, Any]) -> None:
        data_b64 = event.get("data_b64") or ""
        if not data_b64:
            return
        self._flush_assistant_line()
        try:
            raw = base64.b64decode(data_b64, validate=True)
        except (ValueError, TypeError):
            wid = event.get("worker_id") or "<worker>"
            self._console.print(
                Text(
                    f"· pty {_tail(wid)} ({len(data_b64)}b · invalid base64)",
                    style="grey50",
                )
            )
            return
        # Raw passthrough — preserve any ANSI escapes the sub-process emitted.
        text = raw.decode("utf-8", errors="replace")
        self._console.file.write(text)
        self._console.file.flush()

    def _cli_chunk(self, event: dict[str, Any]) -> None:
        """Live stdout from ``delegate_coder``/``delegate_auditor``
        subprocesses. Renders indented + grey under the parent tool
        block so the operator can SEE claude working in real time."""
        self._flush_assistant_line()
        phase = event.get("phase") or "chunk"
        tool = event.get("tool") or "?"
        if phase == "start":
            self._console.print(
                Text(f"  ↘ {tool} started", style="grey50")
            )
            return
        if phase == "end":
            ec = event.get("exit_code")
            ec_str = "ok" if ec == 0 else f"exit={ec}"
            style = "grey50" if ec == 0 else "red"
            self._console.print(
                Text(f"  ↖ {tool} done ({ec_str})", style=style)
            )
            return
        text = event.get("text") or ""
        if not text:
            return
        for raw_line in text.splitlines():
            self._console.print(Text(f"    {raw_line}", style="grey50"))

    def _unknown(self, event: dict[str, Any]) -> None:
        self._flush_assistant_line()
        kind = event.get("kind") or "<kind>"
        self._console.print(
            Text(f"· unknown event kind={kind!r}", style="grey50")
        )

    # ── stream helpers ────────────────────────────────────────────────

    def _flush_assistant_line(self) -> None:
        if self._assistant_streaming:
            if self._assistant_parser is not None:
                for style, segment in self._assistant_parser.flush():
                    self._console.print(
                        Text(segment, style=style or "magenta"), end=""
                    )
                self._assistant_parser = None
            self._console.print()  # newline
            self._assistant_streaming = False
            # Don't re-render Markdown here; the assistant turn isn't
            # finished, just interrupted by a tool_use / status event.
            self._assistant_buffer = ""

    # ── lifecycle status pushes (TC-5 session_status) ─────────────────

    def render_session_status(self, event: dict[str, Any]) -> None:
        self._flush_assistant_line()
        status = event.get("status") or "<status>"
        reason = event.get("reason") or ""
        line = f"· session {status}" + (f" ({reason})" if reason else "")
        self._console.print(Text(line, style="grey50"))

    # ── slash-command output ─────────────────────────────────────────

    def render_notice(self, text: str, *, style: str = "cyan") -> None:
        """Inline single-line notice — used by slash commands and
        broadcast pushes (session_deleted / session_renamed) so the
        operator sees structured confirmations distinct from assistant
        replies and tool output."""
        self._flush_assistant_line()
        self._console.print(Text(text, style=style))

    def render_help(
        self, entries: list[tuple[str, str]]
    ) -> None:
        """Render the in-session ``/help`` block. ``entries`` is a
        list of ``(command, description)`` pairs; the command column
        is padded so descriptions line up."""
        self._flush_assistant_line()
        if not entries:
            return
        width = max(len(name) for name, _ in entries)
        self._console.print(Text("slash commands:", style="bold cyan"))
        for name, desc in entries:
            line = Text("  ")
            line.append(name.ljust(width + 2), style="bold magenta")
            line.append(desc, style="grey70")
            self._console.print(line)

    def render_clear(self) -> None:
        """ANSI clear-screen + cursor-home. Matches the bare ``clear``
        / ``cls`` behaviour the operator expects; doesn't touch the
        assistant streaming buffer because a ``/clear`` mid-stream
        would also imply abandoning the partial reply (rare and
        explicit)."""
        self._assistant_streaming = False
        self._assistant_parser = None
        self._assistant_buffer = ""
        self._console.file.write("\x1b[2J\x1b[H")
        self._console.file.flush()

    def render_session_deleted(self, event: dict[str, Any]) -> None:
        sid = event.get("session_id") or "?"
        self.render_notice(f"× session deleted: {sid}", style="yellow")

    def render_session_renamed(self, event: dict[str, Any]) -> None:
        sid = event.get("session_id") or "?"
        title = event.get("title") or ""
        self.render_notice(
            f"✎ session {sid} renamed: {title}", style="cyan"
        )

    def render_reload_complete(self, event: dict[str, Any]) -> None:
        self._flush_assistant_line()
        target = event.get("target") or "?"
        reloaded = ", ".join(event.get("reloaded") or [])
        failed = event.get("failed") or []
        pending = event.get("pending_turns") or 0
        line = f"⟳ reload {target}: {reloaded or 'no changes'}"
        if pending:
            line += f" (pending turns: {pending})"
        self._console.print(Text(line, style="cyan"))
        for fail in failed:
            self._console.print(Text(f"  ✗ {fail}", style="red"))


_HANDLERS: dict[str, Callable[[TuiRenderer, dict[str, Any]], None]] = {
    "user_text": TuiRenderer._user_text,
    "assistant_text": TuiRenderer._assistant_text,
    "tool_use": TuiRenderer._tool_use,
    "tool_result": TuiRenderer._tool_result,
    "permission_request": TuiRenderer._permission_request,
    "worker_status": TuiRenderer._worker_status,
    "artifact": TuiRenderer._artifact,
    "child_transcript_ref": TuiRenderer._child_transcript_ref,
    "journal_entry": TuiRenderer._journal_entry,
    "pty_chunk": TuiRenderer._pty_chunk,
    "cli_chunk": TuiRenderer._cli_chunk,
}


__all__ = ["HEADER", "TuiRenderer"]
