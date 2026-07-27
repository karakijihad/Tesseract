from __future__ import annotations

from typing import Any


class ClaudeTurnAccumulator:
    """Folds claude `--output-format stream-json` events into one turn.

    Event shapes (Claude Code stream-json):
      - {"type":"system","subtype":"init","session_id": "..."}  -> session id
      - {"type":"assistant","message":{"content":[{"type":"text","text":...}]}}
      - {"type":"result","subtype":"success"|"error_*","result": "...",
         "usage": {...}, "is_error": bool}  -> turn complete
    """

    def __init__(self) -> None:
        self.session_id: str | None = None
        self._text_parts: list[str] = []
        self._result_field: str | None = None
        self.usage: dict[str, Any] = {}
        self.done: bool = False
        self.is_error: bool = False

    def feed(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "system" and event.get("subtype") == "init":
            sid = event.get("session_id")
            if isinstance(sid, str):
                self.session_id = sid
        elif etype == "assistant":
            msg = event.get("message") or {}
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    self._text_parts.append(str(block.get("text") or ""))
        elif etype == "result":
            self.done = True
            self.is_error = bool(event.get("is_error")) or str(
                event.get("subtype") or ""
            ).startswith("error")
            res = event.get("result")
            if isinstance(res, str):
                self._result_field = res
            usage = event.get("usage")
            if isinstance(usage, dict):
                self.usage = dict(usage)

    @property
    def result_text(self) -> str:
        if self._result_field is not None:
            return self._result_field
        return "".join(self._text_parts).strip()


class CodexTurnAccumulator:
    """Folds `codex exec --json` newline-delimited events into one turn.

    Event shapes (codex-cli 0.131.0 --json):
      - {"type":"thread.started","thread_id":"<uuid>"}          -> session_id
      - {"type":"item.completed","item":{"type":"agent_message","text":"..."}}
                                                                 -> result text
      - {"type":"turn.completed","usage":{...}}                  -> done
      - {"type":"error","message":"..."}  |
        {"type":"turn.failed","error":"..."}                     -> done + is_error
    Non-agent_message item types (reasoning, command_execution, file_change)
    are silently ignored for result_text purposes.
    """

    def __init__(self) -> None:
        self.session_id: str | None = None
        self._text_parts: list[str] = []
        self._error_text: str | None = None
        self.usage: dict[str, Any] = {}
        self.done: bool = False
        self.is_error: bool = False

    def feed(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "thread.started":
            tid = event.get("thread_id")
            if isinstance(tid, str):
                self.session_id = tid
        elif etype == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                text = item.get("text")
                if text is not None:
                    self._text_parts.append(str(text))
        elif etype == "turn.completed":
            self.done = True
            usage = event.get("usage")
            if isinstance(usage, dict):
                self.usage = dict(usage)
        elif etype in ("error", "turn.failed"):
            self.done = True
            self.is_error = True
            msg = event.get("message") or event.get("error")
            if isinstance(msg, str):
                self._error_text = msg

    @property
    def result_text(self) -> str:
        if self._error_text is not None:
            return self._error_text
        return "".join(self._text_parts).strip()
