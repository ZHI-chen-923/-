@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 每日时事新闻汇报

rem ---------- 检查 Python ----------
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 Python。请先安装 Python 3.9 或更高版本：
    echo   https://www.python.org/downloads/
    echo 安装时请勾选 "Add python.exe to PATH"。
    pause
    exit /b 1
)

rem ---------- 检查依赖，缺失则自动安装 ----------
python -c "import ttkbootstrap" >nul 2>nul
if errorlevel 1 (
    echo 首次运行，正在安装依赖（优先本地 wheels 目录，失败时联网安装）...
    python -m pip install --no-deps --find-links "%~dp0wheels" ttkbootstrap pillow
    if errorlevel 1 (
        python -m pip install --no-deps ttkbootstrap pillow
        if errorlevel 1 (
            echo [错误] 依赖安装失败，请检查网络后重新运行本脚本。
            pause
            exit /b 1
        )
    )
)

rem ---------- 启动（隐藏控制台窗口） ----------
where pythonw >nul 2>nul
if errorlevel 1 (
    start "" python main.py
) else (
    start "" pythonw main.py
)
