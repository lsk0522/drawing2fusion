@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Drawing2Fusion

call "%~dp0tools\find_python.bat"
if not defined D2F_PY (
    echo [ERROR] Python 3.9+ not found. Run install.bat first or install Python from https://www.python.org/downloads/
    pause
    exit /b 1
)

rem Drag a part-spec .json onto run.bat to build it right away; otherwise show the menu.
if "%~1"=="" (
    "%D2F_PY%" -m d2f menu
) else (
    "%D2F_PY%" -m d2f quick "%~1"
    echo.
    pause
)
