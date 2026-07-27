"""Phase 14 Settings panel — regression tests for the new routes.

Covers:
  - Extended GET /api/identity carries `models` (4 roles), `roles`,
    `compact_thresholds`, and `cost_tracking`.
  - POST /api/settings/compact-threshold accepts chat_brain in-range,
    writes to the yaml AND updates live ChatSession.compact_threshold,
    and rejects non-chat_brain + out-of-bounds ratios.
  - POST /api/settings/cost validates bounds, persists to yaml, and
    calls CostLedger.reload() (verified via live ledger state).

All tests operate on a tmp copy of models.yaml so the real config is
never touched. Ruamel round-trip is exercised so comment/format survival
is implicitly verified (parse after write must succeed).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.brain.cost import CostLedger
from tesseract.mirror.server.config import ServerConfig, TerminalServerConfig, UploadConfig
from tesseract.mirror.server.routes import settings as settings_route
from tesseract.mirror.server.routes import system as system_route
from tesseract.permissions.policy import PermissionPolicy


# ── fixture helpers ──────────────────────────────────────────────────────


def _providers_payload() -> dict:
    return {
        "availability": {"max_consecutive_failures": 3},
        "cost_tracking": {
            "enabled": True,
            "warning_at_pct": 0.75,
            "log_file": "logs/cost-tracking.jsonl",
        },
        "api": {
            "openai": {
                "api_key_env": "OPENAI_API_KEY",
                "timeout_seconds": 60,
                "max_retries": 3,
                "adapter": "openai",
                "models": {
                    "gpt54_nano": {
                        "model": "gpt-5.4-nano",
                        "context_window": 400000,
                        "max_output_tokens": 8192,
                        "reasoning_effort": "high",
                        "temperature": 1.0,
                        "use_responses_api": True,
                        "cost_per_mtok_in": 0.20,
                        "cost_per_mtok_out": 1.25,
                    },
                },
            },
            "google": {
                "api_key_env": "GOOGLE_API_KEY",
                "timeout_seconds": 60,
                "max_retries": 3,
                "adapter": "gemini",
                "models": {
                    "gemini_25_flash": {
                        "model": "gemini-2.5-flash",
                        "context_window": 1000000,
                        "max_output_tokens": 8192,
                        "temperature": 0.7,
                        "use_responses_api": False,
                        "cost_per_mtok_in": 0.30,
                        "cost_per_mtok_out": 2.50,
                    },
                },
            },
        },
        "cli": {
            "claude": {
                "command": "claude",
                "timeout_seconds": 300,
                "max_retries": 1,
                "adapter": "cli",
                "models": {
                    "opus_47": {
                        "model": "claude-opus-4-7",
                        "context_window": 1000000,
                        "max_output_ratio": 0.35,
                        "temperature": 0.7,
                        "cost_per_mtok_in": 0,
                        "cost_per_mtok_out": 0,
                    },
                },
            },
            "codex": {
                "command": "codex",
                "timeout_seconds": 300,
                "max_retries": 1,
                "adapter": "cli",
                "models": {
                    "gpt54": {
                        "model": "gpt-5.4",
                        "context_window": 200000,
                        "max_output_ratio": 0.5,
                        "temperature": 0.7,
                        "cost_per_mtok_in": 0,
                        "cost_per_mtok_out": 0,
                    },
                },
            },
        },
        "local": {
            "ollama": {
                "base_url": "http://localhost:11434",
                "timeout_seconds": 120,
                "max_retries": 3,
                "adapter": "ollama",
                "host": "this_pc",
                "models": {
                    "nomic_embed": {
                        "kind": "embedding",
                        "model": "nomic-embed-text",
                        "dimensions": 768,
                        "timeout_seconds": 30,
                        "max_retries": 3,
                    },
                },
            },
        },
    }


def _roles_payload() -> dict:
    return {
        "embeddings": {"primary": "local.ollama.nomic_embed"},
        "roles": {
            "chat_brain": {
                "mode": "active",
                "primary": "api.openai.gpt54_nano",
                "fallbacks": ["api.google.gemini_25_flash"],
                "compact_threshold": 0.40,
                "keep_recent_turns": 10,
                "daily_budget_usd": 3.00,
            },
            "claude_cli": {"mode": "active", "primary": "cli.claude.opus_47"},
            "codex_cli": {"mode": "active", "primary": "cli.codex.gpt54"},
            "observer_agent": {
                "mode": "active",
                "primary": "api.openai.gpt54_nano",
                "daily_budget_usd": 1.00,
            },
        },
    }


def _minimal_models_payload() -> dict:
    """Legacy single-file shape — kept ONLY for the cost-ledger
    `from_models_yaml` test fixture which still consumes it inline.
    """
    return {
        "providers": {"openai": {"timeout_seconds": 60, "max_retries": 3}},
        "roles": {
            "chat_brain": {
                "mode": "active",
                "resolution": [
                    {
                        "tier": "api",
                        "provider": "openai",
                        "model": "gpt-5.4-nano",
                        "context_window": 400000,
                        "compact_threshold": 0.40,
                        "keep_recent_turns": 10,
                        "cost_per_mtok_in": 0.20,
                        "cost_per_mtok_out": 1.25,
                    },
                ],
            },
            "observer_agent": {
                "mode": "active",
                "resolution": [
                    {
                        "tier": "api",
                        "provider": "openai",
                        "model": "gpt-5.4-nano",
                        "context_window": 400000,
                        "cost_per_mtok_in": 0.20,
                        "cost_per_mtok_out": 1.25,
                    },
                ],
            },
        },
        "cost_tracking": {
            "enabled": True,
            "warning_at_pct": 0.75,
            "log_file": "logs/cost-tracking.jsonl",
            "per_role": {"chat_brain": 3.00, "observer_agent": 1.00},
        },
    }


def _write_split_config(tmp_path: Path) -> tuple[Path, Path]:
    """Write providers.yaml + roles.yaml to tmp/config/. Returns the pair."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    providers_path = config_dir / "providers.yaml"
    roles_path = config_dir / "roles.yaml"
    providers_path.write_text(yaml.safe_dump(_providers_payload()), encoding="utf-8")
    roles_path.write_text(yaml.safe_dump(_roles_payload()), encoding="utf-8")
    return providers_path, roles_path


