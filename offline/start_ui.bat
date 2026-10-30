@echo off
rem Start the frozen monitor (PBBMonitor). It carries its own Python, so no runtime/ needed.
setlocal
if not exist "%~dp0PBBMonitor\PBBMonitor.exe" (
    echo [start_ui] Missing PBBMonitor\PBBMonitor.exe - is the package complete?
    pause
    exit /b 1
)
if exist "%~dp0environment.bat" call "%~dp0environment.bat"
start "" "%~dp0PBBMonitor\PBBMonitor.exe"
exit /b 0
