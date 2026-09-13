"""OpenAI SDK streaming adapter.

Two code paths, selected by `AdapterOptions.use_responses_api`:
  - False (default): client.chat.completions.create(stream=True)  — legacy path.
  - True:            client.responses.create(stream=True)          — OpenAI-recommended
    for GPT-5 family. Supports reasoning + encrypted_content reuse + prompt_cache_key.

Both paths emit identical StreamChunk events so ChatSession stays agnostic.
prompt_cache_key is auto-derived from the system prompt hash for free prompt caching
(≤90% input-token discount + ≤80% latency reduction on cache hits).

The Responses path also defers tool loading when the model entry asks for it:
tools outside the working set travel flagged rather than withheld, and the
provider appends the schemas it needs inside the same request. That matters
because `tools[]` renders BEFORE `system` and before the conversation, so
adding one unlocked schema mid-conversation used to re-read everything behind
it at full price.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, AsyncGenerator

from tesseract.kernel.adapters._estimate import tokens_from_chars
from tesseract.kernel.adapters.base import (
    CACHE_BOUNDARY,
    AdapterOptions,
    ChunkType,
    ErrorKind,
    ModelAdapter,
    StreamChunk,
)
from tesseract.kernel.adapters.errors import classify_exception
from tesseract.kernel.state import ToolCall
from tesseract.kernel.tools.taxonomy import GROUPS, heading_for

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

# The provider's own tool search, which is what makes deferral worth anything:
# it matches a deferred tool server-side and appends its schema INSIDE the same
# request, so a tool outside the working set costs no round trip. A wire
# constant like the `{"type": "function"}` envelope below; which models a role
# reaches stays `roles.yaml`'s business.
_TOOL_SEARCH_TOOL = {"type": "tool_search", "execution": "server"}


def _function_entry(t: dict[str, Any], *, defer: bool) -> dict[str, Any]:
    """One tool's wire shape, loaded or deferred. The one place both the flat
    working set and a namespace's members are built, so the two never drift
    into describing a tool differently."""
    entry: dict[str, Any] = {
        "type": "function",
        "name": t["name"],
        "description": t.get("description", ""),
        "parameters": t.get("input_schema", {}),
    }
    if defer:
        entry["defer_loading"] = True
    return entry


def _namespace_entries(deferred: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deferred tools, bucketed by `Tool.group` into `namespace` entries.

    Measured 2026-09-11 (M1 in the probe): grouping deferred tools under a
    `namespace` whose description names its members matches today's flat
    selection accuracy (3/5) at 10,005 tokens against 26,659. Bare namespaces
    (no member names) drop accuracy to 1/5, so the names are load-bearing and
    stay in the description, never trimmed as a saving.

    The two halves were measured hours apart and the roster moved between
    them: 26,659 at 158 tools, 10,005 at 159. The extra one is deferred, so it
    costs the second figure almost nothing, but the denominators are written
    down rather than smoothed, because a comparison whose sides were taken on
    different rosters should say so.

    One namespace per taxonomy group, sorted by slug for a stable payload;
    each namespace's members are sorted by name in both the description and
    the `tools` array, for the same reason. Sorted rather than in the
    taxonomy's own declared order because what matters here is that the same
    registry renders the same bytes, and `sorted` says so without depending on
    a dict's insertion order staying put.

    **A tool whose group this file does not know still travels.** Not every
    tool passes through the boot gate that validates `group`: an MCP server's
    tools are constructed and registered after boot (`mcp_client/remote_tool.py`
    declares no group at all, so they inherit `Tool.group = ""`), and they are
    `tier="extended"`, so they defer. Looking their heading up would raise
    `KeyError` inside `stream()` and end the turn before it reached the
    provider. Skipping them instead would be quieter and worse: they would
    vanish from the payload and the model could not call them.

    So an unknown slug goes in a catch-all namespace and stays reachable. The
    grouping is the part we lose, and losing the grouping is survivable in a
    way that losing the turn or losing the tool is not.
    """
    by_group: dict[str, list[dict[str, Any]]] = {}
    for t in deferred:
        slug = t.get("group") or ""
        by_group.setdefault(slug if slug in GROUPS else "", []).append(t)

    namespaces: list[dict[str, Any]] = []
    for slug in sorted(by_group):
        members = sorted(by_group[slug], key=lambda t: t["name"])
        names = ", ".join(t["name"] for t in members)
        heading = heading_for(slug) if slug else "Everything else"
        namespaces.append({
            "type": "namespace",
            "name": slug.replace("-", "_") if slug else "everything_else",
            "description": f"{heading}. Contains: {names}.",
            "tools": [_function_entry(t, defer=True) for t in members],
        })
    return namespaces


