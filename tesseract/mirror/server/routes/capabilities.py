"""GET /api/capabilities — capability report, not a setup gate.

Nothing in TESSERACT requires an API key or a specific provider. This
route reports, per provider and per
chat_brain candidate, its `status` and — when not `ready` — WHY: no API key
set, disabled via `providers.yaml`'s `enabled` bools, or (cli tier) the
binary isn't on PATH or isn't signed in. It never gates the UI (no
`ready`-for-the-whole-app flag) and never returns a secret VALUE, only key
NAMES and presence booleans.

`status` is one of three states, not a bool — collapsing to true/false lost
information (review fix-pass, Important-2): a keyless local provider
(ollama/whisper/kokoro) that is merely `enabled: true` in providers.yaml is
NOT the same claim as "verified working" (no binary, no model files, no
reachable server checked here — that live diagnostic already exists per-
provider in Settings -> Local Models, and a network probe does not belong
in a settings-read endpoint). So:
  - "ready": actually checked and good (key present; or, cli tier, the
    binary was found on PATH AND the cached auth probe says signed in —
    see `tesseract/brain/cli_auth.py`).
  - "unavailable": checked and NOT good (disabled, missing key, binary not
    found, or cli tier signed out / probe failed) — `reason` says which.
  - "unverified": enabled and nothing cheap here can confirm or deny it
    further — local keyless providers, or a cli provider whose boot auth
    probe hasn't landed in the cache yet.

`roles` extends the report (cli-auth DESIGN.md §4/§5): per `roles.yaml`
role, whether it is `broken` — its `primary` resolves to an unauthenticated
`cli` provider and no `fallback` resolves to something usable. Scoped
strictly to cli auth: an api-tier primary is never reported broken by this
field, even if its key is missing (that's the existing `chat` section's
concern for chat_brain specifically).

`POST /api/capabilities/reverify` forces a fresh cli-auth probe (invalidate
+ refresh) and returns the same report shape.

`notice_dismissed` reflects whether the operator dismissed the frontend's
first-run notice (DESIGN.md §5) — persisted via `POST /api/capabilities/
dismiss` as a marker under `<TESSERACT_HOME>/runtime/`, so it survives a
restart. It is independent of `roles`: the frontend self-suppresses the
notice whenever no role is broken, regardless of this flag.

`POST /api/capabilities/provider-enabled` writes the `enabled` bools this
report reads — the tier switch (`<tier>.enabled`) or one provider's
(`<tier>.<provider>.enabled`). It does not predict what a toggle breaks:
`boot.py::build_adapter` already raises a message naming the exact flag
that is false, and fallback chains already skip a disabled ref the way
they skip a missing key. A pre-flight simulator would be a second source
of truth for resolution that can drift from the resolver itself.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from tesseract.config import factory_reset
from tesseract.lib.yaml_io import atomic_write_text, round_trip_yaml
from tesseract.mirror.server.routes._capability_report import (
    _CHANNELS_SECTION,
    _RESERVED_TIER_KEYS,
    _SERVICES_SECTION,
    _TIERS,
    _build_report,
    _dismissal_marker_path,
)
from tesseract.mirror.server.routes._capability_switches import (
    _catalog,
    _providers_yaml_path,
    _queued_download,
    _record_consent_for,
    _set_enabled_flag,
)
from tesseract.mirror.server.routes._localhost import is_localhost_request
from tesseract.paths import config_dir


async def capabilities_status(request: web.Request) -> web.Response:
    # `_build_report` parses the config tree and probes providers — synchronous
    # file IO and YAML, measured at seconds on the loop, which starved every
    # other request behind it (event-loop lag warnings of 11-16s, 2026-08-13).
    return web.json_response(await asyncio.to_thread(_build_report))


async def capabilities_reverify(request: web.Request) -> web.Response:
    """Force a fresh cli-auth probe (DESIGN.md §3/§5), then return the same
    report shape as GET /api/capabilities so the caller doesn't need a
    follow-up GET."""
    from tesseract.brain import cli_auth

    cli_auth.invalidate()
    await cli_auth.refresh()
    return web.json_response(_build_report())


async def capabilities_dismiss(request: web.Request) -> web.Response:
    """POST /api/capabilities/dismiss — persist the first-run notice's
    dismissal under <TESSERACT_HOME>/runtime (DESIGN.md §5). Returns the
    same report shape so the caller doesn't need a follow-up GET."""
    atomic_write_text(
        _dismissal_marker_path(),
        json.dumps({"dismissed_at": datetime.now(timezone.utc).isoformat()}),
    )
    return web.json_response(_build_report())


