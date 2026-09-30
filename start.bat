@echo off
rem One-click launcher for the underwater detection system.
rem NOTE: keep this file pure ASCII to avoid codepage issues.
cd /d "%~dp0"
title Underwater Detection System

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else if exist "C:\Users\12503\.workbuddy\binaries\python\envs\underwater\Scripts\python.exe" (
    set "PY=C:\Users\12503\.workbuddy\binaries\python\envs\underwater\Scripts\python.exe"
) else (
    set "PY=python"
)

echo Starting underwater detection system ...
echo Python: %PY%
echo.
"%PY%" run.py %*

echo.
echo Server exited.
pause
