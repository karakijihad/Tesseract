"""WS handler stamp for ``paired_with_failure`` (codex audit-2 P1 #2
follow-on). The mapper expects this flag — until now it was always
absent, so repeat_switch was effectively dead. Handler now derives
the flag from live operational state.

Test surface targets the pure helper ``_derive_paired_with_failure``
so we can exercise every view without spinning up aiohttp.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.mirror.server.routes.operator_view import _derive_paired_with_failure


class _DictApp(dict):
    """Mirror app exposes ``.get(key)``; a plain dict mimics that."""


# -- recovery view ---------------------------------------------------------


def test_recovery_pairs_when_operator_attention_items_present() -> None:
    app = _DictApp({
        "last_recovery": {
            "operator_attention": [
                {"kind": "proposal", "id": "telegram_sender"},
                {"kind": "agenda", "id": "ag-test"},
            ],
        },
    })
    flag, summary = _derive_paired_with_failure(app, "recovery")
    assert flag is True
    assert "2 item(s) need operator" in summary


def test_recovery_does_not_pair_when_clean() -> None:
    app = _DictApp({"last_recovery": {"operator_attention": []}})
    flag, summary = _derive_paired_with_failure(app, "recovery")
    assert flag is False
    assert summary == ""


def test_recovery_does_not_pair_when_no_recovery_state() -> None:
    app = _DictApp({})
    flag, _ = _derive_paired_with_failure(app, "recovery")
    assert flag is False


# -- approvals view --------------------------------------------------------


class _FakeAgendaStore:
    def __init__(self, items: list) -> None:
        self._items = items

    def iter_active(self):
        return iter(self._items)


class _FakeItem:
    def __init__(self, status) -> None:
        self.status = status


def test_approvals_pairs_when_awaiting_operator_items_exist() -> None:
    from tesseract.orchestrator.autonomy.models import AgendaStatus
    store = _FakeAgendaStore([
        _FakeItem(AgendaStatus.AWAITING_OPERATOR),
        _FakeItem(AgendaStatus.RUNNING),
        _FakeItem(AgendaStatus.AWAITING_OPERATOR),
    ])
    app = _DictApp({"agenda_store": store})
    flag, summary = _derive_paired_with_failure(app, "approvals")
    assert flag is True
    assert "2 agenda" in summary


def test_approvals_no_pair_without_store() -> None:
    app = _DictApp({})
    flag, _ = _derive_paired_with_failure(app, "approvals")
    assert flag is False


# -- blocked view ----------------------------------------------------------


def test_blocked_pairs_when_blocked_items_exist() -> None:
    from tesseract.orchestrator.autonomy.models import AgendaStatus
    store = _FakeAgendaStore([
        _FakeItem(AgendaStatus.BLOCKED),
        _FakeItem(AgendaStatus.RUNNING),
    ])
    app = _DictApp({"agenda_store": store})
    flag, summary = _derive_paired_with_failure(app, "blocked")
    assert flag is True
    assert "blocked" in summary.lower()


class _FakePauseStore:
    def __init__(self, paused: dict) -> None:
        self._paused = paused

    def reload(self) -> dict:
        return dict(self._paused)


def test_blocked_pairs_when_governor_has_paused_sources() -> None:
    store = _FakeAgendaStore([_FakeItem(__import__(
        "tesseract.orchestrator.autonomy.models", fromlist=["AgendaStatus"]
    ).AgendaStatus.RUNNING)])
    pause = _FakePauseStore({"discovery_feed": True})
    app = _DictApp({"agenda_store": store, "pause_store": pause})
    flag, summary = _derive_paired_with_failure(app, "blocked")
    assert flag is True
    assert "paused" in summary.lower()


# -- workers view (uses real iter_active_status_summary on a tmp HOME) -----


def test_workers_pairs_when_failed_worker_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Isolate TESSERACT_HOME, write a single failed worker record, and
    confirm the derivation picks it up."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    from datetime import datetime, timezone
    from tesseract.orchestrator.workers.record import write_record, WorkerRecord, WorkerStatus, RiskClass
    from tesseract.orchestrator.workers.kinds import WorkerKind

    now = datetime.now(timezone.utc)
    write_record(WorkerRecord(
        id="wk-test-failed",
        kind=WorkerKind.TARS_SELF,
        created_at=now,
        updated_at=now,
        agenda_item_id="",
        risk_class=RiskClass.AUTONOMOUS,
        role="",
        prompt="x",
        status=WorkerStatus.FAILED,
    ))
    app = _DictApp({})
    flag, summary = _derive_paired_with_failure(app, "workers")
    assert flag is True
    assert "FAILED" in summary


def test_workers_no_pair_when_no_failed_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    app = _DictApp({})
    flag, _ = _derive_paired_with_failure(app, "workers")
    assert flag is False


# -- unknown / unhandled views never pair ----------------------------------


@pytest.mark.parametrize("view", ["chat", "brief", "pulse", "tars", "soul", "schedule"])
def test_unhandled_views_never_pair(view: str) -> None:
    app = _DictApp({})
    flag, _ = _derive_paired_with_failure(app, view)
    assert flag is False


# -- exception safety ------------------------------------------------------


def test_derivation_swallows_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Best-effort derivation must NOT raise — chat-turn telemetry must
    never crash the WS handler."""
    class _Boom:
        def iter_active(self):
            raise RuntimeError("simulated")
    app = _DictApp({"agenda_store": _Boom()})
    flag, summary = _derive_paired_with_failure(app, "approvals")
    assert flag is False
    assert summary == ""
