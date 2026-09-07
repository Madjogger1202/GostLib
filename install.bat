@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ==========================================================
echo   GostLib - installation
echo ==========================================================
echo.

set "PYEXE="
py -3 --version >nul 2>&1 && set "PYEXE=py -3"
if not defined PYEXE (
    python --version >nul 2>&1 && set "PYEXE=python"
)
if not defined PYEXE (
    echo [!] Python not found.
    echo     Install Python 3.10+ from https://www.python.org/downloads/
    echo     and tick "Add python.exe to PATH".
    pause
    exit /b 1
)

echo [1/4] Python:
%PYEXE% --version

if not exist ".venv\Scripts\python.exe" (
    echo [2/4] Creating virtual environment .venv ...
    %PYEXE% -m venv .venv
    if errorlevel 1 (
        echo [!] Could not create .venv
        pause
        exit /b 1
    )
) else (
    echo [2/4] .venv already exists
)

echo [3/4] Installing dependencies ^(PySide6, olefile^) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [!] Dependency install failed - check internet/proxy.
    pause
    exit /b 1
)

echo [4/4] Preparing work folder and the Altium script ...
chcp 65001 >nul
".venv\Scripts\python.exe" -m gostlib.setup_env

echo.
echo Done. Start the app with run.bat
echo.
pause
