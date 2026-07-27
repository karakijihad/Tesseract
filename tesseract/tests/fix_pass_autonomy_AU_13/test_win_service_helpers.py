"""AU-13 — Windows service shim helpers.

The pywin32-dependent service class is only importable on Windows + with
pywin32 installed; tests target the platform-neutral helpers and the
import surface. The full SCM install/start/stop cycle is exercised by the
operator-attended integration smoke recorded in
``Docs/Plan/autonomy/audits/AU-13.md``.
"""

from __future__ import annotations

import sys

import pytest

from tesseract.supervisor import win_service
from tesseract.supervisor.intent import IntentFile, intent_path, runtime_dir


def test_module_constants_match_phase_doc() -> None:
    assert win_service.SERVICE_NAME == "TesseractSupervisor"
    assert win_service.SERVICE_DISPLAY_NAME == "Tesseract Supervisor"


def test_read_supervisor_pid_returns_none_when_missing(tmp_path) -> None:
    assert win_service._read_supervisor_pid(tmp_path) is None


def test_read_supervisor_pid_happy_path(tmp_path) -> None:
    runtime = runtime_dir(tmp_path)
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "supervisor.pid").write_text("12345\n", encoding="utf-8")
    assert win_service._read_supervisor_pid(tmp_path) == 12345


def test_read_supervisor_pid_returns_none_on_garbage(tmp_path) -> None:
    runtime = runtime_dir(tmp_path)
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "supervisor.pid").write_text("not-a-number", encoding="utf-8")
    assert win_service._read_supervisor_pid(tmp_path) is None


def test_write_operator_quit_intent_lands_with_correct_shape(tmp_path) -> None:
    runtime = runtime_dir(tmp_path)
    runtime.mkdir(parents=True, exist_ok=True)
    win_service._write_operator_quit_intent(tmp_path, reason="unit test")
    path = intent_path(tmp_path)
    assert path.exists()
    written = IntentFile.model_validate_json(path.read_text(encoding="utf-8"))
    assert written.intent == "operator_quit"
    assert written.source == "cli_tool"
    assert written.reason == "unit test"


@pytest.mark.skipif(sys.platform != "win32", reason="pywin32-dependent service class")
def test_service_class_exposes_pywin32_contract() -> None:
    cls = win_service.TesseractSupervisorService
    assert cls is not None
    assert cls._svc_name_ == "TesseractSupervisor"
    assert cls._svc_display_name_ == "Tesseract Supervisor"
    # pywin32's ServiceFramework contract — both must be defined.
    assert hasattr(cls, "SvcDoRun")
    assert hasattr(cls, "SvcStop")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only main() refusal")
def test_main_refuses_on_non_windows(capsys) -> None:
    rc = win_service.main([])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Windows-only" in err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only installer refusal")
def test_installer_refuses_on_non_windows(capsys) -> None:
    from tesseract.scripts import install_supervisor_service

    rc = install_supervisor_service.main([])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Windows-only" in err


def test_stop_grace_exceeds_supervisor_backend_grace() -> None:
    """Race-mitigation invariant: the service's wait budget must sit
    above the supervisor's own _GRACEFUL_STOP_GRACE_S so the backend's
    full drain window can complete before the service force-terminates."""
    from tesseract.supervisor import daemon

    assert win_service._STOP_GRACE_S > daemon._GRACEFUL_STOP_GRACE_S
