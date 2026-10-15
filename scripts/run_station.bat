@echo off
setlocal
rem ASCII only. cmd reads this file with the system code page, so UTF-8
rem comments are executed as commands ("diamond" is not recognized).
rem Redirect chcp first; "chcp 65001 > nul" prints "file not found" on some Windows builds.
>nul 2>&1 chcp 65001
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

rem Usage: run_station.bat [station] [ppx-test run args, e.g. --port relay=COM5]
rem No arguments: list stations, then wait. A test is not started.
rem Logs: logs\<station>\<timestamp>\full.log and logs\console_<station>_<timestamp>.log
cd /d "%~dp0.."

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] .venv not found. Run scripts\setup_env.bat first.
    goto :end
)

if "%~1"=="" (
    "%PY%" -m ppx_testkit list
    echo.
    echo This window only lists stations. Pressing a key closes it and does not start a test.
    echo.
    echo Open a terminal in this folder, then run one station. Examples:
    echo   scripts\run_station.bat relay_probe --port relay=COM9
    echo   .venv\Scripts\python.exe -m ppx_testkit ports
    echo First hardware try, limit the cycle count:
    echo   scripts\run_station.bat relay_power_cycle --set test.cycles=2 --port relay=COM3 --port device=COM13
    echo Replace COM ports with the ones listed by the ports command.
    goto :end
)

set "STATION=%~1"
title ppx-test %STATION%
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "TS=%%i"
if not exist logs mkdir logs
set "CONSOLE_LOG=logs\console_%STATION%_%TS%.log"

powershell -NoProfile -Command "& '%PY%' -m ppx_testkit run %* 2>&1 | Tee-Object -FilePath '%CONSOLE_LOG%'; exit $LASTEXITCODE"
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [result] PASS
) else if "%RC%"=="1" (
    echo [result] FAIL, see the log
) else if "%RC%"=="2" (
    echo [result] configuration error
) else if "%RC%"=="3" (
    echo [result] hardware or communication error
) else if "%RC%"=="130" (
    echo [result] interrupted
) else (
    echo [result] exit code %RC%
)
echo console log: %CONSOLE_LOG%

:end
echo.
pause
endlocal & exit /b %RC%
