@echo off
setlocal
title Monaw Agent Setup
echo Setting up Monaw Agent...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup-windows.ps1"
set "SETUP_EXIT=%ERRORLEVEL%"
echo.
if "%SETUP_EXIT%"=="0" (
  echo Monaw Agent setup completed successfully.
) else (
  echo Monaw Agent setup failed. Check the message above.
)
echo.
pause
exit /b %SETUP_EXIT%
