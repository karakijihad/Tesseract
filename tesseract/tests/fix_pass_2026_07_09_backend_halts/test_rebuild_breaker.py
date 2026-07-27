"""2026-07-09 backend-halt diagnosis — rebuild grind + index wipe.

On the 8GB-VRAM dev box, a local LLM (dreaming / vault_librarian) loads and
evicts the pinned embedding model, so `embed_text` returns None for a window
(182 `Embedding failed` today). `rebuild_from_store` then:
  1. looped every chunk doing a failing HTTP round-trip on the event loop
     (2.2k memories → multi-second loop stall, the 9-12s lag spikes), and
  2. swapped the resulting EMPTY index over the live one — wiping every
     vector until the next healthy rebuild.

Fix: a consecutive-failure circuit breaker (MAX_CONSECUTIVE_FAILURES) aborts
the rebuild early while nothing has embedded, and an all-failed rebuild never
swaps an empty index over live data.
"""

from __future__ import annotations

import asyncio
from pathlib import Path


def _build(tmp_path: Path):
    from tesseract.memory.embeddings import EmbeddingIndex

    return EmbeddingIndex(
        derived_dir=tmp_path,
        provider="fake",
        base_url="http://localhost",
        model="fake",
        dimensions=8,
        timeout_seconds=1.0,
        max_retries=1,
    )


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None

    @staticmethod
    def json() -> dict:
        return {"embedding": [0.1] * 8}


class _FakeClient:
    def __init__(self, *args, **kwargs) -> None:
        self.is_closed = False

    async def post(self, *args, **kwargs) -> _FakeResponse:
        return _FakeResponse()

    async def aclose(self) -> None:
        self.is_closed = True


def test_down_backend_aborts_early_and_preserves_index(tmp_path, monkeypatch) -> None:
    import tesseract.memory.embeddings as embeddings_mod

    monkeypatch.setattr(embeddings_mod.httpx, "AsyncClient", _FakeClient)
    idx = _build(tmp_path)

    async def run():
        # Seed live vectors with a healthy backend.
        await idx.add("mem_live_1", "alpha body")
        await idx.add("mem_live_2", "beta body")
        live_before = idx._index.ntotal
        assert live_before == 2

        # Backend now "down" — every embed returns None.
        calls = {"n": 0}

        async def _down(_text: str):
            calls["n"] += 1
            return None

        monkeypatch.setattr(idx, "embed_text", _down)

        pairs = [(f"mem-{i}", f"body {i}") for i in range(370)]
        count = await idx.rebuild_from_store(pairs)
        return count, live_before, idx._index.ntotal, calls["n"]

    count, live_before, live_after, embed_calls = asyncio.run(run())

    assert count == 0, "a down backend rebuild indexes nothing"
    assert live_after == live_before, "live index must NOT be wiped by a failed rebuild"
    assert embed_calls <= embeddings_mod.REBUILD_MAX_CONSECUTIVE_FAILURES, (
        f"breaker must abort after ~{embeddings_mod.REBUILD_MAX_CONSECUTIVE_FAILURES} "
        f"failures, not grind all 370 (got {embed_calls} calls)"
    )


def test_healthy_rebuild_still_swaps(tmp_path, monkeypatch) -> None:
    """Regression: a healthy backend still rebuilds and swaps normally."""
    import tesseract.memory.embeddings as embeddings_mod

    monkeypatch.setattr(embeddings_mod.httpx, "AsyncClient", _FakeClient)
    idx = _build(tmp_path)

    async def run():
        pairs = [(f"mem-{i}", f"body {i}") for i in range(10)]
        return await idx.rebuild_from_store(pairs)

    assert asyncio.run(run()) == 10
