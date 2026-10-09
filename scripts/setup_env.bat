@echo off
setlocal
chcp 65001 > nul
title ppx-testkit 环境初始化

:: 在项目根目录创建 .venv 并以可编辑模式安装全部依赖
cd /d "%~dp0.."

where py > nul 2>&1
if %ERRORLEVEL% equ 0 (
    set "PY=py -3.11"
) else (
    set "PY=python"
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] 创建虚拟环境 .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] 创建虚拟环境失败，请确认已安装 Python 3.11+
        goto :end
    )
)

echo [2/3] 升级 pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :fail

echo [3/3] 安装 ppx-testkit 及依赖 ...
".venv\Scripts\python.exe" -m pip install -e ".[win,gui,report,dev]"
if errorlevel 1 goto :fail

if not exist "config\local.yaml" (
    copy /y "config\local.yaml.example" "config\local.yaml" > nul
    echo 已生成 config\local.yaml，请按本机串口号修改。
)

echo.
echo 安装完成。运行示例：scripts\run_station.bat relay_power_cycle
goto :end

:fail
echo [ERROR] 依赖安装失败，请检查网络或 pip 源配置。

:end
echo.
pause
endlocal
