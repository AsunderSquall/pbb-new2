@echo off
setlocal
cd /d "%~dp0"
echo Recommended testing time: 20 minutes. Compilation is extra; testing continues beyond the target.
set "PBB_TUNE=tools\tune.py"
if exist "..\runtime\mingw64\bin\g++.exe" if exist "..\environment.bat" call "..\environment.bat"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -X utf8 "%PBB_TUNE%" --mode full %*
) else if exist "..\runtime\python\python.exe" (
    "..\runtime\python\python.exe" -X utf8 "%PBB_TUNE%" --mode full %*
) else (
    python -X utf8 "%PBB_TUNE%" --mode full %*
)
pause