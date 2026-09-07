@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run install.bat first.
    pause
    exit /b 1
)
chcp 65001 >nul
".venv\Scripts\python.exe" -m gostlib.cli %*