async def capabilities_set_provider_enabled(request: web.Request) -> web.Response:
    """POST /api/capabilities/provider-enabled — flip a tier or provider switch.

    Body: `{tier, provider, enabled}`. `provider: null` targets the tier
    switch itself. Returns the same report shape as GET /api/capabilities,
    which `load_bundle()` re-reads from disk, so the response already
    reflects the write. The config watcher rebuilds adapters and the voice
    runtime off the same file change — no restart.

    Plus `pending_download`: what turning this on has queued for the next
    start, or null. Nothing is fetched here, and until this field existed
    nothing said so — the switch went green and the download arrived on a
    later launch with no explanation attached to it.
    """
    # Same-machine only. This route already wrote `providers.yaml`; what this
    # release added is that it also RECORDS CONSENT — an authoritative answer
    # that outranks config and authorises a later download. `env_keys.py` sets
    # the precedent for exactly this shape of endpoint, and the reasoning in
    # `_localhost.py` is that the loopback bind is a setting rather than a law.
    if not is_localhost_request(request):
        return web.json_response({"error": "localhost only"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)

    tier = body.get("tier")
    provider = body.get("provider")
    enabled = body.get("enabled")

    # `services` is a section like a tier — same two switches, same writer.
    # It is not IN `_TIERS`, because that tuple is what the provider report
    # walks and a service has no models to walk. `channels` is the same shape
    # in a different file, handled below.
    sections = (*_TIERS, _SERVICES_SECTION, _CHANNELS_SECTION)
    if tier not in sections:
        return web.json_response(
            {"error": f"tier must be one of {', '.join(sections)}"},
            status=400,
        )
    if not isinstance(enabled, bool):
        return web.json_response({"error": "enabled must be a boolean"}, status=400)
    if provider is not None and not isinstance(provider, str):
        return web.json_response(
            {"error": "provider must be a string or null"}, status=400
        )
    if provider in _RESERVED_TIER_KEYS:
        return web.json_response(
            {"error": f"'{provider}' is a reserved key, not a provider"}, status=400
        )

    # A channel is one level shallower: channels.yaml has no section wrapper,
    # so the block IS the top-level key and there is no switch above it.
    channel_switch = tier == _CHANNELS_SECTION
    if channel_switch and provider is None:
        return web.json_response(
            {"error": "channels has no section switch — name a channel"}, status=400
        )

    path = config_dir() / "channels.yaml" if channel_switch else _providers_yaml_path()
    if not path.is_file():
        return web.json_response({"error": f"{path} not found"}, status=500)

    def _apply(doc: Any) -> None:
        if channel_switch:
            block = doc.get(provider)
            if not isinstance(block, dict):
                raise KeyError(str(provider))
            _set_enabled_flag(block, enabled)
            return
        tier_block = doc.get(tier)
        if not isinstance(tier_block, dict):
            raise KeyError(tier)
        if provider is None:
            _set_enabled_flag(tier_block, enabled)
            return
        block = tier_block.get(provider)
        if not isinstance(block, dict):
            raise KeyError(f"{tier}.{provider}")
        _set_enabled_flag(block, enabled)

    try:
        round_trip_yaml(path, _apply)
    except KeyError as exc:
        return web.json_response(
            {"error": f"{path.name} has no {exc}"}, status=404
        )

    # One read of the just-written catalog for the whole request. Both helpers
    # need it and both used to open the file themselves.
    catalog = _catalog()
    _record_consent_for(provider, enabled, tier=tier, doc=catalog)
    # Carried on this response only. The report GET is a description of what
    # is; this is what the operator's click just set in motion, and it belongs
    # to the click rather than to the state.
    # Same `to_thread` as its GET sibling and as the reset route below. This
    # one was already on the loop before either existed; it is the identical
    # defect two handlers apart, and leaving it because only the newer one was
    # reported would keep a measured 11-16s staller in the file.
    return web.json_response(
        {
            **(await asyncio.to_thread(_build_report)),
            "pending_download": _queued_download(
                provider, enabled, tier=tier, doc=catalog
            ),
        }
    )



async def capabilities_reset_defaults(request: web.Request) -> web.Response:
    """POST /api/capabilities/reset-defaults — put every switch back to shipped.

    The restore itself is `config/factory_reset.py`, the same mechanism the four
    Settings panes reset through: read the factory `providers.yaml` in the
    sealed `app/` tree, write the operator's under `TESSERACT_HOME`. This route
    exists rather than folding into `POST /api/settings/reset-defaults` because
    a switch is not only a value — it is an answer, and the ledger has to hear
    it. See `_record_consent_for` below.

    `factory_reset.SCOPES["capabilities"]` names the keys: the tier and service
    switches and every provider under them. `channels` is deliberately absent —
    a channel is credentials the operator wired up in another file, and
    switching one back on is opening a bridge, not restoring a default.
    """
    if not is_localhost_request(request):
        return web.json_response({"error": "localhost only"}, status=401)

    try:
        moved, missing = await asyncio.to_thread(factory_reset.restore, "capabilities")
    except (OSError, ValueError) as exc:  # ValueError covers yaml.YAMLError
        return web.json_response(
            {"error": f"could not read the shipped config: {exc}"}, status=500
        )

    # Consent is recorded for every switch that MOVED, in whichever direction,
    # exactly as a manual toggle does. Recording only the ONs was the bug
    # `_record_consent_for`'s own docstring describes: a ledger answer outranks
    # config, so a lane the reset switched back OFF would keep its earlier
    # `granted` and the reconciler would go on repairing something the operator
    # had just turned off. A reset has to leave the same trail as clicking each
    # switch by hand would.
    #
    # A change's `path` is `(<tier>,)` or `(<tier>, <provider>)` plus the
    # trailing `enabled` — the switch this pane draws, with the key it lives in.
    catalog = _catalog()
    changed: list[str] = []
    for change in moved:
        tier, *rest = change.path[:-1]
        changed.append(".".join(change.path[:-1]))
        _record_consent_for(
            rest[0] if rest else None, bool(change.value), tier=tier, doc=catalog
        )

    # Off the loop, for the reason `capabilities_status` states above it:
    # `_build_report` is synchronous config parsing plus provider probes,
    # measured at seconds, and it starved every request behind it. A reset
    # reaches more switches than a single toggle does, so this is the worst
    # place in the file to have left it on the loop.
    return web.json_response(
        {
            **(await asyncio.to_thread(_build_report)),
            "reset": {"changed": changed, "missing": missing},
        }
    )


async def capabilities_dependencies(request: web.Request) -> web.Response:
    """GET /api/capabilities/dependencies — what the last launch pass found.

    Reads the artifact; it does NOT reconcile. The pass runs at launch and on
    demand, and a Settings tab polling this must not be able to start a
    hardware probe on every refresh.

    `?refresh=1` runs a real pass, for the operator who has just fixed
    something and wants to see it reflected without relaunching.

    The response leads with `attention` rather than the whole set, because
    that is what a surface renders: a dependency that is fine has nothing to
    say, and a list that is usually full is one people stop reading.
    """
    from tesseract.capability.reconcile import run
    from tesseract.capability.state import read_state

    refresh = request.query.get("refresh", "").lower() in ("1", "true", "yes")
    state = None if refresh else read_state()
    if state is None:
        state = await run()

    return web.json_response(
        {
            "checked_at": state.checked_at,
            "attention": [
                {
                    "id": record.id,
                    "state": record.state.value,
                    "reason": record.reason,
                    "size_mb": record.size_mb,
                    "consent": record.consent.value,
                }
                for record in state.attention
            ],
            "advice": [
                {"id": item.id, "text": item.text, "at": item.at}
                for item in state.advice
            ],
            "dependencies": {
                dep_id: {
                    "state": record.state.value,
                    "consent": record.consent.value,
                    "consent_origin": record.consent_origin.value,
                    "reason": record.reason,
                    "size_mb": record.size_mb,
                    "version": record.version,
                }
                for dep_id, record in state.dependencies.items()
            },
        }
    )


def register(app: web.Application) -> None:
    app.router.add_get("/api/capabilities", capabilities_status)
    app.router.add_get("/api/capabilities/dependencies", capabilities_dependencies)
    app.router.add_post("/api/capabilities/reverify", capabilities_reverify)
    app.router.add_post("/api/capabilities/dismiss", capabilities_dismiss)
    app.router.add_post(
        "/api/capabilities/provider-enabled", capabilities_set_provider_enabled
    )
    app.router.add_post(
        "/api/capabilities/reset-defaults", capabilities_reset_defaults
    )


__all__ = [
    "register",
    "capabilities_status",
    "capabilities_dependencies",
    "capabilities_reverify",
    "capabilities_dismiss",
    "capabilities_set_provider_enabled",
    "capabilities_reset_defaults",
]
