"""Whether a folder can be registered as a project, when the folder is the
app's own code.

The running app must never treat its own source or install tree as a project
it files tasks under. `proj-tesseract` (root = this repo) once caused a task
close to run the whole dev test suite. Refused here, at the one place every
write to the registry passes through, rather than by asking the model to
avoid it in a prompt.

**Every property this must hold at once**, because a change made against only
the finding in front of it is how the guard drops one of the others later:

1. One function decides: `why_root_is_refused(root) -> str`, empty string
   when the root is allowed. Every caller asks it this one way.
2. A root that cannot be resolved is refused, not silently let through.
3. Refused when the resolved root equals the app's source tree (`paths.ROOT`),
   sits inside it, or contains it, and the same for an install's sealed
   `app/` and `runtime/` trees when those exist (a dev checkout has neither).
4. Allowed when the resolved root is the home tree's `workshop/` folder, or a
   folder inside it, even though in a dev checkout that folder sits inside
   the app's own source tree. Checked BEFORE the refusal in (3), because the
   two overlap on purpose in that one shape and nothing else inside the
   source tree is allowed, home itself included.
5. Every path is resolved at call time. `paths.home_dir()` and
   `paths.install_root()` already read `TESSERACT_HOME` at call time; nothing
   here may cache what they return across calls, or a test that redirects the
   env after import would keep reading the first answer.
"""

from __future__ import annotations

from pathlib import Path

from tesseract import paths


def _resolve(path: Path) -> Path | None:
    try:
        return path.resolve()
    except (OSError, ValueError):
        return None


def _is_link(path: Path) -> bool:
    try:
        return path.is_symlink() or path.is_junction()
    except OSError:
        return True


def _inside_workshop(root: Path) -> bool:
    """Whether `root` is the workshop folder or inside it, with no link between.

    Decided on the path as written, not as resolved: `resolve()` follows a
    symlink or a Windows junction, so a `workshop` relinked to the app's own
    code would otherwise carry that code through the one exception there is.
    Every folder from `root` up to and including `workshop` must be a real
    folder, not a link; a folder that does not exist yet (a project about to be
    created) is not a link.
    """
    import os

    # `abspath` collapses `..` on the text alone, so `workshop/<link>/..`
    # would read as `workshop` without the link ever being looked at. A path
    # written with `..` never takes this exception; the resolved checks
    # decide it instead.
    if ".." in Path(str(root)).parts:
        return False
    workshop = Path(os.path.abspath(paths.home_dir() / "workshop"))
    written = Path(os.path.abspath(root))
    if written != workshop and workshop not in written.parents:
        return False
    step = written
    while True:
        if _is_link(step):
            return False
        if step == workshop:
            return True
        step = step.parent


def _overlaps(resolved: Path, tree: Path) -> bool:
    """True when `resolved` and `tree` are the same folder, or one sits
    inside the other, in either direction."""
    return resolved == tree or tree in resolved.parents or resolved in tree.parents


def why_root_is_refused(root: Path | str) -> str:
    """Why `root` cannot be registered or activated as a project, or "" when
    it can.

    Resolved fresh on every call: `paths.ROOT` is a source-tree constant that
    never moves, but `paths.home_dir()` and `paths.install_root()` follow
    `TESSERACT_HOME`, which a test or an install can change between calls.
    """
    resolved = _resolve(Path(root))
    if resolved is None:
        return (
            f"{root} could not be checked, so it cannot be a project. "
            "Try a different folder or check that the path is correct."
        )

    if _inside_workshop(Path(root)):
        return ""

    own_root = _resolve(paths.ROOT)
    if own_root is not None and _overlaps(resolved, own_root):
        return _refused_sentence(resolved)

    for tree_fn in (paths.app_dir, paths.runtime_dir):
        tree = _resolve(tree_fn())
        if tree is None or not tree.exists():
            continue
        if _overlaps(resolved, tree):
            return _refused_sentence(resolved)

    return ""


def _refused_sentence(resolved: Path) -> str:
    return (
        f"{resolved} is the app's own code, so it cannot be a project. "
        "Work on your own projects lives in the workshop folder or anywhere "
        "outside the app."
    )


__all__ = ["why_root_is_refused"]
