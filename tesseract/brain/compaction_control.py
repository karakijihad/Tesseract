"""Turning the boundary setting, from wherever the operator is.

One writer. The Settings pane calls it through
`mirror/server/routes/settings.py`, and `context_set` calls it as a tool, so a
channel reaches it too. A second implementation would be a setting that works
in the cockpit and silently stops working three days from the desk, which is
the fork this runtime exists not to have.

**Four things have to happen together** and a caller that does three of them
has written a setting that appears to work:

  1. `roles.yaml` is rewritten, so it survives a restart.
  2. The in-memory config is updated, so a read-back agrees with the write.
  3. Every OPEN chat session takes the new value, not just the one on screen.
  4. The tools that build sub-agents take it too. A sub-agent runs against the
     same window as the chat that started it and has to bound itself on the
     same numbers; its tools were handed those numbers once, at boot.

The bounds live here for the same reason. A channel that could set something
the pane refuses would be a second policy, and the refusal is where a policy is
easiest to fork without noticing.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from tesseract.lib.yaml_io import round_trip_yaml

logger = logging.getLogger(__name__)

#: What the pane's control allows and what any other surface must allow too.
#: The floor is not zero: a ratio small enough to be crossed on every turn
#: starts the conversation over each time and gets nothing for it.
RATIO_MIN = 0.10
RATIO_MAX = 0.95


def validate_ratio(value: Any) -> float:
    """The boundary setting, or a `ValueError` a person can act on."""
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"the boundary setting has to be a number between {RATIO_MIN} and "
            f"{RATIO_MAX}, as a share of the context window"
        ) from None
    if not RATIO_MIN <= ratio <= RATIO_MAX:
        raise ValueError(
            f"the boundary setting has to be between {RATIO_MIN} and "
            f"{RATIO_MAX}. It is the share of the context window a "
            f"conversation may fill before it is wrapped up and started "
            f"again, so {ratio} would "
            + (
                "wrap up almost every turn"
                if ratio < RATIO_MIN
                else "never wrap up at all"
            )
        )
    return ratio


def open_chat_sessions(sess: Any) -> list[Any]:
    """Every ChatSession a server session holds, active chat included.

    `sess.chats` is the per-chat map and `sess.chat_session` is whichever of
    them is on screen. Iterating only the second is how a setting reached one
    conversation and none of the others.
    """
    out = list((getattr(sess, "chats", None) or {}).values())
    active = getattr(sess, "chat_session", None)
    if active is not None and active not in out:
        out.append(active)
    return out


def apply_to_document(doc: Any, ratio: float | None) -> None:
    """Write the boundary setting where it lives.

    Top-level (`compaction.compact_ratio`): it says what share of a window a
    conversation may fill, which is a statement about how the boundary works
    and not about who is using it.

    It is now the only knob in that block besides the emergency prompt guard.
    Everything else there sized a summarising fold, and there is no fold.
    """
    roles = doc.get("roles")
    if not roles or "chat_brain" not in roles:
        raise KeyError("roles.chat_brain")
    if ratio is not None:
        # Created when absent. A hand-edited file may carry no block at all,
        # and refusing the edit would leave the operator with a control that
        # works on one machine and not another.
        if doc.get("compaction") is None:
            doc["compaction"] = {}
        doc["compaction"]["compact_ratio"] = ratio
        # And the role override goes, because it outranks what we just wrote.
        # Leaving it made the slider work for the rest of the session and snap
        # back on the next restart, with nothing anywhere saying why.
        roles["chat_brain"].pop("compact_threshold", None)


def _sync_in_memory(app: Any, ratio: float | None) -> None:
    roles = app["config"].models.get("roles") or {}
    chat_brain = roles.get("chat_brain") or {}
    if not chat_brain:
        return
    if ratio is not None:
        chat_brain["compact_threshold"] = ratio
    # Legacy synthesized shape also carries `resolution[0]` for compat — keep
    # it in lockstep so reads from either path see the same value.
    resolution = chat_brain.get("resolution") or []
    if resolution:
        primary = resolution[0]
        if ratio is not None:
            primary["compact_threshold"] = ratio


def _update_live_sessions(app: Any, ratio: float | None) -> None:
    for sess in (app.get("server_sessions") or {}).values():
        # Every open chat, not only the one on screen. A background chat holds
        # its own ChatSession and kept the old setting until it was closed and
        # reopened.
        for cs in open_chat_sessions(sess):
            if ratio is not None:
                cs.compact_threshold = ratio


def _update_subagent_tools(app: Any, ratio: float | None) -> None:
    """Carry the same two settings to the tools that build sub-agents.

    A sub-agent runs against the same context window as the chat that started
    it, so it has to bound itself on the same numbers. Its tools were handed
    those numbers once, at boot.
    """
    changes: dict[str, Any] = {}
    if ratio is not None:
        changes["compact_threshold"] = ratio
    if not changes:
        return
    registry = app.get("tool_registry")
    if registry is None:
        return
    for name in ("invoke_agent", "session_open"):
        tool = registry.get(name)
        # A registry that booted without a chat adapter carries neither, which
        # is why a missing one is not an error. A tool that is HERE and cannot
        # take the update is a different thing, and saying nothing about it is
        # how a rename would put sub-agents back on stale settings in silence.
        if tool is None:
            continue
        if not hasattr(tool, "update_compaction"):
            logger.warning(
                "compaction settings not carried to %s: it no longer takes them",
                name,
            )
            continue
        tool.update_compaction(**changes)


def apply_compaction(
    app: Any,
    roles_yaml_path: Path,
    *,
    ratio: float | None = None,
) -> Any:
    """Persist, then carry the change to everything already running.

    Returns the rewritten document. Raises `KeyError` if `roles.yaml` has no
    `chat_brain`, which is a broken config rather than a bad request.
    """
    doc = round_trip_yaml(
        roles_yaml_path, lambda d: apply_to_document(d, ratio)
    )
    _sync_in_memory(app, ratio)
    _update_live_sessions(app, ratio)
    _update_subagent_tools(app, ratio)
    return doc


def describe(app: Any) -> dict[str, Any]:
    """What the boundary setting is now, and where it puts the next one.

    Read off a live session where there is one, because that is the number the
    conversation will actually meet. `context_report` is the one measurement
    and this does not compute a second.
    """
    from tesseract.brain import context_report

    for sess in (app.get("server_sessions") or {}).values():
        for cs in open_chat_sessions(sess):
            try:
                report = context_report.gather(cs)
            except Exception:
                logger.exception("compaction control: report failed")
                continue
            return {
                "ratio": getattr(cs, "compact_threshold", None),
                "boundary_at_tokens": context_report.boundary_ceiling(report),
                # The slice the trigger governs, not the whole assembly.
                # `context_set` falls back to this when it cannot reach the
                # asking conversation, and its two siblings
                # (`context_report.render`, `context_set._this_conversation`)
                # both read this field. Reading `tokens` here made the same
                # tool answer differently depending on which path it took.
                "tokens_now": int(
                    report.get("conversation_tokens") or report.get("tokens") or 0
                ),
                "context_window": int(report.get("context_window") or 0),
            }
    return {}


__all__ = [
    "RATIO_MAX",
    "RATIO_MIN",
    "apply_compaction",
    "apply_to_document",
    "describe",
    "open_chat_sessions",
    "validate_ratio",
]
