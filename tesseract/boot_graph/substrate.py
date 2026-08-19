"""What one substrate declares about itself.

These are facts about the code, not preferences an operator sets. Whether a
preparation holds the interpreter, what the runtime does when it was never
prepared, and whether this machine has any use for it are all answerable only
where the substrate is implemented — so they live here and `boot.yaml` cannot
contradict them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable


@dataclass(frozen=True)
class Substrate:
    """One thing the runtime prepares at boot.

    `prepare` is dispatched on its own shape, and there are three of them: a
    coroutine function is awaited on the loop, a plain callable goes through
    `asyncio.to_thread`, and a plain callable that RETURNS a coroutine is both
    — threaded, then awaited on the loop. The third is what
    `build_substrate_registry` writes (`lambda: _prepare_x(app)`), and dropping
    its return value meant a whole boot prepared nothing. Nothing in YAML says
    which, because Python already holds that fact.

    `holds_gil` is asserted by the author and checked by review — there is no
    runtime probe for it. Claiming `False` wrongly is a review-blocking defect
    with a measurable symptom: loop lag after the window opens.

    `degrade` is the cold behaviour in prose, and it is a claim that has to be
    TRUE. A substrate below the warm line with an empty one is a boot error;
    one with a false one is worse than sitting above the line, because it turns
    an honest wait into a silent breakage.

    `requires` answers "is this worth preparing on THIS machine" and is shaped
    like the authorities it routes to: `None` to prepare, a human reason to
    skip. `brain/boot.py::adapter_unavailable_reason` already has exactly this
    shape, so a provider substrate's `requires` IS that call. A skip is normal
    and is logged at INFO with the reason; it is never a failure.

    `reload_on` names the reload targets that prepare this substrate a SECOND
    time. Empty is the default and the safe answer: prepared once at boot and
    never again, which is what a live worker with in-flight state needs. A
    substrate re-prepared by a reload has to say so.
    """

    id: str
    prepare: Callable[[], Any]
    holds_gil: bool
    degrade: str
    requires: Callable[[], str | None] = lambda: None
    reload_on: frozenset[str] = frozenset()


class SubstrateRegistry:
    """Every substrate the runtime knows how to prepare, by id.

    Built per-process rather than as a module global: the substrates close over
    the application they prepare, and two of those exist (the Mirror backend
    and the controller daemon).
    """

    def __init__(self) -> None:
        self._by_id: dict[str, Substrate] = {}

    def register(self, substrate: Substrate) -> Substrate:
        if substrate.id in self._by_id:
            raise ValueError(
                f"substrate '{substrate.id}' is already registered — "
                f"one id, one preparation"
            )
        self._by_id[substrate.id] = substrate
        return substrate

    def add(
        self,
        id: str,
        prepare: Callable[[], Any],
        *,
        holds_gil: bool,
        degrade: str,
        requires: Callable[[], str | None] = lambda: None,
        reload_on: Iterable[str] = (),
    ) -> Substrate:
        """Register from parts. Every field is keyword-only past `prepare` so a
        registration reads as the declaration it is."""
        return self.register(
            Substrate(
                id=id,
                prepare=prepare,
                holds_gil=holds_gil,
                degrade=degrade,
                requires=requires,
                reload_on=frozenset(reload_on),
            )
        )

    def get(self, id: str) -> Substrate:
        if id not in self._by_id:
            raise KeyError(f"no substrate registered as '{id}'")
        return self._by_id[id]

    def ids(self) -> frozenset[str]:
        return frozenset(self._by_id)

    def __contains__(self, id: object) -> bool:
        return id in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)

    def __iter__(self):
        return iter(self._by_id.values())
