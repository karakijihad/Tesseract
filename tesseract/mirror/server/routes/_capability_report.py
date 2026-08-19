"""Assembling the capability report — the read half of `capabilities.py`.

Everything here answers "what is the state of this install": which providers
and services are switched on, which carry a key, which cli binaries are signed
in, which roles resolve, and which chat candidates can be built. Nothing here
writes.

Split out because the route module had grown to hold report assembly, five
handlers, YAML mutation, download prediction and consent in one file, past the
~500-line mark this project pushes back at.
"""

from __future__ import annotations

import os
import shutil

from tesseract.paths import config_dir, home_dir, runtime_dir


def _bundle(bundle=None):
    """The config bundle, read once per report.

    Every section of this report is a different view of the same two YAML
    files, and each section used to open them for itself — four `load_bundle()`
    calls per request, twelve YAML parses, 96% of what the route cost once the
    adapter construction was gone. Same fix `/api/agents` took: one bundle per
    request, not one per consumer. `None` still reads its own, so a caller
    holding one view (a test, a single section) is unchanged.
    """
    if bundle is not None:
        return bundle
    from tesseract.brain.boot import load_bundle

    return load_bundle()


def _integrations(bundle=None) -> list[dict]:
    """One row per switchable thing that is not a model provider, read from the
    two files that declare them. Exactly one of `service` / `channel` names the
    block the row's switch writes.

    It used to be a tuple here, which made this route a second registry
    beside `providers.yaml` — and a third, since the first-run form kept its
    own copy too. Now `providers.yaml::services` names the outside services
    (Brave, Tavily) and each `channels.yaml` block names its own token, so
    adding either kind is a config edit and this route follows.

    **Every service, not only the keyed ones.** The list was gated on
    `api_key_env`, which hid the browser engine — a service with a 700 MB
    download instead of a key, switchable through the same route the whole
    time. What makes a row belong here is that a switch governs it, not that a
    key does.
    """
    rows: list[dict] = []

    services = _bundle(bundle).providers_raw.get("services") or {}
    # The section switch gates every service under it, the way `api.enabled`
    # gates every provider in its tier.
    section_on = bool(services.get("enabled", True))
    for name, block in services.items():
        if not isinstance(block, dict):
            continue
        # The tool list travels as a LIST, not as the label it used to be
        # joined into: the browser block unlocks seven verbs, and one row of
        # the panel cannot be seven times the width of the others. The panel
        # decides how many of them fit.
        unlocks = [
            verb.strip()
            for verb in str(block.get("unlocks") or name).split(",")
            if verb.strip()
        ]
        own = bool(block.get("enabled", True))
        rows.append({
            "name": name,
            "unlocks": unlocks,
            "key_name": block.get("api_key_env") or None,
            # `enabled` is the AND — what the runtime acts on. The two flags
            # travel BESIDE it because the switch on screen writes only the
            # per-service one: folding the section into the box made a service
            # read off with `services.enabled: false`, and clicking it rewrote
            # an already-true flag with nothing visible happening. The provider
            # rows carry `tier_enabled` / `provider_enabled` for this reason.
            "enabled": section_on and own,
            "section_enabled": section_on,
            "service_enabled": own,
            "service": name,
            "channel": None,
        })

    for key_name, name, enabled in _channel_keys():
        # channels.yaml has no section wrapper, so a channel's own flag IS the
        # answer and the two below are it, unfolded.
        rows.append({
            "name": f"{name}_channel",
            "unlocks": [],
            "key_name": key_name,
            "enabled": enabled,
            "section_enabled": True,
            "service_enabled": enabled,
            "service": None,
            "channel": name,
        })
    return rows


def _channel_keys() -> list[tuple[str | None, str, bool]]:
    """`(key_name, channel, enabled)` for every channel.

    Not "every channel declaring a key": that was the same gate removed from
    the services half above, and it would hide a keyless channel exactly the
    way it hid the browser service.
    """
    import yaml

    path = config_dir() / "channels.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    return [
        (block.get("api_key_env") or None, name, bool(block.get("enabled", True)))
        for name, block in raw.items()
        if name != "defaults" and isinstance(block, dict)
    ]

#: Keys inside a section that are settings of the section, not members of it.
#: `defaults` is channels.yaml's — a block shaped exactly like a channel, which
#: is why it needs naming rather than detecting.
_RESERVED_TIER_KEYS = frozenset({"enabled", "defaults"})

