@echo off
rem Start the TFS web app (double-click this file). Your browser opens http://127.0.0.1:5000.
rem Keep this window open while you use the website; close it (or press Ctrl+C) to stop the app.
title TFS web app - http://127.0.0.1:5000
cd /d "%~dp0"
set "PY=%~dp0..\Work with claude\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo.
    echo  The pipeline's Python environment was not found:
    echo    %PY%
    echo  Run setup_env.ps1 in the "Work with claude" folder first ^(see USER_MANUAL.md^).
    echo.
    pause
    exit /b 1
)
if "%TFS_PORT%"=="" set "TFS_PORT=5000"
start "" /b cmd /c "timeout /t 4 /nobreak >nul & start http://127.0.0.1:%TFS_PORT%"
"%PY%" app.py
echo.
echo  The app has stopped.
pause
