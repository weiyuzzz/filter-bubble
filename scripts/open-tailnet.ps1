<#
.SYNOPSIS
  让同 Tailscale 网段的其它设备（例如远程 Mac）访问本机的 Filter Bubble。

.DESCRIPTION
  Windows 防火墙默认拦截入站连接。这个脚本只放行 Tailscale 的 100.64.0.0/10 网段，
  不会把服务暴露给局域网或公网。

  需要管理员权限运行（改防火墙规则必须提权）。

.EXAMPLE
  # 在“以管理员身份运行”的 PowerShell 里执行
  powershell -ExecutionPolicy Bypass -File scripts\open-tailnet.ps1

.EXAMPLE
  # 撤销放行
  powershell -ExecutionPolicy Bypass -File scripts\open-tailnet.ps1 -Remove
#>
param(
    [int]$Port = 8791,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$ruleName = "Filter Bubble (Tailscale)"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "需要管理员权限。" -ForegroundColor Red
    Write-Host "请右键 PowerShell -> 以管理员身份运行，然后执行："
    Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\open-tailnet.ps1"
    exit 1
}

# 先删掉同名旧规则，避免重复
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule

if ($Remove) {
    Write-Host "已移除规则：$ruleName" -ForegroundColor Yellow
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
    -Profile Any | Out-Null

Write-Host "已放行 TCP $Port，来源限制为 Tailscale 网段 100.64.0.0/10" -ForegroundColor Green

# 顺便打印可用的访问地址
$addresses = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -like "100.*" -or ($_.IPAddress -like "192.168.*" -or $_.IPAddress -like "10.*") }
Write-Host ""
Write-Host "在其它设备上打开："
foreach ($addr in $addresses) {
    $tag = if ($addr.IPAddress -like "100.*") { "Tailscale" } else { "局域网  " }
    Write-Host ("  {0} http://{1}:{2}" -f $tag, $addr.IPAddress, $Port)
}
Write-Host ""
Write-Host "本机服务需要以 --host 0.0.0.0 启动，例如："
Write-Host "  python app.py serve --host 0.0.0.0"
