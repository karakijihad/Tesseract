"""Refuse to start a subprocess whose working directory is inside the seal.

`decide.evaluate` governs the assistant's own tools. It does not govern a `claude` or
`codex` process: those are spawned directly, run for minutes, and edit whatever
their cwd contains. Started inside `app/`, such a process will happily "fix the
bug" in the installed application — an edit that is not in git, that the next
update deletes without a diff, and that nobody ever reviews.

The guard is on the working directory rather than on individual writes because
that is the only moment the runtime still controls. Once the CLI is running,
nothing here can see what it does.
"""

from __future__ import annotations

from pathlib import Path

from tesseract.paths import app_dir, runtime_dir


import logging

log = logging.getLogger(__name__)


class SealViolation(RuntimeError):
    """Raised when a subprocess would start inside a sealed tree."""


def safe_cwd(preferred: str | Path) -> Path:
    """`preferred` if it is outside the seal, otherwise the state root.

    For callers that did not choose their working directory on purpose. The
    delegate tools inherit `ToolContext.workspace_root`, which IS the code tree
    — `<home>/app` in a packaged install — so every delegation would otherwise
    start inside the seal by default. Refusing outright would remove delegation
    from an installed app entirely, including the read-heavy uses that are
    perfectly legitimate; moving it to the user's own tree keeps the capability
    and takes away the write target.

    Callers who named a directory deliberately (a terminal pane) should use
    `assert_cwd_outside_seal` and surface the refusal instead — silently
    relocating someone who typed a path is worse than telling them.

    The fallback is `workshop/`, not the state root: a CLI dropped at the
    top of `home/` sits directly above `memory-store/`, `vault/` and `config/`,
    and "work in the current directory" is a normal thing for it to be asked to
    do. The workshop is the one directory that exists to be written in.
    """
    try:
        assert_cwd_outside_seal(preferred)
        return Path(preferred)
    except SealViolation:
        from tesseract.paths import home_dir

        fallback = home_dir() / "workshop"
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except OSError:  # pragma: no cover — fall back to the state root
            fallback = home_dir()
        log.warning(
            "seal: %s is inside the sealed tree — running in %s instead",
            preferred,
            fallback,
        )
        return fallback


def assert_cwd_outside_seal(cwd: str | Path) -> None:
    """Raise `SealViolation` if `cwd` resolves inside `app/` or `runtime/`.

    Paths outside the install entirely are fine: the seal protects the
    application tree, it does not confine work to the install. Working on an
    unrelated repository elsewhere on disk is explicitly in scope.
    """
    try:
        resolved = Path(cwd).resolve()
    except (OSError, ValueError) as exc:
        raise SealViolation(f"cannot resolve working directory {cwd!r}: {exc}") from exc

    for tree, label in ((app_dir(), "app"), (runtime_dir(), "runtime")):
        try:
            tree_resolved = tree.resolve()
        except OSError:
            continue
        if resolved == tree_resolved or tree_resolved in resolved.parents:
            raise SealViolation(
                f"refusing to start a process inside the sealed {label}/ tree "
                f"({resolved}). That tree is replaced wholesale by every update, "
                f"so any edit made there is destroyed silently. Start in the "
                f"home tree instead — its workshop/ folder for scratch "
                f"work — or in the development repository if the application "
                f"itself needs changing."
            )
