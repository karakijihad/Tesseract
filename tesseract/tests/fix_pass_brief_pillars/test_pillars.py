"""MO-9-13 — three fixed pillars for the daily-brief world section.

These are product-locked; the tests guard the public contract:
- exactly three pillars (tech / science / politics)
- query-template placeholder substitution
- max_results stays in the [1, 10] Tavily range
"""

from __future__ import annotations

from datetime import date

import pytest

from tesseract.orchestrator.brief.pillars import (
    DEFAULT_PILLARS,
    Pillar,
    render_query,
)


def test_three_pillars_present_in_fixed_order() -> None:
    names = [p.name for p in DEFAULT_PILLARS]
    assert names == ["tech", "science", "politics"]


def test_pillar_is_frozen_dataclass() -> None:
    p = DEFAULT_PILLARS[0]
    with pytest.raises(Exception):
        p.name = "mutated"  # type: ignore[misc]


@pytest.mark.parametrize("pillar", DEFAULT_PILLARS)
def test_pillar_max_results_within_tavily_range(pillar: Pillar) -> None:
    assert 1 <= pillar.max_results <= 10, (
        f"{pillar.name}.max_results={pillar.max_results} outside Tavily [1, 10] window"
    )


@pytest.mark.parametrize("pillar", DEFAULT_PILLARS)
def test_pillar_dedupe_window_is_positive(pillar: Pillar) -> None:
    assert pillar.dedupe_window_days >= 1


def test_render_query_substitutes_iso_date_and_week() -> None:
    p = Pillar(
        name="tech",
        query_template="news on {iso_date} for week {week_iso}",
    )
    out = render_query(p, date(2026, 5, 14))
    iso_week = date(2026, 5, 14).isocalendar()
    expected_week = f"{iso_week.year}-W{iso_week.week:02d}"
    assert "2026-05-14" in out
    assert expected_week in out
    assert "{iso_date}" not in out
    assert "{week_iso}" not in out


def test_render_query_unknown_placeholders_pass_through() -> None:
    """A typo in the template stays visible — silent expansion to '' would
    surface as a confusing Tavily query downstream."""
    p = Pillar(name="tech", query_template="news on {iso_dat} {topic}")
    out = render_query(p, date(2026, 5, 14))
    assert "{iso_dat}" in out
    assert "{topic}" in out


def test_default_pillars_render_week_iso() -> None:
    today = date(2026, 5, 14)
    for pillar in DEFAULT_PILLARS:
        out = render_query(pillar, today)
        assert "{" not in out, f"unexpanded placeholder in {pillar.name}: {out!r}"
