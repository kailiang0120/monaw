@echo off
setlocal
echo Starting Monaw Agent...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-windows.ps1"
set "START_EXIT=%ERRORLEVEL%"

if not "%START_EXIT%"=="0" (
  echo.
  echo [ERROR] Monaw Agent failed to start. Exit code: %START_EXIT%
  echo Check launcher logs in:
  echo   %USERPROFILE%\.monaw\runtime\launcher\
  echo.
  pause
)

exit /b %START_EXIT%
