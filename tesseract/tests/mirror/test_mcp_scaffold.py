"""mcp-control-plane — MCP config loader tests.

Covers the mcp.yaml config loader: schema validation, invariants,
host restriction, unknown-verb rejection, and posture validation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.config.mcp import load_mcp_config

_VALID_YAML = """\
server:
  host: "127.0.0.1"
  port: 8000
  token_secret_env: "TESSERACT_MCP_SECRET"
  max_connections: 20
  ask_hold_timeout_s: 30
  idle_timeout_s: 600
clients:
  - name: "operator"
    token_env: "TESSERACT_MCP_SECRET"
    trust_tier: "operator"
trust_tiers:
  operator: "auto"
  trusted: "auto"
  restricted: "ask"
verbs:
  activity.list: "auto"
  activity.watch: "auto"
  memory.search: "auto"
  vault.search: "auto"
  vault.query: "auto"
"""


def _write_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "mcp.yaml"
    p.write_text(body, encoding="utf-8")
    return p


# ── config loader ────────────────────────────────────────────────────────

def test_loader_accepts_valid_config(tmp_path: Path) -> None:
    cfg = load_mcp_config(_write_yaml(tmp_path, _VALID_YAML))
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 8000
    assert cfg.server.max_connections == 20
    assert len(cfg.clients) == 1
    assert cfg.clients[0].trust_tier == "operator"
    assert cfg.verbs["activity.list"] == "auto"


def test_loader_raises_on_missing_server_key(tmp_path: Path) -> None:
    body = _VALID_YAML.replace('  port: 8000\n', "")
    with pytest.raises(RuntimeError, match="server"):
        load_mcp_config(_write_yaml(tmp_path, body))


def test_loader_rejects_non_local_host(tmp_path: Path) -> None:
    body = _VALID_YAML.replace('host: "127.0.0.1"', 'host: "0.0.0.0"')
    with pytest.raises(RuntimeError, match="127.0.0.1"):
        load_mcp_config(_write_yaml(tmp_path, body))


def test_loader_rejects_unknown_verb(tmp_path: Path) -> None:
    body = _VALID_YAML + '  bogus.verb: "ask"\n'
    with pytest.raises(RuntimeError, match="not a known verb"):
        load_mcp_config(_write_yaml(tmp_path, body))


def test_loader_rejects_invalid_posture(tmp_path: Path) -> None:
    # (Floor-relaxation invariant 4 isn't reachable in P2 — every P2 verb floor
    #  is the loosest posture "auto" — so it lands with the Phase 3 write verbs.)
    body = _VALID_YAML.replace('activity.list: "auto"', 'activity.list: "sometimes"')
    with pytest.raises(RuntimeError, match="posture"):
        load_mcp_config(_write_yaml(tmp_path, body))


def test_loader_requires_operator_token_match(tmp_path: Path) -> None:
    body = _VALID_YAML.replace('token_env: "TESSERACT_MCP_SECRET"\n    trust_tier: "operator"',
                               'token_env: "OTHER_ENV"\n    trust_tier: "operator"')
    with pytest.raises(RuntimeError, match="token_secret_env"):
        load_mcp_config(_write_yaml(tmp_path, body))
