@echo off
setlocal
call "%~dp0environment.bat"
start "" "%~dp0PBBMonitor\PBBMonitor.exe"
