@echo off
rem CLI entry point.
rem 1) pure-only installs have no runtime\python\python.exe, so fall back to python on PATH.
rem 2) no hardcoded -c: main.py picks the default config (same as the UI default).
setlocal
pushd "%~dp0"

set "PBB_PY=%~dp0runtime\python\python.exe"
if not exist "%PBB_PY%" (
  set "PBB_PY=python"
  where python >nul 2>nul
  if errorlevel 1 (
    echo [start_cli] Python not found: extract the environment package, or install Python 3.10+ and add it to PATH.
    pause
    popd
    exit /b 1
  )
)

if exist "%~dp0environment.bat" call "%~dp0environment.bat"
"%PBB_PY%" "%~dp0source\main.py" %*
set "PBB_EXIT=%ERRORLEVEL%"
if not "%PBB_EXIT%"=="0" pause
popd
exit /b %PBB_EXIT%
