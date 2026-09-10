@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" launcher.py
) else (
  echo Virtual environment not found. Running installer first...
  call install_windows.bat
  if errorlevel 1 exit /b 1
  ".venv\Scripts\python.exe" launcher.py
)
