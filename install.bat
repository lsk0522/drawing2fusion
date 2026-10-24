@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Drawing2Fusion - install

echo ============================================================
echo   Drawing2Fusion install
echo ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 (
    echo.
    echo [ERROR] add-in install failed. Close Fusion and try again.
    pause
    exit /b 1
)

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo created .env  ^(edit it to turn on Discord / Telegram alerts^)
)

call "%~dp0tools\find_python.bat"
if not defined D2F_PY (
    echo.
    echo [ERROR] Python 3.9+ not found. Install it from https://www.python.org/downloads/
    pause
    exit /b 1
)
echo using Python: %D2F_PY%
echo.
"%D2F_PY%" -m d2f doctor
echo.
echo Next: in Fusion press Shift+S, open Add-Ins, select Drawing2Fusion, click Run.
echo       After that, double-click run.bat
echo.
pause
