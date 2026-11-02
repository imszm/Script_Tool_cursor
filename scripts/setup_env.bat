@echo off
setlocal EnableExtensions
rem ASCII only. See scripts\run_station.bat for why Chinese text is not used here.
>nul 2>&1 chcp 65001
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
title ppx-testkit setup
set "RC=0"

rem Create .venv in the repo root and install the package in editable mode.
cd /d "%~dp0.."

call :resolve_python
if not defined PY goto :no_python

echo Using %PY%
%PY% -c "import sys; print(sys.version)"

if exist ".venv\Scripts\python.exe" goto :check_venv

echo [1/3] Creating virtual environment .venv ...
%PY% -m venv .venv
if errorlevel 1 (
    echo [ERROR] Failed to create the virtual environment.
    set "RC=1"
    goto :end
)
goto :install

:check_venv
".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] This .venv cannot run. It is older than Python 3.11, or it was copied from another computer.
    echo Delete the .venv folder next to pyproject.toml, then run scripts\setup_env.bat again.
    set "RC=1"
    goto :end
)

:install
echo [2/3] Upgrading pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :fail

echo [3/3] Installing ppx-testkit and dependencies ...
".venv\Scripts\python.exe" -m pip install -e ".[win,gui,report,dev]"
if errorlevel 1 goto :fail

if not exist "config\local.yaml" (
    copy /y "config\local.yaml.example" "config\local.yaml" >nul
    echo Created config\local.yaml. Edit the COM ports for this PC before running hardware.
)

echo.
echo Setup finished.
echo List stations:  scripts\run_station.bat
echo List ports:     .venv\Scripts\python.exe -m ppx_testkit ports
echo First hardware try, limit cycles. Do not start relay_power_cycle with its default 200000 cycles:
echo   scripts\run_station.bat relay_power_cycle --set test.cycles=2 --port relay=COM3 --port device=COM13
goto :end

:fail
echo [ERROR] Dependency installation failed. Check the network or the pip index.
set "RC=1"
goto :end

:no_python
echo [ERROR] Python 3.11 or newer was not found.
echo start_ui.bat and run_station.bat need the .venv that this script creates.
echo Python 3.8, 3.9, and 3.10 cannot run this project.
echo.
echo Install Python 3.11 or any newer 3.x from:
echo   https://www.python.org/downloads/windows/
echo In the installer, turn on the py launcher and "Add python.exe to PATH".
echo Close this window, open a new one, and run scripts\setup_env.bat again.
echo Do not copy a .venv folder from another computer.
echo.
echo Interpreters visible on this PC:
where py >nul 2>&1 && py -0p
where python >nul 2>&1 && python --version
set "RC=1"

:end
echo.
pause
endlocal & exit /b %RC%

:resolve_python
rem py -3 is the newest Python 3. A machine with only 3.12+ has no "3.11" tag.
set "PY="
where py >nul 2>&1
if errorlevel 1 goto :resolve_python_exe
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 goto :resolve_python_exe
set "PY=py -3"
exit /b 0

:resolve_python_exe
where python >nul 2>&1
if errorlevel 1 exit /b 1
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 exit /b 1
set "PY=python"
exit /b 0
