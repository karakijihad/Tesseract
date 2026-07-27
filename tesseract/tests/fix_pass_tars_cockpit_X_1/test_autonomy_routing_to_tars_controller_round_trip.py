"""X-1 — autonomy ``OPERATOR_GATE`` items reach framed-IPC dispatch.

The phase plan called this test ``...through the worker, end-to-end`` — that
phrasing reflected the audit's wording, but the actual autonomy path does not
instantiate ``TarsControllerWorker``. The kernel runner routes
``WorkerKind.TARS_CONTROLLER`` through the ``delegate_tars_controller`` tool
(``KernelWorkerRunner._route_for_kind``), which calls
``dispatch_to_controller`` — already framed-IPC since the 2026-05-27 migration.

Either way the operator-visible invariant is the same: **no autonomy code path
that resolves to ``TARS_CONTROLLER`` may reach the controller daemon via raw
newline-JSON / ``readline`` IPC anymore.** This test pins both reachable
entry points:

Autonomy → ``DelegateTarsControllerTool`` → ``dispatch_to_controller``
(the kernel-runner branch). Confirmed via ``_route_for_kind`` + the tool's
underlying dispatch import.

If this path later drifts back to raw IPC, exactly one assertion below
will fail — fast diagnosis without scraping logs.

(The mission-orchestrator branch this test formerly also pinned —
``TarsControllerWorker`` → ``run_controller_step`` — was deleted with the
mission engine; tracking is lanes + agenda + activity now.)
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone

from tesseract.orchestrator.autonomy.kernel_worker_runner import (
    _route_for_kind,
)
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.record import RiskClass, WorkerRecord


def _operator_gate_record(prompt: str = "drive controller", summary: str = "title") -> WorkerRecord:
    now = datetime.now(timezone.utc)
    return WorkerRecord(
        id="wk-x1",
        kind=WorkerKind.TARS_CONTROLLER,
        created_at=now,
        updated_at=now,
        agenda_item_id="ag-x1",
        risk_class=RiskClass.OPERATOR_GATE,
        role="",
        prompt=prompt,
        summary=summary,
    )


def test_autonomy_route_for_tars_controller_targets_delegate_tool() -> None:
    """Autonomy never touches ``TarsControllerWorker`` directly — the runner
    routes ``TARS_CONTROLLER`` through the ``delegate_tars_controller`` tool
    (whose dispatch is framed IPC). Pinning the contract here keeps a future
    refactor from re-introducing a worker-side IPC dance."""
    record = _operator_gate_record(prompt="ship the migration", summary="X-1 ship")
    tool_name, args = _route_for_kind(record)
    assert tool_name == "delegate_tars_controller"
    assert isinstance(args, dict)
    assert args["task"] == "ship the migration"
    assert args["title"] == "X-1 ship"


def test_delegate_tars_controller_imports_framed_dispatch() -> None:
    """The kernel-runner branch reaches ``dispatch_to_controller`` — the same
    shared primitive the dispatcher / TUI client share.
    A raw IPC regression in the delegate tool would break this import path."""
    from tesseract.kernel.tools import delegate_tars_controller as delegate_mod
    from tesseract.orchestrator.tars_controller.dispatcher import dispatch_to_controller

    assert delegate_mod.dispatch_to_controller is dispatch_to_controller
    source = inspect.getsource(delegate_mod)
    # No raw newline-JSON dance on the autonomy side either.
    assert "readline" not in source
