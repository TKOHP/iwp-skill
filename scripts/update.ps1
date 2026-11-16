#Requires -Version 7
<#
.SYNOPSIS
    iwp skill 更新入口（按安装形态自动分派）
.DESCRIPTION
    形态判定、更新执行与后置校验全部由 scripts/skill_update.py 完成。本脚本只做
    转发：不再维护第二份状态文件清单，也不直接调用 npx，避免「脚本只看一条更新
    路径、目录却是另一种形态」的假成功。
    用法: pwsh -File <技能目录>\scripts\update.ps1 [--preview | --apply [--force]]
    无参数等价于 --preview（只输出计划，不改动任何文件）。
#>
$ErrorActionPreference = 'Stop'

# scripts/ 的上级 = 技能根目录
$skillDir = Split-Path -Parent $PSScriptRoot

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Error "python 不在 PATH，无法执行更新（技能目录 $skillDir）"
    exit 1
}

$runner = Join-Path $PSScriptRoot 'skill_update.py'
if (-not (Test-Path -LiteralPath $runner)) {
    Write-Error "缺少 scripts/skill_update.py（技能目录 $skillDir 不完整）"
    exit 1
}

& $python.Source $runner @args
exit $LASTEXITCODE
