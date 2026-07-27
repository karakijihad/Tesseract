"""P6 Task 3 §G4 — failures_reader: tripped breakers + stalled/vanished-spawn
counts surfaced in the autonomy digest.

Design: Docs/Plan/lean-agent-os/idle-wake-design.md §G4. Adds a third
`render_digest` reader alongside agenda/reflections, rendering at most ~3
ambient lines (one per non-empty fact); empty/all-zero → no section (same
fail-quiet contract as the other readers).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tesseract.brain import failures_signal
from tesseract.brain.autonomy_digest import FailuresSnapshot, ReflectionEntry, render_digest

NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _reset_failures_signal():
    """Module-level counters are process-global — isolate every test in this
    file from state a sibling test (or another test file run in the same
    pytest session, e.g. the halt-watchdog suite) may have left behind."""
    failures_signal.reset_for_tests()
    yield
    failures_signal.reset_for_tests()


# -- render_digest (pure) ---------------------------------------------------


def test_failures_lines_appended_when_all_present():
    out = render_digest(
        lambda: [], lambda: [],
        failures_reader=lambda: FailuresSnapshot(("spawn-wake",), 2, 3),
        now=NOW,
    )
    lines = out.splitlines()
    assert "Failure: breaker tripped — spawn-wake" in lines
    assert "Failure: 2 spawn(s) stalled" in lines
    assert "Failure: 3 spawn(s) vanished (backend restart)" in lines
    assert len(lines) == 3


def test_failures_section_omitted_when_all_zero():
    out = render_digest(
        lambda: [], lambda: [],
        failures_reader=lambda: FailuresSnapshot((), 0, 0),
        now=NOW,
    )
    assert out == ""


def test_failures_absent_when_no_reader():
    out = render_digest(lambda: [], lambda: [], now=NOW)
    assert out == ""


def test_only_nonzero_facts_produce_lines():
    out = render_digest(
        lambda: [], lambda: [],
        failures_reader=lambda: FailuresSnapshot((), 5, 0),
        now=NOW,
    )
    assert out == "Failure: 5 spawn(s) stalled"


def test_multiple_tripped_breakers_sorted_and_joined():
    out = render_digest(
        lambda: [], lambda: [],
        failures_reader=lambda: FailuresSnapshot(("spawn-wake", "vault_lint"), 0, 0),
        now=NOW,
    )
    assert out == "Failure: breaker tripped — spawn-wake, vault_lint"


def test_failures_reader_raising_is_isolated():
    def _boom():
        raise RuntimeError("breaker log corrupt")

    reflections = [ReflectionEntry(text="still here", created_at=NOW)]
    out = render_digest(lambda: [], lambda: reflections, failures_reader=_boom, now=NOW)
    assert "still here" in out
    assert "Failure" not in out


def test_failures_reader_returning_none_treated_as_empty():
    out = render_digest(
        lambda: [], lambda: [], failures_reader=lambda: None, now=NOW,
    )
    assert out == ""


# -- prompt.py integration --------------------------------------------------


def test_assemble_system_prompt_shows_failures_line_for_tripped_breaker(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    from tesseract.brain import prompt as prompt_module
    from tesseract.context.circuit_breaker import CircuitBreaker

    breaker_dir = tmp_path / "logs" / "circuit-breakers"
    breaker = CircuitBreaker(name="spawn-wake", max_failures=1, log_dir=breaker_dir)
    breaker.record_failure("boom")
    assert breaker.is_tripped

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    memory_store_dir = tmp_path / "memory-store"

    prompt = prompt_module.assemble_system_prompt(
        workspace_dir=workspace_dir, memory_store_dir=memory_store_dir,
    )
    assert "Failure: breaker tripped — spawn-wake" in prompt


def test_assemble_system_prompt_shows_stalled_and_vanished_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    from tesseract.brain import prompt as prompt_module

    failures_signal.record_stall(2)
    failures_signal.record_vanished(1)

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    memory_store_dir = tmp_path / "memory-store"

    prompt = prompt_module.assemble_system_prompt(
        workspace_dir=workspace_dir, memory_store_dir=memory_store_dir,
    )
    assert "Failure: 2 spawn(s) stalled" in prompt
    assert "Failure: 1 spawn(s) vanished (backend restart)" in prompt


def test_assemble_system_prompt_omits_failures_when_clean(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    from tesseract.brain import prompt as prompt_module

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    memory_store_dir = tmp_path / "memory-store"

    prompt = prompt_module.assemble_system_prompt(
        workspace_dir=workspace_dir, memory_store_dir=memory_store_dir,
    )
    assert "Autonomy digest" not in prompt
    assert "Failure" not in prompt
