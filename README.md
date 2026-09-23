# iwp skill

> **IWP（创新工作平台）MCP 客户端。**
> 标准 OAuth 2.1 PKCE 流程，无 cookie，无手动粘贴 token。
> 覆盖课题任务与周报两类 MCP 工具（数量以 MCP `tools/list` 为准）。

## 快速开始

### 1. 获取技能

**首选：Vercel skills CLI**（需 Node ≥ 22.20，Windows/Linux/macOS 均可用）：

```bash
npx skills add TKOHP/iwp-skill -g -y
```

CLI 自动检测本机已安装的 agent（Claude Code / Cursor / Codex / Gemini CLI 等），将技能装入对应技能目录并记录安装来源。

**备选：git clone**（无 Node 环境或希望直接跟随仓库）：

```bash
git clone https://github.com/TKOHP/iwp-skill.git
# 将仓库目录放入所用 agent 的技能目录，或建立符号链接
```

**更新到最新版**：

最省事的方式：直接对 agent 说一句「**更新 iwp 技能**」——agent 会按技能内置的更新工作流（`references/_shared/update-flow.md`）自动识别安装形态并执行（Windows CLI 用户走无损脚本，clone 用户自动 `git pull`）。手动方式如下：

```bash
# 推荐：无损更新（自动备份并恢复 .env 与凭证，更新后核验授权）
pwsh -File <技能目录>\scripts\update.ps1

# clone 用户（在仓库目录内，gitignore 的状态文件不受影响）
git pull
```

> 直接运行 `npx skills update iwp` 会**清空技能目录后重拷**，`.env` 与凭证缓存（`.token_key` 等）会被删除，导致重新配置并重新授权——CLI 用户请使用上面的无损更新脚本。

版本发布模型：push 到 main 即发布新版本（内容哈希判定，无 semver）；当前版本以 `SKILL.md` frontmatter 的 `metadata.version` 为准。

### 2. 安装依赖

依赖见 `requirements.txt`（`cryptography`、`httpx` 两个非标准库）。技能会自动管理：首次使用时 agent 运行事前检查（`python scripts/preflight.py`），发现缺失即执行 `pip install -r requirements.txt`；也可提前手动安装。

### 3. 配置（.env 文件）

技能根目录的 `.env` 是唯一外部配置来源，shell 环境变量不再被读取。**推荐交给 agent 自动完成**：直接开始使用或说一句「配置 iwp」，agent 会按技能内置的配置工作流（`references/_shared/setup-flow.md`）询问 `MCP_PUBLIC_BASE_URL` 并以 `.env.example` 为底稿创建 `.env`；也可手动复制 `.env.example` 为 `.env` 后按需修改。

| 变量 | 默认 | 说明 |
|------|------|------|
| `MCP_PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | MCP server 公网入口（唯一必须按环境确认的项） |
| `IWP_CLIENT_ID` | `test-client` | OAuth 客户端 ID（必须注册到 MCP server `MCP_ALLOWED_CLIENTS`） |
| `IWP_REDIRECT_URI` | `http://localhost:9999/callback` | OAuth redirect_uri（必须注册到 MCP `MCP_ALLOWED_REDIRECT_URIS`） |
| `IWP_LOCAL_CALLBACK_PORT` | `9999` | 本地回调端口 |

### 4. 第一次使用

agent 在任何工具调用前会先做事前检查（依赖 + `.env`）并按需引导配置，随后进入授权：

```bash
# 任何 MCP 工具调用前必须先授权
python cli.py auth status
# 未授权 → 走三方流程:auth start 输出授权链接
python cli.py auth start
# agent 用 ask_user 展示 authorize_url → 用户浏览器确认 →
python cli.py auth finish
```

完整授权工作流见 `references/_shared/auth-flow.md`。

## 路由表

| 用户想做什么 | 路由 | 类型 | 参考文件 |
|--------------|------|------|---------|
| 课题任务增删改查、日志、课题发现 | **tasks** | 业务 | `references/tasks/README.md` |
| 周报 CRUD、批量、**根据课题任务生成周报** | **reports** | 业务 | `references/reports/README.md` |
| 工具发现、任意工具透传 | **free-mode** | 业务 | `references/free-mode/README.md` |
| 更新 iwp 技能本身 | **update** | 维护 | `references/_shared/update-flow.md` |

所有路由平权，没有主流程与兜底之分（free-mode 的通配透传是路由模式，不是层级兜底）。路由指向**参考文件（场景提示词 / 工作流）**，参考文件内部指导脚本使用；**skill 不维护硬编码工具清单**，工具事实来源是 MCP `tools/list`（见 `references/_shared/tool-discovery.md`）。命名演进见 `docs/architecture.md` ADR-012。

## 架构亮点

- **PKCE S256**：本地客户端无 client_secret，PKCE 保证 code 截获不可兑换。
- **三方 OAuth**：iwp skill → IWP SPA → IWP 后端 → MCP server；凭证全程走服务端 HMAC 背信道，浏览器只见无敏感内容的关联 ID。
- **独立会话**：MCP 授权使用 IWP 独立会话（`session_type="mcp"`），Web 登出不杀 MCP 绑定。
- **RFC 7009 + RFC 8707**：支持 token 吊销 + resource 参数强校验。
- **去封装 + 动态发现**（2026-09-20）：scripts 只留基础设施（auth / client / token_store / swagger_meta），场景知识单点收进参考文件，消除 skill 与 MCP 的版本漂移面。

## 错误码速查

| MCP 错误码 | 含义 | 客户端动作 |
|-----------|------|----------|
| `iwp_credential_expired` | IWP refresh_token 失效 | 清缓存 + 引导重新授权 |
| `mcp_token_invalid` | MCP access_token 无效 | 清缓存 + 引导重新授权 |
| `permission_denied` | 权限不足 | 提示用户检查 IWP 角色权限 |
| `rate_limited` | 限流 | 退避后重试 |
| `upstream_error` / `upstream_timeout` | 上游 Flask 异常 | 退避后重试 |
| `validation_error` | 入参错误 | 检查参数 |
| `not_found` | 资源不存在 | 检查 ID |

完整翻译见 `references/_shared/failure-modes.md`。

## 相关文档

- `SKILL.md` - skill 路由器入口
- `references/` - 各路由场景提示词 / 工作流 + 共享约定
- `docs/oauth-flow.md` - OAuth 三方交互详解
- `docs/architecture.md` - 架构决策记录（ADR-009/010 记录本次重构）

## 版本历史

当前版本以 `SKILL.md` frontmatter 的 `metadata.version` 为准（单一事实来源），此处仅追加历史条目。

- v1.1.1（2026-09-23）：修复更新工作流的误导性信号——`update.ps1` 状态清单精确枚举且输出带文件名；`auth status` 新增 `reason` 字段（`not_configured` / `expired_or_invalid`）；`update-flow.md` 新增 Step 0 更新前基线与 Step 3 双分支核验；移除用户安装中不存在的 `.env.local` / `.env.server` 模板引用与 dev 测试文件。
- v1.1.0（2026-09-22）：接入 Vercel skills CLI 分发；快速开始新增获取/更新指导与无损更新脚本 `scripts/update.ps1`；SKILL.md 增加版本元数据与「更新技能」路由（`references/_shared/update-flow.md`）。
- v1.0.0（2026-09-21）：从 CCBSkillsHub 独立为 standalone 项目；独立前的迭代记录见原仓库 git 历史。
