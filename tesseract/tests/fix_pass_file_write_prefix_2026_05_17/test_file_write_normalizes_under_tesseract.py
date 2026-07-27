"""2026-05-17 — close the bare-relative-path lockdown bypass.

`FileWriteInput.file_path` is now normalized by a Pydantic field
validator that prepends `tesseract/` to any relative path missing the
prefix. Without it, TARS could send `kernel/chat.py` and slip past the
`tesseract/kernel/` DENY rule in `path_overrides` (the policy layer
saw the raw input).
"""

from __future__ import annotations

from pathlib import Path

from tesseract.kernel.tools.base import PermissionResult
from tesseract.kernel.tools.file_write import FileWriteInput
from tesseract.permissions.policy import PermissionPolicy


def _build_policy(workspace_root: Path) -> PermissionPolicy:
    return PermissionPolicy(
        tools_defaults={"file_write": "ask"},
        modes={"max": {"overrides": {}}, "headless": {"overrides": {}}},
        path_overrides={
            "file_write": [
                {"path_prefix": "tesseract/memory-store/", "posture": "auto"},
                {"path_prefix": "tesseract/tars-workshop/", "posture": "auto"},
                {"path_prefix": "tesseract/brain/", "posture": "deny"},
                {"path_prefix": "tesseract/kernel/", "posture": "deny"},
                {"path_prefix": "tesseract/permissions/", "posture": "deny"},
            ]
        },
        current_mode="max",
        workspace_root=str(workspace_root),
    )


def test_bare_relative_kernel_path_is_normalized_then_denied(tmp_path: Path) -> None:
    inp = FileWriteInput(file_path="kernel/chat.py", content="x")
    assert inp.file_path == "tesseract/kernel/chat.py"
    policy = _build_policy(tmp_path)
    assert policy.get_posture("file_write", inp) == PermissionResult.DENY


def test_bare_relative_brain_path_is_normalized_then_denied(tmp_path: Path) -> None:
    inp = FileWriteInput(file_path="brain/prompt.py", content="x")
    assert inp.file_path == "tesseract/brain/prompt.py"
    policy = _build_policy(tmp_path)
    assert policy.get_posture("file_write", inp) == PermissionResult.DENY


def test_bare_relative_downloads_lands_under_tesseract(tmp_path: Path) -> None:
    inp = FileWriteInput(file_path="downloads/elevated-toilet-seat.png", content="x")
    assert inp.file_path == "tesseract/downloads/elevated-toilet-seat.png"


def test_canonical_tesseract_prefix_left_alone() -> None:
    inp = FileWriteInput(file_path="tesseract/memory-store/diary.md", content="x")
    assert inp.file_path == "tesseract/memory-store/diary.md"


def test_absolute_path_left_alone_for_path_validator(tmp_path: Path) -> None:
    target = tmp_path / "tesseract" / "memory-store" / "diary.md"
    inp = FileWriteInput(file_path=str(target), content="x")
    assert inp.file_path == str(target)


def test_workshop_relative_is_normalized_and_auto(tmp_path: Path) -> None:
    inp = FileWriteInput(file_path="tars-workshop/2026-05-17/notes.md", content="x")
    assert inp.file_path == "tesseract/tars-workshop/2026-05-17/notes.md"
    policy = _build_policy(tmp_path)
    assert policy.get_posture("file_write", inp) == PermissionResult.PASSTHROUGH


def test_backslash_relative_path_is_normalized(tmp_path: Path) -> None:
    inp = FileWriteInput(file_path="kernel\\tools\\foo.py", content="x")
    assert inp.file_path == "tesseract/kernel/tools/foo.py"
    policy = _build_policy(tmp_path)
    assert policy.get_posture("file_write", inp) == PermissionResult.DENY
