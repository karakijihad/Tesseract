"""The boot graph: layers, their validation against the registry, and the run.

`boot.yaml` owns the shape and nothing else. It cannot decide whether a
substrate is worth preparing — the runtime answers that — and it cannot
override a constraint the substrate declares in code. What it CAN do is
re-order the boot: which layers exist, what fires together, and where the
window opens.

The two rules that keep it from being re-ordered into a regression are
mechanical, not reviewed: a substrate that holds the interpreter may not sit
after the window opens, and one that sits there must have said what happens
when it is cold.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

from tesseract.boot_graph.substrate import Substrate, SubstrateRegistry

log = logging.getLogger(__name__)

FIRES = frozenset({"serial", "parallel"})


class BootGraphError(RuntimeError):
    """The graph is malformed, or contradicts what the substrates declare."""


@dataclass(frozen=True)
class Layer:
    id: str
    fires: str
    blocks_window: bool
    carries: tuple[str, ...]


@dataclass(frozen=True)
class BootReport:
    """What a run actually did, for a caller that has to answer for it.

    The runner isolates failures — one broken substrate leaves the rest
    prepared — which means a caller learns nothing from the absence of a raise.
    The controller's reload has to answer an IPC message with the names it
    rebuilt and the ones it could not, so the run says so rather than only
    logging it.
    """

    prepared: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]
    failed: tuple[tuple[str, str], ...]


def default_boot_graph_path() -> Path:
    """Canonical path to `tesseract/config/boot.yaml`.

    Resolved via `config_dir()` at call time rather than a frozen constant, so
    a `TESSERACT_HOME` change is honoured without a fresh import — the same
    reason every other config reader here does it.
    """
    from tesseract.paths import config_dir

    return config_dir() / "boot.yaml"


def load_graph(path: Path | None = None) -> tuple[Layer, ...]:
    """Parse `boot.yaml` into ordered layers. Raises loudly on any malformation.

    No defaults: a missing file, a missing key or a wrong type is a broken
    install, not something to guess at.
    """
    target = path or default_boot_graph_path()
    if not target.exists():
        raise BootGraphError(f"boot.yaml missing at {target}")
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise BootGraphError(f"boot.yaml must be a mapping, got {type(raw).__name__}")
    entries = raw.get("layers")
    if not isinstance(entries, list) or not entries:
        raise BootGraphError(f"boot.yaml needs a non-empty `layers:` list ({target})")

    layers: list[Layer] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"boot.yaml layers[{index}]"
        if not isinstance(entry, dict):
            raise BootGraphError(f"{where} must be a mapping, got {type(entry).__name__}")
        layer_id = entry.get("id")
        if not isinstance(layer_id, str) or not layer_id:
            raise BootGraphError(f"{where} needs a non-empty string `id`")
        if layer_id in seen:
            raise BootGraphError(f"boot.yaml declares layer '{layer_id}' twice")
        seen.add(layer_id)
        fires = entry.get("fires")
        if fires not in FIRES:
            raise BootGraphError(
                f"{where} ('{layer_id}') has fires={fires!r} — must be one of "
                f"{', '.join(sorted(FIRES))}"
            )
        blocks = entry.get("blocks_window")
        if not isinstance(blocks, bool):
            raise BootGraphError(
                f"{where} ('{layer_id}') needs `blocks_window` as true or false"
            )
        carries = entry.get("carries")
        if not isinstance(carries, list) or not all(isinstance(c, str) for c in carries):
            raise BootGraphError(
                f"{where} ('{layer_id}') needs `carries` as a list of substrate ids"
            )
        layers.append(
            Layer(
                id=layer_id,
                fires=fires,
                blocks_window=blocks,
                carries=tuple(carries),
            )
        )
    return tuple(layers)


def validate(layers: Iterable[Layer], registry: SubstrateRegistry) -> None:
    """Check the graph against the registry. Every condition raises; none warns.

    Called before any substrate runs, so a contradiction surfaces at boot
    rather than at the moment something needed the substrate nobody prepared.
    """
    layers = tuple(layers)

    placements: dict[str, list[str]] = {}
    for layer in layers:
        for substrate_id in layer.carries:
            if substrate_id not in registry:
                raise BootGraphError(
                    f"boot.yaml layer '{layer.id}' carries '{substrate_id}', which "
                    f"no substrate registers — the graph names something that does "
                    f"not exist"
                )
            placements.setdefault(substrate_id, []).append(layer.id)

    for substrate_id in sorted(registry.ids()):
        where = placements.get(substrate_id, [])
        if not where:
            raise BootGraphError(
                f"substrate '{substrate_id}' is registered but appears in no "
                f"boot.yaml layer — it would silently never be prepared"
            )
        if len(where) > 1:
            raise BootGraphError(
                f"substrate '{substrate_id}' appears in layers {', '.join(where)} — "
                f"it would be prepared twice, possibly concurrently"
            )

    # The boundary is positional, so it has to BE positional: one `true` run
    # followed by one `false` run. A blocking layer after a non-blocking one
    # would mean the window opened and then something started blocking it
    # again, which is the regression this whole graph exists to make
    # impossible.
    opened: str | None = None
    for layer in layers:
        if not layer.blocks_window:
            opened = opened or layer.id
            continue
        if opened is not None:
            raise BootGraphError(
                f"boot.yaml layer '{layer.id}' blocks the window, but '{opened}' "
                f"already opened it — every blocks_window: true layer must come "
                f"first, because the boundary is positional"
            )

    for layer in layers:
        if layer.blocks_window:
            continue
        for substrate_id in layer.carries:
            substrate = registry.get(substrate_id)
            if substrate.holds_gil:
                raise BootGraphError(
                    f"substrate '{substrate_id}' holds the GIL and sits in "
                    f"'{layer.id}', which is below the warm line — it would "
                    f"freeze the loop after the window opened. Move it above the "
                    f"line; the declaration is not negotiable from YAML"
                )
            if not substrate.degrade.strip():
                raise BootGraphError(
                    f"substrate '{substrate_id}' sits in '{layer.id}', below the "
                    f"warm line, with no declared degrade — say what the runtime "
                    f"does when it is cold, or move it above the line"
                )


def layers_for_reload(
    layers: Iterable[Layer], registry: SubstrateRegistry, target: str
) -> tuple[Layer, ...]:
    """The subset of the graph a reload target prepares a second time.

    A substrate says which targets re-prepare it and the default is none, so
    anything with live state — a worker pool, an in-flight lane — stays out of
    every reload by saying nothing. The layers keep their order and their
    `fires`, because a rebuild has the same dependencies the boot had.
    """
    subset: list[Layer] = []
    for layer in layers:
        carries = tuple(
            c for c in layer.carries if target in registry.get(c).reload_on
        )
        if carries:
            subset.append(replace(layer, carries=carries))
    return tuple(subset)


async def _prepare(substrate: Substrate) -> None:
    """Run one substrate's preparation, on the loop or off it as its shape says.

    Three shapes reach here, not two. A coroutine function is awaited on the
    loop. A plain callable goes to a worker thread. And a plain callable that
    RETURNS a coroutine — `lambda: _prepare_x(app)`, which is how every Mirror
    substrate is registered — is both: the call itself is trivial, and the
    coroutine it hands back is the work.

    That third shape was not handled, and the failure was silent in the worst
    way: the lambda ran in its thread, the coroutine it built was dropped
    unawaited, and every substrate logged `ready in 0.00s` having done nothing.
    The first live boot after the migration is what surfaced it.
    """
    if asyncio.iscoroutinefunction(substrate.prepare):
        await substrate.prepare()
        return
    result = await asyncio.to_thread(substrate.prepare)
    if inspect.isawaitable(result):
        await result


async def _run_one(substrate: Substrate) -> str | None:
    """Prepare one substrate. Returns the failure text, or `None` on success."""
    started = time.perf_counter()
    try:
        await _prepare(substrate)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Failure isolation is the boot's oldest invariant: one broken
        # substrate leaves the rest prepared and the backend partial-ready.
        log.exception(
            "boot: substrate %r raised — continuing partial-ready (cold: %s)",
            substrate.id, substrate.degrade or "undeclared",
        )
        return str(exc) or type(exc).__name__
    log.info("boot: %s ready in %.2fs", substrate.id, time.perf_counter() - started)
    return None


async def run_layers(
    layers: Iterable[Layer],
    registry: SubstrateRegistry,
    *,
    on_window_open: Callable[[], Any] | None = None,
) -> BootReport:
    """Prepare every substrate the graph places, layer by layer, in order.

    `on_window_open` fires once, after the last `blocks_window: true` layer
    finishes and BEFORE the first layer below the line starts. That is the
    whole point of the ordering: the operator gets the window at the earliest
    moment everything they can immediately touch is ready.
    """
    layers = tuple(layers)
    prepared: list[str] = []
    skipped: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    last_blocking = None
    for layer in layers:
        if layer.blocks_window:
            last_blocking = layer.id

    opened = False

    async def _open() -> None:
        nonlocal opened
        if opened or on_window_open is None:
            opened = True
            return
        opened = True
        result = on_window_open()
        if asyncio.iscoroutine(result):
            await result

    if last_blocking is None:
        await _open()

    for layer in layers:
        members: list[Substrate] = []
        for substrate_id in layer.carries:
            substrate = registry.get(substrate_id)
            reason = substrate.requires()
            if reason:
                log.info("boot: skipping %s — %s", substrate_id, reason)
                skipped.append((substrate_id, reason))
                continue
            members.append(substrate)

        started = time.perf_counter()
        if layer.fires == "serial":
            outcomes: list[Any] = []
            for substrate in members:
                outcomes.append(await _run_one(substrate))
        else:
            outcomes = list(
                await asyncio.gather(
                    *(_run_one(substrate) for substrate in members),
                    return_exceptions=True,
                )
            )
        for substrate, outcome in zip(members, outcomes):
            if outcome is None:
                prepared.append(substrate.id)
            else:
                # `_run_one` returns text; `gather` can also hand back a
                # cancellation, whose str() is empty.
                failed.append(
                    (substrate.id, str(outcome) or type(outcome).__name__)
                )
        log.info(
            "boot: layer %s (%s, %d of %d prepared) in %.2fs",
            layer.id, layer.fires, len(members), len(layer.carries),
            time.perf_counter() - started,
        )

        if layer.id == last_blocking:
            await _open()

    return BootReport(
        prepared=tuple(prepared),
        skipped=tuple(skipped),
        failed=tuple(failed),
    )
