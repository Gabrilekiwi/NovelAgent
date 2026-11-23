@echo off
setlocal
cd /d "%~dp0"

REM Official start: if workbench/core sources differ from HEAD, run_workbench.py
REM serves committed HEAD UI from a sibling official worktree, not dirty overlays.

title NovelAgent Workbench
set "PYTHONUTF8=1"

set "NOVELAGENT_PYTHON=%~dp0.venv\Scripts\python.exe"
if exist "%NOVELAGENT_PYTHON%" goto check_environment
echo First launch: setting up NovelAgent. Internet access or an offline package source is required.
py -3.12 -V >nul 2>&1
if errorlevel 1 goto try_python
py -3.12 scripts\setup_environment.py --runtime-only
if errorlevel 1 goto setup_failed
goto check_environment
:try_python
python scripts\setup_environment.py --runtime-only
if errorlevel 1 goto setup_failed
:check_environment
"%NOVELAGENT_PYTHON%" -B scripts\check_environment.py
if errorlevel 1 (
  echo Repair setup: py -3.12 scripts\setup_environment.py --runtime-only
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

:setup_failed
echo Setup did not finish. Check Python 3.12 and your package source.
echo Retry: py -3.12 scripts\setup_environment.py --runtime-only
pause
endlocal & exit /b 1
