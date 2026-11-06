# fb-control.ps1 —— Filter Bubble 启停控制台（Windows）
#
# 一般由项目根目录的 fb.bat 调用，也可以直接用参数模式：
#   fb.bat                 打开菜单（双击）
#   fb.bat start           启动（成功后自动打开界面）
#   fb.bat stop            停止（只结束属于本项目的进程）
#   fb.bat restart         重启
#   fb.bat status          查看状态（运行中退出码 0，已停止退出码 1）
#   fb.bat open            只打开界面（没运行就先启动）
#   fb.bat log             看最近的日志
#   fb.bat shortcut        在桌面创建「开关」快捷方式
#
# 日志写在 data\serve.out.log / data\serve.err.log，
# 每次启动会把上一轮日志轮换成 serve.prev.*.log。

[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$Action = '',
    [switch]$NoBrowser,
    [string]$ShortcutDir = ''
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }

$Root       = Split-Path -Parent $PSScriptRoot
$DataDir    = Join-Path $Root 'data'
$PidFile    = Join-Path $DataDir 'serve.pid'
$OutLog     = Join-Path $DataDir 'serve.out.log'
$ErrLog     = Join-Path $DataDir 'serve.err.log'
$PrevOutLog = Join-Path $DataDir 'serve.prev.out.log'
$PrevErrLog = Join-Path $DataDir 'serve.prev.err.log'
$DefaultPort = 8791

function Get-PythonPath {
    foreach ($name in @('python.exe', 'pythonw.exe')) {
        $candidate = Join-Path $Root "runtime\python\$name"
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw '找不到 Python 运行时（应为 runtime\python\python.exe）'
}

function Get-ProjectPort {
    $port = $DefaultPort
    $envFile = Join-Path $Root '.env'
    if (Test-Path -LiteralPath $envFile) {
        foreach ($line in (Get-Content -LiteralPath $envFile -Encoding UTF8)) {
            if ($line -match '^\s*GITPULSE_PORT\s*=\s*"?(\d+)"?\s*$') { $port = [int]$Matches[1] }
        }
    }
    if ($env:GITPULSE_PORT -and $env:GITPULSE_PORT -match '^\d+$') { $port = [int]$env:GITPULSE_PORT }
    return $port
}

function Get-ListenerPids([int]$Port) {
    $found = New-Object System.Collections.Generic.List[int]
    try {
        Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            ForEach-Object { $found.Add([int]$_.OwningProcess) }
    } catch { }
    if ($found.Count -eq 0) {
        foreach ($line in (& netstat -ano 2>$null)) {
            if ($line -match "^\s*TCP\s+\S*[:.]$Port\s+\S+\s+LISTENING\s+(\d+)") {
                $found.Add([int]$Matches[1])
            }
        }
    }
    return @($found | Sort-Object -Unique)
}

function Get-ServerInfo {
    $port = Get-ProjectPort
    foreach ($procId in (Get-ListenerPids $port)) {
        $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if (-not $proc) { continue }
        $exePath = ''
        try { $exePath = $proc.Path } catch { }
        $belongs = $false
        if ($exePath) {
            $belongs = $exePath.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase)
        }
        return [pscustomobject]@{
            Running    = $true
            Pid        = $procId
            Port       = $port
            Process    = $proc.ProcessName
            ExePath    = $exePath
            Belongs    = $belongs
            PythonLike = ($proc.ProcessName -like 'python*')
            Started    = $proc.StartTime
            Url        = "http://127.0.0.1:$port"
        }
    }
    return [pscustomobject]@{
        Running = $false; Pid = 0; Port = $port; Process = ''; ExePath = ''
        Belongs = $false; PythonLike = $false; Started = $null
        Url = "http://127.0.0.1:$port"
    }
}

