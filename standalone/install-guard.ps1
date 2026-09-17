<#
.SYNOPSIS
  注册 Windows 计划任务：登录时触发一次 + 之后每隔 N 分钟跑一次 hhuvpn guard --once。
  作用：校园网掉线自动重登；WebVPN 会话过期自动重登。纯本机功能，与 agent 无关。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File standalone/install-guard.ps1
  powershell -ExecutionPolicy Bypass -File standalone/install-guard.ps1 -IntervalMinutes 5 -Databases sd,cnki

.NOTES
  - 必须保持 UTF-8 BOM：Windows PowerShell 5.1 没有 BOM 时会按本地代码页解析，
    中文串会被截断甚至把后面的命令当代码执行（本文件因此踩过一次坑）。
  - 用计划任务 API（Register-ScheduledTask）而不是 schtasks，避免 /TR 的引号地狱。
  - 两个触发器：① 登录时（开机登录后立刻把校园网/WebVPN 弄通，不用等下一分钟）
                ② 从现在起每 N 分钟重复（3650 天窗口，配合 StartWhenAvailable 补跑漏掉的）
#>
[CmdletBinding()]
param(
  [int]$IntervalMinutes = 1,
  [string]$TaskName = "hhuvpn-guard",
  # 注意：参数名不能叫 Db——PowerShell 里 db 是 -Debug 的内置别名，会冲突
  [string]$Databases = "",
  [switch]$UsePythonConsole
)

$ErrorActionPreference = "Stop"
$proj = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$entry = Join-Path $proj "hhuvpn.py"

if (-not (Test-Path $entry)) { throw ("entry not found: " + $entry) }

# 默认用 pythonw.exe：计划任务每分钟跑一次，不能每次闪一个黑窗口
$exe = $null
if (-not $UsePythonConsole) {
  $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
  if ($cmd) { $exe = $cmd.Source }
}
if (-not $exe) {
  $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
  if (-not $cmd) { throw "python.exe / pythonw.exe not found in PATH" }
  $exe = $cmd.Source
}

$argList = @('"' + $entry + '"', "guard", "--once", "--quiet")
if (-not [string]::IsNullOrWhiteSpace($Databases)) { $argList += @("--db", $Databases) }
$arguments = $argList -join " "

Write-Host ("interpreter : " + $exe)
Write-Host ("entry       : " + $entry)
Write-Host ("arguments   : " + $arguments)
Write-Host ("interval    : every " + $IntervalMinutes + " minute(s), plus once at logon")

$action = New-ScheduledTaskAction -Execute $exe -Argument $arguments -WorkingDirectory $proj

$triggers = @()
$triggers += New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME   # 仅当前用户：不需要管理员权限
$triggers += New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings -Force -Description "hhuvpn guard: campus network relogin + WebVPN session keepalive (standalone, no agent required)" | Out-Null

Write-Host ""
Write-Host ("scheduled task registered: " + $TaskName) -ForegroundColor Green
Write-Host ("query  : Get-ScheduledTask -TaskName " + $TaskName)
Write-Host ("run now: Start-ScheduledTask -TaskName " + $TaskName)
Write-Host ('status : python "' + $entry + '" guard --status')
Write-Host "remove : standalone/uninstall-guard.cmd"
Write-Host ""
Write-Host "tip: you can skip the scheduled task entirely - just double-click the .cmd launcher." -ForegroundColor Cyan
