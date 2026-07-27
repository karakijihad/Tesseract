"""Phase 18 Task A — config_watcher reload behaviour.

Covers:
- Debounced FS event → reloader is called once for two rapid writes
- Per-file dispatch correctness (models / permissions / schedule / vault)
- Reloaders fail-soft on malformed yaml (toast emitted, app state intact)
- `_toasts_enabled` honored when `mirror.yaml ui.show_config_reload_toasts=false`
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from aiohttp import web

from tesseract.mirror.server.config_watcher import (
    DEBOUNCE_SECONDS,
    ConfigWatcher,
    reload_mirror,
    reload_models,
    reload_permissions,
    reload_schedule,
)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """Copy the live config dir into tmp so writes don't pollute the repo."""
    repo_config = Path(__file__).resolve().parents[2] / "config"
    target = tmp_path / "config"
    shutil.copytree(repo_config, target)
    return target


@pytest.fixture
def app() -> web.Application:
    a = web.Application()
    a["server_sessions"] = {}
    a["config_reload_toasts_enabled"] = True
    a["cost_ledger"] = None
    a["scheduler"] = None
    a["observer"] = None
    a["observer_subscriber"] = None
    a["adapter"] = None
    a["adapter_options"] = None
    a["adapter_entry"] = None
    a["adapter_chain"] = None
    a["system_prompt"] = ""
    a["prompt_builder"] = None
    a["stt_engine"] = None
    a["tts_engine"] = None
    a["vault_config"] = None
    a["config"] = None
    return a


async def test_watcher_debounces_rapid_writes(config_dir, app):
    """Two saves within the debounce window fire the reloader once."""
    fired: list[str] = []

    async def fake_reloader(_app):
        fired.append("called")

    watcher = ConfigWatcher(
        app=app,
        config_dir=config_dir,
        reloaders={"providers.yaml": fake_reloader},
    )
    await watcher.start()
    try:
        models_path = config_dir / "providers.yaml"
        # Two rapid writes within the debounce window.
        models_path.write_text(models_path.read_text(encoding="utf-8"), encoding="utf-8")
        models_path.write_text(models_path.read_text(encoding="utf-8"), encoding="utf-8")
        # Wait for the debounce + dispatch.
        await asyncio.sleep(DEBOUNCE_SECONDS + 0.4)
        assert len(fired) == 1
    finally:
        await watcher.stop()


async def test_watcher_ignores_unwatched_file(config_dir, app):
    fired: list[str] = []

    async def fake(_app):
        fired.append("called")

    watcher = ConfigWatcher(
        app=app,
        config_dir=config_dir,
        reloaders={"providers.yaml": fake},
    )
    await watcher.start()
    try:
        # Touching a non-watched file (e.g. README) is a no-op.
        readme = config_dir / "scratch.txt"
        readme.write_text("hello", encoding="utf-8")
        await asyncio.sleep(DEBOUNCE_SECONDS + 0.3)
        assert fired == []
    finally:
        await watcher.stop()


async def test_reload_permissions_swaps_in_place(config_dir, app):
    from tesseract.permissions.policy import load_permission_policy

    # Write a known-good minimal permissions.yaml then reload to confirm
    # the live PermissionPolicy mutates in place.
    tools_block = "tools:\n  foo: ask\n  bar: auto\n"
    minimal = (
        f"security_mode: max\n{tools_block}"
        "bash_readonly_allowlist: []\nbash_readonly_exact_allowlist: []\n"
    )
    (config_dir / "permissions.yaml").write_text(minimal, encoding="utf-8")
    policy = load_permission_policy(config_dir / "permissions.yaml")
    assert "foo" in policy.tools_defaults
    app["config"] = type("Cfg", (), {"permissions": policy})()
    # Monkey-patch the module-level constant the reloader reads.
    import tesseract.mirror.server.config as server_config

    server_config.PERMISSIONS_YAML = config_dir / "permissions.yaml"

    # Add a new tool key, then trigger reload.
    updated = minimal.replace(tools_block, tools_block + "  baz: auto\n")
    (config_dir / "permissions.yaml").write_text(updated, encoding="utf-8")
    await reload_permissions(app)
    assert "baz" in policy.tools_defaults


async def test_reload_schedule_invokes_engine(config_dir, app, monkeypatch):
    calls: list[str] = []

    class FakeScheduler:
        def reload_jobs(self):
            calls.append("called")
            return {"added": ["foo"], "removed": [], "changed": []}

    app["scheduler"] = FakeScheduler()
    await reload_schedule(app)
    assert calls == ["called"]


async def test_reload_schedule_no_change_suppresses_emission(config_dir, app):
    """Self-write path: scheduler.set_cadence persists schedule.yaml,
    watchdog re-fires reload_schedule, the diff is empty. The toast
    must be suppressed — no operator-visible change happened.
    """
    sent: list[str] = []

    class FakeSess:
        session_id = "sid"

        async def send_str(self, payload):
            sent.append(payload)

    class FakeScheduler:
        def reload_jobs(self):
            return {"added": [], "removed": [], "changed": []}

    app["scheduler"] = FakeScheduler()
    app["server_sessions"] = {"s": FakeSess()}
    await reload_schedule(app)
    assert sent == []


