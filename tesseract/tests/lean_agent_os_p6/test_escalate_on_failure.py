"""P6 Task 5 — escalate-on-failure reflex.

Design: Docs/Plan/lean-agent-os/p6-p7-alive-scout-design.md §"P6 amendment 2
— escalate-on-failure reflex". Two parts, tested separately:

1. Rule content — `11-error-recovery.md` (rewritten in place, not a new
   file) carries the two-strikes + `lane_turn` escalation directive, loaded
   by the REAL prompt builder (`assemble_system_prompt`).
2. Signal side — consecutive same-tool error streaks (>=2) within a turn
   are recorded into `failures_signal` by `ChatSession._run_pending_calls`
   (same in-memory idiom as Task 3's stall/vanished counters — see commit
   17dcd360), and `failures_reader` renders one digest line for it. A
   single failure records nothing; a success of the streaked tool clears
   it; a different tool's failure/success doesn't touch an unrelated
   streak.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from tesseract.brain import failures_signal
from tesseract.brain.autonomy_digest import FailuresSnapshot, render_digest
from tesseract.brain.chat import ChatSession
from tesseract.kernel.adapters.base import AdapterOptions, ChunkType, ModelAdapter, StreamChunk
from tesseract.kernel.state import ToolCall
from tesseract.kernel.tools.base import ToolContext, ToolResult

NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _reset_failures_signal():
    """Module-level state is process-global — isolate this file from any
    sibling test (or another test file sharing the pytest session, e.g.
    the halt-watchdog / failures-digest suites) that leaves state behind."""
    failures_signal.reset_for_tests()
    yield
    failures_signal.reset_for_tests()


# -- rule content (real builder) --------------------------------------------


def test_rule_11_carries_two_strikes_and_lane_turn_directive(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    from tesseract.brain import prompt as prompt_module

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    memory_store_dir = tmp_path / "memory-store"

    prompt = prompt_module.assemble_system_prompt(
        workspace_dir=workspace_dir, memory_store_dir=memory_store_dir,
    )

    assert "lane_turn" in prompt
    assert "third identical attempt" in prompt
    assert "escalate" in prompt.lower()

    # ROLES-ARE-PILLARS — no model IDs in rule 11 itself (other rules, e.g.
    # the multimodal one, may legitimately name a model family elsewhere).
    from tesseract.brain.prompt import RULES_DIR
    rule_text = (RULES_DIR / "11-error-recovery.md").read_text(encoding="utf-8").lower()
    assert "claude-" not in rule_text
    assert "gpt-" not in rule_text


# -- failures_signal (unit) --------------------------------------------------


def test_tool_error_streak_record_get_clear_roundtrip():
    assert failures_signal.tool_error_streak() is None
    failures_signal.record_tool_error_streak("web_search", 2)
    assert failures_signal.tool_error_streak() == ("web_search", 2)
    failures_signal.clear_tool_error_streak()
    assert failures_signal.tool_error_streak() is None


# -- render_digest (pure) -----------------------------------------------------


def test_tool_error_streak_line_rendered():
    out = render_digest(
        lambda: [], lambda: [],
        failures_reader=lambda: FailuresSnapshot((), 0, 0, ("web_search", 3)),
        now=NOW,
    )
    assert out == "Failure: web_search failed 3x consecutively last turn"


def test_tool_error_streak_absent_produces_no_line():
    out = render_digest(
        lambda: [], lambda: [],
        failures_reader=lambda: FailuresSnapshot((), 0, 0, None),
        now=NOW,
    )
    assert out == ""


def test_tool_error_streak_line_joins_other_failure_facts():
    out = render_digest(
        lambda: [], lambda: [],
        failures_reader=lambda: FailuresSnapshot(("spawn-wake",), 1, 0, ("web_search", 2)),
        now=NOW,
    )
    lines = out.splitlines()
    assert "Failure: breaker tripped — spawn-wake" in lines
    assert "Failure: 1 spawn(s) stalled" in lines
    assert "Failure: web_search failed 2x consecutively last turn" in lines
    assert len(lines) == 3


# -- prompt.py integration ----------------------------------------------------


def test_assemble_system_prompt_shows_tool_error_streak_line(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    from tesseract.brain import prompt as prompt_module

    failures_signal.record_tool_error_streak("web_search", 2)

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    memory_store_dir = tmp_path / "memory-store"

    prompt = prompt_module.assemble_system_prompt(
        workspace_dir=workspace_dir, memory_store_dir=memory_store_dir,
        failures_scope="",
    )
    assert "Failure: web_search failed 2x consecutively last turn" in prompt


# -- ChatSession._run_pending_calls (scripted double-failure) ----------------


@dataclass
class _StubResult:
    output: str
    is_error: bool = False
    denied_hard: bool = False
    deny_reason: str = ""
    metadata: dict | None = None


def _make_session(registry: Any) -> ChatSession:
    """Barebones ChatSession, same construction idiom as
    `fix_pass_2026_04_29_owner_batch1/test_run_pending_calls_safety_net.py`
    — just enough wiring for `_run_pending_calls` to run standalone, no
    adapter/network calls."""
    sess = ChatSession.__new__(ChatSession)
    sess.history = []
    sess.registry = registry
    sess.tool_context = ToolContext(workspace_root=".", session_id="test")
    sess.ask_fn = None
    sess.policy = None
    sess.options = AdapterOptions(role="chat_brain", model="stub", provider="stub")
    sess.adapter = MagicMock()
    sess.cost_ledger = None
    sess._observer_last_index = 0
    sess._pending_suggestions = []
    sess._pending_conscience = []
    sess._observed_ids = set()
    sess._turn_injection = ""
    sess._tool_error_streak_name = ""
    sess._tool_error_streak_count = 0
    sess._failures_scope_id = "test"
    return sess


async def _drain(gen):
    out = []
    async for chunk in gen:
        out.append(chunk)
    return out


def _registry_for(tool_name: str) -> MagicMock:
    registry = MagicMock()
    registry.get.return_value = MagicMock(is_concurrency_safe=lambda: False, untrusted_source=False)
    return registry


@pytest.mark.asyncio
async def test_same_tool_failing_twice_consecutively_records_streak(monkeypatch):
    registry = _registry_for("web_search")

    async def failing_execute(**kwargs):
        return _StubResult(output="boom", is_error=True)

    monkeypatch.setattr("tesseract.brain.chat.execute_tool", failing_execute)
    sess = _make_session(registry)

    # Two separate tool-loop iterations (as a real retry would produce),
    # same tool, both erroring.
    await _drain(sess._run_pending_calls([ToolCall(id="c1", name="web_search", input={})]))
    assert failures_signal.tool_error_streak("test") is None  # single failure -> no line yet

    await _drain(sess._run_pending_calls([ToolCall(id="c2", name="web_search", input={})]))
    assert failures_signal.tool_error_streak("test") == ("web_search", 2)


@pytest.mark.asyncio
async def test_single_failure_records_no_streak(monkeypatch):
    registry = _registry_for("web_search")

    async def failing_execute(**kwargs):
        return _StubResult(output="boom", is_error=True)

    monkeypatch.setattr("tesseract.brain.chat.execute_tool", failing_execute)
    sess = _make_session(registry)

    await _drain(sess._run_pending_calls([ToolCall(id="c1", name="web_search", input={})]))
    assert failures_signal.tool_error_streak() is None


@pytest.mark.asyncio
async def test_streak_resets_on_tool_success(monkeypatch):
    registry = _registry_for("web_search")
    outcomes = iter([True, True, False])  # error, error, success

    async def scripted_execute(**kwargs):
        return _StubResult(output="x", is_error=next(outcomes))

    monkeypatch.setattr("tesseract.brain.chat.execute_tool", scripted_execute)
    sess = _make_session(registry)

    await _drain(sess._run_pending_calls([ToolCall(id="c1", name="web_search", input={})]))
    await _drain(sess._run_pending_calls([ToolCall(id="c2", name="web_search", input={})]))
    assert failures_signal.tool_error_streak("test") == ("web_search", 2)

    await _drain(sess._run_pending_calls([ToolCall(id="c3", name="web_search", input={})]))
    assert failures_signal.tool_error_streak("test") is None


@pytest.mark.asyncio
async def test_different_tool_failure_does_not_continue_streak(monkeypatch):
    registry = _registry_for("web_search")

    async def failing_execute(**kwargs):
        return _StubResult(output="boom", is_error=True)

    monkeypatch.setattr("tesseract.brain.chat.execute_tool", failing_execute)
    sess = _make_session(registry)

    await _drain(sess._run_pending_calls([ToolCall(id="c1", name="web_search", input={})]))
    await _drain(sess._run_pending_calls([ToolCall(id="c2", name="file_read", input={})]))

    # web_search failed once, file_read failed once — neither reaches 2
    # consecutive same-tool failures.
    assert failures_signal.tool_error_streak() is None


# -- fix-pass 1 regression: clear must be gated on the RECORDED tool, not --
# -- the local per-turn tracker (reviewer finding on commit 99f91c9a) -------


@pytest.mark.asyncio
async def test_unrelated_tool_success_in_a_later_turn_does_not_wipe_recorded_streak(monkeypatch):
    """Repro from review: turn 1 records a genuine web_search streak. A
    later turn's local tracker resets (as `send()` does) and then gets
    reassigned to an unrelated tool (file_read) that fails once and then
    succeeds. That success must NOT clear web_search's recorded streak —
    web_search itself was never retried."""
    registry = _registry_for("web_search")

    async def failing_web_search(**kwargs):
        return _StubResult(output="boom", is_error=True)

    monkeypatch.setattr("tesseract.brain.chat.execute_tool", failing_web_search)
    sess = _make_session(registry)

    # Turn 1: web_search fails twice consecutively.
    await _drain(sess._run_pending_calls([ToolCall(id="c1", name="web_search", input={})]))
    await _drain(sess._run_pending_calls([ToolCall(id="c2", name="web_search", input={})]))
    assert failures_signal.tool_error_streak("test") == ("web_search", 2)

    # New turn: `send()` resets the local per-turn tracker.
    sess._tool_error_streak_name = ""
    sess._tool_error_streak_count = 0

    # Turn 2: an entirely unrelated tool fails once, then succeeds.
    outcomes = iter([True, False])

    async def scripted_file_read(**kwargs):
        return _StubResult(output="x", is_error=next(outcomes))

    monkeypatch.setattr("tesseract.brain.chat.execute_tool", scripted_file_read)
    await _drain(sess._run_pending_calls([ToolCall(id="c3", name="file_read", input={})]))
    await _drain(sess._run_pending_calls([ToolCall(id="c4", name="file_read", input={})]))

    assert failures_signal.tool_error_streak("test") == ("web_search", 2), (
        "file_read's own recover-after-one-failure must not erase "
        "web_search's genuine, still-unresolved streak"
    )


@pytest.mark.asyncio
async def test_recorded_tool_succeeding_in_a_later_turn_still_clears(monkeypatch):
    """The flip side of the same fix: the clear gate must check the
    actually-recorded signal, not the (turn-reset) local tracker — so a
    later-turn success of the SAME tool that was streaked still clears it
    even though the local tracker was zeroed at the new turn's start."""
    registry = _registry_for("web_search")

    async def failing(**kwargs):
        return _StubResult(output="boom", is_error=True)

    monkeypatch.setattr("tesseract.brain.chat.execute_tool", failing)
    sess = _make_session(registry)

    await _drain(sess._run_pending_calls([ToolCall(id="c1", name="web_search", input={})]))
    await _drain(sess._run_pending_calls([ToolCall(id="c2", name="web_search", input={})]))
    assert failures_signal.tool_error_streak("test") == ("web_search", 2)

    # New turn: local tracker resets, same as `send()` does.
    sess._tool_error_streak_name = ""
    sess._tool_error_streak_count = 0

    async def succeeding(**kwargs):
        return _StubResult(output="ok", is_error=False)

    monkeypatch.setattr("tesseract.brain.chat.execute_tool", succeeding)
    await _drain(sess._run_pending_calls([ToolCall(id="c3", name="web_search", input={})]))

    assert failures_signal.tool_error_streak("test") is None


