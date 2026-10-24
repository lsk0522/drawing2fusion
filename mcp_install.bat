@echo off
chcp 65001 >nul
cd /d "%~dp0"
call tools\find_python.bat
if not defined D2F_PY (
    echo Python을 찾지 못했습니다. install.bat 을 먼저 실행해 주세요.
    pause
    exit /b 1
)
"%D2F_PY%" tools\install_mcp.py
pause
