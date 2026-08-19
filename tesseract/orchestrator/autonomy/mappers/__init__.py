"""Event → AgendaItemDraft mappers.

One module per :class:`AgendaSource`. Each mapper exposes a single
``map(event) -> list[AgendaItemDraft]`` function — pure, no IO, no
hidden state. The kernel reads ``tesseract/config/agenda-mappers.yaml``
on start and only registers mappers whose ``enabled`` is true.

**A mapper exists to turn something that HAPPENED into work.** Six were deleted
because they turned something *observed* into work instead: ``operator_view``
(where the operator was looking), ``self_reflection`` and ``strategist`` (a
model asked what might be worth doing), ``vault_signal`` (which never had a
publisher at all), and ``scout`` / ``repo_upgrade``, both of which proposed
changes to the application — a tree an update replaces wholesale, so the
proposal had nowhere durable to land. An earlier prune took ``repo_health``,
``scheduler``, ``observer``, ``test_failure``, ``channel`` and ``memory_signal``
for the same reason.

What remains produces a draft only from a fact: the operator asked for it, or a
probe measured it. Recovery items are written by the recovery pass directly
rather than through the bus.
"""

from tesseract.orchestrator.autonomy.mappers.operator import map as map_operator
from tesseract.orchestrator.autonomy.mappers.provider_watch import (
    map as map_provider_watch,
)
from tesseract.orchestrator.autonomy.models import AgendaSource

# Source → mapper function. The kernel filters this by the
# ``enabled`` flag in ``agenda-mappers.yaml`` before subscribing.
DEFAULT_MAPPERS = {
    AgendaSource.OPERATOR: map_operator,
    AgendaSource.PROVIDER_WATCH: map_provider_watch,
}


# Source → what publishes its events. A mapper with no producer is a source
# that can never fire, and three shipped that way until they were deleted.
# Declared here rather than in config because which code path emits which event
# is a fact about the code, and it must stay beside the mapper list it has to
# agree with.
#
# `job:<name>` resolves against `schedule.yaml`; `live:<what>` is an
# always-running in-process publisher with no schedule row of its own.
SOURCE_PRODUCERS: dict[AgendaSource, tuple[str, ...]] = {
    AgendaSource.OPERATOR: ("live:mirror_agenda_route",),
    AgendaSource.PROVIDER_WATCH: ("job:provider_probe",),
}


__all__ = [
    "DEFAULT_MAPPERS",
    "SOURCE_PRODUCERS",
    "map_operator",
    "map_provider_watch",
]
