"""P2 Task 2 — MCP auto-provisioning (`mcp_provision.provision`).

Covers: correct `.mcp.json` / `~/.codex/config.toml` shapes; idempotent
re-run; missing token env raises; unknown client raises; foreign keys
survive; a stale `[mcp_servers.tesseract]` block is replaced in place;
`kind="terminal"` provisions both files. `HOME`/`USERPROFILE` are
redirected to `tmp_path` for every test so the real `~/.codex` is NEVER
touched."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from tesseract.config.mcp import MCPClient, MCPConfig, MCPServerBind
from tesseract.orchestrator.tars_controller.lanes import mcp_provision


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


def _server() -> MCPServerBind:
    return MCPServerBind(
        host="127.0.0.1",
        port=8000,
        token_secret_env="TESSERACT_MCP_SECRET",
        max_connections=20,
        ask_hold_timeout_s=30,
        idle_timeout_s=600,
    )


def _cfg(*clients: MCPClient) -> MCPConfig:
    return MCPConfig(
        server=_server(),
        clients=clients,
        verbs={},
        trust_tiers={"operator": "auto", "trusted": "auto", "restricted": "ask"},
    )


LANE_CLAUDE = MCPClient(name="lane-claude", token_env="TESSERACT_MCP_LANE_CLAUDE_TOKEN", trust_tier="trusted")
LANE_CODEX = MCPClient(name="lane-codex", token_env="TESSERACT_MCP_LANE_CODEX_TOKEN", trust_tier="trusted")
TERMINAL = MCPClient(name="terminal-manual", token_env="TESSERACT_MCP_TERMINAL_TOKEN", trust_tier="trusted")


# ---------------------------------------------------------------- claude


def test_claude_writes_mcp_json_with_correct_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TESSERACT_MCP_LANE_CLAUDE_TOKEN", "tok")
    working_dir = tmp_path / "lane"
    mcp_provision.provision(working_dir, "claude", _cfg(LANE_CLAUDE))

    data = json.loads((working_dir / ".mcp.json").read_text(encoding="utf-8"))
    entry = data["mcpServers"]["tesseract"]
    assert entry["type"] == "http"
    assert entry["url"] == "http://127.0.0.1:8000/mcp"
    assert entry["headers"]["Authorization"] == "Bearer ${TESSERACT_MCP_LANE_CLAUDE_TOKEN}"


def test_claude_preserves_foreign_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TESSERACT_MCP_LANE_CLAUDE_TOKEN", "tok")
    working_dir = tmp_path / "lane"
    working_dir.mkdir()
    (working_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"other": {"type": "stdio", "command": "x"}}, "topLevel": 1}),
        encoding="utf-8",
    )
    mcp_provision.provision(working_dir, "claude", _cfg(LANE_CLAUDE))

    data = json.loads((working_dir / ".mcp.json").read_text(encoding="utf-8"))
    assert data["topLevel"] == 1
    assert data["mcpServers"]["other"] == {"type": "stdio", "command": "x"}
    assert "tesseract" in data["mcpServers"]


def test_claude_idempotent_rerun_logically_equal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TESSERACT_MCP_LANE_CLAUDE_TOKEN", "tok")
    working_dir = tmp_path / "lane"
    mcp_provision.provision(working_dir, "claude", _cfg(LANE_CLAUDE))
    first = json.loads((working_dir / ".mcp.json").read_text(encoding="utf-8"))
    mcp_provision.provision(working_dir, "claude", _cfg(LANE_CLAUDE))
    second = json.loads((working_dir / ".mcp.json").read_text(encoding="utf-8"))
    assert first == second


def test_claude_missing_token_env_raises(tmp_path: Path) -> None:
    working_dir = tmp_path / "lane"
    with pytest.raises(RuntimeError, match="TESSERACT_MCP_LANE_CLAUDE_TOKEN"):
        mcp_provision.provision(working_dir, "claude", _cfg(LANE_CLAUDE))
    assert not (working_dir / ".mcp.json").exists()


def test_missing_client_entry_raises(tmp_path: Path) -> None:
    """The mcp.yaml the caller supplies has no `lane-claude` client at
    all — a config-authoring error, must raise, not silently no-op."""
    working_dir = tmp_path / "lane"
    with pytest.raises(RuntimeError, match="lane-claude"):
        mcp_provision.provision(working_dir, "claude", _cfg(LANE_CODEX))


# ----------------------------------------------------------------- codex


def test_codex_writes_config_toml_with_correct_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TESSERACT_MCP_LANE_CODEX_TOKEN", "tok")
    mcp_provision.provision(tmp_path / "lane", "codex", _cfg(LANE_CODEX))

    config_path = Path.home() / ".codex" / "config.toml"
    assert config_path.is_relative_to(tmp_path)
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    entry = parsed["mcp_servers"]["tesseract"]
    assert entry["url"] == "http://127.0.0.1:8000/mcp"
    assert entry["bearer_token_env_var"] == "TESSERACT_MCP_LANE_CODEX_TOKEN"


def test_codex_preserves_foreign_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TESSERACT_MCP_LANE_CODEX_TOKEN", "tok")
    codex_dir = Path.home() / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(
        '[mcp_servers.other]\nurl = "https://example.com/mcp"\n'
        'bearer_token_env_var = "OTHER_TOKEN"\n',
        encoding="utf-8",
    )
    mcp_provision.provision(tmp_path / "lane", "codex", _cfg(LANE_CODEX))

    text = (codex_dir / "config.toml").read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["other"]["url"] == "https://example.com/mcp"
    assert parsed["mcp_servers"]["tesseract"]["url"] == "http://127.0.0.1:8000/mcp"


def test_codex_idempotent_rerun_no_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TESSERACT_MCP_LANE_CODEX_TOKEN", "tok")
    mcp_provision.provision(tmp_path / "lane", "codex", _cfg(LANE_CODEX))
    config_path = Path.home() / ".codex" / "config.toml"
    before = config_path.read_text(encoding="utf-8")

    mcp_provision.provision(tmp_path / "lane", "codex", _cfg(LANE_CODEX))
    after = config_path.read_text(encoding="utf-8")
    assert before == after


def test_codex_replaces_stale_tesseract_block_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing `[mcp_servers.tesseract]` block with the wrong url
    (e.g. a stale port from an old mcp.yaml) is replaced by line-range —
    the following unrelated section must survive untouched."""
    monkeypatch.setenv("TESSERACT_MCP_LANE_CODEX_TOKEN", "tok")
    codex_dir = Path.home() / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(
        '[mcp_servers.tesseract]\n'
        'url = "http://127.0.0.1:9999/mcp"\n'
        'bearer_token_env_var = "STALE_TOKEN"\n'
        '[mcp_servers.after]\n'
        'url = "https://after.example.com/mcp"\n',
        encoding="utf-8",
    )
    mcp_provision.provision(tmp_path / "lane", "codex", _cfg(LANE_CODEX))

    parsed = tomllib.loads((codex_dir / "config.toml").read_text(encoding="utf-8"))
    assert parsed["mcp_servers"]["tesseract"]["url"] == "http://127.0.0.1:8000/mcp"
    assert parsed["mcp_servers"]["tesseract"]["bearer_token_env_var"] == "TESSERACT_MCP_LANE_CODEX_TOKEN"
    assert parsed["mcp_servers"]["after"]["url"] == "https://after.example.com/mcp"


