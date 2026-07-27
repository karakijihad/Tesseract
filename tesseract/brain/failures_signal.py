"""Process-global failure counters feeding the autonomy digest (P6 Task 3 §G4,
extended P6 Task 5 §escalate-on-failure).

``tesseract.brain.prompt.assemble_system_prompt`` is a pure, session-agnostic
function — it has no ``ChatSession`` reference to read a per-chat stall count
off of. Rather than thread one through every caller, the halt-watchdog
(``ChatSession._sweep_stalled_spawns``) and the resume-time vanished-spawn
sweep (``ChatSession.mark_vanished_spawns``) bump these two module-level
counters, and the digest's ``failures_reader`` (``prompt.py::
_read_failures_snapshot``) reads them back.

Counts are cumulative since this backend process booted ("for that boot" —
idle-wake-design.md §G5). In-memory only, never persisted: the underlying
events already have durable evidence elsewhere (circuit-breaker JSONLs for
breakers, the spawn journal for vanished spawns) — this is ambient signal,
not a record.

P6 Task 5 adds ``_tool_error_streaks`` — the same in-memory idiom, but a
dict of overwritable ``(tool_name, count)`` snapshots keyed by scope, rather
than a single cumulative counter: ``ChatSession._run_pending_calls`` records
into its own scope's slot when the same tool fails ≥2 times consecutively
within a turn, and clears that slot when the tool next succeeds. This lets
the digest line read "last turn", not "since boot".

Whole-phase P6 review (2026-07-06): the streak was originally a single
overwritable ``(tool_name, count)`` slot shared by every ``ChatSession`` in
the process. With concurrent chats (cockpit multi-chat, Telegram bridge,
synthetic turns) one chat's tool failure leaked into every other chat's
digest, and any chat's success of that tool name cleared an unrelated
chat's unresolved streak. Scoping key is ``ChatSession._failures_scope_id``
— a per-instance id, deliberately NOT ``tool_context.session_id`` (which
Task 3 threads into the spawn journal): a synthetic fork
(``ChatSession.fork_for_synthetic``) shares its parent's ``session_id`` by
design but runs concurrently with it, so scoping on ``session_id`` would
have let a fork's tool call clear or collide with its parent's still-open
streak. Every ``ChatSession()`` construction (including a fork's) mints a
fresh scope id via ``default_factory``, so no extra plumbing is needed to
keep forks isolated too.

The read side (``prompt.py::_read_failures_snapshot``) takes an explicit
scope; a scope of ``None`` renders no streak line at all — correct for a
frozen/boot prompt with no turn history. ``ChatSession._current_system_prompt``
threads its own scope through the ambient ``bind_scope``/``active_scope``
contextvar (see there) so the digest reflects the calling session without
changing ``prompt_builder``'s zero-arg ``Callable[[], str]`` signature —
asyncio contextvars are per-Task, so concurrent ``send()`` calls in sibling
tasks never see each other's bound scope.

``stalled_count``/``vanished_count`` stay global by design (ambient,
process-wide signal) — only the streak is scoped.
"""

from __future__ import annotations

import contextvars

_stalled_count = 0
_vanished_count = 0
_tool_error_streaks: dict[str, tuple[str, int]] = {}

# Sane upper bound so a process that accumulates many distinct session_ids
# over a long uptime (e.g. one-off synthetic/scout turns) can't grow this
# dict unbounded. Not config — purely a safety guard, not a tunable.
_MAX_SCOPES = 512

_active_scope: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "failures_signal_active_scope", default=None
)


def record_stall(n: int = 1) -> None:
    global _stalled_count
    _stalled_count += n


def record_vanished(n: int = 1) -> None:
    global _vanished_count
    _vanished_count += n


def stalled_count() -> int:
    return _stalled_count


def vanished_count() -> int:
    return _vanished_count


def record_tool_error_streak(tool_name: str, count: int, scope: str = "") -> None:
    if scope not in _tool_error_streaks and len(_tool_error_streaks) >= _MAX_SCOPES:
        # Evict the oldest scope (dict preserves insertion order) rather than
        # growing without bound.
        oldest = next(iter(_tool_error_streaks))
        del _tool_error_streaks[oldest]
    _tool_error_streaks[scope] = (tool_name, count)


def clear_tool_error_streak(scope: str = "") -> None:
    _tool_error_streaks.pop(scope, None)


def tool_error_streak(scope: str = "") -> tuple[str, int] | None:
    return _tool_error_streaks.get(scope)


def bind_scope(scope: str) -> contextvars.Token:
    """Bind ``scope`` as the "currently assembling" scope for the duration
    of a ``prompt_builder()`` call. Caller MUST ``reset_scope`` in a
    ``finally`` — see ``ChatSession._current_system_prompt``."""
    return _active_scope.set(scope)


def reset_scope(token: contextvars.Token) -> None:
    _active_scope.reset(token)


def active_scope() -> str | None:
    """The scope bound by the innermost ``bind_scope``, or ``None`` when no
    ``ChatSession`` prompt rebuild is in flight (e.g. a boot/frozen prompt
    assembled outside any per-turn call)."""
    return _active_scope.get()


def reset_for_tests() -> None:
    """Test-only: zero all counters. Module-level global state otherwise
    leaks between tests that don't share a process boot boundary."""
    global _stalled_count, _vanished_count
    _stalled_count = 0
    _vanished_count = 0
    _tool_error_streaks.clear()


__all__ = [
    "record_stall",
    "record_vanished",
    "stalled_count",
    "vanished_count",
    "record_tool_error_streak",
    "clear_tool_error_streak",
    "tool_error_streak",
    "bind_scope",
    "reset_scope",
    "active_scope",
    "reset_for_tests",
]
