from __future__ import annotations

from pathlib import PurePosixPath

# Any target path under this prefix must NOT run as an in-backend headless
# delegate — editing it can bounce the Mirror backend (Vite HMR / watcher),
# killing the in-process job mid-flight. Route to a controller terminal
# session instead.
_TERMINAL_REQUIRED_PREFIX = "tesseract/mirror/"

HANDOFF_REDIRECT_MESSAGE = (
    "This task targets tesseract/mirror/** — editing it can restart the "
    "backend and kill an in-process job. Do NOT run it headless. Call "
    "start_controller_session(task=..., launch_terminal=True) so the work "
    "runs in an out-of-process controller terminal the operator can watch "
    "and that survives a backend restart."
)


def requires_terminal(target_paths: list[str] | None) -> bool:
    """True if any declared target path is under tesseract/mirror/**.

    Paths are normalised to forward slashes so Windows-style inputs match.
    """
    if not target_paths:
        return False
    for raw in target_paths:
        norm = PurePosixPath(str(raw).replace("\\", "/")).as_posix()
        if norm.startswith(_TERMINAL_REQUIRED_PREFIX):
            return True
    return False
