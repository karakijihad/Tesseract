@echo off
REM tesseract-status — report supervisor state without starting anything.

setlocal
cd /d %~dp0\..\..
python -m tesseract.supervisor --status
endlocal
