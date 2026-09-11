"""How full the conversation was when the boundary last measured it.

A decision that depends on how full the window is, taken by something that has
to ask first, is a decision usually taken without asking. So the number arrives
with the turn instead of behind a `context_read` call.

**It cannot be measured where it is read.** ``assemble_system_prompt`` is
session-agnostic and ``PromptInputs`` carries no ``ChatSession`` by design, and
every path to the figure — ``measure_payload``, ``token_estimate``,
``should_compact`` — assembles the payload, which assembles the prompt. A
section builder that reached for it would re-enter its own assembly.

So the boundary publishes and the prompt reads. ``ChatSession.should_compact``
already measures both halves once per turn for its own decision, and hands them
here on the way past; the next turn's prompt renders what that measurement
found. One turn behind, and that is the honest reading: it describes the
conversation the last turn ended with.

The scope key is ``ChatSession._failures_scope_id``, the same per-instance id
``failures_signal`` scopes its streaks by and the same one
``_current_system_prompt`` binds for the section builders to read back. It is
per instance and not per ``session_id`` because a synthetic fork shares its
parent's ``session_id`` and runs beside it. The name stays that module's,
rather than being renamed across thirty call sites for a second reader.

In memory only, and dropped when the conversation it describes is rewritten:
a fold or a wipe leaves the last reading describing a conversation that no
longer exists, and a stale one reads exactly like a current one.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Same guard as `failures_signal`, for the same reason: a long-lived process
#: mints a scope per `ChatSession`, including every one-off synthetic turn.
MAX_SCOPES = 512


@dataclass(frozen=True)
class Fullness:
    """What the fold decision measured, as it measured it.

    Both numbers, not the ratio, because the two are what the decision compared
    and a ratio cannot be checked against anything afterwards.
    """

    conversation_tokens: int
    trigger_tokens: int

    @property
    def ratio(self) -> float:
        """How far the conversation is towards the boundary. 1.0 is at it.

        A trigger of zero or less means the runtime has no boundary to reach
        here, which is a fresh or unconfigured session rather than a full one,
        so it reads as empty rather than as past the line.
        """
        if self.trigger_tokens <= 0:
            return 0.0
        return self.conversation_tokens / self.trigger_tokens


_by_scope: dict[str, Fullness] = {}


def record(scope: str, *, conversation_tokens: int, trigger_tokens: float) -> None:
    if not scope:
        return
    if scope not in _by_scope and len(_by_scope) >= MAX_SCOPES:
        return
    _by_scope[scope] = Fullness(int(conversation_tokens), int(trigger_tokens))


def read(scope: str | None) -> Fullness | None:
    """The last reading for this conversation, or `None` if there is not one.

    `None` is the ordinary answer, not a failure: a conversation short enough
    that the fold decision returns before measuring has nothing to report, and
    so does a measurement taken outside any session.
    """
    if not scope:
        return None
    return _by_scope.get(scope)


def forget(scope: str) -> None:
    """Drop a reading whose conversation has been rewritten."""
    _by_scope.pop(scope, None)


__all__ = ["Fullness", "MAX_SCOPES", "record", "read", "forget"]
