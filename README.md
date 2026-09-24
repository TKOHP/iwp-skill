# iwp skill

> **IWP（创新工作平台）MCP 客户端。**
> 用一句话管理课题任务、写周报：标准 OAuth 2.1 授权，浏览器一键确认，无需手动粘贴任何 token。

## 快速开始

### 1. 安装技能

需要 Node ≥ 22.20（Windows / Linux / macOS 均可）：

```bash
npx skills add TKOHP/iwp-skill -g -y
```

CLI 会自动检测本机已安装的 agent（Claude Code / Cursor / Codex / Gemini CLI 等），把技能装入对应技能目录。

> 没有 Node 环境也可以 `git clone` 本仓库，把目录放入所用 agent 的技能目录（或建立符号链接）。

### 2. 配置技能

对 agent 说一句「**配置 iwp**」（或 `/iwp 安装`），其余交给它：

- 自动创建 `.env` 并安装依赖——唯一需要你确认的是 MCP server 地址，其余配置都有默认值
- 首次使用会自动引导授权：浏览器自动打开，登录并确认后回到对话即可

### 3. 更新技能

对 agent 说一句「**更新 iwp 技能**」（或 `/iwp 更新`）：

- 自动识别安装形态并无损更新：`.env` 与授权凭证自动备份、恢复、核验，不会丢失
- 更新在**新会话**生效；当前版本见 `SKILL.md` 的 `metadata.version`

## 使用案例

四个典型场景的完整交互演示（你说什么 → agent 做什么 → 你会看到什么）见 **[examples/README.md](examples/README.md)**：课题任务管理、周报管理、自由模式、授权与排障。

## 路由表

| 用户想做什么 | 路由 | 类型 | 参考文件 |
|--------------|------|------|---------|
| 课题任务增删改查、日志、课题发现 | **tasks** | 业务 | `references/tasks/README.md` |
| 周报 CRUD、批量、**根据课题任务生成周报** | **reports** | 业务 | `references/reports/README.md` |
| 工具发现、任意工具透传 | **free-mode** | 业务 | `references/free-mode/README.md` |
| 更新 iwp 技能本身 | **update** | 维护 | `references/_shared/update-flow.md` |
| 首次使用 / 配置 / 安装依赖 | **setup** | 维护 | `references/_shared/setup-flow.md` |

所有路由平权，没有主流程与兜底之分（free-mode 的通配透传是路由模式，不是层级兜底）。工具清单不做硬编码，以 MCP `tools/list` 实时发现为准。

## 架构亮点

- **PKCE S256**：本地客户端无 client_secret，PKCE 保证 code 截获不可兑换。
- **三方 OAuth**：iwp skill → IWP SPA → IWP 后端 → MCP server；凭证全程走服务端 HMAC 背信道，浏览器只见无敏感内容的关联 ID。
- **独立会话**：MCP 授权使用 IWP 独立会话（`session_type="mcp"`），Web 登出不杀 MCP 绑定。
- **RFC 7009 + RFC 8707**：支持 token 吊销 + resource 参数强校验。
- **去封装 + 动态发现**（2026-09-20）：scripts 只留基础设施（auth / client / token_store / swagger_meta），场景知识单点收进参考文件，消除 skill 与 MCP 的版本漂移面。

## 遇到问题怎么办

| 症状 | 你可以做什么 |
|------|-------------|
| 授权异常（过期、超时、浏览器没弹出） | 说「诊断 iwp」，agent 会跑一轮聚合体检并给出处置建议 |
| 工具调用报错 | 把报错留在对话里即可，agent 会翻译错误并按规则处置 |
| 更新后想确认状态 | 说「诊断 iwp」；配置或授权确有丢失时说「配置 iwp」重配 |

面向 agent 的完整错误码对照与处置表见 `references/_shared/failure-modes.md`。

## 相关文档

- `SKILL.md` - skill 路由器入口
- `examples/` - 使用案例（按场景的交互演示）
- `references/` - 各路由场景提示词 / 工作流 + 共享约定
- `docs/oauth-flow.md` - OAuth 三方交互详解
- `docs/architecture.md` - 架构决策记录（ADR-009/010 记录本次重构）

## 版本历史

当前版本以 `SKILL.md` frontmatter 的 `metadata.version` 为准（单一事实来源），此处仅追加历史条目。

- v1.2.0（2026-09-24）：回声授权（Echo Auth）——授权推进以回调到达为准，无需用户口头确认；`auth finish` 改为单查状态契约（`waiting`/`completed`/`callback_dead`）并新增 `--wait` 长轮询；`auth start` 自动打开浏览器（`IWP_AUTO_OPEN` 可关，失败静默降级）；回调服务过期主动引导重新授权；新增 `auth doctor` 聚合体检；删除死常量并修正错误注释（方案见 `docs/回声授权-方案设计.md`）。
- v1.1.1（2026-09-23）：修复更新工作流的误导性信号——`update.ps1` 状态清单精确枚举且输出带文件名；`auth status` 新增 `reason` 字段（`not_configured` / `expired_or_invalid`）；`update-flow.md` 新增 Step 0 更新前基线与 Step 3 双分支核验；移除用户安装中不存在的 `.env.local` / `.env.server` 模板引用与 dev 测试文件。
- v1.1.0（2026-09-22）：接入 Vercel skills CLI 分发；快速开始新增获取/更新指导与无损更新脚本 `scripts/update.ps1`；SKILL.md 增加版本元数据与「更新技能」路由（`references/_shared/update-flow.md`）。
- v1.0.0（2026-09-21）：从 CCBSkillsHub 独立为 standalone 项目；独立前的迭代记录见原仓库 git 历史。
