"""Embedding-role probe — vector-dim sanity check.

Uses the existing ``EmbeddingClient`` (Ollama adapter) to embed a known
string and validates the returned vector has the expected ``dimensions``
from ``providers.yaml::<tier>.<provider>.models.<id>.dimensions``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, ClassVar

from tesseract.scheduler.tasks._probes.base import ProbeResult

log = logging.getLogger(__name__)

_KNOWN_GOOD_TEXT = "Tesseract probe — embedding vector dimension check."


class EmbeddingRoleProbe:
    role_kind: ClassVar[str] = "embedding"

    def __init__(self, *, embed_fn: Any) -> None:
        # ``embed_fn(text) -> list[float] | Awaitable[list[float]]``.
        # Injection only — the orchestrator passes either the live
        # ``EmbeddingIndex.embed_text`` bound method or a fake.
        self._embed_fn = embed_fn

    async def probe(self, role_name: str, ref: str) -> ProbeResult:
        return await _run_embedding_probe(self._embed_fn, role_name, ref)


async def _run_embedding_probe(
    embed_fn: Any, role_name: str, ref: str
) -> ProbeResult:
    t0 = time.monotonic()
    now = datetime.now(timezone.utc).isoformat()
    expected_dim = _expected_dim(ref)
    try:
        coro_or_vec = embed_fn(_KNOWN_GOOD_TEXT)
        if asyncio.iscoroutine(coro_or_vec):
            vec = await coro_or_vec
        else:
            vec = coro_or_vec
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            role=role_name,
            ref=ref,
            ok=False,
            drift_kind="http_error",
            evidence={"exception": repr(exc)},
            probed_at=now,
            latency_ms=(time.monotonic() - t0) * 1000.0,
        )
    latency_ms = (time.monotonic() - t0) * 1000.0
    try:
        actual_dim = len(vec)
    except TypeError:
        return ProbeResult(
            role=role_name,
            ref=ref,
            ok=False,
            drift_kind="shape_mismatch",
            evidence={"reason": "embedding did not return a sized vector",
                      "raw_type": type(vec).__name__},
            probed_at=now,
            latency_ms=latency_ms,
        )
    if actual_dim == 0:
        return ProbeResult(
            role=role_name,
            ref=ref,
            ok=False,
            drift_kind="empty_output",
            evidence={"actual_dim": 0},
            probed_at=now,
            latency_ms=latency_ms,
        )
    if expected_dim is not None and actual_dim != expected_dim:
        return ProbeResult(
            role=role_name,
            ref=ref,
            ok=False,
            drift_kind="shape_mismatch",
            evidence={"expected_dim": expected_dim, "actual_dim": actual_dim},
            probed_at=now,
            latency_ms=latency_ms,
        )
    return ProbeResult(
        role=role_name,
        ref=ref,
        ok=True,
        drift_kind="none",
        evidence={"actual_dim": actual_dim, "expected_dim": expected_dim},
        probed_at=now,
        latency_ms=latency_ms,
    )


def _expected_dim(ref: str) -> int | None:
    """Resolve ``providers.yaml::<ref>.dimensions`` for the embedding ref.

    Returns ``None`` when the catalog entry doesn't pin a dimension, in
    which case the probe still validates "non-empty vector" but cannot
    assert exact width."""
    try:
        from tesseract.config.loader import load_config
        bundle = load_config()
        resolved = bundle.resolve(ref)
    except Exception:  # noqa: BLE001
        return None
    raw_dim = resolved.model.fields.get("dimensions")
    if isinstance(raw_dim, int) and raw_dim > 0:
        return raw_dim
    return None


__all__ = ["EmbeddingRoleProbe"]
