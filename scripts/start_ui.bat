@echo off
setlocal
rem ASCII only. cmd reads this file with the system code page.
>nul 2>&1 chcp 65001
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

rem Open the local web console in the browser. Tests still run via ppx-test.
cd /d "%~dp0.."

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] .venv not found. Run scripts\setup_env.bat first.
    goto :end
)

title ppx-test ui
"%PY%" -m ppx_testkit ui
set "RC=%ERRORLEVEL%"
echo.
echo Web console stopped. exit code %RC%

:end
echo.
pause
endlocal & exit /b %RC%
