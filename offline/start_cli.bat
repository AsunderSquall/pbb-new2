@echo off
rem CLI entry point.
rem 1) pure-only installs have no runtime\python\python.exe, so _python.bat falls back to PATH.
rem 2) no hardcoded -c: main.py picks the default config (same as the UI default).
setlocal
call "%~dp0_python.bat"
if errorlevel 1 (
    echo [start_cli] Python not found: extract the environment package, or install Python 3.10+ and add it to PATH.
    pause
    exit /b 1
)
if exist "%~dp0environment.bat" call "%~dp0environment.bat"
pushd "%~dp0"
"%PBB_PY%" "%~dp0source\main.py" %*
set "PBB_EXIT=%ERRORLEVEL%"
if not "%PBB_EXIT%"=="0" pause
popd
exit /b %PBB_EXIT%
