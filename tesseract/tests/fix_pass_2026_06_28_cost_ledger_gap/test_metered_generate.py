"""Cost-ledger gap (2026-06-28): scheduler + agent LLM calls must hit the ledger.

``MeteredAdapter`` wraps an adapter so ``generate`` / ``stream`` record token
usage to the ``CostLedger`` and run ``check_preflight`` before *priced* calls,
restoring parity with the chat path's ``_record_turn_usage`` / preflight guard.
Usage is read from the local STOP chunk (never round-tripped through shared
adapter state) so two wrappers over one live adapter can't cross-bill.

These tests use fakes — no real CostLedger, no real provider — so nothing is
written under ``tesseract/logs/``.
"""

from __future__ import annotations

from typing import Any

import pytest

from tesseract.brain.cost.ledger import BudgetExhausted, CostUsage
from tesseract.brain.cost.metered_adapter import MeteredAdapter
from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ModelAdapter,
    StreamChunk,
)


class _FakeAdapter(ModelAdapter):
    """Minimal adapter whose stream() yields text then a STOP usage chunk."""

    model = "fake-model"

    def __init__(self, usage: dict[str, int] | None) -> None:
        self._usage = usage

    async def stream(self, messages, tools=None, options=None):  # type: ignore[override]
        yield StreamChunk(type=ChunkType.TEXT, text="PONG")
        raw = {"usage": self._usage} if self._usage is not None else {}
        yield StreamChunk(type=ChunkType.STOP, stop_reason="end_turn", raw=raw)

    def count_tokens(self, messages) -> int:  # type: ignore[override]
        return 0

    async def check_available(self) -> bool:  # type: ignore[override]
        return True


class _RecordingLedger:
    """Captures record()/check_preflight() calls without touching disk.

    ``pricing`` mirrors ``CostLedger.pricing`` ({model: (in, out)}) so the
    wrapper's free-model detection can be exercised.
    """

    def __init__(
        self,
        *,
        preflight_raises: bool = False,
        pricing: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        self.records: list[tuple[str, str, CostUsage]] = []
        self.preflights: list[str] = []
        self._preflight_raises = preflight_raises
        self.pricing = pricing or {}

    def record(self, role: str, model: str, usage: CostUsage) -> None:
        self.records.append((role, model, usage))

    def check_preflight(self, role: str) -> None:
        self.preflights.append(role)
        if self._preflight_raises:
            raise BudgetExhausted(role, 1.0, 0.5, "role")


# --- MeteredAdapter records + preflights ------------------------------------


@pytest.mark.asyncio
async def test_metered_generate_records_usage() -> None:
    ledger = _RecordingLedger()
    opts = AdapterOptions(model="fake-model", role="autonomy_heartbeat", tier="api")
    inner = _FakeAdapter({"input_tokens": 100, "output_tokens": 20})
    metered = MeteredAdapter(inner, opts, ledger)

    out = await metered.generate("ping")

    assert out == "PONG"
    assert ledger.preflights == ["autonomy_heartbeat"]
    assert len(ledger.records) == 1
    role, model, usage = ledger.records[0]
    assert role == "autonomy_heartbeat"
    assert model == "fake-model"
    assert usage.input_tokens == 100
    assert usage.output_tokens == 20


@pytest.mark.asyncio
async def test_metered_preflight_skipped_for_free_model() -> None:
    """$0-priced models (NIM cataloged under `api:`, local, CLI) must never be
    blocked — they're the safety valve when the paid primary is capped."""
    ledger = _RecordingLedger(
        preflight_raises=True, pricing={"nim-model": (0.0, 0.0)}
    )
    opts = AdapterOptions(model="nim-model", role="agents_default", tier="api")
    metered = MeteredAdapter(_FakeAdapter({"input_tokens": 1}), opts, ledger)

    out = await metered.generate("ping")  # must NOT raise

    assert out == "PONG"
    assert ledger.preflights == []  # skipped: model is free
    assert len(ledger.records) == 1  # still recorded ($0)


@pytest.mark.asyncio
async def test_metered_preflight_blocks_paid_call() -> None:
    ledger = _RecordingLedger(preflight_raises=True, pricing={"gpt": (1.0, 2.0)})
    opts = AdapterOptions(model="gpt", role="chat_brain", tier="api")
    metered = MeteredAdapter(_FakeAdapter({"input_tokens": 1}), opts, ledger)

    with pytest.raises(BudgetExhausted):
        await metered.generate("ping")
    assert ledger.records == []  # blocked before spend


@pytest.mark.asyncio
async def test_metered_record_survives_unpriced_model() -> None:
    """record() raising RuntimeError (unknown/unpriced model) must not crash the job."""

    class _RaisingLedger(_RecordingLedger):
        def record(self, role, model, usage):  # type: ignore[override]
            raise RuntimeError("unknown model")

    opts = AdapterOptions(model="mystery", role="x", tier="api")
    metered = MeteredAdapter(_FakeAdapter({"input_tokens": 1}), opts, _RaisingLedger())
    out = await metered.generate("ping")  # swallows RuntimeError like chat.py
    assert out == "PONG"


@pytest.mark.asyncio
async def test_metered_stream_records_on_stop() -> None:
    ledger = _RecordingLedger()
    opts = AdapterOptions(model="fake-model", role="r", tier="api")
    metered = MeteredAdapter(_FakeAdapter({"input_tokens": 7}), opts, ledger)

    chunks = [c async for c in metered.stream([{"role": "user", "content": "hi"}])]

    assert any(c.type == ChunkType.STOP for c in chunks)
    assert len(ledger.records) == 1
    assert ledger.records[0][2].input_tokens == 7


@pytest.mark.asyncio
async def test_metered_delegates_model_and_availability() -> None:
    opts = AdapterOptions(model="fake-model", role="r", tier="api")
    metered = MeteredAdapter(_FakeAdapter({"input_tokens": 1}), opts, _RecordingLedger())
    assert metered.model == "fake-model"
    assert await metered.check_available() is True
