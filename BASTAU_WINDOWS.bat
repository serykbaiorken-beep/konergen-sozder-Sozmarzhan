@echo off
cd /d "%~dp0"
if not exist "data\moderator.json" (
  python server.py --setup
  if errorlevel 1 (
    pause
    exit /b 1
  )
)
python server.py
pause
