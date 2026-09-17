<#
.SYNOPSIS
  打出可分发的源码包（zip），排除隐私文件，并在打包后自检。

.DESCRIPTION
  排除项（这些绝不能进包 / 进仓库）：
    config.ini        可能含学号与明文密码
    state/            登录态 cookie、会话 ticket、DPAPI 凭据、含学号的日志
    .pack-check/      上一次验证时解压出来的整份副本（含本机绝对路径）
    *.dpapi / *.pyc   凭据与编译产物
    __pycache__/ tests/.tmp/ .git/ .dl-test/ 等缓存

  打包后会扫描 staging 目录：命中隐私标记或敏感文件名就**删除 zip 并报错**，
  避免"以为干净、其实带出去了"。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools/make-package.ps1
  powershell -ExecutionPolicy Bypass -File tools/make-package.ps1 -OutDir D:/dist

.NOTES
  必须保持 UTF-8 BOM（Windows PowerShell 5.1 无 BOM 时中文会乱码）。
  $PSScriptRoot 在 param 默认值里取不到，所以在函数体里兜底。
#>
[CmdletBinding()]
param(
  [string]$OutDir = ""
)

$ErrorActionPreference = "Stop"
$proj = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($OutDir)) { $OutDir = Split-Path -Parent $proj }
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }

$name = Split-Path $proj -Leaf
$version = (& python (Join-Path $proj "hhuvpn.py") --version) -replace '^hhuvpn\s+', ''
$zip = Join-Path $OutDir ("{0}-{1}.zip" -f $name, $version)
$stage = Join-Path ([System.IO.Path]::GetTempPath()) ("hhu-pack-" + [guid]::NewGuid().ToString("N").Substring(0, 8))

Write-Host ("source  : " + $proj)
Write-Host ("version : " + $version)
New-Item -ItemType Directory -Force -Path (Join-Path $stage $name) | Out-Null

# robocopy 退出码 0-7 都算成功（1=有复制, 2=有额外文件, 3=两者皆有…）
robocopy $proj (Join-Path $stage $name) /E /NFL /NDL /NJH /NJS /NP /XD state __pycache__ .git .tmp .dl-test .pack-check .pack-staging .pytest_cache logs /XF config.ini *.pyc *.pyo *.dpapi cookies.txt out.txt err.txt p.txt t.txt | Out-Null
if ($LASTEXITCODE -ge 8) { throw ("robocopy failed, exit " + $LASTEXITCODE) }

# ---- 打包后自检：宁可不发包，也不要带隐私出去 ----
$suspects = @()
# 标记拆开拼接：本文件自己也不能出现这些字面量，否则自检会命中自己
$marks = @(('Sam' + 'my0308'), ('2508' + '120114'), ('359' + '36'), ('LAPTOP' + '-HR47'), ('随便' + '做做'))
$patterns = $marks + 'wengine_vpn_ticketwebvpn_hhu_edu_cn\s*[=:]\s*[0-9a-fA-F]{16,}'
foreach ($pat in $patterns) {
  $hits = Get-ChildItem (Join-Path $stage $name) -Recurse -File |
    Select-String -Pattern $pat -ErrorAction SilentlyContinue
  foreach ($h in $hits) { $suspects += ("命中 {0}: {1}:{2}" -f $pat, $h.Path, $h.LineNumber) }
}
foreach ($bad in @('config.ini', 'state', 'credentials.dpapi')) {
  $found = Get-ChildItem (Join-Path $stage $name) -Recurse -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -eq $bad }
  foreach ($f in $found) { $suspects += ("不该出现的文件/目录: " + $f.FullName) }
}
if ($suspects.Count -gt 0) {
  Write-Host "自检未通过，已放弃打包：" -ForegroundColor Red
  $suspects | ForEach-Object { Write-Host ("  " + $_) }
  Remove-Item $stage -Recurse -Force
  throw "package self-check failed"
}

if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $stage $name) -DestinationPath $zip -CompressionLevel Optimal
Remove-Item $stage -Recurse -Force

$size = [math]::Round((Get-Item $zip).Length / 1KB)
Write-Host ("self-check : passed") -ForegroundColor Green
Write-Host ("package    : " + $zip + "  (" + $size + " KB)") -ForegroundColor Green
