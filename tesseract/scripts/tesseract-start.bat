@echo off
REM tesseract-start — one-command launch for TESSERACT.
REM
REM Starts the AU-1 supervisor, which then spawns the Mirror backend
REM in its own console window. Operator sees TWO terminal windows:
REM   * this one (supervisor)        — heartbeat, intent routing, restarts
REM   * a new Mirror console window  — aiohttp server + scheduler + bridges
REM
REM Stop with Ctrl-C in this window (the operator-quit path), or run
REM tesseract-stop.bat from any cmd. Either way: supervisor honors
REM intent → propagates to Mirror → both exit cleanly.

setlocal
cd /d %~dp0\..\..
title TESSERACT Supervisor
echo TESSERACT Supervisor — Ctrl-C to stop everything cleanly.
echo Backend window opens shortly...
echo.
python -m tesseract.supervisor %*
endlocal
