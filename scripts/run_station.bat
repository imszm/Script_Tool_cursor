@echo off
setlocal
chcp 65001 > nul

:: 用法：run_station.bat <工位名> [ppx-test run 的其它参数，如 --port relay=COM5]
:: 不带参数时列出全部工位。
:: 程序本身会在 logs\<工位>\<时间戳>\ 下生成 full.log / error.log / device_raw.log；
:: 此外本脚本把控制台输出完整保存到 logs\console_<工位>_<时间戳>.log，用于排查启动阶段的问题。
cd /d "%~dp0.."

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] 未找到 .venv，请先运行 scripts\setup_env.bat
    goto :end
)

if "%~1"=="" (
    "%PY%" -m ppx_testkit list
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
    echo [结果] 测试通过
) else if "%RC%"=="1" (
    echo [结果] 测试未通过，详见日志
) else if "%RC%"=="2" (
    echo [结果] 配置错误
) else if "%RC%"=="3" (
    echo [结果] 硬件/通信故障
) else if "%RC%"=="130" (
    echo [结果] 用户中断
) else (
    echo [结果] 退出码 %RC%
)
echo 控制台日志: %CONSOLE_LOG%

:end
echo.
pause
endlocal & exit /b %RC%
