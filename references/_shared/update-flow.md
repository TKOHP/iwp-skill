# 更新 iwp 技能工作流

用户要求「更新 iwp 技能」时按本文件执行。SKILL.md 只做路由，具体步骤以本文件为准。

**本文件不判断安装形态。** 形态探测、更新路径选择与成败判定全部由
`scripts/skill_update.py` 完成；本文只描述「运行哪个命令、如何转述、如何核验」。

## 自动检测（preflight 附带，只提示、不执行）

`python scripts/preflight.py` 的输出可能多一个顶层 `update` 块：

```json
{"checked":true,"available":true,"status":"behind","form":"git",
 "remote_sha":"<40hex>","remote_date":"YYYY-MM-DD",
 "detail":{"modified":2,"missing":0,"stale":1,"extra":0},
 "action":"update","prompt":true}
```

- `form` 表示技能目录与上游的绑定方式：
  - `git` 目录含 `.git` 且 origin 等于上游仓库
  - `npx` 目录由 `npx skills list -g` 记录在册（path、来源仓库、tracked 三项同时命中）
  - `bare` 解压安装或手工拷贝（无 `.git`、也不在 npx 记录中），两者不区分
  - `git_foreign` 含 `.git` 但 origin 不是上游仓库（分叉或开发副本）——**不产生提示**
- `status` 表示本地与远端的关系：
  - `behind` 上游有新内容（正常更新场景）
  - `drift` 上游未变、本地文件被改过（提示时说明「本地有改动」）
  - `conflict` 双方都变（提示时说明「本地与上游都有改动」）
  - `synced` 一致；`unknown` 无历史快照，只能判「不一致」
- `available: true` = 本地与远端不一致（事实）；`prompt: true` = 本次要不要提示
  （同一 `<remote_sha>:<status>` 24h 内最多一次）
- `action` 固定为 `update`：更新路径由程序按形态自行选择，不要据此手工执行 git 或 npx
- 提示只占回复**正文末尾**一行，按 `status` 与 `form` 选文案：
  - `behind` + `form: "git"`：`检测到上游有新提交（<sha7>，<date>）。当前是开发副本，更新走 git pull，新会话生效。`
  - `behind`（`npx` / `bare`）：`检测到 iwp 技能有更新（<sha7>，<date>）。回复「更新 iwp 技能」即可更新，新会话生效。`
  - `drift` / `conflict`：`本地 iwp 技能与上游不一致（<sha7>，<date>：<modified> 个文件不同）。回复「更新 iwp 技能」查看差异并确认覆盖。`
- 静默是默认：无 `update` 块、`prompt` 非 true 或 `available: false` 时，回复保持原样
- 提示不改变任何行为：**不得自动更新**；用户要求更新时按本文件 Step 0–3 执行
- 探测失败（断网、限流、状态不可写）由脚本自身退避：网络错误 15 分钟后重试，配额耗尽按
  `x-ratelimit-reset` 重试，回复中同样保持静默
- 关闭或改仓库：`.env` 设 `IWP_UPDATE_CHECK=0`（**总开关**：检测与执行一并关闭——不探测、不提示，
  `scripts/skill_update.py` 的 `--preview` / `--apply` 都直接返回 `error: update_disabled` 与退出码 6，
  不发任何请求）；`IWP_UPDATE_REPO=owner/repo` 覆盖默认 `TKOHP/iwp-skill`
- 单独诊断：`python scripts/update_check.py`（只读，不写状态、不改变提示闸门，输出含
  `form` / `form_reason` / `form_evidence` 与 `diff` 每类前 10 条路径）

## Step 0：记录更新前基线

任一次更新开始前先记录以下两项，作为 Step 3 的唯一对照物：

1. 技能目录下的状态文件实际清单。**唯一来源**是 `.gitignore` 中「不含通配符（`*` `?` `[`）
   且不以 `/` 结尾」的条目，与目录中实际存在的文件求交；直接读 `--preview` 输出的
   `keepalive` 字段即可，本文不维护第二份清单。
2. `python cli.py auth status` 的 `authorized` 值（未授权时记录其 `reason` 字段）。

完成判据：两项均已记录，可用于更新后逐项对照。禁止以记忆或另一份副本（dev 仓库、
其他安装位置）的状态代替本目录基线。

## Step 1：预览（只读）

```bash
python scripts/skill_update.py --preview
```

输出 `plan` 节点，关键字段：

