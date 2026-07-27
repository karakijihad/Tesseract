"""WS coverage for `/soul-show` and `/reflect`.

Closes audit gap T2 — before this test, F1 D19-D21 had zero WS/Playwright
coverage. The tests drive `commands.cmd_soul_show` and `commands.cmd_reflect`
directly and capture the emitted envelopes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _fake_reflect_in_background(saves: int):
    """Build a stand-in for `reflect_in_background` that fires `on_complete`
    asynchronously and returns the resulting Task. The tests await the
    task to drive the callback to completion.

    `saves` is a count; the helper synthesises that many ``memory_save``
    summaries to match the new ``list[dict]`` callback contract.
    """
    pending: list[asyncio.Task] = []
    saves_list = [
        {"tool": "memory_save", "title": f"item-{i}", "snippet": f"snippet-{i}"}
        for i in range(saves)
    ]

    def factory(session, reason, *, on_complete=None, on_error=None):
        async def _run():
            if on_complete is not None:
                await on_complete(saves_list, reason)
        task = asyncio.create_task(_run(), name="fake_reflect_bg")
        pending.append(task)
        return task

    return factory, pending


@pytest.mark.asyncio
async def test_soul_show_emits_soul_updated_envelope(tmp_path: Path) -> None:
    from tesseract.mirror.server import commands as commands_module

    soul_path = tmp_path / "SOUL.md"
    soul_path.write_text("## Core Truths\n- test", encoding="utf-8")

    sent: list[dict] = []

    class _FakeWS:
        closed = False

        async def send_json(self, payload: dict) -> None:
            sent.append(payload)

    fake_session = SimpleNamespace(
        session_id="sess-1",
        event_log=[],
        ws=_FakeWS(),
    )

    with patch.object(commands_module, "soul_path", lambda: soul_path):
        await commands_module.cmd_soul_show(fake_session)

    matches = [env for env in sent if env.get("type") == "soul_updated"]
    assert len(matches) == 1, f"expected single soul_updated, got {sent}"
    env = matches[0]
    # Contract shape per _shared/mirror-envelopes.md post-2026-04-21:
    # type=soul_updated, category=session, data={content, source}.
    assert env["category"] == "session"
    assert env["data"]["source"] == "soul_show"
    assert "Core Truths" in env["data"]["content"]


@pytest.mark.asyncio
async def test_reflect_runs_librarian_and_returns_stats(tmp_path: Path) -> None:
    from tesseract.mirror.server import commands as commands_module

    sent: list[dict] = []

    class _FakeWS:
        closed = False

        async def send_json(self, payload: dict) -> None:
            sent.append(payload)

    class _FakeLibrarian:
        async def run_pass(self) -> dict:
            return {"promoted": 3, "deduped": 1, "skipped": 2, "counts": {}, "top": 0, "recent": 0}

        async def distill_personality_candidates(self, soul_path) -> None:
            pass

    class _FakeBundle:
        librarian = _FakeLibrarian()
        store = SimpleNamespace(store_dir=tmp_path / "memory-store")

    fake_session = SimpleNamespace(
        session_id="sess-2",
        event_log=[],
        ws=_FakeWS(),
        chat_session=SimpleNamespace(history=[]),
    )
    fake_app: dict = {"memory_bundle": _FakeBundle()}

    fake_factory, pending = _fake_reflect_in_background(saves=4)

    with (
        patch.object(commands_module, "reflect_in_background", new=fake_factory),
        patch.object(
            commands_module, "soul_path",
            lambda: SimpleNamespace(stat=lambda: SimpleNamespace(st_mtime=0.0), exists=lambda: False),
        ),
    ):
        await commands_module.cmd_reflect(fake_app, fake_session)
        for task in pending:
            await task

    results = [env for env in sent if env.get("type") == "reflect_result"]
    assert len(results) == 1, f"expected single reflect_result, got {sent}"
    data = results[0]["data"]
    assert data["saves"] == 4
    assert data["soul_edited"] is False
    assert data["librarian"] == {
        "promoted": 3, "deduped": 1, "skipped": 2, "counts": {}, "top": 0, "recent": 0,
    }


@pytest.mark.asyncio
async def test_reflect_survives_librarian_failure(tmp_path: Path) -> None:
    """Librarian failure is logged but must not cancel reflection.

    Regression guard for the `try/except` around bundle.librarian.run_pass
    in cmd_reflect — reflection's save count is the primary outcome; the
    librarian is metadata.
    """
    from tesseract.mirror.server import commands as commands_module

    sent: list[dict] = []

    class _FakeWS:
        closed = False

        async def send_json(self, payload: dict) -> None:
            sent.append(payload)

    class _ExplodingLibrarian:
        async def run_pass(self) -> dict:
            raise RuntimeError("boom")

        async def distill_personality_candidates(self, soul_path) -> None:
            pass

    fake_session = SimpleNamespace(
        session_id="sess-3",
        event_log=[],
        ws=_FakeWS(),
        chat_session=SimpleNamespace(history=[]),
    )
    fake_app: dict = {
        "memory_bundle": SimpleNamespace(
            librarian=_ExplodingLibrarian(),
            store=SimpleNamespace(store_dir=tmp_path / "memory-store"),
        )
    }

    fake_factory, pending = _fake_reflect_in_background(saves=1)

    with (
        patch.object(commands_module, "reflect_in_background", new=fake_factory),
        patch.object(
            commands_module, "soul_path",
            lambda: SimpleNamespace(stat=lambda: SimpleNamespace(st_mtime=0.0), exists=lambda: False),
        ),
    ):
        await commands_module.cmd_reflect(fake_app, fake_session)
        for task in pending:
            await task

    results = [env for env in sent if env.get("type") == "reflect_result"]
    assert len(results) == 1
    data = results[0]["data"]
    assert data["saves"] == 1
    assert data["librarian"] is None, "failed librarian pass should yield None, not crash"
