@echo off
setlocal
cd /d "%~dp0.."
echo Starting Microtask Autopilot local agent...
start "Microtask Autopilot Agent" /min py -3 "%~dp0..\microtask_monitor\local_server.py"
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:8765"
echo.
echo The local agent will sync the latest successful GitHub report every 15 minutes.
echo Keep this window's background agent running while you want local updates.
echo Close the "Microtask Autopilot Agent" process to stop it.
endlocal
