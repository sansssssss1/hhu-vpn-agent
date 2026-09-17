<#
.SYNOPSIS
  【可选】把 hhu-database-access 技能安装到 agent 的 skills 目录。
  核心功能不依赖这一步；不装 skill，命令行 / 守护任务照常工作。

.DESCRIPTION
  - 来源：integrations/SKILL.md（安装时把 {{HHUVPN_DIR}} 换成本机真实路径）
  - 默认目标：%USERPROFILE%/.codex/skills（DSH 的 ~/.dsh/skills 通常是指向它的 junction）
  - 可选 -AddToPath：把插件目录加进用户 PATH，之后 agent 可直接调用 hhuvpn

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts/install-skill.ps1
  powershell -ExecutionPolicy Bypass -File scripts/install-skill.ps1 -SkillsDir C:/Users/me/.claude/skills -AddToPath

.NOTES
  必须保持 UTF-8 BOM（Windows PowerShell 5.1 无 BOM 时中文会乱码并报语法错）。
#>
[CmdletBinding()]
param(
  [string]$SkillsDir = (Join-Path $env:USERPROFILE ".codex/skills"),
  [string]$Name = "hhu-database-access",
  [switch]$AddToPath,
  [switch]$Force
)

$ErrorActionPreference = "Stop"
$pluginDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$target = Join-Path $SkillsDir $Name

Write-Host ("plugin dir : " + $pluginDir)
Write-Host ("skill dir  : " + $target)

if ((Test-Path $target) -and -not $Force) {
  Write-Host "skill exists, skip (use -Force to overwrite)" -ForegroundColor Yellow
} else {
  New-Item -ItemType Directory -Force -Path $target | Out-Null
}

$src = Join-Path $pluginDir "integrations/SKILL.md"
if (-not (Test-Path $src)) { throw ("SKILL.md not found: " + $src) }

# 正斜杠：YAML 双引号串里的反斜杠是转义符，会把 frontmatter 弄坏；Windows 下 Python 也认正斜杠
$pluginDirFwd = $pluginDir.Replace("\", "/")
$content = Get-Content -LiteralPath $src -Raw -Encoding UTF8
$content = $content.Replace("{{HHUVPN_DIR}}", $pluginDirFwd)
Set-Content -LiteralPath (Join-Path $target "SKILL.md") -Value $content -Encoding UTF8
Write-Host "SKILL.md written" -ForegroundColor Green

Copy-Item (Join-Path $pluginDir "README.md") (Join-Path $target "README.md") -Force

if ($AddToPath) {
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  if ($null -eq $userPath) { $userPath = "" }
  if (($userPath -split ";") -notcontains $pluginDir) {
    $newPath = $userPath.TrimEnd(";") + ";" + $pluginDir
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "added to user PATH (new terminals only)" -ForegroundColor Green
  } else {
    Write-Host "already in PATH"
  }
}

$entry = Join-Path $pluginDir "hhuvpn.py"
Write-Host ""
Write-Host "verify:" -ForegroundColor Cyan
Write-Host ('  python "' + $entry + '" doctor --json')
Write-Host ('  python "' + $entry + '" ensure --db sd,cnki --json')
