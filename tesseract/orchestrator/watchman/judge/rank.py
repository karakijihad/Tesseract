"""Stage 5 — needs the operator, or worth knowing.

Two tiers, because there are two questions and the report was answering them
with one number. The header counted `swept.defects`; the narration was built
from ALL findings; the bullets counted defects again. Three different sets in
one message, and nothing saying which was which — so *"1 thing went wrong"*
arrived above a paragraph about two things.

**The header, the narration and the bullets describe the same set after this
stage.** Which set is a choice, and the choice is `needs_you`: the message
exists to make a person act, and a message whose first line counts things they
cannot act on trains them to skip the first line.

**This stage is now a sort, not an inference.** It used to read a boolean and
then correct it against a hand-written list of four kinds that were
"technically defects but not really" — a restart count, a sleep window, a
governor pause, a recovery. That list existed because a boolean cannot say
*degraded*, so anything short of broken had to be special-cased back out by
name. Every finding declares `info`, `warn` or `bad` at its collector now, so
the list is gone and the rule is one line: bad and warn need you, info is the
runtime describing itself.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from tesseract.lib.log_envelope import BAD, WARN

if TYPE_CHECKING:
    from tesseract.orchestrator.watchman.judge import Verdict

STAGE = "rank"

# What a person is being asked to look at. `info` is not a lesser fault, it is
# the runtime describing itself — restarts inside their normal range, a
# governor pause, an outage while the machine slept, a recovery — and it
# belongs in the artifact on disk, under its own heading, where someone reads
# it when they are already looking.
_ACTIONABLE = frozenset({BAD, WARN})


def apply(verdicts: list["Verdict"], *, now: datetime) -> list["Verdict"]:
    from tesseract.orchestrator.watchman.judge import (
        NEEDS_YOU,
        WORTH_KNOWING,
        Verdict,
    )

    out: list[Verdict] = []
    for verdict in verdicts:
        if not verdict.kept:
            out.append(verdict)
            continue
        tier = (
            NEEDS_YOU if verdict.finding.severity in _ACTIONABLE else WORTH_KNOWING
        )
        out.append(Verdict(
            finding=verdict.finding,
            # The stage that last acted on it is kept: ranking is not a
            # decision about whether a finding survives, and overwriting
            # `attribute` with `rank` would lose the reason it was recast.
            stage=verdict.stage,
            action=verdict.action,
            why=verdict.why,
            tier=tier,
        ))
    return out


__all__ = ["STAGE", "apply"]
