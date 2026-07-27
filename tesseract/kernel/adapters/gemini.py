"""Google Gemini streaming adapter.

Uses the google-genai SDK async client. Same interface as every other
adapter — yields StreamChunk, retries on connect/timeout with exponential
backoff, logs at WARNING on transient failures.

System prompt is extracted from the messages list (role=='system') and
passed via GenerateContentConfig.system_instruction; the rest is converted
to Gemini's `contents` shape (role + parts[{text}]).

Tool calls come as single events in Gemini (not streamed tokens). We emit
TOOL_CALL_START with the full input; no TOOL_CALL_DELTA stream, and a
TOOL_CALL_END to match the shape other adapters use.
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
_GEMINI_SCHEMA_DROP_KEYS = {
    "$defs",
    "$schema",
    "additionalProperties",
    "default",
    "exclusiveMaximum",
    "exclusiveMinimum",
    "title",
}


class GeminiAdapter(ModelAdapter):
    def __init__(self, *, api_key: str, timeout: float, max_retries: int) -> None:
        from google import genai

        self._genai = genai
        self.client = genai.Client(api_key=api_key)
        self.timeout = timeout
        self.max_retries = max_retries

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        opts = options or AdapterOptions()

        system_msg, contents = self._split_messages(messages)
        config_kwargs = self._build_config(opts, system_msg, tools)

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                async for chunk in self._do_stream(
                    model=opts.model, contents=contents, config_kwargs=config_kwargs
                ):
                    yield chunk
                return
            except Exception as e:
                error_name = type(e).__name__.lower()
                transient = (
                    "connect" in error_name
                    or "timeout" in error_name
                    or "unavailable" in error_name
                )
                if transient and attempt < self.max_retries - 1:
                    last_error = e
                    wait = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "Gemini transient error (attempt %d/%d), retrying in %.1fs: %s",
                        attempt + 1, self.max_retries, wait, e,
                    )
                    await asyncio.sleep(wait)
                else:
                    yield StreamChunk(
                        type=ChunkType.ERROR,
                        error=f"Gemini error: {e}",
                        error_kind=classify_exception(e),
                    )
                    return

        yield StreamChunk(
            type=ChunkType.ERROR,
            error=f"Gemini unavailable after {self.max_retries} retries: {last_error}",
            error_kind=classify_exception(last_error) if last_error else ErrorKind.TRANSIENT,
        )

    async def _do_stream(
        self,
        *,
        model: str,
        contents: list[dict[str, Any]],
        config_kwargs: dict[str, Any],
    ) -> AsyncGenerator[StreamChunk, None]:
        types = self._genai.types
        config = types.GenerateContentConfig(**config_kwargs)

        stream = await self.client.aio.models.generate_content_stream(
            model=model, contents=contents, config=config,
        )

        stop_reason = ""
        usage_meta: Any = None
        tool_call_seq = 0
        async for chunk in stream:
            # Walk parts directly so thought parts (`part.thought` flag, sent
            # when a thinking model has include_thoughts on) surface as
            # THINKING instead of polluting answer text. Falls back to the
            # `.text` accessor when no parts are exposed (stubbed chunks).
            emitted_from_parts = False
            cand = self._first_candidate(chunk)
            for part in (getattr(getattr(cand, "content", None), "parts", None) or []):
                ptext = getattr(part, "text", None)
                if not ptext:
                    continue
                emitted_from_parts = True
                if getattr(part, "thought", False):
                    yield StreamChunk(type=ChunkType.THINKING, thinking=ptext)
                else:
                    yield StreamChunk(type=ChunkType.TEXT, text=ptext)
            if not emitted_from_parts:
                text = getattr(chunk, "text", None) or ""
                if text:
                    yield StreamChunk(type=ChunkType.TEXT, text=text)

            for call in self._extract_function_calls(chunk):
                name = call.get("name", "")
                raw_id = call.get("id")
                if raw_id:
                    tc_id = raw_id
                else:
                    tool_call_seq += 1
                    tc_id = f"gemini_{name or 'tool'}_{tool_call_seq}"
                args = call.get("args") or {}
                yield StreamChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call_id=tc_id,
                    tool_call=ToolCall(id=tc_id, name=name, input=args),
                )
                yield StreamChunk(type=ChunkType.TOOL_CALL_END, tool_call_id=tc_id)

            cand = self._first_candidate(chunk)
            finish = getattr(cand, "finish_reason", None) if cand else None
            if finish:
                stop_reason = str(finish).lower()

            # Gemini emits cumulative usage_metadata on chunks; the last non-None
            # value is authoritative. Captured here so the STOP envelope matches
            # OpenAI's `raw={"usage": {...}}` shape and the cost ledger sees both
            # input and cached counts (not just output).
            meta = getattr(chunk, "usage_metadata", None)
            if meta is not None:
                usage_meta = meta

        usage_raw: dict[str, int] = {}
        if usage_meta is not None:
            prompt = getattr(usage_meta, "prompt_token_count", 0) or 0
            candidates = getattr(usage_meta, "candidates_token_count", 0) or 0
            cached = getattr(usage_meta, "cached_content_token_count", 0) or 0
            usage_raw = {
                "input_tokens": int(prompt),
                "output_tokens": int(candidates),
            }
            if cached:
                usage_raw["cached_tokens"] = int(cached)

        yield StreamChunk(
            type=ChunkType.STOP,
            stop_reason=stop_reason or "end",
            raw={"usage": usage_raw} if usage_raw else None,
        )

    def _split_messages(
        self, messages: list[dict[str, Any]]
    ) -> tuple[str, list[dict[str, Any]]]:
        """Translate Chat-Completions-shaped history → (system, Gemini contents).

        Tool round-trip: assistant ``tool_calls`` become ``function_call``
        parts on a model turn; ``role:"tool"`` results become
        ``function_response`` parts on a user turn. Gemini keys responses by
        function NAME (not call id), so a call-id→name map is built while
        walking — an orphaned result whose call was trimmed from history is
        dropped (same guard as the openai/anthropic adapters). Before this
        conversion, tool results leaked into user turns as plain text and
        assistant tool_calls vanished, so multi-step tool loops degraded.
        ``_reasoning`` marker messages are Responses-API-internal — skipped.
        """
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []
        call_names: dict[str, str] = {}
        for msg in messages:
            if msg.get("_reasoning"):
                continue
            role = msg.get("role")
            content = msg.get("content", "")
            if role == "system":
                if isinstance(content, str) and content:
                    system_parts.append(content)
                continue
            if role == "tool":
                call_id = msg.get("tool_call_id", "")
                name = call_names.get(call_id)
                if not name:
                    continue  # orphan — its function_call was trimmed
                # `id` disambiguates parallel calls to the SAME function name
                # (two file_reads in one turn) — without it Gemini matches
                # responses by name alone and can cross-wire the results
                # (reviewer finding 2026-07-16).
                fr: dict[str, Any] = {
                    "name": name,
                    "response": {
                        "output": content if isinstance(content, str) else str(content),
                    },
                }
                if call_id:
                    fr["id"] = call_id
                contents.append({
                    "role": "user",
                    "parts": [{"function_response": fr}],
                })
                continue
            gemini_role = "model" if role == "assistant" else "user"
            parts: list[dict[str, Any]] = []
            if isinstance(content, str) and content:
                parts.append({"text": content})
            elif isinstance(content, list):
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    kind = p.get("type")
                    if kind in ("text", "input_text") and isinstance(p.get("text"), str):
                        parts.append({"text": p["text"]})
                    elif kind in ("image", "file") and p.get("data") and p.get("mime_type"):
                        parts.append({
                            "inline_data": {
                                "mime_type": p["mime_type"],
                                "data": p["data"],
                            },
                        })
            if role == "assistant":
                for tc in msg.get("tool_calls", []) or []:
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    call_id = tc.get("id", "")
                    call_names[call_id] = name
                    raw_args = fn.get("arguments", "{}")
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except json.JSONDecodeError:
                        args = {"raw": raw_args}
                    fc: dict[str, Any] = {
                        "name": name,
                        "args": args if isinstance(args, dict) else {"raw": args},
                    }
                    if call_id:
                        fc["id"] = call_id  # pairs with function_response.id
                    parts.append({"function_call": fc})
            if parts:
                contents.append({"role": gemini_role, "parts": parts})
        return "\n\n".join(system_parts), contents

    def _build_config(
        self,
        opts: AdapterOptions,
        system_msg: str,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        cfg: dict[str, Any] = {
            "temperature": opts.temperature,
            "max_output_tokens": opts.max_output_tokens,
        }
        if system_msg:
            cfg["system_instruction"] = system_msg
        if tools:
            cfg["tools"] = [self._convert_tool_schema(t) for t in tools]
        return cfg

    def _convert_tool_schema(self, tool: dict[str, Any]) -> Any:
        """Convert internal tool schema into google-genai Tool object."""
        types = self._genai.types
        return types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name=tool.get("name", ""),
                    description=tool.get("description", ""),
                    parameters=self._sanitize_schema(
                        tool.get("input_schema", tool.get("parameters", {}))
                    ),
                )
            ]
        )

    def _sanitize_schema(self, schema: Any) -> Any:
        """Drop JSON Schema keywords unsupported by Gemini FunctionDeclaration.

        Pydantic emits Draft 2020-12 details such as `exclusiveMinimum`;
        google-genai validates tool schemas against its narrower function
        declaration shape and rejects those keys before the request is sent.

        The `properties` map is special-cased: its keys are property names
        (not schema keywords), so dropping `title`/`default` there would erase
        a field named "title" or "default" from the surface — which then
        fails Gemini's required-property check (e.g. memory_save has a
        `title` field, fix 2026-04-29).
        """
        if isinstance(schema, list):
            return [self._sanitize_schema(item) for item in schema]
        if not isinstance(schema, dict):
            return schema
        cleaned: dict[str, Any] = {}
        for key, value in schema.items():
            if key in _GEMINI_SCHEMA_DROP_KEYS:
                continue
            if key == "properties" and isinstance(value, dict):
                cleaned[key] = {
                    prop_name: self._sanitize_schema(prop_schema)
                    for prop_name, prop_schema in value.items()
                }
                continue
            if key == "anyOf" and isinstance(value, list):
                non_null = [
                    self._sanitize_schema(item)
                    for item in value
                    if not (isinstance(item, dict) and item.get("type") == "null")
                ]
                if len(non_null) == 1:
                    cleaned.update(non_null[0])
                else:
                    cleaned[key] = non_null
                continue
            cleaned[key] = self._sanitize_schema(value)
        return cleaned

    def _first_candidate(self, chunk: Any) -> Any:
        cands = getattr(chunk, "candidates", None) or []
        return cands[0] if cands else None

    def _extract_function_calls(self, chunk: Any) -> list[dict[str, Any]]:
        cand = self._first_candidate(chunk)
        if not cand:
            return []
        content = getattr(cand, "content", None)
        parts = getattr(content, "parts", None) or []
        out: list[dict[str, Any]] = []
        for part in parts:
            fc = getattr(part, "function_call", None)
            if fc and getattr(fc, "name", None):
                out.append({
                    "id": getattr(fc, "id", "") or "",
                    "name": fc.name,
                    "args": dict(getattr(fc, "args", {}) or {}),
                })
        return out

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Rough estimate — one token per 4 chars. Matches other adapters' heuristic."""
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(content) // 4
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        if part.get("text"):
                            total += len(str(part.get("text", ""))) // 4
                        elif part.get("type") in ("image", "file"):
                            total += 256
        return total

    async def check_available(self) -> bool:
        try:
            await self.client.aio.models.list(config={"page_size": 1})
            return True
        except Exception as e:
            logger.warning("Gemini check_available failed: %s", e)
            return False
