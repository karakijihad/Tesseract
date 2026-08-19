from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tesseract.orchestrator.outcome import RunOutcome, outcome_from_ok

# The outcomes the scheduler must treat as a failed run.
_NOT_OK_OUTCOMES = frozenset(
    {RunOutcome.FAILED, RunOutcome.SKIPPED_UPSTREAM_FAILED}
)


# Who caused this run. A `runs.jsonl` record used to carry no trigger at all,
# so a hand-fired job and a cron tick were byte-identical in shape — which is
# how a run at 08:50 against a 22:30 cadence got diagnosed as a scheduler bug
# that did not exist. `manual` is deliberately NOT one of these: two different
# actors fire jobs by hand (the operator's Run-now button and the assistant's
# `schedule_run` tool) and telling those apart is the whole question.
TRIGGER_SOURCES = frozenset(
    {
        "scheduled",  # the cron tick matched
        "catchup",    # a tick was missed while the process was down
        "operator",   # a human pressed Run now / typed the command
        "assistant",  # the assistant called `schedule_run`
        "alarm",      # a one-shot or recurring alarm came due
        "event",      # a `when:` condition on the row said the thing happened
    }
)


@dataclass(frozen=True)
class JobContext:
    job_name: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    fired_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    app: Any = field(default=None, compare=False, repr=False)
    config: dict[str, Any] = field(default_factory=dict)
    # Scheduler's own log directory (runs.jsonl lives here). Injected by the
    # engine so jobs that consume `runs.jsonl` (e.g. DailyWriterJob) read
    # from the engine's own log_dir rather than a hardcoded repo path.
    log_dir: Path | None = field(default=None, compare=False, repr=False)
    # Operator's per-job override of the cognitive role for this LLM call.
    # `None` means "use the handler's `default_model_role`". Only set on
    # handlers with `uses_llm = True`; ignored otherwise. Carrying it on
    # JobContext (rather than smuggling through `config`) keeps the inner
    # `config:` block strictly handler-private and makes the override
    # visible in tests via direct attribute access.
    model_role: str | None = field(default=None, compare=False)
    # The chain this run should ride, when the work names one instead of a
    # role. Roles are pillars — `chat_brain`, the seats, the defaults — and a
    # background job that needed none of them used to get a role invented for
    # it purely to hold a budget line. Naming the chain directly is what
    # removes the need for that invention. `model_role` still wins when both
    # are set: an operator's per-row override is an answer about which role,
    # and answering it with a chain would ignore them.
    model_chain: str | None = field(default=None, compare=False)
    # What the ledger bills this run to. Empty means the role, which is the
    # historical behaviour and still right for a job on a pillar. A run that
    # names a chain has no role to bill, so it bills to the manifest entry it
    # belongs to — spend attributed to the work rather than to a role that
    # exists to hold it.
    billing_key: str = field(default="", compare=False)
    # Shared CostLedger singleton (``app["cost_ledger"]``), threaded so LLM-using
    # jobs bill spend and preflight the daily cap via ``MeteredAdapter`` (built
    # in ``role_chain.build_chain_for_role``). None for tests / ledger-disabled
    # boots — metering becomes a no-op. (2026-06-28 cost-ledger gap.)
    cost_ledger: Any = field(default=None, compare=False, repr=False)
    # One of TRIGGER_SOURCES, written into the run record by `scheduler/log.py`.
    # Defaults to `scheduled` so a context built outside the engine still
    # produces a valid record; the engine and the alarm runner always set it.
    trigger_source: str = "scheduled"


@dataclass(frozen=True)
class JobResult:
    job_name: str
    run_id: str
    ok: bool
    detail: str = ""
    payload: dict = field(default_factory=dict)
    duration_ms: float = 0.0
    # What came of the run, from the closed vocabulary in
    # `orchestrator/outcome.py`. `ok` cannot tell a job that found nothing to
    # do apart from one that produced its output, and cannot tell a refusal
    # apart from a crash — so a job that knows which it was says so here.
    # Left unset, it widens `ok` and nothing changes for the ~120 call sites
    # that have not declared one yet.
    outcome: RunOutcome | None = None
    # Plain language, for any outcome that is not `succeeded`. Rendered to the
    # operator as-is, so write it for them and not for a log grep.
    outcome_reason: str = ""

    def __post_init__(self) -> None:
        if self.outcome is None:
            # The compatibility boundary. A caller that only set `ok` gets its
            # outcome widened, and a failure that carries no reason of its own
            # borrows `detail` — which every such call site does fill. The
            # contract is a reason for every non-succeeded state; refusing the
            # ~120 legacy sites outright would just crash jobs that already say
            # what went wrong, in the wrong field.
            derived = outcome_from_ok(self.ok)
            object.__setattr__(self, "outcome", derived)
            if derived is not RunOutcome.SUCCEEDED and not self.outcome_reason.strip():
                object.__setattr__(
                    self,
                    "outcome_reason",
                    self.detail.strip() or "the job failed without saying why",
                )
            return
        # A declared outcome wins, and `ok` is made to agree with it. Two
        # fields that can disagree about the same run is the drift this
        # vocabulary exists to remove — and the engine keys retries, the
        # consecutive-failure breaker and the activity chip off `ok`, so a
        # job that says it failed must not also report OK to any of them.
        # `refused`, `degraded` and `truncated` stay OK: a paused source or a
        # partial result is not a fault to retry.
        object.__setattr__(self, "ok", self.outcome not in _NOT_OK_OUTCOMES)
        if self.outcome is not RunOutcome.SUCCEEDED and not self.outcome_reason.strip():
            # The contract is a reason for every non-succeeded state, and an
            # unexplained refusal on a health surface is the same dead end as
            # the empty success this vocabulary replaced. Raised at
            # construction, inside the handler's own try, so a job that forgets
            # one fails loudly in a test rather than quietly in a log.
            raise ValueError(
                f"{self.job_name}: outcome {self.outcome.value} needs an "
                "outcome_reason a person can read"
            )
