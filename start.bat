@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONPYCACHEPREFIX=%~dp0.pycache
"%~dp0runtime\python\python.exe" app.py serve
pause
