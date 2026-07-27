"""Phase-1 state relocation: workspace/agents resolve + seed under TESSERACT_HOME.

Task 3 extends this file with the two remaining code-tree leaks: agent-card
readers (loader.py + brief_render.py) and the Mirror sessions route.
"""
import os
from pathlib import Path


def test_workspace_dir_honors_home_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    from tesseract.paths import workspace_dir
    assert workspace_dir() == tmp_path / "workspace"


def test_agents_dir_honors_home_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    from tesseract.paths import agents_dir
    assert agents_dir() == tmp_path / "agents"


def test_workspace_seeds_from_code_tree_once(monkeypatch, tmp_path):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    from tesseract.config_seed import ensure_workspace_seeded
    ensure_workspace_seeded()
    assert (tmp_path / "workspace" / "SOUL.md").exists()
    # operator edit must survive a second seeding call
    (tmp_path / "workspace" / "SOUL.md").write_text("edited", encoding="utf-8")
    ensure_workspace_seeded()
    assert (tmp_path / "workspace" / "SOUL.md").read_text(encoding="utf-8") == "edited"


def test_agents_seed_excludes_source_files(monkeypatch, tmp_path):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    from tesseract.config_seed import ensure_agents_seeded
    ensure_agents_seeded()
    dest = tmp_path / "agents"
    assert (dest / "INDEX.md").exists()
    assert not (dest / "loader.py").exists()
    assert not (dest / "__init__.py").exists()
    assert not (dest / "__pycache__").exists()


def test_workspace_change_applies_under_home_leaves_code_tree_untouched(monkeypatch, tmp_path):
    """`apply_change` must resolve PROPOSABLE_PATHS targets under the
    home-anchored `workspace_dir()`, never the code tree — the exact bug
    class an app update replacing the code tree would otherwise wipe."""
    import hashlib

    from tesseract.paths import TESSERACT_DIR

    code_soul = TESSERACT_DIR / "workspace" / "SOUL.md"
    hash_before = hashlib.sha256(code_soul.read_bytes()).hexdigest()

    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    from tesseract.config_seed import ensure_workspace_seeded
    ensure_workspace_seeded()

    from tesseract.kernel.workspace_changes import apply_change

    applied = apply_change(
        repo_root=tmp_path,  # accepted for signature compat, unused for resolution
        target_path="tesseract/workspace/SOUL.md",
        action="append_to_section",
        content="- distributable-app relocation smoke test bullet\n",
        section="Growth",
    )
    assert applied.no_op_reason is None

    home_soul = tmp_path / "workspace" / "SOUL.md"
    assert "distributable-app relocation smoke test bullet" in home_soul.read_text(encoding="utf-8")

    hash_after = hashlib.sha256(code_soul.read_bytes()).hexdigest()
    assert hash_after == hash_before, "code-tree SOUL.md must remain untouched"


def test_seed_noop_in_dev_checkout(monkeypatch):
    monkeypatch.delenv("TESSERACT_HOME", raising=False)
    from tesseract.config_seed import ensure_workspace_seeded, ensure_agents_seeded
    from tesseract.paths import TESSERACT_DIR

    workspace = TESSERACT_DIR / "workspace"
    agents = TESSERACT_DIR / "agents"
    workspace_before = sorted(p.relative_to(workspace) for p in workspace.rglob("*"))
    agents_before = sorted(p.relative_to(agents) for p in agents.rglob("*"))

    # dev: home == code tree; must be a no-op, never copy onto itself
    ensure_workspace_seeded()
    ensure_agents_seeded()

    workspace_after = sorted(p.relative_to(workspace) for p in workspace.rglob("*"))
    agents_after = sorted(p.relative_to(agents) for p in agents.rglob("*"))
    assert workspace_before == workspace_after
    assert agents_before == agents_after


def test_agent_cards_resolve_under_home(monkeypatch, tmp_path):
    """`tesseract.agents.loader` (a card READER — `tesseract.agents` the
    Python module itself is unaffected) must resolve its default agents
    dir at call time, honoring TESSERACT_HOME even though the module was
    already imported before the env var was set."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    from tesseract.config_seed import ensure_agents_seeded

    ensure_agents_seeded()

    from tesseract.agents.loader import list_agents, load_agent

    assert "observer" in list_agents()
    agent = load_agent("observer")
    assert agent.name == "observer"


def test_brief_render_tool_agents_dir_resolves_under_home(monkeypatch, tmp_path):
    """`BriefRenderTool`'s default `_agents_dir` used to capture
    `TESSERACT_DIR / "agents"` (code tree) at construction time. It must
    now resolve `TESSERACT_HOME/agents` at construction time instead."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    from tesseract.config_seed import ensure_agents_seeded

    ensure_agents_seeded()

    from tesseract.kernel.tools.brief_render import BriefRenderTool

    tool = BriefRenderTool()
    assert tool._agents_dir == tmp_path / "agents"
    assert (tool._agents_dir / "observer.md").exists()


def test_env_loads_from_home(monkeypatch, tmp_path):
    """`mirror/server/app.py::_load_env` used to import `boot.ENV_PATH` —
    frozen at `tesseract.brain.boot`'s first import, before an env var set
    later in the process (or on a relocated install) takes effect. It must
    now resolve `home_dir() / ".env"` at call time instead."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    monkeypatch.delenv("TESSERACT_LOG_LEVEL", raising=False)
    (tmp_path / ".env").write_text("TESSERACT_LOG_LEVEL=DEBUG\n", encoding="utf-8")

    from tesseract.mirror.server.app import _load_env

    _load_env()
    assert os.environ["TESSERACT_LOG_LEVEL"] == "DEBUG"


def test_locked_config_denied_under_relocated_home(monkeypatch, tmp_path):
    """`_check_runtime_lockdown` must deny a locked config file living under
    the call-time home's `config/` dir even when `workspace_root` (always
    the code tree) points somewhere else entirely — the exact gap a
    relocated `TESSERACT_HOME` opens up."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    (tmp_path / "config").mkdir(parents=True)
    target = tmp_path / "config" / "permissions.yaml"
    target.write_text("x: 1\n", encoding="utf-8")

    from tesseract.kernel.tools.file_write import _check_runtime_lockdown

    reason = _check_runtime_lockdown(target.resolve(), tmp_path / "somewhere-else")
    assert reason is not None
    assert "permissions.yaml" in reason


def test_non_locked_config_allowed_under_relocated_home(monkeypatch, tmp_path):
    """Converse of the above: a non-locked yaml under the same relocated
    config dir must NOT be denied — the guard targets the four locked
    filenames specifically, not the whole config dir."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    (tmp_path / "config").mkdir(parents=True)
    target = tmp_path / "config" / "vault.yaml"
    target.write_text("x: 1\n", encoding="utf-8")

    from tesseract.kernel.tools.file_write import _check_runtime_lockdown

    reason = _check_runtime_lockdown(target.resolve(), tmp_path / "somewhere-else")
    assert reason is None


def test_mirror_sessions_route_uses_home(monkeypatch, tmp_path):
    """`routes/sessions.py` used to freeze `_SESSIONS_DIR` at import time
    via `Path(__file__).resolve().parents[3] / "sessions"`. `_sessions_dir()`
    must now call-time-resolve to the same directory boot.py's
    `SESSIONS_DIR` uses (`TESSERACT_HOME/sessions`)."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    from tesseract.mirror.server.routes import sessions as sroute

    assert sroute._sessions_dir() == tmp_path / "sessions"