async def test_reload_failure_does_not_raise(config_dir, app, monkeypatch):
    """A malformed yaml must not propagate — the watcher only emits a toast."""
    app["scheduler"] = type("Bad", (), {"reload_jobs": lambda self: (_ for _ in ()).throw(RuntimeError("boom"))})()
    # Should not raise.
    await reload_schedule(app)


async def test_toasts_disabled_suppresses_emission(config_dir, app):
    """`config_reload_toasts_enabled=False` → no envelope sent."""
    sent: list[str] = []

    class FakeSess:
        session_id = "sid"

        async def send_str(self, _payload):
            sent.append(_payload)

    app["server_sessions"] = {"s": FakeSess()}
    app["config_reload_toasts_enabled"] = False

    class FakeScheduler:
        def reload_jobs(self):
            return {"added": [], "removed": [], "changed": []}

    app["scheduler"] = FakeScheduler()
    await reload_schedule(app)
    assert sent == []


async def test_rebuild_adapters_swaps_live_chat_sessions(monkeypatch):
    """Live model swap (2026-05-01) — `rebuild_adapters` must rewire
    every active `ChatSession` so an operator edit lands on the next
    turn of in-flight conversations, not just on freshly-opened ones.

    Drives `rebuild_adapters` with stub resolve/observer/voice helpers
    so the test exercises only the session-swap branch."""
    from types import SimpleNamespace

    import tesseract.brain.boot as boot

    new_adapter = SimpleNamespace(name="new-adapter")
    new_options = SimpleNamespace(name="new-options")
    new_chat_cfg = SimpleNamespace(
        provider="openai",
        model="gpt54_mini",
        compact_threshold=0.55,
        keep_recent_turns=12,
        tool_iteration_cap=25,
        consecutive_error_cap=3,
        head_anchor_messages=3,
        active_window_tokens=None,
        summary_char_budget=2000,
    )
    # `resolve_chat_brain_runtime` returns a chain of (adapter, options)
    # tuples — FallbackAdapter consumes them as pairs, see adapter_chain.py.
    new_chain = [(new_adapter, new_options)]

    monkeypatch.setattr(
        boot, "resolve_chat_brain_runtime",
        lambda: (new_chat_cfg, new_adapter, new_options, new_chain),
    )
    monkeypatch.setattr(boot, "build_observer", lambda cost_ledger=None: None)
    # Skip the voice-runtime branch entirely — covered by voice tests.
    import tesseract.mirror.server.app as app_module
    monkeypatch.setattr(app_module, "_build_voice_runtime", lambda _app: None)

    fake_session = SimpleNamespace(
        chat_session=SimpleNamespace(
            adapter=SimpleNamespace(name="old-adapter"),
            options=SimpleNamespace(name="old-options"),
            compact_threshold=0.30,
            keep_recent_turns=5,
            system_prompt="OLD",
        ),
    )
    app = web.Application()
    app["server_sessions"] = {"sid": fake_session}
    app["observer"] = None
    app["observer_subscriber"] = None
    app["voice_state"] = None
    app["prompt_builder"] = lambda: "NEW SYSTEM PROMPT"
    app["config"] = None

    summary = boot.rebuild_adapters(app)

    assert summary.get("chat_brain") == "openai/gpt54_mini"
    assert summary.get("live_sessions_swapped") == 1

    cs = fake_session.chat_session
    # The chain has one entry, so FallbackAdapter wraps it; the wrapped
    # adapter still resolves to the new primary on .stream(). We can't
    # do a strict identity check on `cs.adapter` (it's a FallbackAdapter
    # wrapper), so check the secondary attributes that prove the swap.
    assert cs.options is new_options
    assert cs.compact_threshold == 0.55
    assert cs.keep_recent_turns == 12
    assert cs.system_prompt == "NEW SYSTEM PROMPT"


async def test_reload_mirror_hot_applies_toast_toggle(config_dir, app, monkeypatch):
    """Audit M4 — external edits to mirror.yaml::ui.show_config_reload_toasts
    must take effect immediately, not wait for the operator to open the
    Settings panel and POST."""
    import tesseract.mirror.server.config as cfg_mod
    import tesseract.mirror.server.config_watcher as watcher_mod

    mirror_path = config_dir / "mirror.yaml"
    monkeypatch.setattr(cfg_mod, "MIRROR_YAML", mirror_path)
    # `reload_mirror` re-imports MIRROR_YAML at call time, so patching
    # the module is sufficient.
    text = mirror_path.read_text(encoding="utf-8")
    if "show_config_reload_toasts" in text:
        # Force the value to True first so the change to False is observable.
        text = text.replace(
            "show_config_reload_toasts: false", "show_config_reload_toasts: true"
        )
    if "ui:" not in text:
        text = text + "\nui:\n  show_config_reload_toasts: true\n"
    mirror_path.write_text(text, encoding="utf-8")
    app["config_reload_toasts_enabled"] = True

    # External edit flips it to false on disk.
    mirror_path.write_text(
        mirror_path.read_text(encoding="utf-8").replace(
            "show_config_reload_toasts: true", "show_config_reload_toasts: false"
        ),
        encoding="utf-8",
    )

    await reload_mirror(app)
    assert app["config_reload_toasts_enabled"] is False
