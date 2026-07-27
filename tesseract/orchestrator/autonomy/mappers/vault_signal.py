"""``AgendaSource.VAULT_SIGNAL`` event → AgendaItemDraft.

Closes codex audit-2 2026-05-19 P1 #1 — `agenda-mappers.yaml` had
``vault_signals`` enabled but no mapper module existed.

Expected producers (future):

* ``VaultIndexer`` after a delta ingest pass (new doc indexed, source
  page hash changed, contradiction detected by ``vault_lint``).
* ``VaultLibrarianJob`` when a wiki rebuild surfaces a previously
  unreviewed contradiction or orphan.
* Operator-driven imports (``vault_ingest``) that promote a new doc to
  ``vault/sources/``.

Existing producers (``AU-22 VaultRawWatchJob``) bundle ingest results
into a ``vault_raw_ingest_batch`` workspace event — those go through
the workspace inbox, not the autonomy bus. This mapper exists for the
day a vault-side change wants to *propose* operator work directly.
"""

from __future__ import annotations

from tesseract.orchestrator.autonomy.drafts import AgendaItemDraft
from tesseract.orchestrator.autonomy.event_bus import AutonomyEvent
from tesseract.orchestrator.autonomy.models import (
    AgendaSource,
    ApprovalGate,
    RiskClass,
)


_MAX_RATIONALE_CHARS = 2000
_MAX_GOAL_CHARS = 500


def map(event: AutonomyEvent) -> list[AgendaItemDraft]:
    payload = event.payload or {}

    # Expected payload shape:
    #   kind:        "ingest" / "contradiction" / "orphan" / "rebuild"
    #   summary:     operator-facing description (required)
    #   vault_path:  optional path under vault/ (e.g. "sources/foo.md")
    #   change_type: "added" / "updated" / "removed" / "contradiction"
    #   risk_class:  optional override, defaults to PROPOSE
    summary = str(payload.get("summary") or "").strip()
    if not summary:
        return []

    kind = str(payload.get("kind") or "vault_signal").strip() or "vault_signal"
    vault_path = str(payload.get("vault_path") or payload.get("path") or "").strip()
    change_type = str(payload.get("change_type") or "").strip().lower()

    risk = _coerce_risk(payload.get("risk_class"))
    approvals = ()
    if risk in (RiskClass.PROPOSE, RiskClass.OPERATOR_GATE):
        target = vault_path or kind
        approvals = (ApprovalGate(kind="operator_review", target=f"vault:{target}"),)

    location = vault_path or "(no path)"
    goal = f"review vault signal ({kind}): {summary}"[:_MAX_GOAL_CHARS]

    rationale_parts = [
        f"vault_signal: {kind}",
        f"location: {location}",
        f"summary: {summary}",
    ]
    if change_type:
        rationale_parts.append(f"change_type: {change_type}")
    rationale = " | ".join(rationale_parts)[:_MAX_RATIONALE_CHARS]

    slug_seed = (kind + ("-" + vault_path.replace("/", "_") if vault_path else "")).lower()[:40] or "vault-signal"
    return [
        AgendaItemDraft(
            goal=goal,
            source=AgendaSource.VAULT_SIGNAL,
            risk_class=risk,
            source_event_id=event.event_id,
            rationale=rationale,
            approvals_required=approvals,
            slug=f"vault-{slug_seed}",
        )
    ]


def _coerce_risk(raw) -> RiskClass:
    if isinstance(raw, RiskClass):
        if raw is RiskClass.ABSOLUTE_DENY:
            return RiskClass.PROPOSE
        return raw
    value = str(raw or "").strip().lower()
    if value == "autonomous":
        return RiskClass.AUTONOMOUS
    if value == "operator_gate":
        return RiskClass.OPERATOR_GATE
    return RiskClass.PROPOSE


__all__ = ["map"]
