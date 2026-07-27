"""Phase-1 exit gate: an app update (code-tree replacement) must lose no
operator state.

Three tests, deliberately different in what they prove:

- `test_replacing_code_tree_loses_no_state` — the brief's literal spec. It
  simulates the update (rmtree + recreate a stand-in `app/` dir) and checks
  that state living under a *separate* home dir survived. This proves the
  relocation model is sound, but never touches a single real Task 1-4
  writer — a consumer that still resolves a path against `TESSERACT_DIR`
  at import time would pass this test undetected, because nothing here
  calls that consumer.

- `test_real_writers_leave_code_tree_untouched` — exercises the actual
  production writer FUNCTIONS/CLASSES this phase touched (workspace change
  apply, agent card create, session save, memory-store write, workspace-
  event log write, diary append), each constructed directly with a
  home-anchored path, with `TESSERACT_HOME` pointed at a temp dir. It then
  hashes every file under the REAL `tesseract/{workspace,agents,sessions,
  logs,config,memory-store,vault}` code-tree directories before and after.
  Any writer that missed the home-dir migration and fell back to a
  `TESSERACT_DIR`-relative path would show up here as a new or modified
  hash — the assertion the simulation above cannot make.

  IMPORTANT SCOPE NOTE: this test constructs each writer directly (e.g.
  `DiaryAppendTool(repo_root=home_dir())`), so it proves the WRITER CLASS
  is home-dir-correct when given a home-dir path. It does NOT prove the
  production WIRING (`brain/boot.py::build_tool_registry`) actually hands
  it one — a call site that regressed to `repo_root=TESSERACT_DIR` would
  NOT be caught here, because this test never calls `build_tool_registry`.
  That gap is exactly what happened during this task's own review: an
  earlier version of this file claimed reverting `boot.py`'s wiring made
  THIS test fail. It does not — verified by literally doing it (see
  `test_boot_wires_diary_append_under_home` below for the real check, and
  the Task 5 report for the corrected experiment record).

- `test_boot_wires_diary_append_under_home` — the real wiring check.
  Runs `build_tool_registry()` for real, in a fresh subprocess with
  `TESSERACT_HOME` set in the environment *before* Python starts (matching
  how the packaged app actually launches), and asserts the registered
  `diary_append` tool's `_repo_root` is the relocated home dir. This is
  what actually catches a `boot.py` wiring regression; the writer-probe
  test above cannot.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def test_replacing_code_tree_loses_no_state(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("TESSERACT_HOME", str(home))
    from tesseract.config_seed import (
        ensure_agents_seeded, ensure_config_seeded, ensure_workspace_seeded,
    )
    ensure_config_seeded(); ensure_workspace_seeded(); ensure_agents_seeded()
    (home / "workspace" / "SOUL.md").write_text("operator soul", encoding="utf-8")
    (home / "memory-store").mkdir()
    (home / "memory-store" / "m1.md").write_text("memory", encoding="utf-8")
    (home / "sessions").mkdir()
    (home / "sessions" / "s.jsonl").write_text("{}", encoding="utf-8")

    fake_app = tmp_path / "app"          # stand-in for <home>/app
    fake_app.mkdir()
    (fake_app / "old.py").write_text("old", encoding="utf-8")
    shutil.rmtree(fake_app)              # exactly what provision/update does
    fake_app.mkdir()
    (fake_app / "new.py").write_text("new", encoding="utf-8")

    assert (home / "workspace" / "SOUL.md").read_text(encoding="utf-8") == "operator soul"
    assert (home / "memory-store" / "m1.md").exists()
    assert (home / "sessions" / "s.jsonl").exists()


# Critical 2 fix: includes memory-store/ and vault/ — the exact subtree the
# flagship diary bug wrote into. Full-content sha256 over this dev
# checkout's ~6100 files / ~57MB takes ~5s per pass (measured); acceptable
# for a single exit-gate test, so no sampling/cap fallback is used.
_WATCHED_SUBDIRS = ("workspace", "agents", "sessions", "logs", "config", "memory-store", "vault")


def _hash_tree(root: Path) -> dict[str, str]:
    """sha256 of every file under `root / subdir` for each watched subdir.

    Skips `__pycache__` / `.pyc` — bytecode caching is a side effect of
    importing `tesseract.config.*` modules during this very test run, not
    state a writer produced. Keying by the relative path (not just a
    combined digest) means a file that's ADDED or REMOVED changes the dict
    just as much as one that's modified.
    """
    snapshot: dict[str, str] = {}
    for subdir in _WATCHED_SUBDIRS:
        base = root / subdir
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            rel = path.relative_to(root).as_posix()
            snapshot[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def test_real_writers_leave_code_tree_untouched(monkeypatch, tmp_path):
    """Run the real Task 1-4 writers against a relocated TESSERACT_HOME and
    assert the code tree's watched directories are byte-for-byte unchanged
    — new files included, not just modified ones.

    Scope: each writer below is constructed directly with a home-anchored
    path (`home_dir()`, `agents_dir()`, ...), not resolved through
    `brain/boot.py::build_tool_registry`'s wiring. See module docstring.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("TESSERACT_HOME", str(home))

    from tesseract.config_seed import (
        ensure_agents_seeded, ensure_config_seeded, ensure_workspace_seeded,
    )
    ensure_config_seeded()
    ensure_workspace_seeded()
    ensure_agents_seeded()

    from tesseract.paths import TESSERACT_DIR, agents_dir, home_dir

    before = _hash_tree(TESSERACT_DIR)

    # 1. Workspace change apply (propose/approve mutation path).
    from tesseract.kernel.workspace_changes import apply_change, workspace_events_dir

    applied = apply_change(
        repo_root=home,  # accepted for signature compat, unused for resolution
        target_path="tesseract/workspace/SOUL.md",
        action="append_to_section",
        content="- task-5 exit-gate writer probe\n",
        section="Growth",
    )
    assert applied.no_op_reason is None

    # 2. Agent-card write (quarantine).
    from tesseract.kernel.tools.agent_create import AgentCreateInput, AgentCreateTool
    from tesseract.kernel.tools.base import ToolContext

    agent_tool = AgentCreateTool(
        agents_dir=agents_dir(),
        models_config={"roles": {"chat_brain": {}}},
    )
    agent_input = AgentCreateInput(
        name="task5-probe",
        model_role="chat_brain",
        description="Task 5 exit-gate probe agent.",
        role_body="Probe role body.",
        prompt_sections={"Body": "Probe section body."},
        rationale="Task 5 exit-gate writer probe.",
    )
    agent_result = asyncio.run(
        agent_tool.run(agent_input, ToolContext(workspace_root=str(agents_dir())))
    )
    assert not agent_result.is_error

    # 3. Session write (also touches SOUL.md interaction_count + work index).
    from tesseract.brain.session_store import save_session

    save_session(
        home_dir() / "sessions",
        "task5-probe-session",
        "test-model",
        datetime.now(timezone.utc).isoformat(),
        [{"role": "user", "content": "task 5 exit-gate probe"}],
    )

    # 4. Memory-store write.
    from tesseract.memory.store import MemoryStore
    from tesseract.memory.types import MemoryFrontmatter, MemoryType

    memory_store = MemoryStore(store_dir=home_dir() / "memory-store")
    now = datetime.now(timezone.utc)
    frontmatter = MemoryFrontmatter(
        id="mem_task5probe",
        type=MemoryType.PROJECT,
        title="Task 5 exit-gate probe",
        summary="Task 5 exit-gate probe",
        created_at=now,
        updated_at=now,
        source_session="task5-probe-session",
        source_type="test",
    )
    written = memory_store.write(
        frontmatter,
        "Task 5 exit-gate memory-store writer probe body.",
        skip_wnts_check=True,
    )
    assert written is True

    # 5. Logs write (workspace-events store).
    from tesseract.workspace_events import EventStore, WorkspaceEvent

    event_store = EventStore(workspace_events_dir())
    event_store.append_event(
        WorkspaceEvent.new(
            kind="nudge",
            source="tars",
            title="Task 5 exit-gate probe",
            summary="Task 5 exit-gate logs writer probe.",
            payload={},
        )
    )

    # 6. Diary append (memory-store/diary/ — found leaking into the code
    # tree via boot.py's DiaryAppendTool(repo_root=TESSERACT_DIR) wiring;
    # fixed in the same commit as this test to repo_root=home_dir()). This
    # step constructs the tool directly with a correct path — it does NOT
    # exercise boot.py's wiring; see test_boot_wires_diary_append_under_home.
    from tesseract.kernel.tools.diary_append import DiaryAppendInput, DiaryAppendTool

    diary_tool = DiaryAppendTool(repo_root=home_dir())
    diary_result = asyncio.run(
        diary_tool.run(
            DiaryAppendInput(text="Task 5 exit-gate diary writer probe."),
            ToolContext(workspace_root=str(home)),
        )
    )
    assert not diary_result.is_error

    after = _hash_tree(TESSERACT_DIR)
    assert before == after, "a real writer touched the CODE tree instead of TESSERACT_HOME"

    # Sanity: the same writers actually landed under the relocated home,
    # so a no-op writer (e.g. a silently-skipped call) can't fake a pass.
    assert "task-5 exit-gate writer probe" in (home / "workspace" / "SOUL.md").read_text(encoding="utf-8")
    assert (agents_dir() / "pending" / "task5-probe.md").exists()
    assert (home_dir() / "sessions" / "task5-probe-session.json").exists()
    assert (home_dir() / "memory-store" / "project" / "mem_task5probe.md").exists()
    assert (home_dir() / "logs" / "workspace" / "events.jsonl").exists()
    assert any((home_dir() / "memory-store" / "diary").glob("*.md"))


