@echo off
setlocal EnableExtensions
title Instagram Control Head Setup

cd /d "%~dp0"

echo.
echo ============================================================
echo   Instagram Control Head - Setup Wizard Bootstrap
echo ============================================================
echo.

set "PYEXE="

where py >nul 2>nul
if %errorlevel%==0 (
    py -3.11 -c "import sys; print(sys.executable)" > "%TEMP%\igch_python.txt" 2>nul
    if %errorlevel%==0 (
        set /p PYEXE=<"%TEMP%\igch_python.txt"
    )
)

if not defined PYEXE (
    where python >nul 2>nul
    if %errorlevel%==0 (
        for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)"') do set "PYEXE=%%P"
    )
)

if not defined PYEXE (
    echo Python was not found.
    echo Installing Python 3.11 with WinGet...
    where winget >nul 2>nul
    if not %errorlevel%==0 (
        echo.
        echo ERROR: WinGet is unavailable.
        echo Install Python 3.11 manually, then run this file again.
        pause
        exit /b 1
    )

    winget install --id Python.Python.3.11 -e --source winget --accept-package-agreements --accept-source-agreements --disable-interactivity

    if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
        set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    )
)

if not defined PYEXE (
    echo.
    echo ERROR: Python could not be located after installation.
    echo Close this window and run the installer again.
    pause
    exit /b 1
)

echo Using Python:
echo   %PYEXE%
echo.

"%PYEXE%" "%~dp0instagram_control_head_setup_wizard.py"

if errorlevel 1 (
    echo.
    echo The setup wizard exited with an error.
    pause
)

endlocal
