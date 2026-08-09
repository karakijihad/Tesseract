from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class SessionStatus(str, Enum):
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TurnResult:
    handle: str
    target: str                 # "claude" | "codex" | "<agent_name>"
    turn_index: int
    result_text: str
    usage: dict[str, Any] = field(default_factory=dict)
    status: SessionStatus = SessionStatus.DONE
    is_error: bool = False


class InteractiveSession(Protocol):
    """One multi-turn session against a backend. Stateful: holds the
    backend-specific resume key (CLI session id / live ChatSession)."""

    handle: str
    target: str

    async def open(self, task: str) -> TurnResult: ...
    async def send(self, message: str) -> TurnResult: ...
    async def close(self) -> None: ...
