"""The gate between the runtime and whatever model it is talking to.

Every adapter this runtime uses is built by ``brain/boot.py::build_adapter``,
and that factory wraps what it built in this class. The wrap is the mechanism:
a provider added later is a new class and a new branch in that factory's
dispatch, and it is screened on the way out without its own code taking part.
An adapter that has to remember to call something is an adapter that can
forget, and this runtime cannot audit an install it does not ship.

**It fails closed, and it is the only one of the four boundaries that does.**
A credential store that cannot be read means the payload cannot be checked, and
an unchecked payload does not leave the machine. The other three boundaries
degrade instead — losing one tool result, one memory or one log line is
recoverable, and turning a corrupt file into a runtime that cannot answer is
not. What is not recoverable is a value reaching a third party.

**It redacts every stored value, with no host check, and that is correct
rather than lazy.** No credential is ever legitimately spent through a model
adapter: a consumer that means to send one calls
``CredentialStore.resolve(id, host)``, which enforces the record's own
``allowed_hosts`` exactly. So on this path there is no such thing as a value
that belongs in the payload, and a second copy of the host rule here would be
a second answer to a question already answered.

**What it does not cover.** ``ImageGenerateTool`` posts to the genai endpoint
directly and never passes through ``build_adapter``, so it is outside this
gate. It is inside the ingress wall at ``brain/tools.py::execute_tool``, and
its payload is a prompt the model wrote, which under GOVERNANCE §1 cannot
contain a value the model was never given.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ModelAdapter,
    StreamChunk,
)


class ScreenedAdapter(ModelAdapter):
    """Delegates to ``inner`` with the outbound payload screened first."""

    def __init__(self, inner: ModelAdapter) -> None:
        self._inner = inner

    @property
    def inner(self) -> ModelAdapter:
        """What is being screened. For the chain, which unwraps to compare."""
        return self._inner

    @property
    def defers_tool_loading(self) -> bool:  # type: ignore[override]
        """A wrapper has no opinion; it answers for what it wraps."""
        return bool(getattr(self._inner, "defers_tool_loading", False))

    @property
    def model(self) -> str:
        return getattr(self._inner, "model", "")

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        return self._inner.count_tokens(messages)

    async def check_available(self) -> bool:
        return await self._inner.check_available()

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        # Imported at call time so `kernel.adapters` does not import the
        # store. The adapters are the lowest layer here and the credential
        # store sits above them; a module-level import would invert that and
        # drag DPAPI into the import graph of every process that builds one.
        from tesseract.credentials.redaction import redact_payload

        # `redact_payload` raises `RedactionUnavailable` and it is deliberately
        # not caught. The request has not been made at this point, so an
        # exception here is the request not happening — which is what failing
        # closed means. The fallback chain will try the next entry, that entry
        # is screened too, and every one of them will refuse for the same
        # reason until the store is readable again.
        screened = redact_payload(messages)
        async for chunk in self._inner.stream(screened, tools=tools, options=options):
            yield chunk

    def __repr__(self) -> str:
        return f"ScreenedAdapter({self._inner!r})"


__all__ = ["ScreenedAdapter"]
