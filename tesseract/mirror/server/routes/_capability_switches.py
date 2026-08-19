"""Flipping a capability switch — the write half of `capabilities.py`.

Everything here answers "what does turning this on or off actually do": where
the flag is written, what a switch has queued for the next start, and what
consent that click records. Nothing here assembles the report.

Split out for the same reason as `_capability_report.py`: the route module held
both halves plus five handlers.
"""

from __future__ import annotations

import logging
from typing import Any

from tesseract.paths import config_dir

log = logging.getLogger(__name__)


def _providers_yaml_path():
    """Resolved at call time via `config_dir()` — the same resolution
    `load_bundle()` reads through, so a write and the report that follows it
    can never address different files (and tests can point both at a fixture
    tree with `monkeypatch.setenv("TESSERACT_HOME", ...)`).
    """
    return config_dir() / "providers.yaml"


def _set_enabled_flag(block: Any, enabled: bool) -> None:
    """Write `enabled` into a tier or provider block.

    Blocks that never carried the key explicitly (the readers all default it
    to True) get it inserted at the top, where every hand-written block in
    providers.yaml already keeps it, rather than appended below the models.
    """
    if "enabled" in block:
        block["enabled"] = enabled
    elif hasattr(block, "insert"):  # ruamel CommentedMap
        block.insert(0, "enabled", enabled)
    else:
        block["enabled"] = enabled


#: Provider block name -> the dependencies the reconciler knows it by. Only
#: the ones that download something: flipping a switch with nothing behind it
#: is not an answer to a question about disk space.
#:
#: `ollama` is two, and that is not a tidiness point. The switch's repair is
#: `_ensure_ollama`, which installs the daemon AND pulls every model config
#: names — the same pairing `apply_first_run_setup` records from the form's one
#: embeddings answer. Recording only the binary left `ollama-models` at
#: `never_asked` while the repair downloaded them anyway, which is a download
#: outside the ledger: the same class of defect as a voice-lane decline
#: nobody gave, in the opposite direction.
#: `browser` is here because `apply_first_run_setup._CONSENT_FROM_ANSWERS`
#: records it from the setup form and this did not — so an operator who
#: declined the browser engine at setup and turned it on here kept a DECLINED
#: answer that outranks the config switch they had just flipped.
#: One map, `block -> (dependency ids, size key)`. It was two, which had to be
#: kept in step by hand: `_queued_download` filtered its blocks against one and
#: looked up ids in the other, so a provider added to one and not the other
#: went silent rather than wrong — the failure nobody notices.
#:
#: The size key is what `download_sizes_mb` prices the block under, and every
#: block here has an artifact behind it. A switch that downloads nothing queues
#: nothing, and announcing a download for it would be the same defect in the
#: other direction.
_OPTIONAL_BLOCKS: dict[str, tuple[tuple[str, ...], str]] = {
    "whisper": (("whisper",), "whisper"),
    "kokoro": (("kokoro",), "kokoro"),
    "onnx_reranker": (("reranker",), "reranker"),
    "ollama": (("ollama", "ollama-models"), "embeddings"),
    "browser": (("browser-engine",), "browser"),
}

#: Kept as a name because it reads as one at both call sites.
_CONSENT_DEPENDENCIES = {
    block: dependencies for block, (dependencies, _size) in _OPTIONAL_BLOCKS.items()
}


