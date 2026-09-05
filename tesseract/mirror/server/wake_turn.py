"""Driving one wake turn, and saying honestly how it went.

Three things wake a chat that nobody is speaking to: a background spawn
finishing (`spawn_wake`), a spawn taking too long (`spawn_heartbeat`), and a
press on a card the assistant drew (`card_wake`). They differ in what makes
them fire and in what they do afterwards. The turn in the middle is the same
turn, and this is it.

**Why it is worth one function.** The hard part is not running the turn, it is
reading the outcome. Both drivers can swallow an ordinary turn failure without
raising: the cockpit path reports it through an `outcome` dict, the channel
path by returning a string. Counting only exceptions would let a chat fail
every wake and never trip the breaker that exists to stop it looping. That
reading has already been corrected twice, and a third copy would have meant
correcting it three times by hand with nothing to say the copies had drifted.

Cancellation is neither a failure nor a success. The operator pressed stop; the
breaker learns nothing from that, and a wake that records it as a failure would
count the operator's own choices toward switching the feature off.

Neither is a refusal. A turn the runtime declined to begin — a spending cap, a
tool cap — did not exercise the wake path at all, so counting it says the wake
is broken when nothing about the wake is. Three turns refused on a spending cap
switched proactive waking off for 26 hours that way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class WakeOutcome:
    """What one wake turn did, after the breaker has been told.

    `committed` answers a question only the completion path asks: whether the
    turn delivered what it drained. A turn can end perfectly well without
    committing, because an adapter error it recovered from means the retry did
    not carry the block, and that is the difference between "try again" and
    "trying again just repeats it".
    """

    error: str | None = None
    cancelled: bool = False
    committed: bool = True

    @property
    def clean(self) -> bool:
        return self.error is None and not self.cancelled


async def drive(
    app: Any,
    session: Any,
    chat_id: str,
    *,
    breaker: Any,
    body: str,
    error_label: str,
    runtime_origin: str,
    turn_driver: Callable[..., Any] | None = None,
) -> WakeOutcome:
    """Run one wake turn, record the result on `breaker`, and report it.

    `body` is the turn's text on the cockpit path. A channel `turn_driver`
    composes its own, which is why `spawn_heartbeat` stashes the sentence it
    wants rather than passing it: the driver's only route to a body is the
    function it calls, so that is where a caller has to reach.

    `runtime_origin` says which of the three fired, and rides onto the stored
    message so the transcript draws the turn as the runtime's rather than the
    operator's (`brain/chat.py::RUNTIME_ORIGINS`). It reaches the cockpit path
    only: a channel driver composes its own body and has no way to say which
    wake it is serving, and no channel surface renders a speaker for it.

    An exception is recorded and re-raised, unchanged: the caller's follow-up
    is skipped and the task carries the failure, which is what a genuine
    blow-up should do. `CancelledError` is not an `Exception` and passes
    through untouched, so a cancelled task records nothing here.
    """
    error: str | None = None
    cancelled = False
    committed = True
    refused = False
    try:
        if turn_driver is None:
            from tesseract.mirror.server.turn_runner import _run_chat_turn

            outcome: dict[str, Any] = {}
            await _run_chat_turn(
                app, session, body,
                chat_id=chat_id, outcome=outcome, runtime_origin=runtime_origin,
            )
            committed = bool(outcome.get("committed", True))
            refused = bool(outcome.get("refused"))
            if outcome.get("cancelled"):
                cancelled = True
            elif outcome.get("ok") is False:
                error = error_label
        else:
            # The channel driver returns its error as a string, so refusal
            # needs a second way back. A box it fills, rather than the
            # session's `last_turn_outcome` read afterwards: that field
            # outlives the turn that wrote it, and a wake that died before the
            # stream would be handed the previous turn's refusal and forgiven.
            # Empty means the driver never got far enough to say, which is not
            # a refusal.
            refused_out: list[bool] = []
            error = await turn_driver(app, session, chat_id, refused_out=refused_out)
            committed = error is None
            refused = bool(error) and bool(refused_out) and refused_out[0]
    except Exception as exc:
        breaker.record_failure(str(exc))
        raise

    if cancelled:
        pass  # The operator stopped it. Neither a failure nor a success.
    elif error and refused:
        pass  # The turn never began. Not this path's failure to answer for.
    elif error:
        breaker.record_failure(error)
    else:
        breaker.record_success()
    return WakeOutcome(error=error, cancelled=cancelled, committed=committed)


__all__ = ["WakeOutcome", "drive"]