_RESPONSES_TRANSIENT_CODES = frozenset({
    "rate_limit_exceeded",
    "server_error",
    "service_unavailable",
    "timeout",
    "engine_overloaded",
})


def _usage_raw(
    usage: Any,
    in_field: str,
    out_field: str,
    in_details_field: str,
    out_details_field: str,
) -> dict[str, int]:
    """Normalise a usage object into the shape STOP carries to the ledger.

    Four call sites — streamed and one-shot, Chat Completions and Responses —
    apply the same rules to two different sets of field names. Kept in one
    place so a correction lands on all four: the one-shot paths were added by
    copying the streamed extraction, and a rule applied to one twin and missed
    on the other is how the `TOOL_CALL_START` gap happened.

    **A key the provider did not send is absent, never zero.** The ledger
    cannot otherwise tell a model that reports no cache from one that reported
    a cache and used none of it, and that distinction is what says whether a
    missing charge is a provider fact or a gap in this function. It was a gap
    here: cache reads were the only detail read, so 236M cached tokens went by
    without one recorded cache write, and the surcharge term in `_compute_usd`
    was never once exercised on the model doing nearly all the caching.

    Reasoning tokens are read but not separately billed: on the Responses API
    they are already inside `output_tokens`. Recording the split is how a
    reasoning-heavy role can be told apart from a verbose one on the same row.
    """
    if not usage:
        return {}
    out = {
        "input_tokens": getattr(usage, in_field, 0) or 0,
        "output_tokens": getattr(usage, out_field, 0) or 0,
    }
    for field, source, keys in (
        ("cached_tokens", in_details_field, ("cached_tokens",)),
        # Two names for one number, and the one we read was never the
        # provider's. OpenAI documents `cache_write_tokens`; the installed
        # SDK's `InputTokensDetails` declares neither, so whichever the
        # provider sends arrives as an extra field. Reading only
        # `cache_creation_tokens` meant this term has never once returned a
        # value on the path doing nearly all the caching, and a write nobody
        # can see is the surcharge `_compute_usd` never exercises. The output
        # key stays as it is: it is the ledger's provider-neutral name and
        # every row on disk already uses it.
        (
            "cache_creation_tokens",
            in_details_field,
            ("cache_write_tokens", "cache_creation_tokens"),
        ),
        ("reasoning_tokens", out_details_field, ("reasoning_tokens",)),
    ):
        details = getattr(usage, source, None)
        if not details:
            continue
        for key in keys:
            value = getattr(details, key, None)
            if value is not None:
                out[field] = int(value)
                break
    return out


def _chat_usage(usage: Any) -> dict[str, int]:
    return _usage_raw(
        usage,
        "prompt_tokens",
        "completion_tokens",
        "prompt_tokens_details",
        "completion_tokens_details",
    )


def _responses_usage(usage: Any) -> dict[str, int]:
    return _usage_raw(
        usage,
        "input_tokens",
        "output_tokens",
        "input_tokens_details",
        "output_tokens_details",
    )


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


# Chars of the system prompt that decide which cache a request is routed to.
# Only the HEAD is hashed. The assembled prompt ends in a minute-level clock
# and carries a memory capsule that changes whenever a memory is written, so
# hashing the whole of it produces a new key constantly and sends every turn
# to a cache that has never seen it. The first 2k chars are the identity head,
# stable across turns and sessions.
#
# The truncation used to apply to the routing header only, while both
# `prompt_cache_key` call sites hashed the whole prompt a few lines away from
# the comment saying not to. Measured over 120 turns before the fix: 65.7% hit
# rate, 41 full misses, 3.2M tokens re-processed at full price, and crossing a
# minute boundary tripled the miss rate.
_ROUTING_KEY_PREFIX_CHARS = 2000


