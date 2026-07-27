"""Textual TUI for ``tars`` — the visual upgrade.

The old ``tars_cli`` writes ANSI strings to stdout. That works on any
terminal but loses every interactive affordance — no scrolling, no
collapsible tool blocks, no live status bar, no proper input field.
This module rebuilds the operator surface on top of ``textual``:

* :class:`TarsApp` — the root :class:`textual.app.App`. Owns the
  ``ControllerClient`` connection, the push loop, and the layout.
* :class:`TranscriptView` — vertically-scrolling area where every
  transcript event lands as its own widget. Auto-scrolls to bottom
  on new content unless the operator has scrolled up.
* :class:`AssistantMessage` — renders streamed assistant text with
  persona-tag parsing (``<answer>`` stripped, ``<intent>`` dimmed) and
  a Markdown re-render once the turn closes (bold / italic / clickable
  links / code blocks).
* :class:`ToolBlock` — a collapsible card per tool call. Header shows
  ``● tool_name`` with status color. Body holds input summary +
  streaming sub-process output (``cli_chunk``) + final result preview.
  Click / Enter to expand the body and see everything.
* :class:`StatusBar` — bottom strip showing current activity (which
  tool is running, elapsed time, session id).
* :class:`InputBar` — the prompt at the bottom. Reads the same
  ``:approve`` / ``:deny`` / ``:quit`` commands the old CLI accepted.

Headless fallback: the app degrades to the legacy ANSI renderer when
stdout isn't a TTY (CI, piped output, ``--legacy``). That keeps
``tars --list`` and tests usable without spinning a full terminal app.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from typing import Any

from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import (
    Horizontal,
    ScrollableContainer,
    Vertical,
)
from textual.screen import ModalScreen
from textual.widgets import Collapsible, Footer, Header, Input, Static

from tesseract.orchestrator.tars_controller.ipc_client import (
    ControllerClient,
    ControllerClientError,
)
from tesseract.orchestrator.tars_controller.slash_commands import (
    is_slash_command,
    known_commands,
)

log = logging.getLogger(__name__)


# ── persona-tag handling (shared with the legacy renderer) ────────────


_TAG_STRIP = ("<answer>", "</answer>")
_TAG_INTENT_OPEN = "<intent>"
_TAG_INTENT_CLOSE = "</intent>"
_MAX_TAG_LEN = max(
    len(t) for t in (*_TAG_STRIP, _TAG_INTENT_OPEN, _TAG_INTENT_CLOSE)
)


def strip_persona_tags(text: str) -> str:
    """Strip ``<answer>``/``</answer>`` and unwrap ``<intent>`` blocks
    (as blockquotes so Markdown still distinguishes them). Used by the
    AssistantMessage widget for both the streamed plaintext and the
    final Markdown re-render.
    """
    out = text
    for tag in _TAG_STRIP:
        out = out.replace(tag, "")
    out = out.replace(_TAG_INTENT_OPEN, "\n> ")
    out = out.replace(_TAG_INTENT_CLOSE, "\n")
    return out


# Light-grey italic for <intent> announcements ("TARS is about to do X").
# Used at finalize instead of a Markdown blockquote, which Rich rendered as a
# hot magenta/red bar (operator-flagged 2026-05-26 — too harsh on the eye).
_INTENT_STYLE = "italic grey62"
_INTENT_BLOCK_RE = re.compile(
    re.escape(_TAG_INTENT_OPEN) + r"(.*?)" + re.escape(_TAG_INTENT_CLOSE),
    re.DOTALL,
)


def render_persona_segments(text: str):
    """Final-render the assistant buffer: ``<intent>`` blocks become calm
    light-grey italic ``Text``; the rest renders as Markdown. Returns a single
    Rich renderable (a ``Group`` when both kinds are present).

    Replaces the old "intent → Markdown blockquote" path, which Rich styled as
    a bright magenta/red bar. Keeps Markdown for the actual reply so bold /
    code / links still render.
    """
    cleaned = text
    for tag in _TAG_STRIP:
        cleaned = cleaned.replace(tag, "")

    renderables: list[Any] = []

    def _add_markdown(chunk: str) -> None:
        if chunk.strip():
            renderables.append(Markdown(chunk.strip(), code_theme="monokai"))

    pos = 0
    for match in _INTENT_BLOCK_RE.finditer(cleaned):
        _add_markdown(cleaned[pos : match.start()])
        intent = match.group(1).strip()
        if intent:
            renderables.append(Text(intent, style=_INTENT_STYLE))
        pos = match.end()
    _add_markdown(cleaned[pos:])

    if not renderables:
        return Text("")
    if len(renderables) == 1:
        return renderables[0]
    return Group(*renderables)


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", re.MULTILINE)
# Matches the delegate_* background-spawn handle line, e.g.
# "delegate_claude spawned in background: handle=del-claude-20260524-…".
_SPAWN_HANDLE_RE = re.compile(r"handle=([A-Za-z0-9_\-]+)")


def looks_like_diff(text: str) -> bool:
    """Decide if a tool result is a real unified diff worth colourising.

    STRICT on purpose: an earlier loose ``+/-`` line-count heuristic
    painted ordinary markdown bullet lists (every line starts with
    ``- ``) entirely red — e.g. `recall_history` work-history hits.
    A unified diff is only claimed when we see a hunk header
    (``@@ -a,b +c,d @@``) OR a real file-header pair (``--- `` then
    ``+++ ``). Bullet text has neither, so it renders normally.
    """
    if not text:
        return False
    head = text[:4000]
    if _HUNK_RE.search(head):
        return True
    return "--- " in head and "+++ " in head and (
        head.find("--- ") < head.find("+++ ")
    )


def render_diff(text: str) -> Text:
    """Audit-3 M8 — colorize a unified diff. Header lines (``--- ...``,
    ``+++ ...``, ``@@ ... @@``) are cyan-dim; additions green; deletions
    red; context lines plain. Caller decides whether to wrap in a
    Collapsible — this just returns a styled :class:`rich.text.Text`."""
    out = Text()
    for line in text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            out.append(line + "\n", style="bold cyan")
        elif line.startswith("@@"):
            out.append(line + "\n", style="bold magenta")
        elif line.startswith("+"):
            out.append(line + "\n", style="green")
        elif line.startswith("-"):
            out.append(line + "\n", style="red")
        else:
            out.append(line + "\n", style="dim")
    return out


def summarize_input(payload: dict[str, Any], *, cap: int = 80) -> str:
    """Compact one-liner of a tool_use input dict for the ToolBlock
    header. Long string values get an ellipsis; nested types collapse
    to the type name."""
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
    if len(joined) > cap:
        joined = joined[: cap - 1] + "…"
    return joined


# ── themes ────────────────────────────────────────────────────────────


# Audit-3 M8 — simple theme registry. Each entry is a CSS snippet
# appended to the root Screen's stylesheet via Textual's
# ``Screen.styles`` API. Keeping themes module-level (not
# instance-level) so adding a new one is a single dict entry.
_THEMES: dict[str, dict[str, str]] = {
    "dark": {
        "background": "#0b0d10",
        "surface": "#15181d",
        "primary": "#5fafff",
        "accent": "#ffd75f",
        "text": "#e6e6e6",
        "text-muted": "#9ba1a6",
    },
    "light": {
        "background": "#fafafa",
        "surface": "#eeeeee",
        "primary": "#005f87",
        "accent": "#af5f00",
        "text": "#1a1a1a",
        "text-muted": "#555555",
    },
    "high-contrast": {
        "background": "#000000",
        "surface": "#1c1c1c",
        "primary": "#ffffff",
        "accent": "#ffff00",
        "text": "#ffffff",
        "text-muted": "#bcbcbc",
    },
}

_THEME_NAMES = tuple(_THEMES.keys())


# ── widgets ───────────────────────────────────────────────────────────


class AssistantMessage(Static):
    """Streaming assistant text + post-turn Markdown render.

    Each chunk is appended to a buffer. While streaming, the widget
    shows plaintext (so the operator sees text arrive live). Once
    ``finalize()`` is called (``partial=False`` event), the buffer is
    re-rendered as Markdown so bold / italic / links / code blocks
    look right.
    """

    DEFAULT_CSS = """
    AssistantMessage {
        margin: 0 0 1 0;
        padding: 0 1;
        color: $text;
    }
    AssistantMessage.streaming {
        color: $accent;
    }
    """

    def __init__(self) -> None:
        super().__init__("")
        self._buffer = ""
        self._finalized = False
        self.add_class("streaming")

    def feed(self, text: str) -> None:
        if self._finalized:
            # Defensive: a stray chunk after finalize starts a new
            # buffer rather than mutating the rendered Markdown.
            self._buffer = ""
            self._finalized = False
            self.add_class("streaming")
        self._buffer += text
        # Show the raw streamed text so the operator sees progress.
        # Persona tags are stripped INLINE so they never flash on screen.
        self.update(Text(strip_persona_tags(self._buffer)))

    def finalize(self) -> None:
        """End the streaming turn. Re-render the full buffer as Markdown
        (clickable links, bold, italic, code blocks all enabled)."""
        if self._finalized:
            return
        self._finalized = True
        self.remove_class("streaming")
        if self._buffer.strip():
            try:
                # Intent → light-grey italic; reply → Markdown (see
                # render_persona_segments). Replaces the old blockquote path
                # that Rich rendered hot magenta/red.
                self.update(render_persona_segments(self._buffer))
            except Exception:  # noqa: BLE001 — fall back to plain text
                self.update(Text(strip_persona_tags(self._buffer)))
        else:
            self.update(Text(""))


class ThinkingIndicator(Static):
    """Transient 'TARS is working' sign (operator-requested 2026-05-26).

    Mounted the instant a message is sent so the operator knows it landed and
    the turn is in flight; removed when the first reply / tool output arrives.
    Calm light-grey italic to match the rest of the surface — not a hot color.
    """

    DEFAULT_CSS = """
    ThinkingIndicator {
        margin: 0 0 1 0;
        padding: 0 1;
        color: $text-muted;
        text-style: italic;
    }
    """

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def on_mount(self) -> None:
        self._frame = 0
        self._paint()
        # Cheap 100ms spinner; the widget (and its timer) are removed when the
        # reply lands, so the interval never outlives the turn.
        self.set_interval(0.1, self._paint)

    def _paint(self) -> None:
        spinner = self._FRAMES[self._frame % len(self._FRAMES)]
        self._frame += 1
        self.update(f"{spinner} thinking…")


class UserMessage(Static):
    """Echo of an operator-typed input. Rendered dim because the
    operator's input is already visible in the input box above — this
    line exists for transcript replay / observer-window context."""

    DEFAULT_CSS = """
    UserMessage {
        margin: 0 0 0 0;
        padding: 0 1;
        color: $text-muted;
    }
    """

    def __init__(self, text: str) -> None:
        super().__init__(Text(f"› {text}", style="dim cyan"))


class ToolBlock(Collapsible):
    """Collapsible card per tool call.

    Header: ``● tool_name`` (yellow while running, green on success,
    red on failure) + a one-line input summary.

    Body: live ``cli_chunk`` output indented + dim, then the final
    result preview. Click the header (or press Enter when focused) to
    collapse / expand so the operator can hide noisy outputs and
    drill into specific tools.
    """

    DEFAULT_CSS = """
    ToolBlock {
        margin: 0 0 1 0;
    }
    ToolBlock > CollapsibleTitle {
        background: $surface;
        color: $text;
    }
    ToolBlock #output {
        padding: 0 1;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        *,
        tool: str,
        tool_use_id: str,
        input_summary: str,
    ) -> None:
        self._tool = tool
        self._tool_use_id = tool_use_id
        self._input_summary = input_summary
        self._cli_lines: list[str] = []
        self._result_text: str = ""
        self._status: str = "running"
        self._output_widget = Static("", id="output")
        super().__init__(
            self._output_widget,
            title=self._make_title("running"),
            collapsed=True,
        )

    @property
    def tool_use_id(self) -> str:
        return self._tool_use_id

    def _make_title(self, status: str) -> Text:
        # Status determines the marker colour: yellow=running,
        # green=done, red=failed/timed_out.
        color = {
            "running": "yellow",
            "done": "green",
            "failed": "red",
            "timed_out": "red",
        }.get(status, "yellow")
        title = Text()
        title.append("● ", style=f"bold {color}")
        title.append(self._tool, style="bold")
        if self._input_summary:
            title.append(f"  {self._input_summary}", style="dim")
        return title

    def append_cli(self, text: str) -> None:
        """Live sub-process output (``cli_chunk`` phase=chunk)."""
        if not text:
            return
        self._cli_lines.extend(text.splitlines())
        self._refresh_body()

    def mark_cli_phase(self, phase: str, exit_code: int | None) -> None:
        """``cli_chunk`` phase=start / phase=end markers."""
        if phase == "start":
            self._cli_lines.append(f"↘ {self._tool} started")
        elif phase == "end":
            ec_str = "ok" if exit_code == 0 else f"exit={exit_code}"
            self._cli_lines.append(f"↖ {self._tool} done ({ec_str})")
        self._refresh_body()

    def finalize(
        self,
        *,
        success: bool,
        timed_out: bool,
        result_text: str,
    ) -> None:
        """Tool returned — flip the header marker color, attach result
        preview, auto-expand on failure so the operator sees what went
        wrong without an extra click."""
        if timed_out:
            self._status = "timed_out"
        elif success:
            self._status = "done"
        else:
            self._status = "failed"
        self._result_text = result_text
        self.title = self._make_title(self._status)
        self._refresh_body()
        if self._status in ("failed", "timed_out"):
            self.collapsed = False

    def _refresh_body(self) -> None:
        body = Text()
        for line in self._cli_lines:
            body.append(f"  {line}\n", style="dim")
        if self._result_text:
            body.append("\n")
            body.append(self._result_text, style="dim")
        self._output_widget.update(body)


