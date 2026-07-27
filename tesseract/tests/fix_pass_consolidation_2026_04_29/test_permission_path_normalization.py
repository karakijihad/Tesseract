"""Audit C1 regression — absolute paths inside the workspace must hit the
relative DENY rules in `permissions.yaml`.

Before 2026-04-29, `_path_posture` did a raw prefix match against
`tool_input.file_path`. Rules were written as relative prefixes (e.g.
`tesseract/brain/`); a model passing an absolute path to file_write
(e.g. `C:\\...\\tesseract\\brain\\chat.py`) bypassed the kernel
lockdown and fell through to the default `file_write: ask` posture.

The fix passes `workspace_root` into `PermissionPolicy` and normalizes
absolute paths inside the workspace to forward-slash relative form
before the prefix check.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from tesseract.kernel.tools.base import PermissionResult
from tesseract.permissions.policy import PermissionPolicy


class _FileWriteInput(BaseModel):
    file_path: str
    content: str = ""


def _build_policy(workspace_root: Path) -> PermissionPolicy:
    return PermissionPolicy(
        tools_defaults={"file_write": "ask"},
        modes={"max": {"overrides": {}}, "headless": {"overrides": {}}},
        path_overrides={
            "file_write": [
                {"path_prefix": "tesseract/memory-store/", "posture": "auto"},
                {"path_prefix": "tesseract/brain/", "posture": "deny"},
                {"path_prefix": "tesseract/kernel/", "posture": "deny"},
                {"path_prefix": "tesseract/permissions/", "posture": "deny"},
            ]
        },
        current_mode="max",
        workspace_root=str(workspace_root),
    )


def test_relative_kernel_path_denied(tmp_path: Path) -> None:
    policy = _build_policy(tmp_path)
    inp = _FileWriteInput(file_path="tesseract/brain/chat.py")
    assert policy.get_posture("file_write", inp) == PermissionResult.DENY


def test_absolute_kernel_path_inside_workspace_denied(tmp_path: Path) -> None:
    """The audit-3 C1 bypass: absolute paths used to escape the DENY rule."""
    policy = _build_policy(tmp_path)
    target = tmp_path / "tesseract" / "brain" / "chat.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x", encoding="utf-8")
    inp = _FileWriteInput(file_path=str(target))
    assert policy.get_posture("file_write", inp) == PermissionResult.DENY


def test_absolute_permissions_path_inside_workspace_denied(tmp_path: Path) -> None:
    policy = _build_policy(tmp_path)
    target = tmp_path / "tesseract" / "permissions" / "policy.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x", encoding="utf-8")
    inp = _FileWriteInput(file_path=str(target))
    assert policy.get_posture("file_write", inp) == PermissionResult.DENY


def test_absolute_writable_path_still_auto(tmp_path: Path) -> None:
    """Writable AUTO surfaces (memory-store) still resolve to PASSTHROUGH
    when given as absolute paths."""
    policy = _build_policy(tmp_path)
    target = tmp_path / "tesseract" / "memory-store" / "diary" / "entry.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    inp = _FileWriteInput(file_path=str(target))
    assert policy.get_posture("file_write", inp) == PermissionResult.PASSTHROUGH


def test_absolute_path_outside_workspace_falls_through(tmp_path: Path) -> None:
    """Paths outside the workspace can't be normalized to a relative form;
    the prefix match misses and the default posture (ASK) applies."""
    policy = _build_policy(tmp_path)
    inp = _FileWriteInput(file_path="/tmp/scratch.txt")
    assert policy.get_posture("file_write", inp) == PermissionResult.ASK


def test_relative_dotdot_traversal_through_auto_prefix_denied(tmp_path: Path) -> None:
    """W1 reviewer follow-up — `tesseract/memory-store/../brain/chat.py`
    used to pass through the AUTO `memory-store/` prefix without ever
    consulting the DENY `brain/` rule because raw prefix matching never
    resolved the `..`. The fix must resolve every path against
    workspace_root before prefix matching."""
    policy = _build_policy(tmp_path)
    target = tmp_path / "tesseract" / "brain" / "chat.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x", encoding="utf-8")
    inp = _FileWriteInput(file_path="tesseract/memory-store/../brain/chat.py")
    assert policy.get_posture("file_write", inp) == PermissionResult.DENY


def test_relative_redundant_separators_denied(tmp_path: Path) -> None:
    """Defense in depth — a path like `tesseract//brain//chat.py` should
    still resolve to the kernel-lockdown DENY."""
    policy = _build_policy(tmp_path)
    target = tmp_path / "tesseract" / "brain" / "chat.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x", encoding="utf-8")
    inp = _FileWriteInput(file_path="tesseract//brain//chat.py")
    assert policy.get_posture("file_write", inp) == PermissionResult.DENY


def test_no_workspace_root_preserves_legacy_relative_only(tmp_path: Path) -> None:
    """Backward-compat: when workspace_root is None, only relative-prefix
    matching applies (the audit's pre-fix behavior). This keeps the legacy
    PermissionPolicy(...) call sites in tests working."""
    policy = PermissionPolicy(
        tools_defaults={"file_write": "ask"},
        modes={"max": {"overrides": {}}},
        path_overrides={
            "file_write": [{"path_prefix": "tesseract/brain/", "posture": "deny"}]
        },
        current_mode="max",
    )
    rel_inp = _FileWriteInput(file_path="tesseract/brain/chat.py")
    assert policy.get_posture("file_write", rel_inp) == PermissionResult.DENY
    abs_inp = _FileWriteInput(file_path="/Users/x/tesseract/brain/chat.py")
    # Without workspace_root, no normalization — bypass remains (documented
    # back-compat behavior; production callers must pass workspace_root).
    assert policy.get_posture("file_write", abs_inp) == PermissionResult.ASK
