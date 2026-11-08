@echo off
setlocal
rem Runs every check from the repository root, wherever this file is launched from.
cd /d "%~dp0"
if not exist "microtask_monitor\sync_reports.py" (
  echo ERROR: run-windows-checks.bat must stay in the microtask-autopilot repository root.
  pause
  exit /b 1
)
if not exist data mkdir data
set "LOG=%~dp0data\windows-check.log"
where py >nul 2>nul
if errorlevel 1 (
  echo ERROR: The Python launcher "py" was not found. Install Python 3.12 from python.org with "py launcher" ticked.
  pause
  exit /b 1
)
echo Repository: %CD% > "%LOG%"
git rev-parse --abbrev-ref HEAD >> "%LOG%" 2>&1
git status --short >> "%LOG%" 2>&1
echo. >> "%LOG%"
echo === 1/3 Test suite === >> "%LOG%"
py -3 -m unittest discover -s tests -v >> "%LOG%" 2>&1
echo Test exit code: %errorlevel% >> "%LOG%"
echo. >> "%LOG%"
echo === 2/3 First sync === >> "%LOG%"
py -3 microtask_monitor\sync_reports.py >> "%LOG%" 2>&1
echo Sync exit code: %errorlevel% >> "%LOG%"
echo. >> "%LOG%"
echo === 3/3 Repeat sync (must report already imported, no duplicates) === >> "%LOG%"
py -3 microtask_monitor\sync_reports.py >> "%LOG%" 2>&1
echo Repeat sync exit code: %errorlevel% >> "%LOG%"
if exist data\latest_report.json (echo latest_report.json: present >> "%LOG%") else (echo latest_report.json: MISSING >> "%LOG%")
if exist data\autopilot.sqlite3 (echo autopilot.sqlite3: present >> "%LOG%") else (echo autopilot.sqlite3: MISSING >> "%LOG%")
type "%LOG%"
echo.
echo Full log saved to: %LOG%
echo Copy everything above and paste it to Claude.
pause
endlocal