def test_codex_missing_token_env_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="TESSERACT_MCP_LANE_CODEX_TOKEN"):
        mcp_provision.provision(tmp_path / "lane", "codex", _cfg(LANE_CODEX))
    assert not (Path.home() / ".codex" / "config.toml").exists()


def test_codex_second_client_overwrites_first_clients_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (independent review finding 1): the no-op check must not
    key on `url` alone — every client shares the same derived url, so a
    lane-codex provision followed by a terminal-manual provision (or vice
    versa) must NOT silently no-op and leave the first client's
    `bearer_token_env_var` stale."""
    monkeypatch.setenv("TESSERACT_MCP_LANE_CODEX_TOKEN", "tok-a")
    monkeypatch.setenv("TESSERACT_MCP_TERMINAL_TOKEN", "tok-b")
    mcp_provision.provision(tmp_path / "lane", "codex", _cfg(LANE_CODEX))
    mcp_provision.provision(tmp_path / "pane", "terminal", _cfg(TERMINAL))

    parsed = tomllib.loads(
        (Path.home() / ".codex" / "config.toml").read_text(encoding="utf-8")
    )
    assert parsed["mcp_servers"]["tesseract"]["bearer_token_env_var"] == (
        "TESSERACT_MCP_TERMINAL_TOKEN"
    )


def test_codex_commented_out_header_does_not_crash_and_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (independent review finding 2): a `[mcp_servers.
    tesseract]` substring that isn't its own exact line (e.g. commented
    out) must not crash the line-lookup — it falls through to the append
    branch instead of raising StopIteration."""
    monkeypatch.setenv("TESSERACT_MCP_LANE_CODEX_TOKEN", "tok")
    codex_dir = Path.home() / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(
        "# [mcp_servers.tesseract]  -- disabled during troubleshooting\n",
        encoding="utf-8",
    )
    mcp_provision.provision(tmp_path / "lane", "codex", _cfg(LANE_CODEX))

    text = (codex_dir / "config.toml").read_text(encoding="utf-8")
    assert "# [mcp_servers.tesseract]" in text
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["tesseract"]["url"] == "http://127.0.0.1:8000/mcp"


