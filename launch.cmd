@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Audio Archive is not set up yet.
  echo Run scripts\setup.ps1 once, then launch this file again.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m audio_archive init
if errorlevel 1 (
  echo Audio Archive could not initialize.
  pause
  exit /b 1
)

rem The local browser server will replace this CLI handoff in the GUI build slice.
".venv\Scripts\python.exe" -m audio_archive list
pause