# -- full send() turn (scripted adapter) -------------------------------------


class _RetryToolAdapter(ModelAdapter):
    """Emits the same tool call on the first two tool-loop iterations of a
    turn, then gives up and stops with plain text — the shape of a model
    retrying once after a failure per rule 11, then escalating instead of
    trying a third time."""

    def __init__(self) -> None:
        self._calls = 0

    async def stream(self, messages, tools=None, options=None):
        self._calls += 1
        if self._calls <= 2:
            yield StreamChunk(
                type=ChunkType.TOOL_CALL_END,
                tool_call_id=f"call_{self._calls}",
                tool_call=ToolCall(id=f"call_{self._calls}", name="flaky_tool", input={}),
            )
            yield StreamChunk(type=ChunkType.STOP, stop_reason="tool_use", raw={"usage": {}})
        else:
            yield StreamChunk(type=ChunkType.TEXT, text="escalating to a coder lane")
            yield StreamChunk(type=ChunkType.STOP, stop_reason="end_turn", raw={"usage": {}})

    def count_tokens(self, messages) -> int:
        return 0

    async def check_available(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_send_turn_scripted_double_failure_records_streak_for_digest(monkeypatch):
    """Drives a full `send()` turn (real tool loop, not just
    `_run_pending_calls`) where the same tool errors on both retries. The
    next prompt build's digest must carry the escalate-now line."""

    async def failing_execute(**kwargs):
        return ToolResult(output="boom", is_error=True)

    monkeypatch.setattr("tesseract.brain.chat.execute_tool", failing_execute)

    registry = _registry_for("flaky_tool")
    cs = ChatSession(
        adapter=_RetryToolAdapter(),
        system_prompt="",
        max_tool_iterations=5,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="fake", provider="fake"),
        registry=registry,
        tool_context=ToolContext(),
    )

    async for _chunk in cs.send("fetch this for me"):
        pass

    # `cs` was built through the real `ChatSession(...)` constructor, so its
    # `_failures_scope_id` is a freshly-minted uuid (not the "" default) —
    # assert against the session's own scope, not the "" bucket.
    assert failures_signal.tool_error_streak(cs._failures_scope_id) == ("flaky_tool", 2)


