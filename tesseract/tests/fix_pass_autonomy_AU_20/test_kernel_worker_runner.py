"""AU-20 follow-up — KernelWorkerRunner replaces _NoopRunner.

Covers per-WorkerKind routing, terminal-state writeback, error
handling, and graceful FAILED for deferred kinds.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tesseract.kernel.tools.base import ToolContext, ToolResult
from tesseract.orchestrator.autonomy.kernel_worker_runner import (
    KernelWorkerRunner,
    _route_for_kind,
)
from tesseract.orchestrator.autonomy.models import RiskClass
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.record import (
    Billing,
    WorkerRecord,
    WorkerStatus,
    load_record,
    mint_worker_id,
    write_record,
)


# ── Fakes ───────────────────────────────────────────────────────────


class _FakeRegistry:
    """Stand-in for ToolRegistry; execute_tool delegates here via the
    monkeypatch below."""

    def __init__(
        self,
        output: str = "ok",
        is_error: bool = False,
        denied: bool = False,
        raise_exc: Exception | None = None,
        timed_out: bool = False,
    ):
        self.output = output
        self.is_error = is_error
        self.denied = denied
        self.raise_exc = raise_exc
        self.timed_out = timed_out
        self.calls: list[tuple[str, dict[str, Any]]] = []


async def _fake_execute_tool(registry, tool_name, tool_input, context, ask_fn=None, policy=None):
    registry.calls.append((tool_name, tool_input))
    if registry.raise_exc is not None:
        raise registry.raise_exc
    return ToolResult(
        output=registry.output,
        is_error=registry.is_error,
        denied_hard=registry.denied,
        deny_reason="ASK denied" if registry.denied else "",
        timed_out=registry.timed_out,
    )


@pytest.fixture
def patch_execute(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.kernel_worker_runner.execute_tool",
        _fake_execute_tool,
    )


def _make_record(
    kind: WorkerKind,
    *,
    prompt: str = "do the thing",
    role: str = "agents_default",
    status: WorkerStatus = WorkerStatus.RUNNING,
) -> WorkerRecord:
    now = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
    return WorkerRecord(
        id=mint_worker_id(kind, now=now),
        kind=kind,
        created_at=now,
        updated_at=now,
        agenda_item_id="ag-2026-05-19-1200-test",
        risk_class=RiskClass.AUTONOMOUS,
        role=role,
        prompt=prompt,
        status=status,
    )


# ── Routing ─────────────────────────────────────────────────────────


def test_route_claude_cli() -> None:
    record = _make_record(WorkerKind.CLAUDE_CLI, prompt="audit module X")
    tool_name, args = _route_for_kind(record)
    assert tool_name == "delegate_claude"
    assert args == {"task": "audit module X", "background": False}


def test_route_codex_cli() -> None:
    record = _make_record(WorkerKind.CODEX_CLI, prompt="review pr 42")
    tool_name, args = _route_for_kind(record)
    assert tool_name == "delegate_codex"
    assert args == {"task": "review pr 42", "background": False}


def test_route_markdown_agent_requires_role() -> None:
    record = _make_record(WorkerKind.MARKDOWN_AGENT, role="vault_librarian", prompt="summarise vault")
    tool_name, args = _route_for_kind(record)
    assert tool_name == "invoke_agent"
    assert args == {"name": "vault_librarian", "task": "summarise vault", "background": False}


def test_route_markdown_agent_empty_role_uses_default_slug() -> None:
    """Updated 2026-05-19 per codex audit P0 #1 — empty role now falls
    back to ``DEFAULT_MARKDOWN_AGENT`` instead of failing dispatch.
    The prior 'unsupported' contract was the source of the audit's
    15-of-16-workers-failing pattern."""
    from tesseract.orchestrator.autonomy.kernel_worker_runner import DEFAULT_MARKDOWN_AGENT

    record = _make_record(WorkerKind.MARKDOWN_AGENT, role="", prompt="summarise vault")
    tool_name, args = _route_for_kind(record)
    assert tool_name == "invoke_agent"
    assert args["name"] == DEFAULT_MARKDOWN_AGENT
    assert args["task"] == "summarise vault"


def test_route_tars_self_with_role_pinned() -> None:
    record = _make_record(WorkerKind.TARS_SELF, role="research-brief", prompt="look up X")
    tool_name, args = _route_for_kind(record)
    assert tool_name == "invoke_agent"
    assert args == {"name": "research-brief", "task": "look up X", "background": False}


def test_route_tars_self_empty_role_uses_default() -> None:
    from tesseract.orchestrator.autonomy.kernel_worker_runner import DEFAULT_TARS_SELF_AGENT

    record = _make_record(WorkerKind.TARS_SELF, role="", prompt="reflect on activity")
    tool_name, args = _route_for_kind(record)
    assert tool_name == "invoke_agent"
    assert args["name"] == DEFAULT_TARS_SELF_AGENT
    assert args["task"] == "reflect on activity"


def test_route_terminal_unsupported() -> None:
    record = _make_record(WorkerKind.TERMINAL)
    tool_name, msg = _route_for_kind(record)
    assert tool_name is None


def test_route_empty_prompt_unsupported() -> None:
    record = _make_record(WorkerKind.CLAUDE_CLI, prompt="   ")
    tool_name, msg = _route_for_kind(record)
    assert tool_name is None
    assert "empty prompt" in msg


# ── End-to-end runner ───────────────────────────────────────────────


async def test_claude_cli_happy_path_writes_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch_execute) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    registry = _FakeRegistry(output="reviewed three files; no issues.")
    runner = KernelWorkerRunner(registry, workspace_root=tmp_path)
    record = _make_record(WorkerKind.CLAUDE_CLI, prompt="audit X")
    write_record(record)

    await runner.run(record)
    persisted = load_record(record.id)
    assert persisted is not None
    assert persisted.status == WorkerStatus.DONE
    assert "reviewed three files" in persisted.summary
    assert registry.calls == [("delegate_claude", {"task": "audit X", "background": False})]


async def test_spawning_record_transitions_to_running_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch_execute,
) -> None:
    """The kernel hands the runner records still in SPAWNING; the runner
    must flip them to RUNNING (reason ``runner_start``) before dispatch
    so recovery never sees a live worker stuck in a pre-run status."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    registry = _FakeRegistry(output="done")
    runner = KernelWorkerRunner(registry, workspace_root=tmp_path)
    record = _make_record(WorkerKind.CLAUDE_CLI, prompt="audit X", status=WorkerStatus.SPAWNING)
    write_record(record)

    await runner.run(record)
    persisted = load_record(record.id)
    assert persisted is not None
    assert persisted.status == WorkerStatus.DONE
    transitions = [(t.from_status, t.to_status, t.reason) for t in persisted.status_history]
    assert ("spawning", "running", "runner_start") in transitions


