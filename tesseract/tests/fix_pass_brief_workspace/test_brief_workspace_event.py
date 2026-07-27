"""MO-9-14 — BriefRenderer emits a `daily_brief` workspace event.

The renderer keeps the markdown file as the authoritative audit
artifact AND appends a structured workspace event so the operator
sees the newsletter card in the workspace stream. Voice `/read_brief`
keeps reading the .md (unchanged); the workspace event is the visual
read surface.

Per CLAUDE.md log-safety: every test monkeypatches ``TESSERACT_HOME``
before instantiating writers so the brief markdown, dedupe store,
events.jsonl, and interests profile all land under tmp_path.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tesseract.orchestrator.brief.pillars import DEFAULT_PILLARS, Pillar
from tesseract.orchestrator.brief.renderer import BriefRenderer
from tesseract.workspace_events import EventStore


@pytest.fixture(autouse=True)
def _tesseract_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def briefs_dir(tmp_path: Path) -> Path:
    return tmp_path / "memory-store" / "daily" / "briefs"


@pytest.fixture
def interests_path(tmp_path: Path) -> Path:
    return tmp_path / "memory-store" / "interests" / "profile.yaml"


@pytest.fixture
def event_store(tmp_path: Path) -> EventStore:
    return EventStore(tmp_path / "logs")


def _mock_invoker(outputs: dict[str, str]):
    async def _invoke(name: str, payload: dict) -> str:
        return outputs.get(name, "")
    return _invoke


def _stub_tavily(per_pillar: dict[str, list[dict]]):
    async def _fetch(query: str, options: dict) -> list[dict]:
        for pillar_name, hits in per_pillar.items():
            if pillar_name in query:
                return hits
        return []
    return _fetch


@pytest.mark.asyncio
async def test_render_emits_workspace_event(
    briefs_dir: Path, interests_path: Path, event_store: EventStore
) -> None:
    """Happy path — renderer writes both the .md and the workspace event."""
    invoker = _mock_invoker({
        "workspace-digest": "Two missions closed.",
        "mission-digest": "DONE — Mission alpha.",
        "memory-digest": "Belief about routing solidified.",
        "vault-digest": "- New — Phase 17 portability notes.",
        "world-digest": "Tech\n\nSome prose.",
    })
    renderer = BriefRenderer(
        briefs_dir=briefs_dir,
        interests_path=interests_path,
        invoke_digester=invoker,
        tavily_search=_stub_tavily({
            "tech":     [{"title": "Tech A", "url": "https://example.org/t1",
                          "content": "tech summary", "published_at": "2026-05-13"}],
            "science":  [],
            "politics": [],
        }),
        memory_store=None,
        event_store=event_store,
    )
    result = await renderer.render(date(2026, 5, 14))
    assert result.path.exists()
    assert result.workspace_event_id is not None

    events = event_store.list_events(kinds=("daily_brief",))
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "daily_brief"
    assert ev.source == "daily_brief"
    assert ev.title == "Daily brief — 2026-05-14"
    payload = ev.payload
    assert payload["date"] == "2026-05-14"
    sections = payload["sections"]
    assert sections["yesterday_in_tesseract"] == "Two missions closed."
    assert sections["yesterday_with_you"] == "DONE — Mission alpha."
    assert sections["what_i_learned"] == "Belief about routing solidified."
    assert sections["vault"] == ["New — Phase 17 portability notes."]
    # AU-23 / AU-24 — initiatives + ecosystem slots are present even when
    # the digesters return nothing, so the React renderer can render the
    # absent state without crashing on a missing key.
    assert sections["ecosystem"] == ""
    assert sections["initiatives"] == []
    world = sections["world"]
    assert list(world.keys()) == ["tech", "science", "politics"]
    assert len(world["tech"]) == 1
    card = world["tech"][0]
    assert card["title"] == "Tech A"
    assert card["url"] == "https://example.org/t1"
    assert card["source"] == "example.org"
    assert card["published_at"] == "2026-05-13"
    assert payload["cost_cap_reached"] is False


@pytest.mark.asyncio
async def test_render_without_event_store_is_noop(
    briefs_dir: Path, interests_path: Path
) -> None:
    """No event_store → renderer still writes the .md, no event emitted."""
    invoker = _mock_invoker({"workspace-digest": "Just one section."})
    renderer = BriefRenderer(
        briefs_dir=briefs_dir,
        interests_path=interests_path,
        invoke_digester=invoker,
        tavily_search=None,
        memory_store=None,
        event_store=None,
    )
    result = await renderer.render(date(2026, 5, 14))
    assert result.path.exists()
    assert result.workspace_event_id is None
    # workspace_payload is still assembled (downstream callers may persist)
    assert isinstance(result.workspace_payload, dict)
    assert result.workspace_payload["kind"] == "daily_brief"


@pytest.mark.asyncio
async def test_render_idempotent_overwrite_does_not_double_emit(
    briefs_dir: Path, interests_path: Path, event_store: EventStore
) -> None:
    """A cron path render with overwrite=False that hits the skipped-existing
    branch must NOT append a second workspace event for the same day."""
    invoker_first = _mock_invoker({"workspace-digest": "first"})
    renderer = BriefRenderer(
        briefs_dir=briefs_dir,
        interests_path=interests_path,
        invoke_digester=invoker_first,
        tavily_search=None,
        memory_store=None,
        event_store=event_store,
    )
    await renderer.render(date(2026, 5, 14), overwrite=True)
    assert len(event_store.list_events(kinds=("daily_brief",))) == 1

    invoker_second = _mock_invoker({"workspace-digest": "second"})
    renderer_cron = BriefRenderer(
        briefs_dir=briefs_dir,
        interests_path=interests_path,
        invoke_digester=invoker_second,
        tavily_search=None,
        memory_store=None,
        event_store=event_store,
    )
    skipped = await renderer_cron.render(date(2026, 5, 14), overwrite=False)
    assert skipped.skipped_existing is True
    # Still exactly one event for that day — skipped path returns early.
    assert len(event_store.list_events(kinds=("daily_brief",))) == 1


@pytest.mark.asyncio
async def test_render_overwrite_appends_a_second_event(
    briefs_dir: Path, interests_path: Path, event_store: EventStore
) -> None:
    """`/brief` slash semantics: re-running overwrites the .md AND appends
    a fresh event so the workspace shows the updated card. Each render is
    a new feed item — operator dismisses the prior one via Resolve.

    This documents intentional behavior; if a later phase consolidates
    re-runs into the same event_id, this test will change accordingly.
    """
    invoker = _mock_invoker({"workspace-digest": "v1"})
    renderer = BriefRenderer(
        briefs_dir=briefs_dir,
        interests_path=interests_path,
        invoke_digester=invoker,
        tavily_search=None,
        memory_store=None,
        event_store=event_store,
    )
    await renderer.render(date(2026, 5, 14), overwrite=True)
    await renderer.render(date(2026, 5, 14), overwrite=True)
    events = event_store.list_events(kinds=("daily_brief",))
    assert len(events) == 2


@pytest.mark.asyncio
async def test_world_card_sort_follows_renderer_affinity(
    briefs_dir: Path, interests_path: Path, event_store: EventStore
) -> None:
    """The interests-affinity ordering at the renderer carries through to
    the workspace card shape — the top item per pillar in the payload is
    the highest-scoring hit, not the Tavily-default order."""
    from tesseract.orchestrator.brief.interests import (
        InterestsProfile,
        Signal,
        record_signal,
        save_profile,
    )
    profile = InterestsProfile(pillars={"tech": {}, "science": {}, "politics": {}})
    for _ in range(5):
        profile = record_signal(profile, "tech", "local-first", Signal.INTERESTED)
    save_profile(profile, interests_path)

    tech_hits = [
        {"title": "Unrelated SaaS", "url": "https://example.org/a", "content": "saas"},
        {"title": "Local-first revival", "url": "https://example.org/b",
         "content": "local-first details"},
    ]
    renderer = BriefRenderer(
        briefs_dir=briefs_dir,
        pillars=(Pillar(name="tech", query_template="tech {week_iso}", max_results=5),),
        interests_path=interests_path,
        invoke_digester=_mock_invoker({}),
        tavily_search=_stub_tavily({"tech": tech_hits}),
        memory_store=None,
        event_store=event_store,
    )
    await renderer.render(date(2026, 5, 14))
    ev = event_store.list_events(kinds=("daily_brief",))[0]
    tech_cards = ev.payload["sections"]["world"]["tech"]
    assert tech_cards[0]["url"] == "https://example.org/b"
    assert tech_cards[1]["url"] == "https://example.org/a"


def test_event_kind_literal_includes_daily_brief() -> None:
    """Regression-guard the EventKind / EventSource Literal extension."""
    import typing as t

    from tesseract.workspace_events.events import EventKind, EventSource
    assert "daily_brief" in t.get_args(EventKind)
    assert "daily_brief" in t.get_args(EventSource)