# --------------------------------------------------------------- terminal


def test_terminal_kind_provisions_both_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TESSERACT_MCP_TERMINAL_TOKEN", "tok")
    working_dir = tmp_path / "pane"
    mcp_provision.provision(working_dir, "terminal", _cfg(TERMINAL))

    mcp_json = json.loads((working_dir / ".mcp.json").read_text(encoding="utf-8"))
    assert mcp_json["mcpServers"]["tesseract"]["headers"]["Authorization"] == (
        "Bearer ${TESSERACT_MCP_TERMINAL_TOKEN}"
    )
    config_toml = tomllib.loads(
        (Path.home() / ".codex" / "config.toml").read_text(encoding="utf-8")
    )
    assert config_toml["mcp_servers"]["tesseract"]["bearer_token_env_var"] == (
        "TESSERACT_MCP_TERMINAL_TOKEN"
    )


def test_terminal_missing_token_env_raises_before_any_write(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "pane"
    with pytest.raises(RuntimeError, match="TESSERACT_MCP_TERMINAL_TOKEN"):
        mcp_provision.provision(working_dir, "terminal", _cfg(TERMINAL))
    assert not (working_dir / ".mcp.json").exists()
    assert not (Path.home() / ".codex" / "config.toml").exists()


# ------------------------------------------------------------------ misc


def test_unknown_kind_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="unknown kind"):
        mcp_provision.provision(tmp_path, "bash", _cfg(LANE_CLAUDE))  # type: ignore[arg-type]


def test_zero_writes_to_tesseract_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TESSERACT_MCP_LANE_CLAUDE_TOKEN", "tok")
    monkeypatch.setenv("TESSERACT_MCP_LANE_CODEX_TOKEN", "tok")
    mcp_provision.provision(tmp_path / "lane", "claude", _cfg(LANE_CLAUDE))
    mcp_provision.provision(tmp_path / "lane", "codex", _cfg(LANE_CODEX))
    logs_dir = tmp_path / "logs"
    assert not logs_dir.exists()
