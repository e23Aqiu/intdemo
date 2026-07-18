@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py
) else (
    echo 未找到项目虚拟环境，将使用系统 Python。
    echo 推荐先执行: py -3.9 -m venv .venv
    python main.py
)

if errorlevel 1 pause