| 字段 | 含义 |
|------|------|
| `form` / `form_reason` | 形态与判定依据（`form_evidence` 含 npx 记录、`other_copies` 等同仓库其他副本） |
| `status` | `synced` / `behind` / `drift` / `conflict` / `unknown` / `no_evidence` |
| `update_action` | 程序将要执行的路径：`git_pull` / `npx_update` / `zip_overlay` |
| `needs_confirm` | 本次是否有覆盖风险，需要用户先确认 |
| `detail` / `dirty_paths` | 差异计数与脏文件预览（最多 10 条） |
| `keepalive` | 更新时会备份回填的状态文件清单 |

按 `status` 与退出码分流：

- `available: false`（退出码 2）：已是最新或无可比证据 → 告知用户，结束，不执行任何改动
- `error: update_disabled`（退出码 6）：`.env` 的 `IWP_UPDATE_CHECK=0`，更新能力被整体关闭 → 说明这是
  显式配置而非故障；需要更新时先把该开关改为 1，**不要**绕过脚本手工执行更新
- `needs_confirm: true`（`drift` / `conflict` / `unknown`，或形态为 `npx`）→ 必须先把
  `status`、`detail`、`update_action` 与「更新会覆盖什么」写进本条回复正文，取得用户确认后
  再进入 Step 2 并加 `--force`。`npx` 形态无法判定本地文件是否被改动，一律按需确认处理
- `error: foreign_repo` / 退出码 5：目录是分叉或链接安装 → 说明原因，建议手工处理，不执行更新
- `error: probe_failed`（退出码 4）：断网或限流 → 告知用户当前无法判断，不重试

## Step 2：执行更新

```bash
python scripts/skill_update.py --apply          # 无覆盖风险时
python scripts/skill_update.py --apply --force  # 用户已在 Step 1 确认覆盖风险
```

程序按 `form` 自行分派，三条路径的差别**不需要**在回复里展开：

| 形态 | 实际动作 |
|------|----------|
| `git` | `git pull --ff-only`（本地改动阻挡快进时只报告，不 `reset --hard`） |
| `npx` | 备份保活文件 → `npx skills update iwp -g` → 回填 → 逐文件哈希校验 |
| `bare` | 按远端 sha 下载定版 zip → 写回滚副本 → 原子覆盖 → 删除「上游已删且本地未改」的文件 |

**不要手工执行上表中的任何一条命令**：手工调用 `npx skills update` 会整目录替换并删除
`.env`、凭证与其他未跟踪文件。

## Step 3：更新后核验与提示

1. 汇报完整输出，至少包含 `steps` 每项的 `ok`、`post_check`、以及 `error` / `rollback` /
   `backup_dir`（若有）。`post_check.ok` 为 `false` 时转述 `expected` 与 `actual`、不一致的
   文件清单，不自动重试、不手工修补。
2. 用 Step 0 基线核验状态文件与授权，按分支处理：
   - **分支一：更新前已配置**（基线含状态文件或 `authorized: true`）——更新后出现
     `env_present: false` 或 `authorized: false` 判定为状态丢失：转述脚本备份目录位置，
     按 `setup-flow.md` 重新配置、`auth-flow.md` 重新授权。
   - **分支二：更新前未配置（全新安装）**（基线无状态文件且 `authorized: false`）——
     `env_present: false` 与 `authorized: false` 属预期初始态，直接按 `setup-flow.md`
     引导首次配置，无需查找备份。
   - 完成判据：更新后输出与 Step 0 基线一致，或已按对应分支处理完毕。
3. 提示：新版本文件已就位，**当前会话仍按旧版指令执行，新会话生效**。
4. 更新失败时：转述 `error`、`steps` 中失败项与备份/回滚目录位置，等用户决定下一步，
   不擅自重试。

## 退出码

| 退出码 | 含义 |
|--------|------|
| 0 | 预览完成（无风险）或更新成功 |
| 1 | 更新失败（`error` 给出原因；`rollback` / `backup_dir` 给出现场位置） |
| 2 | 已是最新，无需更新 |
| 3 | 需要用户先确认覆盖风险（Step 2 需加 `--force`） |
| 4 | 探测失败（断网、限流、状态不可写） |
| 5 | 拒绝执行（分叉仓库、链接安装、保活清单不可读） |
| 6 | 已被配置禁用（`.env` 的 `IWP_UPDATE_CHECK=0`，未发起任何请求） |

## 非 Windows

`scripts/update.ps1` 仅支持 Windows/pwsh；`python scripts/skill_update.py` 本身与平台无关，
覆盖式路径（`bare`）在 macOS/Linux 上同样可用，`git` 路径依赖本机 git。无论哪种平台，
都不要手工执行 `npx skills update iwp`——它会清空技能目录、删除 `.env` 与凭证。
