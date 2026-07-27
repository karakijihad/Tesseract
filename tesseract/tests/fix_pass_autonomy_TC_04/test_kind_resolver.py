"""``_kind_for_item`` dynamic OPERATOR_GATE routing."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tesseract.orchestrator.autonomy.kernel import _kind_for_item
from tesseract.orchestrator.autonomy.models import AgendaItem, AgendaSource
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.tars_controller import reset_port_alive_cache
from tesseract.orchestrator.workers.record import RiskClass


@pytest.fixture(autouse=True)
def _flush_probe_cache() -> None:
    """The probe TTL-caches by port-file path; flush between tests so a
    monkeypatched `controller_port_alive` is honored on the next call."""
    reset_port_alive_cache()
    yield
    reset_port_alive_cache()


def _item(risk: RiskClass) -> AgendaItem:
    now = datetime.now(timezone.utc)
    return AgendaItem(
        id="ag-1",
        created_at=now,
        updated_at=now,
        source=AgendaSource.OPERATOR,
        goal="test",
        risk_class=risk,
    )


def test_propose_keeps_markdown_agent(isolated_home: Path) -> None:
    assert _kind_for_item(_item(RiskClass.PROPOSE)) is WorkerKind.MARKDOWN_AGENT


def test_autonomous_keeps_tars_self(isolated_home: Path) -> None:
    assert _kind_for_item(_item(RiskClass.AUTONOMOUS)) is WorkerKind.TARS_SELF


def test_operator_gate_routes_to_claude_when_no_controller(
    isolated_home: Path,
) -> None:
    """No controller.port on disk → fall back to CLAUDE_CLI."""
    assert _kind_for_item(_item(RiskClass.OPERATOR_GATE)) is WorkerKind.CLAUDE_CLI


def test_operator_gate_routes_to_controller_when_alive(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-05-24 — dynamic routing restored now that the runner has a
    real ``delegate_tars_controller`` dispatch path. Audit-2 C-1's
    conservative revert is reversed because ``_route_for_kind`` now
    routes ``TARS_CONTROLLER`` instead of returning
    ``unsupported_kind``.
    """
    import tesseract.orchestrator.tars_controller as ctrl_pkg

    monkeypatch.setattr(
        ctrl_pkg, "controller_port_alive", lambda timeout=0.5: True
    )
    assert (
        _kind_for_item(_item(RiskClass.OPERATOR_GATE))
        is WorkerKind.TARS_CONTROLLER
    )


def test_operator_gate_falls_back_when_probe_fails(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tesseract.orchestrator.tars_controller as ctrl_pkg

    monkeypatch.setattr(
        ctrl_pkg, "controller_port_alive", lambda timeout=0.5: False
    )
    assert (
        _kind_for_item(_item(RiskClass.OPERATOR_GATE)) is WorkerKind.CLAUDE_CLI
    )


def test_port_alive_probe_is_ttl_cached(isolated_home: Path) -> None:
    """One synchronous TCP connect per cache window — keeps the event
    loop unblocked when the kernel admits multiple OPERATOR_GATE items
    in a single tick."""
    from tesseract.orchestrator.tars_controller.port_probe import (
        _PORT_ALIVE_CACHE,
        controller_port_alive,
        reset_port_alive_cache,
    )

    reset_port_alive_cache()
    calls = {"n": 0}

    def _fake_probe(_timeout: float) -> bool:
        calls["n"] += 1
        return False

    import tesseract.orchestrator.tars_controller.port_probe as port_probe_mod

    port_probe_mod._do_port_probe = _fake_probe  # noqa: SLF001 — test injection
    try:
        controller_port_alive()
        controller_port_alive()
        controller_port_alive()
    finally:
        # The cache key is the port-file path string; the first probe
        # populated it. Confirm a single underlying call.
        assert calls["n"] == 1
        assert _PORT_ALIVE_CACHE  # populated
        reset_port_alive_cache()
