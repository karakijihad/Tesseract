"""What this conversation costs, measured once and written once.

The numbers were reachable from exactly one place: `emit_stats` measured them
and pushed a WebSocket envelope, so the cockpit's HUD could draw a bar and
nothing else could ask. Not a channel, not the assistant, not the cockpit's own
chat. "How much room is left before you forget the start of this?" had no
answer on any surface, which is the same shape `autonomy_read` was built for.

So the measuring lives here, on its own, and has two readers: `emit_stats`
wraps it for the HUD, and `context_read` renders it as the paragraph a person
gets wherever they asked. One measurement, because a second one drifts, and a
panel drawing its own idea of the fold is the defect `fold_measurements`
already exists to stop.

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
    # `foldable_tokens` is what the fold trigger is actually compared against,
    # while `tokens` is the whole payload including the parts a fold cannot
    # touch. Reporting one against the other drew the bar over-full.
    if hasattr(cs, "measure_payload"):
        # Asked BEFORE the call, not caught after it. An `except AttributeError`
        # around the call cannot tell "this stand-in has no such method" from an
        # AttributeError raised inside it, and downgrading the second to the
        # legacy path would silently drop `foldable_tokens` and put the bar back
        # to dividing the whole payload by a foldable-only trigger, which is the
        # defect this measurement exists to fix.
        total, foldable, system_tokens = cs.measure_payload()
        report = {
            "tokens": total,
            "foldable_tokens": foldable,
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
        report.update(cs.fold_measurements(report["system_tokens"]))
    except Exception:
        logger.exception("context report: fold measurement failed")

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
    try:
        report.update(_last_turn_cache(getattr(cs, "history", [])))
    except Exception:
        logger.exception("context report: the last turn's cache split failed")
    return report


def _last_turn_cache(history: list[dict[str, Any]]) -> dict[str, int]:
    """The newest turn's cache split, off the record the bubbles already read.

    `_meta.usage` is stamped by the turn loop and is what the Mirror's own
    "cache in / cached" pill renders, so this is the same number the operator
    can already see beside one message rather than a second accounting of it.
    Zeros when no turn has run yet, which a renderer reads as "nothing to say"
    rather than "nothing was cached".
    """
    for message in reversed(history):
        usage = (message.get("_meta") or {}).get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            continue
        cached = int(usage.get("cached_tokens") or 0)
        sent = int(usage.get("input_tokens") or 0)
        return {"last_turn_input_tokens": sent, "last_turn_cached_tokens": cached}
    return {"last_turn_input_tokens": 0, "last_turn_cached_tokens": 0}


def fold_ceiling(report: dict[str, Any]) -> int:
    """The number a fold actually happens at.

    `fold_trigger_tokens` is measured against the turns that exist and carries
    the floor the runtime enforces; `compact_threshold_tokens` is the setting
    alone. They differ the first time a turn runs heavy, and the one worth
    telling somebody is the one that will fire.
    """
    return int(report.get("fold_trigger_tokens") or report.get("compact_threshold_tokens") or 0)


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
    ceiling = fold_ceiling(report)
    tokens = int(report.get("tokens") or 0)
    # Against the same slice the trigger governs. `tokens` is the WHOLE
    # assembly; the trigger applies to the part a fold can remove, so dividing
    # one by the other drew the bar over-full by the size of the system prompt
    # and the turn's late half. It went unnoticed because the tests that check
    # this used an empty system prompt, which makes the two identical.
    measured = int(report.get("foldable_tokens") or tokens)
    ratio = (measured / ceiling) if ceiling else 0.0

    lines = [f"Context  {_bar(ratio)}  {round(ratio * 100)}%"]
    if ceiling:
        lines.append(
            f"{_short(measured)} of {_short(ceiling)} before it folds"
            f" · {report.get('turns', 0)} turns"
        )
    else:
        lines.append(
            f"{_short(tokens)} carried · {report.get('turns', 0)} turns"
            " · no window is configured, so nothing here can say when it folds"
        )
    lines.append(
        f"manifest {_short(int(report.get('system_tokens') or 0))}"
        f" · everything else {_short(int(report.get('rest_tokens') or 0))}"
    )

    sent = int(report.get("last_turn_input_tokens") or 0)
    cached = int(report.get("last_turn_cached_tokens") or 0)
    if sent:
        lines.append(
            f"cache {round(cached / sent * 100)}% of the last turn's input"
            f" ({_short(cached)} of {_short(sent)})"
        )
    else:
        lines.append("cache: no turn has reported its usage yet")

    tail_turns = int(report.get("tail_turns") or 0)
    tail_tokens = int(report.get("tail_tokens") or 0)
    anchor = int(report.get("head_anchor_tokens") or 0)
    if tail_turns or anchor:
        lines.append(
            f"a fold keeps the last {tail_turns} turns ({_short(tail_tokens)})"
            f" and the opening ({_short(anchor)}); everything between them"
            " becomes a summary"
        )

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
            "lower the fold setting as well"
        )
    return "\n".join(lines)


__all__ = ["BAR_CELLS", "fold_ceiling", "gather", "render"]
