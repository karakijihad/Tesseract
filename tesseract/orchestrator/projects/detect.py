"""Read a directory and describe what it is — git identity and how it verifies.

Detection is a *suggestion*, never an assertion. Every command it returns is
offered to the operator for confirmation and is executed through the normal
bash policy path when the gate eventually runs it, so a wrong guess here is
visible and refusable rather than silently authoritative.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from .models import VcsInfo, VerifyCommands

log = logging.getLogger(__name__)

# git can block on credential prompts or a slow network remote. Every call here
# is local metadata, so a hang means something is wrong rather than slow.
_GIT_TIMEOUT_S = 10

_CONVENTION_FILES = ("AGENTS.md", "CLAUDE.md", "CONVENTIONS.md")


def _git(args: list[str], *, cwd: Path) -> str | None:
    """Captured stdout of a local git query, or ``None`` on any failure.

    Detection never raises: a directory that is not a repo, a git that is not
    installed, and a repo that is broken all mean the same thing to the caller
    — no git identity to record.
    """
    try:
        result = subprocess.run(  # noqa: S603 — args list, no shell
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        log.debug("project detect: git %s failed in %s", args, cwd, exc_info=True)
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def detect_vcs(root: Path) -> VcsInfo:
    """Git identity of ``root``.

    A directory *inside* a repo does not count as that repo: the toplevel must
    resolve to ``root`` itself, or registering a subdirectory would inherit the
    parent's remote and default branch and report them as its own.
    """
    toplevel = _git(["rev-parse", "--show-toplevel"], cwd=root)
    if toplevel is None:
        return VcsInfo(git=False)
    try:
        same = Path(toplevel).resolve() == Path(root).resolve()
    except OSError:
        same = False
    if not same:
        return VcsInfo(git=False)
    return VcsInfo(
        git=True,
        remote=_git(["remote", "get-url", "origin"], cwd=root),
        default_branch=_default_branch(root),
    )


def _default_branch(root: Path) -> str | None:
    """The repo's default branch, not whichever branch is checked out.

    ``origin/HEAD`` is the authority when the remote published it. Falling back
    to the current branch is a guess, and on a feature branch it is the wrong
    one — but a wrong-but-present answer is what the operator can see and
    correct, and a clone with no ``origin/HEAD`` has nothing better.
    """
    ref = _git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], cwd=root)
    if ref:
        return ref.rsplit("/", 1)[-1]
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)


def _node_runner(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm run"
    if (root / "yarn.lock").exists():
        return "yarn"
    return "npm run"


def _from_package_json(root: Path) -> dict[str, str]:
    path = root / "package.json"
    if not path.exists():
        return {}
    try:
        scripts = json.loads(path.read_text(encoding="utf-8")).get("scripts") or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        log.debug("project detect: package.json unreadable at %s", path, exc_info=True)
        return {}
    if not isinstance(scripts, dict):
        return {}
    runner = _node_runner(root)
    out: dict[str, str] = {}
    for slot, names in (
        ("test", ("test",)),
        ("typecheck", ("typecheck", "type-check", "tsc")),
        ("lint", ("lint",)),
    ):
        hit = next((n for n in names if n in scripts), None)
        if hit is not None:
            out[slot] = f"{runner} {hit}"
    if "typecheck" not in out and (root / "tsconfig.json").exists():
        out["typecheck"] = "npx tsc --noEmit"
    return out


def _from_pyproject(root: Path) -> dict[str, str]:
    path = root / "pyproject.toml"
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {"test": "pytest -q"}
    if "ruff" in text:
        out["lint"] = "ruff check ."
    if "mypy" in text:
        out["typecheck"] = "mypy ."
    return out


def detect_verify(root: Path) -> VerifyCommands:
    """Verification commands suggested by what is in the tree.

    Node wins over Python when both are present: a repo carrying both usually
    drives the whole thing from ``package.json`` scripts, and the operator can
    override either way.
    """
    found: dict[str, str] = {}
    if (root / "Cargo.toml").exists():
        found = {"test": "cargo test", "lint": "cargo clippy"}
    elif (root / "go.mod").exists():
        found = {"test": "go test ./...", "lint": "go vet ./..."}
    else:
        found = {**_from_pyproject(root), **_from_package_json(root)}
    return VerifyCommands(
        test=found.get("test"),
        typecheck=found.get("typecheck"),
        lint=found.get("lint"),
    )


def detect_conventions_file(root: Path) -> str | None:
    """The cross-tool conventions file already in the tree, if any."""
    return next((n for n in _CONVENTION_FILES if (root / n).exists()), None)


__all__ = [
    "detect_conventions_file",
    "detect_verify",
    "detect_vcs",
]
