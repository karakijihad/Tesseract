"""Filesystem watcher for `tesseract/config/*.yaml`.

External edits (operator save in VS Code, `delegate_*` write, scheduler
mutator) reflect live in the running Mirror without a restart. Each
config file has a dedicated reloader that mutates the live in-memory
state and emits a `config_reloaded` envelope so the UI can toast.

Watchdog runs its observer thread off the event loop. Per-file changes
are debounced for 250ms (operators sometimes save a file twice in quick
succession, editors sometimes touch the file before writing); the
debounced callback marshals back onto the event loop via
`asyncio.run_coroutine_threadsafe`.

Reloaders are intentionally tolerant: a malformed yaml fires a toast
("providers.yaml reload failed: …") and leaves the running config alone.
The watcher itself never raises — Mirror must keep running even when
the operator's edit is broken.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web
from watchdog.events import FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 0.25

# Names we react to. Anything else under tesseract/config/ is ignored.
# `providers.yaml` (catalog) and `roles.yaml` (wiring) share the
# `reload_models` reloader in `default_reloaders()`, since either change
# rebuilds adapters and reloads cost-ledger pricing.
WATCHED_NAMES = frozenset({
    "providers.yaml",
    "roles.yaml",
    "permissions.yaml",
    "schedule.yaml",
    "vault.yaml",
    "mirror.yaml",
    "identity.yaml",
    "working_set.yaml",
    "channels.yaml",
    "routing.yaml",
    "mcp_servers.yaml",
    "runtime.yaml",
})


@dataclass
class _PendingReload:
    timer: threading.Timer | None = None


class ConfigWatcher:
    """Watch `config_dir/*.yaml`; dispatch debounced async reloaders.

    Lifecycle:
        watcher = ConfigWatcher(app, config_dir, reloaders)
        await watcher.start()
        ...
        await watcher.stop()
    """

    def __init__(
        self,
        app: web.Application,
        config_dir: Path,
        reloaders: dict[str, Callable[[web.Application], Awaitable[None]]],
    ) -> None:
        self._app = app
        self._config_dir = config_dir
        self._reloaders = reloaders
        self._observer: Observer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: dict[str, _PendingReload] = {}
        self._pending_lock = threading.Lock()

    async def start(self) -> None:
        if self._observer is not None:
            return
        self._loop = asyncio.get_running_loop()
        handler = _Handler(self)
        self._observer = Observer()
        self._observer.schedule(handler, str(self._config_dir), recursive=False)
        self._observer.start()
        log.info("config_watcher: observing %s", self._config_dir)

    async def stop(self) -> None:
        if self._observer is None:
            return
        # Cancel pending debounce timers so a stale fire after stop()
        # cannot push work onto a closed loop.
        with self._pending_lock:
            for entry in self._pending.values():
                if entry.timer is not None:
                    entry.timer.cancel()
            self._pending.clear()
        try:
            self._observer.stop()
            self._observer.join(timeout=2.0)
        except Exception:
            log.exception("config_watcher: observer stop failed")
        self._observer = None
        log.info("config_watcher: stopped")

    def _on_change(self, name: str) -> None:
        """Called from watchdog's observer thread. Debounce per-file."""
        with self._pending_lock:
            entry = self._pending.get(name)
            if entry is None:
                entry = _PendingReload()
                self._pending[name] = entry
            if entry.timer is not None:
                entry.timer.cancel()
            entry.timer = threading.Timer(
                DEBOUNCE_SECONDS,
                self._fire_debounced,
                args=(name,),
            )
            entry.timer.daemon = True
            entry.timer.start()

    def _fire_debounced(self, name: str) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        reloader = self._reloaders.get(name)
        if reloader is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(reloader(self._app), loop)
        except RuntimeError:
            # Loop is shutting down — drop quietly.
            return


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: ConfigWatcher) -> None:
        self._watcher = watcher

    def on_modified(self, event: FileModifiedEvent) -> None:
        self._dispatch(event.src_path)

    def on_created(self, event) -> None:
        # Some editors save by creating a new file then renaming it.
        self._dispatch(event.src_path)

    def on_moved(self, event) -> None:
        # `move` events fire on rename-on-save; the destination is the
        # operator's intent, so dispatch on `dest_path`.
        dest = getattr(event, "dest_path", None)
        if dest:
            self._dispatch(dest)

    def _dispatch(self, raw_path: str | bytes) -> None:
        path = raw_path.decode("utf-8") if isinstance(raw_path, bytes) else raw_path
        name = Path(path).name
        if name in WATCHED_NAMES:
            self._watcher._on_change(name)


