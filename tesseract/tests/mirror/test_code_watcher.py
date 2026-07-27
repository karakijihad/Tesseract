"""CodeWatcher classification + loop behavior.

Two layers under test:

1. ``classify_drift`` — pure path-based bucket logic.
2. ``CodeWatcher`` — boot snapshot + tick diff + emit + auto-restart paths,
   driven over a tmp git repo.

Tests MUST NOT write to ``tesseract/logs/`` (project log-leak rule). Fixtures
build everything inside ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from tesseract.mirror.server.code_watcher import (
    CodeWatcher,
    Classification,
    classify_drift,
)


# ── classifier tests ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "paths, expected",
    [
        # Backend Python under tesseract/ but not under mirror/src → restart_required.
        (["tesseract/brain/prompt.py"], "restart_required"),
        (["tesseract/orchestrator/lifeline/growth.py"], "restart_required"),
        (["tesseract/kernel/tools/lifeline_summary.py"], "restart_required"),
        # Frontend only.
        (["tesseract/mirror/src/views/AutonomyView.tsx"], "frontend_only"),
        (["tesseract/mirror/src/styles/globals.css"], "frontend_only"),
        (["tesseract/mirror/src/stores/dispatch.ts"], "frontend_only"),
        # YAML under tesseract/config/ is config_watcher's domain → ignore.
        (["tesseract/config/mirror.yaml"], "ignore"),
        (["tesseract/config/permissions.yaml"], "ignore"),
        # Docs and markdown.
        (["Docs/Sessions/2026-05-20.md"], "ignore"),
        (["Docs/Logs/CODEMAP.md"], "ignore"),
        (["README.md"], "ignore"),
        # Mixed — escalate to highest tier.
        (
            [
                "Docs/Plan/foo.md",
                "tesseract/mirror/src/foo.tsx",
                "tesseract/brain/prompt.py",
            ],
            "restart_required",
        ),
        # Mixed frontend + doc → frontend_only.
        (
            [
                "Docs/Plan/foo.md",
                "tesseract/mirror/src/foo.tsx",
            ],
            "frontend_only",
        ),
        # Empty list.
        ([], "ignore"),
        # Windows-style separators normalize cleanly.
        (["tesseract\\brain\\prompt.py"], "restart_required"),
        # Repo-root scripts.
        (["scripts/migrate.py"], "restart_required"),
    ],
)
def test_classify_drift(paths: list[str], expected: Classification) -> None:
    assert classify_drift(paths) == expected


# ── watcher integration tests ─────────────────────────────────────────


def _git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout


@pytest.fixture
def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tiny tmp git repo with a backend file and a frontend file."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "tesseract").mkdir()
    (repo / "tesseract" / "brain").mkdir()
    (repo / "tesseract" / "mirror").mkdir()
    (repo / "tesseract" / "mirror" / "src").mkdir()
    (repo / "tesseract" / "brain" / "prompt.py").write_text("v1\n", encoding="utf-8")
    (repo / "tesseract" / "mirror" / "src" / "App.tsx").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.mark.asyncio
async def test_watcher_silent_when_unchanged(_repo: Path) -> None:
    """Two ticks with no edits emit nothing."""
    emissions: list[tuple] = []

    async def emit(cls, paths, head_drift, dirty_drift, head_sha):
        emissions.append((cls, list(paths), head_drift, dirty_drift, head_sha))

    watcher = CodeWatcher(
        repo_root=_repo,
        emit_fn=emit,
        interval_seconds=0.05,
    )
    await watcher.start()
    await asyncio.sleep(1.5)  # plenty of time for several ticks on Windows
    await watcher.stop()
    assert emissions == []


@pytest.mark.asyncio
async def test_watcher_emits_on_backend_edit(_repo: Path) -> None:
    emissions: list[tuple] = []

    async def emit(cls, paths, head_drift, dirty_drift, head_sha):
        emissions.append((cls, list(paths), head_drift, dirty_drift, head_sha))

    watcher = CodeWatcher(repo_root=_repo, emit_fn=emit, interval_seconds=0.05)
    await watcher.start()
    # Edit a backend .py file → restart_required, dirty_drift True.
    (_repo / "tesseract" / "brain" / "prompt.py").write_text("v2\n", encoding="utf-8")
    await asyncio.sleep(2.0)
    await watcher.stop()

    assert any(e[0] == "restart_required" for e in emissions), emissions
    first = next(e for e in emissions if e[0] == "restart_required")
    assert "tesseract/brain/prompt.py" in first[1]
    assert first[3] is True  # dirty_drift
    # head_sha threads through — should be the boot SHA (40-char hex) since
    # we never moved HEAD in this test.
    assert isinstance(first[4], str) and len(first[4]) == 40


@pytest.mark.asyncio
async def test_watcher_emits_on_frontend_edit(_repo: Path) -> None:
    emissions: list[tuple] = []

    async def emit(cls, paths, head_drift, dirty_drift, head_sha):
        emissions.append((cls, list(paths), head_drift, dirty_drift, head_sha))

    watcher = CodeWatcher(repo_root=_repo, emit_fn=emit, interval_seconds=0.05)
    await watcher.start()
    (_repo / "tesseract" / "mirror" / "src" / "App.tsx").write_text("v2\n", encoding="utf-8")
    await asyncio.sleep(2.0)
    await watcher.stop()

    assert any(e[0] == "frontend_only" for e in emissions), emissions


@pytest.mark.asyncio
async def test_watcher_no_emit_for_docs_only(_repo: Path) -> None:
    """Markdown edits stay silent."""
    emissions: list[tuple] = []

    async def emit(cls, paths, head_drift, dirty_drift, head_sha):
        emissions.append((cls, list(paths), head_drift, dirty_drift, head_sha))

    (_repo / "Docs").mkdir()
    watcher = CodeWatcher(repo_root=_repo, emit_fn=emit, interval_seconds=0.05)
    await watcher.start()
    (_repo / "Docs" / "Notes.md").write_text("hello\n", encoding="utf-8")
    await asyncio.sleep(2.0)
    await watcher.stop()
    assert emissions == []


@pytest.mark.asyncio
async def test_watcher_save_and_revert_silent(_repo: Path) -> None:
    """A file written then reverted to identical content within one tick
    leaves the snapshot unchanged. Content hashes make this deterministic
    (unlike mtime)."""
    emissions: list[tuple] = []

    async def emit(cls, paths, head_drift, dirty_drift, head_sha):
        emissions.append((cls, list(paths), head_drift, dirty_drift, head_sha))

    target = _repo / "tesseract" / "brain" / "prompt.py"
    original = target.read_text(encoding="utf-8")
    watcher = CodeWatcher(repo_root=_repo, emit_fn=emit, interval_seconds=2.0)
    await watcher.start()
    target.write_text("temp\n", encoding="utf-8")
    target.write_text(original, encoding="utf-8")
    # No tick happens before stop (interval=2s, sleep=0.1s) but the watcher's
    # in-memory snapshot reflects the original. Manually force a tick.
    await watcher._tick()
    await watcher.stop()
    assert emissions == []


@pytest.mark.asyncio
async def test_watcher_auto_restart_invokes_callback(_repo: Path) -> None:
    """`auto_restart=True` + restart_required drift → restart_fn is called."""
    restart_calls: list[tuple] = []

    async def emit(cls, paths, head_drift, dirty_drift, head_sha):
        pass

    async def restart(paths, short_sha):
        restart_calls.append((list(paths), short_sha))

    watcher = CodeWatcher(
        repo_root=_repo,
        emit_fn=emit,
        interval_seconds=0.05,
        auto_restart=True,
        restart_fn=restart,
    )
    await watcher.start()
    (_repo / "tesseract" / "brain" / "prompt.py").write_text("v3\n", encoding="utf-8")
    await asyncio.sleep(2.0)
    await watcher.stop()
    assert len(restart_calls) >= 1


@pytest.mark.asyncio
async def test_watcher_handles_renames_cleanly(_repo: Path) -> None:
    """A rename emits ``R<status> dest\\x00src`` in `--porcelain -z` output.
    The watcher must consume the source token and not parse it as its own
    XY-prefixed entry."""
    emissions: list[tuple] = []

    async def emit(cls, paths, head_drift, dirty_drift, head_sha):
        emissions.append((cls, list(paths), head_drift, dirty_drift, head_sha))

    watcher = CodeWatcher(repo_root=_repo, emit_fn=emit, interval_seconds=2.0)
    await watcher.start()
    # `git mv` produces a rename in the status output.
    src = _repo / "tesseract" / "brain" / "prompt.py"
    dst = _repo / "tesseract" / "brain" / "prompt_renamed.py"
    _git(_repo, "mv", str(src.relative_to(_repo)).replace("\\", "/"),
         str(dst.relative_to(_repo)).replace("\\", "/"))
    await watcher._tick()
    await watcher.stop()
    # Renaming a .py file still counts as restart_required.
    assert any(e[0] == "restart_required" for e in emissions), emissions
    # The destination path is what shows up in the change list.
    first = next(e for e in emissions if e[0] == "restart_required")
    assert any("prompt_renamed.py" in p for p in first[1])


@pytest.mark.asyncio
async def test_watcher_disables_when_not_a_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-repo dir → boot snapshot fails → watcher disables, no crash."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path / "home"))
    nonrepo = tmp_path / "nonrepo"
    nonrepo.mkdir()

    async def emit(cls, paths, head_drift, dirty_drift, head_sha):
        pass

    watcher = CodeWatcher(repo_root=nonrepo, emit_fn=emit, interval_seconds=0.05)
    await watcher.start()
    assert watcher._task is None  # loop never started
    await watcher.stop()  # idempotent


@pytest.mark.asyncio
async def test_watcher_fail_open_on_process_lookup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduces the Windows Proactor race where ``proc.communicate()``
    raises ``ProcessLookupError`` during transport teardown (cpython
    bpo-45034 family). The watcher's contract is fail-open — any
    subprocess error must disable the watcher and return cleanly,
    NOT bubble up and abort backend boot as "code_watcher: refused
    to start — code drift will not surface."
    """
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path / "home"))

    # Patch the module-level _run_git's subprocess primitive so the
    # very first call raises ProcessLookupError. This is what the user
    # actually saw in their backend log.
    import tesseract.mirror.server.code_watcher as cw

    real_exec = asyncio.create_subprocess_exec

    async def boom(*_args, **_kwargs):
        raise ProcessLookupError(3, "no such process")

    monkeypatch.setattr(cw.asyncio, "create_subprocess_exec", boom)

    async def emit(cls, paths, head_drift, dirty_drift, head_sha):
        pass

    watcher = CodeWatcher(
        repo_root=tmp_path, emit_fn=emit, interval_seconds=0.05,
    )
    # MUST NOT raise. Previously this would bubble ProcessLookupError
    # out of `_run_git` → `_git_head_sha` → `_snapshot_tree` → start().
    await watcher.start()
    assert watcher._task is None
    assert watcher._disabled is True
    await watcher.stop()

    # Restore so other tests in the module keep working.
    monkeypatch.setattr(cw.asyncio, "create_subprocess_exec", real_exec)
