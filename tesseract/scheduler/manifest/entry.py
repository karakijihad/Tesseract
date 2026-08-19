"""What something that runs on its own has to say about itself.

Four layers fired independently before this: the schedule rows, the engine's own
ticks, the autonomy kernel, and a set of in-process loops that appeared in no
registry and no config file. They are one declared set here — four `Runs` kinds,
not four registries — and the shape of a declaration is the one IS-1 put on
tools, a level up: a name, a sentence, and what it costs.

**The prose is written for the operator.** `summary` is what it does and `why`
is what would be lost if it stopped, in the language an agent card uses rather
than the language a maintainer would. The floor plan renders both verbatim, so
a field left thin here is a thin card there — which is why both have a floor
and a blank one raises.

**Facts here, cadence in config.** `boot.yaml` draws the same line for the boot
graph: the file owns the shape, the substrate's own code owns the claims about
it. WHEN a row fires is the operator's business and lives in `schedule.yaml`;
what it is for is not theirs to edit and lives here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

# A chain this entry does not name, because the work it runs names one instead:
# `provider_probe` walks whatever roles are active, and the kernel's chain is
# whichever the dispatched worker asks for. Declaring a literal list for either
# would be a claim that goes stale the next time a role moves.
DISPATCHED = "*"

# Floors, not style. `why` is the field that makes "every automated thing gets
# decided, not inherited" checkable, and "needed" satisfies a non-blank test
# while answering nothing.
MIN_SUMMARY_CHARS = 20
MIN_WHY_CHARS = 30

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_SITE = re.compile(r"^tesseract/[\w/]+\.py:\w+$")


class ManifestError(ValueError):
    """A declaration that does not hold. Raised at import of the registry."""


class Runs(str, Enum):
    """How an entry comes to fire."""

    ROW = "row"                # a `schedule.yaml` row: the anchor, or an interval
    SERVICE = "service"        # runs continuously while the app does
    TRIGGER = "trigger"        # a named event fires it
    ON_DEMAND = "on_demand"    # the operator or the assistant arms it


class Kind(str, Enum):
    """What a run costs.

    Three values, not two: a free local embedding pass and a billed remote call
    were the same word before this, and showing the difference is the whole
    reason the floor plan exists.
    """

    DETERMINISTIC = "deterministic"
    LOCAL_MODEL = "local_model"
    REMOTE_MODEL = "remote_model"


class Owner(str, Enum):
    """Which of the three things this serves."""

    HOME = "home"          # the library — memory, vault, skills, agents
    RUNTIME = "runtime"    # housekeeping — logs, processes, health
    DELIVERY = "delivery"  # it reaches the operator


@dataclass(frozen=True)
class Entry:
    """One thing that runs on its own, and its account of itself."""

    name: str
    runs: Runs
    summary: str
    why: str
    kind: Kind
    owner: Owner
    # The chains in `roles.yaml::chains` this entry's calls ride, or
    # `(DISPATCHED,)` when the chain belongs to the work rather than to the
    # entry. Empty for deterministic entries and required for the rest.
    chains: tuple[str, ...] = ()
    # Where the loop is, as `tesseract/path/to.py:function`. Required of a
    # service: a service is a loop, and a loop nobody can point at is how six
    # of these came to run without appearing anywhere. A row is found through
    # `schedule.yaml` and needs no site.
    site: str = ""
    # The most this entry may spend in a day, in USD. The ledger reads it at
    # load AND at every reload, which is the reason a ceiling has to live here
    # rather than beside the work: caps are rebuilt from config on every
    # reload, so one registered anywhere else survives until the next time an
    # operator saves a yaml file. `None` means no ceiling of its own — the
    # global cap still applies.
    daily_budget_usd: float | None = None
    # The `boot.yaml` substrate that starts it, when one does. Optional
    # deliberately: a per-session or per-pane loop is started by the session,
    # not by the boot graph, and naming a substrate for it would be a claim
    # about where to look that is wrong.
    substrate: str = ""

    def __post_init__(self) -> None:
        if not _NAME.match(self.name or ""):
            raise ManifestError(
                f"manifest entry name {self.name!r} must be a lowercase slug — it is "
                "the id the ledger bills to and the view keys on"
            )
        for field, text, floor in (
            ("summary", self.summary, MIN_SUMMARY_CHARS),
            ("why", self.why, MIN_WHY_CHARS),
        ):
            if len(text.strip()) < floor:
                raise ManifestError(
                    f"manifest entry {self.name!r}: {field} is {len(text.strip())} "
                    f"characters and the floor is {floor}. Say what it does and what "
                    "would be lost without it — the operator reads this, not the code"
                )
        if self.summary.strip() == self.why.strip():
            raise ManifestError(
                f"manifest entry {self.name!r}: why restates summary. What it does and "
                "what would be lost if it stopped are different questions"
            )
        if self.kind is Kind.DETERMINISTIC and self.chains:
            raise ManifestError(
                f"manifest entry {self.name!r} is deterministic and names "
                f"{list(self.chains)} — a chain it never calls is a cost the floor "
                "plan would show it paying"
            )
        if self.kind is not Kind.DETERMINISTIC and not self.chains:
            raise ManifestError(
                f"manifest entry {self.name!r} is {self.kind.value} and names no "
                f"chain. Name the chains it rides, or {DISPATCHED!r} when the work "
                "it runs chooses one"
            )

        if self.daily_budget_usd is not None:
            if self.kind is Kind.DETERMINISTIC:
                raise ManifestError(
                    f"manifest entry {self.name!r} is deterministic and carries a "
                    "daily budget — nothing it does is billed"
                )
            if self.daily_budget_usd <= 0:
                raise ManifestError(
                    f"manifest entry {self.name!r} has a budget of "
                    f"{self.daily_budget_usd} — to stop it spending, disable it "
                    "rather than leaving it armed with a ceiling it cannot clear"
                )
        if self.runs is Runs.SERVICE and not _SITE.match(self.site or ""):
            raise ManifestError(
                f"manifest entry {self.name!r} is a service and names no site. Give "
                "it as `tesseract/path/to.py:function` — the loop has to be one "
                "somebody can open"
            )
        if self.runs is not Runs.SERVICE and self.site:
            raise ManifestError(
                f"manifest entry {self.name!r} is a {self.runs.value} and names a "
                "site; only a service is a loop"
            )

    @property
    def free(self) -> bool:
        """Costs nothing at use-time. `local_model` needs Ollama and is free."""
        return self.kind is not Kind.REMOTE_MODEL


__all__ = [
    "DISPATCHED",
    "MIN_SUMMARY_CHARS",
    "MIN_WHY_CHARS",
    "Entry",
    "Kind",
    "ManifestError",
    "Owner",
    "Runs",
]
