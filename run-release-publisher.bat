@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Client development environment was not found: .venv\Scripts\python.exe
    echo Create the project virtual environment and install requirements first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m release_publisher
exit /b %ERRORLEVEL%
