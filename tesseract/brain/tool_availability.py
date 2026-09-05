"""Whether the thing behind a tool is answering, asked at the one funnel.

Every tool call goes through `brain/tools.py::_dispatch_tool`, so one gate
there covers the whole roster, including a tool written after this was. What
keeps that from being the naive version — a counter on the tool name, which
opens on the model's bad input and takes a working tool off the board — is
that **the breaker is keyed on the DEPENDENCY, not the tool**, and a tool with
no dependency is never gated and never counted at all.

`kernel/tools/dependency.py` resolves the declaration to a catalog ref, so a
provider added to `providers.yaml` names its own breaker and nothing registers
anything. `context/circuit_breaker.py::held_breaker` builds it on first ask,
which puts it where `breaker_status` and `breaker_reset` already look — so a
seat nobody has ever heard of is answerable from a phone the moment it fails.

**The caller-versus-dependency split, in the order it is asked:**

  1. No dependency, no opinion. `depends_on: ""` means the tool's failures are
     its caller's. `file_read` saying *File not found* is the model calling it
     wrong, and it never reaches this module.
  2. A structured fault wins. A tool that knows what it hit puts a
     `ProviderFault` in `result.metadata["fault"]`, and its `kind` is the
     answer. This is the path worth widening; everything below is inference.
  3. A raised exception counts only where the shared vocabulary places it.
     `FileNotFoundError`, `KeyError` and `ValueError` out of a tool that
     forgot to catch its own input error stay `unknown` and stay uncounted.
  4. A returned error with no fault counts only for the four kinds a
     provider's own sentence can say and a tool's cannot plausibly mean
     something else by. `not_found` is deliberately NOT among them: a tool's
     own sentence about a missing page or row would otherwise open a healthy
     ref, and the safe half of two wrong answers is the one that keeps a
     working dependency on the board.

**A quota belongs to an account.** `usage` and `auth` are recorded against
`dependency.account_of(ref)`, so an exhausted subscription shuts every seat on
it rather than one model while a second role keeps spending. Every other kind
stays on the full ref, because `not_found` and `schema` really are about the
one entry. The gate asks both names for the same reason.

**Nothing here may fail a turn.** A telemetry surface that raises inside the
funnel would turn an outage in the thing that watches for outages into an
outage in every tool. Both entry points swallow.
"""

from __future__ import annotations

import logging
from typing import Any

from tesseract.kernel.tools.base import ToolResult

logger = logging.getLogger(__name__)

#: What a bare `is_error` sentence may be counted as. The narrow half of
#: `CLASSIFIED_KINDS` — see rule 4 in the module docstring for why `not_found`
#: and `schema` are missing rather than forgotten.
COUNTED_FROM_TEXT = frozenset({"usage", "auth", "rate_limit", "server"})

#: What a structured fault or a placed exception may be counted as. Wider,
#: because the caller that built it knew what it hit rather than guessing.
COUNTED_FROM_FAULT = frozenset(
    {"usage", "auth", "not_found", "rate_limit", "server", "no_answer"}
)

#: The metadata key a tool sets to name what it ACTUALLY reached, when that is
#: not what it declared. A seat names the CLI that fills it by default and a
#: call may borrow the other one; the declaration is the default and this is
#: the correction.
REF_KEY = "ref"

#: The metadata key carrying a `ProviderFault` (or its `evidence()` dict).
FAULT_KEY = "fault"


def _check_every_counted_kind_has_a_window() -> None:
    """Every kind this module can count must have a cooldown window.

    `record` resolves the window inside a `try` that swallows, because
    telemetry may not break a turn — which would turn `window_class_for_fault`
    raising on an unplaced kind, the guarantee that stops a new kind going
    quiet, into exactly the silence it exists to prevent. So the two lists are
    checked against each other HERE, at import, where a mismatch is loud.
    """
    # Through the PUBLIC accessor, one kind at a time. Reaching for
    # `_WINDOW_CLASS` itself would put a private name on the import path of
    # `brain/tools.py`, so renaming it would stop the runtime booting rather
    # than fail a test.
    from tesseract.orchestrator.provider_failure import window_class_for_fault

    unplaced = []
    for kind in sorted(COUNTED_FROM_FAULT | COUNTED_FROM_TEXT):
        try:
            window_class_for_fault(kind)
        except KeyError:
            unplaced.append(kind)
    if unplaced:
        raise RuntimeError(
            f"tool_availability counts {unplaced} but provider_failure places "
            "no cooldown window for them. Add a row to `_WINDOW_CLASS`, or "
            "stop counting the kind."
        )


