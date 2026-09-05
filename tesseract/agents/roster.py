"""The agent roster — what cards exist, and whether anything ever calls them.

The schedule half of `WHAT-RUNS.md` answers *declared but not firing*. This is
the same question of the other thing that runs on its own, and its answer is
**declared but never invoked**: three of the shipped cards are referenced from
nowhere in source, and a roster with a last-invoked column says so without
anyone reading nineteen files.

Derived, never authored, exactly as the schedule half is: the two roots decide
which section a card sits in (ownership is a location), the card's own
`description` is what it says about itself, and `agents/invocations.py` is what
says whether it ran. Nothing here is a second copy of any of the three.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tesseract.agents.invocations import Invocation, last_invocations

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CardRow:
    """One card, as the roster prints it."""

    name: str
    origin: str  # "system" (the app ships it) | "user" (the operator's)
    description: str
    model_role: str
    disabled: bool
    shadows_system: bool
    last: Invocation | None  # None means: never invoked, which is a state
    # Why this card cannot run, in the contract's own words, or None when
    # nothing stops it. A shipped card in this state stops the boot, so in
    # practice this carries an operator's own card: theirs to fix, and
    # invisible until a surface said it.
    cannot_run: str | None = None

    @property
    def never_invoked(self) -> bool:
        return self.last is None


def read_roster(
    *,
    agents_dir: Path | None = None,
    now: datetime | None = None,
) -> list[CardRow]:
    """Every card the runtime can see, with what is known about each.

    **A card that will not parse is a ROW, not a gap in the list.** It used to
    be skipped, on the reasoning that the contract check reports it — and that
    report was a log line, so the one card most worth seeing was the only one
    missing from the table that claims to list them all. It carries the parse
    error as its `cannot_run` and nothing else, because nothing else is known.

    `cannot_run` carries the contract's verdict for the cards that do parse,
    because "an operator's card is reported rather than refused" was a claim
    with no reader: every surface drew such a card as healthy.

    Each card is opened ONCE. The gap checks take the card this loop already
    holds (`gaps_for_card`), rather than a second pass over the same files.
    """
    from tesseract.agents.contract import (
        catalog_or_none,
        gaps_for_card,
        unreadable_gap,
    )
    from tesseract.agents.loader import list_agents_by_origin, load_agent

    invoked = last_invocations(now=now)
    catalog = catalog_or_none()
    rows: list[CardRow] = []
    for origin, names in list_agents_by_origin(agents_dir=agents_dir).items():
        for name in names:
            try:
                card = load_agent(name, agents_dir=agents_dir)
            except Exception as exc:  # noqa: BLE001 — the card IS the finding
                rows.append(CardRow(
                    name=name,
                    origin=origin,
                    description="",
                    model_role="",
                    disabled=False,
                    shadows_system=False,
                    last=invoked.get(name),
                    cannot_run=unreadable_gap(name, origin, exc).detail,
                ))
                continue
            try:
                gaps = gaps_for_card(name, origin, card, bundle=catalog)
            except Exception:  # noqa: BLE001 — a roster that cannot judge still lists
                # Said out loud, because the column going quiet looks exactly
                # like every card being fine.
                logger.warning("agent roster: %s could not be judged", name, exc_info=True)
                gaps = []
            blocking = next((g.detail for g in gaps if g.blocking), None)
            rows.append(CardRow(
                name=name,
                origin=origin,
                description=(card.description or "").strip(),
                model_role=(card.model_role or "").strip(),
                disabled=bool(card.disabled),
                shadows_system=bool(card.shadows_system),
                last=invoked.get(name),
                cannot_run=blocking,
            ))
    rows.sort(key=lambda row: row.name)
    return rows


__all__ = ["CardRow", "read_roster"]
