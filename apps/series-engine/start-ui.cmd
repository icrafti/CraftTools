@echo off
setlocal
where python >nul 2>&1
if errorlevel 1 (
    echo Python was not found. Install Python and add it to PATH.
    pause
    exit /b 1
)
python "%~dp0server.py" --open-browser %*
if errorlevel 1 pause