# -- whole-phase review fix: streak scoped per session, no cross-chat leak --


def _make_session_with_scope(registry: Any, session_id: str) -> ChatSession:
    """Same barebones session as `_make_session`, but with an explicit
    `session_id` (also used as the `_failures_scope_id` here, for
    readable assertions — production forks are the one case where the two
    diverge; see `test_fork_gets_its_own_scope_id_despite_sharing_session_id`)."""
    sess = _make_session(registry)
    sess.tool_context = ToolContext(workspace_root=".", session_id=session_id)
    sess._failures_scope_id = session_id
    return sess


@pytest.mark.asyncio
async def test_two_sessions_streaks_are_isolated(monkeypatch):
    registry = _registry_for("web_search")

    async def failing_execute(**kwargs):
        return _StubResult(output="boom", is_error=True)

    monkeypatch.setattr("tesseract.brain.chat.execute_tool", failing_execute)

    sess_a = _make_session_with_scope(registry, "chat-a")
    sess_b = _make_session_with_scope(registry, "chat-b")

    await _drain(sess_a._run_pending_calls([ToolCall(id="a1", name="web_search", input={})]))
    await _drain(sess_a._run_pending_calls([ToolCall(id="a2", name="web_search", input={})]))

    assert failures_signal.tool_error_streak("chat-a") == ("web_search", 2)
    assert failures_signal.tool_error_streak("chat-b") is None

    from tesseract.brain.prompt import _read_failures_snapshot
    assert _read_failures_snapshot("chat-a").tool_error_streak == ("web_search", 2)
    assert _read_failures_snapshot("chat-b").tool_error_streak is None
    # A frozen/boot prompt (no scope in flight) shows neither chat's streak.
    assert _read_failures_snapshot(None).tool_error_streak is None


