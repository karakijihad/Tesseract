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

from typing import Any

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


#: Live sources that write straight to the store. They have no mapper and no
#: entry above, deliberately: the boot check fails a source given neither, and
#: `models.py` says why for each. Named here because "can anything file under
#: this again" is not answerable from the mapper table alone, and reading a
#: live source as dead offers the operator `remove` on something still running.
WRITES_ITS_OWN = frozenset(
    {AgendaSource.RECOVERY, AgendaSource.FOLLOW_UP, AgendaSource.TASK}
)


def can_still_fire(source: AgendaSource, kernel: Any = None) -> bool:
    """Whether anything can file work under this source again.

    Asked by the panel, because a pause on a source whose producer was deleted
    is a record and not a thing to resume: offering `unpause` on one is a verb
    that names a mechanism and does nothing, and the panel held exactly that
    for three weeks. `TASK` is live and deliberately has no mapper, which is
    why this is a function rather than a membership test on the table above.

    **Two questions, and the operator answers the second.** A source can have
    a producer in the code and still be switched off in
    `config/agenda-mappers.yaml`, whose own header says setting `enabled:
    false` is how you stop a source generating items. A panel that read only
    the table above would offer `unpause` on a source the operator has turned
    off, which restarts nothing.

    **Ask the kernel, because the kernel is what decides.** It reads that file
    once, at construction, and nothing reloads it, so a reader that re-read the
    file would be current with disk and wrong about the running process: it
    would offer to resume a source the kernel still refuses, right up until the
    next restart. `kernel` is optional only because a reader can run with no
    kernel at all, and then the file is the only answer there is and there is
    nothing for it to disagree with.
    """
    if source in WRITES_ITS_OWN:
        return True
    if source not in SOURCE_PRODUCERS:
        return False
    if kernel is not None:
        try:
            return bool(kernel.is_source_enabled(source))
        except Exception:  # noqa: BLE001 — a reading that failed decides nothing
            return True
    return _is_switched_on(source)


def _is_switched_on(source: AgendaSource) -> bool:
    """What the file says, for a caller with no kernel to ask.

    The same reading `kernel.py::_is_enabled` makes, including its answer for a
    source with no entry at all: only the operator's own items fire without
    one, because those are typed by a person and are never noise.
    """
    from tesseract import paths
    from tesseract.orchestrator.autonomy.kernel import load_mapper_configs

    try:
        path = paths.config_dir() / "agenda-mappers.yaml"
        if not path.exists():
            # No file is not an answer. It means this process is looking
            # somewhere that has no config yet, not that the operator has
            # switched everything off, and reading it as the second would
            # replace every resume button on the panel with a remove button.
            return True
        configs = load_mapper_configs(path)
    except Exception:  # noqa: BLE001 — a read that failed decides nothing
        return True
    cfg = configs.get(source)
    if cfg is None:
        # The file exists and says nothing about this source, which is the
        # reading `kernel.py::_is_enabled` makes: only the operator's own
        # items fire without an entry, because those are typed by a person.
        return source is AgendaSource.OPERATOR
    return bool(cfg.enabled)


__all__ = [
    "DEFAULT_MAPPERS",
    "SOURCE_PRODUCERS",
    "WRITES_ITS_OWN",
    "can_still_fire",
    "map_operator",
    "map_provider_watch",
]
