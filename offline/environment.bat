@echo off
set "PBB_TOOLCHAIN_DIR=%~dp0runtime\mingw64"
set "PATH=%PBB_TOOLCHAIN_DIR%\bin;%~dp0runtime\python;%PATH%"
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONNOUSERSITE=1"
