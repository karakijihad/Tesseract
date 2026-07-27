"""Deferred-item fix — BriefRenderer <-> mission-digest re-source.

The ``## Yesterday with you`` section (payload key ``yesterday_with_you``,
locked schema per MO-9-14) used to be fed by a mission registry that no
longer exists (mission engine deleted, P4 prune wave 1). The renderer
now pre-fetches DONE/BLOCKED agenda-store items and hands them to the
``mission-digest`` agent only when at least one landed in the window —
same empty-signal-skip contract as ecosystem-digest / vault-digest.

Per CLAUDE.md log-safety: every test monkeypatches ``TESSERACT_HOME``
before instantiating writers.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tesseract.orchestrator.brief.renderer import BriefRenderer, SECTION_ORDER


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


def _mock_invoker(outputs: dict[str, str]):
    captured: list[tuple[str, dict]] = []

    async def _invoke(name: str, payload: dict) -> str:
        captured.append((name, payload))
        return outputs.get(name, "")

    _invoke.captured = captured  # type: ignore[attr-defined]
    return _invoke


def _seed_done_item(home: Path, when: datetime) -> None:
    archive = home / "agenda" / "archive" / "2026-07"
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "ag_done.json").write_text(
        json.dumps(
            {
                "id": "ag_done",
                "status": "done",
                "goal": "roll out the pricing sync",
                "blocked_reason": "",
                "source": "strategist",
                "updated_at": when.astimezone(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )


# ────────────────────────────────────────────────────────────────────
# 1. Section stays in its locked slot between workspace-digest and
#    memory-digest, keyed by the unchanged slug/header pair.
# ────────────────────────────────────────────────────────────────────


def test_section_order_mission_digest_unchanged() -> None:
    slugs = [slug for slug, _ in SECTION_ORDER]
    headers = dict(SECTION_ORDER)
    assert slugs.index("mission-digest") == slugs.index("workspace-digest") + 1
    assert slugs.index("memory-digest") == slugs.index("mission-digest") + 1
    assert headers["mission-digest"] == "## Yesterday with you"


# ────────────────────────────────────────────────────────────────────
# 2. Mission-digest is invoked with the pre-fetched agenda payload when
#    an item went DONE/BLOCKED in the window, and the rendered body +
#    workspace payload both carry the digester's prose under the
#    locked ``yesterday_with_you`` key.
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mission_digest_invoked_with_agenda_payload_when_signal_present(
    tmp_path: Path, briefs_dir: Path, interests_path: Path
) -> None:
    target = date(2026, 7, 12)
    _seed_done_item(
        tmp_path,
        datetime.combine(target, datetime.min.time(), tzinfo=timezone.utc)
        - timedelta(hours=2),
    )
    invoker = _mock_invoker(
        {"mission-digest": "DONE — rolled out the pricing sync."}
    )
    renderer = BriefRenderer(
        briefs_dir=briefs_dir,
        interests_path=interests_path,
        invoke_digester=invoker,
        tavily_search=None,
        memory_store=None,
        ecosystem_home=tmp_path,
    )
    result = await renderer.render(target)
    text = result.path.read_text(encoding="utf-8")
    assert "## Yesterday with you" in text
    assert "rolled out the pricing sync" in text
    assert "mission-digest" in result.sections_rendered

    payloads = [p for n, p in invoker.captured if n == "mission-digest"]  # type: ignore[attr-defined]
    assert payloads, "mission-digest must be invoked when an agenda item landed"
    payload = payloads[0]
    assert payload["since_hours"] == 24
    assert len(payload["items"]) == 1
    assert payload["items"][0]["goal"] == "roll out the pricing sync"
    assert payload["items"][0]["status"] == "done"

    ws_payload = result.workspace_payload or {}
    assert (
        ws_payload.get("sections", {}).get("yesterday_with_you")
        == "DONE — rolled out the pricing sync."
    )


# ────────────────────────────────────────────────────────────────────
# 3. No agenda item went DONE/BLOCKED in the window → section dropped
#    and the digester is NOT invoked (saves an LLM round-trip and a
#    guaranteed hallucination against an empty payload).
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mission_digest_skipped_when_no_agenda_activity(
    tmp_path: Path, briefs_dir: Path, interests_path: Path
) -> None:
    target = date(2026, 7, 12)
    invoker = _mock_invoker({"mission-digest": "should not appear"})
    renderer = BriefRenderer(
        briefs_dir=briefs_dir,
        interests_path=interests_path,
        invoke_digester=invoker,
        tavily_search=None,
        memory_store=None,
        ecosystem_home=tmp_path,
    )
    result = await renderer.render(target)
    text = result.path.read_text(encoding="utf-8")
    assert "## Yesterday with you" not in text
    assert "mission-digest" in result.sections_dropped
    invoked = [name for name, _ in invoker.captured]  # type: ignore[attr-defined]
    assert "mission-digest" not in invoked


# ────────────────────────────────────────────────────────────────────
# 4. Backward compat — when ecosystem_home (the shared TESSERACT_HOME
#    root) is not wired, the renderer falls through to the legacy
#    since-24h placeholder so stub-digester tests keep working.
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_home_falls_through_to_since_hours_stub(
    briefs_dir: Path, interests_path: Path
) -> None:
    invoker = _mock_invoker({"mission-digest": "Stubbed mission output."})
    renderer = BriefRenderer(
        briefs_dir=briefs_dir,
        interests_path=interests_path,
        invoke_digester=invoker,
        tavily_search=None,
        memory_store=None,
        # ecosystem_home NOT set — legacy renderer construction.
    )
    result = await renderer.render(date(2026, 7, 12))
    text = result.path.read_text(encoding="utf-8")
    assert "## Yesterday with you" in text
    assert "Stubbed mission output." in text
    payloads = [p for n, p in invoker.captured if n == "mission-digest"]  # type: ignore[attr-defined]
    assert payloads[0] == {"since_hours": 24}
