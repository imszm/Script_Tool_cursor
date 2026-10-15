@echo off
setlocal
rem ASCII only. See scripts\run_station.bat for why Chinese text is not used here.
>nul 2>&1 chcp 65001
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
title ppx-testkit setup

rem Create .venv in the repo root and install the package in editable mode.
cd /d "%~dp0.."

where py >nul 2>&1
if %ERRORLEVEL% equ 0 (
    set "PY=py -3.11"
) else (
    set "PY=python"
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating virtual environment .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create the virtual environment. Install Python 3.11 or newer.
        goto :end
    )
)

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

:end
echo.
pause
endlocal
