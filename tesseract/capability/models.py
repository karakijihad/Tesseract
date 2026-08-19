"""The four model lanes, resolved from config and judged against their pins.

Every lane answers the same two questions — are the files here, and are they
the ones this version names — but each finds its pin somewhere different, and
that is a fact about the catalog rather than a wrinkle worth abstracting away:

- **whisper** pins per checkpoint, under the entry's ``downloads:`` map, so
  switching model size switches pin.
- **kokoro** pins once on the *connection*, because every catalogued voice is
  a mix over the same two files.
- **reranker** pins on the model entry, beside the filenames it loads.

Resolution reuses the same public helpers the fetch scripts use —
``configured_refs``, ``parse_download_block``, ``lane_dir`` — rather than
re-deriving paths. A checker that looked somewhere the fetcher does not write
would report a healthy install as empty.

**An unconfigured lane is not a fault.** ``configured_refs`` returns nothing
when the operator's config does not name an engine, which is what declining a
lane at setup produces. That is reported as absent-and-not-wanted, never as a
problem to fix.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from tesseract.capability import pins
from tesseract.capability.state import (
    Consent,
    ConsentOrigin,
    DependencyRecord,
    DependencyState,
    VerifiedPin,
)

logger = logging.getLogger(__name__)

#: Where each optional artifact's `download_mb` sits in `providers.yaml`.
#:
#: Paths into config, never figures. The numbers live beside the pins they
#: describe, so re-measuring one is a catalog edit and nothing here moves —
#: which is the whole reason this replaced a literal that a second copy in
#: `splash.html` had to be kept in step with by a test.
#:
#: `max_download_mb` is deliberately not used for this: it is a REFUSAL CAP
#: (2048 against a 1,622 MB whisper) and quoting it would tell the operator a
#: 1.6 GB model is a 2 GB one.
#:
#: Whisper is absent because it pins per checkpoint — `_whisper_download_mb`
#: resolves the one this machine was actually given.
_SIZE_PATHS: dict[str, tuple[str, ...]] = {
    "kokoro": ("local", "kokoro", "download"),
    "embeddings": ("local", "ollama"),
    "browser": ("services", "browser"),
}

#: State precedence when a lane has several pins and they disagree. `stale`
#: outranks everything: a wrong artifact is loaded and producing wrong
#: behaviour right now, where a missing one is a capability that is merely
#: off. `unknown` outranks `ok` so a lane nobody could check never reports
#: healthy.
_SEVERITY = {
    DependencyState.STALE: 3,
    DependencyState.ABSENT: 2,
    DependencyState.UNKNOWN: 1,
    DependencyState.OK: 0,
}


#: What each optional artifact is CALLED to the operator, keyed the way
#: `download_sizes_mb` prices it.
#:
#: One place, because a capability can be turned on in two: the setup form
#: offers it, and Settings → Capabilities offers it again months later. Named
#: differently in the two, they read as two different things.
DOWNLOAD_LABELS: dict[str, str] = {
    "whisper": "Speech recognition",
    "kokoro": "The local voice",
    "embeddings": "Semantic search",
    "reranker": "Better ranking",
    "browser": "Reading web pages",
}


def _dig(raw: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = raw
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def _as_mb(value: Any) -> int | None:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return None
    return size if size > 0 else None


def _whisper_download_mb(raw: Mapping[str, Any], checkpoint: str | None) -> int | None:
    """The size of one checkpoint — the catalog's, or the one asked for.

    `provision_hardware` writes `model:` per machine before anything is
    fetched, so the entry has to be read rather than a fixed key looked up.
    Reading the largest pin is what made the setup form quote 1,600 MB of
    speech recognition to a laptop that was about to download 148.
    """
    models = _dig(raw, ("local", "whisper", "models"))
    if not isinstance(models, Mapping):
        return None
    for entry in models.values():
        if not isinstance(entry, Mapping):
            continue
        downloads = entry.get("downloads")
        if not isinstance(downloads, Mapping):
            continue
        block = downloads.get(checkpoint or str(entry.get("model") or ""))
        if isinstance(block, Mapping):
            return _as_mb(block.get("download_mb"))
    return None


def download_sizes_mb(
    providers_raw: Mapping[str, Any] | None = None,
    *,
    stt_model: str | None = None,
) -> dict[str, int]:
    """What each optional artifact costs to fetch, read from the catalog.

    Keyed the way the setup form and the capability report both name these —
    `whisper`, `kokoro`, `reranker`, `embeddings`, `browser`. An entry whose
    figure is missing or unreadable is left out rather than guessed: a row
    with no size is a row the operator can still judge, and an invented one
    is not.

    `stt_model` names a checkpoint to price INSTEAD of the one the catalog
    currently carries, and the setup form is why it exists: the form runs
    before `provision_hardware` has written this machine's choice, so the only
    honest figure to quote is the one the machine's profile is about to
    select.

    A catalog that will not load yields nothing. This is what a number on a
    screen is drawn from, never what decides whether a download may happen.
    """
    if providers_raw is None:
        try:
            from tesseract.config.loader import load_config

            providers_raw = load_config().providers_raw
        except Exception as exc:  # noqa: BLE001 — a size is never worth a crash
            logger.warning("capability: could not read download sizes (%s)", exc)
            return {}

    sizes: dict[str, int] = {}
    for lane, path in _SIZE_PATHS.items():
        block = _dig(providers_raw, path)
        size = _as_mb(block.get("download_mb")) if isinstance(block, Mapping) else None
        if size is not None:
            sizes[lane] = size

    whisper = _whisper_download_mb(providers_raw, stt_model)
    if whisper is not None:
        sizes["whisper"] = whisper

    # The reranker pins on its model entry, and which entry is the operator's
    # to choose in `roles.yaml`. Every entry under the provider carries the
    # same artifact class, so the first one with a figure is the answer —
    # naming a model id here would be a fourth copy of a choice config owns.
    rerankers = _dig(providers_raw, ("local", "onnx_reranker", "models"))
    if isinstance(rerankers, Mapping):
        for entry in rerankers.values():
            block = entry.get("download") if isinstance(entry, Mapping) else None
            size = _as_mb(block.get("download_mb")) if isinstance(block, Mapping) else None
            if size is not None:
                sizes["reranker"] = size
                break
    return sizes


@dataclass(frozen=True)
class PinnedLocation:
    """One upstream pin and the directory its files land in."""

    base_url: str
    files: dict[str, str]
    dest_dir: Path


@dataclass(frozen=True)
class ModelLane:
    """A dependency made of pinned files, ready to be judged."""

    id: str
    #: Empty when the config does not ask for this engine at all.
    locations: list[PinnedLocation] = field(default_factory=list)
    configured: bool = True
    #: Set when resolution itself failed — a config that would not load, a
    #: download block that would not parse. Distinct from "not configured".
    unresolvable: str = ""


def _source_to_location(source, dest_dir: Path) -> PinnedLocation:  # noqa: ANN001
    return PinnedLocation(
        base_url=source.base_url, files=dict(source.files), dest_dir=dest_dir
    )


def whisper_lane() -> ModelLane:
    from tesseract.lib.pinned_fetch import parse_download_block
    from tesseract.voice import model_files
    from tesseract.voice.model_files import configured_refs

    refs = configured_refs("stt", "local_whisper")
    if not refs:
        return ModelLane(id="whisper", configured=False)

    ref = refs[0]
    model_name = str(ref.model.model)
    downloads = ref.model.fields.get("downloads") or {}
    block = downloads.get(model_name) if hasattr(downloads, "get") else None
    if block is None:
        # The catalog names a checkpoint it has no pin for. Reported rather
        # than silently absent: faster-whisper would resolve it by an unpinned
        # download at first use, which is the failure the fetch script exists
        # to prevent.
        return ModelLane(
            id="whisper",
            unresolvable=f"the catalog names {model_name}, which has no pinned download",
        )
    source = parse_download_block(
        block, where=f"providers.yaml::{ref.ref}.downloads.{model_name}"
    )
    if source is None:
        return ModelLane(id="whisper", unresolvable="its download pin could not be read")
    return ModelLane(
        id="whisper",
        locations=[
            _source_to_location(source, model_files.whisper_snapshot_dir(model_name))
        ],
    )


def kokoro_lane() -> ModelLane:
    from tesseract.lib.pinned_fetch import parse_download_block
    from tesseract.voice import model_files
    from tesseract.voice.model_files import configured_refs

    refs = configured_refs("tts", "kokoro")
    if not refs:
        return ModelLane(id="kokoro", configured=False)
    source = parse_download_block(
        refs[0].connection.extra.get("download"),
        where=f"providers.yaml::{refs[0].ref.rsplit('.', 1)[0]}",
    )
    if source is None:
        return ModelLane(id="kokoro", unresolvable="its download pin could not be read")
    return ModelLane(
        id="kokoro",
        locations=[_source_to_location(source, model_files.lane_dir("kokoro"))],
    )


def reranker_lane() -> ModelLane:
    from tesseract.brain.boot import load_reranker_cfg
    from tesseract.lib.pinned_fetch import parse_download_block

    cfg = load_reranker_cfg()
    if not cfg:
        return ModelLane(id="reranker", configured=False)
    source = parse_download_block(
        cfg.get("download"), where="providers.yaml reranker entry"
    )
    if source is None:
        return ModelLane(id="reranker", unresolvable="its download pin could not be read")
    return ModelLane(
        id="reranker",
        locations=[_source_to_location(source, Path(cfg["model_path"]).parent)],
    )


#: Every model lane, in the order a person would care about them.
LANE_RESOLVERS = (whisper_lane, kokoro_lane, reranker_lane)


def resolve_lane(resolver) -> ModelLane:  # noqa: ANN001
    """Run one resolver, turning any failure into an `unresolvable` lane.

    Config loading is the step outside every guard below it: a `providers.yaml`
    that will not parse raises from `configured_refs`, and this runs beside a
    launch where an exception reaches nobody.
    """
    try:
        return resolver()
    except Exception as exc:  # noqa: BLE001 — a lane that cannot be resolved is a state, not a crash
        lane_id = getattr(resolver, "__name__", "model").removesuffix("_lane")
        logger.warning("capability: could not resolve the %s lane (%s)", lane_id, exc)
        return ModelLane(id=lane_id, unresolvable="its configuration could not be read")


def check_lane(
    lane: ModelLane,
    recorded: dict[str, VerifiedPin] | None = None,
    sizes: Mapping[str, int] | None = None,
) -> DependencyRecord:
    """Judge one lane against its pins.

    Blocking: it may hash a file that has never been seen before. Callers on
    the event loop run this in a thread.

    `sizes` is `download_sizes_mb()` already resolved. `load_config` has no
    cache and re-parses both YAML files on every call, so a pass that let each
    lane resolve its own did the work three times per pass and logged the same
    warning three times on a tree with no config yet. Optional, because a
    caller judging one lane on its own should not have to know that.
    """
    if sizes is None:
        sizes = download_sizes_mb()
    size_mb = sizes.get(lane.id)

    if lane.unresolvable:
        return DependencyRecord(
            id=lane.id,
            kind="model",
            state=DependencyState.UNKNOWN,
            reason=lane.unresolvable,
            size_mb=size_mb,
        )

    if not lane.configured:
        # Consent stays NEVER_ASKED rather than becoming DECLINED, and the
        # distinction is the point: an unconfigured lane may be one the
        # operator turned off OR one nobody has ever been asked about, and
        # asserting the first would be inventing an answer. `needs_attention`
        # is False either way, so nothing is reported until there is a real
        # answer to report against.
        return DependencyRecord(
            id=lane.id,
            kind="model",
            state=DependencyState.ABSENT,
            reason="not switched on, so nothing is downloaded for it",
            size_mb=size_mb,
        )

    worst = DependencyState.OK
    reasons: list[str] = []
    merged: dict[str, VerifiedPin] = {}

    for location in lane.locations:
        state, resolved, reason = pins.resolve(
            base_url=location.base_url,
            files=location.files,
            dest_dir=location.dest_dir,
            recorded=recorded,
        )
        merged.update(resolved)
        if reason:
            reasons.append(reason)
        if _SEVERITY[state] > _SEVERITY[worst]:
            worst = state

    return DependencyRecord(
        id=lane.id,
        kind="model",
        state=worst,
        # The config naming this lane IS the consent signal, and has been for
        # as long as the fetch scripts have existed: declining at setup writes
        # `enabled: false` and they fetch nothing. Recording it as such lets
        # the launch pass repair a configured lane exactly as today, while a
        # real answer from the form or Settings outranks it.
        consent=Consent.GRANTED,
        consent_origin=ConsentOrigin.CONFIG,
        reason="; ".join(dict.fromkeys(reasons)),
        size_mb=size_mb,
        pins=merged,
    )


def check_all(
    recorded: dict[str, dict[str, VerifiedPin]] | None = None,
) -> list[DependencyRecord]:
    """Every model lane, judged. Blocking; run it in a thread."""
    recorded = recorded or {}
    # Once for the pass, not once per lane.
    sizes = download_sizes_mb()
    out: list[DependencyRecord] = []
    for resolver in LANE_RESOLVERS:
        lane = resolve_lane(resolver)
        out.append(check_lane(lane, recorded.get(lane.id), sizes))
    return out
