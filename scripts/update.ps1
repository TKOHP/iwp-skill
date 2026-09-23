#Requires -Version 7
<#
.SYNOPSIS
    iwp skill 无损更新脚本
.DESCRIPTION
    备份技能目录内未被 git 跟踪的状态文件（.env、凭证缓存等）->
    执行 npx skills update iwp -> 恢复状态文件 -> 提示核验授权。
    用法: pwsh -File <技能目录>\scripts\update.ps1
#>
$ErrorActionPreference = 'Stop'

# scripts/ 的上级 = 技能根目录
$skillDir = Split-Path -Parent $PSScriptRoot

# 开发者安装（技能目录本身是链接）时，CLI 更新会穿过链接清空源仓库，禁止执行
$link = Get-Item -LiteralPath $skillDir -Force
if ($link.LinkType) {
    Write-Error "技能目录 $skillDir 是 $($link.LinkType)（开发者安装）。请直接在源仓库执行 git pull，勿用本脚本。"
    exit 1
}

# 用户态状态文件清单（.env.example 被 git 跟踪、不属于状态文件）
$statePatterns = @(
    '.env',
    '.token_key',
    '.token_cache.enc',
    '.swagger_meta.enc',
    '.oauth_next_step.json',
    '.oauth_next_step.json.bak',
    '.callback_result.json'
)

$backupDir = Join-Path ([System.IO.Path]::GetTempPath()) ("iwp-skill-backup-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null

$backedUp = @()
foreach ($pat in $statePatterns) {
    Get-ChildItem -LiteralPath $skillDir -File -Force -Filter $pat -ErrorAction SilentlyContinue |
        ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $backupDir -Force
            $backedUp += $_.Name
        }
}
if ($backedUp.Count) {
    Write-Host "[BACKUP] $($backedUp.Count) state file(s): $($backedUp -join ', ') -> $backupDir"
} else {
    Write-Host '[BACKUP] no state file found (fresh install), updating directly'
}

$ok = $false
try {
    npx -y skills@latest update iwp -g -y
    $ok = ($LASTEXITCODE -eq 0)
}
finally {
    foreach ($name in $backedUp) {
        Copy-Item -LiteralPath (Join-Path $backupDir $name) -Destination (Join-Path $skillDir $name) -Force
    }
    if ($backedUp.Count) { Write-Host "[RESTORE] state file(s) restored: $($backedUp -join ', ')" }
}

if ($ok) {
    # 恢复完整性校验：备份含凭证文件，全部校验一致才清理，避免 %TEMP% 留多余凭证副本
    $allMatch = $true
    foreach ($name in $backedUp) {
        $hashBackup = (Get-FileHash (Join-Path $backupDir $name)).Hash
        $hashRestored = (Get-FileHash (Join-Path $skillDir $name)).Hash
        if ($hashBackup -ne $hashRestored) {
            $allMatch = $false
            Write-Warning "[VERIFY] hash mismatch: $name"
        }
    }
    if ($allMatch) {
        Remove-Item -LiteralPath $backupDir -Recurse -Force
        Write-Host '[VERIFY] hashes match; backup cleaned up'
    } else {
        Write-Warning "[VERIFY] MISMATCH found; backup kept at $backupDir for inspection"
    }
    Write-Host '[DONE] verify auth: python cli.py auth status'
} else {
    Write-Error "[FAILED] update failed; state file(s) restored: $($backedUp -join ', '); backup at $backupDir"
    exit 1
}
