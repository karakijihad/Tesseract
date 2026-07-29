"""Distributable-app release gate — CL-M9. Five places in this repo each
declare a product version:

- ``tesseract/__init__.py::__version__`` — the AUTHORITY. It's what
  ``mirror/server/routes/system.py`` surfaces to the user in-app, so
  it's the one value an operator actually sees.
- ``tesseract/pyproject.toml`` ``[project].version``
- ``tesseract/mirror/package.json`` ``version``
- ``tesseract/mirror/src-tauri/Cargo.toml`` ``[package].version``
- ``tesseract/mirror/src-tauri/tauri.conf.json`` ``version`` (the
  installer/app version)

``tesseract/mirror/src-tauri/src/provision.rs::DEPS_VERSION`` is
deliberately NOT covered here — it's a deps-cache-invalidation counter,
not a product version.

This script never rewrites any of the five — picking a target version is
an operator decision. It only reports disagreement, loudly, so drift
can't ship silently.

Usage:
    python -m tesseract.scripts.check_version_consistency
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from tesseract.paths import ROOT

AUTHORITY_LABEL = "tesseract/__init__.py::__version__"


@dataclass
class VersionDeclaration:
    label: str
    path: Path
    version: str | None  # None means the file is missing or unparseable


def _read_json_version(path: Path) -> str | None:
    data = json.loads(path.read_text(encoding="utf-8"))
    version = data.get("version")
    return version if isinstance(version, str) else None


def _read_cargo_toml_version(path: Path) -> str | None:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    version = data.get("package", {}).get("version")
    return version if isinstance(version, str) else None


def _read_pyproject_version(path: Path) -> str | None:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    version = data.get("project", {}).get("version")
    return version if isinstance(version, str) else None


def _read_init_version(path: Path) -> str | None:
    text = path.read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
    return match.group(1) if match else None


def collect_declarations(root: Path = ROOT) -> list[VersionDeclaration]:
    sources: list[tuple[str, Path, callable]] = [
        (AUTHORITY_LABEL, root / "tesseract" / "__init__.py", _read_init_version),
        ("tesseract/pyproject.toml::[project].version", root / "tesseract" / "pyproject.toml", _read_pyproject_version),
        ("tesseract/mirror/package.json::version", root / "tesseract" / "mirror" / "package.json", _read_json_version),
        (
            "tesseract/mirror/src-tauri/Cargo.toml::[package].version",
            root / "tesseract" / "mirror" / "src-tauri" / "Cargo.toml",
            _read_cargo_toml_version,
        ),
        (
            "tesseract/mirror/src-tauri/tauri.conf.json::version",
            root / "tesseract" / "mirror" / "src-tauri" / "tauri.conf.json",
            _read_json_version,
        ),
    ]
    declarations = []
    for label, path, reader in sources:
        version = reader(path) if path.exists() else None
        declarations.append(VersionDeclaration(label, path, version))
    return declarations


def build_report(declarations: list[VersionDeclaration]) -> str | None:
    """Returns None if every declaration matches the authority. Otherwise a
    diff-style report naming every offender (mismatched or missing)."""
    authority = next(d for d in declarations if d.label == AUTHORITY_LABEL)
    if authority.version is None:
        return (
            f"[version-check] authority {AUTHORITY_LABEL} ({authority.path}) has no "
            "parseable version — cannot verify consistency against it."
        )

    offenders = []
    for d in declarations:
        if d is authority:
            continue
        if d.version is None:
            offenders.append(f"  MISSING   {d.label} ({d.path}) — no version could be parsed")
        elif d.version != authority.version:
            offenders.append(f"  MISMATCH  {d.label} ({d.path}) = {d.version!r}, expected {authority.version!r}")

    if not offenders:
        return None

    header = (
        f"[version-check] version declarations disagree with the authority "
        f"{AUTHORITY_LABEL} = {authority.version!r}:"
    )
    return "\n".join([header, *offenders])


def main() -> int:
    declarations = collect_declarations()
    report = build_report(declarations)
    if report is None:
        authority = next(d for d in declarations if d.label == AUTHORITY_LABEL)
        print(f"[version-check] all version declarations agree: {authority.version}")
        return 0
    print(report, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
