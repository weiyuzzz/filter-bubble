@echo off
rem Filter Bubble launcher. Double-click for the menu, or pass an argument.
rem   fb.bat          menu
rem   fb.bat start    start
rem   fb.bat stop     stop
rem   fb.bat restart  restart
rem   fb.bat status   status
chcp 65001 >nul
setlocal

set "PS=powershell"
where pwsh >nul 2>nul && set "PS=pwsh"

"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\fb-control.ps1" %*
set "CODE=%ERRORLEVEL%"

endlocal & exit /b %CODE%