_check_every_counted_kind_has_a_window()


def _windows() -> tuple[float, float]:
    """`(self_clearing, needs_a_person)` from `providers.yaml::availability`.

    Raises on a missing key, deliberately: a window nobody set is a breaker
    that heals on a number this file invented.
    """
    from tesseract.config.loader import load_config

    block = load_config().availability
    return (
        float(block["cooldown_seconds"]),
        float(block["cooldown_seconds_until_fixed"]),
    )


def doors_for(ref: str) -> list[Any]:
    """Every breaker that can hold this ref shut, nearest first.

    Three, and they are not interchangeable. The ENTRY's own breaker, the
    ACCOUNT's (a subscription is spent per account, so one seat being out
    shuts every seat on it), and any FOREIGN breaker registered under either
    name. The chain registers its per-entry cooldown as a foreign breaker, so
    without that last one the funnel would let a call through to an entry the
    chain had already given up on, under a name they now share.
    """
    from tesseract.context.circuit_breaker import foreign_breakers, held_breaker
    from tesseract.kernel.tools.dependency import account_of

    if not ref:
        return []
    names = [ref]
    account = account_of(ref)
    if account != ref:
        names.append(account)
    doors: list[Any] = []
    for name in names:
        doors.append(held_breaker(name))
        doors.extend(foreign_breakers(name))
    return doors


def refusal(ref: str, subject: str) -> tuple[str, str] | None:
    """`(breaker name, what it says)` when this ref is shut, else None.

    The one refusal check. `allow` records what was turned away, so asking is
    not free and the caller must act on the answer rather than ask twice.
    """
    # The two kinds answer different questions and only one of them keeps a
    # ledger. A `CircuitBreaker` is asked with `allow`, which records what was
    # turned away. A foreign breaker (the chain's per-entry cooldown) has no
    # `allow` at all — its interface is `is_open_now`/`says`/`close` — so it is
    # READ, not asked. Calling `allow` on one raised, and because this whole
    # check swallows, the raise would have read as "nothing is shut": the gate
    # would have silently stopped working the moment a chain breaker existed
    # under the same name.
    try:
        for door in doors_for(ref):
            allow = getattr(door, "allow", None)
            if callable(allow):
                shut = not allow(subject)
            else:
                shut = bool(door.is_open_now())
            if shut:
                return (getattr(door, "name", "") or getattr(door, "ref", "") or ref, door.says())
    except Exception:  # noqa: BLE001 — a gate may not fail the work it guards
        logger.debug("availability: refusal check raised for %s", ref, exc_info=True)
    return None


def note_success(ref: str) -> None:
    """It answered. Close whatever was holding it, entry and account both.

    Foreign breakers included: the chain owns its own success bookkeeping on
    its own path, and closing one here on a call that really did reach the
    provider is the same fact arriving by another door.
    """
    try:
        for door in doors_for(ref):
            record = getattr(door, "record_success", None)
            if callable(record):
                record()
    except Exception:  # noqa: BLE001 — telemetry never breaks the work
        logger.debug("availability: success for %s", ref, exc_info=True)


def note_provider_failure(
    ref: str,
    detail: str,
    *,
    kind: str | None = None,
    status: int | None = None,
) -> None:
    """Count one failure against the right door, on the window its kind takes.

    `kind` None reads it out of `detail` with the shared classifier, and
    `status` is handed to it because an HTTP code says what a body sometimes
    cannot: a 429 with an unhelpful sentence is still a rate limit.

    `usage` and `auth` are recorded against the ACCOUNT, everything else
    against the entry, which is the same rule the tool funnel applies and was
    the thing the per-entry callers were bypassing.
    """
    if not ref:
        return
    try:
        from tesseract.context.circuit_breaker import held_breaker
        from tesseract.kernel.tools.dependency import account_of
        from tesseract.orchestrator.provider_failure import (
            NEEDS_A_PERSON,
            classify,
            window_class_for_fault,
        )

        placed = kind or classify(detail, status=status)
        target = account_of(ref) if placed in ("usage", "auth") else ref
        breaker = held_breaker(target)
        self_clearing, until_fixed = _windows()
        breaker.cooldown_seconds = (
            until_fixed if window_class_for_fault(placed) == NEEDS_A_PERSON else self_clearing
        )
        breaker.record_failure(f"{placed}: {detail}")
    except Exception:  # noqa: BLE001 — telemetry never breaks the work
        logger.debug("availability: could not record %s", ref, exc_info=True)


