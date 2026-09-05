"""Run one stage on its own, from wherever it was asked.

The thing cron did well was letting an operator fire one piece of work and
watch it. The pipeline kept that as `--stage` on the CLI, and for a long time
the CLI was the only caller: a stage could be run from a terminal on the
machine and from nowhere else. The Atlas room is the first surface to ask for
one, and the answer must not be a second way of running a stage.

So this is the one way. It answers the two questions any caller has to answer
before it can run something, in the same words for all of them:

* **is there a stage by that name**, from the registry, which is where the
  declaration lives.
* **can it run here**, from `Stage.needs_app` and the app it was handed. Six
  stages read the running backend and return "<key> unavailable" without it.
  A bare CLI process has no app, so those refuse there; the Mirror has one, so
  through the Mirror they run. That difference is a fact about the CALLER, and
  before this it was written down as a rule in the CLI.

**A stage run alone gets what it would have got in the row**, which is three
things and not one. The app it was handed, or letting it through the gate above
buys nothing: the body would resolve `None` and refuse one step later, more
expensively. The row's `config:` block for that stage, because a model stage
names its CHAIN there and one that ran without it would silently fall back to
its handler's default role, which is the drift that block's own comment exists
to stop. And the row as its billing entry, because a ceiling in `roles.yaml`
sits on the row's name: billed to the stage instead, a hand-fired model stage
would spend under a key with no cap on it.

It returns a result rather than raising, because a refusal is an answer a
person has to read and every surface has to render the same one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tesseract.scheduler.pipeline.registry import find_stage
from tesseract.scheduler.pipeline.runner import PipelineRunner

log = logging.getLogger(__name__)


def row_config(row_name: str) -> dict[str, Any]:
    """The schedule row's `config:` block, which is one sub-block per stage.

    Read here rather than passed in, because every caller of this function
    would otherwise have to know that a stage's chain lives in `schedule.yaml`.
    A schedule that cannot be read costs the stage its block and not its run:
    it falls back to its handler's own default, which is what happens today
    when a stage has no block at all.
    """
    from tesseract.paths import config_dir
    from tesseract.scheduler.config_loader import load_schedule_config

    try:
        schedule = load_schedule_config(config_dir())
    except Exception:  # noqa: BLE001 — a missing block is not a reason to refuse
        log.warning("run_stage: the schedule could not be read for %r", row_name)
        return {}
    for job in schedule.jobs:
        if job.name == row_name:
            return dict(job.config)
    return {}


@dataclass(frozen=True)
class StageRun:
    """What came of asking for one stage.

    `ran` is False when nothing was started: no stage by that name, or one that
    cannot run where it was asked. `reason` is readable in every case, because
    a caller that has to compose its own sentence is a second author of it.
    """

    stage: str
    ran: bool
    outcome: str
    reason: str
    #: Whether anything declares a stage by that name. A typo and a stage that
    #: cannot run in this process are both refusals, and a caller that scripts
    #: against them needs to tell them apart.
    found: bool = True
    duration_ms: float = 0.0

    @property
    def line(self) -> str:
        """One line, for a chat reply or a terminal. The same sentence
        wherever it is printed."""
        if not self.ran:
            return self.reason
        took = f" in {self.duration_ms / 1000:.1f}s" if self.duration_ms else ""
        return f"{self.stage}: {self.outcome}{took}" + (
            f". {self.reason}" if self.reason else ""
        )


def missing_keys(stage: Any, app: Any) -> tuple[str, ...]:
    """The keys this stage needs that the app it was handed does not carry.

    An app is anything with a `get`. `None` is a process with no backend at
    all, which is the CLI, and then everything declared is missing.
    """
    if not stage.needs_app:
        return ()
    if app is None or not hasattr(app, "get"):
        return tuple(stage.needs_app)
    return tuple(key for key in stage.needs_app if app.get(key) is None)


async def run_stage(
    name: str, *, app: Any = None, anchor: datetime | None = None
) -> StageRun:
    """Run the stage named `name`, or say why it was not run."""
    # Importing the stage modules is what puts the rows in the registry. A
    # caller that has not imported them would be told the stage does not
    # exist, which is the least useful true answer available.
    from tesseract.scheduler.pipeline import stages as _stages  # noqa: F401

    found = find_stage(name)
    if found is None:
        return StageRun(
            stage=name,
            ran=False,
            outcome="",
            found=False,
            reason=(
                f"nothing on this machine declares a step called {name!r}, so "
                "there is nothing to run"
            ),
        )
    owner, stage = found
    absent = missing_keys(stage, app)
    if absent:
        return StageRun(
            stage=name,
            ran=False,
            outcome="",
            reason=(
                f"{name} reads {', '.join(absent)} from the running app, and "
                "this process has no handle on it. Ask for it from the app "
                "itself, which has one"
            ),
        )

    runner = PipelineRunner(
        owner.stages,
        app=app,
        config=row_config(owner.name),
        external_reads=owner.external_reads,
        # The row, so what a model stage spends is checked against the ceiling
        # that row's name carries in `roles.yaml`.
        entry=owner.name,
    )
    row = await runner.run_one(name, anchor=anchor)
    return StageRun(
        stage=name,
        ran=True,
        outcome=row.outcome.value,
        reason=row.reason,
        duration_ms=row.duration_ms,
    )


__all__ = ["StageRun", "missing_keys", "row_config", "run_stage"]
