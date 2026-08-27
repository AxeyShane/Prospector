@echo off
REM ===================================================================
REM  Prospector - developer / no-installer launcher.
REM  Double-click to run from source. End users get ProspectorSetup.exe
REM  instead (built with installer\build_installer.ps1).
REM ===================================================================
setlocal
cd /d "%~dp0"
title Prospector

set "VENV=%~dp0.venv"
set "PY=%VENV%\Scripts\python.exe"

if exist "%PY%" goto :check

echo.
echo   Setting up Prospector for the first time.
echo   This takes a couple of minutes. You only see this once.
echo.

where py >nul 2>&1 && (set "BOOTPY=py -3") || (set "BOOTPY=python")

%BOOTPY% --version >nul 2>&1
if errorlevel 1 (
  echo   [X] Python was not found on this computer.
  echo.
  echo       Install Python 3.11 or newer from https://www.python.org/downloads/
  echo       On the first screen, tick "Add python.exe to PATH".
  echo       Then run this file again.
  echo.
  pause
  exit /b 1
)

echo   Creating a private Python environment...
%BOOTPY% -m venv "%VENV%"
if errorlevel 1 (
  echo   [X] Could not create the environment. See the message above.
  pause
  exit /b 1
)

echo   Installing Prospector and its dependencies...
"%PY%" -m pip install --upgrade pip --quiet
"%PY%" -m pip install -e . --quiet
if errorlevel 1 (
  echo.
  echo   [X] Install failed. Check your internet connection and try again.
  pause
  exit /b 1
)
echo.
echo   Setup complete.
echo.

:check
REM An existing .venv from an older version can be missing a package added
REM since it was created. Repair it rather than failing with a traceback.
"%PY%" -c "import flask, typer, rich, httpx, bs4, openpyxl, dotenv" >nul 2>&1
if errorlevel 1 (
  echo   Updating dependencies...
  "%PY%" -m pip install -e . --quiet --upgrade
  if errorlevel 1 (
    echo.
    echo   [X] Could not install the dependencies. Check your internet connection.
    pause
    exit /b 1
  )
)

echo   Starting Prospector - your browser will open in a moment.
echo   Leave this window open while you use the app. Close it when finished.
echo.
"%PY%" -m prospector
if errorlevel 1 (
  echo.
  echo   Prospector stopped unexpectedly. The message above says why.
  pause
)
endlocal
