"""Boot-warm helpers — verify Mirror startup fires model warm-ups as
fire-and-forget background tasks that never block boot and never crash
on failure.

Tests target the helpers directly (`_schedule_warmup`,
`_drain_warmup_tasks`) rather than the full `_on_startup` boot path
because they are the pure surface that can fail or block. STT no longer
needs a warm-up — Gemini Flash audio is cloud-only, no model load — so
the dedicated `_schedule_stt_warmup` helper has been removed; only the
generic `_schedule_warmup` (used for embeddings) is exercised here.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from tesseract.mirror.server import app as app_module


def _make_app() -> web.Application:
    a = web.Application()
    a["_warmup_tasks"] = []
    return a


async def test_schedule_warmup_returns_immediately_with_slow_coro():
    """`_schedule_warmup` must register the task and return at once even if
    the coroutine sleeps — proves boot is not blocked on warm-up."""
    a = _make_app()

    async def slow():
        await asyncio.sleep(5.0)

    app_module._schedule_warmup(a, slow(), name="slow")

    assert len(a["_warmup_tasks"]) == 1
    task = a["_warmup_tasks"][0]
    assert not task.done()
    assert task.get_name() == "warmup:slow"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_schedule_warmup_swallows_exception(caplog):
    """A failing warm-up must log via `log.exception` and not propagate —
    Mirror must keep running even if a downstream service is unreachable."""
    a = _make_app()

    async def boom():
        raise RuntimeError("ollama is down")

    with caplog.at_level(logging.ERROR, logger=app_module.log.name):
        app_module._schedule_warmup(a, boom(), name="boom")
        await asyncio.gather(*a["_warmup_tasks"])

    task = a["_warmup_tasks"][0]
    assert task.done()
    assert task.exception() is None
    assert any(
        "warmup task 'boom' failed" in rec.getMessage()
        for rec in caplog.records
    )


async def test_drain_warmup_tasks_cancels_pending():
    """Mirror shutdown must cancel in-flight warm-ups so the process exits
    cleanly. Threads spawned via `to_thread` cannot be killed mid-load,
    but the wrapping task is reaped."""
    a = _make_app()

    async def slow():
        await asyncio.sleep(10.0)

    app_module._schedule_warmup(a, slow(), name="slow")
    task = a["_warmup_tasks"][0]
    assert not task.done()

    await app_module._drain_warmup_tasks(a)

    assert task.done()
    assert task.cancelled() or task.exception() is None


async def test_drain_warmup_tasks_noop_when_empty():
    """No warm-up tasks => drain returns instantly without error."""
    a = _make_app()
    a["_warmup_tasks"] = []
    await app_module._drain_warmup_tasks(a)
