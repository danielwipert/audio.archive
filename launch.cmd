@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Audio Archive is not set up yet.
  echo Run scripts\setup.ps1 once, then launch this file again.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m audio_archive doctor
if errorlevel 1 (
  echo.
  echo Audio Archive is not ready. Review the diagnostics above.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m audio_archive serve
if errorlevel 1 (
  echo.
  echo Audio Archive stopped with an error.
  pause
  exit /b 1
)
