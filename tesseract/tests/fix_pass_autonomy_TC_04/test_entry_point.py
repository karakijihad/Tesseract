"""`python -m tesseract.scripts.tars_controller` import smoke test.

The exit-criterion check is that the module can be imported and ``main``
is callable with ``--help``. Booting the full asyncio daemon is exercised
by the live IPC tests; this guards against an import-time explosion
sneaking in undetected when the brain wiring is touched.
"""

from __future__ import annotations

import sys

import pytest


def test_module_imports() -> None:
    mod = __import__(
        "tesseract.scripts.tars_controller", fromlist=["main"]
    )
    assert callable(mod.main)


def test_main_help_exits_clean(capsys: pytest.CaptureFixture[str]) -> None:
    from tesseract.scripts.tars_controller import main

    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert "tars_controller" in (captured.out + captured.err).lower()
