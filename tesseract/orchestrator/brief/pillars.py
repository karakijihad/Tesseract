"""Fixed pillar set for the daily-brief world section.

Replaces the operator-curated ``tracked-topics.yaml`` from MO-9-8. Three
pillars — tech / science / politics — are product-locked: the brief is
voice-friendly and the operator should not have to predict the world
they care about up front. Affinity ordering inside each pillar lives in
:mod:`tesseract.orchestrator.brief.interests` and biases world-digest's
pick order at render time.

Contract: ``Docs/Plan/mission-orchestrator/MO-9/phase-MO-9-13-...md §2``.

Plain frozen dataclasses, no YAML — the pillar set is part of the
product, not config. Adding / renaming a pillar is a code change with a
test update; the operator's lever is the interest-affinity profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Pillar:
    """One world-digest pillar.

    ``query_template`` is a Tavily query string with optional
    ``{iso_date}`` / ``{week_iso}`` placeholders (no ``{topic}`` —
    pillars are not topics, the agent fans out within them).
    """

    name: str
    query_template: str
    max_results: int = 5
    dedupe_window_days: int = 7


DEFAULT_PILLARS: tuple[Pillar, ...] = (
    Pillar(
        name="tech",
        query_template="major technology news this week {week_iso}",
        max_results=5,
        dedupe_window_days=7,
    ),
    Pillar(
        name="science",
        query_template="significant science news this week {week_iso}",
        max_results=5,
        dedupe_window_days=7,
    ),
    Pillar(
        name="politics",
        query_template="major world politics news this week {week_iso}",
        max_results=5,
        dedupe_window_days=7,
    ),
)


def render_query(pillar: Pillar, today: date) -> str:
    """Substitute ``{iso_date}`` / ``{week_iso}`` in the template.

    Unknown placeholders pass through unchanged so a typo does not
    silently expand to an empty string — mirrors the ``tracked_topics``
    loader's substitution semantics for parity with MO-9-8 tests.
    """
    iso_week = today.isocalendar()
    week_iso = f"{iso_week.year}-W{iso_week.week:02d}"
    return (
        pillar.query_template
        .replace("{iso_date}", today.isoformat())
        .replace("{week_iso}", week_iso)
    )


__all__ = ["Pillar", "DEFAULT_PILLARS", "render_query"]