class PtyStreamBlock(Collapsible):
    """Collapsible body for a PTY (or orphan CLI) stream.

    Audit-3 M4 — the previous TUI suppressed every ``pty_chunk`` to
    ``pty <id> (output suppressed in TUI)`` and dropped ``cli_chunk``
    rows that arrived before a parent ``ToolBlock`` existed. Both
    failure modes hid live subprocess work. This widget renders the
    decoded bytes inline, capped at a sensible scrollback so a noisy
    process can't blow the renderer up.
    """

    DEFAULT_CSS = """
    PtyStreamBlock {
        margin: 0 0 1 0;
    }
    PtyStreamBlock > CollapsibleTitle {
        background: $surface;
        color: $text;
    }
    PtyStreamBlock #pty-output {
        padding: 0 1;
        color: $text-muted;
    }
    """

    MAX_LINES = 200

    def __init__(
        self,
        *,
        label: str,
        stream_id: str,
        kind: str = "pty",
    ) -> None:
        self._label = label
        self._stream_id = stream_id
        self._kind = kind
        self._lines: list[str] = []
        self._body = Static("", id="pty-output")
        super().__init__(
            self._body,
            title=self._make_title("running"),
            collapsed=False,
        )

    @property
    def stream_id(self) -> str:
        return self._stream_id

    def _make_title(self, status: str) -> Text:
        color = {
            "running": "yellow",
            "done": "green",
            "failed": "red",
        }.get(status, "yellow")
        title = Text()
        title.append("▮ ", style=f"bold {color}")
        title.append(f"{self._kind} {self._label}", style="bold")
        title.append(f"  #{self._stream_id[-8:]}", style="dim")
        return title

    def append(self, text: str) -> None:
        if not text:
            return
        new_lines = text.splitlines() or [text]
        self._lines.extend(new_lines)
        # Bounded scrollback — keep tail only.
        if len(self._lines) > self.MAX_LINES:
            self._lines = self._lines[-self.MAX_LINES :]
        body = Text()
        for line in self._lines:
            body.append(f"  {line}\n", style="dim")
        # Guard the render call so the widget can be fed before mount
        # (and unit-tested without spinning a full Textual app).
        try:
            self._body.update(body)
        except Exception:  # noqa: BLE001 — pre-mount renders are no-ops
            pass

    def mark_done(self, *, success: bool) -> None:
        try:
            self.title = self._make_title("done" if success else "failed")
        except Exception:  # noqa: BLE001 — pre-mount: silently no-op
            pass