async def test_tool_error_writes_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch_execute) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    registry = _FakeRegistry(output="something broke", is_error=True)
    runner = KernelWorkerRunner(registry, workspace_root=tmp_path)
    record = _make_record(WorkerKind.CODEX_CLI, prompt="review pr 99")
    write_record(record)

    await runner.run(record)
    persisted = load_record(record.id)
    assert persisted is not None
    assert persisted.status == WorkerStatus.FAILED
    assert persisted.status_history[-1].reason == "tool_error"
    assert "something broke" in persisted.summary


async def test_tool_denied_writes_failed_with_deny_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch_execute,
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    registry = _FakeRegistry(output="", is_error=True, denied=True)
    runner = KernelWorkerRunner(registry, workspace_root=tmp_path)
    record = _make_record(WorkerKind.CLAUDE_CLI, prompt="dangerous task")
    write_record(record)

    await runner.run(record)
    persisted = load_record(record.id)
    assert persisted is not None
    assert persisted.status == WorkerStatus.FAILED
    assert persisted.status_history[-1].reason == "tool_denied"
    assert "ASK denied" in (persisted.summary + (persisted.error_message or ""))


async def test_runner_crash_writes_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch_execute) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    registry = _FakeRegistry(raise_exc=RuntimeError("network gone"))
    runner = KernelWorkerRunner(registry, workspace_root=tmp_path)
    record = _make_record(WorkerKind.MARKDOWN_AGENT, role="vault_librarian", prompt="ping")
    write_record(record)

    await runner.run(record)
    persisted = load_record(record.id)
    assert persisted is not None
    assert persisted.status == WorkerStatus.FAILED
    assert persisted.error_class == "RuntimeError"
    assert "network gone" in (persisted.error_message or "")
    assert persisted.status_history[-1].reason == "runner_crash"


