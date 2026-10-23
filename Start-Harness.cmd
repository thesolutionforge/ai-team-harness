@echo off
rem Double-click this file to start the AI team dashboard.
rem It runs the local server (127.0.0.1 only) and opens the page in your browser.
cd /d "%~dp0"
echo Starting the AI team dashboard at http://127.0.0.1:8765
echo Close this window (or press Ctrl+C) to stop it.
python dashboard.py --open
pause