# Not a tier: key-gated services with no models. Writable through the same
# switch route, because the operator flips it for the same reason.
_SERVICES_SECTION = "services"

# Also not a tier, and not even the same FILE — a channel's switch lives in
# channels.yaml. It is written here anyway because the operator throws it for
# the third time for the same reason, and a row that is the only one on the
# screen without a switch reads as a row of a different kind.
_CHANNELS_SECTION = "channels"

# The three tier blocks in providers.yaml. Everything else at the top level
# (`chain`, `cost_tracking`, `availability`) is not a tier and carries no
# provider blocks — hence an explicit tuple rather than "every top-level
# mapping". Providers *within* a tier are discovered, never listed.
_TIERS = ("api", "cli", "local")


def key_present(name: str | None) -> bool:
    """Whether `name` names an env var holding a usable key.

    Public because more than this report answers that question — the voice
    catalog reports the same thing per lane. A whitespace-only value is not
    a key, and two surfaces disagreeing about whether the operator holds one
    is worse than either answer.
    """
    if not name:
        return False
    value = os.environ.get(name)
    return bool(value and value.strip())


def _cli_binary_found(command: str) -> bool:
    # Same PATH probe `tesseract/brain/boot.py::build_adapter` runs for the
    # `cli` adapter before spawning it — cheap, no subprocess, no network.
    return shutil.which(command) is not None or shutil.which(f"{command}.cmd") is not None


def _cli_auth_status(command: str, provider_name: str) -> tuple[str, str | None, dict | None]:
    """(status, reason, auth_detail) for one `cli`-tier provider row.

    Layers the process-wide auth cache (`tesseract/brain/cli_auth.py`) on
    top of the existing PATH check — see DESIGN.md §2's state table.
    `auth_detail` carries the richer per-provider detail (login_hint,
    checked_at) for the frontend; `None` when there's nothing cached yet.
    """
    from tesseract.brain import cli_auth

    if not command:
        return "unavailable", "missing 'command' in providers.yaml", None
    if not _cli_binary_found(command):
        return "unavailable", f"cli binary '{command}' not found on PATH", None

    state = cli_auth.get(provider_name)
    if state is None:
        return "unverified", "signed-in status not yet verified", None

    auth_detail = {
        "status": state.status,
        "reason": state.reason,
        "login_hint": state.login_hint,
        "checked_at": state.checked_at,
    }
    if state.status == "ready":
        return "ready", None, auth_detail
    return "unavailable", state.reason, auth_detail


def _provider_rows(bundle=None) -> list[dict]:
    providers_raw = _bundle(bundle).providers_raw
    rows: list[dict] = []
    for tier in _TIERS:
        tier_block = providers_raw.get(tier) or {}
        tier_enabled = bool(tier_block.get("enabled", True))
        for name, block in tier_block.items():
            if name in _RESERVED_TIER_KEYS or not isinstance(block, dict):
                continue
            provider_enabled = bool(block.get("enabled", True))
            enabled = tier_enabled and provider_enabled
            key_name = block.get("api_key_env")
            has_key = key_present(key_name) if key_name else None
            auth_detail: dict | None = None

            if not tier_enabled:
                status, reason = "unavailable", f"tier '{tier}' disabled in providers.yaml"
            elif not provider_enabled:
                status, reason = "unavailable", f"disabled in providers.yaml ({tier}.{name}.enabled=false)"
            elif key_name is not None:
                if has_key:
                    status, reason = "ready", None
                else:
                    status, reason = "unavailable", f"{key_name} not set"
            elif tier == "cli":
                status, reason, auth_detail = _cli_auth_status(block.get("command"), name)
            else:
                # Local, keyless, non-cli provider (ollama reachability,
                # whisper/kokoro model files) — enabled but not cheaply
                # verifiable here. See module docstring.
                status, reason = "unverified", "enabled — see Settings -> Local Models for live status"

            rows.append({
                "tier": tier,
                "provider": name,
                "enabled": enabled,
                # `enabled` above is the AND. The two flags are also reported
                # separately because the toggles write them separately: a
                # provider keeps its own `true` while its tier is off, and
                # turning the tier back on must restore exactly that.
                "tier_enabled": tier_enabled,
                "provider_enabled": provider_enabled,
                "key_name": key_name,
                "key_present": has_key,
                "status": status,
                "reason": reason,
                "auth": auth_detail,
            })
    return rows


