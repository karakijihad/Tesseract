"""OpenAI SDK streaming adapter.

Two code paths, selected by `AdapterOptions.use_responses_api`:
  - False (default): client.chat.completions.create(stream=True)  — legacy path.
  - True:            client.responses.create(stream=True)          — OpenAI-recommended
    for GPT-5 family. Supports reasoning + encrypted_content reuse + prompt_cache_key.

Both paths emit identical StreamChunk events so ChatSession stays agnostic.
prompt_cache_key is auto-derived from the system prompt hash for free prompt caching
(≤90% input-token discount + ≤80% latency reduction on cache hits).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, AsyncGenerator

from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ErrorKind,
    ModelAdapter,
    StreamChunk,
)
from tesseract.kernel.adapters.errors import classify_exception
from tesseract.kernel.state import ToolCall

logger = logging.getLogger(__name__)

_BACKOFF_BASE = 1.0

# Finish reasons that mean the response was cut off mid-generation, as opposed
# to declined (`content_filter`) or completed. Only these justify discarding a
# tool call whose arguments failed to parse — everywhere else, malformed JSON
# is the model's mistake and is passed through for the tool to reject.
_TRUNCATING_FINISH_REASONS = frozenset({"length"})

_RESPONSES_HARD_CODES = frozenset({
    "invalid_request_error",
    "invalid_api_key",
    "authentication_error",
    "permission_denied",
    "model_not_found",
    "context_length_exceeded",
    "insufficient_quota",
    "billing_hard_limit_reached",
})

_RESPONSES_TRANSIENT_CODES = frozenset({
    "rate_limit_exceeded",
    "server_error",
    "service_unavailable",
    "timeout",
    "engine_overloaded",
})


def _usage_raw(usage: Any, in_field: str, out_field: str, details_field: str) -> dict[str, int]:
    """Normalise a usage object into the shape STOP carries to the ledger.

    Four call sites — streamed and one-shot, Chat Completions and Responses —
    apply the same three rules to two different sets of field names. Kept in
    one place so a correction lands on all four: the one-shot paths were added
    by copying the streamed extraction, and a rule applied to one twin and
    missed on the other is how the `TOOL_CALL_START` gap happened.
    """
    if not usage:
        return {}
    out = {
        "input_tokens": getattr(usage, in_field, 0) or 0,
        "output_tokens": getattr(usage, out_field, 0) or 0,
    }
    details = getattr(usage, details_field, None)
    cached = getattr(details, "cached_tokens", None) if details else None
    if cached is not None:
        out["cached_tokens"] = int(cached)
    return out


def _chat_usage(usage: Any) -> dict[str, int]:
    return _usage_raw(usage, "prompt_tokens", "completion_tokens", "prompt_tokens_details")


def _responses_usage(usage: Any) -> dict[str, int]:
    return _usage_raw(usage, "input_tokens", "output_tokens", "input_tokens_details")


def _chat_stop_reason(finish_reason: str) -> str:
    """Map a Chat Completions `finish_reason` onto our stop vocabulary.

    `tool_calls` and `stop` are the clean endings and get normalised names.
    Everything else (`length`, `content_filter`) passes through verbatim —
    collapsing those to `end_turn` told the tool loop a truncated response had
    finished, which is how a cut-off turn came back indistinguishable from a
    complete one.
    """
    if finish_reason == "tool_calls":
        return "tool_use"
    if finish_reason in ("stop", ""):
        return "end_turn"
    return finish_reason


def _classify_responses_error_code(code: str | None, msg: str) -> ErrorKind:
    if code:
        c = code.lower()
        if c in _RESPONSES_HARD_CODES:
            return ErrorKind.HARD
        if c in _RESPONSES_TRANSIENT_CODES:
            return ErrorKind.TRANSIENT
    lowered = (msg or "").lower()
    if any(s in lowered for s in ("insufficient_quota", "context length", "invalid api key", "authentication")):
        return ErrorKind.HARD
    if any(s in lowered for s in ("rate limit", "overloaded", "temporarily unavailable", "server error", "timeout")):
        return ErrorKind.TRANSIENT
    return ErrorKind.UNKNOWN


def _cache_key_from_system(system_text: str) -> str:
    """Stable 16-hex cache-routing key derived from the system prompt.

    Identical system prompts across requests → identical key → OpenAI
    routes requests to the same cache node → prefix reuse.
    """
    if not system_text:
        return ""
    return hashlib.sha1(system_text.encode("utf-8")).hexdigest()[:16]


# Chars of system prompt hashed into the cache-routing header value. Only
# the PREFIX is hashed: the assembled prompt ends with an ephemeral
# "Right now" block (minute-level local time), so a full-text hash would
# change every minute and route each turn to a different cache node —
# exactly the miss pattern the header exists to prevent. The first 2k
# chars are the static IDENTITY head, stable across turns and sessions.
_ROUTING_KEY_PREFIX_CHARS = 2000


class OpenAIAdapter(ModelAdapter):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout: float,
        max_retries: int,
        supports_prompt_cache_key: bool = False,
        supports_stream_usage: bool = True,
        cache_routing_header: str | None = None,
    ) -> None:
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        self.max_retries = max_retries
        # openai-COMPATIBLE providers (NIM, etc.) reuse this adapter but 400
        # on the OpenAI-only `prompt_cache_key` param — only send it to real OpenAI.
        self._supports_prompt_cache_key = supports_prompt_cache_key
        # Per the OpenAI streaming spec, token usage is only reported when
        # `stream_options: {"include_usage": true}` is sent — and it arrives
        # in a dedicated final chunk whose `choices` array is EMPTY (xAI
        # follows this to the letter; vLLM-backed providers like NIM accept
        # the flag too). Opt out per provider in providers.yaml
        # (`supports_stream_usage: false`) if a compat endpoint 400s on it.
        self._supports_stream_usage = supports_stream_usage
        # Providers with automatic-but-node-local prompt caching (xAI) need
        # a routing header (`x-grok-conv-id`) so same-prefix requests land
        # on the same cache node; without it they scatter and never hit.
        # Header NAME comes from providers.yaml (`cache_routing_header`).
        self._cache_routing_header = cache_routing_header

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        opts = options or AdapterOptions()

        if opts.use_responses_api:
            async for chunk in self._stream_responses(messages, tools, opts):
                yield chunk
            return

        async for chunk in self._stream_chat_completions(messages, tools, opts):
            yield chunk

    # ─── Chat Completions (legacy) ──────────────────────────────────────────

    async def _stream_chat_completions(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        opts: AdapterOptions,
    ) -> AsyncGenerator[StreamChunk, None]:
        # Strip reasoning-item markers — only Responses API cares about them.
        clean_messages = [
            self._to_chat_completions_message(m)
            for m in messages
            if not m.get("_reasoning")
        ]
        system_text = next((m.get("content", "") for m in clean_messages if m.get("role") == "system"), "")

        kwargs: dict[str, Any] = {
            "model": opts.model,
            "messages": clean_messages,
            "max_completion_tokens": opts.max_output_tokens,
            "stream": opts.stream,
        }
        # See `AdapterOptions.temperature` — absent in the catalog means the
        # model does not take one, so the key is omitted rather than invented.
        if opts.temperature is not None:
            kwargs["temperature"] = opts.temperature
        # Streaming-only param: sending it on a non-streamed request is a 400
        # on spec-faithful providers.
        if opts.stream and self._supports_stream_usage:
            kwargs["stream_options"] = {"include_usage": True}
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {}),
                    },
                }
                for t in tools
            ]
        if opts.reasoning_effort:
            kwargs["reasoning_effort"] = opts.reasoning_effort
        if self._supports_prompt_cache_key:
            cache_key = _cache_key_from_system(system_text)
            if cache_key:
                kwargs["prompt_cache_key"] = cache_key
        if self._cache_routing_header:
            route_key = _cache_key_from_system(system_text[:_ROUTING_KEY_PREFIX_CHARS])
            if route_key:
                # SDK-level kwarg — rides as an HTTP header, not request body,
                # so compat endpoints can't 400 on it.
                kwargs["extra_headers"] = {self._cache_routing_header: route_key}

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                producer = self._do_stream if opts.stream else self._do_request
                async for chunk in producer(kwargs):
                    yield chunk
                return
            except Exception as e:
                error_name = type(e).__name__
                if "connect" in error_name.lower() or "timeout" in error_name.lower():
                    last_error = e
                    if attempt < self.max_retries - 1:
                        wait = _BACKOFF_BASE * (2 ** attempt)
                        logger.warning("OpenAI connection error (attempt %d/%d), retrying in %.1fs", attempt + 1, self.max_retries, wait)
                        await asyncio.sleep(wait)
                else:
                    yield StreamChunk(
                        type=ChunkType.ERROR,
                        error=f"OpenAI error: {e}",
                        error_kind=classify_exception(e),
                    )
                    return

        # All in-adapter retries exhausted on a connection/timeout error.
        # Surface as TRANSIENT so the chain may try the same provider once
        # more (chain-level retries operate after SDK-level retries).
        yield StreamChunk(
            type=ChunkType.ERROR,
            error=f"OpenAI unavailable after {self.max_retries} retries: {last_error}",
            error_kind=classify_exception(last_error) if last_error else ErrorKind.TRANSIENT,
        )

    async def _do_request(self, kwargs: dict[str, Any]) -> AsyncGenerator[StreamChunk, None]:
        """One-shot Chat Completions, yielding the same chunk contract as the
        streamed path so nothing downstream can tell the difference.

        Truncation is classified from `finish_reason` exactly as the streamed
        path does, and a tool call cut off mid-JSON is dropped rather than
        executed on a fragment of its own arguments.
        """
        resp = await self.client.chat.completions.create(**kwargs)

        usage_raw = _chat_usage(getattr(resp, "usage", None))

        choice = resp.choices[0] if resp.choices else None
        message = getattr(choice, "message", None) if choice else None

        if message is not None:
            reasoning = (
                getattr(message, "reasoning_content", None)
                or getattr(message, "reasoning", None)
            )
            if isinstance(reasoning, str) and reasoning:
                yield StreamChunk(type=ChunkType.THINKING, thinking=reasoning)
            if message.content:
                yield StreamChunk(type=ChunkType.TEXT, text=message.content)

        _finish = str(getattr(choice, "finish_reason", "") or "") if choice else ""
        stop_reason = _chat_stop_reason(_finish)

        for tc in (getattr(message, "tool_calls", None) or []) if message else []:
            fn = getattr(tc, "function", None)
            raw_args = getattr(fn, "arguments", "") or ""
            try:
                parsed = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                if _finish in _TRUNCATING_FINISH_REASONS:
                    logger.warning(
                        "openai: dropping tool call %r — arguments cut off by "
                        "finish_reason=%s (%d chars)",
                        getattr(fn, "name", ""), _finish, len(raw_args),
                    )
                    continue
                parsed = {"raw": raw_args}
            call = ToolCall(id=tc.id, name=getattr(fn, "name", ""), input=parsed)
            # START before END even with no deltas between them: consumers key
            # off START to capture a call at all (`session_ops._reflect`), and
            # the streamed paths, Gemini and Anthropic all pair them.
            yield StreamChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call_id=tc.id,
                tool_call=call,
            )
            yield StreamChunk(
                type=ChunkType.TOOL_CALL_END,
                tool_call_id=tc.id,
                tool_call=call,
            )

        yield StreamChunk(
            type=ChunkType.STOP,
            stop_reason=stop_reason or "end_turn",
            raw={"usage": usage_raw},
        )

    async def _do_stream(self, kwargs: dict[str, Any]) -> AsyncGenerator[StreamChunk, None]:
        tool_calls_acc: dict[int, dict[str, str]] = {}
        usage_raw: dict[str, int] = {}
        stop_reason = ""

        stream = await self.client.chat.completions.create(**kwargs)
        async for chunk in stream:
            # Usage can ride on ANY chunk. With `stream_options.include_usage`
            # (OpenAI spec, xAI follows it) it arrives in a dedicated final
            # chunk whose `choices` is EMPTY — so it MUST be read before the
            # empty-choice skip below. NIM instead attaches it to the
            # finish_reason chunk; both shapes land here. Last non-empty wins.
            if chunk.usage:
                usage_raw = _chat_usage(chunk.usage)

            choice = chunk.choices[0] if chunk.choices else None
            if not choice:
                continue

            delta = choice.delta

            # Reasoning models on compat providers (xAI grok, DeepSeek,
            # NIM-served thinkers, GLM with thinking on) stream chain-of-
            # thought in `reasoning_content` (some gateways name it
            # `reasoning`), separate from `content`. The SDK parses unknown
            # fields into model extras, so getattr sees them. Surfaced as
            # THINKING chunks — operator-visible, never appended to history.
            if delta:
                reasoning = (
                    getattr(delta, "reasoning_content", None)
                    or getattr(delta, "reasoning", None)
                )
                if isinstance(reasoning, str) and reasoning:
                    yield StreamChunk(type=ChunkType.THINKING, thinking=reasoning)

            if delta and delta.content:
                yield StreamChunk(type=ChunkType.TEXT, text=delta.content)

            if delta and delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {"id": "", "name": "", "arguments": ""}

                    acc = tool_calls_acc[idx]
                    if tc_delta.id:
                        acc["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            acc["name"] = tc_delta.function.name
                            yield StreamChunk(
                                type=ChunkType.TOOL_CALL_START,
                                tool_call_id=acc["id"],
                                tool_call=ToolCall(id=acc["id"], name=acc["name"], input={}),
                            )
                        if tc_delta.function.arguments:
                            acc["arguments"] += tc_delta.function.arguments
                            yield StreamChunk(
                                type=ChunkType.TOOL_CALL_DELTA,
                                text=tc_delta.function.arguments,
                                tool_call_id=acc["id"],
                            )

            if choice.finish_reason:
                # `tool_calls` and `stop` are the clean endings and keep their
                # normalised names. Everything else (`length`,
                # `content_filter`) passes through verbatim — collapsing those
                # to `end_turn` told the tool loop a truncated response had
                # finished, which is how a cut-off turn came back
                # indistinguishable from a complete one.
                _finish = str(choice.finish_reason or "")
                stop_reason = _chat_stop_reason(_finish)

                for acc in tool_calls_acc.values():
                    try:
                        parsed = json.loads(acc["arguments"]) if acc["arguments"] else {}
                    except json.JSONDecodeError:
                        if _finish in _TRUNCATING_FINISH_REASONS:
                            # Cut off mid-JSON: these arguments are not
                            # malformed, they are unfinished. Handing them on as
                            # `{"raw": ...}` executes a tool with a fragment of
                            # its own input. Dropping the call leaves the
                            # response tool-less, so the chat loop sees the
                            # truncated stop and retries it instead.
                            logger.warning(
                                "openai: dropping tool call %r — arguments cut off by "
                                "finish_reason=%s (%d chars accumulated)",
                                acc["name"], _finish, len(acc["arguments"]),
                            )
                            continue
                        parsed = {"raw": acc["arguments"]}
                    yield StreamChunk(
                        type=ChunkType.TOOL_CALL_END,
                        tool_call_id=acc["id"],
                        tool_call=ToolCall(id=acc["id"], name=acc["name"], input=parsed),
                    )
                tool_calls_acc.clear()

        # STOP is deferred to stream end (NOT emitted at finish_reason):
        # with include_usage the usage chunk arrives AFTER the finish_reason
        # chunk, so emitting STOP there would ship zero usage to the cost
        # ledger — the 2026-07-15 "$0 grok spend" bug. A stream that ends
        # without any finish_reason still gets a STOP so callers terminate.
        yield StreamChunk(
            type=ChunkType.STOP,
            stop_reason=stop_reason or "end_turn",
            raw={"usage": usage_raw},
        )

    # ─── Responses API ──────────────────────────────────────────────────────

    async def _stream_responses(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        opts: AdapterOptions,
    ) -> AsyncGenerator[StreamChunk, None]:
        instructions, input_items = self._to_responses_input(messages)
        kwargs: dict[str, Any] = {
            "model": opts.model,
            "input": input_items,
            "max_output_tokens": opts.max_output_tokens,
            "stream": opts.stream,
            # stateless — we own history + reasoning stash. NOTE: `store` (and
            # the `include: reasoning.encrypted_content` below) are Responses-API-
            # only; an openai-COMPATIBLE provider (NIM, etc.) would 400 on them.
            # Safe today because only genuine OpenAI sets `use_responses_api: true`.
            # If a NIM model ever enables it, gate these like `prompt_cache_key`.
            "store": False,
        }
        # The Responses API takes a temperature too, and this path was dropping
        # the catalog's on the floor — invisible only because both entries that
        # use it happen to declare 1.0, which is also the API default. Omitted
        # when the entry declares none, as everywhere else.
        if opts.temperature is not None:
            kwargs["temperature"] = opts.temperature
        if instructions:
            kwargs["instructions"] = instructions
            if self._supports_prompt_cache_key:
                cache_key = _cache_key_from_system(instructions)
                if cache_key:
                    kwargs["prompt_cache_key"] = cache_key
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {}),
                }
                for t in tools
            ]
        if opts.reasoning_effort:
            kwargs["reasoning"] = {"effort": opts.reasoning_effort}
            # Only ask for encrypted reasoning when the model will actually generate some.
            # `none` is a valid effort meaning "skip reasoning" — no blob to include.
            if opts.reasoning_effort not in ("none",):
                kwargs["include"] = ["reasoning.encrypted_content"]

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                producer = (
                    self._do_stream_responses if opts.stream else self._do_request_responses
                )
                async for chunk in producer(kwargs):
                    yield chunk
                return
            except Exception as e:
                error_name = type(e).__name__
                if "connect" in error_name.lower() or "timeout" in error_name.lower():
                    last_error = e
                    if attempt < self.max_retries - 1:
                        wait = _BACKOFF_BASE * (2 ** attempt)
                        logger.warning(
                            "OpenAI Responses connection error (attempt %d/%d), retrying in %.1fs",
                            attempt + 1, self.max_retries, wait,
                        )
                        await asyncio.sleep(wait)
                else:
                    yield StreamChunk(
                        type=ChunkType.ERROR,
                        error=f"OpenAI Responses error: {e}",
                        error_kind=classify_exception(e),
                    )
                    return

        yield StreamChunk(
            type=ChunkType.ERROR,
            error=f"OpenAI Responses unavailable after {self.max_retries} retries: {last_error}",
            error_kind=classify_exception(last_error) if last_error else ErrorKind.TRANSIENT,
        )

    async def _do_request_responses(self, kwargs: dict[str, Any]) -> AsyncGenerator[StreamChunk, None]:
        """One-shot Responses call, yielding the streamed path's chunk contract.

        Keeps every distinction the streamed path learned to make: a refusal
        is text rather than silence, `status == "incomplete"` reports the
        truncation reason instead of a clean `end_turn`, and encrypted
        reasoning still round-trips as a REASONING_ITEM.
        """
        resp = await self.client.responses.create(**kwargs)

        emitted_any_tool = False
        for item in getattr(resp, "output", None) or []:
            itype = getattr(item, "type", "")

            if itype == "reasoning":
                encrypted = getattr(item, "encrypted_content", None)
                if encrypted:
                    yield StreamChunk(
                        type=ChunkType.REASONING_ITEM,
                        raw={"item": {
                            "type": "reasoning",
                            "id": getattr(item, "id", ""),
                            "encrypted_content": encrypted,
                        }},
                    )

            elif itype == "message":
                for part in getattr(item, "content", None) or []:
                    ptype = getattr(part, "type", "")
                    if ptype == "output_text":
                        text = getattr(part, "text", "")
                        if text:
                            yield StreamChunk(type=ChunkType.TEXT, text=text)
                    elif ptype == "refusal":
                        refusal = getattr(part, "refusal", "")
                        if refusal:
                            yield StreamChunk(type=ChunkType.TEXT, text=str(refusal))

            elif itype == "function_call":
                call_id = getattr(item, "call_id", "") or getattr(item, "id", "")
                name = getattr(item, "name", "") or ""
                raw_args = getattr(item, "arguments", "") or ""
                emitted_any_tool = True
                try:
                    parsed = json.loads(raw_args) if raw_args else {}
                except json.JSONDecodeError:
                    parsed = {"raw": raw_args}
                call = ToolCall(id=call_id, name=name, input=parsed)
                # START before END — see `_do_request` for why the pair matters.
                yield StreamChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call_id=call_id,
                    tool_call=call,
                )
                yield StreamChunk(
                    type=ChunkType.TOOL_CALL_END,
                    tool_call_id=call_id,
                    tool_call=call,
                )

        usage_raw = _responses_usage(getattr(resp, "usage", None))

        status = str(getattr(resp, "status", "") or "")

        # A failed Responses call comes back as HTTP 200 carrying
        # `status: "failed"` and an `error` object, so the SDK never raises and
        # the retry loop above never sees it. Without this branch the response
        # fell through to `end_turn`: no text, no ERROR, and — because STOP is
        # a committed chunk — `FallbackAdapter` recorded the turn as a SUCCESS,
        # resetting the breaker instead of retrying or advancing. That is the
        # silent turn P12 closed, reopened on a path that had no handler yet.
        if status in ("failed", "cancelled"):
            err_obj = getattr(resp, "error", None)
            msg = (
                getattr(err_obj, "message", None)
                or f"Responses call ended with status={status}"
            )
            code = getattr(err_obj, "code", None)
            yield StreamChunk(
                type=ChunkType.ERROR,
                error=f"OpenAI Responses error: {msg}",
                error_kind=_classify_responses_error_code(code, str(msg)),
            )
            return

        if status == "incomplete":
            incomplete = getattr(resp, "incomplete_details", None)
            reason = getattr(incomplete, "reason", None) if incomplete else None
            stop = str(reason) if reason else "incomplete"
        else:
            stop = "tool_use" if emitted_any_tool else "end_turn"
        yield StreamChunk(type=ChunkType.STOP, stop_reason=stop, raw={"usage": usage_raw})

    async def _do_stream_responses(self, kwargs: dict[str, Any]) -> AsyncGenerator[StreamChunk, None]:
        tool_acc: dict[str, dict[str, str]] = {}  # item_id → {call_id, name, args}
        emitted_any_tool = False
        refusal_streamed = False

        stream = await self.client.responses.create(**kwargs)
        async for event in stream:
            t = getattr(event, "type", "")

            if t == "response.output_text.delta":
                yield StreamChunk(type=ChunkType.TEXT, text=getattr(event, "delta", ""))

            # A refusal arrives on its own channel, not as output text. With no
            # handler the stream carried no text at all and the response still
            # completed cleanly, so the turn ended with an empty bubble and no
            # explanation — the same silence this phase exists to remove, on a
            # path the chat loop cannot distinguish from a deliberate one.
            elif t == "response.refusal.delta":
                delta = getattr(event, "delta", "")
                if delta:
                    refusal_streamed = True
                    yield StreamChunk(type=ChunkType.TEXT, text=delta)

            elif t == "response.refusal.done":
                # Only when nothing streamed — `done` repeats the full text,
                # and emitting both would print the refusal twice.
                if not refusal_streamed:
                    refusal = getattr(event, "refusal", "")
                    if refusal:
                        yield StreamChunk(type=ChunkType.TEXT, text=str(refusal))

            elif t == "response.output_item.added":
                item = getattr(event, "item", None)
                itype = getattr(item, "type", "") if item else ""
                if itype == "function_call":
                    item_id = getattr(item, "id", "") or getattr(item, "call_id", "")
                    call_id = getattr(item, "call_id", "") or item_id
                    name = getattr(item, "name", "") or ""
                    tool_acc[item_id] = {"call_id": call_id, "name": name, "args": ""}
                    emitted_any_tool = True
                    yield StreamChunk(
                        type=ChunkType.TOOL_CALL_START,
                        tool_call_id=call_id,
                        tool_call=ToolCall(id=call_id, name=name, input={}),
                    )

            elif t == "response.function_call_arguments.delta":
                item_id = getattr(event, "item_id", "")
                acc = tool_acc.get(item_id)
                if acc is not None:
                    delta = getattr(event, "delta", "")
                    acc["args"] += delta
                    yield StreamChunk(
                        type=ChunkType.TOOL_CALL_DELTA,
                        text=delta,
                        tool_call_id=acc["call_id"],
                    )

            elif t == "response.function_call_arguments.done":
                item_id = getattr(event, "item_id", "")
                acc = tool_acc.get(item_id)
                if acc is None:
                    continue
                try:
                    parsed = json.loads(acc["args"]) if acc["args"] else {}
                except json.JSONDecodeError:
                    parsed = {"raw": acc["args"]}
                yield StreamChunk(
                    type=ChunkType.TOOL_CALL_END,
                    tool_call_id=acc["call_id"],
                    tool_call=ToolCall(id=acc["call_id"], name=acc["name"], input=parsed),
                )

            elif t == "response.output_item.done":
                item = getattr(event, "item", None)
                if item is None:
                    continue
                if getattr(item, "type", "") == "reasoning":
                    encrypted = getattr(item, "encrypted_content", None)
                    if encrypted:
                        yield StreamChunk(
                            type=ChunkType.REASONING_ITEM,
                            raw={"item": {
                                "type": "reasoning",
                                "id": getattr(item, "id", ""),
                                "encrypted_content": encrypted,
                            }},
                        )

            # `response.incomplete` is the Responses-API truncation event —
            # the model hit `max_output_tokens` or a content filter mid-answer.
            # It was previously unhandled, so a truncated stream emitted no
            # STOP at all and the tool loop simply ran out of chunks: a turn
            # that was cut off looked exactly like a turn that finished.
            elif t in ("response.completed", "response.incomplete"):
                resp = getattr(event, "response", None)
                usage_raw = _responses_usage(
                    getattr(resp, "usage", None) if resp is not None else None
                )
                if t == "response.incomplete":
                    incomplete = getattr(resp, "incomplete_details", None) if resp else None
                    reason = getattr(incomplete, "reason", None) if incomplete else None
                    stop = str(reason) if reason else "incomplete"
                else:
                    stop = "tool_use" if emitted_any_tool else "end_turn"
                yield StreamChunk(type=ChunkType.STOP, stop_reason=stop, raw={"usage": usage_raw})

            elif t in ("response.failed", "error"):
                msg = getattr(event, "message", None) or "unknown Responses API failure"
                # Responses API surfaces `error.code` (e.g. "rate_limit_exceeded",
                # "context_length_exceeded", "invalid_api_key") on failure events.
                # Map to ErrorKind so the chain can retry vs advance correctly.
                err_obj = getattr(event, "error", None)
                code = getattr(err_obj, "code", None) or getattr(event, "code", None)
                kind = _classify_responses_error_code(code, msg)
                yield StreamChunk(
                    type=ChunkType.ERROR,
                    error=f"OpenAI Responses error: {msg}",
                    error_kind=kind,
                )
                return

    def _to_responses_input(
        self,
        messages: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        """Translate Chat-Completions-shaped history → (instructions, input items).

        Reasoning items (marked `_reasoning: True`) are re-hydrated as
        type="reasoning" items so encrypted_content round-trips.
        """
        instructions_parts: list[str] = []
        input_items: list[dict[str, Any]] = []
        for m in messages:
            if m.get("_reasoning"):
                input_items.append({
                    "type": "reasoning",
                    "id": m.get("id", ""),
                    "encrypted_content": m.get("encrypted_content", ""),
                    "summary": [],
                })
                continue
            role = m.get("role", "")
            content = m.get("content", "")
            if role == "system":
                if isinstance(content, str) and content:
                    instructions_parts.append(content)
                continue
            if role == "tool":
                input_items.append({
                    "type": "function_call_output",
                    "call_id": m.get("tool_call_id", ""),
                    "output": content if isinstance(content, str) else json.dumps(content),
                })
                continue
            if role == "assistant":
                if isinstance(content, str) and content:
                    input_items.append({
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                    })
                for tc in m.get("tool_calls", []) or []:
                    fn = tc.get("function", {})
                    input_items.append({
                        "type": "function_call",
                        "call_id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", "{}"),
                    })
                continue
            if role == "user":
                if isinstance(content, str):
                    input_items.append({
                        "role": "user",
                        "content": [{"type": "input_text", "text": content}],
                    })
                elif isinstance(content, list):
                    parts = []
                    for p in content:
                        if not isinstance(p, dict):
                            continue
                        kind = p.get("type")
                        if kind in ("text", "input_text") and isinstance(p.get("text"), str):
                            parts.append({"type": "input_text", "text": p["text"]})
                        elif kind in ("image", "input_image"):
                            image_url = p.get("image_url")
                            if not image_url and p.get("data") and p.get("mime_type"):
                                image_url = f"data:{p['mime_type']};base64,{p['data']}"
                            if isinstance(image_url, str) and image_url:
                                parts.append({"type": "input_image", "image_url": image_url})
                        elif kind in ("file", "input_file"):
                            file_part: dict[str, Any] = {"type": "input_file"}
                            if isinstance(p.get("filename"), str):
                                file_part["filename"] = p["filename"]
                            if isinstance(p.get("file_id"), str):
                                file_part["file_id"] = p["file_id"]
                            elif isinstance(p.get("file_data"), str):
                                file_part["file_data"] = p["file_data"]
                            elif isinstance(p.get("data"), str):
                                mime = p.get("mime_type") or "application/octet-stream"
                                file_part["file_data"] = f"data:{mime};base64,{p['data']}"
                            if "file_id" in file_part or "file_data" in file_part:
                                parts.append(file_part)
                    if parts:
                        input_items.append({"role": "user", "content": parts})

        # The Responses API rejects a `function_call_output` whose `call_id`
        # has no matching `function_call` in the same input ("No tool call
        # found for function call output with call_id ..." → HTTP 400, which
        # kills the turn on every fallback in the chain). History trimming
        # (prompt budget) or a tool-loop-cap reset can drop the assistant
        # `function_call` message while keeping the tool-result message —
        # strip those orphans so the request stays well-formed regardless of
        # how history was trimmed (2026-07-09 backend-halt fix).
        call_ids = {
            it.get("call_id")
            for it in input_items
            if it.get("type") == "function_call"
        }
        input_items = [
            it
            for it in input_items
            if it.get("type") != "function_call_output"
            or it.get("call_id") in call_ids
        ]

        return "\n\n".join(instructions_parts), input_items

    def _to_chat_completions_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        content = msg.get("content")
        if not isinstance(content, list):
            return msg
        parts: list[dict[str, Any]] = []
        for p in content:
            if not isinstance(p, dict):
                continue
            kind = p.get("type")
            if kind in ("text", "input_text") and isinstance(p.get("text"), str):
                parts.append({"type": "text", "text": p["text"]})
            elif kind in ("image", "input_image"):
                image_url = p.get("image_url")
                if not image_url and p.get("data") and p.get("mime_type"):
                    image_url = f"data:{p['mime_type']};base64,{p['data']}"
                if isinstance(image_url, str) and image_url:
                    parts.append({"type": "image_url", "image_url": {"url": image_url}})
            elif kind in ("file", "input_file"):
                # Chat Completions file part — mirrors the Responses API
                # path above so PDFs ride through to models that accept
                # them (GPT-5 family natively, plus any NIM model that
                # supports file parts). Models that don't will ignore the
                # part rather than choke. Prior behavior was a
                # `[attached file: ...]` text stub which lost the binary.
                file_part: dict[str, Any] = {"type": "file", "file": {}}
                if isinstance(p.get("filename"), str):
                    file_part["file"]["filename"] = p["filename"]
                if isinstance(p.get("file_id"), str):
                    file_part["file"]["file_id"] = p["file_id"]
                elif isinstance(p.get("file_data"), str):
                    file_part["file"]["file_data"] = p["file_data"]
                elif isinstance(p.get("data"), str):
                    mime = p.get("mime_type") or "application/octet-stream"
                    file_part["file"]["file_data"] = f"data:{mime};base64,{p['data']}"
                if "file_id" in file_part["file"] or "file_data" in file_part["file"]:
                    parts.append(file_part)
                else:
                    filename = p.get("filename") or "file"
                    parts.append({"type": "text", "text": f"[attached file: {filename}]"})
        return {**msg, "content": parts}

    # ─── Utilities ──────────────────────────────────────────────────────────

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        total = 0
        for msg in messages:
            if msg.get("_reasoning"):
                # encrypted_content isn't user-visible but consumes context
                total += len(msg.get("encrypted_content", "")) // 4
                continue
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(content) // 4
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if "text" in block:
                        total += len(block["text"]) // 4
                    elif block.get("type") in ("image", "input_image", "file", "input_file"):
                        total += 256
        return total

    async def check_available(self) -> bool:
        try:
            await self.client.models.list()
            return True
        except Exception:
            return False
