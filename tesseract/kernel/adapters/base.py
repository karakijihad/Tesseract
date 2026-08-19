"""Model adapter ABC — the interface all adapters implement.

The loop never knows which adapter it's using. All yield identical
StreamChunk format. Swap models by changing config, not code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncGenerator

from tesseract.kernel.state import ToolCall


class ChunkType(str, Enum):
    TEXT = "text"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_CALL_END = "tool_call_end"
    TOOL_RESULT = "tool_result"
    STOP = "stop"
    ERROR = "error"
    MODEL_SELECTED = "model_selected"
    THINKING = "thinking"  # streamed reasoning text delta (`thinking` field) — emitted as its own `thinking` stream kind, never appended to history
    REASONING_ITEM = "reasoning_item"  # OpenAI Responses API — encrypted reasoning blob for stateless reuse
    USER_INJECT = "user_inject"  # ChatSession-synthesized: operator typed a follow-up mid-turn; surfaced once injected into history at next tool boundary
    SPAWN_DONE = "spawn_done"  # ChatSession-synthesized: a background spawn (delegate_* / invoke_agent with background=true) completed; surfaced once at next tool boundary so the UI clears the "running" indicator and the assistant can spawn_await if it wants the result


class ErrorKind(str, Enum):
    """Classification used by `FallbackAdapter` to route on ERROR chunks.

    - `TRANSIENT`: a retry of the same chain entry is worth attempting
      (HTTP 408/425/429/5xx, network/timeout). Retried up to
      `chain.transient_retries` times before advancing to the next
      entry.
    - `HARD`: retrying won't help (auth, model-not-found, billing,
      malformed request, hard quota). Advance to the next entry
      immediately, no backoff.
    - `UNKNOWN`: adapter did not classify. Treated as `TRANSIENT` by
      the chain (safe default — retry first, then advance).
    """

    TRANSIENT = "transient"
    HARD = "hard"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class StreamChunk:
    type: ChunkType
    text: str = ""
    thinking: str = ""
    tool_call: ToolCall | None = None
    tool_call_id: str = ""
    stop_reason: str = ""
    error: str = ""
    error_kind: ErrorKind | None = None  # set on ERROR chunks; None on non-error chunks
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AdapterOptions:
    model: str = ""
    provider: str = ""          # provider tag for the resolved entry (cli/ollama/anthropic/openai/google)
    role: str = ""              # role label from roles.yaml (e.g. "chat_brain")
    tier: str = "api"           # role-level tier (api / cli / local — derived from roles.yaml)
    # `None` means the catalog entry declares no temperature, which is a
    # statement about the model rather than a missing value: the Claude Opus 5
    # generation removed the sampling parameters and 400s on them. Adapters
    # omit the field entirely when it is None — substituting a number here is
    # what sent 0.7 to a model that only accepts its own default.
    temperature: float | None = None
    max_output_tokens: int = 4096
    context_window: int = 32768
    reasoning_effort: str = ""  # OpenAI reasoning-effort field (model-agnostic)
    knowledge_cutoff: str = ""  # ISO date carried from roles.yaml — consumable by prompt builders
    use_responses_api: bool = False  # OpenAI only — prefer Responses API over Chat Completions
    # Whether to request a streamed response. A per-model property of the
    # catalog entry (`providers.yaml: stream: false`), not an adapter
    # constant — the catalog already owns every other per-model quirk
    # (`use_responses_api`, `reasoning_effort`). Absence means stream, which
    # is what nearly every entry wants; set it false for an endpoint that
    # serves the model reliably only in one shot.
    stream: bool = True
    think: bool | None = None   # Ollama: enable interleaved thinking for models that support it
    keep_alive: str = ""        # Ollama: how long to keep model loaded (e.g. "30m", "1h", "0")
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UsageStats:
    input_tokens: int = 0
    output_tokens: int = 0


class ModelAdapter(ABC):
    @abstractmethod
    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        yield  # type: ignore[misc]

    @abstractmethod
    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Estimate the prompt's size, for compaction and the chain's guard.

        Declared on the instance because two implementations genuinely need
        one — `FallbackAdapter` delegates to its primary and `MeteredAdapter`
        to what it wraps. Every concrete PROVIDER adapter is a `staticmethod`
        instead: their estimates are pure functions of the messages, and the
        chain's context-window guard has to ask a fallback entry for one
        WITHOUT constructing it, which is a 0.6–1.6 s client build for an
        entry that will usually never run.
        """
        ...

    @abstractmethod
    async def check_available(self) -> bool: ...

    async def generate(
        self,
        prompt: str,
        options: AdapterOptions | None = None,
    ) -> str:
        """Collect a full non-streaming response for a single prompt.

        Used by components that need a complete response (e.g. C-post selector).
        Default implementation consumes stream(); adapters may override with
        a native non-streaming endpoint for efficiency.
        """
        messages = [{"role": "user", "content": prompt}]
        parts: list[str] = []
        async for chunk in self.stream(messages, options=options):
            if chunk.type == ChunkType.TEXT:
                parts.append(chunk.text)
            elif chunk.type == ChunkType.ERROR:
                raise RuntimeError(f"Adapter error during generate: {chunk.error}")
        return "".join(parts)