async def test_terminal_kind_marks_failed_without_tool_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch_execute,
) -> None:
    """TERMINAL is the only kind still deferred. Sanity check that the
    runner surfaces it as a clean FAILED with no tool fire."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    registry = _FakeRegistry()
    runner = KernelWorkerRunner(registry, workspace_root=tmp_path)
    record = _make_record(WorkerKind.TERMINAL, prompt="open shell")
    write_record(record)

    await runner.run(record)
    persisted = load_record(record.id)
    assert persisted is not None
    assert persisted.status == WorkerStatus.FAILED
    assert persisted.status_history[-1].reason == "unsupported_kind"
    assert registry.calls == []


async def test_tars_self_dispatches_invoke_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch_execute,
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    registry = _FakeRegistry(output="memory has 2 entries matching")
    runner = KernelWorkerRunner(registry, workspace_root=tmp_path)
    record = _make_record(WorkerKind.TARS_SELF, role="tars-self", prompt="recent activity?")
    write_record(record)

    await runner.run(record)
    persisted = load_record(record.id)
    assert persisted is not None
    assert persisted.status == WorkerStatus.DONE
    assert registry.calls == [
        ("invoke_agent", {"name": "tars-self", "task": "recent activity?", "background": False}),
    ]


async def test_markdown_agent_dispatches_invoke_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch_execute,
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    registry = _FakeRegistry(output="vault has 3 fresh entries today.")
    runner = KernelWorkerRunner(registry, workspace_root=tmp_path)
    record = _make_record(WorkerKind.MARKDOWN_AGENT, role="vault_librarian", prompt="summarise")
    write_record(record)

    await runner.run(record)
    persisted = load_record(record.id)
    assert persisted is not None
    assert persisted.status == WorkerStatus.DONE
    assert registry.calls == [
        ("invoke_agent", {"name": "vault_librarian", "task": "summarise", "background": False}),
    ]


async def test_long_output_truncated_to_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch_execute,
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    long_output = "A" * 100 + "TAIL_OF_OUTPUT" + "B" * 600
    registry = _FakeRegistry(output=long_output)
    runner = KernelWorkerRunner(registry, workspace_root=tmp_path)
    record = _make_record(WorkerKind.CLAUDE_CLI)
    write_record(record)

    await runner.run(record)
    persisted = load_record(record.id)
    assert persisted is not None
    assert len(persisted.summary) <= 500
    # Tail-of-output anchor lives in the kept slice.
    assert persisted.summary.endswith("B" * 500)


# ── Billing posture per WorkerKind ─────────────────────────────────────


async def test_billing_subscription_on_cli_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch_execute,
) -> None:
    """CLAUDE_CLI / CODEX_CLI route through the flat-rate subscription —
    must persist as ``Billing.SUBSCRIPTION`` so the dashboard renders
    `sub` instead of a misleading ``$0.00``."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    for kind in (WorkerKind.CLAUDE_CLI, WorkerKind.CODEX_CLI):
        registry = _FakeRegistry(output="done")
        runner = KernelWorkerRunner(registry, workspace_root=tmp_path)
        record = _make_record(kind, prompt="x")
        write_record(record)
        await runner.run(record)
        persisted = load_record(record.id)
        assert persisted is not None
        assert persisted.billing == Billing.SUBSCRIPTION, kind


async def test_billing_api_on_invoke_agent_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch_execute,
) -> None:
    """MARKDOWN_AGENT / TARS_SELF hit the metered chat_brain API —
    per-call cost IS reportable; billing posture reflects that."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    for kind, role in ((WorkerKind.MARKDOWN_AGENT, "vault_librarian"), (WorkerKind.TARS_SELF, "tars-self")):
        registry = _FakeRegistry(output="done")
        runner = KernelWorkerRunner(registry, workspace_root=tmp_path)
        record = _make_record(kind, role=role, prompt="x")
        write_record(record)
        await runner.run(record)
        persisted = load_record(record.id)
        assert persisted is not None
        assert persisted.billing == Billing.API, kind


async def test_billing_unknown_on_unsupported_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch_execute,
) -> None:
    """Deferred kinds (TERMINAL) fall through ``unsupported_kind`` without
    a posture mapping; UNKNOWN is the honest default."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    registry = _FakeRegistry()
    runner = KernelWorkerRunner(registry, workspace_root=tmp_path)
    record = _make_record(WorkerKind.TERMINAL, prompt="open shell")
    write_record(record)
    await runner.run(record)
    persisted = load_record(record.id)
    assert persisted is not None
    assert persisted.billing == Billing.UNKNOWN


