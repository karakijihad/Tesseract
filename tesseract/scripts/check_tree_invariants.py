#!/usr/bin/env python3
"""Tree invariant checker — fails if the repo layout violates structural rules.

Rules:
  1. No .py or .yaml/.yml files anywhere under Docs/
  2. No .py or .yaml/.yml files at repo root (except the allowlist)
  3. No runtime-state folder names (memory-store, logs, sessions, vault,
     transcripts, reviewtmp, tmp_*) outside tesseract/

Run: python scripts/check_tree_invariants.py
Exit 0 = clean. Exit 1 = violations found.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Files allowed at repo root (everything else that matches *.py or *.yaml fails)
ROOT_ALLOWLIST = {
    "pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "pytest.ini",
    "setup.cfg",
    "CLAUDE.md",
    "README.md",
    "LICENSE",
    ".gitignore",
    ".gitattributes",
}

# Folder names that must not appear outside tesseract/
RUNTIME_STATE_NAMES = {
    "memory-store",
    "logs",
    "sessions",
    "vault",
    "transcripts",
    "reviewtmp",
}

violations: list[str] = []


def check_docs_no_code() -> None:
    docs = REPO_ROOT / "Docs"
    if not docs.exists():
        return
    for p in docs.rglob("*"):
        if p.is_file() and p.suffix in (".py", ".yaml", ".yml"):
            violations.append(f"Code/config under Docs/: {p.relative_to(REPO_ROOT)}")


def check_root_no_code() -> None:
    for p in REPO_ROOT.iterdir():
        if p.is_file() and p.name not in ROOT_ALLOWLIST:
            if p.suffix in (".py", ".yaml", ".yml"):
                violations.append(f"Code/config at repo root: {p.name}")


def check_runtime_state_outside_tesseract() -> None:
    tesseract = REPO_ROOT / "tesseract"
    for p in REPO_ROOT.iterdir():
        if p == tesseract or p.name.startswith("."):
            continue
        if p.is_dir():
            # Check exact names
            if p.name in RUNTIME_STATE_NAMES:
                violations.append(f"Runtime-state folder outside tesseract/: {p.name}/")
            # Check tmp_* pattern
            if p.name.startswith("tmp"):
                violations.append(f"Temp folder at repo root: {p.name}/")


def main() -> int:
    check_docs_no_code()
    check_root_no_code()
    check_runtime_state_outside_tesseract()

    if violations:
        print("TREE INVARIANT VIOLATIONS:")
        for v in violations:
            print(f"  - {v}")
        return 1

    print("Tree invariants OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
