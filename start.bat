@echo off
echo Starting Monaw Agent...
set "CONDA_ENV_NAME=agent"

rem Try regular conda activation first (works if shell is initialized)
call conda activate %CONDA_ENV_NAME% >nul 2>&1
if errorlevel 1 (
  rem Fallback: activate via common install paths
  if exist "%USERPROFILE%\miniconda3\Scripts\activate.bat" (
    call "%USERPROFILE%\miniconda3\Scripts\activate.bat" %CONDA_ENV_NAME%
  ) else if exist "%USERPROFILE%\anaconda3\Scripts\activate.bat" (
    call "%USERPROFILE%\anaconda3\Scripts\activate.bat" %CONDA_ENV_NAME%
  ) else (
    echo [WARN] Conda activation script not found. Continuing without conda env.
  )
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-windows.ps1"
exit /b %ERRORLEVEL%
