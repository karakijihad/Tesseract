"""``python -m tesseract.scripts.install_supervisor_service`` — Windows-only.

Thin installer over :mod:`tesseract.supervisor.win_service`. Delegates to
``pywin32``'s ``win32serviceutil.HandleCommandLine`` so the operator gets
the standard subcommand surface — ``install`` / ``update`` / ``start`` /
``stop`` / ``restart`` / ``remove`` / ``debug`` — for the ``Tesseract
Supervisor`` service.

Requires administrator rights for install / update / remove. Run from an
elevated cmd.exe::

    pip install tesseract[supervisor-service]
    python -m tesseract.scripts.install_supervisor_service install --startup auto
    net start "Tesseract Supervisor"

Stop with ``net stop "Tesseract Supervisor"`` or via ``Services.msc``.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    if sys.platform != "win32":
        print(
            "install_supervisor_service: Windows-only. The supervisor runs in "
            "the foreground on POSIX — use a systemd unit or your distro's "
            "preferred service manager.",
            file=sys.stderr,
        )
        return 2

    try:
        from tesseract.supervisor.win_service import (
            TesseractSupervisorService,
        )
    except ImportError as exc:
        print(
            "install_supervisor_service: pywin32 is not installed. "
            "Run `pip install tesseract[supervisor-service]` first.\n"
            f"  underlying error: {exc}",
            file=sys.stderr,
        )
        return 1

    import win32serviceutil  # type: ignore[import-not-found]

    win32serviceutil.HandleCommandLine(TesseractSupervisorService, argv=argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
