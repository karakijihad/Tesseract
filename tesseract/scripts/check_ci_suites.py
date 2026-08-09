"""Keep the CI test-suite manifest honest.

The regression job used to name its directories inline in the workflow. A
suite added afterwards was never run and nothing said so — the build stayed
green while coverage narrowed. 24 of 184 suite directories were live when
this was written.

The manifest (`tests/ci-suites.txt`) fixes the silence rather than the
coverage: every suite directory must appear under `[run]` or `[excluded]`,
and `--check` fails naming any that appear under neither. Adding a suite is
still a deliberate act, but forgetting one is now a red build instead of a
quiet omission.

Usage:
    python -m tesseract.scripts.check_ci_suites --check
    python -m tesseract.scripts.check_ci_suites --run [extra pytest args]
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parents[1] / "tests"
_MANIFEST = _TESTS_DIR / "ci-suites.txt"


def _suite_dirs() -> set[str]:
    """Every directory under tests/ that actually holds test modules."""
    return {
        entry.name
        for entry in _TESTS_DIR.iterdir()
        if entry.is_dir()
        and entry.name != "__pycache__"
        and any(entry.glob("test_*.py"))
    }


def _parse_manifest() -> tuple[list[str], set[str]]:
    """Return `(run_order, excluded)`. Run order is preserved as written."""
    if not _MANIFEST.exists():
        raise SystemExit(f"manifest missing: {_MANIFEST}")
    run: list[str] = []
    excluded: set[str] = set()
    section = ""
    for raw in _MANIFEST.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section == "run":
            run.append(line)
        elif section == "excluded":
            excluded.add(line)
        else:
            raise SystemExit(f"entry outside any section in {_MANIFEST.name}: {line!r}")
    return run, excluded


def _check() -> int:
    on_disk = _suite_dirs()
    run, excluded = _parse_manifest()
    listed = set(run) | excluded

    unaccounted = sorted(on_disk - listed)
    phantom = sorted(listed - on_disk)
    duplicated = sorted(set(run) & excluded)

    for name in unaccounted:
        print(
            f"unaccounted suite: tests/{name} is in neither [run] nor [excluded]. "
            "Add it to [run] once it passes, or to [excluded] with a reason.",
            file=sys.stderr,
        )
    for name in phantom:
        print(f"stale manifest entry: tests/{name} does not exist", file=sys.stderr)
    for name in duplicated:
        print(f"listed twice: tests/{name} is in both sections", file=sys.stderr)

    if unaccounted or phantom or duplicated:
        return 1
    print(f"[ok] {len(on_disk)} suite(s) accounted for — {len(run)} run, {len(excluded)} excluded")
    return 0


def _run(extra: list[str]) -> int:
    rc = _check()
    if rc:
        return rc
    run, _ = _parse_manifest()
    if not run:
        print("manifest [run] section is empty", file=sys.stderr)
        return 1
    cmd = [sys.executable, "-m", "pytest", *[f"tests/{name}" for name in run], *extra]
    print(" ".join(cmd))
    return subprocess.call(cmd, cwd=_TESTS_DIR.parent)


def main(argv: list[str]) -> int:
    if not argv or argv[0] == "--check":
        return _check()
    if argv[0] == "--run":
        return _run(argv[1:])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
