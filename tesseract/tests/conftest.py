"""pytest path setup.

The test suite imports `tesseract.*` modules. Ensure the repo root is on
sys.path so `from tesseract.brain.observer import Observer` resolves
regardless of where pytest is invoked from.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(autouse=True)
def _reset_tokenjuice_cache():
    """AU-15: the TokenJuice module-level cache in `brain.tools` is loaded
    once on first `execute_tool` call. Reset between tests so a previous
    test's monkeypatched `TESSERACT_HOME` (and its expired tmp_path
    `user_rules_dir`) cannot poison subsequent tests, and a one-time
    `init_failed` latch cannot freeze the whole session."""
    from tesseract.brain.tools import reset_tokenjuice_cache
    reset_tokenjuice_cache()
    yield
    reset_tokenjuice_cache()


@pytest.fixture(autouse=True)
def _isolate_tesseract_home(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """AU-14 14b: default ``TESSERACT_HOME`` to a per-test tmp dir so
    production tripwires (FallbackAdapter HARD, image_generate uniform,
    tavily/web HTTP errors) and any other writer that resolves
    ``TESSERACT_HOME`` at call time cannot leak rows into
    ``tesseract/logs/`` from a test that forgot to isolate itself.

    **Fallback only.** Tests that need their JSONL writes and
    assertions to share a directory keep calling
    ``monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))`` themselves —
    the local override wins because it runs after this autouse setup.
    The local ``tmp_path`` fixture and this one's ``tmp_path_factory``
    allocate distinct dirs; the autouse dir is a write-deflector for
    tests whose assertions don't care where the file lives (most prior
    suites). Tests asserting ``ph.provider_health_dir()`` content MUST
    define their own ``isolated_home`` (the AU-14 fix_pass directory
    follows this pattern) so the assertion reads from the same dir the
    write targeted.

    Distributable-app Task 3: agent-card readers (``tesseract.agents.
    loader``, ``brief_render``, ``memory.classifier``) now resolve
    ``agents_dir()`` at call time like every other TESSERACT_HOME-anchored
    path, so this isolated dir starts with no agent cards. All three
    production entry points that can reach card loading —
    ``mirror/server/__main__.py::main``, ``supervisor/__main__.py::main``,
    and ``scripts/tars_controller.py::main`` — run ``ensure_config_seeded()``
    + ``ensure_workspace_seeded()`` + ``ensure_agents_seeded()`` before
    anything reads a card; mirror that here so tests calling
    `load_agent`/`list_agents` without an explicit `agents_dir=` override
    still find the built-ins, same as at boot.
    """
    isolated = tmp_path_factory.mktemp("tesseract_home")
    monkeypatch.setenv("TESSERACT_HOME", str(isolated))
    from tesseract.config_seed import ensure_agents_seeded
    ensure_agents_seeded()
    return isolated
