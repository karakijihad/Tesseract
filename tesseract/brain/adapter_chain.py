"""FallbackAdapter — per-turn failover across the configured chat_brain chain.

Audit M3 fix (2026-04-29): before, `resolve_chat_brain_runtime()` resolved
the chain at startup and `app["adapter_chain"]` was stashed but never
consumed. `ChatSession` only ever talked to the primary adapter; if the
primary 5xx-ed mid-conversation, the turn errored and the operator had to
manually flip models. Hermes/OpenClaw-style runtimes failover transparently.

Operator policy (2026-05-02): the chain must distinguish *transient* from
*hard* errors:

- TRANSIENT (429, 5xx, timeout, network) → retry the same chain entry up to
  `transient_retries` times (with backoff) before advancing.
- HARD (auth, model-not-found, billing, malformed request, context overflow)
  → advance immediately, no retry.
- UNKNOWN (adapter did not classify) → treated as TRANSIENT (safe default).

Compaction is excluded from this chain — see `compact_history` for the
unwrap-to-primary path.

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
- `count_tokens` is delegated to the primary (ChatSession's compaction
  threshold uses primary's context window).
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


@dataclass
class _EntryBreaker:
    """Per-chain-entry cooldown breaker.

    Counts consecutive *advances* (chain walked past this entry — HARD
    error or transient retries exhausted). When the count crosses
    ``max_failures`` the breaker opens for ``cooldown_seconds``; while
    open, the chain skips this entry and falls through to the next.
    Cooldown elapsed → next attempt acts as a half-open probe; success
    closes, failure restarts the cooldown. Disabled by setting either
    knob ≤ 0.
    """
    max_failures: int
    cooldown_seconds: float
    failures: int = 0
    open_until: float = 0.0  # monotonic timestamp; 0 == closed

    def is_disabled(self) -> bool:
        return self.max_failures <= 0 or self.cooldown_seconds <= 0

    def is_open(self, now: float) -> bool:
        return self.open_until > 0.0 and now < self.open_until

    def remaining_cooldown(self, now: float) -> float:
        return max(0.0, self.open_until - now)

    def record_failure(self, now: float) -> None:
        if self.is_disabled():
            return
        self.failures += 1
        if self.failures >= self.max_failures:
            self.open_until = now + self.cooldown_seconds

    def record_success(self) -> None:
        self.failures = 0
        self.open_until = 0.0


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
        self._chain = chain
        self._transient_retries = transient_retries
        self._transient_backoff_ms = transient_backoff_ms
        self._cooldown_max_failures = cooldown_max_failures
        self._cooldown_seconds = cooldown_seconds
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

    def _build_breaker(self, options: AdapterOptions) -> _EntryBreaker:
        extra = getattr(options, "extra", None) or {}
        return _EntryBreaker(
            max_failures=int(
                extra.get("chain_cooldown_max_failures", self._cooldown_max_failures)
            ),
            cooldown_seconds=float(
                extra.get("chain_cooldown_seconds", self._cooldown_seconds)
            ),
        )

    def fork(self) -> "FallbackAdapter":
        """Return a fresh FallbackAdapter wrapping the SAME underlying chain.

        WP-2: synthetic workspace turns get their own FallbackAdapter
        instance so their failures don't trip the chat turn's breakers
        (and vice-versa). The inner primary/fallback adapters are stateless
        wrappers around HTTP/subprocess and are safe to share — only the
        per-instance breaker state needs to be fresh. Audit reference:
        Docs/Plan/workspace-parallel/audit.md §C.
        """
        return FallbackAdapter(
            chain=list(self._chain),
            transient_retries=self._transient_retries,
            transient_backoff_ms=self._transient_backoff_ms,
            cooldown_max_failures=self._cooldown_max_failures,
            cooldown_seconds=self._cooldown_seconds,
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
        last_pre_commit_error: str | None = None
        last_pre_commit_kind: ErrorKind = ErrorKind.UNKNOWN
        # Cumulative retries burnt across previously-advanced entries — so
        # the MODEL_SELECTED envelope on the entry that finally commits
        # discloses how many primary-side retries were spent before
        # falling back. Per-entry counter resets each iteration.
        retries_burnt_before_this_entry = 0
        primary_options = self._chain[0][1]
        skipped_in_cooldown = 0

        for idx, (adapter, entry_options) in enumerate(self._chain):
            entry_label = getattr(adapter, "model", entry_options.model) or entry_options.model

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
                if last_pre_commit_error is None:
                    last_pre_commit_error = (
                        f"entry idx={idx} ({entry_label}) in cooldown "
                        f"({remaining:.1f}s remaining)"
                    )
                    last_pre_commit_kind = ErrorKind.TRANSIENT
                skipped_in_cooldown += 1
                continue

            # Context-window guard — an entry whose window cannot even hold
            # the prompt is guaranteed a provider-side 400 (NIM/vLLM compute
            # max_tokens = window - prompt server-side; observed 2026-07-12:
            # a ~253k-token history vs gpt-oss-120b's 131072 window produced
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
                    if last_pre_commit_error is None:
                        last_pre_commit_error = (
                            f"entry idx={idx} ({entry_label}) context overflow "
                            f"(~{est_tokens} tokens >= {window}-token window)"
                        )
                        last_pre_commit_kind = ErrorKind.HARD
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
                attempt_error: str | None = None
                attempt_failed = False
                try:
                    async for chunk in adapter.stream(
                        messages=messages,
                        tools=tools,
                        options=entry_options,
                    ):
                        if chunk.type == ChunkType.ERROR and not committed:
                            attempt_error = chunk.error or "unknown"
                            attempt_kind = chunk.error_kind or ErrorKind.UNKNOWN
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
                                        "fallback_reason": last_pre_commit_error or "",
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
                                self._breakers[idx].record_failure(self._time_func())
                                provider_error = chunk.error or "unknown"
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
                        # transient (worth a retry). AU-14 14b drops a
                        # tripwire row so a persistently-empty provider
                        # surfaces to AU-5's mapper without waiting for
                        # the next probe tick.
                        attempt_failed = True
                        attempt_kind = ErrorKind.UNKNOWN
                        attempt_error = "adapter exited without output"
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
                        self._breakers[idx].record_failure(self._time_func())
                        provider_error = f"{type(exc).__name__}: {exc}"
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
                    attempt_error = f"{type(exc).__name__}: {exc}"
                    attempt_kind = _exception_kind(exc)

                if not attempt_failed:
                    # Stream ended after commit on a happy path.
                    self._breakers[idx].record_success()
                    return

                # Decide: retry same entry, or advance to next.
                last_pre_commit_error = attempt_error or "unknown"
                last_pre_commit_kind = attempt_kind

                if attempt_kind == ErrorKind.HARD:
                    logger.warning(
                        "FallbackAdapter: idx=%d (%s) HARD pre-commit error: %s — advancing",
                        idx, entry_label, last_pre_commit_error,
                    )
                    # AU-14 14b production tripwire — a HARD pre-commit
                    # error against the chat chain is the exact signal
                    # the scheduled probe would have caught. Drop a row
                    # so AU-5's mapper sees it before the next probe
                    # tick.
                    _emit_production_tripwire(
                        entry_options,
                        drift_kind=_drift_kind_for_hard(last_pre_commit_error),
                        evidence={
                            "error": last_pre_commit_error,
                            "chain_index": idx,
                        },
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
                        last_pre_commit_error,
                    )
                    await self._sleep_backoff(transient_attempts, entry_backoff_ms)
                    continue

                logger.warning(
                    "FallbackAdapter: idx=%d (%s) %s pre-commit error after %d retries: %s — advancing",
                    idx,
                    entry_label,
                    attempt_kind.value,
                    transient_attempts,
                    last_pre_commit_error,
                )
                advanced = True
                break

            if not advanced and not committed:
                # Defensive: should not be reachable, but if the inner
                # loop fell through without advancing or committing,
                # avoid silently looping back over the same entry.
                advanced = True

            # This entry advanced — record a failure on its breaker so
            # repeated advances eventually open the cooldown.
            self._breakers[idx].record_failure(self._time_func())

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
            error_text = (
                f"no chat_brain model available — "
                f"all {len(self._chain)} chain entries exhausted; "
                f"last error ({last_pre_commit_kind.value}): "
                f"{last_pre_commit_error or 'unknown'}"
            )
        yield StreamChunk(
            type=ChunkType.ERROR,
            error=error_text,
            error_kind=ErrorKind.HARD,
        )

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        return self.primary.count_tokens(messages)

    async def check_available(self) -> bool:
        for adapter, _ in self._chain:
            try:
                if await adapter.check_available():
                    return True
            except Exception:
                logger.debug("FallbackAdapter: check_available raised", exc_info=True)
        return False


# ── AU-14 14b: production tripwire emission ───────────────────────────
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


def _drift_kind_for_hard(error_text: str | None) -> str:
    """Bucket a HARD pre-commit error message into a ``DriftKind``.

    Mirrors the schema in :mod:`tesseract.scheduler.tasks._probes.base`.
    Defaults to ``http_error`` so an unrecognised message still produces
    actionable telemetry.

    **Tie-break order is deliberate.** ``unavailable`` (auth / not-found
    / invalid-key) wins over ``shape_mismatch`` because a compound
    message like "Authentication failed: schema validation rejected
    request" is operator-actionable as a credential problem first —
    AU-5's mapper can suggest a credential rotation, but cannot draft a
    schema fix for a request that never got past auth. ``schema_error``
    (context-window) wins over ``http_error`` because the surface fix
    is a config edit, not a retry.
    """
    if not error_text:
        return "http_error"
    lowered = error_text.lower()
    if "auth" in lowered or "permission" in lowered or "not found" in lowered or "invalid api key" in lowered:
        return "unavailable"
    if "shape" in lowered or "schema" in lowered or "json" in lowered:
        return "shape_mismatch"
    if "context" in lowered and "window" in lowered:
        return "schema_error"
    return "http_error"
