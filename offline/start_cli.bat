@echo off
setlocal
call "%~dp0environment.bat"
pushd "%~dp0"
"%~dp0runtime\python\python.exe" "%~dp0source\main.py" -c config\config.yaml %*
if errorlevel 1 pause
popd