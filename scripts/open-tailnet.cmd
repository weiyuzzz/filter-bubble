@echo off
REM 以管理员身份打开防火墙，只放行 Tailscale 网段访问 8791。
REM 双击运行即可，会弹出 UAC 授权框。
setlocal
set SCRIPT=%~dp0open-tailnet.ps1
powershell -NoProfile -Command "Start-Process -FilePath powershell -Verb RunAs -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File','\"%SCRIPT%\"')"
if errorlevel 1 (
    echo.
    echo 提权失败。请手动以管理员身份运行 PowerShell，然后执行：
    echo   powershell -ExecutionPolicy Bypass -File "%SCRIPT%"
    pause
)
endlocal
