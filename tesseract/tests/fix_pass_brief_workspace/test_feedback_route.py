"""MO-9-14 — POST /api/brief/feedback applies signals to InterestsProfile.

Atomic write, rejects unknown signal kinds, returns updated affinity row.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.mirror.server.routes import brief as brief_route


@pytest.fixture(autouse=True)
def _tesseract_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


async def _make_client() -> TestClient:
    app = web.Application()
    app.router.add_post("/api/brief/feedback", brief_route.brief_feedback)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_feedback_interested_writes_profile(_tesseract_home: Path) -> None:
    client = await _make_client()
    try:
        r = await client.post(
            "/api/brief/feedback",
            json={
                "date": "2026-05-14",
                "pillar": "tech",
                "url": "https://example.org/a",
                "topic": "local-first",
                "signal": "interested",
            },
        )
        assert r.status == 200
        body = await r.json()
        assert body["pillar"] == "tech"
        assert body["signal"] == "INTERESTED"
        assert body["affinity"]["local-first"] == pytest.approx(1.0)
        # File written under tmp_path.
        profile = _tesseract_home / "memory-store" / "interests" / "profile.yaml"
        assert profile.exists()
        raw = yaml.safe_load(profile.read_text(encoding="utf-8"))
        assert raw["pillars"]["tech"]["local-first"] == 1.0
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_feedback_repeated_signals_accumulate(_tesseract_home: Path) -> None:
    client = await _make_client()
    try:
        for _ in range(3):
            r = await client.post(
                "/api/brief/feedback",
                json={
                    "date": "2026-05-14",
                    "pillar": "tech",
                    "url": "https://example.org/a",
                    "topic": "wasm",
                    "signal": "dig_deeper",
                },
            )
            assert r.status == 200
        final = await r.json()  # noqa: F821 — last r in loop
        # 3 × +0.5 = +1.5
        assert final["affinity"]["wasm"] == pytest.approx(1.5)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_feedback_rejects_unknown_signal(_tesseract_home: Path) -> None:
    client = await _make_client()
    try:
        r = await client.post(
            "/api/brief/feedback",
            json={
                "date": "2026-05-14",
                "pillar": "tech",
                "url": "https://example.org/a",
                "signal": "thumbs_sideways",
            },
        )
        assert r.status == 400
        body = await r.json()
        assert "signal" in body["error"].lower()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_feedback_rejects_invalid_date(_tesseract_home: Path) -> None:
    client = await _make_client()
    try:
        r = await client.post(
            "/api/brief/feedback",
            json={
                "date": "2026/05/14",  # wrong separator
                "pillar": "tech",
                "url": "https://example.org/a",
                "signal": "interested",
            },
        )
        assert r.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_feedback_missing_pillar_returns_400(_tesseract_home: Path) -> None:
    client = await _make_client()
    try:
        r = await client.post(
            "/api/brief/feedback",
            json={
                "date": "2026-05-14",
                "url": "https://example.org/a",
                "signal": "interested",
            },
        )
        assert r.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_feedback_keys_on_url_when_topic_absent(_tesseract_home: Path) -> None:
    """When the operator clicks 👍 on a card without an explicit topic
    tag, the route keys the signal on the URL so source-level affinity
    accumulates over time."""
    client = await _make_client()
    try:
        url = "https://example.org/article-slug"
        r = await client.post(
            "/api/brief/feedback",
            json={
                "date": "2026-05-14",
                "pillar": "science",
                "url": url,
                "signal": "interested",
            },
        )
        assert r.status == 200
        body = await r.json()
        assert body["topic"] == url
        assert body["affinity"][url] == pytest.approx(1.0)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_feedback_topic_or_url_required(_tesseract_home: Path) -> None:
    """Both topic and url empty/missing → 400."""
    client = await _make_client()
    try:
        r = await client.post(
            "/api/brief/feedback",
            json={
                "date": "2026-05-14",
                "pillar": "politics",
                "signal": "not_for_me",
            },
        )
        assert r.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_feedback_not_for_me_decreases(_tesseract_home: Path) -> None:
    client = await _make_client()
    try:
        for signal in ("not_for_me", "not_for_me", "not_for_me"):
            r = await client.post(
                "/api/brief/feedback",
                json={
                    "date": "2026-05-14",
                    "pillar": "tech",
                    "url": "https://example.org/x",
                    "topic": "crypto",
                    "signal": signal,
                },
            )
            assert r.status == 200
        final = await r.json()  # noqa: F821
        assert final["affinity"]["crypto"] == pytest.approx(-3.0)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_feedback_invalid_json_body(_tesseract_home: Path) -> None:
    client = await _make_client()
    try:
        r = await client.post(
            "/api/brief/feedback",
            data="not json at all",
            headers={"Content-Type": "application/json"},
        )
        assert r.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_concurrent_feedback_does_not_drop_signals(_tesseract_home: Path) -> None:
    """Phase-gate IMPORTANT fold: 50 concurrent POSTs against the same
    topic must accumulate every signal — the route serialises the
    load→record→save sequence under `_FEEDBACK_LOCK`."""
    import asyncio
    client = await _make_client()
    try:
        async def _hit() -> None:
            r = await client.post(
                "/api/brief/feedback",
                json={
                    "date": "2026-05-14",
                    "pillar": "tech",
                    "url": "https://example.org/x",
                    "topic": "concurrent-topic",
                    "signal": "interested",
                },
            )
            assert r.status == 200
        await asyncio.gather(*(_hit() for _ in range(50)))
        # 50 × +1.0 clamped to WEIGHT_CLAMP=10.0 — pre-fix a lost-update
        # race would land somewhere below 10.0 with timing flake; with
        # the lock, every signal is observed and the result saturates.
        from tesseract.orchestrator.brief.interests import (
            WEIGHT_CLAMP,
            load_profile,
        )
        profile = load_profile()
        assert profile.pillars["tech"]["concurrent-topic"] == WEIGHT_CLAMP
    finally:
        await client.close()


def test_routes_schedule_handler_whitelist_includes_brief_jobs() -> None:
    """Phase-gate MINOR fold: the Mirror schedule "Add job" modal lists
    DailyBriefJob + InterestsDecayJob so the operator can add either
    via UI without typing the dotpath manually. Bind this to the
    handlers list so a future rename doesn't silently drop the entry."""
    import inspect

    from tesseract.mirror.server.routes import schedule as schedule_route

    src = inspect.getsource(schedule_route.list_handlers)
    assert "DailyBriefJob" in src
    assert "InterestsDecayJob" in src
