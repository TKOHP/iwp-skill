# 更新 iwp 技能工作流

用户要求「更新 iwp 技能」时按本文件执行。SKILL.md 只做路由，具体步骤以本文件为准。

## Step 0：记录更新前基线

任一形态的更新开始前，先记录以下两项，作为 Step 3 的唯一对照物：

1. 技能目录下的状态文件实际清单（清单以 `scripts/update.ps1` 的 `$statePatterns`
   为唯一来源，本文不重复维护第二份列表）；
2. `python cli.py auth status` 的 `authorized` 值（未授权时记录其 `reason` 字段）。

完成判据：两项均已记录，可用于更新后逐项对照。禁止以记忆或另一份副本（dev 仓库、
其他安装位置）的状态代替本目录基线。

## Step 1：识别安装形态

技能目录 = 当前 SKILL.md 所在目录（SKILL_DIR）。按以下特征判定：

| 形态 | 判定特征 |
|------|---------|
| CLI 安装 | 技能目录含 `scripts/update.ps1`，且 `%USERPROFILE%\.agents\.skill-lock.json` 中记录了 `iwp` |
| clone / symlink 安装 | 技能目录内含 `.git` 子目录；或目录本身是符号链接（`Get-Item <技能目录> -Force` 的 `LinkType` 非空） |

## Step 2：按形态更新

### CLI 安装（Windows + pwsh）

```bash
pwsh -File <技能目录>\scripts\update.ps1
```

脚本内部自动完成：状态文件备份（`.env`、`.token_key` 等，输出含文件名清单）→
`npx skills update iwp`（按内容哈希判定，无更新则空转）→ 恢复 → 哈希校验 → 清理备份。
**向用户如实转述脚本输出，不代做额外解释或重试。**

### clone / symlink 安装

```bash
git -C <技能目录> pull
```

gitignore 的状态文件不受影响。

### 非 Windows 且 CLI 安装

告知用户：`update.ps1` 仅支持 Windows/pwsh；手动 `npx skills update iwp` 会清空技能目录、删除 `.env` 与凭证（需重新配置并授权）。建议改用 clone 方式或联系维护者补充平台脚本。

## Step 3：更新后核验与提示

1. 汇报完整输出（脚本 stdout 或 git pull 结果）。
2. 运行 `python scripts/preflight.py` 与 `python cli.py auth status`，**以 Step 0 基线为准**按分支核验：
   - **分支一：更新前已配置**（基线含状态文件或 `authorized: true`）——更新后出现
     `env_present: false` 或 `authorized: false` 判定为状态丢失：转述脚本备份目录位置，
     按 `setup-flow.md` 重新配置、`auth-flow.md` 重新授权。
   - **分支二：更新前未配置（全新安装）**（基线无状态文件且 `authorized: false`，或更新后
     `reason: not_configured`）——`env_present: false` 与 `authorized: false` 属预期初始态，
     直接按 `setup-flow.md` 引导首次配置，无需查找备份。
   - 完成判据：更新后输出与 Step 0 基线一致，或已按对应分支处理完毕。
3. 提示：新版本文件已就位，**当前会话仍按旧版指令执行，新会话生效**。
4. 更新失败时：转述错误输出与备份目录位置（脚本输出的失败分支含文件名清单），等用户决定下一步，不擅自重试。
