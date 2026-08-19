"""Rolling transcript per conversation + the machine's PTY buffer.

Two shapes, two owners. `ObservationTranscript` is a conversation's own
rolling window and lives on the `ChatSession` — every entry point that
builds one gets a transcript by construction, so two conversations can
never interleave into one. `PtyBuffer` is the machine's terminal
context: panes belong to the host, not to a chat, so one buffer is held
by the `Observer` and rendered into every prompt.

Instance state is in-memory only; reset on observer disarm, on the
conversation ending, or on server restart.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Literal, TypedDict

CHAT_TURN_CAP = 48          # 4x DEFAULT_CONTEXT_TURNS from observer.py
PTY_LINE_CAP = 200          # ~one terminal screen of scrollback per _shared/observer-state-shape.md
PTY_LINE_MAX_CHARS = 2048   # per-line cap into the observer prompt (fix-pass SEC-1)

# ANSI CSI escape sequences — strip before passing PTY text to the LLM.
# Covers colors (\x1b[1;31m), cursor moves (\x1b[2J), OSC title sets
# (\x1b]0;title\x07), and C1 controls. Conservative: drops escape
# sequences but keeps the surrounding printable text.
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07]*\x07")
_ANSI_OTHER_RE = re.compile(r"\x1b[@-_]")

class PtyLine(TypedDict):
    role: Literal["pty"]
    pane_id: str
    text: str
    timestamp: str


# memory_deltas stream is deferred — see fix-pass 2026-04-20 / Codex #6.
# The MemoryDelta dataclass + its feed path were removed; a future
# memory-save subscription can reintroduce it alongside a real caller.


@dataclass
class ObservationTranscript:
    """One conversation's rolling window. Owned by its `ChatSession`."""

    chat_turns: Deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=CHAT_TURN_CAP)
    )

    def append_chat_turns(self, new_turns: list[dict[str, Any]]) -> int:
        """Append user/assistant text turns from `new_turns`.

        Callers pass an already-deduped delta — `ChatSession` maintains
        `_observer_last_index` for that. Filter drops tool stubs and
        empty-content turns; the role+content sequence preserves
        insertion order. Returns the number of turns appended.
        """
        added = 0
        for turn in new_turns:
            role = turn.get("role")
            if role not in ("user", "assistant"):
                continue
            content = turn.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            self.chat_turns.append({"role": role, "content": content})
            added += 1
        return added

    def reset(self) -> None:
        self.chat_turns.clear()


@dataclass
class PtyBuffer:
    """The machine's terminal context. Owned by the `Observer`.

    A pane belongs to the host rather than to a conversation, so this is
    one buffer whatever is being observed, and `drop_pane` — a consent
    revoke — has to clear it everywhere at once to mean anything.
    """

    lines: Deque[PtyLine] = field(
        default_factory=lambda: deque(maxlen=PTY_LINE_CAP)
    )

    def append_lines(self, lines: list[PtyLine]) -> int:
        """Strip ANSI escape sequences and cap line length before
        buffering. PTY output is sent verbatim to the observer's LLM
        prompt; keeping raw escapes + unbounded length would (a) waste
        tokens on non-printable control bytes and (b) inject terminal
        title sequences / OSC codes that the LLM might echo back into
        chat. Security: terminals frequently carry secrets in ANSI-
        formatted output (key dumps, env listings) — stripping doesn't
        sanitise secrets, but the cap prevents single-line exfiltration
        of a whole file into one observer call."""
        added = 0
        for line in lines:
            text = line.get("text")
            if not isinstance(text, str):
                continue
            cleaned = _ANSI_CSI_RE.sub("", text)
            cleaned = _ANSI_OSC_RE.sub("", cleaned)
            cleaned = _ANSI_OTHER_RE.sub("", cleaned)
            if not cleaned.strip():
                continue
            if len(cleaned) > PTY_LINE_MAX_CHARS:
                cleaned = cleaned[:PTY_LINE_MAX_CHARS]
            self.lines.append({**line, "text": cleaned})
            added += 1
        return added

    def drop_pane(self, pane_id: str) -> int:
        kept = [line for line in self.lines if line.get("pane_id") != pane_id]
        dropped = len(self.lines) - len(kept)
        if dropped:
            self.lines.clear()
            self.lines.extend(kept)
        return dropped

    def reset(self) -> None:
        self.lines.clear()
