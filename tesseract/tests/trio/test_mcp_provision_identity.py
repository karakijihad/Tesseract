"""W1 identity-clobber fix (W0 audit D4): a terminal provision must not
overwrite an existing LANE identity in `.mcp.json` or the GLOBAL codex
config; lane provisions still overwrite freely (lane wins)."""

from __future__ import annotations

import json
import pathlib

import pytest

from tesseract.orchestrator.tars_controller.lanes import mcp_provision


@pytest.fixture(autouse=True)
def scratch_home(tmp_path, monkeypatch):
    """Point Path.home() at tmp so the real ~/.codex/config.toml is never
    touched (provision writes the codex config via Path.home())."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    return home


def _mcp_json_env(workdir) -> str | None:
    path = workdir / ".mcp.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    auth = data["mcpServers"]["tesseract"]["headers"]["Authorization"]
    return auth.removeprefix("Bearer ${").removesuffix("}")


def _codex_env(home) -> str | None:
    path = home / ".codex" / "config.toml"
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("bearer_token_env_var"):
            return line.split('"')[1]
    return None


def test_terminal_does_not_clobber_lane_claude_mcp_json(
    tmp_path, scratch_home, fake_tokens, mcp_cfg
):
    workdir = tmp_path / "lane-wd"
    mcp_provision.provision(workdir, "claude", mcp_cfg)
    assert _mcp_json_env(workdir) == "TESSERACT_MCP_LANE_CLAUDE_TOKEN"
    mcp_provision.provision(workdir, "terminal", mcp_cfg)
    assert _mcp_json_env(workdir) == "TESSERACT_MCP_LANE_CLAUDE_TOKEN"


def test_terminal_does_not_clobber_lane_codex_global_config(
    tmp_path, scratch_home, fake_tokens, mcp_cfg
):
    workdir = tmp_path / "lane-wd"
    mcp_provision.provision(workdir, "codex", mcp_cfg)
    assert _codex_env(scratch_home) == "TESSERACT_MCP_LANE_CODEX_TOKEN"
    mcp_provision.provision(workdir, "terminal", mcp_cfg)
    assert _codex_env(scratch_home) == "TESSERACT_MCP_LANE_CODEX_TOKEN"


def test_terminal_provisions_fresh_dir_normally(
    tmp_path, scratch_home, fake_tokens, mcp_cfg
):
    workdir = tmp_path / "fresh-wd"
    mcp_provision.provision(workdir, "terminal", mcp_cfg)
    assert _mcp_json_env(workdir) == "TESSERACT_MCP_TERMINAL_TOKEN"
    assert _codex_env(scratch_home) == "TESSERACT_MCP_TERMINAL_TOKEN"


def test_lane_overwrites_terminal_identity(
    tmp_path, scratch_home, fake_tokens, mcp_cfg
):
    workdir = tmp_path / "wd"
    mcp_provision.provision(workdir, "terminal", mcp_cfg)
    mcp_provision.provision(workdir, "claude", mcp_cfg)
    assert _mcp_json_env(workdir) == "TESSERACT_MCP_LANE_CLAUDE_TOKEN"
    mcp_provision.provision(workdir, "codex", mcp_cfg)
    assert _codex_env(scratch_home) == "TESSERACT_MCP_LANE_CODEX_TOKEN"


def test_terminal_reprovision_over_terminal_still_works(
    tmp_path, scratch_home, fake_tokens, mcp_cfg
):
    workdir = tmp_path / "wd"
    mcp_provision.provision(workdir, "terminal", mcp_cfg)
    mcp_provision.provision(workdir, "terminal", mcp_cfg)
    assert _mcp_json_env(workdir) == "TESSERACT_MCP_TERMINAL_TOKEN"


def test_terminal_overwrites_unknown_stale_identity(
    tmp_path, scratch_home, fake_tokens, mcp_cfg
):
    """An entry pointing at an env var that is not a known client identity
    is stale — terminal provisioning reclaims it."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    (workdir / ".mcp.json").write_text(
        json.dumps({
            "mcpServers": {"tesseract": {
                "type": "http",
                "url": "http://old",
                "headers": {"Authorization": "Bearer ${SOME_OLD_VAR}"},
            }}
        }),
        encoding="utf-8",
    )
    mcp_provision.provision(workdir, "terminal", mcp_cfg)
    assert _mcp_json_env(workdir) == "TESSERACT_MCP_TERMINAL_TOKEN"


def test_mcp_json_lock_is_per_path(tmp_path):
    # M12: the .mcp.json read-modify-write must serialize per resolved path so
    # concurrent lane/terminal provisions can't tear the file.
    a = tmp_path / "wd-a" / ".mcp.json"
    b = tmp_path / "wd-b" / ".mcp.json"
    lock_a1 = mcp_provision._mcp_json_lock(a)
    lock_a2 = mcp_provision._mcp_json_lock(tmp_path / "wd-a" / ".mcp.json")
    lock_b = mcp_provision._mcp_json_lock(b)
    assert lock_a1 is lock_a2  # same path → same lock
    assert lock_a1 is not lock_b  # different path → different lock


def test_concurrent_provision_same_dir_is_race_safe(
    tmp_path, scratch_home, fake_tokens, mcp_cfg
):
    # M12: hammer one working dir with interleaved lane(claude) + terminal
    # provisions from many threads. The file must never tear (always valid
    # JSON), the lane identity must win, and no temp files may leak.
    import concurrent.futures

    workdir = tmp_path / "wd"
    kinds = ["claude", "terminal"] * 12

    def _one(kind: str) -> None:
        mcp_provision.provision(workdir, kind, mcp_cfg)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for fut in concurrent.futures.as_completed(
            [ex.submit(_one, k) for k in kinds]
        ):
            fut.result()  # surface any torn-read RuntimeError / JSONDecodeError

    assert _mcp_json_env(workdir) == "TESSERACT_MCP_LANE_CLAUDE_TOKEN"  # lane wins
    assert not list(workdir.glob(".mcp.json.*"))  # no leaked temp files


def test_provision_preserves_foreign_keys(
    tmp_path, scratch_home, fake_tokens, mcp_cfg
):
    workdir = tmp_path / "wd"
    workdir.mkdir()
    (workdir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"other": {"type": "stdio"}}, "x": 1}),
        encoding="utf-8",
    )
    mcp_provision.provision(workdir, "claude", mcp_cfg)
    data = json.loads((workdir / ".mcp.json").read_text(encoding="utf-8"))
    assert data["x"] == 1
    assert data["mcpServers"]["other"] == {"type": "stdio"}