# ── Reloaders ─────────────────────────────────────────────────────────


async def reload_models(app: web.Application) -> None:
    """providers.yaml / roles.yaml → rebuild adapters + cost ledger pricing + voice runtime."""
    summary_parts: list[str] = []
    detail: dict[str, Any] = {}
    try:
        from tesseract.brain.boot import rebuild_adapters

        result = rebuild_adapters(app)
        if result.get("chat_brain"):
            summary_parts.append(f"chat_brain → {result['chat_brain']}")
            detail["chat_brain"] = result["chat_brain"]
        if result.get("voice"):
            summary_parts.append("voice runtime reloaded")
            detail["voice"] = result["voice"]
            # A rebuilt engine is a new chain, so the "a different voice is
            # speaking" notice has to be able to fire against it. The latch is
            # per session and outlives the engine that set it, which would
            # otherwise leave an operator who changed their primary voice
            # hearing nothing about the NEW chain falling through — for the
            # rest of the session, describing lanes that are no longer in it.
            # Guarded on its own: clearing a notice is a courtesy, and an
            # import failure here would be caught by the reload's own handler
            # and reported to the operator as "models NOT reloaded" — a worse
            # and wrong message about a reload that succeeded.
            try:
                from tesseract.mirror.server.tts import clear_fallback_notices

                clear_fallback_notices(app)
            except Exception as exc:  # noqa: BLE001
                log.info("config: could not reset the voice fallback notice (%s)", exc)
        # Keyed on presence, not truthiness: `reranker: None` is the valid
        # "operator cleared the role" result, and reporting it by falsiness
        # renders a real change as "no live changes". A failure has to reach
        # `summary` too — `detail` alone is invisible in the toast, so an
        # unapplied reload would read as a successful one.
        if "reranker" in result:
            ref = result["reranker"]
            summary_parts.append(f"reranker → {ref}" if ref else "reranker disabled")
            detail["reranker"] = ref
        if result.get("reranker_error"):
            summary_parts.append(f"reranker NOT reloaded: {result['reranker_error']}")
            detail["reranker_error"] = result["reranker_error"]
    except Exception as exc:
        log.exception("config_watcher: providers/roles rebuild_adapters failed")
        await _emit_failed(app, "providers.yaml", str(exc))
        return

    ledger = app.get("cost_ledger")
    if ledger is not None:
        try:
            ledger.reload()
            summary_parts.append("cost pricing reloaded")
        except Exception as exc:
            log.exception("config_watcher: cost_ledger.reload failed")
            detail["cost_pricing_error"] = str(exc)

    # TC-5 — bridge into the running agent controller daemon (if any).
    # Best-effort: a missing controller short-circuits in
    # ``notify_controller_reload`` without raising.
    await _notify_controller_if_alive(app, target="config", detail=detail, summary_parts=summary_parts)

    summary = " · ".join(summary_parts) or "no live changes"
    await _emit_reloaded(app, "providers.yaml", summary, detail)


async def _skipped(app: web.Application, file: str, subsystem: str) -> None:
    """Say that a save landed on disk and reached nothing live.

    A reload whose subsystem never started must not return silently: the
    operator's save then looks like it worked. It has — the file is written and
    the next process start reads it — but nothing in the running app changed,
    and the two are not the same thing. Logged as well as toasted, because the
    toast is behind a setting and this is not.
    """
    log.warning(
        "config_watcher: %s saved, but %s is not running — the change applies "
        "when it starts, not now", file, subsystem,
    )
    await _emit_failed(app, file, f"{subsystem} is not running — applies on next start")


async def reload_mcp_servers(app: web.Application) -> None:
    """`mcp_servers.yaml` → diff the outbound MCP-client allowlist and
    connect/disconnect servers in place. Reports a
    skip when the client manager never built (registry unavailable)."""
    manager = app.get("mcp_clients")
    if manager is None:
        await _skipped(app, "mcp_servers.yaml", "the outbound MCP client manager")
        return
    try:
        from tesseract.config.mcp_client import load_mcp_client_config

        result = await manager.reload(load_mcp_client_config())
    except Exception as exc:
        log.exception("config_watcher: mcp_servers.yaml reload failed")
        await _emit_failed(app, "mcp_servers.yaml", str(exc))
        return
    parts: list[str] = []
    if result["added"]:
        parts.append(f"connected {', '.join(result['added'])}")
    if result["removed"]:
        parts.append(f"disconnected {', '.join(result['removed'])}")
    await _emit_reloaded(
        app, "mcp_servers.yaml", " · ".join(parts) or "no changes", result
    )