def _conversation_lane(messages: list[dict[str, Any]] | None) -> str:
    """What tells this conversation apart from every other one on this machine.

    The earliest message that is not the system prompt. It is fixed for the
    life of a conversation, it is different in every conversation, and it is
    already in the request, so nothing has to be threaded through the chain
    or the session to reach here.

    A compaction rewrites the front of the history and therefore moves this.
    That is correct rather than a defect: the prefix genuinely changed, so the
    old lane holds nothing worth matching, and the next turn opens a new one.
    """
    for msg in messages or ():
        if not isinstance(msg, dict) or msg.get("role") == "system":
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            content = json.dumps(content, sort_keys=True, default=str)
        if content:
            return hashlib.sha1(content.encode("utf-8")).hexdigest()[:16]
    return ""


def _routing_key(system_text: str, messages: list[dict[str, Any]] | None = None) -> str:
    """Which cache this request belongs with, as 16 hex chars.

    Two halves, and both are needed.

    The **head** decides which prompt this is. Only the first 2k chars are
    hashed: the assembled prompt ends in a minute-level clock and carries a
    memory capsule that changes whenever a memory is written, so hashing all
    of it produces a new key constantly and sends every turn to a cache that
    has never seen it.

    The **conversation** decides which of this machine's chats it is. Without
    it the key collapses: `SOUL.md` is over 2,000 characters, so the hashed
    head is a fragment of SOUL and nothing else, identical for the cockpit,
    every channel chat, every sub-agent and all eleven roles that share this
    model. Measured 2026-08-30, two live conversations alternating a second
    apart: each sat pinned at exactly its own head size (32,804 and 32,547 of
    93,438 and 221,331 tokens) and neither ever kept its own tail. One label
    for everything is a label the provider cannot route on.

    Every caller goes through here, so the two consumers cannot drift apart:
    identical head and conversation, identical key, and the provider hands
    back the prefix it already processed.
    """
    head = system_text[:_ROUTING_KEY_PREFIX_CHARS]
    if not head:
        return ""
    lane = _conversation_lane(messages)
    material = f"{head}\x00{lane}" if lane else head
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


# How many breakpoints to offer. A read matches only against breakpoints
# present in THIS request, so one is never enough: the entry the last request
# wrote sits at the item that was ITS boundary, and this request has to be
# offering a breakpoint there too or the match is never attempted. Measured
# with a single breakpoint, correctly placed every turn: `cached=0`, six turns
# out of six. Two would do for a clean conversation; eight covers a tool loop,
# a retry, and a turn that added more than one message, and the provider reads
# the latest fifty anyway.
_CACHE_BREAKPOINTS = 8

#: How far apart the fixed anchors sit, in items. The grid exists so two
#: requests compute the SAME absolute positions; the stride decides how much
#: of the tail a miss re-reads and how far back the eight marks reach. At 32 a
#: turn appending an ordinary tool loop still shares every anchor, and seven
#: anchors span 224 items, which covers the longest conversation measured here
#: (974 items) at its own boundary rather than from the start.
_ANCHOR_STRIDE = 32


