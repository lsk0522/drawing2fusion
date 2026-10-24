@echo off
rem Finds a Python 3.9+ and stores its full path in D2F_PY.
rem Order: py launcher -> python on PATH -> Python bundled with Autodesk Fusion.
set "D2F_PY="
set "D2F_CHECK=import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"

where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "%D2F_CHECK%" >nul 2>nul
    if not errorlevel 1 (
        for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)"') do set "D2F_PY=%%P"
    )
)
if defined D2F_PY goto :found

for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined D2F_PY (
        "%%P" -c "%D2F_CHECK%" >nul 2>nul
        if not errorlevel 1 set "D2F_PY=%%P"
    )
)
if defined D2F_PY goto :found

for /d %%D in ("%LOCALAPPDATA%\Autodesk\webdeploy\production\*") do (
    if exist "%%D\Python\python.exe" set "D2F_PY=%%D\Python\python.exe"
)
if defined D2F_PY goto :found
goto :eof

:found
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
goto :eof