async def test_billing_unknown_on_runner_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch_execute,
) -> None:
    """Crash path writes the record at the default UNKNOWN — we never
    falsely label a crashed dispatch as subscription/api after the fact."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    registry = _FakeRegistry(raise_exc=RuntimeError("network gone"))
    runner = KernelWorkerRunner(registry, workspace_root=tmp_path)
    record = _make_record(WorkerKind.CLAUDE_CLI, prompt="x")
    write_record(record)
    await runner.run(record)
    persisted = load_record(record.id)
    assert persisted is not None
    assert persisted.status == WorkerStatus.FAILED
    assert persisted.billing == Billing.UNKNOWN


# ── Wallclock timeout — BLOCKED instead of FAILED + notify ─────────────


async def test_timeout_transitions_to_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch_execute,
) -> None:
    """delegate_claude emits 'timed out after 1800s' on wallclock; the
    runner must distinguish that from a generic tool error and route
    to BLOCKED so the agenda item survives for operator follow-up."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    registry = _FakeRegistry(
        output="delegate_claude timed out after 1800s (PTY route)",
        is_error=True,
    )
    runner = KernelWorkerRunner(registry, workspace_root=tmp_path)
    record = _make_record(WorkerKind.CLAUDE_CLI, prompt="long task")
    write_record(record)
    await runner.run(record)
    persisted = load_record(record.id)
    assert persisted is not None
    assert persisted.status == WorkerStatus.BLOCKED
    assert persisted.status_history[-1].reason == "wallclock_exceeded"
    # Generic tool errors still go FAILED — confirm we don't over-rescue
    assert "timed out" in persisted.summary.lower()


async def test_timeout_fires_notifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch_execute,
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    notified: list[str] = []

    async def _notify(record):
        notified.append(record.id)

    registry = _FakeRegistry(
        output="delegate_claude timed out after 1800s",
        is_error=True,
    )
    runner = KernelWorkerRunner(
        registry, workspace_root=tmp_path, timeout_notifier=_notify,
    )
    record = _make_record(WorkerKind.CLAUDE_CLI, prompt="x")
    write_record(record)
    await runner.run(record)
    assert notified == [record.id]


async def test_timeout_notifier_exception_does_not_break_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch_execute,
) -> None:
    """Notifier callback failure is a Telegram problem, not a worker
    problem — the BLOCKED transition must still land on disk."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    async def _bad_notify(record):
        raise RuntimeError("telegram unreachable")

    registry = _FakeRegistry(
        output="delegate_claude timed out after 1800s",
        is_error=True,
    )
    runner = KernelWorkerRunner(
        registry, workspace_root=tmp_path, timeout_notifier=_bad_notify,
    )
    record = _make_record(WorkerKind.CLAUDE_CLI, prompt="x")
    write_record(record)
    await runner.run(record)
    persisted = load_record(record.id)
    assert persisted is not None
    assert persisted.status == WorkerStatus.BLOCKED


async def test_worker_timeouts_threaded_to_delegate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch_execute,
) -> None:
    """Operator-set per-kind timeouts must reach the tool's args dict.
    Without this the autonomy runner silently uses the tool's own 300s
    default — the original bug that wasted the heartbeat worker."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    registry = _FakeRegistry(output="done")
    runner = KernelWorkerRunner(
        registry,
        workspace_root=tmp_path,
        worker_timeouts={
            WorkerKind.CLAUDE_CLI: 1800.0,
            WorkerKind.CODEX_CLI: 900.0,
        },
    )
    record = _make_record(WorkerKind.CLAUDE_CLI, prompt="x")
    write_record(record)
    await runner.run(record)
    assert registry.calls == [
        ("delegate_claude", {"task": "x", "background": False, "timeout": 1800.0}),
    ]


async def test_structured_timed_out_flag_routes_to_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch_execute,
) -> None:
    """The new ToolResult.timed_out flag is the primary signal — set it
    without the legacy 'timed out' substring in the output and confirm
    BLOCKED still lands. Future tool rewordings then can't silently
    flip BLOCKED to FAILED."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    registry = _FakeRegistry(
        output="deadline exceeded",  # no "timed out" substring
        is_error=True,
        timed_out=True,
    )
    runner = KernelWorkerRunner(registry, workspace_root=tmp_path)
    record = _make_record(WorkerKind.CLAUDE_CLI, prompt="x")
    write_record(record)
    await runner.run(record)
    persisted = load_record(record.id)
    assert persisted is not None
    assert persisted.status == WorkerStatus.BLOCKED
    assert persisted.status_history[-1].reason == "wallclock_exceeded"


async def test_worker_timeouts_not_threaded_to_invoke_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch_execute,
) -> None:
    """invoke_agent has no timeout arg — passing one would noisily
    surface as an unknown-arg error. Confirm the runner only threads
    timeout into delegate_claude / delegate_codex."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    registry = _FakeRegistry(output="done")
    runner = KernelWorkerRunner(
        registry,
        workspace_root=tmp_path,
        worker_timeouts={WorkerKind.MARKDOWN_AGENT: 600.0},
    )
    record = _make_record(WorkerKind.MARKDOWN_AGENT, role="vault_librarian", prompt="x")
    write_record(record)
    await runner.run(record)
    assert registry.calls == [
        ("invoke_agent", {"name": "vault_librarian", "task": "x", "background": False}),
    ]
