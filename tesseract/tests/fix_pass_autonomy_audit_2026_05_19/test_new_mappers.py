"""Codex audit-2 2026-05-19 P1 #1 — mapper for ``vault_signal``.

This source was declared in ``agenda-mappers.yaml`` as ``enabled: true``
but had no corresponding mapper module; bus events under this source
fell on the floor. Now it emits ``AgendaItemDraft``s on a plausible
payload shape and drops silently on malformed payloads.

(The ``memory_signal`` mapper added alongside this one was retired in
P4 prune wave 2 — zero bus producers ever; the ``MEMORY_SIGNAL``
``AgendaSource`` member survives as historical-only for the on-disk
items it already produced.)
"""

from __future__ import annotations

from tesseract.orchestrator.autonomy.event_bus import AutonomyEvent
from tesseract.orchestrator.autonomy.mappers import (
    DEFAULT_MAPPERS,
    map_vault_signal,
)
from tesseract.orchestrator.autonomy.models import AgendaSource, RiskClass


def _event(source: AgendaSource, payload: dict) -> AutonomyEvent:
    return AutonomyEvent.make(source, payload)


# -- DEFAULT_MAPPERS registry ----------------------------------------------


def test_default_mappers_registers_vault_signal() -> None:
    assert AgendaSource.VAULT_SIGNAL in DEFAULT_MAPPERS, "vault_signal mapper not registered"


# -- vault_signal ----------------------------------------------------------


def test_vault_signal_emits_draft_on_minimal_payload() -> None:
    event = _event(
        AgendaSource.VAULT_SIGNAL,
        {"summary": "new doc indexed: tesseract-roadmap-q3", "kind": "ingest"},
    )
    drafts = map_vault_signal(event)
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.source == AgendaSource.VAULT_SIGNAL
    assert draft.risk_class == RiskClass.PROPOSE


def test_vault_signal_drops_on_missing_summary() -> None:
    event = _event(AgendaSource.VAULT_SIGNAL, {"kind": "ingest"})
    assert map_vault_signal(event) == []


def test_vault_signal_threads_path_into_rationale_and_gate() -> None:
    event = _event(
        AgendaSource.VAULT_SIGNAL,
        {
            "summary": "contradiction surfaced",
            "kind": "contradiction",
            "vault_path": "sources/competitor-x.md",
            "change_type": "contradiction",
        },
    )
    draft = map_vault_signal(event)[0]
    assert "competitor-x" in draft.rationale.replace(".md", "").replace("/", "_") or \
        "sources/competitor-x.md" in draft.rationale
    assert "contradiction" in draft.rationale
    assert draft.approvals_required[0].target.startswith("vault:")
