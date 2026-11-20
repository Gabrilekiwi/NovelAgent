@echo off
setlocal
cd /d "%~dp0"
title NovelAgent Current Workbench
set "PYTHONUTF8=1"
set "NOVELAGENT_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%NOVELAGENT_PYTHON%" goto failed
"%NOVELAGENT_PYTHON%" -B scripts\check_environment.py
if errorlevel 1 goto failed
echo Starting the current workspace version, including local changes.
echo Close an older Workbench instance first to use this version.
"%NOVELAGENT_PYTHON%" -B run_workbench.py --use-working-tree %*
set "NOVELAGENT_EXIT=%errorlevel%"
if not "%NOVELAGENT_EXIT%"=="0" goto failed
endlocal & exit /b 0
:failed
echo Workbench could not start. Check the message above.
echo Environment setup: py -3.12 scripts\setup_environment.py
pause
endlocal & exit /b 1
