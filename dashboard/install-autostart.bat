@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $python=(Get-Command py -ErrorAction Stop).Source; $script=Join-Path '%~dp0..' 'microtask_monitor\local_server.py'; $action=New-ScheduledTaskAction -Execute $python -Argument ('-3 ""'+$script+'""'); $trigger=New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; $settings=New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero); Register-ScheduledTask -TaskName 'MicrotaskAutopilotLocalAgent' -Action $action -Trigger $trigger -Settings $settings -Description 'Start local Microtask Autopilot dashboard and free GitHub report synchronization at sign-in.' -Force | Out-Null; Write-Host 'Autostart installed for the current Windows user.'"
if errorlevel 1 (
  echo Could not install autostart. Open PowerShell and inspect the error above.
  pause
  exit /b 1
)
echo Autostart is installed. The agent will start when you sign in to Windows.
pause
endlocal
