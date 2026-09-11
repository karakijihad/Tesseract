"""FallbackAdapter — per-turn failover across the configured chat_brain chain.

Audit M3 fix (2026-04-29): before, `resolve_chat_brain_runtime()` resolved
the chain at startup and `app["adapter_chain"]` was stashed but never
consumed. `ChatSession` only ever talked to the primary adapter; if the
primary 5xx-ed mid-conversation, the turn errored and the operator had to
manually flip models. A chain that is declared and not consumed is not a
fallback; failing over has to be transparent to the turn or it is not one.

Operator policy (2026-05-02): the chain must distinguish *transient* from
*hard* errors:

- TRANSIENT (429, 5xx, timeout, network) → retry the same chain entry up to
  `transient_retries` times (with backoff) before advancing.
- HARD (auth, model-not-found, billing, malformed request, context overflow)
  → advance immediately, no retry.
- UNKNOWN (adapter did not classify) → treated as TRANSIENT (safe default).

A boundary is not a model call this chain answers: `session_ops` runs the
reflection as a turn of its own, so nothing here needs an unwrap-to-primary
path any more. The summariser that did is gone.

Contract:

- The chain is a list of `(ModelAdapter, AdapterOptions)` pairs in
  preference order. The first entry is the primary.
- On each call to `stream()`, the FallbackAdapter tries the primary first.
  Within a single chain entry, transient pre-commit errors trigger up to
  `transient_retries` retries against the same adapter before advancing.
  Hard pre-commit errors advance immediately.
- Once an adapter has *committed* to the turn (yielded a TEXT or
  TOOL_CALL_* chunk), failover and retries are no longer safe — partial
  output cannot be rewound. A late failure surfaces as a normal ERROR
  chunk.
- Each entry brings its own AdapterOptions. The `options=` kwarg from the
  caller is intentionally ignored: model name / temperature / context window
  vary per adapter and are baked into the chain.
- `count_tokens` is delegated to the primary (ChatSession's boundary
  threshold uses primary's context window). The CHARACTER ceiling is not the
  chain's to answer: `boot._apply_chain_ceiling` gives every member the
  tightest declared `max_prompt_chars` before a session is built, so the one
  number `ChatSession` carries is already safe for whichever entry answers.
- `check_available` returns True if any entry is reachable.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Callable

from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ErrorKind,
    ModelAdapter,
    StreamChunk,
)
from tesseract.orchestrator import provider_failure
from tesseract.orchestrator.provider_failure import (
    NEEDS_A_PERSON,
    SELF_CLEARING,
    ProviderFault,
    window_class_for_fault,
)

logger = logging.getLogger(__name__)


# Provider-side request IDs surface inside error messages (OpenAI: `req_<hex>`,
# Anthropic: `req_01<...>`). Extracted into the post-commit ERROR envelope so
# operators can correlate Mirror notes with provider-side incidents.
_REQUEST_ID_RE = re.compile(r"req_[0-9a-zA-Z]{20,}")


def _extract_request_id(text: str) -> str | None:
    if not text:
        return None
    match = _REQUEST_ID_RE.search(text)
    return match.group(0) if match else None


#: How long an entry stays skipped after it opened its breaker, by the kind of
#: failure that opened it. Two classes, because one number cannot be right for
#: both: a dropped socket may have cleared by the time you ask again, and an
#: account with no credits in it has not and will not until a person adds some.
#:
#: **This is NOT the retry partition, and the difference is the design.**
#: `errors.py::_NEVER_RETRY` answers *is this call worth making again now*, and
#: `schema` belongs in it because the same malformed request stays malformed.
#: But `schema` is a property of the REQUEST, not of this entry: the next turn
#: sends a different one. Shutting a working model out of the chain for an
#: hour because one prompt overflowed its window would be a second outage
#: caused by the thing that watches for outages.
#:
#: Total over `provider_failure.FaultKind`; `window_class_for_fault` raises on
#: a kind nobody placed, for the reason AR-17 gives: a default is how a new
#: kind goes quiet.


# `eq=False` so identity is identity: a breaker is a live, mutable door and
# two of them holding the same ref shut are two doors, not one value. It is
# also what makes it hashable, which is what the weak registry needs.
@dataclass(eq=False)
class _EntryBreaker:
    """Per-chain-entry cooldown breaker.

    Counts consecutive *advances* (chain walked past this entry — HARD
    error or transient retries exhausted). When the count crosses
    ``max_failures`` the breaker opens; while open, the chain skips this
    entry and falls through to the next. The window elapsed → next attempt
    acts as a half-open probe; success closes, failure restarts the wait on
    whatever kind failed THAT time.

    Disabled by ``max_failures`` ≤ 0, which is about the BREAKER. A window of
    ≤ 0 is about one class of failure and leaves the breaker shut for that
    class alone, still counting the failure — the two are separate questions
    and answering them in one predicate made failures stop counting when both
    windows were off and keep counting when only one was.

    Which window a failure takes is `_WINDOW_CLASS` above, keyed on the kind
    the shared vocabulary read out of the provider's own words.
    """
    max_failures: int
    cooldown_seconds: float
    cooldown_seconds_until_fixed: float
    failures: int = 0
    open_until: float = 0.0  # monotonic timestamp; 0 == closed
    #: The entry's ref, which is what a chain entry is called everywhere else
    #: in this runtime and therefore what the operator is offered when they
    #: ask what is refusing. Empty for a chain built without one, which is
    #: only ever a test: an anonymous door is not one anybody can open.
    ref: str = ""
    #: The same clock its `FallbackAdapter` reads. A breaker asked from
    #: outside the turn has no `now` handed to it and must not invent one from
    #: a different clock than the one `open_until` was written against.
    time_func: Callable[[], float] = time.monotonic
    #: What it is holding the door shut FOR, in the shared vocabulary. This is
    #: the difference between "wait 3521 seconds" and "the account has no
    #: credits in it", and it is the whole reason the window got long.
    last_kind: str = ""

    def is_disabled(self) -> bool:
        return self.max_failures <= 0

    def window_for(self, kind: str) -> float:
        if window_class_for_fault(kind) == NEEDS_A_PERSON:
            return self.cooldown_seconds_until_fixed
        return self.cooldown_seconds

    def is_open(self, now: float) -> bool:
        return self.open_until > 0.0 and now < self.open_until

    def remaining_cooldown(self, now: float) -> float:
        return max(0.0, self.open_until - now)

    def record_failure(self, now: float, kind: str) -> None:
        if self.is_disabled():
            return
        self.failures += 1
        if self.failures < self.max_failures:
            return
        self.last_kind = kind
        window = self.window_for(kind)
        if window <= 0:
            # The operator turned this window off. Counting the failure still
            # matters --- it is what a later kind's window is measured from ---
            # but opening for zero seconds would report an outage that ended
            # before it was read.
            return
        self.open_until = now + window

    def record_success(self) -> None:
        # Outright, not "let the remaining window run". A person adding credits
        # shows up here as the next probe succeeding, and the hour it was shut
        # for was a guess about how long they would take.
        self.failures = 0
        self.open_until = 0.0
        self.last_kind = ""

    # -- what `context/circuit_breaker.py::Closeable` asks ------------------
    #
    # A chain entry's cooldown is a door held shut, and the operator asking
    # "stop waiting and try it now" is asking one question. Answering it here
    # is what lets `breaker_status` and `breaker_reset` cover both kinds of
    # breaker instead of the runtime growing a second tool for the second
    # class.

    def is_open_now(self) -> bool:
        return self.is_open(self.time_func())

    def remaining_now(self) -> float:
        return self.remaining_cooldown(self.time_func())

    def says(self) -> str:
        if not self.last_kind:
            return "this chain entry has not failed in this process"
        if window_class_for_fault(self.last_kind) == NEEDS_A_PERSON:
            return f"{self.last_kind}, which does not clear on its own"
        return self.last_kind

    def close(self) -> None:
        self.record_success()


# "Committed" = caller has received content that cannot be rewound.
# MODEL_SELECTED is emitted by ChatSession itself before adapter.stream(),
# never by the adapter, so including it here is inert today — but if a
# future adapter ever yields it, committing on it would block legitimate
# pre-output failover. REASONING_ITEM (Responses API) fires after the
# reasoning phase but before any TEXT, so committing on it would also
# block failover in a "reasoning-then-5xx" pattern. Both excluded.
_COMMITTED_CHUNK_TYPES = frozenset({
    ChunkType.TEXT,
    ChunkType.TOOL_CALL_START,
    ChunkType.TOOL_CALL_DELTA,
    ChunkType.TOOL_CALL_END,
    ChunkType.STOP,
})


def _retry_kind(fault: ProviderFault) -> ErrorKind:
    """Whether a fault of this kind is worth trying again.

    Used only where the adapter gave no `error_kind` of its own — the adapter
    has the status code, which is better evidence about the transport than a
    sentence is, so it wins where it exists. Where it does not, the words are
    all there is and they are enough: a spent account is `usage` and `usage`
    is never worth a retry, which is the whole of the 2026-08-29 defect.
    """
    from tesseract.kernel.adapters.errors import retry_kind_for_fault

    return retry_kind_for_fault(fault.kind)


def _exception_kind(exc: BaseException) -> ErrorKind:
    """Local re-classification of a raised exception. Identical to the
    helper in ``tesseract.kernel.adapters.errors`` — duplicated as a
    light import-time shim so the chain doesn't pull adapter internals.
    """
    from tesseract.kernel.adapters.errors import classify_exception

    return classify_exception(exc)


class FallbackAdapter(ModelAdapter):
    def __init__(
        self,
        chain: list[tuple[ModelAdapter, AdapterOptions]],
        *,
        transient_retries: int,
        transient_backoff_ms: int,
        cooldown_max_failures: int = 0,
        cooldown_seconds: float = 0.0,
        cooldown_seconds_until_fixed: float = 0.0,
        time_func: Callable[[], float] = time.monotonic,
    ) -> None:
        if not chain:
            raise ValueError("FallbackAdapter requires a non-empty chain")
        if transient_retries < 0:
            raise ValueError("transient_retries must be >= 0")
        if transient_backoff_ms < 0:
            raise ValueError("transient_backoff_ms must be >= 0")
        if cooldown_max_failures < 0:
            raise ValueError("cooldown_max_failures must be >= 0")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be >= 0")
        if cooldown_seconds_until_fixed < 0:
            raise ValueError("cooldown_seconds_until_fixed must be >= 0")
        self._chain = chain
        self._transient_retries = transient_retries
        self._transient_backoff_ms = transient_backoff_ms
        self._cooldown_max_failures = cooldown_max_failures
        self._cooldown_seconds = cooldown_seconds
        self._cooldown_seconds_until_fixed = cooldown_seconds_until_fixed
        self._time_func = time_func
        # Tracks which entry's options were used by the most recent stream().
        # Cost ledger reads this so failover spend is billed to the actual
        # model that produced the STOP chunk, not the primary's name (W3
        # reviewer follow-up, 2026-04-29).
        self._last_used_options: AdapterOptions = chain[0][1]
        # Per-entry cooldown breakers. Built once per FallbackAdapter
        # instance — state lives for the life of the session, which is
        # the natural scope of "this provider has been failing recently".
        self._breakers: list[_EntryBreaker] = [
            self._build_breaker(opts) for _, opts in chain
        ]

    @staticmethod
    def _ref_of(options: AdapterOptions) -> str:
        """The entry's ref, the way every other producer writes it.

        Same shape as `_emit_production_tripwire`'s, deliberately: a name the
        operator sees in `breaker_status` and a name they see in the drift log
        have to be the same name or they are two facts about two things.
        """
        # `getattr`, not attribute access. This runs in the CONSTRUCTOR, for
        # every entry, so anything it assumes about the options object is
        # assumed about every chain ever built. A caller handing in something
        # that is not a full `AdapterOptions` used to get a chain; it now got
        # an `AttributeError` from a naming helper, which is a hard failure
        # introduced by a label. An entry that cannot be named simply is not
        # named: it gets no ref, so it registers no breaker and the chain is
        # otherwise exactly as it was.
        provider = str(getattr(options, "provider", "") or "").strip()
        model = str(getattr(options, "model", "") or "").strip()
        if not provider or not model:
            return ""
        tier = str(getattr(options, "tier", "") or "api").strip()
        # Through the catalog, not by joining what is to hand. `options.model`
        # is the id the PROVIDER answers to (`gpt-5.6-terra`); the ref every
        # other reader uses is the catalog KEY (`gpt56_terra`). Joining gave
        # this breaker a name of its own, and the cost was not untidiness: the
        # refusal sentence the tool funnel shows an operator names a ref and
        # tells them to type `breaker_reset <ref>`, and for codex that reset
        # reached the funnel's breaker and not this one, because this one was
        # registered under a name nothing else in the runtime ever writes.
        # `catalog_ref` falls back to the id when no entry claims it, so an
        # entry outside the catalog is named exactly as it was before.
        from tesseract.kernel.tools.dependency import catalog_ref

        return catalog_ref(tier, provider, model)

    def _build_breaker(self, options: AdapterOptions) -> _EntryBreaker:
        extra = getattr(options, "extra", None) or {}
        breaker = _EntryBreaker(
            max_failures=int(
                extra.get("chain_cooldown_max_failures", self._cooldown_max_failures)
            ),
            cooldown_seconds=float(
                extra.get("chain_cooldown_seconds", self._cooldown_seconds)
            ),
            # Deliberately NOT overridable per provider. How long an outage
            # lasts is a property of the provider and worth tuning per entry;
            # how long a spent balance lasts is a property of the operator's
            # afternoon and is not.
            cooldown_seconds_until_fixed=self._cooldown_seconds_until_fixed,
            ref=self._ref_of(options),
            time_func=self._time_func,
        )
        if breaker.ref:
            from tesseract.context.circuit_breaker import register_foreign_breaker

            register_foreign_breaker(breaker.ref, breaker)
        return breaker

    def fork(self) -> "FallbackAdapter":
        """Return a fresh FallbackAdapter wrapping the SAME underlying chain.

        Synthetic workspace turns get their own FallbackAdapter instance so
        their failures don't trip the chat turn's breakers (and vice-versa).
        The inner primary/fallback adapters are stateless wrappers around
        HTTP/subprocess and are safe to share — only the per-instance breaker
        state needs to be fresh.
        """
        return FallbackAdapter(
            chain=list(self._chain),
            transient_retries=self._transient_retries,
            transient_backoff_ms=self._transient_backoff_ms,
            cooldown_max_failures=self._cooldown_max_failures,
            cooldown_seconds=self._cooldown_seconds,
            cooldown_seconds_until_fixed=self._cooldown_seconds_until_fixed,
            time_func=self._time_func,
        )

    @property
    def primary(self) -> ModelAdapter:
        return self._chain[0][0]

    @property
    def primary_options(self) -> AdapterOptions:
        return self._chain[0][1]

    @property
    def chain_length(self) -> int:
        return len(self._chain)

    @property
    def last_used_options(self) -> AdapterOptions:
        return self._last_used_options

    @property
    def transient_retries(self) -> int:
        return self._transient_retries

    async def _sleep_backoff(self, retry_num: int, base_ms: int | None = None) -> None:
        # retry_num is 1-indexed for the *first retry*; double each step.
        # `base_ms` lets the caller pass a per-entry override; when None,
        # the constructor-level global is used.
        ms = self._transient_backoff_ms if base_ms is None else base_ms
        if ms <= 0:
            return
        wait_ms = ms * (2 ** (retry_num - 1))
        await asyncio.sleep(wait_ms / 1000.0)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        # A fault, not a string. The chain is the last reader that made its own
        # sense of a provider's sentence; it asks the shared vocabulary now, so
        # its headline, its telemetry and the text the operator finally sees
        # all carry the same word and the provider's own wording beneath it.
        last_pre_commit_fault: ProviderFault | None = None
        # Cumulative retries burnt across previously-advanced entries — so
        # the MODEL_SELECTED envelope on the entry that finally commits
        # discloses how many primary-side retries were spent before
        # falling back. Per-entry counter resets each iteration.
        retries_burnt_before_this_entry = 0
        primary_options = self._chain[0][1]
        skipped_in_cooldown = 0

        for idx, (adapter, entry_options) in enumerate(self._chain):
            # From the OPTIONS, never the adapter. The catalog and the adapter
            # carry the same model name, but an entry may not be constructed
            # yet — reading an attribute off it to write a log line would
            # build a client for every entry the chain merely considered.
            entry_label = entry_options.model or f"idx={idx}"

            # Cooldown breaker — if this entry has been failing
            # consecutively, the breaker is open and we skip until the
            # cooldown window expires. The chain falls through to the
            # next entry; the operator gets a fast switch instead of
            # burning another retry budget against a wedged provider.
            breaker = self._breakers[idx]
            now = self._time_func()
            if breaker.is_open(now):
                remaining = breaker.remaining_cooldown(now)
                logger.warning(
                    "FallbackAdapter: idx=%d (%s) breaker OPEN — "
                    "skipping for %.1fs more (failures=%d)",
                    idx, entry_label, remaining, breaker.failures,
                )
                if last_pre_commit_fault is None:
                    # `ours`: no request was made, so nothing about the
                    # provider is known and claiming otherwise would be a
                    # guess. This is the distinction `origin` exists for.
                    last_pre_commit_fault = provider_failure.ours(
                        f"entry idx={idx} ({entry_label}) in cooldown "
                        f"({remaining:.1f}s remaining)"
                    )
                skipped_in_cooldown += 1
                continue

            # Context-window guard — an entry whose window cannot even hold
            # the prompt is guaranteed a provider-side 400 (NIM/vLLM compute
            # max_tokens = window - prompt server-side; observed 2026-07-12:
            # a ~253k-token history vs a 131072-token window produced
            # max_tokens=-122002). Skip up front: no request, no retry burn,
            # and no breaker failure — the provider isn't at fault. The
            # estimate is the adapter's own heuristic (undercounts), so only
            # outright overflow skips; borderline cases still surface as
            # provider HARD errors and advance through the normal path.
            window = entry_options.context_window
            if window and window > 0:
                try:
                    est_tokens = adapter.count_tokens(messages)
                except Exception:  # noqa: BLE001 — estimator failure must not block the entry
                    est_tokens = 0
                if est_tokens >= window:
                    logger.warning(
                        "FallbackAdapter: idx=%d (%s) prompt ~%d tokens >= "
                        "context_window %d — skipping",
                        idx, entry_label, est_tokens, window,
                    )
                    if last_pre_commit_fault is None:
                        # `ours` for the same reason: we declined to ask.
                        last_pre_commit_fault = provider_failure.ours(
                            f"entry idx={idx} ({entry_label}) context overflow "
                            f"(~{est_tokens} tokens >= {window}-token window)"
                        )
                    continue

            self._last_used_options = entry_options

            # Per-entry override of the chain-level retry policy. Set in
            # `boot.adapter_options_from_chat_brain` from
            # `providers.yaml::<tier>.<provider>.transient_retries` /
            # `transient_backoff_ms`. Absent → inherit the global default
            # passed into the constructor. Type coercion happens in
            # `loader._build_connection`; values reaching here are already int.
            entry_extra = entry_options.extra or {}
            entry_retries = entry_extra.get(
                "chain_transient_retries", self._transient_retries,
            )
            entry_backoff_ms = entry_extra.get(
                "chain_transient_backoff_ms", self._transient_backoff_ms,
            )

            # Try this entry once, then retry on TRANSIENT/UNKNOWN up to
            # `entry_retries` times. HARD errors break the inner loop
            # immediately and advance to the next entry.
            committed = False
            transient_attempts = 0
            advanced = False  # set True when this entry should yield to next
            while True:
                pre_commit_buffer: list[StreamChunk] = []
                attempt_kind: ErrorKind = ErrorKind.UNKNOWN
                attempt_fault: ProviderFault | None = None
                attempt_failed = False
                try:
                    async for chunk in adapter.stream(
                        messages=messages,
                        tools=tools,
                        options=entry_options,
                    ):
                        if chunk.type == ChunkType.ERROR and not committed:
                            # The adapter reached the provider, so this is
                            # THEIRS and its own words are the record. Where
                            # the adapter did not classify, the words decide:
                            # that is the path a spent account took sixty
                            # times as `UNKNOWN`, which the chain retries.
                            attempt_fault = provider_failure.theirs(
                                chunk.error or "the provider failed without saying why"
                            )
                            attempt_kind = chunk.error_kind or _retry_kind(attempt_fault)
                            attempt_failed = True
                            break
                        if chunk.type in _COMMITTED_CHUNK_TYPES:
                            if not committed and pre_commit_buffer:
                                for buffered in pre_commit_buffer:
                                    yield buffered
                                pre_commit_buffer.clear()
                            if not committed and (idx > 0 or transient_attempts > 0):
                                # First committed chunk after either a
                                # fallback advance or a retry-recovery —
                                # disclose the actual responder + how many
                                # retries the primary burnt before this won.
                                yield StreamChunk(
                                    type=ChunkType.MODEL_SELECTED,
                                    raw={
                                        "role": entry_options.role or "chat_brain",
                                        "provider": entry_options.provider or "",
                                        "model": entry_options.model or "",
                                        "tier": entry_options.tier or "api",
                                        "reasoning_effort": entry_options.reasoning_effort or "",
                                        "is_fallback": idx > 0,
                                        "chain_index": idx,
                                        "primary": {
                                            "provider": primary_options.provider or "",
                                            "model": primary_options.model or "",
                                            "reasoning_effort": primary_options.reasoning_effort or "",
                                        },
                                        "fallback_reason": (
                                            last_pre_commit_fault.line
                                            if last_pre_commit_fault
                                            else ""
                                        ),
                                        "transient_retries_exhausted": (
                                            retries_burnt_before_this_entry
                                            if idx > 0
                                            else transient_attempts
                                        ),
                                    },
                                )
                            committed = True
                            yield chunk
                        elif committed:
                            if chunk.type == ChunkType.ERROR:
                                # Post-commit ERROR (e.g. OpenAI Responses
                                # `response.failed` after one TEXT delta).
                                # Partial output already flowed to the
                                # caller — we cannot rewind and re-stream
                                # from a fresh entry safely. Symmetric with
                                # the post-commit raised-exception path
                                # below: log a breaker failure, surface a
                                # tagged ERROR (TRANSIENT so chat.py knows
                                # it is retry-eligible on the next turn),
                                # and exit. ChatSession.send() converts
                                # that into a synthetic system message + a
                                # bounded retry loop (Layer 2, 2026-05-05).
                                provider_error = chunk.error or "unknown"
                                self._breakers[idx].record_failure(
                                    self._time_func(),
                                    provider_failure.classify(provider_error),
                                )
                                msg = (
                                    f"adapter idx={idx} ({entry_label}) ERROR "
                                    f"chunk after commit: {provider_error}"
                                )
                                logger.warning("FallbackAdapter: %s", msg)
                                yield StreamChunk(
                                    type=ChunkType.ERROR,
                                    error=msg,
                                    error_kind=chunk.error_kind or ErrorKind.TRANSIENT,
                                    raw={
                                        "severity": "soft",
                                        "kind": "post_commit_partial",
                                        "model": entry_label,
                                        "chain_index": idx,
                                        "provider_error": provider_error,
                                        "request_id": _extract_request_id(provider_error),
                                    },
                                )
                                return
                            yield chunk
                        else:
                            pre_commit_buffer.append(chunk)
                    else:
                        # Generator exhausted cleanly.
                        if committed:
                            self._breakers[idx].record_success()
                            return
                        # An adapter that exits without committing and
                        # without an ERROR chunk is unusual — treat as
                        # transient (worth a retry). A tripwire row is
                        # dropped so a persistently-empty provider surfaces
                        # to the failure mapper without waiting for the
                        # next probe tick.
                        attempt_failed = True
                        attempt_kind = ErrorKind.UNKNOWN
                        attempt_fault = ProviderFault(
                            origin="theirs",
                            kind="no_answer",
                            detail="the adapter exited without output",
                        )
                        _emit_production_tripwire(
                            entry_options,
                            drift_kind="empty_output",
                            evidence={"chain_index": idx},
                        )
                except Exception as exc:
                    if committed:
                        # Mid-stream failure after commit — partial output
                        # was already delivered. Count as a breaker
                        # failure so a flaky provider that crashes
                        # mid-turn eventually trips into cooldown.
                        provider_error = f"{type(exc).__name__}: {exc}"
                        self._breakers[idx].record_failure(
                            self._time_func(),
                            provider_failure.from_exception(exc).kind,
                        )
                        msg = (
                            f"adapter idx={idx} ({entry_label}) raised mid-stream "
                            f"after commit: {provider_error}"
                        )
                        logger.warning("FallbackAdapter: %s", msg)
                        yield StreamChunk(
                            type=ChunkType.ERROR,
                            error=msg,
                            error_kind=ErrorKind.TRANSIENT,
                            raw={
                                "severity": "soft",
                                "kind": "post_commit_exception",
                                "model": entry_label,
                                "chain_index": idx,
                                "provider_error": provider_error,
                                "request_id": _extract_request_id(provider_error),
                            },
                        )
                        return
                    attempt_failed = True
                    attempt_fault = provider_failure.from_exception(exc)
                    attempt_kind = _exception_kind(exc)

                if not attempt_failed:
                    # Stream ended after commit on a happy path.
                    self._breakers[idx].record_success()
                    return

                # Decide: retry same entry, or advance to next.
                last_pre_commit_fault = attempt_fault or ProviderFault(
                    origin="theirs", kind="unknown", detail="the provider failed"
                )

                if attempt_kind == ErrorKind.HARD:
                    logger.warning(
                        "FallbackAdapter: idx=%d (%s) HARD pre-commit error (%s): %s — advancing",
                        idx, entry_label, last_pre_commit_fault.kind,
                        last_pre_commit_fault.detail,
                    )
                    # A HARD pre-commit error against the chat chain is
                    # the exact signal the scheduled probe would have
                    # caught. Drop a row so the failure mapper sees it
                    # before the next probe tick.
                    #
                    # `evidence` is the same three fields every probe writes,
                    # so a reader of `runtime/logs/provider-health/` never has
                    # to learn which producer wrote a row before knowing
                    # whether the fault was ours and what the provider said.
                    from tesseract.orchestrator.provider_health import (
                        drift_kind_for_fault,
                    )

                    _emit_production_tripwire(
                        entry_options,
                        drift_kind=drift_kind_for_fault(last_pre_commit_fault.kind),
                        evidence=provider_failure.evidence(
                            last_pre_commit_fault, chain_index=idx
                        ),
                    )
                    advanced = True
                    break

                # TRANSIENT or UNKNOWN: retry within budget.
                if transient_attempts < entry_retries:
                    transient_attempts += 1
                    logger.warning(
                        "FallbackAdapter: idx=%d (%s) %s pre-commit error (retry %d/%d): %s",
                        idx,
                        entry_label,
                        attempt_kind.value,
                        transient_attempts,
                        entry_retries,
                        last_pre_commit_fault.detail,
                    )
                    await self._sleep_backoff(transient_attempts, entry_backoff_ms)
                    continue

                logger.warning(
                    "FallbackAdapter: idx=%d (%s) %s pre-commit error after %d retries: %s — advancing",
                    idx,
                    entry_label,
                    attempt_kind.value,
                    transient_attempts,
                    last_pre_commit_fault.detail,
                )
                advanced = True
                break

            if not advanced and not committed:
                # Defensive: should not be reachable, but if the inner
                # loop fell through without advancing or committing,
                # avoid silently looping back over the same entry.
                advanced = True

            # This entry advanced — record a failure on its breaker so
            # repeated advances eventually open the cooldown, on the window
            # its own fault names. `attempt_fault` and not
            # `last_pre_commit_fault`: the latter can still be carrying an
            # EARLIER entry's fault when the defensive fall-through above
            # fires, and shutting this entry for an hour over the previous
            # one's spent account is a second outage invented by the guard.
            self._breakers[idx].record_failure(
                self._time_func(),
                attempt_fault.kind if attempt_fault else "unknown",
            )

            # Carry this entry's retry burn forward so the entry that
            # finally commits can disclose the total cost of advancing.
            retries_burnt_before_this_entry += transient_attempts

        # All chain entries exhausted without success.
        all_in_cooldown = skipped_in_cooldown == len(self._chain)
        if all_in_cooldown:
            error_text = (
                f"no chat_brain model available — all {len(self._chain)} "
                f"chain entries cooling down; retry after cooldown expires"
            )
        else:
            # The kind, then the provider's own sentence. Both, because the
            # kind is what a reader routes on and the sentence is what tells
            # a person what to do: `usage` says which shape of failure this
            # was, `add credits to continue` says how it ends.
            error_text = (
                f"no chat_brain model available — "
                f"all {len(self._chain)} chain entries exhausted; "
                f"last error ({last_pre_commit_fault.kind if last_pre_commit_fault else 'unknown'}): "
                f"{last_pre_commit_fault.line if last_pre_commit_fault else 'unknown'}"
            )
        yield StreamChunk(
            type=ChunkType.ERROR,
            error=error_text,
            error_kind=ErrorKind.HARD,
        )

    # No `defers_tool_loading` here, deliberately. A chain does not have one
    # answer: `roles.yaml::chain_2` is two OpenAI entries that defer and one
    # xAI entry that cannot. It used to answer `all()`, so one fallback
    # switched the feature off for the primary and it never ran on the machine
    # it was written for. Each member projects the runtime's one classification
    # for itself now, inside its own `stream`, so the question stops being the
    # chain's to answer.

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        return self.primary.count_tokens(messages)

    async def check_available(self) -> bool:
        """True if any entry that has been BUILT is reachable.

        Narrowed deliberately: three of the four provider adapters answer this
        by calling their SDK client, so asking an unbuilt entry would
        construct one — the whole chain, on a question nobody in production
        asks. `MeteredAdapter` delegates here and nothing else calls it, so
        this narrows a contract with no live consumer rather than changing a
        behaviour someone sees. An unbuilt entry reports itself unavailable,
        which is honest: it has not been reached yet.
        """
        for adapter, _ in self._chain:
            try:
                if await adapter.check_available():
                    return True
            except Exception:
                logger.debug("FallbackAdapter: check_available raised", exc_info=True)
        return False


# ── Production tripwire emission ──────────────────────────────────────
#
# These helpers live at module bottom so the FallbackAdapter body stays
# readable. The tripwire never fails the call — every code path is
# wrapped in a broad except, and missing role/model on the options
# silently skips the write rather than emitting a junk row.


def _emit_production_tripwire(
    options: AdapterOptions,
    *,
    drift_kind: str,
    evidence: dict[str, Any],
) -> None:
    """Write a ``production_tripwire`` row to ``provider-health``.

    Role + ref come from ``options.role`` / ``options.{tier,provider,model}``.
    An options block missing either of those skips the write — anonymous
    rows would pollute the JSONL keyspace.
    """
    role = (options.role or "").strip()
    tier = (options.tier or "").strip()
    provider = (options.provider or "").strip()
    model = (options.model or "").strip()
    if not role or not provider or not model:
        return
    ref = f"{tier or 'api'}.{provider}.{model}"
    try:
        from tesseract.orchestrator.provider_health import note_production_tripwire
        note_production_tripwire(role, ref, drift_kind, evidence)
    except Exception:  # noqa: BLE001
        logger.debug("FallbackAdapter: tripwire write failed", exc_info=True)
