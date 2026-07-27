"""Plan 2c Task 5a — runtime source files must not hardcode the operator's name.

Guards three tracked source files against reintroducing the operator's real
name and proves the neutral replacement phrasing landed. Task 8: the name
itself is no longer hardcoded here (this file ships) — it's loaded from
`.pii-tokens.local.json` like the sibling PII tests, and the check skips
loudly when that file is absent (every friend's clone).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

_RETRIEVAL_PY = _REPO_ROOT / "tesseract" / "memory" / "retrieval.py"
_PROMPT_CONTENT_PY = _REPO_ROOT / "tesseract" / "brain" / "prompt_content.py"
_WORKSPACE_PY = _REPO_ROOT / "tesseract" / "mirror" / "server" / "routes" / "workspace.py"

_TARGET_FILES = (_RETRIEVAL_PY, _PROMPT_CONTENT_PY, _WORKSPACE_PY)


def test_no_operator_name_present() -> None:
    tokens_file = _REPO_ROOT / ".pii-tokens.local.json"
    if not tokens_file.exists():
        pytest.skip("operator PII token list not present on this machine")
    tokens = json.loads(tokens_file.read_text(encoding="utf-8"))["tokens"]
    for path in _TARGET_FILES:
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for tok in tokens:
            assert tok.lower() not in lowered, f"{path} still contains an operator PII token"


def test_retrieval_uses_neutral_example_name() -> None:
    text = _RETRIEVAL_PY.read_text(encoding="utf-8")
    assert "Ada Lovelace" in text


def test_prompt_content_uses_neutral_phrasing() -> None:
    text = _PROMPT_CONTENT_PY.read_text(encoding="utf-8")
    assert "the operator approved" in text
    assert "the operator's back at the desk" in text


def test_workspace_reject_template_neutral_and_gender_neutral() -> None:
    text = _WORKSPACE_PY.read_text(encoding="utf-8")
    assert "I checked with the operator — they prefer" in text
    # Format placeholder must survive the edit so .format(tool=...) still works.
    assert "{tool}" in text


def test_target_modules_import_cleanly() -> None:
    import tesseract.brain.prompt_content  # noqa: F401
    import tesseract.memory.retrieval  # noqa: F401
    import tesseract.mirror.server.routes.workspace  # noqa: F401
