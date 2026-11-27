@echo off
setlocal
echo Starting Monaw Agent...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-windows.ps1"
set "START_EXIT=%ERRORLEVEL%"

if not "%START_EXIT%"=="0" (
  echo.
  echo [ERROR] Monaw Agent failed to start. Exit code: %START_EXIT%
  echo See the error and log paths above.
  echo Setup and troubleshooting guide: docs\source-setup.md
  echo.
  pause
)

exit /b %START_EXIT%
