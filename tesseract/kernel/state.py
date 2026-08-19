"""Immutable state for the agentic loop.

All state transitions use dataclasses.replace(). No mutation in the loop.
Recovery paths update state and continue — they never raise exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class RuntimeMode(str, Enum):
    FULL_HIVE = "full_hive"
    CLOUD_ASSISTED = "cloud_assisted"
    LOCAL_SOVEREIGN = "local_sovereign"
    SAFE_HOLD = "safe_hold"


class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class StopReason(str, Enum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"
    ERROR = "error"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DIMINISHING_RETURNS = "diminishing_returns"
    ESCALATION_EXHAUSTED = "escalation_exhausted"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]
    # An opaque token the provider issued WITH this call and requires back
    # when the call is replayed in history. Base64 text, so it survives the
    # session file and the wire; empty for every provider that asks for
    # nothing. Gemini 3 is why it exists: it returns a `thought_signature` on
    # each functionCall part and rejects the follow-up request without it, so
    # a tool loop that dropped this could complete exactly one turn.
    provider_signature: str = ""


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    output: str
    is_error: bool = False


@dataclass(frozen=True)
class Message:
    role: MessageRole
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
    timestamp: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.timestamp:
            object.__setattr__(
                self, "timestamp", datetime.now(timezone.utc).isoformat()
            )


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class RecoveryCounts:
    max_output_tokens: int = 0
    prompt_too_long: int = 0
    overloaded: int = 0
    escalations: int = 0


@dataclass(frozen=True)
class LoopState:
    session_id: str
    messages: tuple[Message, ...] = ()
    turn_count: int = 0
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    recovery_counts: RecoveryCounts = field(default_factory=RecoveryCounts)
    # Runtime must always supply this via Session (routing.yaml
    # default_chain[0]). The empty-string default exists only for test
    # fixtures that construct LoopState in isolation; any real turn that
    # reaches the router with "" will be resolved via the chain anyway.
    active_model_role: str = ""
    session_mode: RuntimeMode = RuntimeMode.FULL_HIVE
    circuit_breaker_flags: dict[str, bool] = field(default_factory=dict)
    consecutive_short_turns: int = 0
    stop_reason: StopReason | None = None
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            object.__setattr__(
                self, "created_at", datetime.now(timezone.utc).isoformat()
            )

    def append_message(self, message: Message) -> LoopState:
        return replace(self, messages=self.messages + (message,))

    def set_active_model_role(self, role: str) -> LoopState:
        return replace(self, active_model_role=role)

    def increment_turn(self) -> LoopState:
        return replace(self, turn_count=self.turn_count + 1)

    def increment_recovery(self, recovery_type: str) -> LoopState:
        current = self.recovery_counts
        counts = {
            "max_output_tokens": current.max_output_tokens,
            "prompt_too_long": current.prompt_too_long,
            "overloaded": current.overloaded,
            "escalations": current.escalations,
        }
        counts[recovery_type] = counts.get(recovery_type, 0) + 1
        return replace(
            self,
            recovery_counts=RecoveryCounts(**counts),
        )
