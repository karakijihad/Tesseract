"""AU-23 Session 3 — BriefRenderer ↔ strategist Initiatives section.

The renderer reads the most recent `strategist_summary` workspace event
and emits a flat bullet list under `## Initiatives`. No sub-agent, no
LLM call. Empty / stale → section dropped (mirrors the vault-empty
rule).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tesseract.orchestrator.brief.renderer import BriefRenderer, SECTION_ORDER
from tesseract.workspace_events.events import EventStore, WorkspaceEvent


@pytest.fixture(autouse=True)
def _tesseract_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def briefs_dir(tmp_path: Path) -> Path:
    return tmp_path / "memory-store" / "daily" / "briefs"


def _mock_invoker(outputs: dict[str, str] | None = None):
    captured: list[tuple[str, dict]] = []
    outputs = outputs or {}

    async def _invoke(name: str, payload: dict) -> str:
        captured.append((name, payload))
        return outputs.get(name, "")

    _invoke.captured = captured  # type: ignore[attr-defined]
    return _invoke


def _seed_strategist_summary(
    store: EventStore,
    *,
    initiatives: list[dict[str, Any]],
    ts: str | None = None,
) -> WorkspaceEvent:
    body_lines = [
        f"• {i['slug']} — {i['goal'][:120]}" for i in initiatives
    ]
    ev = WorkspaceEvent.new(
        kind="strategist_summary",
        source="strategist",
        title=f"strategist — {len(initiatives)} initiative(s) this tick",
        summary="\n".join(body_lines)[:1200],
        payload={
            "emitted_at": ts or datetime.now(timezone.utc).isoformat(),
            "initiatives": initiatives,
        },
        author_id="strategist",
        author_display="Autonomy strategist",
    )
    if ts is not None:
        # Override ts so we can simulate a stale event.
        ev = WorkspaceEvent(
            event_id=ev.event_id,
            ts=ts,
            kind=ev.kind,
            source=ev.source,
            title=ev.title,
            summary=ev.summary,
            payload=ev.payload,
            status=ev.status,
            priority=ev.priority,
            decided_at=ev.decided_at,
            decided_reason=ev.decided_reason,
            delivered_to_tars=ev.delivered_to_tars,
            author_id=ev.author_id,
            author_display=ev.author_display,
        )
    store.append_event(ev)
    return ev


# ── section ordering ───────────────────────────────────────────────


def test_initiatives_section_appears_after_world() -> None:
    slugs = [slug for slug, _ in SECTION_ORDER]
    assert slugs.index("strategist-initiatives") == slugs.index("world-digest") + 1


# ── rendered when a recent summary exists ──────────────────────────


@pytest.mark.asyncio
async def test_initiatives_rendered_when_summary_within_window(
    tmp_path: Path, briefs_dir: Path,
):
    target = date(2026, 5, 20)
    event_store = EventStore(tmp_path / "logs")
    _seed_strategist_summary(event_store, initiatives=[
        {
            "slug": "rotate-tavily-key",
            "goal": "Rotate the Tavily API key and update .env.",
            "rationale": "Three rotations overdue per the security policy.",
            "suggested_risk_class": "operator_gate",
            "confidence": 0.85,
            "horizon_days": 5,
        }
    ])
    renderer = BriefRenderer(
        briefs_dir=briefs_dir,
        invoke_digester=_mock_invoker(),
        tavily_search=None,
        memory_store=None,
        event_store=event_store,
    )
    result = await renderer.render(target)
    text = result.path.read_text(encoding="utf-8")
    assert "## Initiatives" in text
    assert "Rotate the Tavily API key" in text
    assert "operator_gate" in text
    assert "confidence 85%" in text
    assert "horizon 5d" in text
    assert "strategist-initiatives" in result.sections_rendered

    # Codex audit 2026-05-20 §M3 — the structured workspace payload must
    # also surface the bullets so the Mirror DailyBriefBody card is not
    # silently empty.
    payload = result.workspace_payload or {}
    initiatives_bullets = payload.get("sections", {}).get("initiatives", [])
    assert isinstance(initiatives_bullets, list)
    assert initiatives_bullets, "initiatives bullets must reach the workspace payload"
    assert any("Rotate the Tavily API key" in b for b in initiatives_bullets)


@pytest.mark.asyncio
async def test_initiatives_section_dropped_when_no_summary(
    tmp_path: Path, briefs_dir: Path,
):
    target = date(2026, 5, 20)
    event_store = EventStore(tmp_path / "logs")
    renderer = BriefRenderer(
        briefs_dir=briefs_dir,
        invoke_digester=_mock_invoker(),
        tavily_search=None,
        memory_store=None,
        event_store=event_store,
    )
    result = await renderer.render(target)
    text = result.path.read_text(encoding="utf-8")
    assert "## Initiatives" not in text
    assert "strategist-initiatives" in result.sections_dropped


@pytest.mark.asyncio
async def test_initiatives_section_dropped_when_summary_too_old(
    tmp_path: Path, briefs_dir: Path,
):
    target = date(2026, 5, 20)
    event_store = EventStore(tmp_path / "logs")
    # Stale summary, well outside the default 72h lookback.
    old_ts = (datetime(2026, 5, 1, tzinfo=timezone.utc)).isoformat()
    _seed_strategist_summary(event_store, initiatives=[
        {
            "slug": "ancient",
            "goal": "Do an ancient thing.",
            "rationale": "Older than the lookback window.",
            "confidence": 0.9,
            "horizon_days": 5,
        }
    ], ts=old_ts)

    renderer = BriefRenderer(
        briefs_dir=briefs_dir,
        invoke_digester=_mock_invoker(),
        tavily_search=None,
        memory_store=None,
        event_store=event_store,
    )
    result = await renderer.render(target)
    text = result.path.read_text(encoding="utf-8")
    assert "## Initiatives" not in text


@pytest.mark.asyncio
async def test_initiatives_section_dropped_when_event_store_absent(
    briefs_dir: Path,
):
    target = date(2026, 5, 20)
    renderer = BriefRenderer(
        briefs_dir=briefs_dir,
        invoke_digester=_mock_invoker(),
        tavily_search=None,
        memory_store=None,
        # event_store not passed.
    )
    result = await renderer.render(target)
    text = result.path.read_text(encoding="utf-8")
    assert "## Initiatives" not in text
    assert "strategist-initiatives" in result.sections_dropped