async def reload_permissions(app: web.Application) -> None:
    """`permissions.yaml` → rebuild PermissionPolicy in place."""
    config = app.get("config")
    if config is None or config.permissions is None:
        await _skipped(app, "permissions.yaml", "the permission policy")
        return
    try:
        from tesseract.mirror.server.config import PERMISSIONS_YAML

        config.permissions.reload(PERMISSIONS_YAML)
    except Exception as exc:
        log.exception("config_watcher: permissions.yaml reload failed")
        await _emit_failed(app, "permissions.yaml", str(exc))
        return
    summary_parts: list[str] = [
        f"posture rules reloaded (mode={config.permissions.mode})"
    ]
    detail: dict[str, Any] = {}
    # TC-5 — permissions changes invalidate the controller's tool
    # registry. Bridge into the controller daemon if it is alive.
    await _notify_controller_if_alive(
        app, target="tools", detail=detail, summary_parts=summary_parts
    )
    await _emit_reloaded(
        app, "permissions.yaml", " · ".join(summary_parts), detail
    )


async def reload_schedule(app: web.Application) -> None:
    """`schedule.yaml` → tell the engine to diff + re-arm without restart."""
    scheduler = app.get("scheduler")
    if scheduler is None:
        await _skipped(app, "schedule.yaml", "the scheduler")
        return
    try:
        result = scheduler.reload_jobs()
    except Exception as exc:
        log.exception("config_watcher: schedule.yaml reload failed")
        await _emit_failed(app, "schedule.yaml", str(exc))
        return
    parts: list[str] = []
    if result.get("added"):
        parts.append(f"+{len(result['added'])} added")
    if result.get("removed"):
        parts.append(f"-{len(result['removed'])} removed")
    if result.get("changed"):
        parts.append(f"~{len(result['changed'])} re-armed")
    if result.get("refused"):
        # A row the FIRE refuses tells the operator itself. One a reload
        # refuses never fired, so this is the only place it can be said.
        names = ", ".join(result["refused"])
        parts.append(f"{names} turned off, the tool it runs needs your approval")
    if not parts:
        # Self-write path: scheduler.set_enabled / set_cadence persists
        # schedule.yaml, watchdog re-fires reload_schedule, the diff is
        # empty. Suppress the toast — operator already knows about the
        # change because they triggered it from the UI.
        return
    summary = ", ".join(parts)
    await _emit_reloaded(app, "schedule.yaml", summary, dict(result))


async def reload_vault(app: web.Application) -> None:
    """`vault.yaml` → re-read into app['vault_config'] (used by VaultLintTool etc)."""
    try:
        from tesseract.brain.boot import load_vault_config

        vault_cfg = load_vault_config()
    except Exception as exc:
        log.exception("config_watcher: vault.yaml reload failed")
        await _emit_failed(app, "vault.yaml", str(exc))
        return
    app["vault_config"] = vault_cfg
    await _emit_reloaded(app, "vault.yaml", "vault config reloaded", {})


async def reload_channels(app: web.Application) -> None:
    """`channels.yaml` → refresh the typed config.

    Re-reads the file into the typed :class:`ChannelsConfig` and stashes it on
    ``app["channels_config"]`` so later reads (attachment caps, the gate
    policy, the channel document's generated regions) see the new values on the
    next turn.

    There is nothing to push at an adapter: a channel session is bounded by
    compaction (`mirror/server/after_turn.py`), which takes no
    per-channel knob and so needs no reload.
    """
    try:
        from tesseract.integrations._channels_config import load_channels_config
    except Exception as exc:
        log.exception("config_watcher: channels.yaml import failed")
        await _emit_failed(app, "channels.yaml", str(exc))
        return

    try:
        typed = load_channels_config()
    except Exception as exc:
        log.exception("config_watcher: channels.yaml typed reload failed")
        await _emit_failed(app, "channels.yaml", str(exc))
        return
    app["channels_config"] = typed
    channels = typed.known_channels()
    summary = f"channels.yaml reloaded ({', '.join(channels)})"
    await _emit_reloaded(app, "channels.yaml", summary, {"channels": channels})


