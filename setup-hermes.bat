@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title Hermes Agent Setup (Windows)

:: ==============================
:: 颜色定义
:: ==============================
set "GREEN=[OK]"
set "YELLOW=[WARN]"
set "RED=[ERROR]"
set "CYAN=[INFO]"

:: 脚本目录
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"
set "PYTHON_VERSION=3.11"

echo.
echo ==============================
echo  Hermes Agent 安装程序 (Windows)
echo ==============================
echo.

:: ==============================
:: 1. 安装 uv
:: ==============================
echo %CYAN% 检查 uv 包管理器...
where uv >nul 2>nul
if %errorlevel% equ 0 (
    echo %GREEN% uv 已安装
    set "UV_CMD=uv"
) else (
    echo %CYAN% 正在安装 uv...
    powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
    where uv >nul 2>nul
    if %errorlevel% equ 0 (
        set "UV_CMD=uv"
        echo %GREEN% uv 安装成功
    ) else (
        echo %RED% uv 安装失败
        pause
        exit /b 1
    )
)

:: ==============================
:: 2. 安装 Python
:: ==============================
echo %CYAN% 检查 Python %PYTHON_VERSION%...
%UV_CMD% python find %PYTHON_VERSION% >nul 2>&1
if %errorlevel% neq 0 (
    %UV_CMD% python install %PYTHON_VERSION%
)
echo %GREEN% Python 就绪

:: ==============================
:: 3. 重建虚拟环境
:: ==============================
echo %CYAN% 创建虚拟环境...
if exist venv rmdir /s /q venv
python -m venv venv
echo %UV_CMD% venv venv --python %PYTHON_VERSION%

set "PYTHON_EXE=%SCRIPT_DIR%venv\Scripts\python.exe"
set "PIP_EXE=%SCRIPT_DIR%venv\Scripts\pip.exe"

:: ==============================
:: 4. 强制安装完整依赖（修复版）
:: ==============================
echo %CYAN% 安装项目依赖...
%PIP_EXE% install --upgrade pip setuptools wheel
%PIP_EXE% install -e "." -e ".[all]" pyyaml python-dotenv typer click rich

echo %GREEN% 依赖安装完成

:: ==============================
:: 5. 子模块
:: ==============================
echo %CYAN% 检查子模块...
if exist "tinker-atropos\pyproject.toml" (
    %PIP_EXE% install -e "./tinker-atropos"
)

:: ==============================
:: 6. ripgrep
:: ==============================
echo %CYAN% 检查 ripgrep...
where rg >nul 2>nul
if %errorlevel% neq 0 (
    echo %YELLOW% 未安装 ripgrep，不影响核心使用
)

:: ==============================
:: 7. .env
:: ==============================
if not exist .env (
    if exist .env.example copy .env.example .env
)

:: ==============================
:: 8. 全局 hermes 命令
:: ==============================
echo %CYAN% 创建全局命令...
set "TARGET=%USERPROFILE%\AppData\Local\Microsoft\WindowsApps\hermes.bat"
echo @echo off > "%TARGET%"
echo "%SCRIPT_DIR%venv\Scripts\hermes.exe" %%* >> "%TARGET%"

:: ==============================
:: 9. 同步技能
:: ==============================
echo %CYAN% 同步技能...
set "SKILLS_DIR=%USERPROFILE%\.hermes\skills"
mkdir "%SKILLS_DIR%" 2>nul
"%PYTHON_EXE%" "%SCRIPT_DIR%tools\skills_sync.py" 2>nul

:: ==============================
:: 完成
:: ==============================
echo.
echo ==============================================
echo %GREEN% ✅ 安装完成！
echo ==============================================
echo.
echo 立即使用：
echo hermes setup    配置向导
echo hermes         启动助手
echo.

set /p "RUN=现在启动配置向导？[Y/n] "
if /i "!RUN!"=="Y" (
    "%PYTHON_EXE%" -m hermes_cli.main setup
)

pause