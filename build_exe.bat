@echo off
rem Сборка GostLib.exe. Все ключи прокидываются в build_exe.py:
rem   build_exe.bat --console --clean --name MyApp
rem Файл в кодировке CP866 -- родной для консоли Windows, без BOM,
rem иначе cmd спотыкается на первой строке.
cd /d "%~dp0"

rem Окружение проекта имеет приоритет: именно в нём стоят PySide6 и
rem остальные зависимости, а системный python может быть пустым.
if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

echo Интерпретатор: %PY%
echo.
"%PY%" build_exe.py %*

echo.
pause
