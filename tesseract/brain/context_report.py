"""What this conversation costs, measured once and written once.

The numbers were reachable from exactly one place: `emit_stats` measured them
and pushed a WebSocket envelope, so the cockpit's HUD could draw a bar and
nothing else could ask. Not a channel, not the assistant, not the cockpit's own
chat. "How much room is left before you forget the start of this?" had no
answer on any surface, which is the same shape `autonomy_read` was built for.

So the measuring lives here, on its own, and has two readers: `emit_stats`
wraps it for the HUD, and `context_read` renders it as the paragraph a person
gets wherever they asked. One measurement, because a second one drifts, and a
panel drawing its own idea of where the boundary falls is the defect
`boundary_measurements` already exists to stop.

`gather` takes a ChatSession by duck type rather than by import: this module
sits under the session's feet and importing it back would be a cycle.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Cells in the rendered bar. Twenty reads at a glance on a phone and still
#: fits a narrow terminal.
BAR_CELLS = 20
BAR_FULL = "█"
BAR_EMPTY = "░"


def gather(cs: Any) -> dict[str, Any]:
    """Every number this conversation's shape rests on, in one pass.

    The token estimate is the spine and is allowed to raise: without it there
    is no report, and a zero would read as an empty conversation rather than
    as a failure to count one. **Every other measurement is guarded on its
    own**, because a report missing one line is worth more than no answer at
    all, and a guard over three of the five would have been that promise made
    and not kept. `render` reads every field with a default for the same
    reason.
    """
    # All three from ONE assembly. The assembly walks the whole history and
    # this runs after every turn, so asking separately was three walks; and
    # `conversation_tokens` is what the trigger is actually compared against,
    # while `tokens` is the whole payload including the parts a boundary
    # cannot clear. Reporting one against the other drew the bar over-full.
    if hasattr(cs, "measure_payload"):
        # Asked BEFORE the call, not caught after it. An `except AttributeError`
        # around the call cannot tell "this stand-in has no such method" from an
        # AttributeError raised inside it, and downgrading the second to the
        # legacy path would silently drop `conversation_tokens` and put the
        # bar back to dividing the whole payload by a conversation-only
        # trigger, which is the defect this measurement exists to fix.
        total, conversation, system_tokens = cs.measure_payload()
        report = {
            "tokens": total,
            "conversation_tokens": conversation,
            "system_tokens": system_tokens,
        }
    else:
        # A session stand-in that predates `measure_payload`. The token count
        # is the report's spine and is still allowed to raise.
        report = {"tokens": cs.token_estimate()}
        try:
            report["system_tokens"] = cs.system_prompt_tokens()
        except Exception:
            logger.exception("context report: system token count failed")
            report["system_tokens"] = 0

    try:
        report["turns"] = cs.turn_count()
    except Exception:
        logger.exception("context report: turn count failed")
        report["turns"] = len(getattr(cs, "history", [])) // 2

    try:
        report.update(cs.boundary_measurements(report["system_tokens"]))
    except Exception:
        logger.exception("context report: boundary measurement failed")

    try:
        window = report.get("context_window") or getattr(cs.options, "context_window", 0)
        report["context_window"] = window
        report["compact_threshold_ratio"] = cs.compact_threshold
        report["compact_threshold_tokens"] = int(window * cs.compact_threshold)
        report["model"] = getattr(cs.options, "model", "") or ""
    except Exception:
        logger.exception("context report: the window and the threshold failed")
    # NOT the conversation alone. `token_estimate` counts the prompt head, the
    # volatile late half and the history; `system_prompt_tokens` counts the
    # head only, on purpose ("The head only, to match what
    # `_assemble_for_turn` puts in the system message"). So the difference is
    # the turn state plus what was said, which is why the rendered label says
    # "everything else" rather than naming it.
    report["rest_tokens"] = max(report["tokens"] - report["system_tokens"], 0)
    # The tools array rides every turn as a separate field, not inside the
    # system message, so `system_tokens` alone was never the size of what goes
    # out. The Conscience payload panel prices it in; a report that left it out
    # was the reason its number and this one never agreed. `_tools_tokens` is
    # the session's own cached figure (`brain/chat.py`), the same structural
    # reader the panel calls through `request_size.measure_tools`, so this is
    # not a second measurement, it is the first one asked for what it already
    # knows. Guarded like everything past the spine: a session with no such
    # method (a stand-in, a sub-agent double) reports zero rather than raising.
    try:
        getter = getattr(cs, "_tools_tokens", None)
        report["tools_tokens"] = int(getter()) if callable(getter) else 0
    except Exception:
        logger.exception("context report: tools token count failed")
        report["tools_tokens"] = 0
    report["payload_tokens"] = report["system_tokens"] + report["tools_tokens"]
    try:
        report.update(_last_turn_cache(getattr(cs, "history", [])))
    except Exception:
        logger.exception("context report: the last turn's cache split failed")
    # The measured size of two actual requests, not the structural estimate
    # above. `history` is cleared start to finish by `ChatSession.reset()`,
    # which both `/reset` and the agent's own consolidation boundary call, so
    # scanning it fresh on every report is the reset for free: a cleared
    # conversation has no assistant message yet and both come back `None`
    # until its own first call lands.
    try:
        history = getattr(cs, "history", [])
        first = _first_request_tokens(history)
        if first is not None:
            report["first_request_tokens"] = first
        last = _last_request_tokens(history)
        if last is not None:
            report["last_request_tokens"] = last
    except Exception:
        logger.exception("context report: the measured request figures failed")
    return report


def _last_turn_cache(history: list[dict[str, Any]]) -> dict[str, int]:
    """The newest TURN's cache split, off the records the bubbles already read.

    `_meta.usage` is stamped once per model CALL, and a turn is up to eighty of
    them: every tool iteration appends its own assistant message with its own
    usage. So the last turn is every assistant message back to the operator's
    question, summed.

    It used to read only the newest message, which is one call. The cockpit's
    chip sums the turn, so the same question was answered two different ways
    depending on where it was asked, and on a channel the answer was a third of
    the truth on a three-call turn. Measured 2026-09-09: the chip said 127,744
    in over three calls; this said 42,665.

    `calls` travels with the split because the percentage cannot be read
    without it. One cold call in a three-call turn is 33%, the same cold call
    in a six-call turn is 80%, and both were mistaken for a broken cache.

    Zeros when no turn has run yet, which a renderer reads as "nothing to say"
    rather than "nothing was cached".
    """
    sent = cached = calls = 0
    for message in reversed(history):
        if not isinstance(message, dict):
            continue
        # The operator's question ends the turn. Everything after it is one
        # turn's traffic however many tool iterations it took.
        if message.get("role") == "user":
            break
        usage = (message.get("_meta") or {}).get("usage")
        if not isinstance(usage, dict):
            continue
        sent += int(usage.get("input_tokens") or 0)
        cached += int(usage.get("cached_tokens") or 0)
        calls += 1
    return {
        "last_turn_input_tokens": sent,
        "last_turn_cached_tokens": cached,
        "last_turn_calls": calls,
    }


def _first_request_tokens(history: list[dict[str, Any]]) -> int | None:
    """The provider's own `input_tokens` for this conversation's COLD START:
    the first model call since the conversation began, or since it was last
    cleared.

    `ChatSession.reset()` clears `history` down to nothing, and both `/reset`
    and the agent's own consolidation boundary call it, so the earliest
    assistant message left in `history` at any moment IS the first call of
    whatever conversation is standing now. Nothing here needs to know a
    boundary happened; the history it reads already forgot the old one.

    `None` until that first call has landed, which a renderer reads as "no
    measurement yet" rather than a real zero.
    """
    for message in history:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        usage = (message.get("_meta") or {}).get("usage")
        if isinstance(usage, dict) and usage.get("input_tokens") is not None:
            return int(usage.get("input_tokens") or 0)
    return None


def _last_request_tokens(history: list[dict[str, Any]]) -> int | None:
    """The provider's own `input_tokens` for the single most recent model
    call, not the whole turn's total `_last_turn_cache` sums.

    A turn can be several calls deep (a tool loop), and `last_turn_input_tokens`
    is right to sum all of them for the cache question it answers. This is a
    different question: what did the request the provider most recently
    priced actually weigh, on its own. `None` when the newest assistant
    message has not had usage stamped on it, same reasoning as
    `_first_request_tokens`.
    """
    for message in reversed(history):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        usage = (message.get("_meta") or {}).get("usage")
        if isinstance(usage, dict) and usage.get("input_tokens") is not None:
            return int(usage.get("input_tokens") or 0)
        return None
    return None


def boundary_ceiling(report: dict[str, Any]) -> int:
    """The number a boundary actually happens at.

    `boundary_trigger_tokens` has the manifest taken out of it, because only
    the conversation is what a boundary clears; `compact_threshold_tokens` is
    the setting applied to the whole window. The one worth telling somebody is
    the one that will fire.
    """
    return int(
        report.get("boundary_trigger_tokens")
        or report.get("compact_threshold_tokens")
        or 0
    )


def _short(n: int) -> str:
    """Thousands to one decimal, and no trailing `.0` — a window is `120k`,
    not `120.0k`, and the extra character is one more thing to read past."""
    if n < 1000:
        return str(n)
    return f"{n / 1000:.1f}".removesuffix(".0") + "k"


def _bar(ratio: float) -> str:
    filled = max(0, min(BAR_CELLS, round(ratio * BAR_CELLS)))
    return BAR_FULL * filled + BAR_EMPTY * (BAR_CELLS - filled)


def render(report: dict[str, Any]) -> str:
    """The report as the paragraph a person reads, on any surface that carries
    text. Blocks rather than a drawn widget, so a phone, a terminal and a chat
    bubble all show the same picture."""
    ceiling = boundary_ceiling(report)
    tokens = int(report.get("tokens") or 0)
    # Against the same slice the trigger governs. `tokens` is the WHOLE
    # assembly; the trigger applies to the part a boundary can clear, so
    # dividing one by the other drew the bar over-full by the size of the
    # system prompt and the turn's late half. It went unnoticed because the
    # tests that check this used an empty system prompt, which makes the two
    # identical.
    measured = int(report.get("conversation_tokens") or tokens)
    ratio = (measured / ceiling) if ceiling else 0.0

    lines = [f"Context  {_bar(ratio)}  {round(ratio * 100)}%"]
    if ceiling:
        lines.append(
            f"{_short(measured)} of {_short(ceiling)} before this "
            f"conversation is wrapped up and started again"
            f" · {report.get('turns', 0)} turns"
        )
    else:
        lines.append(
            f"{_short(tokens)} carried · {report.get('turns', 0)} turns"
            " · no window is configured, so nothing here can say when it "
            "will be wrapped up"
        )
    system_tokens = int(report.get("system_tokens") or 0)
    tools_tokens = int(report.get("tools_tokens") or 0)
    payload_tokens = int(report.get("payload_tokens") or (system_tokens + tools_tokens))
    lines.append(
        f"payload {_short(payload_tokens)} (head {_short(system_tokens)}"
        f" + tools {_short(tools_tokens)})"
        f" · everything else {_short(int(report.get('rest_tokens') or 0))}"
    )

    # The same two figures the HUD's chip shows, in the same words, so a
    # channel and the cockpit answer "what did the first request actually
    # weigh" with one answer rather than two. Both fall back to the
    # structural estimate above until their own call has happened, which is
    # why this line never has nothing to say: `payload_tokens` and
    # `rest_tokens` already default to zero everywhere else in this
    # function, and the fallback here is the same total that line already
    # computes.
    total_estimate = payload_tokens + int(report.get("rest_tokens") or 0)
    cold_start = report.get("first_request_tokens")
    latest_request = report.get("last_request_tokens")
    cold_start_label = (
        "payload at the start of this conversation, measured"
        if cold_start is not None
        else "payload at the start of this conversation, estimate"
    )
    latest_label = (
        "the last request, measured"
        if latest_request is not None
        else "the next request, estimate"
    )
    lines.append(
        f"{cold_start_label} {_short(int(cold_start if cold_start is not None else total_estimate))}"
        f" · {latest_label} {_short(int(latest_request if latest_request is not None else total_estimate))}"
    )

    sent = int(report.get("last_turn_input_tokens") or 0)
    cached = int(report.get("last_turn_cached_tokens") or 0)
    calls = int(report.get("last_turn_calls") or 0)
    if sent:
        # The same four figures the cockpit's chip carries, in the same order,
        # because this is the answer on a channel and they have to be one
        # answer. What was re-read is stated rather than left to be
        # subtracted: those tokens were not lost, they were sent again at full
        # price, and that is the number worth acting on.
        call_part = f" over {calls} call{'s' if calls != 1 else ''}" if calls else ""
        lines.append(
            f"cache {round(cached / sent * 100)}% of the last turn's input"
            f"{call_part} ({_short(cached)} of {_short(sent)},"
            f" {_short(sent - cached)} re-read)"
        )
    else:
        lines.append("cache: no turn has reported its usage yet")


    # Only when it has happened. A line that always prints is one more thing to
    # read past, and the guard trimming nothing is the normal case.
    firings = int(report.get("guard_firings") or 0)
    if firings:
        trim = int(report.get("guard_trim_tokens") or 0)
        lines.append(
            f"the size guard has trimmed this conversation {firings} "
            f"{'time' if firings == 1 else 'times'}: a single turn grew past "
            f"{_short(trim)} and tool results were dropped from it to keep it "
            "alive. One large tool result can do that on its own at any size, "
            "so it is not always the conversation. If it keeps happening, "
            "lower the boundary setting as well"
        )
    return "\n".join(lines)


__all__ = ["BAR_CELLS", "boundary_ceiling", "gather", "render"]