async def reload_routing(app: web.Application) -> None:
    """`routing.yaml` — check the table and say what it now says.

    Nothing is cached: every send reads the table, so an edit is already
    live by the time this runs. What this adds is the answer. A table with a
    kind spelled wrong is refused when it is read, and without this the
    operator would learn that from a message that never arrived.
    """
    try:
        from tesseract.orchestrator.autonomy.outbound_routing import (
            load_outbound_routing,
            routing_summary,
        )
    except Exception as exc:
        log.exception("config_watcher: routing.yaml import failed")
        await _emit_failed(app, "routing.yaml", str(exc))
        return

    try:
        routing = load_outbound_routing()
    except Exception as exc:
        log.exception("config_watcher: routing.yaml reload failed")
        await _emit_failed(app, "routing.yaml", str(exc))
        return

    lines = routing_summary(routing)
    log.info("config_watcher: routing.yaml reloaded — %s", "; ".join(lines))
    await _emit_reloaded(
        app,
        "routing.yaml",
        "where each message goes has been updated",
        {"routes": lines},
    )


async def reload_mirror(app: web.Application) -> None:
    """`mirror.yaml` — bind/CORS still need a restart, but the
    `ui.show_config_reload_toasts` flag IS hot-reloadable. Without this, an
    external edit to that key has no effect until
    the operator opens Settings and saves the section, which contradicts
    the workstream's "external YAML edits reflect live" goal.
    """
    detail: dict[str, Any] = {"requires_restart": True}
    summary_parts: list[str] = []
    try:
        from tesseract.mirror.server.config import MIRROR_YAML
        import yaml as _yaml

        raw = _yaml.safe_load(MIRROR_YAML.read_text(encoding="utf-8")) or {}
        ui = (raw.get("ui") or {}) if isinstance(raw, dict) else {}
        if "show_config_reload_toasts" in ui:
            new_flag = bool(ui.get("show_config_reload_toasts", True))
            old_flag = bool(app.get("config_reload_toasts_enabled", True))
            app["config_reload_toasts_enabled"] = new_flag
            if new_flag != old_flag:
                detail["show_config_reload_toasts"] = new_flag
                summary_parts.append(f"toast toggle → {new_flag}")
    except Exception as exc:
        log.exception("config_watcher: mirror.yaml refresh failed")
        await _emit_failed(app, "mirror.yaml", str(exc))
        return

    summary_parts.append("restart required for bind/CORS changes")
    await _emit_reloaded(app, "mirror.yaml", " · ".join(summary_parts), detail)


async def reload_identity(app: web.Application) -> None:
    """`identity.yaml` — the whole file is hot. The wake-word gate builds its
    phrase from `name` on every utterance, so a rename that only landed at the
    next restart would leave the assistant answering to a name the operator has
    already changed. Name, operator name and the wake-word block are re-parsed
    together and swapped onto `app["config"]`.

    The rest of the file — `born_at`, `documentation`, `time_of_day_buckets` —
    is re-read from disk by the prompt builder on every turn and needs nothing
    here.
    """
    detail: dict[str, Any] = {}
    summary_parts: list[str] = []
    try:
        from tesseract.mirror.server.config import IDENTITY_YAML
        import yaml as _yaml

        raw = _yaml.safe_load(IDENTITY_YAML.read_text(encoding="utf-8")) or {}
        # Parse BEFORE touching app state, so that "a broken edit changes
        # nothing" is true rather than nearly true.
        _apply_identity(app, _parse_identity(raw), detail, summary_parts)
    except Exception as exc:
        # A success toast here would be the worst outcome: the operator's
        # edit did NOT take, the gate is still running the old name, and
        # the only signal said "reloaded".
        log.exception("config_watcher: identity.yaml refresh failed")
        await _emit_failed(app, "identity.yaml", str(exc))
        return

    if not summary_parts:
        summary_parts.append("identity unchanged")
    await _emit_reloaded(app, "identity.yaml", " · ".join(summary_parts), detail)


