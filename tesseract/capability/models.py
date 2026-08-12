"""The four model lanes, resolved from config and judged against their pins.

Every lane answers the same two questions — are the files here, and are they
the ones this version names — but each finds its pin somewhere different, and
that is a fact about the catalog rather than a wrinkle worth abstracting away:

- **whisper** pins per checkpoint, under the entry's ``downloads:`` map, so
  switching model size switches pin.
- **kokoro** pins once on the *connection*, because every catalogued voice is
  a mix over the same two files.
- **piper** pins per *voice*, so a chain of three voices is three pins landing
  in one directory.
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

from tesseract.capability import pins
from tesseract.capability.state import (
    Consent,
    ConsentOrigin,
    DependencyRecord,
    DependencyState,
    VerifiedPin,
)

logger = logging.getLogger(__name__)

#: Download sizes, in the decimal megabytes the operator is quoted elsewhere.
#:
#: Literals, and they have to be: `max_download_mb` in the catalog is a
#: REFUSAL CAP (2048 against a ~1.6 GB whisper), not a size, and rendering a
#: figure from it would tell the operator a 1.6 GB model is a 2 GB one. These
#: are facts about the artifact — the same bytes on every machine — which is
#: why they may be shown at all; see the phase file's copy rule.
#:
#: The splash carries the same three facts as its own literal, because it runs
#: before Python exists. A test reconciles the two rather than trusting them.
LANE_SIZES_MB: dict[str, int] = {
    "whisper": 1600,
    "kokoro": 340,
    "piper": 65,
    "reranker": 23,
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


def piper_lane() -> ModelLane:
    from tesseract.lib.pinned_fetch import parse_download_block
    from tesseract.voice import model_files
    from tesseract.voice.model_files import configured_refs

    refs = configured_refs("tts", "piper")
    if not refs:
        return ModelLane(id="piper", configured=False)

    dest = model_files.lane_dir("piper")
    locations: list[PinnedLocation] = []
    for ref in refs:
        source = parse_download_block(
            ref.model.fields.get("download"), where=f"providers.yaml::{ref.ref}"
        )
        if source is None:
            continue
        locations.append(_source_to_location(source, dest))
    if not locations:
        return ModelLane(id="piper", unresolvable="no configured voice has a readable pin")
    # Every configured voice, not only the primary: a fallback whose files are
    # missing is a lane that fails the moment the one ahead of it does, which
    # is the one moment it was supposed to help.
    return ModelLane(id="piper", locations=locations)


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
LANE_RESOLVERS = (whisper_lane, kokoro_lane, piper_lane, reranker_lane)


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
    lane: ModelLane, recorded: dict[str, VerifiedPin] | None = None
) -> DependencyRecord:
    """Judge one lane against its pins.

    Blocking: it may hash a file that has never been seen before. Callers on
    the event loop run this in a thread.
    """
    size_mb = LANE_SIZES_MB.get(lane.id)

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
    out: list[DependencyRecord] = []
    for resolver in LANE_RESOLVERS:
        lane = resolve_lane(resolver)
        out.append(check_lane(lane, recorded.get(lane.id)))
    return out
