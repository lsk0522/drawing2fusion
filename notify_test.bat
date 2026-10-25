@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0tools\find_python.bat"
if not defined D2F_PY (
    echo [ERROR] Python 3.9+ not found.
    pause
    exit /b 1
)
"%D2F_PY%" -m d2f notify-test
echo.
pause
