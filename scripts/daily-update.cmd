@echo off
rem Deterministic daily update for Windows (Task Scheduler / Harness entry point).
rem Exit codes: 0 = today's digest ready, 75 = offline (retry later), 1 = error.
setlocal
cd /d "%~dp0.."
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONPYCACHEPREFIX=%CD%\.pycache
"%~dp0..\runtime\python\python.exe" "%~dp0daily_update.py"
exit /b %ERRORLEVEL%
