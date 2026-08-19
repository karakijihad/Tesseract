"""The declared edges, checked before anything runs.

Two of the four boot checks in `_shared/stage-contract.md` live here, and both
are defects this repo has already shipped rather than hypotheticals: an
artifact nobody produces (a job read four keys its writer never wrote, for
months, on every run) and an ordering that only holds because of what o'clock
it is.

A declaration error raises. There is no degraded mode for a graph that cannot
be ordered — the alternative is running the stages in an order nobody chose.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence

from tesseract.scheduler.pipeline.stage import Stage, StageKind


class PipelineDeclarationError(Exception):
    """A stage graph that cannot be run as declared."""


def producers(stages: Iterable[Stage]) -> dict[str, tuple[str, ...]]:
    """artifact name → the stages that write it."""
    out: dict[str, list[str]] = {}
    for stage in stages:
        for name in stage.writes:
            out.setdefault(name, []).append(stage.name)
    return {name: tuple(owners) for name, owners in out.items()}


def check_producers(
    stages: Sequence[Stage], *, external_reads: frozenset[str] = frozenset()
) -> None:
    """Boot check 1 — every declared `reads` has a producer.

    The message names both sides because the usual cause is a rename on one of
    them, and a checker that only says "unsatisfied" leaves the reader to find
    which half moved.

    `external_reads` are artifacts a DIFFERENT row produces — the nightly row's
    topic routing consumes what the capture row seals. They are declared on the
    row rather than assumed, and the registry proves each one has a producer
    somewhere before any of it runs.
    """
    owned = producers(stages)
    missing = [
        (stage.name, name)
        for stage in stages
        for name in stage.reads
        if name not in owned and name not in external_reads
    ]
    if missing:
        detail = "; ".join(
            f"stage {consumer!r} reads {artifact!r}, which no stage writes"
            for consumer, artifact in missing
        )
        raise PipelineDeclarationError(f"unsatisfied pipeline reads: {detail}")


def upstreams(stages: Sequence[Stage]) -> dict[str, frozenset[str]]:
    """stage name → the stages whose output it consumes.

    Data dependencies only. `after` edges order the run but are deliberately
    absent here: this map is what the failure cascade reads, and a stage that
    merely runs later must not be skipped because the stage before it failed.
    """
    owned = producers(stages)
    return {
        stage.name: frozenset(
            owner
            for name in stage.reads
            for owner in owned.get(name, ())
            if owner != stage.name
        )
        for stage in stages
    }


def ordering_edges(stages: Sequence[Stage]) -> dict[str, frozenset[str]]:
    """stage name → everything that must run before it: data and `after`."""
    by_name = {stage.name: stage for stage in stages}
    data = upstreams(stages)
    out: dict[str, frozenset[str]] = {}
    for stage in stages:
        unknown = [name for name in stage.after if name not in by_name]
        if unknown:
            raise PipelineDeclarationError(
                f"stage {stage.name!r} declares after={unknown}, which is not in "
                "its row — ordering across rows has no meaning, they fire apart"
            )
        out[stage.name] = data[stage.name] | frozenset(stage.after)
    return out


def execution_order(
    stages: Sequence[Stage], *, external_reads: frozenset[str] = frozenset()
) -> tuple[Stage, ...]:
    """Boot check 4 — the declared edges form a DAG, in the order they run.

    Ties are broken deterministic-first, then by name: a stage that needs no
    provider should commit its output before anything waits on one, and two
    runs of the same graph must produce the same order.
    """
    by_name = {stage.name: stage for stage in stages}
    if len(by_name) != len(stages):
        counted = Counter(stage.name for stage in stages)
        dupes = sorted(name for name, n in counted.items() if n > 1)
        raise PipelineDeclarationError(f"duplicate stage names: {dupes}")
    check_producers(stages, external_reads=external_reads)

    waiting = {name: set(deps) for name, deps in ordering_edges(stages).items()}
    ordered: list[Stage] = []
    while True:
        ready = sorted(
            (name for name, deps in waiting.items() if not deps),
            key=lambda name: (by_name[name].kind is not StageKind.DETERMINISTIC, name),
        )
        if not ready:
            break
        # One at a time, re-reading readiness after each pick. Taking a whole
        # level at once would put a deterministic stage that becomes ready at
        # level 2 behind every model stage that was ready at level 1 — which is
        # precisely the hostage-taking the contract forbids. Picking singly,
        # everything a provider cannot affect has committed before anything
        # waits on one.
        name = ready[0]
        ordered.append(by_name[name])
        del waiting[name]
        for deps in waiting.values():
            deps.discard(name)
    if waiting:
        raise PipelineDeclarationError(
            "pipeline stages form a cycle: "
            + ", ".join(
                f"{name} waits on {sorted(deps)}" for name, deps in sorted(waiting.items())
            )
        )
    return tuple(ordered)


__all__ = [
    "PipelineDeclarationError",
    "check_producers",
    "execution_order",
    "ordering_edges",
    "producers",
    "upstreams",
]
