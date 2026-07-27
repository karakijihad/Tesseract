"""Cost-metering wrapper for background LLM calls.

The chat path bills the cost ledger in ``ChatSession._record_turn_usage`` and
runs ``check_preflight`` before each paid turn. Scheduler tasks call
``adapter.generate()`` directly and never touched the ledger, so the daily
budget cap could never fire — the 2026-06-28 incident ($9 OpenAI fallback
spend showing as $0.04). ``MeteredAdapter`` restores parity: wrap each chain
entry once (see ``scheduler.role_chain.build_chain_for_role``) and every
``generate()`` / ``stream()`` records usage and preflights paid calls.

Agent (``invoke_agent``) sub-sessions don't need this — they run a real
``ChatSession`` which already meters; they just need the ledger handle.
"""

from __future__ import annotations

import logging
from typing import Any

from tesseract.brain.cost.ledger import CostLedger, CostUsage
from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ModelAdapter,
    StreamChunk,
)

log = logging.getLogger(__name__)


class MeteredAdapter(ModelAdapter):
    """Delegates to ``inner`` and bills ``ledger`` for the spend.

    ``options`` is the chain entry's resolved ``AdapterOptions`` (carrying the
    role + model + tier), used both as the default for calls that pass none and
    as the billing key. Preflight only fires for ``tier == "api"`` (paid),
    mirroring ``ChatSession.send``.
    """

    def __init__(
        self,
        inner: ModelAdapter,
        options: AdapterOptions,
        ledger: CostLedger | None,
    ) -> None:
        self._inner = inner
        self._options = options
        self._ledger = ledger

    @property
    def model(self) -> str:
        return getattr(self._inner, "model", self._options.model) or self._options.model

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        return self._inner.count_tokens(messages)

    async def check_available(self) -> bool:
        return await self._inner.check_available()

    async def generate(
        self,
        prompt: str,
        options: AdapterOptions | None = None,
    ) -> str:
        # Drive our own stream() rather than inner.generate() so usage is read
        # from the local STOP chunk — never round-tripped through shared mutable
        # adapter state, which would race when two MeteredAdapters wrap the same
        # live `app["adapter_chain"]` entry (reviewer finding, 2026-06-28).
        opts = options or self._options
        parts: list[str] = []
        async for chunk in self.stream([{"role": "user", "content": prompt}], options=opts):
            if chunk.type == ChunkType.TEXT:
                parts.append(chunk.text)
            elif chunk.type == ChunkType.ERROR:
                raise RuntimeError(f"Adapter error during generate: {chunk.error}")
        return "".join(parts)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ):
        opts = options or self._options
        self._preflight(opts)
        async for chunk in self._inner.stream(messages, tools=tools, options=opts):
            if chunk.type == ChunkType.STOP:
                usage = chunk.raw.get("usage") if isinstance(chunk.raw, dict) else None
                self._record(opts, usage)
            yield chunk

    # --- internals ----------------------------------------------------------

    def _preflight(self, opts: AdapterOptions) -> None:
        if self._ledger is None:
            return
        if self._is_free(opts.model):
            # Free models (NIM, local, CLI subscriptions — priced $0 in the
            # catalog) must never be blocked: they're the safety valve that
            # absorbs traffic when the paid primary is capped. Gating on price
            # rather than tier matters because NIM is cataloged under `api:`.
            return
        # Raises BudgetExhausted — propagated so the scheduler job fails this
        # tick (caught by the engine, retried next cadence). Background work
        # has no operator to unlock an overage, so failing closed is correct.
        self._ledger.check_preflight(opts.role or "chat_brain")

    def _is_free(self, model: str) -> bool:
        pricing = getattr(self._ledger, "pricing", None) or {}
        rates = pricing.get(model)
        return rates is not None and rates[0] == 0.0 and rates[1] == 0.0

    def _record(self, opts: AdapterOptions, usage: dict[str, Any] | None) -> None:
        if self._ledger is None or not isinstance(usage, dict) or not opts.model:
            return
        try:
            self._ledger.record(
                opts.role or "chat_brain",
                opts.model,
                CostUsage(
                    input_tokens=int(usage.get("input_tokens") or 0),
                    output_tokens=int(usage.get("output_tokens") or 0),
                    cached_tokens=int(usage.get("cached_tokens") or 0),
                    cache_creation_tokens=int(usage.get("cache_creation_tokens") or 0),
                ),
            )
        except RuntimeError:
            # Unknown/unpriced model — ledger.record raises rather than silently
            # zero-billing. Mirror chat.py's defensive swallow so a pricing-gap
            # never crashes a background job.
            log.exception("MeteredAdapter: cost ledger record failed")


def meter_chain(
    chain: list[tuple[ModelAdapter, AdapterOptions]],
    ledger: CostLedger | None,
) -> list[tuple[ModelAdapter, AdapterOptions]]:
    """Wrap each ``(adapter, options)`` entry so its spend hits ``ledger``.

    No-op (returns the chain unchanged) when ``ledger`` is None, so test/back-
    compat callers that don't thread a ledger keep the bare adapters.

    Idempotent: entries already wrapped in ``MeteredAdapter`` are passed through
    untouched, so a chain may be metered at build time AND again at the
    consumption site without double-billing.
    """
    if ledger is None:
        return chain
    return [
        (a if isinstance(a, MeteredAdapter) else MeteredAdapter(a, o, ledger), o)
        for a, o in chain
    ]
