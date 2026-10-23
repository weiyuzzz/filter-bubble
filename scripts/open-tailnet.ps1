<#
.SYNOPSIS
  让同 Tailscale 网段的其它设备（例如远程 Mac）访问本机的 Filter Bubble。

.DESCRIPTION
  典型故障：Windows 弹出「是否允许 Python 访问网络」时点了取消，Windows 会记住这个选择，
  生成一条 **阻止（Block）** 入站规则。之后即使放开端口也连不上——因为在 Windows 防火墙里
  **Block 规则的优先级高于 Allow**，光加放行规则没用，必须先删掉那条 Block。

  这个脚本做两件事：
    1. 删除本机 python.exe 的入站 Block 规则（就是导致超时的那条）；
    2. 添加一条只允许 Tailscale 网段 100.64.0.0/10 的放行规则。

  需要管理员权限（改防火墙必须提权）。

.EXAMPLE
  # 双击 scripts\open-tailnet.cmd 也行；或者管理员 PowerShell 里执行
  powershell -ExecutionPolicy Bypass -File scripts\open-tailnet.ps1

.EXAMPLE
  # 只看当前状态，什么都不改
  powershell -ExecutionPolicy Bypass -File scripts\open-tailnet.ps1 -Check

.EXAMPLE
  # 撤销放行
  powershell -ExecutionPolicy Bypass -File scripts\open-tailnet.ps1 -Remove
#>
param(
    [int]$Port = 8791,
    [string]$PythonExe = (Join-Path $PSScriptRoot "..\runtime\python\python.exe"),
    [switch]$Remove,
    [switch]$Check
)

$ErrorActionPreference = "Stop"
$ruleName = "Filter Bubble (Tailscale)"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

$pythonPath = [System.IO.Path]::GetFullPath($PythonExe)

function Get-RulesForPython {
    Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object {
        $app = $_ | Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue
        $app -and $app.Program -and ($app.Program.Trim('"') -ieq $pythonPath)
    }
}

# 非管理员读不到 Get-NetFirewallRule，但注册表里的规则表是可读的
function Get-PythonRulesFromRegistry {
    $key = 'HKLM:\SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\FirewallRules'
    $entry = Get-ItemProperty -Path $key -ErrorAction SilentlyContinue
    if (-not $entry) { return @() }
    $escaped = [regex]::Escape($pythonPath)
    @($entry.PSObject.Properties |
        Where-Object { $_.Name -notmatch '^PS' -and $_.Value -match $escaped } |
        ForEach-Object {
            $fields = @{}
            foreach ($part in ($_.Value -split '\|')) {
                if ($part -match '^([A-Za-z]+)=(.*)$') { $fields[$matches[1]] = $matches[2] }
            }
            [pscustomobject]@{
                Name     = if ($fields['Name']) { $fields['Name'] } else { $_.Name }
                Action   = $fields['Action']
                Dir      = $fields['Dir']
                LPort    = $fields['LPort']
                Profile  = $fields['Profile']
                Protocol = $fields['Protocol']
            }
        })
}

if ($Check) {
    Write-Host "python: $pythonPath" -ForegroundColor Cyan
    $rules = @(Get-RulesForPython)
    if ($rules.Count -gt 0) {
        $rules | ForEach-Object {
            $port0 = ($_ | Get-NetFirewallPortFilter).LocalPort
            "{0,-8} {1,-6} port={2,-10} {3}" -f $_.Action, $_.Direction, ($port0 -join ","), $_.DisplayName
        }
    } else {
        # 非管理员读不到 Get-NetFirewallRule，但注册表的规则表可读
        $regRules = @(Get-PythonRulesFromRegistry)
        if ($regRules.Count -eq 0) {
            Write-Host "没有找到针对该 python.exe 的规则" -ForegroundColor Yellow
        } else {
            foreach ($r in $regRules) {
                $color = if ($r.Action -eq "Block") { "Red" } else { "Green" }
                Write-Host ("{0,-6} Dir={1,-3} LPort={2,-8} Profile={3,-8} Protocol={4}  ({5})" -f `
                    $r.Action, $r.Dir, $r.LPort, $r.Profile, $r.Protocol, $r.Name) -ForegroundColor $color
            }
            $blocks = @($regRules | Where-Object { $_.Action -eq "Block" -and $_.Dir -eq "In" })
            if ($blocks.Count -gt 0) {
                Write-Host ""
                Write-Host "发现入站 Block 规则 —— 这就是 Mac 连接超时的原因。" -ForegroundColor Red
                Write-Host "Block 优先级高于 Allow，必须用管理员权限删掉它：" -ForegroundColor Yellow
                Write-Host "  scripts\open-tailnet.cmd"
            }
        }
    }
    exit 0
}

if (-not $isAdmin) {
    Write-Host "需要管理员权限。" -ForegroundColor Red
    Write-Host "请右键 PowerShell -> 以管理员身份运行，然后执行："
    Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\open-tailnet.ps1"
    Write-Host ""
    Write-Host "不想提权的话，改用 Tailscale 自带代理（无需改防火墙）："
    Write-Host "  tailscale serve --bg $Port     # 然后访问 https://<机器名>.<tailnet>.ts.net/"
    exit 1
}

# 1) 删掉阻止规则（这是超时的真正原因）
$blocked = Get-RulesForPython | Where-Object { $_.Action -eq "Block" -and $_.Direction -eq "Inbound" }
if ($blocked) {
    foreach ($rule in $blocked) {
        Write-Host ("删除阻止规则：{0}" -f $rule.DisplayName) -ForegroundColor Yellow
        $rule | Remove-NetFirewallRule
    }
} else {
    Write-Host "没有需要删除的阻止规则" -ForegroundColor DarkGray
}

# 2) 重建放行规则
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule

if ($Remove) {
    Write-Host "已移除放行规则：$ruleName" -ForegroundColor Yellow
    exit 0
}

New-NetFirewallRule `
    -DisplayName $ruleName `
    -Description "Allow Filter Bubble from Tailscale tailnet only" `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalPort $Port `
    -RemoteAddress 100.64.0.0/10 `
    -Program $pythonPath `
    -Profile Any | Out-Null

Write-Host "已放行 TCP $Port（来源限制 100.64.0.0/10）" -ForegroundColor Green

# 3) 打印可访问地址
Write-Host ""
Write-Host "在 Mac 上打开："
$addresses = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -like "100.*" }
foreach ($addr in $addresses) {
    Write-Host ("  http://{0}:{1}" -f $addr.IPAddress, $Port)
}

# 如果已经配了 tailscale serve，顺带把 HTTPS 地址也列出来
$cli = "D:\Tailscale\tailscale.exe"
if (Test-Path $cli) {
    $serve = & $cli serve status 2>&1 | Out-String
    if ($serve -match $Port) {
        Write-Host ""
        Write-Host "Tailscale serve 已配置，也可以直接用（自带 HTTPS）：" -ForegroundColor Green
        $serve -split "`n" | Where-Object { $_ -match "^https://" } | ForEach-Object {
            Write-Host ("  " + $_.Trim())
        }
    }
}

Write-Host ""
Write-Host "服务需要以 --host 0.0.0.0 启动（本仓库的 .env 已配置）："
Write-Host "  python app.py serve --host 0.0.0.0"
