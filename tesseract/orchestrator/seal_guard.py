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

import os
import tempfile
from pathlib import Path

from tesseract.paths import app_dir, runtime_dir


import logging

log = logging.getLogger(__name__)


class SealViolation(RuntimeError):
    """Raised when a subprocess would start inside a sealed tree."""


def _scratch_cwd() -> Path:
    """A writable directory outside the seal, for when the workshop is not.

    Never `home_dir()` itself, which is what this replaced: the state root
    sits directly above `memory-store/`, `vault/` and `config/`, and keeping
    a CLI away from those is the entire reason the workshop is the fallback.

    One directory per PROCESS, not per call. The workshop being unusable is
    normally a standing condition — it exists as a file, or the permission is
    wrong — so a fresh `mkdtemp` each time would leave another empty
    directory in the system temp folder on every delegation, forever, with
    nothing reaping them.

    **Every candidate is checked, including the last one.** An earlier version
    called the temp root "outside the install by construction" and returned it
    unchecked, which is the same assumption this whole module exists to
    refuse: `tempfile.gettempdir()` reads `TMPDIR`/`TEMP`/`TMP`, so it is an
    environment variable, not a fact. Nothing in this repo points it into the
    install, and the default Windows temp folder is a sibling of
    the install root rather than a child, so this is narrow. It is also one
    line to check, and an unchecked fallback inside a guard is how the bare
    state root survived here in the first place.

    Raises `SealViolation` when every candidate is sealed. That is not a
    fallback failing, it is the machine having nowhere safe to run a CLI, and
    a caller that starts one anyway is worse off than one that stops.
    """
    root = Path(tempfile.gettempdir())
    for candidate in (root / f"tesseract-cwd-{os.getpid()}", root):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            assert_cwd_outside_seal(candidate)
        except (OSError, SealViolation):
            continue
        return candidate
    raise SealViolation(
        f"no working directory outside the sealed tree is available: the "
        f"workshop under {os.fspath(root)!r}'s sibling state root could not "
        f"be used and the temp root itself resolves inside the seal. Check "
        f"whether TEMP/TMP/TMPDIR points into the installation."
    )


def safe_cwd(preferred: str | Path) -> Path:
    """`preferred` if it is outside the seal, otherwise the workshop.

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
            # Checked, not assumed. `mkdir(exist_ok=True)` succeeds on an
            # existing directory symlink, so a `workshop` junction pointing
            # into the sealed tree would otherwise be handed straight back as
            # the safe answer.
            assert_cwd_outside_seal(fallback)
        except (OSError, SealViolation):
            fallback = _scratch_cwd()
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
