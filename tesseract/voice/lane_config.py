"""Mutations of the ``roles.yaml::voice`` lanes.

Two callers write the voice lane, and they must agree about what selecting a
voice means: the Identity tab's picker (`mirror/server/routes/voice.py`) and
first-run setup (`scripts/apply_first_run_setup.py`). These functions are
pure document mutations with no web or config-loading dependency, so the
provisioning script can reach them without importing the server.
"""

from __future__ import annotations

from typing import Any

# `config.role_modes` only, never `config.loader` — this module is reached by
# first-run provisioning, which imports nothing else from the package.
from tesseract.config.role_modes import ROLE_MODE_ACTIVE, ROLE_MODE_INACTIVE


def apply_tts_primary(doc: Any, ref: str) -> None:
    """Promote `ref` to ``voice.tts.primary``, demoting the old primary.

    A lane that was `mode: inactive` is switched back on — see the comment
    below.

    The displaced primary goes to the head of `fallbacks` so the lane the
    operator was just using stays the first thing that speaks when the new
    one fails. `ref` is removed from `fallbacks` — a ref listed twice would
    build the same engine lane twice.
    """
    voice = doc.get("voice")
    if voice is None:
        raise KeyError("voice")
    lane = voice.get("tts")
    if lane is None:
        raise KeyError("voice.tts")
    # Picking a voice IS the request to use it, so a lane that was switched
    # off comes back on. Without this the lane is a one-way door: the loader
    # drops an inactive lane, the catalog then reports it as absent rather
    # than off, and the operator's pick returns success while nothing speaks.
    # Set before the already-primary early return, or re-picking the voice the
    # lane already names leaves it off.
    reactivated = lane.get("mode") == ROLE_MODE_INACTIVE
    if reactivated:
        lane["mode"] = ROLE_MODE_ACTIVE
    current = lane.get("primary")
    if current == ref and not reactivated:
        return
    fallbacks = [f for f in (lane.get("fallbacks") or []) if f != ref]
    if current:
        fallbacks.insert(0, current)
    lane["primary"] = ref
    lane["fallbacks"] = fallbacks


def drop_tts_fallbacks(doc: Any, adapter_refs: set[str]) -> None:
    """Remove the named refs from ``voice.tts.fallbacks``.

    First-run setup uses this when the operator picks the lighter engine:
    leaving the heavier one in the chain would download its model anyway,
    which is the opposite of what choosing it meant. Never touches
    `primary` — dropping the lane that speaks is a separate decision.
    """
    voice = doc.get("voice")
    if voice is None:
        return
    lane = voice.get("tts")
    if lane is None:
        return
    lane["fallbacks"] = [
        f for f in (lane.get("fallbacks") or []) if f not in adapter_refs
    ]