@pytest.mark.asyncio
async def test_other_sessions_success_does_not_clear_this_sessions_streak(monkeypatch):
    registry = _registry_for("web_search")

    async def failing_execute(**kwargs):
        return _StubResult(output="boom", is_error=True)

    monkeypatch.setattr("tesseract.brain.chat.execute_tool", failing_execute)

    sess_a = _make_session_with_scope(registry, "chat-a")
    sess_b = _make_session_with_scope(registry, "chat-b")

    await _drain(sess_a._run_pending_calls([ToolCall(id="a1", name="web_search", input={})]))
    await _drain(sess_a._run_pending_calls([ToolCall(id="a2", name="web_search", input={})]))
    assert failures_signal.tool_error_streak("chat-a") == ("web_search", 2)

    async def succeeding_execute(**kwargs):
        return _StubResult(output="ok", is_error=False)

    monkeypatch.setattr("tesseract.brain.chat.execute_tool", succeeding_execute)
    await _drain(sess_b._run_pending_calls([ToolCall(id="b1", name="web_search", input={})]))

    assert failures_signal.tool_error_streak("chat-a") == ("web_search", 2), (
        "chat-b succeeding at the same tool name must not clear chat-a's "
        "still-unresolved streak"
    )
    assert failures_signal.tool_error_streak("chat-b") is None


