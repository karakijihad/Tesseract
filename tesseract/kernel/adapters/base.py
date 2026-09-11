"""Model adapter ABC — the interface all adapters implement.

The loop never knows which adapter it's using. All yield identical
StreamChunk format. Swap models by changing config, not code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncGenerator, ClassVar

from tesseract.kernel.state import ToolCall

#: Sidecar key naming the last message a provider may cache up to.
#:
#: Caching is a prefix match, so how much of a request can be reused is
#: decided by where its first difference from the last request sits. That is
#: a fact about message ORDER, and message order is assembled once, for every
#: provider, in `ChatSession._assemble_for_turn`. It stamps this key on the
#: last message whose bytes hold still between turns; everything after it is
#: this turn's own state and is expected to be re-read.
#:
#: An adapter either declares that boundary in whatever form its provider
#: takes, or ignores the key. Anthropic puts a `cache_control` breakpoint
#: there, because its cache is only where you say it is. OpenAI's Responses
#: API reads it too when the catalog entry asks for explicit caching: it
#: places `prompt_cache_breakpoint` items and only then sends the explicit
#: mode. OpenAI's Chat Completions path and Gemini match the longest prefix
#: themselves and need nothing, which is what this said about OpenAI as a
#: whole until one of its two wire paths learned to read the key.
#:
#: What no adapter does is decide WHERE the boundary is. It cannot see the
#: turn — only a flat list — so it can only guess from message roles, and two
#: adapters guessing separately is how the same conversation got two
#: different cache behaviours.
#:
#: Ours, not the provider's: strip it before the request goes out.
CACHE_BOUNDARY = "_cache_boundary"

#: The keys `ToolRegistry.schemas_for_adapter` adds to say what a tool IS, and
#: which every projection strips before the payload reaches a provider.
#: `defer_loading` is the one exception: a deferring adapter puts it back,
#: because there it is the provider's own vocabulary rather than ours.
_PAYLOAD_KEYS = frozenset({"defer_loading", "_runtime_search"})


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
    # Does this model take `prompt_cache_options` / `prompt_cache_breakpoint`?
    # A catalog fact, not a family guess: gpt-5.6-luna accepts both and
    # gpt-5.4-mini answers each with a 400, and both sit on the Responses
    # path behind the same connection. Absence means the provider picks the
    # breakpoint, which is what every non-OpenAI entry wants.
    prompt_cache_explicit: bool = False
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


def call_timeout(options: AdapterOptions) -> float:
    """How long a caller may wait on this ref, per the catalog.

    `role_chain._options_for_ref` carries `connection.timeout_seconds` here,
    and `timeout_seconds` is required on every connection — so a caller that
    wraps `generate()` in a wait has a number to use and nothing to invent.
    Raises when the key is absent, which means the options were hand-built:
    the alternative is a constant deciding, and two of those already reported
    a 300 s CLI as drift for being slower than 30.
    """
    extra = options.extra or {}
    if "timeout_seconds" not in extra:
        raise KeyError(
            f"AdapterOptions for {options.provider or '?'}/{options.model or '?'} "
            "carries no timeout_seconds — build it with role_chain, or pass the "
            "connection's own value"
        )
    return float(extra["timeout_seconds"])


@dataclass(frozen=True)
class UsageStats:
    input_tokens: int = 0
    output_tokens: int = 0


class ModelAdapter(ABC):
    #: Does this adapter want the WHOLE registry, with everything outside the
    #: working set flagged `defer_loading`, instead of the filtered set?
    #:
    #: The working set exists because a demoted tool costs a round trip to
    #: reach: the model calls `tool_search`, the turn ends, the answer comes
    #: back, a new turn begins. One provider removes that price — it matches
    #: deferred tools server-side and appends their schemas INSIDE the same
    #: request, so the model never leaves the turn.
    #:
    #: Declared rather than inferred, and false by default, so an adapter that
    #: says nothing keeps filtering exactly as before. The alternative is a
    #: provider check at the call site, which is the shape this runtime spent
    #: three phases removing.
    #:
    #: **Visibility only.** Deferring changes what is LOADED, never what is
    #: permitted — `permissions.yaml` decides authority either way.
    defers_tool_loading: ClassVar[bool] = False

    def project_tools(
        self, tools: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]] | None:
        """Turn the runtime's one payload into what THIS provider receives.

        `ToolRegistry.schemas_for_adapter` classifies every tool once per turn
        and translates nothing. This is the translation, and it happens inside
        `stream()` so it happens per REQUEST: a chain whose primary defers and
        whose fallback cannot is two different wire payloads built from one
        frozen classification, and neither entry can be handed the other's.

        **Both halves live here, chosen by `defers_tool_loading`**, rather than
        each adapter overriding for itself. An adapter that declared deferral
        and forgot to override would have been handed the wrong projection in
        silence, which is a worse failure than the one this replaced: it would
        send `defer_loading` to a provider that has never heard of it, or the
        whole registry to one that cannot defer. There is one implementation
        and the flag is the only input.

        A provider that cannot defer gets exactly the payload that existed
        before any of this: the working set, our own `tool_search` so the
        model can still reach what was dropped, and no private key left on
        anything.

        A provider that can gets the opposite: the deferred entries kept, so
        it can match them server-side and append the schemas it needs inside
        the same request, and our search tool dropped because it brings its
        own. Two doors into one room is a choice the model should not be asked
        to make.

        Never a payload where everything defers. The API refuses one with
        nothing loaded, and there would be nothing to search FROM, so the
        flags come off and the whole registry travels as ordinary tools. That
        is expensive and it is not a failed turn.
        """
        if not tools:
            return tools
        if not self.defers_tool_loading:
            return [
                {k: v for k, v in t.items() if k not in _PAYLOAD_KEYS}
                for t in tools
                if not t.get("defer_loading")
            ]
        kept = [
            {k: v for k, v in t.items() if k != "_runtime_search"}
            for t in tools
            if not t.get("_runtime_search")
        ]
        if kept and all(t.get("defer_loading") for t in kept):
            for t in kept:
                t.pop("defer_loading", None)
        return kept

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