async def reload_working_set(app: web.Application) -> None:
    """`working_set.yaml` — which tools carry a schema every turn.

    Hot for the same reason the panel that writes it applies immediately: this
    is a spending dial, and a dial whose effect waits for a restart is one the
    operator turns twice because nothing happened the first time.

    Re-marks the live registry. A tool REMOVED from the file goes back to
    extended, which is why `_apply_tool_tiers` sets both directions rather than
    only promoting.
    """
    from tesseract.brain.boot import _apply_tool_tiers, core_tool_names

    registry = app.get("tool_registry")
    if registry is None:
        return
    try:
        before = len(core_tool_names())
        names = core_tool_names(refresh=True)
        _apply_tool_tiers(registry)
    except Exception as exc:
        # A bad edit leaves the previous set marked and says so. Falling back
        # to "carry nothing" would be an invisible capability cut.
        log.exception("config_watcher: working_set.yaml refresh failed")
        await _emit_failed(app, "working_set.yaml", str(exc))
        return

    await _emit_reloaded(
        app,
        "working_set.yaml",
        f"{len(names)} tools ride every turn (was {before})",
        {"core_count": len(names)},
    )


async def reload_runtime(app: web.Application) -> None:
    """`runtime.yaml` — the chat concurrency cap, live.

    The cap is read into `app` once at boot and consulted per turn from there,
    so an edit to the file alone changed nothing until a restart. The rail's
    own PATCH route reaches in and updates `app` for exactly that reason, which
    left a hand-edit behaving differently from the same number typed into the
    gear. Only this one key is hot; every other value in the file is read at
    boot and stays that way.
    """
    from tesseract.config.runtime_limits import (
        CONCURRENCY_KEY,
        load_max_concurrent_chat_turns_per_provider,
    )
    from tesseract.paths import config_dir

    try:
        value = load_max_concurrent_chat_turns_per_provider(config_dir() / "runtime.yaml")
    except Exception as exc:
        log.exception("config_watcher: runtime.yaml reload failed")
        await _emit_failed(app, "runtime.yaml", str(exc))
        return

    before = app.get(CONCURRENCY_KEY)
    if value == before:
        return
    app[CONCURRENCY_KEY] = value
    # Same drop, and the same bound on it, as the gear's route: a turn already
    # streaming holds its own semaphore and finishes under the old cap.
    if app.get("chat_turn_semaphores") is not None:
        app["chat_turn_semaphores"] = {}

    await _emit_reloaded(
        app,
        "runtime.yaml",
        f"{value} chats may stream at once (was {before})",
        {"max_concurrent_chat_turns_per_provider": value},
    )


def _parse_identity(raw: Any) -> tuple[str, str, Any] | None:
    """Validate the identity keys. Raises on a malformed file, which is what
    keeps `reload_identity` from applying half of one."""
    from tesseract.mirror.server.config import load_identity

    if not isinstance(raw, dict):
        return None
    return load_identity(raw)


def refresh_identity(app: web.Application, path: Path) -> dict[str, Any]:
    """Re-read `path`'s identity keys and swap them onto the live config now.

    The identity write endpoint calls this instead of leaving the job to the
    watcher: the debounce is ~250ms, and in that window the wake-word gate
    still answers to the previous name and a client that re-reads
    `/api/identity` right after saving gets the value it just changed away
    from. Raises on a malformed block, same as the watcher — the caller has
    just written the file, so a parse failure there is its own bug and must
    not be swallowed into a success response.
    """
    import yaml as _yaml

    raw = _yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    _apply_identity(app, _parse_identity(raw), {}, [])
    config = app.get("config")
    if config is None:
        return {}
    return {
        "name": config.entity_name,
        "operator_name": config.operator_name,
        "wake_word": {
            "enabled": config.wake_word.enabled,
            "prefix": config.wake_word.prefix,
        },
    }


def _apply_identity(
    app: web.Application,
    identity: tuple[str, str, Any] | None,
    detail: dict[str, Any],
    summary_parts: list[str],
) -> None:
    """Swap an already-validated identity onto the live `ServerConfig`.

    `ServerConfig` is frozen, so this replaces the object rather than
    mutating it. Every other field carries over **by reference** — the
    same `PermissionPolicy` that `reload_permissions` mutates in place and
    the same `models` dict `rebuild_adapters` refreshes — so neither
    reloader loses its handle. Every consumer reads `app["config"].x` at
    call time, which is what makes the swap safe.
    """
    from dataclasses import replace

    config = app.get("config")
    if config is None or identity is None:
        return
    entity_name, operator_name, wake_word = identity
    if (
        entity_name == config.entity_name
        and operator_name == config.operator_name
        and wake_word == config.wake_word
    ):
        return
    app["config"] = replace(
        config,
        entity_name=entity_name,
        operator_name=operator_name,
        wake_word=wake_word,
    )
    detail["identity"] = {
        "name": entity_name,
        "operator_name": operator_name,
        "wake_word_enabled": wake_word.enabled,
    }
    summary_parts.append(f"identity reloaded (name={entity_name})")


