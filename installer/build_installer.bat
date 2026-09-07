@echo off
rem Сборка установщика GostLib-Setup-*.exe. Все ключи прокидываются в
rem build_installer.py:  build_installer.bat --rebuild --clean
rem Файл в кодировке CP866 -- родной для консоли Windows, без BOM,
rem иначе cmd спотыкается на первой строке.
cd /d "%~dp0"

rem Окружение проекта имеет приоритет: в нём стоят PySide6 и PyInstaller,
rem которыми build_exe.py собирает GostLib.exe. Системный python может
rem оказаться пустым.
if exist "..\.venv\Scripts\python.exe" (
  set "PY=..\.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

echo Интерпретатор: %PY%
echo.
"%PY%" build_installer.py %*

echo.
pause
