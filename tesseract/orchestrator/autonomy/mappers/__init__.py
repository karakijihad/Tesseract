"""Event → AgendaItemDraft mappers.

One module per :class:`AgendaSource`. Each mapper exposes a single
``map(event) -> list[AgendaItemDraft]`` function — pure, no IO, no
hidden state. The kernel reads ``tesseract/config/agenda-mappers.yaml``
on start and only registers mappers whose ``enabled`` is true.

P4 prune wave 2 (2026-07-04) deleted the zero-signal ``repo_health``,
``scheduler``, ``observer``, ``test_failure``, ``channel``, and
``memory_signal`` mappers along with their producer scheduler jobs —
see ``Docs/Plan/lean-agent-os/phase-4-prune-2-docs.md`` Batch 1.
"""

from tesseract.orchestrator.autonomy.mappers.operator import map as map_operator
from tesseract.orchestrator.autonomy.mappers.operator_view import (
    map as map_operator_view,
)
from tesseract.orchestrator.autonomy.mappers.provider_watch import (
    map as map_provider_watch,
)
from tesseract.orchestrator.autonomy.mappers.repo_upgrade import (
    map as map_repo_upgrade,
)
from tesseract.orchestrator.autonomy.mappers.scout import map as map_scout
from tesseract.orchestrator.autonomy.mappers.self_reflection import (
    map as map_self_reflection,
)
from tesseract.orchestrator.autonomy.mappers.strategist import (
    map as map_strategist,
)
from tesseract.orchestrator.autonomy.mappers.vault_signal import (
    map as map_vault_signal,
)
from tesseract.orchestrator.autonomy.models import AgendaSource

# Source → mapper function. The kernel filters this by the
# ``enabled`` flag in ``agenda-mappers.yaml`` before subscribing.
DEFAULT_MAPPERS = {
    AgendaSource.OPERATOR: map_operator,
    AgendaSource.OPERATOR_VIEW: map_operator_view,
    AgendaSource.PROVIDER_WATCH: map_provider_watch,
    AgendaSource.REPO_UPGRADE: map_repo_upgrade,
    AgendaSource.SCOUT: map_scout,
    AgendaSource.SELF_REFLECTION: map_self_reflection,
    AgendaSource.STRATEGIST: map_strategist,
    AgendaSource.VAULT_SIGNAL: map_vault_signal,
}


__all__ = [
    "DEFAULT_MAPPERS",
    "map_operator",
    "map_operator_view",
    "map_provider_watch",
    "map_repo_upgrade",
    "map_scout",
    "map_self_reflection",
    "map_strategist",
    "map_vault_signal",
]