def dependency_of(tool: Any) -> str | None:
    """The breaker name behind `tool`, or None when nothing is behind it.

    Never raises: boot already refused an unresolvable declaration
    (`check_tool_contract`), so a failure here means config changed under a
    running process, and the honest answer to "is this dependency down" is
    then "no opinion" rather than a dead turn.
    """
    from tesseract.kernel.tools.dependency import resolve

    try:
        return resolve(getattr(type(tool), "depends_on", ""))
    except Exception:  # noqa: BLE001 — a gate must not fail
        logger.debug("availability: could not resolve %s", tool, exc_info=True)
        return None


def _breakers(ref: str) -> list[Any]:
    """The breaker for this ref and the one for its account, nearest first."""
    from tesseract.context.circuit_breaker import held_breaker
    from tesseract.kernel.tools.dependency import account_of

    names = [ref]
    account = account_of(ref)
    if account != ref:
        names.append(account)
    return [held_breaker(name) for name in names]


def gate(tool: Any, tool_name: str) -> ToolResult | None:
    """Refuse the call when what it needs is not answering. None means go.

    Asked BEFORE the permission prompt, which is why it returns a result
    rather than raising: asking the operator on a phone to approve a call the
    runtime is about to refuse is the confusing half of a good mechanism.
    """
    ref = dependency_of(tool)
    if ref is None:
        return None
    shut = refusal(ref, f"tool call {tool_name}")
    if shut is None:
        return None
    name, says = shut
    return ToolResult(
        output=(
            f"{tool_name} is not being tried right now: {name} {says} If it "
            f"is fixed, clear it with breaker_reset {name} and ask again."
        ),
        is_error=True,
        metadata={"reason": "breaker_open", "ref": name},
    )


def _fault_kind(result: ToolResult | None, exc: BaseException | None) -> str | None:
    """The kind this outcome counts as, or None when it counts as nothing."""
    from tesseract.orchestrator.provider_failure import classify, from_exception

    if exc is not None:
        kind = from_exception(exc).kind
        return kind if kind in COUNTED_FROM_FAULT else None

    if result is None or not result.is_error:
        return None

    metadata = result.metadata or {}
    fault = metadata.get(FAULT_KEY)
    kind = None
    if isinstance(fault, dict):
        kind = fault.get("kind")
    else:
        kind = getattr(fault, "kind", None)
    if isinstance(kind, str):
        return kind if kind in COUNTED_FROM_FAULT else None

    if result.timed_out:
        return "no_answer"

    read = classify(result.output)
    return read if read in COUNTED_FROM_TEXT else None


def record(
    tool: Any,
    tool_name: str,
    result: ToolResult | None = None,
    exc: BaseException | None = None,
) -> None:
    """Count one outcome against the dependency, when it was the dependency's.

    Success closes the breaker; a placed failure opens it on the window its
    kind takes. A failure this module cannot place changes nothing, which is
    invariant 4: never count a failure you did not cause.
    """
    ref = dependency_of(tool)
    # What it actually reached outranks what it declared. A seat names the CLI
    # that fills it by default and a call may borrow the other one; counting
    # the borrowed failure against the default would shut the wrong account.
    named = (result.metadata or {}).get(REF_KEY) if result is not None else None
    if isinstance(named, str) and named:
        ref = named
    if ref is None:
        return
    kind = _fault_kind(result, exc)
    if kind is None:
        # A tool that returned an error we cannot place is not evidence about
        # the provider, but a tool that SUCCEEDED is: it reached the thing and
        # the thing answered.
        if exc is None and result is not None and not result.is_error:
            note_success(ref)
        return
    detail = ""
    if exc is not None:
        detail = f"{type(exc).__name__}: {exc}"
    elif result is not None:
        detail = result.output
    note_provider_failure(ref, detail, kind=kind)


__all__ = [
    "COUNTED_FROM_FAULT",
    "COUNTED_FROM_TEXT",
    "FAULT_KEY",
    "REF_KEY",
    "dependency_of",
    "doors_for",
    "gate",
    "note_provider_failure",
    "note_success",
    "record",
    "refusal",
]
