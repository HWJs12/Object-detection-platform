@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
rem Resolve the project directory from this launcher, even after moving it.
cd /d "%~dp0"
title Underwater Detection System

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" -c "import sys; assert sys.version_info >= (3, 9), 'Python 3.9+ required'; import fastapi, uvicorn, torch, ultralytics, cv2, multipart, yaml" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] The project Python environment is missing or unavailable.
    echo Project: %~dp0
    echo Expected Python: %~dp0.venv\Scripts\python.exe
    echo See README.md for environment setup instructions.
    pause
    exit /b 1
)

echo Starting underwater detection system ...
echo Python: %PY%
echo.
"%PY%" run.py %* --open-browser
set "SERVER_EXIT=%ERRORLEVEL%"

echo.
echo Server exited.
pause
exit /b %SERVER_EXIT%
