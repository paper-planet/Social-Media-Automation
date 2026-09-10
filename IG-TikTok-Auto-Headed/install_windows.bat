@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 install.py
) else (
  python install.py
)

if errorlevel 1 (
  echo.
  echo Installation failed.
  pause
  exit /b 1
)

echo.
echo Installation complete.
pause
