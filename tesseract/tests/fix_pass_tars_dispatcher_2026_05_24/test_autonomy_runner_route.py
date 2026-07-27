"""``KernelWorkerRunner._route_for_kind`` — TARS_CONTROLLER branch.

The audit-2 C-1 revert (held back dynamic kernel routing because the
runner returned ``unsupported_kind``) is REVERSED as of 2026-05-24
because the runner now routes ``TARS_CONTROLLER`` through the
``delegate_tars_controller`` tool. These tests pin both directions:

* ``_kind_for_item`` selects ``TARS_CONTROLLER`` when the probe is
  alive (dynamic routing back on).
* ``_route_for_kind`` returns ``("delegate_tars_controller", {...})``
  with ``task`` propagated from the worker record's prompt.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tesseract.orchestrator.autonomy.kernel import _kind_for_item
from tesseract.orchestrator.autonomy.kernel_worker_runner import (
    _route_for_kind,
)
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
)
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.record import RiskClass, WorkerRecord


def _agenda(risk: RiskClass) -> AgendaItem:
    now = datetime.now(timezone.utc)
    return AgendaItem(
        id="ag-route-1",
        created_at=now,
        updated_at=now,
        source=AgendaSource.OPERATOR,
        goal="rewrite the broken middleware",
        risk_class=risk,
    )


def _record(
    *, kind: WorkerKind, prompt: str, summary: str | None = None
) -> WorkerRecord:
    now = datetime.now(timezone.utc)
    return WorkerRecord(
        id="wk-route-1",
        kind=kind,
        created_at=now,
        updated_at=now,
        agenda_item_id="ag-route-1",
        risk_class=RiskClass.OPERATOR_GATE,
        role="",
        prompt=prompt,
        summary=summary or "",
    )


def test_kind_for_item_selects_controller_when_probe_alive(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tesseract.orchestrator.tars_controller as ctrl_pkg

    monkeypatch.setattr(
        ctrl_pkg, "controller_port_alive", lambda timeout=0.5: True
    )
    assert (
        _kind_for_item(_agenda(RiskClass.OPERATOR_GATE))
        is WorkerKind.TARS_CONTROLLER
    )


def test_kind_for_item_falls_back_to_claude_when_probe_dead(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tesseract.orchestrator.tars_controller as ctrl_pkg

    monkeypatch.setattr(
        ctrl_pkg, "controller_port_alive", lambda timeout=0.5: False
    )
    assert (
        _kind_for_item(_agenda(RiskClass.OPERATOR_GATE))
        is WorkerKind.CLAUDE_CLI
    )


def test_route_for_kind_tars_controller_dispatches_to_delegate_tool(
    isolated_home: Path,
) -> None:
    record = _record(
        kind=WorkerKind.TARS_CONTROLLER,
        prompt="patch the auth middleware",
        summary="auth-middleware-fix",
    )
    tool_name, args = _route_for_kind(record)
    assert tool_name == "delegate_tars_controller"
    assert isinstance(args, dict)
    assert args["task"] == "patch the auth middleware"
    assert args["title"] == "auth-middleware-fix"


def test_route_for_kind_tars_controller_empty_summary_yields_none_title(
    isolated_home: Path,
) -> None:
    record = _record(
        kind=WorkerKind.TARS_CONTROLLER,
        prompt="some work",
        summary="",
    )
    _tool, args = _route_for_kind(record)
    assert args["title"] is None


def test_route_for_kind_other_kinds_unchanged(isolated_home: Path) -> None:
    # CLAUDE_CLI still routes through delegate_claude — restoring the
    # TARS_CONTROLLER branch did not regress the other lanes.
    record = _record(kind=WorkerKind.CLAUDE_CLI, prompt="hi")
    tool_name, args = _route_for_kind(record)
    assert tool_name == "delegate_claude"
    assert args["task"] == "hi"
