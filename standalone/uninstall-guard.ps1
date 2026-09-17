<#
.SYNOPSIS
  删除 hhuvpn 计划任务。（必须保持 UTF-8 BOM，见 install-guard.ps1 的说明）
#>
[CmdletBinding()]
param([string]$TaskName = "hhuvpn-guard")

$ErrorActionPreference = "Continue"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
  Write-Host ("scheduled task not found: " + $TaskName)
  exit 0
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host ("scheduled task removed: " + $TaskName) -ForegroundColor Green