_LIVE_BOOT_SCRIPT = """
from tesseract.config_seed import ensure_agents_seeded, ensure_config_seeded, ensure_workspace_seeded
ensure_config_seeded(); ensure_workspace_seeded(); ensure_agents_seeded()
from tesseract.brain.boot import build_tool_registry
registry, _mood, _voice, _bundle, _alarms = build_tool_registry()
tool = registry.tools["diary_append"]
print("REPO_ROOT=" + str(tool._repo_root))
"""


def test_boot_wires_diary_append_under_home(tmp_path):
    """Real, live check that `brain/boot.py::build_tool_registry` wires
    `DiaryAppendTool` under the relocated home dir — the regression the
    writer-probe test above cannot catch (see its module-docstring note).

    Runs in a FRESH subprocess with `TESSERACT_HOME` set in the child's
    environment before the interpreter starts, exactly like a real
    packaged-app launch. This sidesteps the in-process frozen-constant
    cascade (`tesseract.paths.CONFIG_DIR`, `tesseract.config.loader.
    PROVIDERS_YAML`/`ROLES_YAML`, `tesseract.brain.boot`'s re-exports of
    the same — each bound once at first import, and in a shared pytest
    process "first import" can be any earlier-collected test file) rather
    than papering over it: a real subprocess boot doesn't have that
    problem, because nothing was imported before the env var was set.

    Attempted in-process first (`monkeypatch.setattr` on
    `tesseract.brain.boot.TESSERACT_HOME`, mirroring
    `fix_pass_survivability_SU_3b/test_production_registry.py`): it failed
    with `ConfigError: providers.yaml missing`, because that existing
    test's apparent success is itself an artifact of `tesseract.brain.boot`
    being imported at COLLECTION time (module-level import in that test
    file) before any fixture runs — not a real in-process fix. Reconciling
    that whole frozen-constant cascade is the pre-existing conftest gap
    the Task 5 brief said not to fix here; this subprocess approach
    verifies the real thing without touching it.
    """
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ)
    env["TESSERACT_HOME"] = str(home)

    result = subprocess.run(
        [sys.executable, "-c", _LIVE_BOOT_SCRIPT],
        cwd=str(Path(__file__).resolve().parents[3]),
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, (
        f"build_tool_registry() failed in subprocess:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    printed = next(
        (line for line in result.stdout.splitlines() if line.startswith("REPO_ROOT=")),
        None,
    )
    assert printed is not None, f"script did not print REPO_ROOT; stdout: {result.stdout}"
    repo_root = Path(printed[len("REPO_ROOT="):])
    assert repo_root == home, (
        f"DiaryAppendTool wired to {repo_root}, expected home dir {home} "
        "— boot.py must construct it with repo_root=home_dir(), not TESSERACT_DIR"
    )
