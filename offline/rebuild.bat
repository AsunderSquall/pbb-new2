@echo off
rem Rebuild both engines (ordinary + UI) for this CPU.
rem Python lookup goes through _python.bat: pure-only installs have no runtime\python,
rem and the old hardcoded path made cmd report "The system cannot find the path specified".
setlocal
call "%~dp0_python.bat"
set "PBB_BUNDLED=0"
if exist "%~dp0runtime\python\python.exe" set "PBB_BUNDLED=1"
if errorlevel 1 (
    echo [rebuild] Python not found: extract the environment package, or install Python 3.10+ and add it to PATH.
    pause
    exit /b 1
)
if not exist "%~dp0source\tools\rebuild_local.py" (
    echo [rebuild] Missing tools\rebuild_local.py - is the package complete?
    pause
    exit /b 1
)
if "%PBB_BUNDLED%"=="0" (
    echo [rebuild] No bundled runtime: using system Python "%PBB_PY%".
    echo [rebuild] Rebuilding needs pybind11 plus a C++ compiler - extract the environment package,
    echo [rebuild] or install those into this Python, if the build fails below.
)
if exist "%~dp0environment.bat" call "%~dp0environment.bat"
pushd "%~dp0"
"%PBB_PY%" "%~dp0source\tools\rebuild_local.py" %*
set "PBB_EXIT=%ERRORLEVEL%"
if not "%PBB_EXIT%"=="0" (
    echo Rebuild failed. Close PBBMonitor and retry.
) else (
    echo Rebuild succeeded. Both engines now target this CPU.
)
popd
pause
exit /b %PBB_EXIT%