def _write_models(tmp_path: Path) -> Path:
    """Compat — emits a single legacy-shape fixture for cost-ledger tests."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    target = config_dir / "models.yaml"
    target.write_text(yaml.safe_dump(_minimal_models_payload()), encoding="utf-8")
    return target


def _build_config(models_payload: dict) -> ServerConfig:
    terminal = TerminalServerConfig(
        default_shell="bash",
        max_tabs=3,
        max_panes_per_tab=3,
        shell_profiles={},
        coalesce_flush_ms=8.0,
        coalesce_flush_chars=4096,
        reattach_grace_s=30.0,
        pause_buffer_cap_chars=2_000_000,
    )
    policy = PermissionPolicy(
        tools_defaults={"tool_a": "ask", "tool_b": "auto", "tool_c": "deny"},
        modes={},
        path_overrides={},
        current_mode="max",
    )
    return ServerConfig(
        host="127.0.0.1",
        port=0,
        entity_name="TARS",
        operator_name="Op",
        cors_origins=(),
        models=models_payload,
        permissions=policy,
        terminal=terminal,
        uploads=UploadConfig(
            max_file_mb=50,
            max_total_mb=50,
            max_files_per_message=5,
            allowed_mime_types=("image/png", "application/pdf"),
        ),
    )


def _write_permissions(tmp_path: Path) -> Path:
    payload = {
        "security_mode": "max",
        "tools": {"memory_save": "auto", "file_write": "ask", "bash": "ask"},
        "modes": {"max": {"overrides": {}}, "standard": {"overrides": {}}, "headless": {"overrides": {}}},
        "path_overrides": {},
    }
    target = tmp_path / "config" / "permissions.yaml"
    target.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return target


class _FakeTool:
    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description or f"{name} (test)"


class _FakeRegistry:
    def __init__(self, names: list[str]) -> None:
        self.tools = {n: _FakeTool(n) for n in names}


async def _make_client(tmp_path: Path, *, with_ledger: bool = True) -> TestClient:
    providers_path, roles_path = _write_split_config(tmp_path)
    _write_permissions(tmp_path)
    from tesseract.config.loader import load_config
    from tesseract.mirror.server.config import synthesize_legacy_models_dict

    bundle = load_config(providers_path=providers_path, roles_path=roles_path)
    config = _build_config(synthesize_legacy_models_dict(bundle))

    app = web.Application()
    app["config"] = config
    app["tesseract_dir"] = tmp_path
    app["sessions"] = {}
    app["server_sessions"] = {}
    app["adapter_options"] = None  # short-circuits _emit_stats fan-out
    app["tool_registry"] = _FakeRegistry(["memory_save", "file_write", "bash"])

    if with_ledger:
        log_path = tmp_path / "cost.jsonl"
        ledger = CostLedger.from_bundle(bundle, log_path=log_path)
        app["cost_ledger"] = ledger
    else:
        app["cost_ledger"] = None

    app.router.add_get("/api/identity", system_route.identity)
    app.router.add_get("/api/tools", system_route.tools)
    app.router.add_post("/api/mode", system_route.set_mode)
    app.router.add_post(
        "/api/settings/compact-threshold", settings_route.set_compact_threshold
    )
    app.router.add_post("/api/settings/cost", settings_route.set_cost)
    app.router.add_get("/api/settings/config-files", settings_route.get_config_files)
    app.router.add_post(
        "/api/settings/tool-permission", settings_route.set_tool_permission
    )

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


# ── GET /api/identity (extended) ────────────────────────────────────────


async def test_identity_carries_all_four_roles(tmp_path: Path) -> None:
    client = await _make_client(tmp_path)
    try:
        resp = await client.get("/api/identity")
        assert resp.status == 200
        body = await resp.json()

        assert set(body["models"].keys()) == {
            "chat_brain",
            "claude_cli",
            "codex_cli",
            "observer_agent",
        }
        assert body["models"]["chat_brain"]["name"] == "gpt-5.4-nano"
        assert body["models"]["chat_brain"]["provider"] == "openai"
        assert body["models"]["chat_brain"]["context_window"] == 400000

        assert body["roles"]["chat_brain"]["mode"] == "active"
        assert body["roles"]["codex_cli"]["mode"] == "active"

        assert body["compact_thresholds"]["chat_brain"]["ratio"] == pytest.approx(0.40)
        assert body["compact_thresholds"]["chat_brain"]["tokens"] == 160000
        assert body["compact_thresholds"]["chat_brain"]["keep_recent_turns"] == 10

        ct = body["cost_tracking"]
        assert ct["enabled"] is True
        # daily_budget_usd is *derived*: sum(per_role) = 3.0 + 1.0 = 4.0
        assert ct["daily_budget_usd"] == pytest.approx(4.0)
        # warning_at_pct replaces warning_at_usd
        assert ct["warning_at_pct"] == pytest.approx(0.75)
        assert ct["per_role"]["chat_brain"] == pytest.approx(3.0)
        assert ct["per_role"]["observer_agent"] == pytest.approx(1.0)
    finally:
        await client.close()


# ── POST /api/settings/compact-threshold ────────────────────────────────


async def test_compact_threshold_valid_updates_yaml_and_live_session(
    tmp_path: Path,
) -> None:
    client = await _make_client(tmp_path)
    app = client.app

    live_cs = SimpleNamespace(compact_threshold=0.40, keep_recent_turns=10)
    app["server_sessions"]["s1"] = SimpleNamespace(session_id="s1", chat_session=live_cs)

    try:
        resp = await client.post(
            "/api/settings/compact-threshold",
            json={"role": "chat_brain", "ratio": 0.42},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["role"] == "chat_brain"
        assert body["ratio"] == pytest.approx(0.42)
        assert body["context_window"] == 400000
        assert body["tokens"] == 168000
        assert body["keep_recent_turns"] == 10  # unchanged

        # Live session updated.
        assert live_cs.compact_threshold == pytest.approx(0.42)
        assert live_cs.keep_recent_turns == 10

        # In-memory config: role-level compact_threshold mutated.
        chat_brain = app["config"].models["roles"]["chat_brain"]
        assert chat_brain["compact_threshold"] == pytest.approx(0.42)
        assert chat_brain["keep_recent_turns"] == 10  # unchanged

        # roles.yaml on disk: role-level knob mutated.
        raw = yaml.safe_load(
            (tmp_path / "config" / "roles.yaml").read_text(encoding="utf-8")
        )
        disk = raw["roles"]["chat_brain"]
        assert disk["compact_threshold"] == pytest.approx(0.42)
        assert disk["keep_recent_turns"] == 10
        # Primary ref unchanged.
        assert disk["primary"] == "api.openai.gpt54_nano"
    finally:
        await client.close()


async def test_compact_threshold_keep_recent_turns_updates_yaml_and_session(
    tmp_path: Path,
) -> None:
    client = await _make_client(tmp_path)
    app = client.app
    live_cs = SimpleNamespace(compact_threshold=0.40, keep_recent_turns=10)
    app["server_sessions"]["s1"] = SimpleNamespace(session_id="s1", chat_session=live_cs)
    try:
        resp = await client.post(
            "/api/settings/compact-threshold",
            json={"role": "chat_brain", "keep_recent_turns": 14},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["keep_recent_turns"] == 14
        assert body["ratio"] == pytest.approx(0.40)
        assert live_cs.keep_recent_turns == 14
        assert live_cs.compact_threshold == pytest.approx(0.40)
        raw = yaml.safe_load(
            (tmp_path / "config" / "roles.yaml").read_text(encoding="utf-8")
        )
        disk = raw["roles"]["chat_brain"]
        assert disk["keep_recent_turns"] == 14
        assert disk["compact_threshold"] == pytest.approx(0.40)
    finally:
        await client.close()


async def test_compact_threshold_combined_update(tmp_path: Path) -> None:
    client = await _make_client(tmp_path)
    app = client.app
    live_cs = SimpleNamespace(compact_threshold=0.40, keep_recent_turns=10)
    app["server_sessions"]["s1"] = SimpleNamespace(session_id="s1", chat_session=live_cs)
    try:
        resp = await client.post(
            "/api/settings/compact-threshold",
            json={"role": "chat_brain", "ratio": 0.5, "keep_recent_turns": 20},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["ratio"] == pytest.approx(0.5)
        assert body["keep_recent_turns"] == 20
        assert live_cs.compact_threshold == pytest.approx(0.5)
        assert live_cs.keep_recent_turns == 20
    finally:
        await client.close()


async def test_compact_threshold_requires_at_least_one_field(tmp_path: Path) -> None:
    client = await _make_client(tmp_path)
    try:
        resp = await client.post(
            "/api/settings/compact-threshold",
            json={"role": "chat_brain"},
        )
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.parametrize("keep", [0, 1, -5, 500, "string"])
async def test_compact_threshold_keep_recent_turns_rejects_bad_values(
    tmp_path: Path, keep
) -> None:
    client = await _make_client(tmp_path)
    try:
        resp = await client.post(
            "/api/settings/compact-threshold",
            json={"role": "chat_brain", "keep_recent_turns": keep},
        )
        assert resp.status == 400
    finally:
        await client.close()


async def test_compact_threshold_observer_rejected(tmp_path: Path) -> None:
    client = await _make_client(tmp_path)
    try:
        resp = await client.post(
            "/api/settings/compact-threshold",
            json={"role": "observer_agent", "ratio": 0.42},
        )
        assert resp.status == 400
        body = await resp.json()
        assert "not supported" in body["error"].lower()
    finally:
        await client.close()


@pytest.mark.parametrize("ratio", [0.05, 0.99, 1.5, -0.1])
async def test_compact_threshold_out_of_bounds_rejected(
    tmp_path: Path, ratio: float
) -> None:
    client = await _make_client(tmp_path)
    try:
        resp = await client.post(
            "/api/settings/compact-threshold",
            json={"role": "chat_brain", "ratio": ratio},
        )
        assert resp.status == 400
        body = await resp.json()
        assert "between" in body["error"].lower()
    finally:
        await client.close()


async def test_compact_threshold_invalid_ratio_type(tmp_path: Path) -> None:
    client = await _make_client(tmp_path)
    try:
        resp = await client.post(
            "/api/settings/compact-threshold",
            json={"role": "chat_brain", "ratio": "banana"},
        )
        assert resp.status == 400
    finally:
        await client.close()


# ── POST /api/settings/cost ─────────────────────────────────────────────


async def test_cost_update_valid_persists_and_reloads_ledger(tmp_path: Path) -> None:
    """POST body is {warning_at_pct, per_role}. Response is IdentityCostTracking.
    daily_budget_usd in the response is the *derived* sum of per_role caps."""
    client = await _make_client(tmp_path)
    app = client.app
    try:
        resp = await client.post(
            "/api/settings/cost",
            json={
                "warning_at_pct": 0.4,
                "per_role": {"chat_brain": 4.0, "observer_agent": 1.0},
            },
        )
        assert resp.status == 200
        body = await resp.json()
        # Derived global: 4.0 + 1.0 = 5.0 (no voice in fixture)
        assert body["daily_budget_usd"] == pytest.approx(5.0)
        assert body["warning_at_pct"] == pytest.approx(0.4)
        assert body["per_role"]["chat_brain"] == pytest.approx(4.0)
        assert body["per_role"]["observer_agent"] == pytest.approx(1.0)

        # In-memory config mutated.
        ct = app["config"].models["cost_tracking"]
        assert ct["warning_at_pct"] == pytest.approx(0.4)
        assert ct["per_role"]["chat_brain"] == pytest.approx(4.0)

        # providers.yaml carries the global knob.
        providers_raw = yaml.safe_load(
            (tmp_path / "config" / "providers.yaml").read_text(encoding="utf-8")
        )
        assert providers_raw["cost_tracking"]["warning_at_pct"] == pytest.approx(0.4)
        # roles.yaml carries per-role caps.
        roles_raw = yaml.safe_load(
            (tmp_path / "config" / "roles.yaml").read_text(encoding="utf-8")
        )
        assert roles_raw["roles"]["chat_brain"]["daily_budget_usd"] == pytest.approx(4.0)

        # Ledger reloaded: derived cap = 5.0, warning_usd = 5.0 * 0.4 = 2.0.
        state = app["cost_ledger"].budget_state("chat_brain")
        assert state.cap_usd == pytest.approx(5.0)
        assert state.warning_usd == pytest.approx(2.0)
        assert state.role_cap_usd == pytest.approx(4.0)
    finally:
        await client.close()


async def test_cost_update_with_null_ledger_succeeds(tmp_path: Path) -> None:
    """Cost route must tolerate `app['cost_ledger'] is None` — the ledger is
    built fail-open at startup, and a missing `cost_tracking` block leaves
    it None. The route must still update yaml + in-memory config and respond
    200 without trying to call `.reload()` on a None.
    """
    client = await _make_client(tmp_path, with_ledger=False)
    app = client.app
    try:
        assert app.get("cost_ledger") is None
        resp = await client.post(
            "/api/settings/cost",
            json={"warning_at_pct": 0.5, "per_role": {"chat_brain": 7.5}},
        )
        assert resp.status == 200
        body = await resp.json()
        # Derived: 7.5 + initial observer_agent(1.0) = 8.5
        assert body["daily_budget_usd"] == pytest.approx(8.5)
        assert body["warning_at_pct"] == pytest.approx(0.5)
        ct = app["config"].models["cost_tracking"]
        assert ct["warning_at_pct"] == pytest.approx(0.5)
    finally:
        await client.close()


async def test_cost_update_warning_pct_out_of_range_rejected(tmp_path: Path) -> None:
    """warning_at_pct must be in [0, 1]; values outside that range are rejected."""
    client = await _make_client(tmp_path)
    try:
        resp = await client.post(
            "/api/settings/cost",
            json={"warning_at_pct": 1.5},
        )
        assert resp.status == 400
        body = await resp.json()
        assert "warning" in body["error"].lower()

        resp2 = await client.post(
            "/api/settings/cost",
            json={"warning_at_pct": -0.1},
        )
        assert resp2.status == 400
    finally:
        await client.close()


async def test_cost_update_negative_cap_rejected(tmp_path: Path) -> None:
    client = await _make_client(tmp_path)
    try:
        resp = await client.post(
            "/api/settings/cost",
            json={"per_role": {"chat_brain": -1.0}},
        )
        assert resp.status == 400
    finally:
        await client.close()


async def test_cost_update_unknown_role_rejected(tmp_path: Path) -> None:
    client = await _make_client(tmp_path)
    try:
        resp = await client.post(
            "/api/settings/cost",
            json={"per_role": {"nonexistent_role": 1.0}},
        )
        assert resp.status == 400
        body = await resp.json()
        assert "unknown role" in body["error"].lower()
    finally:
        await client.close()


async def test_config_files_returns_safe_list(tmp_path: Path) -> None:
    """Only safe-listed yaml files are readable; missing entries marked, not 404'd."""
    client = await _make_client(tmp_path)
    try:
        resp = await client.get("/api/settings/config-files")
        assert resp.status == 200
        body = await resp.json()
        names = [f["name"] for f in body["files"]]
        assert "providers.yaml" in names
        assert "roles.yaml" in names
        assert "permissions.yaml" in names
        # providers.yaml was written in the fixture — readable
        providers_row = next(f for f in body["files"] if f["name"] == "providers.yaml")
        assert providers_row["missing"] is False
        assert providers_row["content"] is not None
        assert providers_row["bytes"] > 0
        # roles.yaml carries chat_brain
        roles_row = next(f for f in body["files"] if f["name"] == "roles.yaml")
        assert roles_row["missing"] is False
        assert "chat_brain" in roles_row["content"]
        # permissions.yaml was written by _write_permissions — readable
        perms_row = next(f for f in body["files"] if f["name"] == "permissions.yaml")
        assert perms_row["missing"] is False
        # A safe-listed file not written by the fixture stays missing
        vault_row = next(f for f in body["files"] if f["name"] == "vault.yaml")
        assert vault_row["missing"] is True
        assert vault_row["content"] is None
    finally:
        await client.close()


