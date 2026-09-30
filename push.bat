@echo off
rem One-click push to GitHub.
rem Notes:
rem   1) git may not be in PATH -> locate it by absolute path first
rem   2) clear proxy env vars so git connects to GitHub directly
rem   3) keep this file pure ASCII

cd /d "%~dp0"
title Push to GitHub

set http_proxy=
set https_proxy=
set HTTP_PROXY=
set HTTPS_PROXY=
set ALL_PROXY=
set all_proxy=

set "GITEXE="
if exist "C:\Users\12503\.workbuddy\binaries\PortableGit\versions\1.2.0\mingw64\bin\git.exe" set "GITEXE=C:\Users\12503\.workbuddy\binaries\PortableGit\versions\1.2.0\mingw64\bin\git.exe"
if not defined GITEXE if exist "C:\Users\12503\.workbuddy\binaries\PortableGit\versions\1.2.0\cmd\git.exe" set "GITEXE=C:\Users\12503\.workbuddy\binaries\PortableGit\versions\1.2.0\cmd\git.exe"
if not defined GITEXE if exist "C:\Program Files\Git\bin\git.exe" set "GITEXE=C:\Program Files\Git\bin\git.exe"
if not defined GITEXE if exist "C:\Program Files\Git\cmd\git.exe" set "GITEXE=C:\Program Files\Git\cmd\git.exe"
if not defined GITEXE (
  for /f "delims=" %%i in ('where git 2^>nul') do if not defined GITEXE set "GITEXE=%%i"
)

if not defined GITEXE (
  echo [ERROR] Cannot find git.exe. Install Git for Windows, then run this again.
  pause
  exit /b 1
)

echo Using git: %GITEXE%
echo Target  : https://github.com/HWJs12/Object-detection-platform.git
echo.
echo A GitHub login window may appear. Choose "Sign in with your browser".
echo.
"%GITEXE%" push -u origin main

if errorlevel 1 (
  echo.
  echo [FAILED] Push did not succeed.
  echo Option 1: run this .bat again after signing in.
  echo Option 2: create a token at https://github.com/settings/tokens ^(check "repo"^), then run:
  echo   "%GITEXE%" push https://HWJs12:YOUR_TOKEN@github.com/HWJs12/Object-detection-platform.git main:main
) else (
  echo.
  echo [DONE] Pushed. Check https://github.com/HWJs12/Object-detection-platform
)

echo.
pause
