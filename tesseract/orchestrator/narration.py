"""Counted facts in, one sentence out, and nothing invented on the way.

The watchman proved this shape and it is the only one this runtime uses for
prose over a record: a model is handed the facts, writes over them, and a
sentence carrying a figure the facts do not is dropped rather than published.
Structure before model. A better narrator over an unjudged record writes
fluently about a failure that had already been fixed, which is what happened on
2026-08-21.

This module is the check, lifted out of `watchman/report.py` so a second caller
does not reimplement it. The watchman keeps its own budget and its own prompt;
what it shares is the one rule that makes any of it trustworthy.

**The number check must never be relaxed.** It is the only thing that caught a
model reading the supervisor's `failures=3`, a soft limit, as the count of
failures, and passing because the digit existed somewhere in the input.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Iterable, Sequence

from tesseract.brain import tool_availability as availability
from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter, call_timeout

log = logging.getLogger(__name__)

_NUMBER = re.compile(r"\d+")

# A figure does not stop being a figure for being spelled. The digit check
# alone passed `Seven sources are failing` over a record of two, which is the
# exact failure this whole rule exists to catch, and a one line summary is
# where a model is most likely to spell one out.
#
# **`one` and `zero` are deliberately absent.** In English they are pronouns as
# often as counts — "one of them", "no one" — and flagging those would replace
# a true sentence with a list of counts for nothing. Everything from two upward
# is a count wherever it appears.
_SPELLED: dict[str, int] = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90, "hundred": 100, "thousand": 1000,
}
_SPELLED_RE = re.compile(
    r"\b(" + "|".join(sorted(_SPELLED, key=len, reverse=True)) + r")\b", re.I
)

# An ISO stamp, or the date half of one. `fact_lines` appends `(last at <iso>)`
# to nearly every line, and a date carries six or more numbers that mean a
# moment rather than a quantity. Left in, they license most small integers: a
# narration could say `21 restarts` over a record of two and pass, because 21
# was the day of the month.
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:[.,]\d+)?"
    r"(?:[+-]\d{2}:?\d{2}|Z)?)?"
)


def invented_figures(text: str, facts: list[str]) -> list[str]:
    """Every number in `text` that no fact supports, and every timestamp.

    Returned rather than merely counted, because the caller that drops a
    sentence has to be able to say WHICH figure was not observed. A narration
    that cannot be traced back to a fact is describing a runtime nobody looked
    at.
    """
    source = " ".join(facts)
    stamps = set(_TIMESTAMP.findall(source))
    bad = [s for s in _TIMESTAMP.findall(text) if s not in stamps]

    flat_source = _TIMESTAMP.sub(" ", source)
    flat_text = _TIMESTAMP.sub(" ", text)
    allowed = set(_NUMBER.findall(flat_source))
    # A fact may spell its own figure, and a narration is allowed to repeat it
    # either way round: `two` over `2 sources` and `2` over `two sources` are
    # both quoting what was observed.
    allowed |= {
        str(_SPELLED[word.lower()]) for word in _SPELLED_RE.findall(flat_source)
    }
    bad += [n for n in _NUMBER.findall(flat_text) if n not in allowed]
    bad += [
        word
        for word in _SPELLED_RE.findall(flat_text)
        if str(_SPELLED[word.lower()]) not in allowed
    ]
    return bad


def is_faithful(text: str, facts: list[str], *, budget: int) -> bool:
    """Whether `text` may be published over `facts`.

    Two rules, and only one of them is about correctness. Every figure it
    names has to be in the facts, which is the rule. And it has to fit the
    budget the caller set, which is a budget: it is the check that was
    throwing away correct reports when it was tightened, so it belongs to the
    caller rather than here.
    """
    if not text.strip():
        return False
    if len(text) > budget:
        return False
    return not invented_figures(text, facts)




async def one_line(
    facts: Sequence[str],
    chain: Iterable[tuple[ModelAdapter, AdapterOptions]],
    *,
    instruction: str,
    budget: int,
    subject: str,
) -> tuple[str, bool]:
    """Walk a chain until one adapter writes a faithful line over `facts`.

    Returns the line and whether a MODEL wrote it. The two are separate
    answers because a model may legitimately return a sentence identical to
    the facts it was given; comparing the text to decide whether one answered
    would read that as a failure and call again forever.

    Lifted out of `panel_lines._one` when a second surface needed it. Every
    branch below is a lesson the panel already paid for, and a copy of this
    loop is a second set of them:

    * an entry set aside by its breaker is skipped, under the SAME breaker
      name every other reader of that ref uses, so one `breaker_reset`
      clears it here and in the chain together. Without it an end-of-life
      entry was called 29 consecutive times, 14 of them paying a full 120s
      timeout first.
    * an empty answer is a provider failure and is counted BEFORE any success
      is recorded. Reading "the call returned" as health let an entry that
      only ever returned empty strings close its own breaker on every pass.
    * a non-empty answer IS this entry's health, whether or not the sentence
      turns out to be usable. A model that writes 154 characters is working;
      faithfulness is a different question and it is the caller's line that
      is dropped, never the provider's record.

    `("", False)` means nothing wrote one. What that should show instead is
    the caller's, because the two callers have different answers: a panel
    room shows its counted facts, and a notification keeps the body its
    template already composed.
    """
    observed = list(facts)
    prompt = "\n".join(
        [instruction, "", "--- OBSERVED ---", *(f"- {line}" for line in observed), ""]
    )
    for adapter, options in chain:
        label = f"{options.provider or '?'}/{options.model or '?'}"
        ref = _entry_ref(options)
        shut = availability.refusal(ref, f"a written line for {subject}") if ref else None
        if shut is not None:
            log.debug("narration: %s is set aside (%s)", label, shut[1])
            continue
        try:
            timeout = call_timeout(options)
        except KeyError as exc:
            log.warning("narration: %s has no timeout (%s)", label, exc)
            continue
        try:
            out = await asyncio.wait_for(
                adapter.generate(prompt, options), timeout=timeout
            )
        except asyncio.TimeoutError:
            log.warning("narration: %s timed out after %.1fs", label, timeout)
            availability.note_provider_failure(
                ref, f"no answer within {timeout:g}s", kind="no_answer"
            )
            continue
        except Exception as exc:  # noqa: BLE001 — a line may fail; a surface may not
            log.warning("narration: %s call failed (%s)", label, exc)
            availability.note_provider_failure(ref, str(exc))
            continue
        said = (out or "").strip()
        if not said:
            log.warning("narration: %s returned empty", label)
            availability.note_provider_failure(
                ref, "the model returned nothing", kind="empty_answer"
            )
            continue
        availability.note_success(ref)
        if not is_faithful(said, observed, budget=budget):
            log.warning(
                "narration: %s wrote %r over %s, which names %s that nobody "
                "observed. Dropping it",
                label, said, subject,
                invented_figures(said, observed) or "nothing, but is too long",
            )
            continue
        return said, True
    return "", False


def _entry_ref(options: AdapterOptions) -> str:
    """The catalog ref these options name, or "" when they name nothing."""
    try:
        from tesseract.kernel.tools.dependency import catalog_ref

        if not options.provider or not options.model:
            return ""
        return catalog_ref(options.tier or "api", options.provider, options.model)
    except Exception:  # noqa: BLE001 — a surface may not fail over its own gate
        log.debug("narration: no ref for %s", options.model, exc_info=True)
        return ""


__all__ = ["invented_figures", "is_faithful", "one_line"]
