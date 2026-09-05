@echo off
REM tesseract-stop — operator clean-stop CLI.
REM
REM Writes the operator_quit intent, asks the running supervisor to stop
REM through its stop-request file, and waits for it to go. The supervisor
REM honors the intent, propagates the stop to the Mirror backend and the
REM dev server, and exits zero. Use from any cmd window (you don't have to
REM be in the supervisor's terminal).

setlocal
cd /d %~dp0\..\..
python -m tesseract.scripts.shutdown %*
endlocal
