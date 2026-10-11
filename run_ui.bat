@echo off
if exist "%~dp0..\runtime\python\pythonw.exe" (
    call "%~dp0..\environment.bat"
    start "" "%~dp0..\runtime\python\pythonw.exe" "%~dp0ui.py"
    exit /b
)
if exist "%~dp0build\ui_dist.txt" (
    set /p PBB_UI_DIST=<"%~dp0build\ui_dist.txt"
)
if defined PBB_UI_DIST (
    call :launch_current
    if not errorlevel 1 exit /b
)
if exist "%~dp0dist_config_import\PBBMonitor\PBBMonitor.exe" (
    start "" "%~dp0dist_config_import\PBBMonitor\PBBMonitor.exe"
    exit /b
)
if exist "%~dp0dist_update\PBBMonitor\PBBMonitor.exe" (
    start "" "%~dp0dist_update\PBBMonitor\PBBMonitor.exe"
    exit /b
)
if exist "%~dp0dist\PBBMonitor\PBBMonitor.exe" (
    start "" "%~dp0dist\PBBMonitor\PBBMonitor.exe"
    exit /b
)
if exist "%~dp0.venv\Scripts\pythonw.exe" (
    start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0ui.py"
) else (
    start "" pythonw.exe "%~dp0ui.py"
)
exit /b

:launch_current
if not exist "%~dp0%PBB_UI_DIST%\PBBMonitor\PBBMonitor.exe" exit /b 1
start "" "%~dp0%PBB_UI_DIST%\PBBMonitor\PBBMonitor.exe"
exit /b 0
