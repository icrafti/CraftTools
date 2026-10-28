@echo off
set "SCRIPT=%~dp0yt_downloader.py"
where pythonw >nul 2>nul
if errorlevel 1 (
  echo [ERROR] pythonw not found. Install Python with Tcl/Tk support from python.org.
  pause
  exit /b 1
)
start "" pythonw "%SCRIPT%"
