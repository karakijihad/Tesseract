"""What may become a memory, and what the funnel actually turned away.

Three endpoints back the Autonomy panel's capture pane:

- ``GET  /api/capture/policy`` — anonymous-readable. Every declared rule with
  its own prose, its effective posture, and the layer that decided it.
- ``POST /api/capture/policy`` — operator-session-gated. Body
  ``{session_id, rule, enabled}``. Writes the runtime overlay at
  ``<HOME>/runtime/capture-policy.json``; the YAML layer is hand-edited only,
  exactly as the notification mutes work. A locked rule is refused with 400.
- ``GET  /api/capture/rates`` — anonymous-readable. What each rule blocked over
  a window, read out of the ledger the write path already keeps.

The audit shape is ``notifications.py``'s, and the operator-session check is
imported from it rather than copied: two gates that must agree about who the
operator is will not stay agreed if they are written twice.

No prose about a rule is composed here. Every word a surface renders comes off
the rule's own declaration, which is the same contract the run manifest and the
tool registry hold: the frontend authors nothing about the machine.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from tesseract.memory.capture_policy import (
    CAPTURE_RULES,
    RULES_BY_KEY,
    RuleState,
    block_counts,
    effective_policy,
    read_config_policy,
    read_runtime_policy,
    write_runtime_policy,
)
from tesseract.mirror.server.routes.notifications import _authed_body

log = logging.getLogger(__name__)

#: How far back the gauge counts, and what the pane says it is showing. A rule
#: that blocked nothing over a month is a rule to question; a shorter window
#: cannot tell a quiet rule from a quiet week.
RATES_WINDOW_DAYS = 30


def _row(state: RuleState, blocked: int = 0) -> dict[str, Any]:
    """One rule, as every response here renders it. Written once.

    It carries its own state and the word for it, because the panel's rule is
    that a surface never decides what colour a thing is. A rule that is on is
    doing its job; a rule that is off is off because somebody chose that, which
    is the panel's `off_by_choice` and not a fault.
    """
    rule = state.rule
    return {
        "rule": rule.key,
        "summary": rule.summary,
        "why": rule.why,
        "enabled": state.enabled,
        "layer": state.layer,
        "locked": rule.locked,
        "shipped_default": rule.default,
        "enforced_by": rule.enforced_by,
        # What the rule's own numbers are set to, as a sentence the rule
        # writes. Null for a rule that has none, which is all but one.
        "detail": state.detail,
        "blocked": blocked,
        "state": "running" if state.enabled else "idle",
        "label": "on" if state.enabled else "off",
        "obligation": "fine" if state.enabled else "off_by_choice",
    }


async def get_policy(request: web.Request) -> web.Response:
    """The pane's one read: the rules, their postures, and what each stopped.

    The count rides the rule rather than arriving from `/rates` for the pane to
    join, on the lesson the Channels room already learned: a room that merges
    three payloads in TSX has become a settings list. Both endpoints call the
    same counter, so there is one implementation and not two answers.
    """
    states = effective_policy()
    counts = await asyncio.to_thread(block_counts, RATES_WINDOW_DAYS)
    rows = [
        _row(states[rule.key], counts.get(rule.counts_under, 0))
        for rule in CAPTURE_RULES
    ]
    # What each layer holds, so the pane can say a value came from the config
    # file without the operator having to open the config file.
    try:
        config = read_config_policy()
    except Exception as exc:  # noqa: BLE001
        log.warning("capture policy config unreadable (%s)", exc)
        config = {}
    return web.json_response({
        "rules": rows,
        "layers": {
            "config": config,
            "runtime": read_runtime_policy(),
        },
        "window_days": RATES_WINDOW_DAYS,
    })


async def post_policy(request: web.Request) -> web.Response:
    body, err = await _authed_body(request)
    if err is not None:
        return err
    assert body is not None
    rule_key = body.get("rule")
    enabled = body.get("enabled")
    if not isinstance(rule_key, str) or rule_key not in RULES_BY_KEY:
        return web.json_response({"error": f"unknown capture rule {rule_key!r}"}, status=400)
    if not isinstance(enabled, bool):
        return web.json_response({"error": "enabled must be boolean"}, status=400)
    rule = RULES_BY_KEY[rule_key]
    if rule.locked and not enabled:
        return web.json_response(
            {
                "error": (
                    f"{rule_key!r} cannot be switched off. It is enforced at "
                    f"{rule.enforced_by or 'the write path'} and a memory is the "
                    "most durable place a credential can land."
                )
            },
            status=400,
        )

    runtime = read_runtime_policy()
    # Drop the override when the layers underneath already say this, so the
    # overlay stays readable as "what this machine changed". What is underneath
    # is the config value where there is one and the code default otherwise,
    # NOT the code default alone: comparing against that meant a machine whose
    # `memory.yaml` disagreed with the shipped answer could never be moved back
    # to it, because the pop handed the rule straight back to the config layer
    # and the response reported a state the operator had not asked for.
    try:
        config = read_config_policy()
    except Exception as exc:  # noqa: BLE001 — a broken config is not a reason to refuse a write
        log.warning("capture policy config unreadable, comparing against the code default (%s)", exc)
        config = {}
    beneath = config.get(rule_key, rule.default)
    if enabled == beneath:
        runtime.pop(rule_key, None)
    else:
        runtime[rule_key] = enabled
    write_runtime_policy(runtime)

    counts = await asyncio.to_thread(block_counts, RATES_WINDOW_DAYS)
    return web.json_response(
        _row(effective_policy()[rule_key], counts.get(rule.counts_under, 0))
    )


async def get_rates(request: web.Request) -> web.Response:
    # The ledger is a growing file and this walks all of it. Off the event loop
    # so the panel's other requests, the heartbeat and an inbound turn keep
    # answering while it reads.
    counts = await asyncio.to_thread(block_counts, RATES_WINDOW_DAYS)
    states = effective_policy()
    rows = [
        {
            "rule": rule.key,
            "blocked": counts.get(rule.counts_under, 0),
            "enabled": states[rule.key].enabled,
        }
        for rule in CAPTURE_RULES
    ]
    # Whatever else turned a record away: the derivation refusal, and any
    # reason no rule here claims, which is how `type_mismatch` shows up. Beside
    # the rules rather than folded into them, because a gauge showing only
    # rules reads as a quiet funnel while records are refused next to it, and
    # folding an unknown reason into a known one is worse than either.
    claimed = {rule.counts_under for rule in CAPTURE_RULES}
    other = [
        {"rule": key, "blocked": count, "enabled": True}
        for key, count in sorted(counts.items())
        if key not in claimed
    ]
    return web.json_response({
        "rows": rows,
        "also_refused": other,
        "window_days": RATES_WINDOW_DAYS,
    })


def register(app: web.Application) -> None:
    app.router.add_get("/api/capture/policy", get_policy)
    app.router.add_post("/api/capture/policy", post_policy)
    app.router.add_get("/api/capture/rates", get_rates)


__all__ = ["RATES_WINDOW_DAYS", "get_policy", "get_rates", "post_policy", "register"]
