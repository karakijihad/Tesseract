"""Commit handlers for the two dial cards: what the runtime spends, what it
carries.

`tuning_proposal` (a role's daily cap) and `working_set_proposal` (which
tools are carried on every turn) both move a number or a list
the operator owns through a seam the job that filed the card never touched
directly — the same boundary, which is why both live here together. Split
out of `routes/workspace.py`; see `workspace_proposal_commits.py` for
the sibling file/memory-edit half.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any

from aiohttp import web

from tesseract.mirror.server.routes.workspace_shared import _store
from tesseract.workspace_events import WorkspaceEvent

log = logging.getLogger(__name__)


async def _commit_tuning_proposal(
    app: web.Application,
    ev: WorkspaceEvent,
) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], int] | None]:
    """Apply a ``tuning_proposal`` by moving the one seam the card named.

    **The job that filed this card wrote nothing.** It read the runtime's own
    gauges and said what it would change; the change happens here, on the
    approval, through the seam that already owns the field. That is the same
    boundary ``_commit_working_set_proposal`` draws, and the reason both exist
    rather than the job editing config directly.

    The kind is checked against ``scheduler/proposals.py`` before anything is
    moved. A card carrying a kind this runtime does not declare is a card from
    a producer that is ahead of, or behind, the list, and applying it would be
    guessing which.
    """
    from tesseract.mirror.server.routes.settings import apply_role_ceilings
    from tesseract.scheduler import proposals

    payload = ev.payload or {}
    key = str(payload.get("kind") or "")
    try:
        declared = proposals.filed(key)
    except proposals.UnknownProposalKind as exc:
        return None, ({"error": "unknown_proposal_kind", "detail": str(exc)}, 400)

    if declared.key != "ceiling":
        # Every other declared kind is filed by a producer whose apply half is
        # not built yet. Refusing here is the difference between a card that
        # cannot be applied and one that reports success and moves nothing.
        return None, (
            {
                "error": "not_applicable_yet",
                "detail": (
                    f"'{declared.key}' proposals can be read and declined, but "
                    "approving one does not move anything yet"
                ),
            },
            400,
        )

    # **The card carries a LIST**, because one run can find two roles at their
    # cap and the operator should answer that once. Applying only the first
    # and reporting success is the shape this whole function exists to refuse,
    # so the whole list goes to the seam in ONE call: `apply_role_ceilings`
    # validates every name before it writes any of them, which is what makes
    # a partly applied card impossible rather than merely unlikely.
    wanted: dict[str, Any] = {}
    # **What the card was worked out FROM travels with it.** A card filed on
    # Monday and approved on Friday would otherwise put Monday's reading back
    # over a cap the operator moved by hand in between, and a card that says
    # "raise" would lower it. `_commit_change_proposal` (in
    # `workspace_proposal_commits.py`) already carries `expected_hash_before`
    # for the same reason; this is that rule for a number rather than a file.
    expected: dict[str, Any] = {}
    rows = payload.get("changes") or []
    for row in rows:
        # **A row this cannot read refuses the whole card.** Skipping it left
        # the rest to be applied and marked `applied`, so a card the operator
        # answered as three changes landed as two and said nothing about the
        # third. The comment below promises one call to one seam precisely so
        # that a partly applied card is impossible; silently dropping a row
        # was that promise being broken one line above it.
        if not isinstance(row, dict):
            return None, (
                {
                    "error": "invalid_proposal",
                    "detail": (
                        "one of the changes on this card is not readable, so "
                        "none of them have been applied"
                    ),
                },
                400,
            )
        role = str(row.get("role") or "")
        proposed = row.get("proposed_usd")
        if not role or proposed is None:
            return None, (
                {
                    "error": "invalid_proposal",
                    "detail": (
                        "one of the changes on this card does not say which "
                        "limit to move or what to move it to, so none of them "
                        "have been applied"
                    ),
                },
                400,
            )
        # **A change with no reading behind it is refused, not applied
        # unchecked.** Every card this job files carries `current_usd`
        # (`Change.as_json`), so a row without one did not come from the job
        # as it stands, and letting it through would be the staleness gate
        # opening for exactly the payload nobody can vouch for.
        was = row.get("current_usd")
        if was is None:
            return None, (
                {
                    "error": "invalid_proposal",
                    "detail": (
                        f"the change for {role} does not say what the limit "
                        "was when it was worked out, so it cannot be checked "
                        "against what the limit is now"
                    ),
                },
                400,
            )
        wanted[role] = proposed
        expected[role] = was
    if not wanted:
        return None, (
            {"error": "invalid_proposal", "detail": "no role and cap to apply"},
            400,
        )

    cost_cfg = app["config"].models.get("cost_tracking") or {}
    try:
        caps = await asyncio.to_thread(
            partial(
                apply_role_ceilings,
                app,
                wanted,
                warning_at_pct=float(cost_cfg.get("warning_at_pct", 0.75)),
                expected=expected,
            )
        )
    except ValueError as exc:
        return None, ({"error": "refused", "detail": str(exc)}, 400)
    except Exception as exc:  # noqa: BLE001 — the operator is waiting on it
        log.exception("tuning proposal: applying a ceiling failed")
        return None, ({"error": "apply_failed", "detail": str(exc)}, 500)

    applied = {role: caps.get(role) for role in wanted}
    _record_tuning_applied(app, ev, applied)
    return {"applied": applied}, None


def _record_tuning_applied(
    app: web.Application, ev: WorkspaceEvent, applied: dict[str, Any]
) -> None:
    """Write what landed back onto the card, so the pane says what changed.

    The same two keys the working set proposal writes, read by the same
    component. Without it the card goes on describing what it WOULD do after
    the operator has already agreed to it, which is the one moment they want
    to see the number that is now in the file.
    """
    lines = [
        f"{role} is now capped at {float(value):.2f} dollars a day."
        for role, value in sorted(applied.items())
        if value is not None
    ]
    try:
        _store(app).merge_event_payload(
            ev.event_id, {"applied": applied, "applied_lines": lines}
        )
    except Exception:  # noqa: BLE001
        log.warning(
            "tuning_proposal: could not record what was applied", exc_info=True
        )


async def _commit_working_set_proposal(
    app: web.Application,
    ev: WorkspaceEvent,
) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], int] | None]:
    """Apply a ``working_set_proposal`` by moving tool names on and off the
    dial.

    **The job that filed this card never touched the file**, and that is the
    whole boundary: the names under ``core:`` are the operator's half of a
    file whose other half is generated, so the change happens here, on the
    approval, and nowhere else. Written through its own generator so the
    annotation beside every name is rebuilt rather than carried forward from
    whatever a previous release wrote.

    Playbooks had a twin of this half once, over
    ``workspace/skills/carried.txt``. It was removed: nothing proposes a
    playbook onto or off the carried dial by a threshold any more, so this
    seam only ever moves tools now.

    Applied live rather than at the next restart, for the reason the tool
    switch in Conscience is: an operator who approved a change and then watched
    the next turn ignore it has no way to tell a slow write from a broken one.
    """
    from tesseract.config.working_set import (
        UNDROPPABLE,
        config_path,
        load_core_tool_names,
    )
    from tesseract.scripts.generate_working_set import _registry_facts, render

    payload = ev.payload or {}
    def _names(key: str, field: str) -> list[str]:
        return [n for n in (str(r.get(field) or "") for r in payload.get(key) or []) if n]

    carry = _names("carry", "tool")
    drop = _names("drop", "tool")
    if not (carry or drop):
        return None, ({"error": "invalid_proposal", "detail": "nothing to apply"}, 400)

    registry = app.get("tool_registry")
    if registry is None:
        return None, ({"error": "the tool registry is not up yet"}, 503)

    changed: dict[str, Any] = {"carried": [], "dropped": [], "refused_custom": []}

    try:
        chosen = set(load_core_tool_names())
    except (OSError, ValueError, KeyError) as exc:
        return None, ({"error": "invalid_proposal", "detail": str(exc)}, 500)
    for name in carry:
        # A tool the card named that has since been removed is skipped
        # rather than written: `working_set.yaml` naming a tool nothing
        # answers to stops the app at boot, and a card can outlive a
        # release that deleted one.
        tool = registry.tools.get(name)
        if tool is None or name in chosen:
            continue
        # And a tool the OPERATOR wrote never goes in this file, whatever
        # a card says. It is regenerated and it ships, so a machine-local
        # name here is destroyed by the next generator run or handed to
        # strangers as a tool they do not have. The proposing stage filters
        # these out only when it had a registry to ask; this is the check
        # that does not depend on that. `conscience.py::set_working_set`
        # makes the same split, to `write_promoted`.
        if getattr(tool, "origin", "shipped") == "custom":
            changed["refused_custom"].append(name)
            continue
        chosen.add(name)
        changed["carried"].append(name)
    for name in drop:
        # Both doors stay, whatever a card says. `tool_search` reaches
        # every tool not carried and `playbook_search` every playbook, so
        # dropping either is a capability cut rather than a saving. Read
        # from the one constant both this and the proposing stage use: a
        # card can outlive the release that added a lock, and this layer
        # is the one that re-derives its guards rather than trusting the
        # stage that filed the card.
        if name in UNDROPPABLE or name not in chosen:
            continue
        chosen.discard(name)
        changed["dropped"].append(name)

    def _write_tools() -> None:
        config_path().write_text(
            render(sorted(chosen), _registry_facts()), encoding="utf-8"
        )

    try:
        await asyncio.to_thread(_write_tools)
    except Exception as exc:  # noqa: BLE001
        log.exception("working_set_proposal: could not write the working set")
        return None, ({"error": "saved nothing", "detail": str(exc)}, 500)

    tiers_error = _reload_tiers(registry)

    # What actually landed, written back onto the card. A card can propose a
    # name a later release removed, or the floor, and both are skipped above:
    # a card that still reads as its original proposal after being applied
    # would be telling the operator something that did not happen. This is
    # locked decision 21's other half, said about one card rather than about a
    # filter.
    _record(app, ev, changed)

    if tiers_error is not None:
        return changed, tiers_error
    return changed, None


def _reload_tiers(registry: Any) -> tuple[dict[str, Any], int] | None:
    """Make the file that just changed take effect on the next turn.

    Separate from the write, which already landed: `_apply_tool_tiers` raises
    when the file names a tool that is not registered, and reporting "saved
    nothing" for a file that just changed on disk is a lie. Returns the error
    the caller should surface, or None.
    """
    from tesseract.brain.boot import _apply_tool_tiers, core_tool_names

    try:
        core_tool_names(refresh=True)
        _apply_tool_tiers(registry)
    except Exception as exc:  # noqa: BLE001
        log.exception("working_set_proposal: applied but could not take effect")
        return (
            {
                "error": (
                    "Saved, but it does not take effect until the next "
                    f"restart: {exc}"
                )
            },
            500,
        )
    return None


def _record(
    app: web.Application,
    ev: WorkspaceEvent,
    changed: dict[str, Any],
) -> None:
    """Write what actually landed back onto the card, ACCUMULATING.

    A card can name a tool a later release removed, the floor, or one the
    operator wrote, and every one of those is skipped above. A card still
    reading as its original proposal after being applied would be telling the
    operator something that did not happen.

    **Merged into whatever the card already recorded, not written over it.**
    A card can be approved more than once if an earlier attempt failed before
    reaching here, so `applied` has to be unioned with what the card already
    carries rather than written over it. `merge_event_payload` is a shallow
    merge over top-level keys, so that union has to happen here or not at all.
    """
    union = _accumulated(ev, changed)
    lines = _applied_lines(union)
    try:
        _store(app).merge_event_payload(
            ev.event_id, {"applied": union, "applied_lines": lines}
        )
    except Exception:  # noqa: BLE001
        log.warning("working_set_proposal: could not record what was applied", exc_info=True)


def _accumulated(ev: WorkspaceEvent, changed: dict[str, Any]) -> dict[str, Any]:
    """Everything this card has applied, across every attempt at it.

    Seeded from what the card already holds and overlaid with this call, so a
    key the card carries and this call does not is kept rather than dropped.
    That is not hypothetical tidiness: a direction added to one code path and
    not the other is exactly how the change count fell behind, and this is the
    same shape one level down.
    """
    previous = (ev.payload or {}).get("applied") or {}
    if not isinstance(previous, dict):
        previous = {}
    union: dict[str, Any] = {
        key: sorted(set(value or []))
        for key, value in previous.items()
        if isinstance(value, list)
    }
    for key, value in changed.items():
        union[key] = sorted(set(union.get(key) or []) | set(value))
    return union


def _applied_lines(changed: dict[str, Any]) -> list[str]:
    """What happened, in the sentences the card renders.

    Written here rather than in the pane, per AR-21's rule that no explanatory
    copy about a proposal lives in TSX: a second wording in a component is a
    second account of what the runtime did.
    """
    lines: list[str] = []
    if changed["carried"]:
        lines.append(
            "Now carried on every turn: " + ", ".join(sorted(changed["carried"])) + "."
        )
    if changed["dropped"]:
        lines.append(
            "No longer carried, and one tool_search away: "
            + ", ".join(sorted(changed["dropped"]))
            + "."
        )
    if changed.get("refused_custom"):
        lines.append(
            "Left alone, because these are tools you wrote and that list is "
            "yours to set in Conscience: "
            + ", ".join(sorted(changed["refused_custom"]))
            + "."
        )
    if not lines:
        lines.append(
            "Nothing changed. Every name on the card was already where it "
            "asked for, or is no longer here."
        )
    return lines
