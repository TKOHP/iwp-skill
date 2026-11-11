# 更新 iwp 技能工作流

用户要求「更新 iwp 技能」时按本文件执行。SKILL.md 只做路由，具体步骤以本文件为准。

## 自动检测（preflight 附带，只提示、不执行）

`python scripts/preflight.py` 的输出可能多一个顶层 `update` 块：

```json
{"checked":true,"available":true,"status":"behind","form":"git",
 "remote_sha":"<40hex>","remote_date":"YYYY-MM-DD",
 "detail":{"modified":2,"missing":0,"stale":1,"extra":0},
 "action":"git_pull","prompt":true}
```

- `status` 表示本地发布集与远端的关系：
  - `behind` 上游有新内容（正常更新场景）
  - `drift` 上游未变、本地文件被改过（提示时说明「本地有改动」）
  - `conflict` 双方都变（提示时说明「本地与上游都有改动」）
  - `synced` 一致；`unknown` 无历史快照，只能判「不一致」
- `available: true` = 本地与远端不一致（事实）；`prompt: true` = 本次要不要提示（同一 `<remote_sha>:<status>` 24h 内最多一次）
- 提示只占回复**正文末尾**一行，按 `status` 与 `form` 选文案：
  - `behind` + `form: "cli"`：`检测到 iwp 技能有更新（<sha7>，<date>）。回复「更新 iwp 技能」即可更新，新会话生效。`
  - `behind` + `form: "git"`：`检测到上游有新提交（<sha7>，<date>）。当前是开发副本，请在技能目录执行 git pull，新会话生效。`
  - `drift` / `conflict`：`本地 iwp 技能与上游不一致（<sha7>，<date>：<modified> 个文件不同）。回复「更新 iwp 技能」查看差异并确认覆盖。`
- 静默是默认：无 `update` 块、`prompt` 非 true 或 `available: false` 时，回复保持原样
- 提示不改变任何行为：**不得自动更新**；用户要求更新时按本文件 Step 0–3 执行，覆盖本地改动前必须先出完整预览
- 探测失败（断网、限流、状态不可写）由脚本自身退避：网络错误 15 分钟后重试，配额耗尽按 `x-ratelimit-reset` 重试，回复中同样保持静默
- 关闭或改仓库：`.env` 设 `IWP_UPDATE_CHECK=0`；`IWP_UPDATE_REPO=owner/repo` 覆盖默认 `TKOHP/iwp-skill`
- 单独诊断：`python scripts/update_check.py`（只读，不写状态、不改变提示闸门，输出含 `diff` 每类前 10 条路径）

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

gitignore 的状态文件不受影响。若 preflight 的 `status` 是 `drift` 或 `conflict`（本地有改动），先 `git -C <技能目录> status --short` 把将被覆盖的文件列进预览，用户确认后再 pull；pull 失败（本地改动挡住快进）时不擅自 `reset --hard`，把冲突文件列给用户决定。

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
