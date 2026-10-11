@echo off
setlocal
call "%~dp0environment.bat"
pushd "%~dp0"
"%~dp0runtime\python\python.exe" "%~dp0source\tools\rebuild_local.py" %*
if errorlevel 1 (
    echo Rebuild failed. Close PBBMonitor and retry.
) else (
    echo Rebuild succeeded. Both engines now target this CPU.
)
popd
pause