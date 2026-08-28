@echo off
setlocal
cd /d "%~dp0"

title NovelAgent Workbench

set "NOVELAGENT_PYTHON="
where py >nul 2>nul
if not errorlevel 1 set "NOVELAGENT_PYTHON=py -3"

if not defined NOVELAGENT_PYTHON (
  where python >nul 2>nul
  if not errorlevel 1 set "NOVELAGENT_PYTHON=python"
)

if not defined NOVELAGENT_PYTHON (
  echo Python 3 was not found.
  echo Install Python 3.12 or newer, then run this launcher again.
  pause
  exit /b 1
)

%NOVELAGENT_PYTHON% run_workbench.py %*
set "NOVELAGENT_EXIT=%errorlevel%"

if not "%NOVELAGENT_EXIT%"=="0" (
  echo.
  echo NovelAgent Workbench failed to start.
  echo Run: python -m pip install -r requirements.txt
  pause
)

endlocal
exit /b %NOVELAGENT_EXIT%
