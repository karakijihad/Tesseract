"""Anthropic SDK streaming adapter — direct API path for Claude models.

Pairs with the existing ``cli.claude`` subscription path (which spawns the
``claude`` CLI for free-at-use heavy lifting). This adapter is for cost-tracked
direct API calls — useful as a chat_brain fallback, or whenever a role pins
``api.anthropic.<model>`` in ``roles.yaml``.

Streaming uses ``client.messages.stream()`` async context manager. Events
mapped to the shared StreamChunk format so ChatSession stays adapter-agnostic.
Prompt caching is enabled by default: the system prompt becomes a single
cache-controlled text block, so identical system prompts across requests reuse
the cache prefix (≤90% input-token discount).
"""

from __future__ import annotations

import asyncio
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

# Stop reasons meaning the message was cut off mid-generation, as opposed to
# declined (`refusal`) or completed. Only these justify discarding a tool call
# whose arguments failed to parse — everywhere else, malformed JSON is the
# model's mistake and is passed through for the tool to reject.
_TRUNCATING_STOP_REASONS = frozenset({"max_tokens"})


class AnthropicAdapter(ModelAdapter):
    """Async streaming adapter against the Anthropic Messages API.

    Required constructor args mirror the other API adapters: every value comes
    from ``providers.yaml`` via the loader — no defaults baked in here.
    """

    def __init__(
        self,
        *,
        api_key: str,
        timeout: float,
        max_retries: int,
        base_url: str | None = None,
        health_check_model: str | None = None,
    ) -> None:
        from anthropic import AsyncAnthropic

        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = AsyncAnthropic(**kwargs)
        self.max_retries = max_retries
        # Catalog-driven health-check probe. boot.build_adapter passes the
        # configured model id so a future catalog rename doesn't strand a
        # hardcoded string here. Falls back to a safe default only when no
        # adapter context is available (e.g. direct unit-test instantiation).
        self._health_check_model = health_check_model

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        opts = options or AdapterOptions()
        system, msg_list = _to_anthropic_messages(messages)

        kwargs: dict[str, Any] = {
            "model": opts.model,
            "messages": msg_list,
            "max_tokens": opts.max_output_tokens,
        }
        # Omitted, not defaulted: `api.anthropic.opus_5` declares no
        # temperature because that generation rejects the sampling parameters
        # outright. Sending one would 400 the request.
        if opts.temperature is not None:
            kwargs["temperature"] = opts.temperature
        if system:
            # System prompt as a single cache-controlled block — identical
            # system text across requests hits the prompt cache.
            kwargs["system"] = [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }]
        if tools:
            kwargs["tools"] = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("input_schema", {}),
                }
                for t in tools
            ]

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                async for chunk in self._do_stream(kwargs):
                    yield chunk
                return
            except Exception as e:
                name = type(e).__name__.lower()
                if "connect" in name or "timeout" in name or "apiconnection" in name:
                    last_error = e
                    if attempt < self.max_retries - 1:
                        wait = _BACKOFF_BASE * (2 ** attempt)
                        logger.warning(
                            "Anthropic connection error (attempt %d/%d), retrying in %.1fs",
                            attempt + 1, self.max_retries, wait,
                        )
                        await asyncio.sleep(wait)
                else:
                    yield StreamChunk(
                        type=ChunkType.ERROR,
                        error=f"Anthropic error: {e}",
                        error_kind=classify_exception(e),
                    )
                    return

        yield StreamChunk(
            type=ChunkType.ERROR,
            error=f"Anthropic unavailable after {self.max_retries} retries: {last_error}",
            error_kind=classify_exception(last_error) if last_error else ErrorKind.TRANSIENT,
        )

    async def _do_stream(self, kwargs: dict[str, Any]) -> AsyncGenerator[StreamChunk, None]:
        # tool block accumulator: content-block index → ToolCall in progress
        tool_acc: dict[int, dict[str, Any]] = {}
        # Closed tool blocks, held until the stop reason arrives — see
        # `content_block_stop` below.
        finalized: list[dict[str, Any]] = []
        usage_raw: dict[str, int] = {}
        stop_reason = "end_turn"

        async with self.client.messages.stream(**kwargs) as stream:
            async for event in stream:
                etype = getattr(event, "type", "")

                if etype == "content_block_start":
                    block = getattr(event, "content_block", None)
                    block_type = getattr(block, "type", "") if block else ""
                    if block_type == "tool_use":
                        idx = getattr(event, "index", -1)
                        call_id = getattr(block, "id", "") or ""
                        name = getattr(block, "name", "") or ""
                        tool_acc[idx] = {"id": call_id, "name": name, "args": ""}
                        yield StreamChunk(
                            type=ChunkType.TOOL_CALL_START,
                            tool_call_id=call_id,
                            tool_call=ToolCall(id=call_id, name=name, input={}),
                        )

                elif etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    delta_type = getattr(delta, "type", "") if delta else ""
                    if delta_type == "text_delta":
                        yield StreamChunk(type=ChunkType.TEXT, text=getattr(delta, "text", ""))
                    elif delta_type == "thinking_delta":
                        # Extended-thinking models stream reasoning as its own
                        # block type. Same operator-visible THINKING channel
                        # as the openai adapter's `reasoning_content`.
                        thinking = getattr(delta, "thinking", "")
                        if thinking:
                            yield StreamChunk(type=ChunkType.THINKING, thinking=thinking)
                    elif delta_type == "input_json_delta":
                        idx = getattr(event, "index", -1)
                        acc = tool_acc.get(idx)
                        if acc is not None:
                            partial = getattr(delta, "partial_json", "")
                            acc["args"] += partial
                            yield StreamChunk(
                                type=ChunkType.TOOL_CALL_DELTA,
                                text=partial,
                                tool_call_id=acc["id"],
                            )

                elif etype == "content_block_stop":
                    idx = getattr(event, "index", -1)
                    acc = tool_acc.pop(idx, None)
                    if acc is not None:
                        # Held rather than emitted. Anthropic sends the stop
                        # reason in `message_delta`, which arrives AFTER every
                        # content block has closed — so at this point we cannot
                        # yet tell a finished tool call from one whose arguments
                        # were cut off. Emitting here handed the tool loop
                        # `{"raw": "{\"path\": \"C:/Us"}` to execute.
                        finalized.append(acc)

                elif etype == "message_delta":
                    delta = getattr(event, "delta", None)
                    sr = getattr(delta, "stop_reason", None) if delta else None
                    if sr:
                        # `tool_use` and `end_turn` are the clean endings and
                        # keep their normalised names. Everything else
                        # (`max_tokens`, `stop_sequence`, `refusal`) passes
                        # through verbatim — collapsing those to `end_turn`
                        # told the tool loop a truncated response had
                        # finished, which is how a cut-off turn came back
                        # indistinguishable from a complete one.
                        stop_reason = "tool_use" if sr == "tool_use" else str(sr)
                    usage = getattr(event, "usage", None)
                    if usage:
                        out_tokens = getattr(usage, "output_tokens", None)
                        if out_tokens is not None:
                            usage_raw["output_tokens"] = int(out_tokens)

                elif etype == "message_start":
                    msg = getattr(event, "message", None)
                    usage = getattr(msg, "usage", None) if msg else None
                    if usage:
                        # Anthropic reports input_tokens EXCLUSIVE of cache reads
                        # and cache creations. The cost ledger's `_compute_usd`
                        # uses OpenAI/Gemini shape (input_tokens = total, then
                        # subtract cached_tokens for the uncached portion). Fold
                        # cache_read into input_tokens here so the math holds;
                        # cache_creation stays separate because it's billed via
                        # its own 1.25× term and folding it in would double-count.
                        in_tokens = int(getattr(usage, "input_tokens", 0) or 0)
                        cached_raw = getattr(usage, "cache_read_input_tokens", None)
                        cache_create_raw = getattr(usage, "cache_creation_input_tokens", None)
                        cached = int(cached_raw) if cached_raw is not None else 0
                        usage_raw["input_tokens"] = in_tokens + cached
                        if cached_raw is not None:
                            usage_raw["cached_tokens"] = cached
                        if cache_create_raw is not None:
                            usage_raw["cache_creation_tokens"] = int(cache_create_raw)

        # Tool calls are released here, once `stop_reason` is known. A call
        # whose arguments are unparseable AND whose message was truncated was
        # cut off mid-JSON: those arguments are unfinished, not malformed, and
        # executing them runs a tool on a fragment of its own input. Dropping
        # it leaves the response tool-less, so the chat loop sees the truncated
        # stop and retries. Unparseable JSON on a clean stop is the model's
        # mistake and still goes through for the tool to reject.
        truncated = stop_reason.strip().lower() in _TRUNCATING_STOP_REASONS
        for acc in finalized:
            try:
                parsed = json.loads(acc["args"]) if acc["args"] else {}
            except json.JSONDecodeError:
                if truncated:
                    logger.warning(
                        "anthropic: dropping tool call %r — arguments cut off by "
                        "stop_reason=%s (%d chars accumulated)",
                        acc["name"], stop_reason, len(acc["args"]),
                    )
                    continue
                parsed = {"raw": acc["args"]}
            yield StreamChunk(
                type=ChunkType.TOOL_CALL_END,
                tool_call_id=acc["id"],
                tool_call=ToolCall(id=acc["id"], name=acc["name"], input=parsed),
            )

        yield StreamChunk(type=ChunkType.STOP, stop_reason=stop_reason, raw={"usage": usage_raw})

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Best-effort token estimate via word count.

        The Messages API exposes ``client.messages.count_tokens()``, but it's
        async and ChatSession calls ``count_tokens`` synchronously for compact
        budget math. Fall back to a coarse char-based estimate (4 chars/token)
        — chat_brain only needs an upper bound for compact decisions, and the
        accurate count flows back through ``StreamChunk.raw["usage"]`` after
        the round-trip.
        """
        total_chars = 0
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        total_chars += len(str(block.get("text", "")))
                        total_chars += len(str(block.get("input", "")))
        return max(1, total_chars // 4)

    async def check_available(self) -> bool:
        """Smoke-test the credentials with a 1-token request.

        Anthropic doesn't expose a free ``/healthz`` for keyed requests; the
        cheapest live check is a 1-token completion against the smallest
        published model. We swallow auth/network errors and return False so a
        missing key never crashes startup probes.
        """
        if not self._health_check_model:
            logger.info("AnthropicAdapter.check_available skipped: no health_check_model wired")
            return False
        try:
            await self.client.messages.create(
                model=self._health_check_model,
                max_tokens=1,
                messages=[{"role": "user", "content": "ok"}],
            )
            return True
        except Exception as e:
            logger.info("AnthropicAdapter.check_available failed: %s", e)
            return False


def _to_anthropic_messages(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Translate Chat-Completions-shaped history → (system, Anthropic messages).

    ChatSession keeps history in the OpenAI shape everywhere (assistant
    `tool_calls` list, `role: "tool"` results). The Messages API rejects
    both — tool calls must be `tool_use` content blocks on the assistant
    message and results must be `tool_result` blocks inside the NEXT user
    message. Before this conversion existed the direct-API path 400'd on
    the second iteration of every tool loop (first call fine, round-trip
    fatal), so `api.anthropic.*` entries only worked for tool-free chat.

    Consecutive `role:"tool"` messages fold into one user message — the API
    requires every `tool_use` to be answered in the immediately-following
    user turn. Orphaned results (their `tool_use` dropped by history
    trimming) are stripped, mirroring the openai adapter's orphan guard.
    `_reasoning` marker messages are Responses-API-internal — skipped.
    """
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    tool_use_ids: set[str] = set()

    def _append_tool_result(block: dict[str, Any]) -> None:
        # Merge into a trailing user message that is already carrying
        # tool_result blocks; otherwise open a new one.
        if (
            out
            and out[-1]["role"] == "user"
            and isinstance(out[-1]["content"], list)
            and any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in out[-1]["content"]
            )
        ):
            out[-1]["content"].append(block)
        else:
            out.append({"role": "user", "content": [block]})

    for m in messages:
        if m.get("_reasoning"):
            continue
        role = m.get("role", "")
        content = m.get("content", "")

        if role == "system":
            if isinstance(content, str) and content:
                system_parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        system_parts.append(str(block.get("text", "")))
            continue

        if role == "tool":
            call_id = m.get("tool_call_id", "")
            if call_id not in tool_use_ids:
                continue  # orphan — its tool_use was trimmed from history
            _append_tool_result({
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": content if isinstance(content, str) else json.dumps(content),
            })
            continue

        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                blocks.extend(_convert_parts(content))
            for tc in m.get("tool_calls", []) or []:
                fn = tc.get("function", {})
                raw_args = fn.get("arguments", "{}")
                try:
                    parsed = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    parsed = {"raw": raw_args}
                call_id = tc.get("id", "")
                tool_use_ids.add(call_id)
                blocks.append({
                    "type": "tool_use",
                    "id": call_id,
                    "name": fn.get("name", ""),
                    "input": parsed if isinstance(parsed, dict) else {"raw": parsed},
                })
            if blocks:  # Messages API rejects empty assistant content
                out.append({"role": "assistant", "content": blocks})
            continue

        if role == "user":
            if isinstance(content, str):
                if content:
                    out.append({"role": "user", "content": content})
            elif isinstance(content, list):
                parts = _convert_parts(content)
                if parts:
                    out.append({"role": "user", "content": parts})

    return ("\n\n".join(system_parts), out)


def _convert_parts(content: list[Any]) -> list[dict[str, Any]]:
    """Convert OpenAI-style multimodal parts → Anthropic content blocks."""
    blocks: list[dict[str, Any]] = []
    for p in content:
        if not isinstance(p, dict):
            continue
        kind = p.get("type")
        if kind in ("text", "input_text") and isinstance(p.get("text"), str):
            blocks.append({"type": "text", "text": p["text"]})
        elif kind in ("image", "input_image"):
            data = p.get("data")
            mime = p.get("mime_type")
            if isinstance(data, str) and data and mime:
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": data},
                })
        elif kind in ("file", "input_file"):
            data = p.get("data") or p.get("file_data")
            mime = p.get("mime_type") or "application/pdf"
            if isinstance(data, str) and data.startswith("data:"):
                # file_data may arrive as a data URI; Anthropic wants raw base64
                header, _, payload = data.partition(",")
                mime = header.removeprefix("data:").split(";")[0] or mime
                data = payload
            if isinstance(data, str) and data:
                blocks.append({
                    "type": "document",
                    "source": {"type": "base64", "media_type": mime, "data": data},
                })
    return blocks
