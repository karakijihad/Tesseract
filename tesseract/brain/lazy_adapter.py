"""A chain entry that has not been built yet.

A role's chain names a primary and its fallbacks, and building one costs a
real client: 1.64 s for `genai.Client`, 0.57 s for `AsyncOpenAI`, each of them
reading the system trust store to construct an SSL context. Building all three
up front pays for two that will almost never run — measured at 2.2–3.7 s per
scheduled job fire, on the event loop, every fire.

So an entry is a `ModelAdapter` that constructs the real one the first time
something actually streams or generates through it. It is deliberately NOT a
new type: seventeen scheduler tasks, the librarian's distillation pass,
compaction's unwrap-to-primary path, `meter_chain` and fifty-five tests all
take a chain as `(ModelAdapter, AdapterOptions)` pairs and reach straight for
element 0. Making the entry a different kind of object would mean editing
every one of them; making it an adapter means all of them get this for free.

Two things are answered WITHOUT building, because answering them by building
would defeat the whole thing:

- `count_tokens` — the chain's context-window guard asks every entry it
  considers, including ones it is about to skip. Answered off the provider's
  class, where the estimate is a `staticmethod`.
- `check_available` — answered only once built. Nothing in production calls
  it, and three of the four provider implementations need a live SDK client,
  so a truthful "not yet" beats constructing the client to find out.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncGenerator

from tesseract.config.loader import ResolvedRef
from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter, StreamChunk

log = logging.getLogger(__name__)


class LazyAdapter(ModelAdapter):
    """One chain entry, constructed on first use and kept for the process."""

    def __init__(self, ref: ResolvedRef) -> None:
        self._ref = ref
        self._adapter: ModelAdapter | None = None
        self._lock = asyncio.Lock()

    @property
    def ref(self) -> ResolvedRef:
        return self._ref

    @property
    def built(self) -> bool:
        return self._adapter is not None

    @property
    def model(self) -> str:
        """The catalog's model name, so a log line never forces a build."""
        return self._ref.model.model

    async def get(self) -> ModelAdapter:
        """The real adapter, built off-thread the first time it is wanted.

        Off-thread because constructing an SDK client reads the system trust
        store to build an SSL context — 107 ms of blocked loop measured, on
        top of the client's own cost. The app never freezes; the turn that
        needed this entry waits once, and every turn after it is free.

        The lock is what makes "once" true: two concurrent turns failing over
        to the same entry would otherwise both build one, and the second would
        replace a client the first is already streaming through.
        """
        if self._adapter is not None:
            return self._adapter
        async with self._lock:
            if self._adapter is not None:
                return self._adapter
            from tesseract.brain.boot import build_adapter

            self._adapter = await asyncio.to_thread(build_adapter, self._ref)
            log.info("chain entry %s built on first use", self._ref.ref)
            return self._adapter

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        adapter = await self.get()
        async for chunk in adapter.stream(messages=messages, tools=tools, options=options):
            yield chunk

    async def generate(
        self,
        prompt: str,
        options: AdapterOptions | None = None,
    ) -> str:
        adapter = await self.get()
        return await adapter.generate(prompt, options)

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Estimate off the class, so an unbuilt entry can still be measured."""
        if self._adapter is not None:
            return self._adapter.count_tokens(messages)
        from tesseract.brain.boot import adapter_class_for

        return adapter_class_for(self._ref).count_tokens(messages)

    async def check_available(self) -> bool:
        """False until built — see the module docstring."""
        if self._adapter is None:
            return False
        return await self._adapter.check_available()

    def __repr__(self) -> str:
        state = "built" if self.built else "unbuilt"
        return f"<LazyAdapter {self._ref.ref} {state}>"
