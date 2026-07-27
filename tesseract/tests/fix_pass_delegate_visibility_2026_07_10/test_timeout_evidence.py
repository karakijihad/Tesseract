"""Fix 1 — timeout errors carry target_paths change evidence so the model can
distinguish productive-but-slow from dead (fix-pass 2026-07-10)."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

from tesseract.kernel.tools import _delegate_runner
from tesseract.kernel.tools.base import ToolContext


def test_snapshot_and_diff_reports_new_and_modified(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    existing = proj / "old.js"
    existing.write_text("v1")

    before = _delegate_runner.snapshot_target_state(str(tmp_path), ["proj"])
    assert before is not None and "proj\\old.js" in before or "proj/old.js" in before

    existing.write_text("v2 longer")
    (proj / "new.js").write_text("fresh")

    msg = _delegate_runner.describe_target_changes(before, str(tmp_path), ["proj"])
    assert "2 file(s)" in msg
    assert "new.js (new)" in msg
    assert "old.js (modified)" in msg
    assert "work WAS happening" in msg


def test_diff_reports_nothing_changed(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.js").write_text("x")
    before = _delegate_runner.snapshot_target_state(str(tmp_path), ["proj"])
    msg = _delegate_runner.describe_target_changes(before, str(tmp_path), ["proj"])
    assert "no files" in msg


def test_no_target_paths_no_evidence(tmp_path):
    assert _delegate_runner.snapshot_target_state(str(tmp_path), []) is None
    assert _delegate_runner.describe_target_changes(None, str(tmp_path), []) == ""


def test_single_file_target(tmp_path):
    f = tmp_path / "single.md"
    f.write_text("a")
    before = _delegate_runner.snapshot_target_state(str(tmp_path), ["single.md"])
    f.write_text("bb")
    msg = _delegate_runner.describe_target_changes(before, str(tmp_path), ["single.md"])
    assert "single.md (modified)" in msg


def _timeout_input(target_paths):
    return SimpleNamespace(task="t", timeout=3, target_paths=target_paths, background=False)


def test_foreground_timeout_appends_evidence_no_sink(tmp_path):
    """Non-streaming path: subprocess writes into the declared target dir then
    outlives the timeout — the error must include the changed file."""
    proj = tmp_path / "proj"
    proj.mkdir()
    script = (
        "import pathlib, time; "
        f"pathlib.Path(r'{proj / 'made.js'}').write_text('x'); "
        "time.sleep(30)"
    )
    result = asyncio.run(
        _delegate_runner.run_delegate_foreground(
            tool_name="delegate_claude",
            cli_label="claude",
            argv=(sys.executable, "-c", script),
            env=None,
            inp=_timeout_input(["proj"]),
            context=ToolContext(workspace_root=str(tmp_path), session_id="s-test"),
            cancel_event=None,
        )
    )
    assert result.is_error and result.timed_out
    assert "timed out" in result.output
    assert "made.js (new)" in result.output


def test_foreground_timeout_appends_evidence_sink_path(tmp_path):
    """Streaming path (cli_sink wired): same evidence contract."""
    proj = tmp_path / "proj"
    proj.mkdir()
    script = (
        "import pathlib, time; "
        f"pathlib.Path(r'{proj / 'made.js'}').write_text('x'); "
        "print('hello', flush=True); "
        "time.sleep(30)"
    )
    events = []

    async def sink(kind, call_id, payload):
        events.append((kind, payload))

    result = asyncio.run(
        _delegate_runner.run_delegate_foreground(
            tool_name="delegate_claude",
            cli_label="claude",
            argv=(sys.executable, "-c", script),
            env=None,
            inp=_timeout_input(["proj"]),
            context=ToolContext(
                workspace_root=str(tmp_path),
                session_id="s-test",
                current_call_id="call-1",
                cli_sink=sink,
            ),
            cancel_event=None,
        )
    )
    assert result.is_error and result.timed_out
    assert "made.js (new)" in result.output
    # The streamed transcript tail rides along too.
    assert "hello" in result.output
    assert any(kind == "cli_end" for kind, _ in events)