class TranscriptView(ScrollableContainer):
    """Vertically scrollable area containing every event widget.

    Auto-scrolls to the bottom on every new child unless the operator
    has manually scrolled up — the standard "chat window that follows
    output but lets you read history" behaviour.
    """

    DEFAULT_CSS = """
    TranscriptView {
        height: 1fr;
        padding: 1 1;
        scrollbar-gutter: stable;
    }
    """


class BackgroundJobsBar(Vertical):
    """Audit-3 M5 — dock-bottom rail of background-agent rows.

    Each row is a live, clickable summary of one worker (delegate
    subprocess, autonomy kernel worker, mission step, etc.). Clicking
    a row pushes a :class:`BackgroundJobDetail` ModalScreen that shows
    the stream + tool history for that worker_id.

    Rows ride above the :class:`StatusBar` (the bar is rendered
    *below* the rail because they both ``dock: bottom``; Textual docks
    in declaration order). The rail is hidden until the first job
    arrives so an idle terminal stays clean.
    """

    DEFAULT_CSS = """
    BackgroundJobsBar {
        dock: bottom;
        height: auto;
        max-height: 6;
        background: $surface;
        padding: 0 1;
        border-top: solid $primary;
        display: none;
    }
    BackgroundJobsBar.has-jobs {
        display: block;
    }
    BackgroundJobsBar > #bg-header {
        color: $text-muted;
    }
    BackgroundJobsBar > Static.bg-row {
        color: $text;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._rows: dict[str, Static] = {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._header = Static("", id="bg-header")

    def compose(self) -> ComposeResult:
        yield self._header

    def update_job(self, job: dict[str, Any]) -> None:
        wid = str(job.get("worker_id") or "")
        if not wid:
            return
        self._jobs[wid] = job
        row = self._rows.get(wid)
        if row is None:
            row = Static("", classes="bg-row")
            self._rows[wid] = row
            # Textual.mount returns AwaitMount; we don't await — the
            # widget is queued for the next render cycle which is what
            # we want from a sync-fire call site.
            self.mount(row)
        self.add_class("has-jobs")
        self._refresh()

    def tick(self) -> None:
        if self._jobs:
            self._refresh()

    def _refresh(self) -> None:
        running = sum(
            1
            for j in self._jobs.values()
            if str(j.get("status")) in {"running", "starting", "queued"}
        )
        head = Text()
        head.append(
            f"⚙ background — {running} running · {len(self._jobs)} total",
            style="bold dim",
        )
        self._header.update(head)
        for wid, row in self._rows.items():
            job = self._jobs.get(wid)
            if job is None:
                continue
            status = str(job.get("status") or "?")
            color = (
                "yellow"
                if status in {"running", "starting", "queued"}
                else "green"
                if status in {"done", "completed"}
                else "red"
                if status in {"failed", "error", "timed_out"}
                else "dim"
            )
            started = job.get("started_at") or time.monotonic()
            elapsed = max(0.0, time.monotonic() - float(started))
            line = Text()
            line.append("▮ ", style=f"bold {color}")
            line.append(f"{str(job.get('kind') or 'worker'):<10}", style="bold")
            line.append(f" #{wid[-8:]}", style="dim")
            line.append(f"  {status}", style=color)
            line.append(f"  {elapsed:5.1f}s", style="dim")
            progress = job.get("progress")
            if progress:
                line.append(f"  {progress}", style="dim")
            row.update(line)

    def on_click(self, event: Any) -> None:
        # Find which row the click landed on by walking the children.
        target = event.widget if hasattr(event, "widget") else None
        for wid, row in self._rows.items():
            if row is target:
                self.app.push_screen(BackgroundJobDetail(wid))
                return


class BackgroundJobDetail(ModalScreen[None]):
    """Audit-3 M5 — modal overlay showing a single background job's
    stream + tool history. Press Escape to close.

    Right now it surfaces the cached job snapshot + the last 50 raw
    events relevant to this worker (cli_chunk / pty_chunk / tool_use /
    tool_result / worker_status). Live updates land as new transcript
    events arrive (the parent app calls ``feed_event`` when an event
    matches the open detail's worker_id).
    """

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Close", show=True),
        Binding("q", "dismiss_screen", "Close", show=False),
    ]

    DEFAULT_CSS = """
    BackgroundJobDetail {
        align: center middle;
    }
    BackgroundJobDetail > Vertical {
        width: 90%;
        height: 80%;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }
    BackgroundJobDetail #detail-header {
        color: $accent;
    }
    BackgroundJobDetail #detail-body {
        color: $text;
        padding: 1 0;
    }
    """

    def __init__(self, worker_id: str) -> None:
        super().__init__()
        self._worker_id = worker_id
        self._header = Static("", id="detail-header")
        self._body = Static("", id="detail-body")

    def compose(self) -> ComposeResult:
        with Vertical():
            yield self._header
            yield ScrollableContainer(self._body)

    def on_mount(self) -> None:
        self.refresh_from_app()

    def refresh_from_app(self) -> None:
        app: TarsApp = self.app  # type: ignore[assignment]
        job = app._jobs.get(self._worker_id)
        head = Text()
        head.append(
            f"background-agent  #{self._worker_id}\n", style="bold cyan"
        )
        if job is not None:
            started = float(job.get("started_at") or time.monotonic())
            elapsed = max(0.0, time.monotonic() - started)
            head.append(
                f"kind={job.get('kind')}  status={job.get('status')}  "
                f"elapsed={elapsed:.1f}s\n",
                style="dim",
            )
            if job.get("progress"):
                head.append(f"progress: {job['progress']}\n", style="dim")
        self._header.update(head)
        # Background-spawn live stream (if this worker is a spawn handle
        # with buffered cli output) takes priority — it's the actual
        # subprocess output the operator wants to inspect.
        spawn_stream = app._spawn_streams.get(self._worker_id)
        if spawn_stream:
            body = Text()
            body.append("── subprocess output ──\n", style="bold cyan")
            tail = "".join(spawn_stream)[-8000:]
            body.append(tail or "(no output captured yet)\n", style="dim")
            self._body.update(body)
            return
        # Last 50 events for this worker.
        rows = [
            e
            for e in app._raw_buffer
            if e.get("worker_id") == self._worker_id
            or e.get("tool_use_id") == self._worker_id
        ][-50:]
        body = Text()
        if not rows:
            body.append("(no captured events yet for this worker)\n", style="dim")
        for evt in rows:
            kind = evt.get("kind") or "?"
            line_color = {
                "tool_use": "yellow",
                "tool_result": "green",
                "cli_chunk": "dim",
                "pty_chunk": "dim",
                "worker_status": "cyan",
            }.get(kind, "white")
            body.append(f"  {kind}", style=f"bold {line_color}")
            snippet = (
                evt.get("text")
                or evt.get("progress")
                or evt.get("status")
                or (evt.get("output") if isinstance(evt.get("output"), str) else "")
            )
            if snippet:
                snippet = str(snippet).replace("\n", " ⏎ ")
                if len(snippet) > 200:
                    snippet = snippet[:200] + "…"
                body.append(f"  {snippet}", style="dim")
            body.append("\n")
        self._body.update(body)

    def action_dismiss_screen(self) -> None:
        self.dismiss()


class StatusBar(Static):
    """Bottom strip — current activity, elapsed time, session id.

    Updated by the app whenever a tool is dispatched / completed; an
    internal interval tick refreshes the elapsed-time string while a
    tool is running so the operator sees the seconds tick over.
    """

    DEFAULT_CSS = """
    StatusBar {
        dock: bottom;
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
    }
    """

    def __init__(self, session_id: str) -> None:
        super().__init__("")
        self._session_id = session_id
        self._active_tool: str | None = None
        self._started_at: float | None = None
        # Audit-3 M7 — most recent SessionMetricsEvent payload.
        self._metrics: dict[str, Any] = {}
        # Audit-3 M5 — count of background jobs (rendered as
        # ``bg=N`` when nonzero).
        self._job_count: int = 0
        self._refresh_text()

    def tool_started(self, tool: str) -> None:
        self._active_tool = tool
        self._started_at = time.monotonic()
        self._refresh_text()

    def tool_finished(self) -> None:
        self._active_tool = None
        self._started_at = None
        self._refresh_text()

    def tick(self) -> None:
        if self._active_tool is not None:
            self._refresh_text()

    def update_metrics(self, metrics: dict[str, Any]) -> None:
        """Audit-3 M7 — merge a SessionMetricsEvent payload into the
        cached state and repaint. Empty / null fields are ignored so a
        partial update (e.g. MODEL_SELECTED-only payload) doesn't
        clobber tokens we already learned about."""
        for key, value in metrics.items():
            if value is None or value == "":
                continue
            self._metrics[key] = value
        self._refresh_text()

    def set_job_count(self, count: int) -> None:
        """Audit-3 M5 — kept in sync with the BackgroundJobsBar so the
        statusline always shows the live background-agent count."""
        self._job_count = max(0, int(count))
        self._refresh_text()

    def _refresh_text(self) -> None:
        sid = self._session_id[-12:] if self._session_id else "?"
        metrics = self._metrics
        # Left segment — model + role + provider when known.
        left = Text()
        model = str(metrics.get("model") or "")
        provider = str(metrics.get("provider") or "")
        role = str(metrics.get("role") or "")
        if model:
            left.append(model, style="bold cyan")
            if provider:
                left.append(f"@{provider}", style="cyan")
            if role:
                left.append(f" ({role})", style="dim cyan")
            left.append(" · ", style="dim")
        # Middle segment — context window usage.
        in_tok = metrics.get("input_tokens")
        out_tok = metrics.get("output_tokens")
        window = metrics.get("context_window")
        if in_tok is not None or out_tok is not None:
            used = (int(in_tok or 0) + int(out_tok or 0))
            if window:
                pct = (used / int(window)) * 100.0 if int(window) > 0 else 0.0
                left.append(
                    f"ctx {used}/{int(window)} ({pct:.0f}%) · ", style="dim"
                )
            else:
                left.append(f"ctx {used} · ", style="dim")
        cost = metrics.get("cost_usd")
        if isinstance(cost, (int, float)) and cost > 0:
            left.append(f"${cost:.4f} · ", style="dim")
        # Activity segment — tool + elapsed, or idle, or thinking, etc.
        turn_state = str(metrics.get("turn_state") or "")
        if self._active_tool is not None:
            elapsed = (
                time.monotonic() - self._started_at
                if self._started_at is not None
                else 0.0
            )
            left.append("● ", style="bold yellow")
            left.append(self._active_tool, style="bold")
            left.append(f" · {elapsed:.1f}s", style="dim")
        elif turn_state == "thinking":
            left.append("◌ thinking", style="bold magenta")
        elif turn_state == "streaming":
            left.append("✎ streaming", style="bold cyan")
        elif turn_state == "error":
            left.append("✗ error", style="bold red")
        else:
            left.append("idle", style="dim")
        if self._job_count:
            left.append(f" · bg={self._job_count}", style="dim yellow")
        left.append(f" · session {sid}", style="dim")
        self.update(left)


# ── app ──────────────────────────────────────────────────────────────


class TarsApp(App[int]):
    """Root Textual app for the ``tars`` CLI.

    The app owns the live ``ControllerClient`` connection, the push
    loop that consumes IPC events from the daemon, and the layout of
    widgets that render those events.

    Exit codes (passed to ``App.exit(code)``):
    * 0 — clean :quit or window close
    * 130 — Ctrl+C / interrupt
    """

    CSS = """
    Screen {
        layout: vertical;
        background: $background;
    }
    Header {
        background: $surface;
        color: $accent;
        text-style: bold;
    }
    /* Flat Claude-style prompt: a › marker + borderless input framed by a
       top + bottom hairline rule. No rounded box, no hot accent — calm grey
       that brightens slightly on focus (operator-requested 2026-05-26). */
    #input-row {
        dock: bottom;
        height: 3;
        background: $background;
        /* solid greys (border colour can't take the alpha that $text-muted
           carries) — calm hairline, brighter on focus. */
        border-top: solid #3c4148;
        border-bottom: solid #3c4148;
        margin: 0 1 0 1;
    }
    #input-row:focus-within {
        border-top: solid #6e7681;
        border-bottom: solid #6e7681;
    }
    #input-prompt {
        width: 2;
        color: $text-muted;
        content-align: left middle;
        height: 1;
    }
    Input#input {
        border: none;
        background: $background;
        height: 1;
        padding: 0;
    }
    Footer {
        background: $surface;
        color: $text-muted;
    }
    /* Soften the footer key hints (^c / ^l / ^p) from the hot gold accent to
       calm greys. */
    FooterKey {
        background: $surface;
    }
    FooterKey .footer-key--key {
        color: $text;
        background: $surface;
    }
    FooterKey .footer-key--description {
        color: $text-muted;
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "request_quit", "Quit (×2)", priority=True),
        Binding("ctrl+l", "clear_transcript", "Clear", show=True),
    ]

    # Ctrl+C must be pressed twice within this window to quit — a single
    # press only arms + warns (claude-CLI parity; operator-requested
    # 2026-05-24 so an accidental Ctrl+C doesn't drop the session).
    _QUIT_ARM_SECONDS = 2.0

    def __init__(
        self,
        *,
        client: ControllerClient,
        session_id: str,
        shutdown_on_exit: bool,
        replay_events: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self._client = client
        self._session_id = session_id
        self._shutdown_on_exit = shutdown_on_exit
        self._replay_events = list(replay_events or [])
        self._tool_blocks: dict[str, ToolBlock] = {}
        self._current_assistant: AssistantMessage | None = None
        # Transient "thinking…" sign shown between send and first output.
        self._thinking: ThinkingIndicator | None = None
        self._last_worker_id: str | None = None
        self._last_tool_use_id: str | None = None
        self._push_task: asyncio.Task | None = None
        self._tick_timer = None
        # Audit-3 M4 — pty/cli stream blocks keyed by worker_id /
        # tool_use_id so orphan chunks (no preceding ToolUseEvent) and
        # PTY streams render in their own collapsible card instead of
        # being dropped or suppressed.
        self._stream_blocks: dict[str, PtyStreamBlock] = {}
        # Audit-3 M7 — most recent metrics payload, kept so the
        # StatusBar can repaint on tick without re-querying the daemon.
        self._last_metrics: dict[str, Any] = {}
        # Audit-3 M5 — per-worker live job state for the
        # BackgroundJobsBar. Keyed by worker_id.
        self._jobs: dict[str, dict[str, Any]] = {}
        # Background-spawn linkage (2026-05-24): delegate_* with
        # background=True returns immediately with a handle, so its
        # tool_result fires (and pops the ToolBlock) BEFORE the
        # subprocess streams cli_chunks — which used to spawn a
        # confusing second "cli delegate_claude" orphan block (operator
        # "why two claude"). We instead route the spawn to the rail:
        # map the delegate's tool_use_id -> spawn handle, buffer the
        # stream under the handle, and let the detail pane show it.
        self._spawn_tool_to_handle: dict[str, str] = {}
        self._spawn_streams: dict[str, list[str]] = {}
        # Audit-3 M8 — active theme name + ring buffer of recent raw
        # events (so ``/raw`` can dump the last N without going back
        # to disk). 200 events keeps the memory footprint trivial.
        self._theme_name: str = "dark"
        self._raw_buffer: list[dict[str, Any]] = []
        # Double-Ctrl+C quit guard.
        self._quit_armed_at: float | None = None

    # ── lifecycle ─────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield TranscriptView(id="transcript")
        # Order matters: dock=bottom widgets stack in declaration order,
        # so the rail sits above the StatusBar, which sits above the
        # Input. Both rail and statusline live above the Input prompt.
        yield BackgroundJobsBar()
        yield StatusBar(self._session_id)
        yield Horizontal(
            Static("›", id="input-prompt"),
            Input(
                placeholder="ask tars…   /help  :approve  :deny  :quit",
                id="input",
            ),
            id="input-row",
        )
        yield Footer()

    async def on_mount(self) -> None:
        # TARS branding in the Header (App.title is what Header paints).
        self.title = "TARS"
        sid = self._session_id[-12:] if self._session_id else "?"
        self.sub_title = f"session {sid}"
        # Apply the default theme so the palette is consistent from the
        # first frame (and /theme has a registered starting point).
        self._apply_theme(self._theme_name)
        # Replay any prior transcript events the controller sent on attach.
        for evt in self._replay_events:
            await self._render_event(evt)
        # Set focus on the input so the operator can just start typing.
        self.query_one("#input", Input).focus()
        # Background loops: push consumer + status-bar tick.
        self._push_task = asyncio.create_task(
            self._push_loop(), name="tars-app-push"
        )
        self._tick_timer = self.set_interval(
            0.5, self._on_tick, name="tars-app-tick"
        )

    async def on_unmount(self) -> None:
        if self._tick_timer is not None:
            self._tick_timer.stop()
        if self._push_task is not None and not self._push_task.done():
            self._push_task.cancel()
            try:
                await self._push_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # ── push consumer ─────────────────────────────────────────────────

    async def _push_loop(self) -> None:
        try:
            async for push in self._client.pushes():
                event_name = push.get("event")
                if event_name == "_disconnected":
                    self._notify_disconnect()
                    return
                if event_name == "transcript_event":
                    transcript = push.get("transcript_event") or {}
                    await self._render_event(transcript)
                elif event_name == "session_status":
                    await self._render_session_status(push)
                elif event_name == "reload_complete":
                    await self._render_reload_complete(push)
                elif event_name == "error":
                    await self._render_error(push)
                # ack / unknown — ignore at the operator level
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("tars app: push loop crashed: %s", exc)

    def _notify_disconnect(self) -> None:
        self.notify(
            "controller disconnected", severity="warning", timeout=4
        )
        self.exit(0)

    # ── event rendering ───────────────────────────────────────────────

    async def _render_event(self, event: dict[str, Any]) -> None:
        # Audit-3 M8 — remember the last N events so /raw can dump them.
        self._raw_buffer.append(event)
        if len(self._raw_buffer) > 200:
            self._raw_buffer = self._raw_buffer[-200:]
        # Audit-3 M5 — worker-status events feed both the transcript
        # line and the BackgroundJobsBar (rail).
        if event.get("kind") == "worker_status":
            self._update_job_from_event(event)
        kind = event.get("kind")
        if kind == "user_text":
            await self._mount(UserMessage(event.get("text") or ""))
        elif kind == "assistant_text":
            await self._render_assistant(event)
        elif kind == "tool_use":
            await self._render_tool_use(event)
        elif kind == "tool_result":
            await self._render_tool_result(event)
        elif kind == "cli_chunk":
            await self._render_cli_chunk(event)
        elif kind == "worker_status":
            await self._render_worker_status(event)
        elif kind == "permission_request":
            await self._render_permission_request(event)
        elif kind == "child_transcript_ref":
            await self._render_child_ref(event)
        elif kind == "journal_entry":
            await self._render_journal(event)
        elif kind == "artifact":
            await self._render_artifact(event)
        elif kind == "pty_chunk":
            await self._render_pty_chunk(event)
        elif kind == "session_metrics":
            await self._render_session_metrics(event)
        else:
            await self._mount(
                Static(Text(f"· unknown event kind={kind!r}", style="dim"))
            )
        self._remember(event)

    async def _show_thinking(self) -> None:
        """Mount the transient 'thinking…' sign (idempotent per turn)."""
        if self._thinking is not None:
            return
        self._thinking = ThinkingIndicator()
        await self._mount(self._thinking)

    def _clear_thinking(self) -> None:
        """Remove the 'thinking…' sign once real output starts."""
        if self._thinking is None:
            return
        try:
            self._thinking.remove()
        except Exception:  # noqa: BLE001 — removal is best-effort
            pass
        self._thinking = None

    async def _render_assistant(self, event: dict[str, Any]) -> None:
        self._clear_thinking()
        partial = bool(event.get("partial"))
        text = event.get("text") or ""
        if self._current_assistant is None:
            msg = AssistantMessage()
            self._current_assistant = msg
            await self._mount(msg)
        self._current_assistant.feed(text)
        if not partial:
            self._current_assistant.finalize()
            self._current_assistant = None

    async def _render_tool_use(self, event: dict[str, Any]) -> None:
        self._clear_thinking()
        tool = event.get("tool") or "<tool>"
        tool_use_id = event.get("tool_use_id") or ""
        summary = summarize_input(event.get("input") or {})
        block = ToolBlock(
            tool=tool, tool_use_id=tool_use_id, input_summary=summary
        )
        if tool_use_id:
            self._tool_blocks[tool_use_id] = block
        await self._mount(block)
        # Status bar reflects the active tool until tool_result lands.
        self.query_one(StatusBar).tool_started(tool)

    async def _render_tool_result(self, event: dict[str, Any]) -> None:
        tool_use_id = event.get("tool_use_id") or ""
        block = self._tool_blocks.pop(tool_use_id, None)
        success = bool(event.get("success"))
        timed_out = bool(event.get("timed_out"))
        out = event.get("output")
        if isinstance(out, dict):
            preview = "\n".join(f"{k}: {v}" for k, v in out.items())
        elif isinstance(out, str):
            preview = out
        else:
            preview = ""
        # Background-spawn handoff: a delegate_* that ran with
        # background=True returns "spawned in background: handle=<H>".
        # Register a running rail job keyed by the handle and link the
        # delegate's tool_use_id to it, so the later cli_chunk stream
        # routes to the rail instead of a second inline block.
        if preview:
            m = _SPAWN_HANDLE_RE.search(preview)
            if m and tool_use_id:
                handle = m.group(1)
                self._spawn_tool_to_handle[tool_use_id] = handle
                self._register_spawn_job(handle)
        # Audit-3 M8 — when the result looks like a diff (file_edit,
        # file_write w/ diff body, or any tool returning unified-diff
        # syntax), render a coloured block alongside the ToolBlock so
        # the operator sees additions/deletions at a glance.
        if preview and looks_like_diff(preview):
            await self._mount(Static(render_diff(preview)))
        if block is not None:
            block.finalize(
                success=success, timed_out=timed_out, result_text=preview
            )
        else:
            # No matching tool_use seen — common on transcript replay
            # if the user attaches mid-tool. Render a standalone line.
            status = "done" if success else ("timed out" if timed_out else "failed")
            color = "green" if success else "red"
            line = Text()
            line.append("● ", style=f"bold {color}")
            line.append(f"{status}", style=color)
            if preview:
                line.append(f"  {preview[:120]}", style="dim")
            await self._mount(Static(line))
        self.query_one(StatusBar).tool_finished()

    async def _render_cli_chunk(self, event: dict[str, Any]) -> None:
        tool_use_id = event.get("tool_use_id") or ""
        block = self._tool_blocks.get(tool_use_id)
        phase = event.get("phase") or "chunk"
        if block is not None:
            if phase == "chunk":
                block.append_cli(event.get("text") or "")
            else:
                ec = event.get("exit_code")
                block.mark_cli_phase(
                    phase, int(ec) if isinstance(ec, int) else None
                )
            return
        # Background-spawn stream: if this tool_use_id was linked to a
        # spawn handle by a prior "spawned in background" tool_result,
        # buffer the output under the handle and surface it on the rail
        # detail pane — NOT as a second inline block (the "two claude"
        # fix). The rail job already represents this process.
        #
        # Live ordering guarantees tool_result precedes cli_chunk (the
        # delegate returns the handle before its subprocess streams). On
        # transcript REPLAY a cli_chunk could in principle arrive before
        # its tool_result if the snapshot serialized it that way — in
        # that narrow case the chunk falls through to the orphan block
        # below, which is cosmetic and self-corrects on the next live
        # chunk once the mapping exists.
        handle = self._spawn_tool_to_handle.get(tool_use_id)
        if handle is not None:
            buf = self._spawn_streams.setdefault(handle, [])
            if phase == "chunk":
                text = event.get("text") or ""
                if text:
                    buf.append(text)
                    if len(buf) > 500:
                        del buf[:-500]
            else:
                ec = event.get("exit_code")
                job = self._jobs.get(handle)
                if job is not None:
                    job["status"] = "done" if ec == 0 else "failed"
                    self._sync_rail()
            return
        # Audit-3 M4 — no parent ToolBlock for this stream. Render in a
        # standalone PtyStreamBlock keyed by tool_use_id so the operator
        # still sees the live subprocess output. Common when attaching
        # mid-stream or when a tool emits cli_chunk before its
        # tool_use lands.
        stream_key = tool_use_id or f"orphan-{id(event)}"
        orphan = self._stream_blocks.get(stream_key)
        if orphan is None:
            label = event.get("tool") or "cli"
            orphan = PtyStreamBlock(
                label=str(label), stream_id=stream_key, kind="cli"
            )
            self._stream_blocks[stream_key] = orphan
            await self._mount(orphan)
        if phase == "chunk":
            orphan.append(event.get("text") or "")
        else:
            ec = event.get("exit_code")
            orphan.mark_done(success=(ec == 0))

    async def _render_pty_chunk(self, event: dict[str, Any]) -> None:
        worker_id = event.get("worker_id") or "?"
        block = self._stream_blocks.get(worker_id)
        if block is None:
            block = PtyStreamBlock(
                label="pty", stream_id=worker_id, kind="pty"
            )
            self._stream_blocks[worker_id] = block
            await self._mount(block)
        data_b64 = event.get("data_b64") or ""
        if data_b64:
            try:
                import base64

                raw = base64.b64decode(data_b64).decode(
                    "utf-8", errors="replace"
                )
            except Exception:  # noqa: BLE001 — bad data shouldn't crash the UI
                raw = "<undecodable pty chunk>"
            block.append(raw)

    async def _render_session_metrics(self, event: dict[str, Any]) -> None:
        # Audit-3 M7 — cache the latest metrics + repaint the statusbar.
        self._last_metrics = dict(event)
        self.query_one(StatusBar).update_metrics(self._last_metrics)

    async def _render_permission_request(
        self, event: dict[str, Any]
    ) -> None:
        tool = event.get("tool") or "<tool>"
        summary = event.get("summary") or ""
        posture = event.get("posture") or "ask"
        line = Text()
        line.append("‼ ", style="yellow")
        line.append(
            f"permission · {tool} · [{posture}]  ", style="bold yellow"
        )
        line.append(summary, style="dim")
        await self._mount(Static(line))
        if event.get("resolved"):
            resolution = event.get("resolution") or "resolved"
            await self._mount(
                Static(Text(f"  → {resolution}", style="dim"))
            )

    async def _render_worker_status(self, event: dict[str, Any]) -> None:
        kind = event.get("worker_kind") or "<kind>"
        worker_id = event.get("worker_id") or ""
        status = event.get("status") or "?"
        suffix = event.get("progress") or ""
        text = Text(
            f"· worker {kind}#{worker_id[-8:]} {status}"
            + (f" · {suffix}" if suffix else ""),
            style="cyan",
        )
        await self._mount(Static(text))

    async def _render_child_ref(self, event: dict[str, Any]) -> None:
        child = event.get("child_session_id") or ""
        path = event.get("child_transcript_path") or "?"
        await self._mount(
            Static(
                Text(f"→ child {child[-8:]} @ {path}", style="dim")
            )
        )

    async def _render_journal(self, event: dict[str, Any]) -> None:
        entry_type = event.get("entry_type") or "?"
        await self._mount(
            Static(Text(f"· journal {entry_type}", style="dim"))
        )

    async def _render_artifact(self, event: dict[str, Any]) -> None:
        kind = event.get("artifact_type") or "?"
        path = event.get("path") or "?"
        await self._mount(
            Static(Text(f"+ artifact {kind} @ {path}", style="green"))
        )

    async def _render_session_status(self, push: dict[str, Any]) -> None:
        status = push.get("status") or "?"
        reason = push.get("reason") or ""
        line = (
            f"· session {status}" + (f" ({reason})" if reason else "")
        )
        await self._mount(Static(Text(line, style="dim")))

    async def _render_reload_complete(self, push: dict[str, Any]) -> None:
        target = push.get("target") or "?"
        reloaded = ", ".join(push.get("reloaded") or [])
        line = f"⟳ reload {target}: {reloaded or 'no changes'}"
        await self._mount(Static(Text(line, style="cyan")))

    async def _render_error(self, push: dict[str, Any]) -> None:
        code = push.get("code") or "error"
        detail = push.get("detail") or ""
        self.notify(f"{code}: {detail}", severity="error", timeout=6)

    async def _mount(self, widget: Any) -> None:
        view = self.query_one(TranscriptView)
        await view.mount(widget)
        view.scroll_end(animate=False)

    def _remember(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind == "worker_status":
            wid = event.get("worker_id")
            if isinstance(wid, str):
                self._last_worker_id = wid
        elif kind == "permission_request" and not event.get("resolved"):
            tuid = event.get("tool_use_id")
            if isinstance(tuid, str):
                self._last_tool_use_id = tuid

    # ── input ─────────────────────────────────────────────────────────

    @on(Input.Submitted, "#input")
    async def _handle_input(self, event: Input.Submitted) -> None:
        text = (event.value or "").strip()
        event.input.value = ""
        if not text:
            return
        if text in (":quit", ":q"):
            await self.action_quit_session()
            return
        cmd = text.split(maxsplit=1)[0]
        if cmd in (":approve", ":deny"):
            await self._handle_approval_cmd(
                text, approved=cmd == ":approve"
            )
            return
        # Audit-3 M6 — Textual TUI used to only special-case `:quit` /
        # `:approve` / `:deny`, sending every other line as user_input.
        # The legacy CLI supports a full `/-command` surface; restore
        # parity here so /help, /clear, /sessions, /new, /delete, /title,
        # /reload, /detach, /quit, /shutdown all work in the new UI.
        if is_slash_command(text):
            await self._handle_slash(text)
            return
        try:
            await self._client.user_input(self._session_id, text)
            # Immediate feedback that the message landed + the turn is in
            # flight; cleared when the first reply / tool output arrives.
            await self._show_thinking()
        except ControllerClientError as exc:
            self.notify(f"send failed: {exc}", severity="error", timeout=6)

    async def _handle_slash(self, text: str) -> None:
        # Strip exactly one leading `/`. ``lstrip("/")`` would strip
        # every leading slash, turning ``//reload`` into ``reload``
        # which silently dispatches a real command — match the legacy
        # `slash_commands.dispatch` which slices a single SLASH_PREFIX.
        payload = text[1:] if text.startswith("/") else text
        parts = payload.split(maxsplit=1)
        name = (parts[0] if parts else "").lower()
        args = parts[1].strip() if len(parts) > 1 else ""
        handler = self._slash_handlers().get(name)
        if handler is None:
            await self._notice(
                f"unknown command: /{name} (try /help)", style="red"
            )
            return
        try:
            await handler(args)
        except ControllerClientError as exc:
            await self._notice(f"/{name}: {exc}", style="red")
        except Exception as exc:  # noqa: BLE001 — never crash the TUI
            log.exception("slash /%s failed", name)
            await self._notice(f"/{name}: {exc}", style="red")

    def _slash_handlers(self) -> dict[str, Any]:
        return {
            "help": self._slash_help,
            "clear": self._slash_clear,
            "sessions": self._slash_sessions,
            "new": self._slash_new,
            "delete": self._slash_delete,
            "title": self._slash_title,
            "reload": self._slash_reload,
            "detach": self._slash_detach,
            "quit": self._slash_quit,
            "shutdown": self._slash_shutdown,
            "theme": self._slash_theme,
            "copy": self._slash_copy,
            "raw": self._slash_raw,
        }

    async def _notice(
        self, message: str, *, style: str = "dim"
    ) -> None:
        await self._mount(Static(Text(message, style=style)))

    async def _slash_help(self, _args: str) -> None:
        lines = Text()
        lines.append("Slash commands\n", style="bold cyan")
        registry = [
            ("/help", "Show this help"),
            ("/clear", "Clear the visible transcript"),
            ("/sessions", "List sessions (● marks current)"),
            ("/new [title]", "Mint a new session"),
            ("/delete <id>", "Delete a non-attached session"),
            ("/title <text>", "Rename the current session"),
            ("/reload [config|roles|tools|all]", "Reload runtime config"),
            ("/detach", "Exit and leave the daemon running"),
            ("/quit", "Exit (same as :quit / Ctrl-C)"),
            ("/shutdown", "Exit and shut the daemon down"),
            ("/theme [name]", "Switch the TUI theme"),
            ("/copy [N]", "Copy the last N assistant blocks (clipboard)"),
            ("/raw [N]", "Show the last N transcript events as raw JSON"),
        ]
        for cmd, desc in registry:
            lines.append(f"  {cmd:<32}", style="bold")
            lines.append(f"{desc}\n", style="dim")
        # Cross-check with the legacy registry so divergence shows.
        legacy = set(known_commands())
        new = {cmd.lstrip("/").split()[0] for cmd, _ in registry}
        missing = legacy - new
        if missing:
            lines.append(
                f"  (legacy CLI also exposes: {', '.join(sorted(missing))})\n",
                style="dim yellow",
            )
        await self._mount(Static(lines))

    async def _slash_clear(self, _args: str) -> None:
        await self.action_clear_transcript()

    async def _slash_sessions(self, _args: str) -> None:
        sessions = await self._client.list_sessions()
        if not sessions:
            await self._notice("no sessions", style="dim")
            return
        lines = Text()
        lines.append(f"{len(sessions)} session(s):\n", style="bold cyan")
        for sess in sessions:
            sid = sess.get("session_id") or "?"
            status = sess.get("status") or "?"
            title = sess.get("title") or "(untitled)"
            marker = "● " if sid == self._session_id else "  "
            lines.append(
                f"{marker}{sid}  {status}  {title}\n", style="dim"
            )
        await self._mount(Static(lines))

    async def _slash_new(self, args: str) -> None:
        title = args.strip() or None
        attached = await self._client.new_session(title=title, origin="cli")
        record = attached.get("session") or {}
        new_id = record.get("session_id") or "?"
        await self._notice(
            f"+ minted session {new_id} — run `tars --session {new_id}` to attach",
            style="green",
        )

    async def _slash_delete(self, args: str) -> None:
        target = args.strip()
        if not target:
            await self._notice(
                "/delete requires a session id (see /sessions)", style="red"
            )
            return
        if target == self._session_id:
            await self._notice(
                "/delete cannot remove the attached session — run /quit, then `tars --delete <id>`",
                style="red",
            )
            return
        push = await self._client.delete_session(target)
        await self._notice(
            f"deleted session {push.get('session_id') or target}",
            style="green",
        )

    async def _slash_title(self, args: str) -> None:
        new_title = args.strip()
        if not new_title:
            await self._notice("/title requires a new title", style="red")
            return
        push = await self._client.rename_session(self._session_id, new_title)
        await self._notice(
            f"renamed to {push.get('title') or new_title}", style="green"
        )

    async def _slash_reload(self, args: str) -> None:
        target = args.strip() or "all"
        if target not in {"config", "roles", "tools", "all"}:
            await self._notice(
                f"/reload: unknown target {target!r} (config|roles|tools|all)",
                style="red",
            )
            return
        push = await self._client.reload(target)
        reloaded = ", ".join(push.get("reloaded") or [])
        await self._notice(
            f"reload {target}: {reloaded or 'no changes'}", style="cyan"
        )

    async def _slash_detach(self, _args: str) -> None:
        # Detach leaves the daemon running regardless of the
        # shutdown_on_exit flag the app was launched with.
        try:
            await self._client.detach(self._session_id)
        except ControllerClientError:
            pass
        self.exit(0)

    async def _slash_quit(self, _args: str) -> None:
        await self.action_quit_session()

    async def _slash_shutdown(self, _args: str) -> None:
        try:
            await self._client.shutdown()
        except ControllerClientError:
            pass
        self.exit(0)

    async def _slash_theme(self, args: str) -> None:
        # Audit-3 M8 — minimal theme switcher. Available names come from
        # ``_THEMES`` and are applied by toggling top-level CSS classes
        # on the Screen. ``/theme`` with no arg lists options.
        name = args.strip().lower()
        if not name:
            await self._notice(
                f"themes: {', '.join(_THEME_NAMES)} (current: {self._theme_name})",
                style="cyan",
            )
            return
        if name not in _THEME_NAMES:
            await self._notice(
                f"/theme: unknown theme {name!r} (try {', '.join(_THEME_NAMES)})",
                style="red",
            )
            return
        self._apply_theme(name)
        await self._notice(f"theme → {name}", style="green")

    async def _slash_copy(self, args: str) -> None:
        # Audit-3 M8 — copy last N assistant blocks to system clipboard
        # via OSC 52 (works in most modern terminals incl. Windows
        # Terminal, iTerm2, Alacritty, Wezterm). N defaults to 1.
        try:
            n = max(1, int(args.strip() or "1"))
        except ValueError:
            await self._notice("/copy: N must be an integer", style="red")
            return
        view = self.query_one(TranscriptView)
        assistants = [
            c for c in view.children if isinstance(c, AssistantMessage)
        ][-n:]
        if not assistants:
            await self._notice("/copy: no assistant text yet", style="dim")
            return
        parts = []
        for w in assistants:
            buf = getattr(w, "_buffer", "")
            if buf:
                parts.append(strip_persona_tags(buf))
        payload = "\n\n".join(parts).strip()
        if not payload:
            await self._notice("/copy: nothing to copy", style="dim")
            return
        # OSC 52: ESC ] 52 ; c ; <base64> BEL
        import base64
        encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        try:
            sys.stdout.write(f"\x1b]52;c;{encoded}\x07")
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            await self._notice(
                "/copy: terminal rejected OSC 52", style="red"
            )
            return
        await self._notice(
            f"copied {len(payload)} chars from {len(assistants)} block(s)",
            style="green",
        )

    async def _slash_raw(self, args: str) -> None:
        # Audit-3 M8 — dump last N transcript events as raw JSON so the
        # operator can inspect the wire format. N defaults to 5.
        import json
        try:
            n = max(1, int(args.strip() or "5"))
        except ValueError:
            await self._notice("/raw: N must be an integer", style="red")
            return
        recent = self._raw_buffer[-n:]
        if not recent:
            await self._notice("/raw: no events buffered", style="dim")
            return
        body = Text()
        for evt in recent:
            try:
                line = json.dumps(evt, indent=2, sort_keys=True, default=str)
            except (TypeError, ValueError):
                line = repr(evt)
            body.append(line + "\n", style="dim")
        await self._mount(Static(body))

    async def _handle_approval_cmd(
        self, text: str, *, approved: bool
    ) -> None:
        parts = text.split(maxsplit=1)
        tool_use_id = (
            parts[1].strip()
            if len(parts) > 1 and parts[1].strip()
            else self._last_tool_use_id
        )
        if not tool_use_id:
            self.notify(
                f"no pending permission to {'approve' if approved else 'deny'}",
                severity="warning",
                timeout=4,
            )
            return
        try:
            await self._client.approval(
                self._session_id, tool_use_id, approved=approved
            )
        except ControllerClientError as exc:
            self.notify(
                f"approval failed: {exc}", severity="error", timeout=6
            )

    # ── actions ───────────────────────────────────────────────────────

    async def action_request_quit(self) -> None:
        """Ctrl+C handler — arms on first press, quits on the second
        within ``_QUIT_ARM_SECONDS``. Prevents an accidental Ctrl+C from
        dropping the session mid-work (claude-CLI parity)."""
        now = time.monotonic()
        if (
            self._quit_armed_at is not None
            and now - self._quit_armed_at <= self._QUIT_ARM_SECONDS
        ):
            await self.action_quit_session()
            return
        self._quit_armed_at = now
        self.notify(
            "Press Ctrl+C again to quit", severity="warning", timeout=self._QUIT_ARM_SECONDS
        )

    async def action_quit_session(self) -> None:
        """The real teardown — bound to ``:quit`` / ``/quit`` and the
        second Ctrl+C. Honors the ``shutdown_on_exit`` flag the caller
        passed (default: shut the daemon down; ``--keep`` flips it off)."""
        try:
            if self._shutdown_on_exit:
                await self._client.shutdown()
            else:
                await self._client.detach(self._session_id)
        except ControllerClientError:
            pass
        self.exit(0)

    async def action_clear_transcript(self) -> None:
        """Ctrl+L — clear the visible transcript without touching the
        on-disk record. Useful when the operator wants a fresh page."""
        view = self.query_one(TranscriptView)
        for child in list(view.children):
            await child.remove()
        self._tool_blocks.clear()
        self._stream_blocks.clear()
        self._spawn_tool_to_handle.clear()
        self._spawn_streams.clear()
        self._current_assistant = None

    # ── timer ─────────────────────────────────────────────────────────

    def _on_tick(self) -> None:
        self.query_one(StatusBar).tick()
        try:
            self.query_one(BackgroundJobsBar).tick()
        except Exception:  # noqa: BLE001 — bar may not be mounted yet
            pass

    # ── theming ────────────────────────────────────────────────────────

    def _apply_theme(self, name: str) -> None:
        """Apply a theme via Textual's runtime theme registry.

        Earlier draft (audit-3 review) mutated ``screen.styles`` keys
        which only repaints the Screen's inline background — child
        widgets read ``$background`` / ``$surface`` / ``$primary`` etc.
        from the App's design-variable table, not from the Screen's
        inline styles, so the previous implementation looked like a
        no-op for every widget below the Screen. The correct API is
        ``App.register_theme(Theme(...))`` + ``App.theme = name``
        which Textual then propagates through CSS-variable resolution.
        """
        palette = _THEMES.get(name)
        if palette is None:
            return
        self._theme_name = name
        try:
            from textual.theme import Theme

            theme = Theme(
                name=name,
                primary=palette["primary"],
                secondary=palette["accent"],
                accent=palette["accent"],
                background=palette["background"],
                surface=palette["surface"],
                panel=palette["surface"],
                dark=name != "light",
                variables={
                    "text": palette["text"],
                    "text-muted": palette["text-muted"],
                },
            )
            # ``register_theme`` is idempotent for the same name on
            # modern Textual; older builds raise if the name is taken,
            # which we swallow so a re-select doesn't error.
            try:
                self.register_theme(theme)
            except Exception:  # noqa: BLE001
                pass
            self.theme = name  # triggers Textual's repaint cascade
        except Exception:  # noqa: BLE001 — theme is cosmetic; never crash
            log.debug("apply_theme(%s) raised", name, exc_info=True)

    # ── background-agent rail ─────────────────────────────────────────

    _MAX_TRACKED_SPAWNS = 50

    def _register_spawn_job(self, handle: str) -> None:
        """Register (or refresh) a background-spawn rail job keyed by its
        handle. Called when a delegate_* returns a background handle."""
        now = time.monotonic()
        job = self._jobs.get(handle)
        if job is None:
            self._jobs[handle] = {
                "worker_id": handle,
                "kind": "delegate",
                "status": "running",
                "progress": "",
                "started_at": now,
                "updated_at": now,
            }
        self._prune_spawn_tracking()
        self._sync_rail()

    def _prune_spawn_tracking(self) -> None:
        """Bound the spawn-linkage maps so a long session doesn't
        accumulate dead handles. Drops the oldest tool→handle mappings
        and their buffered streams once over the cap."""
        if len(self._spawn_tool_to_handle) <= self._MAX_TRACKED_SPAWNS:
            return
        excess = len(self._spawn_tool_to_handle) - self._MAX_TRACKED_SPAWNS
        for tool_id in list(self._spawn_tool_to_handle)[:excess]:
            handle = self._spawn_tool_to_handle.pop(tool_id, None)
            if handle is not None:
                self._spawn_streams.pop(handle, None)

    def _sync_rail(self) -> None:
        """Push current job state into the rail + the running-count
        badge on the statusline. Safe before the widgets are mounted."""
        try:
            bar = self.query_one(BackgroundJobsBar)
            for job in self._jobs.values():
                bar.update_job(dict(job))
            running = sum(
                1
                for j in self._jobs.values()
                if str(j.get("status")) in {"running", "starting", "queued"}
            )
            self.query_one(StatusBar).set_job_count(running)
        except Exception:  # noqa: BLE001 — rail is optional UI
            log.debug("rail sync failed", exc_info=True)

    def _update_job_from_event(self, event: dict[str, Any]) -> None:
        worker_id = event.get("worker_id") or ""
        if not worker_id:
            return
        now = time.monotonic()
        status = str(event.get("status") or "?")
        job = self._jobs.get(worker_id)
        if job is None:
            job = {
                "worker_id": worker_id,
                "kind": str(event.get("worker_kind") or "worker"),
                "status": status,
                "progress": str(event.get("progress") or ""),
                "started_at": now,
                "updated_at": now,
            }
            self._jobs[worker_id] = job
        else:
            job["status"] = status
            job["progress"] = str(event.get("progress") or job.get("progress") or "")
            job["updated_at"] = now
        # Sync with the rail + statusbar (running-only count).
        try:
            bar = self.query_one(BackgroundJobsBar)
            bar.update_job(dict(job))
            running = sum(
                1
                for j in self._jobs.values()
                if str(j.get("status")) in {"running", "starting", "queued"}
            )
            self.query_one(StatusBar).set_job_count(running)
        except Exception:  # noqa: BLE001 — rail is optional UI; never crash
            log.debug("background rail update failed", exc_info=True)


__all__ = [
    "AssistantMessage",
    "StatusBar",
    "TarsApp",
    "ToolBlock",
    "TranscriptView",
    "UserMessage",
    "strip_persona_tags",
    "summarize_input",
]
