"""What is true right now, as opposed to what the runtime wrote down.

`sources.py` reads the record: logs, breakers, worker rows, `runs.jsonl`. Every
collector there is a read of something already on disk, which is why it is
deterministic and why it can run in a thread.

This is the other half. `orchestrator/diagnostics.py::collect_diagnosis` asks
nine questions of the machine itself — the GPU, Ollama, the disk, the audio
path — and it has existed, complete and never raising, with exactly one caller:
the `system_diagnose` tool. A person had to ask. So a failed GPU, a stopped
Ollama and a missing voice model were three things the runtime could describe
perfectly and never mentioned unprompted.

Kept out of `COLLECTORS` on purpose. That tuple's contract is "deterministic,
no model, no network", and a diagnosis talks to localhost and spawns
`nvidia-smi`. Rather than weaken the contract, the watchman runs both and
merges the reads.
"""

from __future__ import annotations

import logging
from datetime import datetime

from tesseract.lib.log_envelope import BAD, WARN
from tesseract.orchestrator.watchman.findings import (
    CURRENT_STATE,
    UNDECLARED,
    Finding,
    SourceRead,
)

log = logging.getLogger(__name__)

# `bad` is a defect: something is wrong now and the operator has to act.
# `warn` is a condition, reported without an evidence report.
_DEFECT = "bad"

# `unknown` is not reported at all. It is a smaller claim than a check that
# ran and disliked what it saw, and on a development checkout two of them
# (`hardware_profile`, `disk_app`) are permanent. They are still counted in
# `scanned`, so the record shows they were asked.
_REPORTED = ("bad", "warn")

# Three of the nine checks read a source the watchman already collects from,
# and reporting both would put one fault in the report twice under two names.
# Deciding that a finding is a consequence of another finding is stage 4's
# job; until it exists, the overlap is settled by whoever reads the source
# directly, which is the collector.
_ALREADY_COLLECTED = frozenset({"provider_health", "circuit_breakers", "scheduler"})


async def read_diagnostics(now: datetime) -> SourceRead:
    """The machine's own health, as one source read.

    Never raises, in keeping with every collector — a diagnosis that dies is
    reported as a source that could not be read, which is what `SourceRead`'s
    `error` field is for.
    """
    from tesseract.orchestrator.diagnostics import collect_diagnosis

    try:
        diagnosis = await collect_diagnosis()
    except Exception as exc:  # noqa: BLE001 — reported, never swallowed
        log.warning("watchman: the diagnosis could not be collected", exc_info=True)
        return SourceRead(name="diagnostics", present=True, error=f"{type(exc).__name__}: {exc}")

    findings = tuple(
        Finding(
            source="diagnostics",
            kind=f"check_{check.status}",
            # The check's own name. Every other producer in this tree sets a
            # `subject` so a repair or a remedy can be matched to the SAME
            # thing tick to tick; this one never did, so `disk_home` and
            # `ollama` both going bad in one sweep shared one identity
            # (`diagnostics/check_bad/`, empty subject) in the standing store,
            # the acknowledgement store and the room's own `department` key.
            # One of the two events was silently the only one ever reported,
            # and no repair declared against a check's name could ever be
            # found by `_handling_for`, which matches a repair by exact
            # subject. Filling it in changes neither `kind` nor `severity`,
            # and matches no remedy: `kind` here is always
            # `check_bad`/`check_warn`, never one of the four declared remedy
            # kinds, and a remedy is looked up by kind alone.
            #
            # It DOES make two repairs matchable that were not before, which
            # is the point and not a side effect. `repairs.py` declares
            # `disk_home` and `disk_runtime` against exactly the names
            # `diagnostics._check_disk` gives those checks, so `_handling_for`
            # now finds them and the room can say the runtime has that fault.
            # It could not say so while the subject was blank, even though the
            # repair ran. Any repair keyed on a check's name behaves the same
            # way, so check both files when adding one.
            by_boot=UNDECLARED,
            by_outage=CURRENT_STATE,
            subject=check.name,
            summary=f"{check.name} is {check.status}: {check.detail}",
            # A check's `detail` is free runtime text — absolute paths, an
            # exception's own words, a provider's reply. The operator's copy
            # keeps all of it; the model gets the two values this runtime
            # chose, which is the same split every other source keeps.
            model_summary=f"{check.name} is {check.status}",
            first_at=now,
            last_at=now,
            # A probe grades itself and this reads the grade rather than
            # re-deciding it: the check knows what its own numbers mean.
            severity=BAD if check.status == _DEFECT else WARN,
        )
        for check in diagnosis.checks
        if check.status in _REPORTED and check.name not in _ALREADY_COLLECTED
    )
    return SourceRead(
        name="diagnostics",
        present=True,
        scanned=len(diagnosis.checks),
        findings=findings,
    )


__all__ = ["read_diagnostics"]