@pytest.mark.asyncio
async def test_own_sessions_success_clears_its_own_streak(monkeypatch):
    registry = _registry_for("web_search")
    outcomes = iter([True, True, False])

    async def scripted_execute(**kwargs):
        return _StubResult(output="x", is_error=next(outcomes))

    monkeypatch.setattr("tesseract.brain.chat.execute_tool", scripted_execute)
    sess_a = _make_session_with_scope(registry, "chat-a")

    await _drain(sess_a._run_pending_calls([ToolCall(id="a1", name="web_search", input={})]))
    await _drain(sess_a._run_pending_calls([ToolCall(id="a2", name="web_search", input={})]))
    assert failures_signal.tool_error_streak("chat-a") == ("web_search", 2)

    await _drain(sess_a._run_pending_calls([ToolCall(id="a3", name="web_search", input={})]))
    assert failures_signal.tool_error_streak("chat-a") is None


def test_current_system_prompt_threads_own_scope_via_contextvar(tmp_path, monkeypatch):
    """`_current_system_prompt` (chat.py ~1112) is the only place a session's
    per-turn prompt gets rebuilt through a shared, zero-arg `prompt_builder`
    closure (the cockpit case shares one closure across every cockpit
    `ChatSession` — see `mirror/server/session.py::_build_chat_session`).
    Verifies the contextvar bridge: each session's own scope reaches the
    digest without the two sessions' streaks crossing."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    from tesseract.brain import prompt as prompt_module

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    memory_store_dir = tmp_path / "memory-store"

    def _shared_builder() -> str:
        return prompt_module.assemble_system_prompt(
            workspace_dir=workspace_dir, memory_store_dir=memory_store_dir,
        )

    sess_a = ChatSession.__new__(ChatSession)
    sess_a.system_prompt = ""
    sess_a.prompt_builder = _shared_builder
    sess_a.tool_context = ToolContext(workspace_root=".", session_id="chat-a")
    sess_a._failures_scope_id = "chat-a"

    sess_b = ChatSession.__new__(ChatSession)
    sess_b.system_prompt = ""
    sess_b.prompt_builder = _shared_builder
    sess_b.tool_context = ToolContext(workspace_root=".", session_id="chat-b")
    sess_b._failures_scope_id = "chat-b"

    failures_signal.record_tool_error_streak("web_search", 2, "chat-a")

    prompt_a = sess_a._current_system_prompt()
    prompt_b = sess_b._current_system_prompt()

    assert "Failure: web_search failed 2x consecutively last turn" in prompt_a
    assert "Failure: web_search failed 2x consecutively last turn" not in prompt_b
