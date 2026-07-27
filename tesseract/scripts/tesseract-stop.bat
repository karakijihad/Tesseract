@echo off
REM tesseract-stop — operator clean-stop CLI.
REM
REM Writes the operator_quit intent and signals the running
REM supervisor. The supervisor honors the intent, propagates the stop
REM to the Mirror backend, and exits zero. Use from any cmd window
REM (you don't have to be in the supervisor's terminal).

setlocal
cd /d %~dp0\..\..
python -m tesseract.scripts.shutdown %*
endlocal