async def test_tool_permission_valid_updates_yaml_and_live_policy(tmp_path: Path) -> None:
    client = await _make_client(tmp_path)
    app = client.app
    try:
        resp = await client.post(
            "/api/settings/tool-permission",
            json={"name": "file_write", "posture": "auto"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body == {"name": "file_write", "posture": "auto"}
        # Live policy dict mutated in place
        assert app["config"].permissions.tools_defaults["file_write"] == "auto"
        # Yaml persisted
        raw = yaml.safe_load(
            (tmp_path / "config" / "permissions.yaml").read_text(encoding="utf-8")
        )
        assert raw["tools"]["file_write"] == "auto"
    finally:
        await client.close()


@pytest.mark.parametrize("posture", ["AUTO", "Ask", "DENY"])
async def test_tool_permission_case_insensitive(tmp_path: Path, posture: str) -> None:
    client = await _make_client(tmp_path)
    try:
        resp = await client.post(
            "/api/settings/tool-permission",
            json={"name": "bash", "posture": posture},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["posture"] == posture.lower()
    finally:
        await client.close()


@pytest.mark.parametrize("posture", ["allow", "maybe", "", 1, None])
async def test_tool_permission_rejects_bad_posture(tmp_path: Path, posture) -> None:
    client = await _make_client(tmp_path)
    try:
        resp = await client.post(
            "/api/settings/tool-permission",
            json={"name": "bash", "posture": posture},
        )
        assert resp.status == 400
    finally:
        await client.close()


async def test_tool_permission_rejects_unknown_tool(tmp_path: Path) -> None:
    client = await _make_client(tmp_path)
    try:
        resp = await client.post(
            "/api/settings/tool-permission",
            json={"name": "nope", "posture": "auto"},
        )
        assert resp.status == 400
        body = await resp.json()
        assert "unknown tool" in body["error"].lower()
    finally:
        await client.close()


async def test_tool_permission_rejects_missing_name(tmp_path: Path) -> None:
    client = await _make_client(tmp_path)
    try:
        resp = await client.post(
            "/api/settings/tool-permission",
            json={"posture": "auto"},
        )
        assert resp.status == 400
    finally:
        await client.close()


# ── GET /api/tools — effective posture truth (M1, 2026-04-25) ───────────


async def test_tools_returns_mode_aware_default_posture(tmp_path: Path) -> None:
    """`/api/tools` must use the live policy resolution path, not raw defaults.

    Switching mode (max -> headless) and re-fetching must reflect the headless
    overrides for any tool the mode block touches.
    """
    client = await _make_client(tmp_path)
    try:
        import dataclasses

        from tesseract.permissions.policy import load_permission_policy

        # `_make_client` writes a minimal permissions.yaml; overwrite with a
        # richer one carrying mode + path overrides, then reload the live
        # policy from disk so /api/tools and /api/mode see the same source.
        perm_yaml = tmp_path / "config" / "permissions.yaml"
        with perm_yaml.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(
                {
                    "security_mode": "max",
                    "tools": {
                        "memory_save": "auto",
                        "file_write": "ask",
                        "bash": "ask",
                    },
                    "modes": {
                        "max": {"overrides": {}},
                        "standard": {"overrides": {}},
                        "headless": {
                            "overrides": {"bash": "auto", "file_write": "auto"},
                        },
                    },
                    "path_overrides": {
                        "file_write": [
                            {"path_prefix": "tesseract/kernel/", "posture": "deny"},
                        ],
                    },
                    "bash_readonly_allowlist": [],
                    "bash_readonly_exact_allowlist": [],
                },
                fh,
            )
        client.app["config"] = dataclasses.replace(
            client.app["config"], permissions=load_permission_policy(perm_yaml)
        )

        resp = await client.get("/api/tools")
        assert resp.status == 200
        body = await resp.json()
        assert body["mode"] == "max"
        rows = {t["name"]: t for t in body["tools"]}
        # In max, no overrides → defaults
        assert rows["bash"]["permission"] == "ask"
        assert rows["bash"]["mode_override"] is False
        assert rows["bash"]["path_sensitive"] is False
        # file_write has path overrides → flagged path_sensitive
        assert rows["file_write"]["permission"] == "ask"
        assert rows["file_write"]["path_sensitive"] is True
        assert rows["file_write"]["mode_override"] is False
        assert rows["file_write"]["default_posture"] == "ask"

        # Switch to headless via the live route, then re-query.
        resp = await client.post("/api/mode", json={"mode": "headless"})
        assert resp.status == 200

        resp = await client.get("/api/tools")
        assert resp.status == 200
        body = await resp.json()
        assert body["mode"] == "headless"
        rows = {t["name"]: t for t in body["tools"]}
        # bash now auto-permitted via mode override
        assert rows["bash"]["permission"] == "auto"
        assert rows["bash"]["mode_override"] is True
        assert rows["bash"]["default_posture"] == "ask"  # raw default unchanged
        # file_write also auto via mode override (and still path_sensitive)
        assert rows["file_write"]["permission"] == "auto"
        assert rows["file_write"]["mode_override"] is True
        assert rows["file_write"]["path_sensitive"] is True
    finally:
        await client.close()


def test_policy_resolve_posture_honors_path_then_mode_then_default() -> None:
    """Direct policy unit test — confirms both server route + ws emit see same truth."""
    policy = PermissionPolicy(
        tools_defaults={"bash": "ask", "file_write": "ask", "memory_save": "auto"},
        modes={
            "max": {"overrides": {}},
            "headless": {"overrides": {"bash": "auto", "file_write": "auto"}},
        },
        path_overrides={
            "file_write": [
                {"path_prefix": "tesseract/kernel/", "posture": "deny"},
                # Workspace redesign (2026-05-06): SOUL.md (and the other
                # 11 workspace .md files) are deny for direct file_write.
                # TARS routes via `propose_change`; the workspace inbox is
                # the operator gate.
                {"path_prefix": "tesseract/workspace/SOUL.md", "posture": "deny"},
            ],
        },
        current_mode="max",
    )
    # max + no path → defaults
    assert policy.resolve_posture("bash", {}) == "ask"
    assert policy.resolve_posture("memory_save", {}) == "auto"
    # path override beats default
    assert policy.resolve_posture("file_write", {"file_path": "tesseract/kernel/x.py"}) == "deny"
    assert policy.resolve_posture("file_write", {"file_path": "tesseract/workspace/SOUL.md"}) == "deny"
    # Switch to headless — mode override applies
    policy.set_mode("headless")
    assert policy.resolve_posture("bash", {}) == "auto"
    # Path override STILL beats mode override (lockdown is absolute)
    assert (
        policy.resolve_posture("file_write", {"file_path": "tesseract/kernel/x.py"})
        == "deny"
    )
    # Headless mode override applies for paths NOT in overrides
    assert (
        policy.resolve_posture("file_write", {"file_path": "tesseract/notes.md"})
        == "auto"
    )
    assert policy.default_posture("bash") == "auto"
    assert policy.has_path_overrides("file_write") is True
    assert policy.has_path_overrides("bash") is False
    assert policy.has_mode_override("bash") is True
    assert policy.has_mode_override("memory_save") is False


async def test_cost_update_partial_body_preserves_other_fields(tmp_path: Path) -> None:
    """Omitting a field in the request should leave that field unchanged.
    Only warning_at_pct and per_role are editable; sending only warning_at_pct
    preserves per_role from yaml."""
    client = await _make_client(tmp_path)
    app = client.app
    try:
        resp = await client.post(
            "/api/settings/cost", json={"warning_at_pct": 0.5}
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["warning_at_pct"] == pytest.approx(0.5)
        # per_role unchanged from fixture (chat_brain=3.0, observer_agent=1.0)
        # derived daily = 3.0 + 1.0 = 4.0
        assert body["daily_budget_usd"] == pytest.approx(4.0)
        assert body["per_role"]["chat_brain"] == pytest.approx(3.0)
        # Disk matches — providers.yaml carries warning_at_pct,
        # roles.yaml carries the per-role caps.
        providers_raw = yaml.safe_load(
            (tmp_path / "config" / "providers.yaml").read_text(encoding="utf-8")
        )
        assert providers_raw["cost_tracking"]["warning_at_pct"] == pytest.approx(0.5)
        roles_raw = yaml.safe_load(
            (tmp_path / "config" / "roles.yaml").read_text(encoding="utf-8")
        )
        assert roles_raw["roles"]["chat_brain"]["daily_budget_usd"] == pytest.approx(3.0)
        # Ledger reflects: warning_usd = 4.0 * 0.5 = 2.0
        state = app["cost_ledger"].budget_state("chat_brain")
        assert state.warning_usd == pytest.approx(2.0)
    finally:
        await client.close()
