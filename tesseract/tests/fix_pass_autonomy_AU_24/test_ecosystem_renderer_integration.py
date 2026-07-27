"""AU-24 — BriefRenderer ↔ ecosystem-radar integration.

The renderer pre-fetches the four streams and hands them to the
``ecosystem-digest`` agent only when at least one stream has rows in
the window. Empty input → section dropped, same contract as
vault-digest's empty-log rule.

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


def _seed_one_memory_signal(home: Path, when: datetime) -> None:
    agenda_active = home / "agenda" / "active"
    agenda_active.mkdir(parents=True, exist_ok=True)
    (agenda_active / "ag_seed.json").write_text(json.dumps({
        "id": "ag_seed",
        "source": "memory_signal",
        "goal": "review memory signal (provider_pricing): Anthropic input rate change",
        "rationale": "memory_signal: provider_pricing | summary: Anthropic input rate change | entities: anthropic, pricing",
        "created_at": when.astimezone(timezone.utc).isoformat(),
    }))


# ────────────────────────────────────────────────────────────────────
# 1. Section order: ecosystem-digest sits after vault, before world.
# ────────────────────────────────────────────────────────────────────


def test_section_order_ecosystem_between_vault_and_world() -> None:
    slugs = [slug for slug, _ in SECTION_ORDER]
    assert slugs.index("ecosystem-digest") == slugs.index("vault-digest") + 1
    assert slugs.index("world-digest") == slugs.index("ecosystem-digest") + 1


# ────────────────────────────────────────────────────────────────────
# 2. Ecosystem section is invoked + rendered when ANY input stream has
#    content in the window.
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ecosystem_invoked_when_signal_present(
    tmp_path: Path, briefs_dir: Path, interests_path: Path
) -> None:
    target = date(2026, 5, 20)
    _seed_one_memory_signal(
        tmp_path,
        datetime.combine(target, datetime.min.time(), tzinfo=timezone.utc)
        - timedelta(hours=2),
    )
    invoker = _mock_invoker({"ecosystem-digest": "Anthropic shifted input pricing. Source: anthropic. Suggested: queue a providers.yaml refresh."})
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
    assert "## Ecosystem" in text
    assert "Anthropic shifted input pricing" in text
    assert "ecosystem-digest" in result.sections_rendered
    # The pre-fetched payload reached the agent.
    ecosystem_payloads = [p for n, p in invoker.captured if n == "ecosystem-digest"]  # type: ignore[attr-defined]
    assert ecosystem_payloads, "ecosystem-digest must be invoked when a signal landed"
    payload = ecosystem_payloads[0]
    assert payload["since_days"] == 7
    assert len(payload["memory_signals"]) == 1
    assert payload["memory_signals"][0]["kind"] == "provider_pricing"

    # Codex audit 2026-05-20 §M3 — the structured workspace payload must
    # also carry the ecosystem prose so the Mirror DailyBriefBody card is
    # not silently empty.
    ws_payload = result.workspace_payload or {}
    ecosystem_section = ws_payload.get("sections", {}).get("ecosystem", "")
    assert "Anthropic shifted input pricing" in ecosystem_section


# ────────────────────────────────────────────────────────────────────
# 3. All-empty streams → ecosystem section dropped and the agent is
#    NOT invoked (mirrors vault-digest's empty-log rule). Saves both an
#    LLM round-trip and a guaranteed hallucination.
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ecosystem_section_skipped_when_all_streams_empty(
    tmp_path: Path, briefs_dir: Path, interests_path: Path
) -> None:
    target = date(2026, 5, 20)
    invoker = _mock_invoker({"ecosystem-digest": "should not appear"})
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
    assert "## Ecosystem" not in text
    assert "ecosystem-digest" in result.sections_dropped
    invoked = [name for name, _ in invoker.captured]  # type: ignore[attr-defined]
    assert "ecosystem-digest" not in invoked


# ────────────────────────────────────────────────────────────────────
# 4. Backward compat — when ecosystem_home is not wired, the renderer
#    falls through to a since-24h placeholder so legacy callers (tests
#    that stub the digester) keep working. Section presence then
#    depends entirely on what the stubbed digester returns.
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_ecosystem_home_falls_through_to_stub(
    briefs_dir: Path, interests_path: Path
) -> None:
    invoker = _mock_invoker({"ecosystem-digest": "Stubbed ecosystem output."})
    renderer = BriefRenderer(
        briefs_dir=briefs_dir,
        interests_path=interests_path,
        invoke_digester=invoker,
        tavily_search=None,
        memory_store=None,
        # ecosystem_home NOT set — legacy renderer construction.
    )
    result = await renderer.render(date(2026, 5, 20))
    text = result.path.read_text(encoding="utf-8")
    assert "## Ecosystem" in text
    assert "Stubbed ecosystem output." in text
    ecosystem_payloads = [p for n, p in invoker.captured if n == "ecosystem-digest"]  # type: ignore[attr-defined]
    assert ecosystem_payloads[0] == {"since_hours": 24}
