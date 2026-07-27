"""2026-07-03 backend crashloop fix — embed calls share one httpx client.

Constructing httpx.AsyncClient per embed_text ran ssl.create_default_context()
synchronously on the event loop; index_rebuild over a 2.2k-memory store pinned
the loop for minutes and the supervisor heartbeat-killed the backend in a
respawn loop. Pins: (1) one client reused across embed calls, (2) a closed
client is transparently replaced, (3) rebuild_from_store yields to the loop
periodically so heartbeats stay serviced.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


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
    instances = 0

    def __init__(self, *args, **kwargs) -> None:
        type(self).instances += 1
        self.is_closed = False
        self.posts = 0

    async def post(self, *args, **kwargs) -> _FakeResponse:
        self.posts += 1
        return _FakeResponse()

    async def aclose(self) -> None:
        self.is_closed = True


@pytest.fixture()
def fake_client(monkeypatch):
    _FakeClient.instances = 0
    import tesseract.memory.embeddings as embeddings_mod

    monkeypatch.setattr(embeddings_mod.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


def test_embed_text_reuses_one_client(tmp_path, fake_client) -> None:
    idx = _build(tmp_path)

    async def run() -> None:
        for _ in range(5):
            assert await idx.embed_text("hello") == [0.1] * 8

    asyncio.run(run())
    assert fake_client.instances == 1


def test_closed_client_is_replaced(tmp_path, fake_client) -> None:
    idx = _build(tmp_path)

    async def run() -> None:
        await idx.embed_text("one")
        await idx.aclose()
        await idx.embed_text("two")

    asyncio.run(run())
    assert fake_client.instances == 2


def test_rebuild_yields_to_event_loop(tmp_path, fake_client) -> None:
    idx = _build(tmp_path)
    pairs = [(f"mem-{i}", f"body {i}") for i in range(120)]
    loop_breaths = 0

    async def heartbeat() -> None:
        nonlocal loop_breaths
        while True:
            loop_breaths += 1
            await asyncio.sleep(0)

    async def run() -> int:
        hb = asyncio.create_task(heartbeat())
        try:
            return await idx.rebuild_from_store(pairs)
        finally:
            hb.cancel()

    count = asyncio.run(run())
    assert count == 120
    # The awaited fake post() never actually suspends, so without the explicit
    # periodic sleep(0) the heartbeat task would barely run mid-rebuild.
    assert loop_breaths >= 2
