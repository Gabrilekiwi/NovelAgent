@echo off
setlocal
cd /d "%~dp0"

title NovelAgent Workbench
set "PYTHONUTF8=1"

set "NOVELAGENT_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%NOVELAGENT_PYTHON%" (
  echo The project environment is missing.
  echo Install Python 3.12, then run: py -3.12 scripts\setup_environment.py
  pause
  exit /b 1
)

"%NOVELAGENT_PYTHON%" -B scripts\check_environment.py
if errorlevel 1 (
  echo Run: py -3.12 scripts\setup_environment.py
  pause
  exit /b 1
)

"%NOVELAGENT_PYTHON%" run_workbench.py %*
set "NOVELAGENT_EXIT=%errorlevel%"

if not "%NOVELAGENT_EXIT%"=="0" (
  echo.
  echo NovelAgent Workbench failed to start.
  echo Environment setup: py -3.12 scripts\setup_environment.py
  pause
)

endlocal & exit /b %NOVELAGENT_EXIT%
