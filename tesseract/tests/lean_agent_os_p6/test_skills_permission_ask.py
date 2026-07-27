"""P6 Task 4 — write gating for `tesseract/workspace/skills/`.

`permissions.yaml::path_overrides.file_write` gives skill-folder writes
`posture: ask` (TARS drafts, operator approves). Uses the real
permissions-evaluate path (`load_permission_policy` against the live
`tesseract/config/permissions.yaml`), not a hand-built policy — mirrors
`tesseract/tests/lean_agent_os/test_readonly_bash_autoposture.py::
test_real_permissions_yaml_loads_and_carves_out_readonly_in_max`.
"""

from __future__ import annotations

from pathlib import Path

from tesseract.kernel.tools.base import PermissionResult
from tesseract.kernel.tools.file_write import FileWriteInput
from tesseract.permissions.policy import load_permission_policy

_REAL_PERMISSIONS_YAML = Path(__file__).resolve().parents[2] / "config" / "permissions.yaml"
_REPO_ROOT = _REAL_PERMISSIONS_YAML.parents[1]


def test_skill_write_is_ask_in_standard_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    policy = load_permission_policy(_REAL_PERMISSIONS_YAML, workspace_root=str(_REPO_ROOT))
    policy.set_mode("standard")

    inp = FileWriteInput(
        file_path="tesseract/workspace/skills/daily-brief/SKILL.md",
        content="---\nname: daily-brief\ndescription: x\n---\nbody\n",
    )
    assert policy.get_posture("file_write", inp) == PermissionResult.ASK


def test_skill_write_is_ask_in_max_mode_too(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    policy = load_permission_policy(_REAL_PERMISSIONS_YAML, workspace_root=str(_REPO_ROOT))
    policy.set_mode("max")

    inp = FileWriteInput(file_path="tesseract/workspace/skills/foo/scripts/x.py", content="print(1)\n")
    assert policy.get_posture("file_write", inp) == PermissionResult.ASK
