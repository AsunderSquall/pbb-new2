@echo off
rem Shared launcher helper: set PBB_PY to the bundled runtime python, else python on PATH.
rem Usage:  call "%~dp0_python.bat"   /   errorlevel 1 means no python at all.
rem ASCII only: cmd decodes .bat with the OEM codepage, so non-ASCII text in a batch
rem file can be parsed as commands (we hit exactly that with Chinese comments).
set "PBB_PY=%~dp0runtime\python\python.exe"
if exist "%PBB_PY%" exit /b 0
set "PBB_PY="
where python >nul 2>nul && set "PBB_PY=python"
if not defined PBB_PY where python3 >nul 2>nul && set "PBB_PY=python3"
if not defined PBB_PY exit /b 1
exit /b 0