# ── TC-5: controller reload bridge ───────────────────────────────────


async def _notify_controller_if_alive(
    app: web.Application,
    *,
    target: str,
    detail: dict[str, Any],
    summary_parts: list[str],
) -> None:
    """Fire the controller-side reload IPC if the controller daemon is
    listening. Silent + best-effort: nothing here may raise back into the
    Mirror reloader. Success adds a ``controller_reload`` block to the
    toast detail so the operator sees both reloads landed."""

    # Allow tests / explicit operator policy to opt out.
    if app.get("controller_reload_bridge_disabled"):
        return
    try:
        from tesseract.orchestrator.agent_controller import (
            notify_controller_reload,
            port_file_path,
        )
    except Exception:  # noqa: BLE001
        log.debug("config_watcher: controller bridge import failed", exc_info=True)
        return
    try:
        if not port_file_path().exists():
            return
    except Exception:  # noqa: BLE001
        return
    try:
        result = await notify_controller_reload(target)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        log.debug("config_watcher: controller bridge raised: %s", exc)
        return
    detail["controller_reload"] = result
    if result.get("ok"):
        reloaded = result.get("reloaded") or []
        failed = result.get("failed") or []
        pending = result.get("pending_turns") or 0
        sessions = result.get("session_count") or 0
        parts: list[str] = []
        if reloaded:
            parts.append(f"controller reloaded: {', '.join(reloaded)}")
        if failed:
            parts.append(f"controller failed: {', '.join(failed)}")
        if pending:
            parts.append(f"controller pending: {pending} turns")
        if sessions:
            parts.append(f"controller sessions: {sessions}")
        if parts:
            summary_parts.append(" / ".join(parts))
    else:
        code = result.get("code", "unknown")
        summary_parts.append(f"controller reload skipped ({code})")


# ── Envelope emission ────────────────────────────────────────────────


async def _emit_reloaded(
    app: web.Application, file: str, summary: str, detail: dict[str, Any]
) -> None:
    if not _toasts_enabled(app):
        return
    from tesseract.mirror.server.envelope import make_config_reloaded
    from tesseract.mirror.server.session import send_envelope

    sessions = app.get("server_sessions") or {}
    for sess in list(sessions.values()):
        env = make_config_reloaded(
            getattr(sess, "session_id", ""),
            file=file,
            summary=summary,
            detail=detail,
            ok=True,
        )
        try:
            await send_envelope(sess, env)
        except Exception:
            log.exception("config_watcher: send_envelope failed")


async def _emit_failed(app: web.Application, file: str, error: str) -> None:
    if not _toasts_enabled(app):
        return
    from tesseract.mirror.server.envelope import make_config_reloaded
    from tesseract.mirror.server.session import send_envelope

    sessions = app.get("server_sessions") or {}
    for sess in list(sessions.values()):
        env = make_config_reloaded(
            getattr(sess, "session_id", ""),
            file=file,
            summary=f"reload failed: {error}",
            detail={"error": error},
            ok=False,
        )
        try:
            await send_envelope(sess, env)
        except Exception:
            log.exception("config_watcher: send_envelope failed")


def _toasts_enabled(app: web.Application) -> bool:
    """Honor `mirror.yaml ui.show_config_reload_toasts`. Default True."""
    flag = app.get("config_reload_toasts_enabled")
    if flag is None:
        return True
    return bool(flag)


def default_reloaders() -> dict[str, Callable[[web.Application], Awaitable[None]]]:
    # providers.yaml + roles.yaml share the models reloader: editing either
    # triggers `boot.rebuild_adapters` + cost-ledger reload, since both feed
    # into the same `ConfigBundle`.
    return {
        "providers.yaml": reload_models,
        "roles.yaml": reload_models,
        "permissions.yaml": reload_permissions,
        "schedule.yaml": reload_schedule,
        "vault.yaml": reload_vault,
        "mirror.yaml": reload_mirror,
        "identity.yaml": reload_identity,
        "working_set.yaml": reload_working_set,
        "channels.yaml": reload_channels,
        "routing.yaml": reload_routing,
        "mcp_servers.yaml": reload_mcp_servers,
        "runtime.yaml": reload_runtime,
    }