function Start-ServerCore {
    $info = Get-ServerInfo
    if ($info.Running) {
        Write-Host ("  已经在运行：PID {0} · {1}" -f $info.Pid, $info.Url) -ForegroundColor Yellow
        return $info
    }
    $python = Get-PythonPath
    if (-not (Test-Path -LiteralPath $DataDir)) { New-Item -ItemType Directory -Force -Path $DataDir | Out-Null }

    # 保留一代历史日志
    if (Test-Path -LiteralPath $OutLog) { Move-Item -LiteralPath $OutLog -Destination $PrevOutLog -Force }
    if (Test-Path -LiteralPath $ErrLog) { Move-Item -LiteralPath $ErrLog -Destination $PrevErrLog -Force }

    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONPYCACHEPREFIX = (Join-Path $Root '.pycache')

    Write-Host ("  正在启动 {0} app.py serve …" -f (Split-Path -Leaf $python))
    $proc = Start-Process -FilePath $python -ArgumentList @('app.py', 'serve') `
        -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog
    Set-Content -LiteralPath $PidFile -Value $proc.Id -Encoding ASCII

    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        if ($proc.HasExited) {
            Write-Host ("  ✗ 进程启动后立刻退出了（退出码 {0}）。日志末尾：" -f $proc.ExitCode) -ForegroundColor Red
            Show-LogTail $ErrLog 15
            Show-LogTail $OutLog 15
            return $null
        }
        $live = Get-ServerInfo
        if ($live.Running) {
            Write-Host ("  ✓ 已启动：PID {0} · {1}" -f $live.Pid, $live.Url) -ForegroundColor Green
            return $live
        }
        Start-Sleep -Milliseconds 400
    }
    Write-Host '  ⚠ 60 秒内没有等到端口监听，请看日志（菜单 [5]）。' -ForegroundColor Yellow
    return $null
}

function Start-Server {
    $info = Start-ServerCore
    if ($info -and -not $NoBrowser) {
        Start-Process $info.Url
        Write-Host ("  已在浏览器打开 {0}" -f $info.Url) -ForegroundColor Green
    }
    return $info
}

function Stop-Server {
    $info = Get-ServerInfo
    if (-not $info.Running) {
        if (Test-Path -LiteralPath $PidFile) { Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue }
        Write-Host '  当前没有运行。' -ForegroundColor Yellow
        return $true
    }
    if (-not ($info.Belongs -or $info.PythonLike)) {
        Write-Host ("  ✗ 端口 {0} 被别的程序占用（PID {1} · {2}），没有动它。" -f $info.Port, $info.Pid, $info.ExePath) -ForegroundColor Red
        return $false
    }
    if (-not $info.Belongs) {
        Write-Host ("  注意：PID {0} 的 python 不在本项目目录下（{1}），仍按项目端口处理。" -f $info.Pid, $info.ExePath) -ForegroundColor Yellow
    }
    Write-Host ("  正在停止 PID {0} …" -f $info.Pid)
    Stop-Process -Id $info.Pid -Force -ErrorAction SilentlyContinue

    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-ServerInfo).Running) { break }
        Start-Sleep -Milliseconds 300
    }
    if ((Get-ServerInfo).Running) {
        Write-Host '  ⚠ 端口仍在监听，可能有残留进程。' -ForegroundColor Yellow
        return $false
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    Write-Host '  ✓ 已停止。' -ForegroundColor Green
    return $true
}

function Restart-Server {
    if (-not (Stop-Server)) { return $null }
    Start-Sleep -Milliseconds 600
    return (Start-Server)
}

function Open-Interface {
    $info = Get-ServerInfo
    if (-not $info.Running) {
        Write-Host '  服务没在运行，先启动它…' -ForegroundColor Yellow
        $info = Start-ServerCore
        if (-not $info) { return }
    }
    Start-Process $info.Url
    Write-Host ("  ✓ 已打开 {0}" -f $info.Url) -ForegroundColor Green
}

function Show-LogTail([string]$Path, [int]$Lines = 30) {
    if (-not (Test-Path -LiteralPath $Path)) {
        Write-Host ("  （还没有 {0}）" -f (Split-Path -Leaf $Path)) -ForegroundColor DarkGray
        return
    }
    Write-Host ("  ── {0} 末尾 {1} 行 ──" -f (Split-Path -Leaf $Path), $Lines) -ForegroundColor DarkGray
    Get-Content -LiteralPath $Path -Tail $Lines -Encoding UTF8
}

function Show-Logs {
    Show-LogTail $OutLog 40
    Write-Host ''
    Show-LogTail $ErrLog 20
}

function New-Shortcut([string]$Dir = '') {
    if (-not $Dir) { $Dir = [Environment]::GetFolderPath('Desktop') }
    if (-not (Test-Path -LiteralPath $Dir)) { New-Item -ItemType Directory -Force -Path $Dir | Out-Null }
    $lnk = Join-Path $Dir 'Filter Bubble 开关.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($lnk)
    $sc.TargetPath = (Join-Path $Root 'fb.bat')
    $sc.WorkingDirectory = $Root
    $sc.IconLocation = (Get-PythonPath) + ',0'
    $sc.Description = 'Filter Bubble 启动 / 停止'
    $sc.Save()
    Write-Host ("  ✓ 已创建快捷方式：{0}" -f $lnk) -ForegroundColor Green
}

function Show-Status {
    $info = Get-ServerInfo
    if ($info.Running) {
        Write-Host ("  运行中   PID {0}   {1}   端口 {2}" -f $info.Pid, $info.Url, $info.Port) -ForegroundColor Green
        if ($info.Started) { Write-Host ("  启动于   {0}" -f $info.Started) -ForegroundColor DarkGray }
        if ($info.ExePath) { Write-Host ("  进程     {0}" -f $info.ExePath) -ForegroundColor DarkGray }
    } else {
        Write-Host ("  已停止   端口 {0} 空闲   {1}" -f $info.Port, $info.Url) -ForegroundColor Yellow
    }
    return $info
}

function Show-Menu {
    # 非交互场景（stdin 被重定向 / 无控制台）只处理一轮，避免空输入导致死循环
    $interactive = $true
    try { $interactive = -not [Console]::IsInputRedirected } catch { }

    while ($true) {
        try { Clear-Host } catch { }
        Write-Host ''
        Write-Host '  ════════════════════════════════════════════════' -ForegroundColor Cyan
        Write-Host '     Filter Bubble · 今日 GitHub 信号（本机服务）' -ForegroundColor Cyan
        Write-Host '  ════════════════════════════════════════════════' -ForegroundColor Cyan
        $info = Get-ServerInfo
        if ($info.Running) {
            Write-Host ("     状态：运行中    PID {0}" -f $info.Pid) -ForegroundColor Green
            Write-Host ("     地址：{0}" -f $info.Url) -ForegroundColor DarkGray
        } else {
            Write-Host '     状态：已停止' -ForegroundColor DarkGray
        }
        Write-Host ''
        Write-Host '     [1] 启动            [2] 停止'
        Write-Host '     [3] 重启            [4] 打开界面'
        Write-Host '     [5] 看日志          [6] 打开数据目录'
        Write-Host '     [7] 生成今日日报    [8] 创建桌面快捷方式'
        Write-Host '     [0] 退出'
        Write-Host ''
        $choice = Read-Host '  请输入编号后回车'

        switch ($choice.Trim()) {
            '1' { Start-Server | Out-Null }
            '2' { Stop-Server | Out-Null }
            '3' { Restart-Server | Out-Null }
            '4' { Open-Interface }
            '5' { Show-Logs }
            '6' { Start-Process explorer.exe $DataDir }
            '7' {
                Write-Host '  在新的命令行窗口里生成今日日报（约 1–2 分钟）…' -ForegroundColor Yellow
                Start-Process -FilePath (Get-PythonPath) -ArgumentList @('app.py', 'refresh') `
                    -WorkingDirectory $Root
            }
            '8' { New-Shortcut }
            '0' { Write-Host ''; return }
            '' { if (-not $interactive) { return } else { continue } }
            default { Write-Host '  没有这个编号。' -ForegroundColor Yellow }
        }
        Write-Host ''
        if (-not $interactive) { return }
        Read-Host '  按回车返回菜单' | Out-Null
    }
}

switch ($Action.Trim().ToLower()) {
    ''         { Show-Menu }
    'menu'     { Show-Menu }
    'start'    { Start-Server | Out-Null }
    'stop'     { Stop-Server | Out-Null }
    'restart'  { Restart-Server | Out-Null }
    'status'   { $s = Show-Status; if (-not $s.Running) { exit 1 } }
    'open'     { Open-Interface }
    'log'      { Show-Logs }
    'logs'     { Show-Logs }
    'shortcut' { New-Shortcut -Dir $ShortcutDir }
    default {
        Write-Host ("  用法：fb.bat [start|stop|restart|status|open|log|shortcut]（未识别的参数：{0}）" -f $Action) -ForegroundColor Yellow
        exit 2
    }
}