"""What the runtime may fix about itself, declared in one list.

Every phase title in this plan is about SEEING, and `autonomy/recovery.py` says
the rest out loud: it writes up the one error and waits for an answer, because
retrying is not a decision and everything else is. That is right for a denial
that cost something. It is not the whole story for a capability that is simply
off and would come back if anything asked.

Four failures in one day, 2026-08-29, every one of them seen and none acted on.
The one this module exists for: vector search had been off since boot, the
bundle held ``None``, ``do_refresh_memory`` existed and had no caller anywhere
in the tree, and nothing noticed for a day.

**The rule, from the operator: anything that breaks gets a breaker, a heal, and
an answer in words.** So:

- **Declared.** A repair is a row here, and the row says what broke and why it
  is safe to do unasked. An operator reading the list is the review this
  authority gets, so a row that cannot say why in one sentence does not belong.
- **Bounded.** Each repair gets a real ``CircuitBreaker``, named
  ``repair:<key>``. A heal that runs every tick against something it cannot fix
  is the retry loop this runtime bans everywhere else wearing a nicer word. It
  being a real breaker is also what gives it the controls every breaker has:
  ``breaker_status`` names it and ``breaker_reset`` clears it, from a phone.
- **Never quiet.** Every attempt leaves a line saying what it tried and what
  came of it, whichever way it went.

What is deliberately NOT here: anything that changes a cap, a config value, a
line of code, or spends money. Those are decisions, they go through
``recovery.py`` and the agenda, and the operator answers them.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: The house ceiling for any retry loop. Three consecutive failures and the
#: repair's breaker opens, which is the same number every other loop here uses.
MAX_CONSECUTIVE_FAILURES = 3

#: What the breaker is called, so the name the operator clears is derivable
#: from the name the row declares and neither has to be looked up.
BREAKER_PREFIX = "repair:"

# Re-exported so a caller building a variant row does not have to know that a
# `Repair` is a dataclass.
__all__ = [
    "BREAKER_PREFIX",
    "MAX_CONSECUTIVE_FAILURES",
    "REPAIRS",
    "Attempt",
    "Repair",
    "attempts_path",
    "reset_repair_breakers",
    "replace",
    "run_repairs",
]


@dataclass(frozen=True)
class Repair:
    """One thing the runtime may put right about itself, without asking.

    ``still_broken`` and ``attempt`` both take the backend app (or ``None``
    where there is not one) and neither may raise for a reason the caller has
    to guess at: raising is allowed and is recorded, but the two are recorded
    DIFFERENTLY. Not knowing whether a thing is broken and trying to fix it and
    failing are different answers, and reporting the second for the first would
    have the operator reading about a repair that never ran.
    """

    #: Lowercase, stable, and the operator-facing half of the breaker name.
    key: str
    #: What this repair does, in words, for a list a person reads.
    title: str
    #: What the operator would otherwise have been living with.
    what_broke: str
    #: Why doing this without being asked is safe. One sentence. A row that
    #: needs a paragraph is a decision, and decisions go to the agenda.
    why_unasked: str
    still_broken: Callable[[Any], Awaitable[bool]]
    attempt: Callable[[Any], Awaitable[str]]


@dataclass(frozen=True)
class Attempt:
    """What came of one repair on one pass.

    ``outcome`` is one of five words and they are the words that go on a
    screen: ``repaired``, ``failed``, ``nothing to do``, ``could not tell``,
    ``held``.
    """

    key: str
    title: str
    outcome: str
    said: str
    at: str


def attempts_path() -> Path:
    """Where the record lives. Resolved at call time, like every other writer
    in this runtime, so a test that isolates ``TESSERACT_HOME`` gets its own."""
    from tesseract.paths import log_dir

    return log_dir("repairs") / "attempts.jsonl"


def _record(attempt: Attempt) -> None:
    """One line per attempt, and never a reason not to write one.

    Best effort in the same sense a memory write is not: the repair has already
    happened when this runs, so a failure here loses the record and not the
    work. It still says so, because a heal nobody can read about is the silence
    this module exists to end.
    """
    try:
        path = attempts_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(attempt.__dict__, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("repairs: could not record the %s attempt", attempt.key)
    try:
        from tesseract.orchestrator.background_event_bus import get_background_bus

        get_background_bus().publish("repair_attempt", dict(attempt.__dict__))
    except Exception:  # noqa: BLE001 — a bus nobody is listening on is not a failure
        log.debug("repairs: nothing was listening for the %s attempt", attempt.key)


#: One breaker per repair, held for the life of the process, because that is
#: the natural scope of "this repair has been failing". Building a fresh one
#: per pass reset its count every tick, so a repair that could never work would
#: have been attempted for ever and the breaker would have been decoration.
#:
#: Keyed by the log directory as well as the repair, so a process whose
#: `TESSERACT_HOME` moves --- which in practice means a test --- gets its own
#: rather than a breaker still counting against somebody else's machine.


def reset_repair_breakers() -> None:
    """Drop every held breaker. For tests, and for the same reason
    `brain.tools.reset_tokenjuice_cache` exists.

    A strong process-lifetime cache is right in production, where the home
    never moves. In a test suite it keeps breakers built against one
    `TESSERACT_HOME` alive in `circuit_breaker._LIVE`, which is keyed by NAME
    and not by home, so an unrelated test's `breaker_report` finds rows
    belonging to a machine it has never looked at.
    """
    from tesseract.context.circuit_breaker import reset_held_breakers

    reset_held_breakers()


def _breaker(key: str):
    """This repair's breaker. The cache and its home-moved eviction live in
    `circuit_breaker.held_breaker`, which was lifted out of here when the tool
    funnel needed the same thing."""
    from tesseract.context.circuit_breaker import held_breaker
    from tesseract.paths import log_dir

    return held_breaker(
        f"{BREAKER_PREFIX}{key}",
        log_dir=log_dir("circuit-breakers"),
        max_failures=MAX_CONSECUTIVE_FAILURES,
    )


#: The outcomes that leave a line behind. Not `nothing to do`, which is the
#: overwhelmingly common pass and would bury the lines that matter under a file
#: of them. And not `held`, which is the subtler one: a repair that has given
#: up is checked again on every tick for as long as it stays given up, so
#: recording it each time would grow this file without bound in exactly the
#: state it exists to report --- and it is declared KEPT, so nothing sweeps it.
#: The breaker IS that record: its trip is in `circuit-breakers/`,
#: `breaker_status` names it, and the attempt still reaches the caller.
_WORTH_RECORDING = frozenset({"repaired", "failed", "could not tell"})


async def _run_one(repair: Repair, app: Any) -> Attempt:
    now = datetime.now(timezone.utc).isoformat()

    def made(outcome: str, said: str) -> Attempt:
        return Attempt(
            key=repair.key, title=repair.title, outcome=outcome, said=said, at=now
        )

    breaker = _breaker(repair.key)
    if breaker.is_tripped:
        # Recorded, not skipped in silence. A repair that has given up is the
        # single most important thing on this list to be able to read, and it
        # is the one an operator can act on: `breaker_reset repair:<key>`.
        return made(
            "held",
            f"stopped trying after {MAX_CONSECUTIVE_FAILURES} failures. "
            f"Clear it with breaker_reset {BREAKER_PREFIX}{repair.key} once the "
            f"cause is gone",
        )

    try:
        broken = await repair.still_broken(app)
    except Exception as exc:  # noqa: BLE001
        log.warning("repairs: %s could not be checked", repair.key, exc_info=True)
        return made("could not tell", f"{type(exc).__name__}: {exc}")

    if not broken:
        # No record and no breaker traffic: the overwhelmingly common pass is
        # everything working, and writing a line for it every tick would bury
        # the lines that matter under a file of them.
        return made("nothing to do", f"{repair.what_broke} is not the case now")

    try:
        said = await repair.attempt(app)
    except Exception as exc:  # noqa: BLE001
        breaker.record_failure(f"{type(exc).__name__}: {exc}")
        return made("failed", f"{type(exc).__name__}: {exc}")

    breaker.record_success()
    return made("repaired", said)


async def run_repairs(app: Any, declared: "tuple[Repair, ...] | None" = None) -> list[Attempt]:
    """Attempt every declared repair whose subject is still broken.

    Sequential on purpose, and it is the one place in this runtime where that
    is the right answer: repairs act on the runtime itself, the list is short,
    and two of them rebuilding overlapping state at once is a race nobody asked
    for. Parallel-by-default is about work that is independent, and these are
    not independent of the thing they are all running inside.

    One failing never stops the rest --- each is caught in `_run_one`, which
    returns an outcome rather than raising, so a caller gets a full list or an
    empty one and never a half.
    """
    out: list[Attempt] = []
    for repair in declared if declared is not None else REPAIRS:
        attempt = await _run_one(repair, app)
        if attempt.outcome in _WORTH_RECORDING:
            _record(attempt)
        out.append(attempt)
    return out


# ── The declared repairs ──────────────────────────────────────────────
#
# One row each, and each row is the whole argument for letting the runtime do
# it unasked.


async def _vector_index_is_off(app: Any) -> bool:
    """Whether memory retrieval is running BM25-only.

    `MemoryBundle.embeddings` is `None` exactly when the configured Ollama
    endpoint was unreachable at build time, and `build_memory_bundle` degrades
    to BM25 rather than failing, which is right and is also why nothing ever
    said so.

    No bundle at all is NOT broken. Boot has not finished, or this is a
    process with no memory in it, and rebuilding what was never built would be
    a claim about a boot that has not happened.
    """
    bundle = app.get("memory_bundle") if hasattr(app, "get") else None
    if bundle is None:
        return False
    return getattr(bundle, "embeddings", None) is None


async def _reattach_vector_index(app: Any) -> str:
    """Rebuild the memory bundle so a mid-session Ollama start is picked up.

    `do_refresh_memory` has done exactly this since it was written and had no
    caller anywhere in the tree, which is how vector search stayed off for a
    day with every piece of the fix already on disk.

    Idempotent, local, and spends nothing: it re-reads the store and asks the
    configured endpoint whether it answers. If Ollama is still down the new
    bundle degrades to BM25 exactly as the old one did, and this says so
    rather than claiming a fix.
    """
    import asyncio

    from tesseract.brain.session_ops import do_refresh_memory

    registry = app.get("tool_registry") if hasattr(app, "get") else None
    if registry is None:
        raise RuntimeError("there is no tool registry to register the rebuilt tools on")

    bundle = await asyncio.to_thread(do_refresh_memory, registry)
    if getattr(bundle, "embeddings", None) is None:
        raise RuntimeError(
            "rebuilt the memory bundle and the embedding endpoint still did not answer, "
            "so search is still BM25-only"
        )
    app["memory_bundle"] = bundle
    return "the embedding index answered again, so memory search is back to full retrieval"


async def _a_substrate_is_missing(app: Any) -> bool:
    """Whether this boot left something out.

    `app["boot_failed"]` is what `run_layers` reported and nothing else writes
    it, so an empty mapping is a clean boot and a missing key is a process that
    has not booted through the graph at all. Both are "nothing to do", and only
    the second could ever be mistaken for one: a controller daemon or a test
    app has no failures to retry because it has no report.
    """
    failed = app.get("boot_failed") if hasattr(app, "get") else None
    return bool(failed)


async def _prepare_the_missing_again(app: Any) -> str:
    """Prepare the isolated substrates again, in boot order.

    Idempotent by the substrates' own contract: preparing one twice is what
    every reload target already does. Spends nothing and changes no setting,
    which is why it may run unasked, and it either brings a capability back or
    leaves the runtime exactly as partial as it already was.

    Raising when nothing came back is deliberate: that is what counts a failure
    against this repair's breaker, so a substrate that can never prepare stops
    being retried after three sweeps and becomes a fault the operator is told
    about instead.
    """
    from tesseract.boot_graph import retry_failed

    layers = app.get("boot_layers") if hasattr(app, "get") else None
    registry = app.get("boot_registry") if hasattr(app, "get") else None
    if layers is None or registry is None:
        raise RuntimeError(
            "this process has no boot graph, so there is nothing to prepare again"
        )
    failed = dict(app.get("boot_failed") or {})
    report = await retry_failed(layers, registry, failed.keys())
    for name in report.prepared:
        failed.pop(name, None)
    for name, _reason in report.skipped:
        failed.pop(name, None)
    for name, reason in report.failed:
        failed[name] = reason
    app["boot_failed"] = failed

    if not report.prepared and not report.skipped:
        raise RuntimeError(
            "prepared "
            + ", ".join(sorted(name for name, _ in report.failed))
            + " again and it still will not come up: "
            + "; ".join(f"{name}: {reason}" for name, reason in report.failed)
        )
    # What is STILL down is said in every branch that has one. This sentence
    # reaches the operator through `read_repairs`, and the skipped-only branch
    # used to return a clean resolution while another substrate was still
    # broken, because the `if failed:` below it could not be reached from there.
    still = f", and {', '.join(sorted(failed))} is still down" if failed else ""
    back = ", ".join(sorted(report.prepared))
    if not report.prepared:
        return (
            "nothing needs "
            + ", ".join(sorted(name for name, _ in report.skipped))
            + " on this machine, so it is no longer counted as missing"
            + still
        )
    if failed:
        return f"{back} came back without a restart{still}"
    return f"{back} came back without a restart, so the runtime is whole again"


REPAIRS: tuple[Repair, ...] = (
    Repair(
        key="boot_substrates",
        title="Prepare what the boot left out",
        what_broke=(
            "Part of the runtime never started, because one substrate raised "
            "while the rest of the boot carried on without it"
        ),
        why_unasked=(
            "it runs the same preparation the boot already ran, in the same "
            "order, spending nothing and changing no setting"
        ),
        still_broken=_a_substrate_is_missing,
        attempt=_prepare_the_missing_again,
    ),
    Repair(
        key="memory_vector_index",
        title="Re-attach the embedding index",
        what_broke=(
            "Memory search running on keywords alone, because the embedding "
            "endpoint was not answering when the runtime started"
        ),
        why_unasked=(
            "it re-reads state the runtime already has and asks one local "
            "endpoint whether it answers, spending nothing and changing no setting"
        ),
        still_broken=_vector_index_is_off,
        attempt=_reattach_vector_index,
    ),
)