def _catalog() -> dict:
    """The catalog as it stands, or an empty document.

    Read ONCE per request — by the route handler, which hands the document to
    both `_record_consent_for` and `_queued_download`. Every helper below used
    to open and parse the file itself, so one tier toggle re-read
    `providers.yaml` ten times; the two that remained after the first fix were
    found by the Trio simplifier lens. No memoization here on purpose: the
    request has just WRITTEN this file, and a cache would be the thing that
    hands back the version from before the write.
    """
    try:
        import yaml

        return yaml.safe_load(_providers_yaml_path().read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — an unreadable catalog announces nothing
        return {}


def _will_fetch(doc: dict, tier: str | None, provider: str) -> bool:
    """Whether this block's artifact can actually be fetched as configured.

    BOTH switches, and that is the finding: a tier write flips only the tier's
    own flag, so a provider left individually off is still off — and a
    provider re-enabled under a section that is off is still gated, because
    `service_disabled_reason` refuses on the section before it looks at the
    service. Announcing either promised a download the fetchers would not make.

    Absent means on, matching how every reader of these switches treats a
    missing `enabled` key.
    """
    if not tier:
        return False
    section = doc.get(tier)
    if not isinstance(section, dict) or not bool(section.get("enabled", True)):
        return False
    block = section.get(provider)
    return bool(block.get("enabled", True)) if isinstance(block, dict) else False


def _queued_download(
    provider: str | None, enabled: bool, *, tier: str | None, doc: dict | None = None
) -> dict | None:
    """What flipping this switch ON just queued, or None if nothing.

    The switch writes `enabled: true` and returns. Nothing downloads here —
    the fetch happens on the next start, in `launch_refresh`'s pass or, for
    the browser engine, in the boot path that reads the same switch. That is
    correct, and it was invisible: the operator turned something on, nothing
    appeared to happen, and several hundred megabytes arrived on a later start
    they had no reason to connect to it.

    Reads the last pass's verdict rather than reconciling — a Settings toggle
    must not start a hardware probe, and a stale "already here" costs a
    notice, not a download. A dependency the pass has never seen counts as
    queued, which is the right way round: the notice is a warning, and the
    silent case is the one worth erring against.
    """
    if not enabled:
        return None
    # The handler reads the catalog once and hands it to both helpers, so one
    # click parses `providers.yaml` once. `_catalog()` here is for the direct
    # callers (tests) that have no document to pass.
    doc = _catalog() if doc is None else doc
    candidates = (
        list(_OPTIONAL_BLOCKS) if provider is None else [provider]
    )
    # One rule for both branches. It used to be two: the tier branch checked
    # the per-provider switch and the single-provider branch checked nothing,
    # so re-enabling one service under a section that was off announced a
    # download the boot path would refuse.
    blocks = [
        name
        for name in candidates
        if name in _OPTIONAL_BLOCKS and _will_fetch(doc, tier, name)
    ]
    if not blocks:
        return None

    from tesseract.capability.models import DOWNLOAD_LABELS, download_sizes_mb
    from tesseract.capability.state import DependencyState, read_state

    state = read_state()
    present = (
        {
            dep_id
            for dep_id, record in state.dependencies.items()
            if record.state is DependencyState.OK
        }
        if state is not None
        else set()
    )
    sizes = download_sizes_mb()

    names: list[str] = []
    total = 0
    for block in blocks:
        dependencies, key = _OPTIONAL_BLOCKS[block]
        if all(dep in present for dep in dependencies):
            continue  # already on disk; turning it back on costs nothing
        names.append(DOWNLOAD_LABELS.get(key, key))
        total += sizes.get(key) or 0
    if not names:
        return None
    return {
        "names": names,
        # None rather than 0 when no figure could be read: a download of
        # unknown size is still a download, and "0 MB" would be a claim.
        "size_mb": total or None,
        # The one honest answer about WHEN. Said as a field rather than as a
        # sentence so the panel words it and the route does not carry copy.
        "when": "next_start",
    }


def _record_consent_for(
    provider: str | None,
    enabled: bool,
    *,
    tier: str | None = None,
    doc: dict | None = None,
) -> None:
    """Turn a Settings toggle into a recorded answer.

    Without this the ledger only ever fills from the first-run form, so a lane
    switched on months later would stay `never_asked` — and the reconciler
    would go on refusing to fetch what the operator had just asked for, which
    reads as the toggle being broken.

    A toggle is an ANSWER, so it outranks what the config implies. That is the
    whole point of recording it: `enabled: false` alone cannot distinguish a
    lane someone turned off from one nobody ever reached.

    **A TIER switch answers for everything under it.** `provider is None` means
    the operator flipped `local` itself, which disables every local provider at
    once — and recording nothing for that left each one's earlier per-provider
    consent standing as `granted`. Because a ledger answer outranks config, the
    reconciler then went on treating lanes the operator had just switched off
    as things it should repair. The tier answer is the more recent one and
    covers the same ground, so it is written across the tier.

    Best-effort. A ledger that cannot be written must not fail the toggle —
    the switch itself already landed in `providers.yaml`, which is what the
    runtime acts on.
    """
    if provider is None:
        doc = _catalog() if doc is None else doc
        dependencies = tuple(
            dep
            for name, deps in _CONSENT_DEPENDENCIES.items()
            if _in_tier(doc, tier, name)
            for dep in deps
        )
    else:
        dependencies = _CONSENT_DEPENDENCIES.get(provider, ())
    if not dependencies:
        return
    try:
        from tesseract.capability.consent import record
        from tesseract.capability.state import Consent, ConsentOrigin

        answer = Consent.GRANTED if enabled else Consent.DECLINED
        record(
            {dependency: answer for dependency in dependencies},
            origin=ConsentOrigin.SETTINGS,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "capabilities: could not record consent for %s (%s)",
            ", ".join(dependencies),
            exc,
        )


def _in_tier(doc: dict, tier: str | None, provider: str) -> bool:
    """Whether `provider` lives under `tier` in the catalog `doc`.

    Asked of the catalog rather than assumed: every entry in
    `_CONSENT_DEPENDENCIES` happens to be under `local` today, and hardcoding
    that would silently answer for the wrong providers the first time one
    moves.

    Takes the already-parsed document because it is called once per optional
    block — reading the file itself made a tier toggle re-parse `providers.yaml`
    once per block, which is the defect `_catalog()` exists to end.
    """
    if not tier:
        return False
    block = doc.get(tier)
    return isinstance(block, dict) and provider in block