def _ref_covers(ref) -> bool:
    """Whether a role's primary/fallback ref is usable enough to keep the
    role off the `broken` list (DESIGN.md §4).

    Scoped strictly to cli auth: a non-cli ref counts as covering the role
    whenever its tier + provider are enabled — general per-tier readiness
    (missing api key, etc.) is a separate, pre-existing concern (the `chat`
    section, `providers` rows above) that this field does not re-litigate.
    A cli ref only counts when the auth cache says `ready`.
    """
    conn = ref.connection
    if not conn.tier_enabled or not conn.enabled:
        return False
    if conn.tier != "cli":
        return True
    from tesseract.brain import cli_auth

    state = cli_auth.get(conn.name)
    return state is not None and state.status == "ready"


def _evaluate_role(role) -> dict:
    base = {"role": role.name, "broken": False, "reason": None, "login_hint": None}
    if role.primary is None or role.primary.connection.tier != "cli":
        return base
    if _ref_covers(role.primary) or any(_ref_covers(fb) for fb in role.fallbacks):
        return base

    from tesseract.brain import cli_auth

    conn = role.primary.connection
    state = cli_auth.get(conn.name)
    if state is None:
        # cli_auth sits below the warm line (`config/boot.yaml`), so a request
        # can land before the probe does — and that must not read as
        # "broken". "Not yet verified" is the `unverified` state, not
        # `unavailable` (DESIGN.md §2); only a completed probe that came
        # back signed-out/failed marks the role broken.
        return base
    return {
        "role": role.name,
        "broken": True,
        "reason": state.reason,
        "login_hint": state.login_hint or (
            conn.auth_check.login_hint if conn.auth_check is not None else None
        ),
    }


def _dismissal_marker_path():
    """`<TESSERACT_HOME>/runtime/cli_auth_notice_dismissed.json` — call-time
    resolution (never a module-level constant), matching the idiom in
    `scheduler/alarms.py::alarms_state_path`: an app update that replaces the
    code tree must not touch operator state living under TESSERACT_HOME.
    """
    return runtime_dir() / "cli_auth_notice_dismissed.json"


def _notice_dismissed() -> bool:
    return _dismissal_marker_path().is_file()


def _role_rows(bundle=None) -> list[dict]:
    return [_evaluate_role(role) for role in _bundle(bundle).roles.values()]


def _chat_candidates(bundle=None) -> tuple[list[dict], bool, str | None]:
    """Per chat_brain candidate: could an adapter be built, and if not, why.

    Asked of `boot.py::adapter_unavailable_reason`, never by building one. A
    report is a read, and constructing the SDK client to answer it is what made
    this route cost 5-27s per poll: each discarded client pulls ~1,300 modules
    in behind it and builds an SSL context off the system trust store. The
    predicate is the same gate `build_adapter` raises from, so the answers here
    are the ones the runtime would give.
    """
    from tesseract.brain.boot import adapter_unavailable_reason, load_chat_brain_chain

    try:
        chain_cfgs = load_chat_brain_chain(bundle)
    except Exception as exc:  # config itself broken — report, don't crash the route
        return [], False, str(exc)

    candidates: list[dict] = []
    any_available = False
    for cfg in chain_cfgs:
        unavailable = adapter_unavailable_reason(cfg.ref)
        any_available = any_available or unavailable is None
        candidates.append({
            "provider": cfg.provider,
            "model": cfg.model,
            "available": unavailable is None,
            "reason": unavailable,
        })
    reason = None
    if not any_available:
        reason = "no chat provider available — " + "; ".join(
            f"{c['provider']} ({c['model']}): {c['reason']}" for c in candidates
        )
    return candidates, any_available, reason


def _build_report() -> dict:
    # One read of providers.yaml + roles.yaml for the whole report. Every
    # section below is a view of the same two files, and each opening them for
    # itself is what the request cost once nothing constructed adapters.
    bundle = _bundle()
    candidates, chat_available, chat_reason = _chat_candidates(bundle)
    return {
        "env_path": str(home_dir() / ".env"),
        "chat": {
            "available": chat_available,
            "reason": chat_reason,
            "candidates": candidates,
        },
        "providers": _provider_rows(bundle),
        "roles": _role_rows(bundle),
        "notice_dismissed": _notice_dismissed(),
        "integrations": [
            {
                **row,
                # Reported separately from the key: a service can be off with
                # its key set, which is the whole point of the switch. A row
                # with no key at all reports False and says so in words.
                "key_present": key_present(row["key_name"]),
            }
            for row in _integrations(bundle)
        ],
    }