def _breakpoint_blocks(item: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The block list a breakpoint may hang on, or None if this item is not a
    place the provider will honour one.

    Measured against the API, not inferred: a marked user item is read back
    (9,028 of 9,048), a marked tool result is read back (13,553 of 13,593),
    and a marked ASSISTANT item reads back ZERO. That matches the provider's
    own account of where it puts an implicit breakpoint, the end of the latest
    user or TOOL message, with the assistant turn named nowhere.

    Marking an assistant item is therefore not a wasted marker but a silent
    one: a conversation whose only marks were assistant turns sat at 30% for
    sixty calls while the live tool traffic behind them was re-read every
    iteration.

    A tool result keeps its text under `output`, a user message under
    `content`, which is the only reason this is not a one-line check.
    """
    if item.get("type") == "function_call_output":
        blocks = item.get("output")
    elif item.get("role") == "user":
        blocks = item.get("content")
    else:
        return None
    if isinstance(blocks, list) and blocks and isinstance(blocks[-1], dict):
        return blocks
    return None


def _mark_breakpoints(items: list[dict[str, Any]], boundary_at: int | None) -> int:
    """Offer the provider somewhere to match, and somewhere to write.

    Implicit mode puts its one breakpoint at the end of the latest user or
    tool message, which on this runtime is the per-turn state block: a message
    rebuilt at the new end of every request, so the prefix it stores is one no
    later request reproduces. Measured cost of that: the whole conversation,
    every turn, `cached` frozen at the head.

    `CACHE_BOUNDARY` names the last item that survives unchanged into the next
    request, and the Anthropic adapter has been reading it for its own
    breakpoints all along. Everything after it, the state block included, then
    falls outside the cached prefix, where the provider charges it as uncached
    input and it disturbs nothing.

    Marks walk BACKWARD from there, onto the items `_breakpoint_blocks`
    admits. The marker goes on a block and 400s on an item (`Unknown
    parameter: input[0].prompt_cache_breakpoint`), so `function_call`,
    `reasoning` and assistant items are stepped over: a shorter prefix caches
    less, a rejected request caches nothing and takes the turn with it.

    **One mark hugs the boundary and the rest sit on a fixed grid**, because a
    read needs THIS request's marks to share a position with the LAST one's.
    Marking the eight eligible items nearest the boundary is a window that
    slides by however many items the turn appended, so a tool loop that added
    more than the window is wide moves every mark past the previous set and
    the whole prefix is re-read. Measured over 296 calls, 2026-09-01 to 09-03:
    with no shared index, zero reads in 295 pairs; two pairs had none and both
    were turns that appended more items than the window held, one of them
    263,420 tokens. Eleven more survived on a single shared mark.

    `items` is append-only, so `index // _ANCHOR_STRIDE` names the same
    position in both requests however many arrived between them. That is the
    whole of it: the grid is absolute, the window was relative.

    The boundary mark is kept as well as the grid, and is why this is not
    purely a grid: the anchors can sit well behind the boundary, and without a
    mark near it everything since the last anchor is re-read every turn.

    Returns how many were placed. Zero means the caller must leave
    `prompt_cache_options` off entirely, because in explicit mode a request
    with no breakpoint is a request with no caching at all.
    """
    if boundary_at is None:
        return 0

    def _eligible_at_or_below(start: int) -> int | None:
        for index in range(min(start, boundary_at), -1, -1):
            if _breakpoint_blocks(items[index]) is not None:
                return index
        return None

    wanted: list[int] = []
    tail = _eligible_at_or_below(boundary_at)
    if tail is not None:
        wanted.append(tail)
    anchor = (boundary_at // _ANCHOR_STRIDE) * _ANCHOR_STRIDE
    while anchor >= 0 and len(wanted) < _CACHE_BREAKPOINTS:
        found = _eligible_at_or_below(anchor)
        if found is None:
            break
        if found not in wanted:
            wanted.append(found)
        anchor -= _ANCHOR_STRIDE

    for index in wanted:
        blocks = _breakpoint_blocks(items[index])
        if blocks is not None:
            blocks[-1]["prompt_cache_breakpoint"] = {"mode": "explicit"}
    return len(wanted)


def _breakpoints_at(items: list[dict[str, Any]]) -> list[int]:
    out = []
    for i, item in enumerate(items):
        blocks = _breakpoint_blocks(item) or ()
        if any(isinstance(b, dict) and "prompt_cache_breakpoint" in b for b in blocks):
            out.append(i)
    return out


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def _item_fingerprint(index: int, item: dict[str, Any]) -> str:
    blob = json.dumps(item, sort_keys=True, default=str)
    return "{} {} {} {} {}".format(
        index,
        item.get("type") or item.get("role") or "?",
        item.get("id") or item.get("call_id") or "-",
        _digest(blob),
        len(blob),
    )


def _log_request_fingerprint(kwargs: dict[str, Any]) -> None:
    """Everything a prefix match can see, as one greppable line per request.

    `cache shape` says how much came back cached and what the request looked
    like from the session's side. It cannot say WHERE two requests stopped
    being the same, because the session never sees the wire items: the
    Responses translation happens below it, and `instructions` and `tools`
    are not in the message list at all.

    So this line is the wire request itself, reduced to what prefix matching
    depends on: the routing key, where the breakpoints went, and the head as
    length plus hash.

    The per-item half — every item as `ordinal type id hash length`, which is
    what names the first item that stopped matching — is DEBUG, and for a
    reason measured rather than guessed. It costs a `json.dumps` and a hash of
    every item on every call, and it emits one log line per byte of prompt: at
    222 items and 300KB of tool traffic that is a 20KB line and a visible
    event-loop stall, `_digest <- _item_fingerprint` in the backend's own
    "loop was doing" sample. An instrument that slows the runtime it measures
    is not free evidence, it is a second problem.

    So the cheap half runs always and the expensive half runs when someone is
    looking. `TESSERACT_LOG_LEVEL=DEBUG` on the backend turns it back on, and
    the diff it enables is the thing that found both cache defects.

    Hashes, never content — these logs are read by people who are not the
    operator.
    """
    try:
        instructions = kwargs.get("instructions") or ""
        tools = kwargs.get("tools") or []
        items = kwargs.get("input") or []
        logger.info(
            "cache fingerprint: key=%s bp=%s instr=%d:%s tools=%d items=%d",
            kwargs.get("prompt_cache_key") or "-",
            ",".join(str(i) for i in _breakpoints_at(items)) or "-",
            len(instructions),
            _digest(instructions),
            len(tools),
            len(items),
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "cache items: tools=%s | %s",
                _digest(json.dumps(tools, sort_keys=True, default=str)),
                " | ".join(_item_fingerprint(i, it) for i, it in enumerate(items)),
            )
    except Exception:
        # An instrument that can end a turn is worse than no instrument.
        logger.debug("cache fingerprint failed", exc_info=True)


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
        defers_tool_loading: bool = False,
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
        # Deferral is a Responses-path capability, so the claim and the path
        # are read from ONE field: `boot._build_provider_adapter` passes
        # `ref.model.fields["use_responses_api"]` here, and
        # `AdapterOptions.use_responses_api` comes from the same entry. An
        # openai-COMPATIBLE surface (NIM, xAI, Ollama) declares it false and
        # keeps filtering to the working set exactly as before, so nothing
        # there starts sending a whole registry it cannot defer.
        self.defers_tool_loading = defers_tool_loading

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
        # Projected here too. This path has no deferral, and an adapter that
        # declares it still reaches this branch when a catalog entry does not
        # set `use_responses_api` — so the payload is narrowed to the working
        # set rather than 150 schemas arriving as ordinary functions.
        projected = self.project_tools(tools)
        if projected:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {}),
                    },
                }
                for t in projected
            ]
        if opts.reasoning_effort:
            kwargs["reasoning_effort"] = opts.reasoning_effort
        if self._supports_prompt_cache_key:
            cache_key = _routing_key(system_text, messages)
            if cache_key:
                kwargs["prompt_cache_key"] = cache_key
        if self._cache_routing_header:
            route_key = _routing_key(system_text, messages)
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
        instructions, input_items, boundary_at = self._to_responses_input(messages)
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
                cache_key = _routing_key(instructions, messages)
                if cache_key:
                    kwargs["prompt_cache_key"] = cache_key
        projected = self.project_tools(tools)
        if projected:
            translated: list[dict[str, Any]] = []
            deferred: list[dict[str, Any]] = []
            for t in projected:
                if t.get("defer_loading"):
                    deferred.append(t)
                else:
                    translated.append(_function_entry(t, defer=False))
            # Deferred tools travel namespaced by taxonomy group, not as flat
            # entries wearing `defer_loading` — seeing every name up front
            # costs 26,659 tokens for 158 tools, and namespacing without
            # naming the members inside gets the model to the right tool on
            # 1 of 5 tasks. Naming them in the namespace description is what
            # gets back to today's accuracy at a third of the cost. See
            # `_namespace_entries`.
            translated.extend(_namespace_entries(deferred))
            # The provider's search rides along only when there is something
            # to search for. `project_tools` has already guaranteed that a
            # payload which would defer everything defers nothing instead.
            if deferred:
                translated.append(dict(_TOOL_SEARCH_TOOL))
            kwargs["tools"] = translated
        if opts.reasoning_effort:
            kwargs["reasoning"] = {"effort": opts.reasoning_effort}
            # Only ask for encrypted reasoning when the model will actually generate some.
            # `none` is a valid effort meaning "skip reasoning" — no blob to include.
            if opts.reasoning_effort not in ("none",):
                kwargs["include"] = ["reasoning.encrypted_content"]

        # Implicit caching puts its one breakpoint at the end of the latest
        # user or tool message, which here is the per-turn state block, so the
        # prefix it stores is one no later request reproduces. Placing the
        # breakpoint ourselves puts it back on the last permanent item, and
        # the state block falls after it, where the provider charges it as
        # uncached input and it disturbs nothing.
        #
        # Only for a model that declares it: gpt-5.4-mini answers both fields
        # with a 400, and the option is withheld when no breakpoint could be
        # placed, because explicit mode without one turns caching off.
        # `extra_body` because the SDK does not type the field yet.
        if opts.prompt_cache_explicit and _mark_breakpoints(input_items, boundary_at):
            kwargs["extra_body"] = {"prompt_cache_options": {"mode": "explicit"}}

        _log_request_fingerprint(kwargs)

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
            # Unhandled, a truncated stream emits no STOP at all and the tool
            # loop simply runs out of chunks: a turn that was cut off would
            # look exactly like a turn that finished.
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
    ) -> tuple[str, list[dict[str, Any]], int | None]:
        """Translate Chat-Completions-shaped history → (instructions, input items).

        Reasoning items (marked `_reasoning: True`) are re-hydrated as
        type="reasoning" items so encrypted_content round-trips.

        The third return value carries `CACHE_BOUNDARY` across the translation,
        for the same reason the Anthropic adapter's does: the mapping is not
        one to one in either direction. One assistant message emits a text item
        AND one item per tool call, an empty one emits nothing, and the orphan
        strip at the bottom removes items after the fact. So the marked message
        is followed by identity, and its index is read off the finished list.
        """
        boundary_src = next(
            (i for i, m in enumerate(messages) if m.get(CACHE_BOUNDARY)), -1
        )
        boundary_obj: dict[str, Any] | None = None
        instructions_parts: list[str] = []
        input_items: list[dict[str, Any]] = []
        for i, m in enumerate(messages):
            # Read at the first message past the marked one, which is the
            # earliest moment the marked one is certainly finished. When the
            # marked message emits nothing this keeps the item before it,
            # which is a shorter prefix and still a correct one.
            if i == boundary_src + 1:
                boundary_obj = input_items[-1] if input_items else None
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
                    # A block list rather than a bare string, because a bare
                    # string has nowhere to hang a cache breakpoint and a tool
                    # loop that cannot mark its own results caches none of
                    # them. `input_text` is the only text block accepted here:
                    # `output_text` is a 400 that names the alternatives.
                    "output": [{
                        "type": "input_text",
                        "text": content if isinstance(content, str) else json.dumps(content),
                    }],
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

        if boundary_src == len(messages) - 1:
            boundary_obj = input_items[-1] if input_items else None

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

        boundary_at = next(
            (i for i, it in enumerate(input_items) if it is boundary_obj), None
        )
        if boundary_at is None and boundary_obj is not None:
            # The marked item did not survive the orphan strip: it was a tool
            # result whose `function_call` had been trimmed away upstream. That
            # is a real shape — the strip exists because it once 400'd the
            # request — and it happens on the LONGEST conversations, the ones
            # that have been trimmed.
            #
            # Returning None here would be the quietest possible failure. With
            # no breakpoint the caller withholds `prompt_cache_options` and the
            # request falls back to implicit mode, which on this runtime's
            # shape puts its one breakpoint on the per-turn state block and
            # caches nothing past the system prompt — measured, six turns out
            # of six. So the last surviving markable item stands in: a shorter
            # prefix than we wanted, and an enormous one next to none.
            boundary_at = next(
                (
                    i
                    for i in range(len(input_items) - 1, -1, -1)
                    if _breakpoint_blocks(input_items[i]) is not None
                ),
                None,
            )
            logger.info(
                "cache boundary: the marked item was orphan-stripped; "
                "falling back to item %s",
                boundary_at,
            )
        return "\n\n".join(instructions_parts), input_items, boundary_at

    def _to_chat_completions_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        # Every key we add to a message is underscore-prefixed and none of
        # them is a provider field. The other three translations in this tree
        # build their output dicts from scratch, so they drop ours for free;
        # this one forwards the message as it stands whenever the content is
        # plain text, which is exactly the shape the runtime-state block and
        # the cache boundary arrive in.
        if any(k.startswith("_") for k in msg):
            msg = {k: v for k, v in msg.items() if not k.startswith("_")}
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

    @staticmethod
    def count_tokens(messages: list[dict[str, Any]]) -> int:
        chars = 0
        attachment_tokens = 0
        for msg in messages:
            if msg.get("_reasoning"):
                # encrypted_content isn't user-visible but consumes context
                chars += len(msg.get("encrypted_content", ""))
                continue
            content = msg.get("content", "")
            if isinstance(content, str):
                chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if "text" in block:
                        chars += len(block["text"])
                    elif block.get("type") in ("image", "input_image", "file", "input_file"):
                        attachment_tokens += 256
        return tokens_from_chars(chars) + attachment_tokens

    async def check_available(self) -> bool:
        try:
            await self.client.models.list()
            return True
        except Exception:
            return False
