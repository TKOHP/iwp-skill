# iwp skill

> **IWP（创新工作平台）MCP 客户端。**
> 标准 OAuth 2.1 PKCE 流程，无 cookie，无手动粘贴 token。
> 覆盖课题任务与周报两类 MCP 工具（数量以 MCP `tools/list` 为准）。

## 快速开始

### 1. 安装依赖（skill 自动管理）

依赖见 `requirements.txt`（`cryptography`、`httpx` 两个非标准库）。安装：`pip install -r requirements.txt`。

### 2. 配置（环境变量，可选）

| 变量 | 默认 | 说明 |
|------|------|------|
| `MCP_PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | MCP server 公网入口 |
| `IWP_FRONTEND_BASE_URL` | `http://127.0.0.1:5173` | IWP SPA origin（**与 MCP server 端 `IWP_FRONTEND_BASE_URL` 必须一致**） |
| `IWP_CLIENT_ID` | `test-client` | OAuth 客户端 ID（必须注册到 MCP server `MCP_ALLOWED_CLIENTS`） |
| `IWP_REDIRECT_URI` | `http://localhost:9999/callback` | OAuth redirect_uri（必须注册到 MCP `MCP_ALLOWED_REDIRECT_URIS`） |
| `IWP_LOCAL_CALLBACK_PORT` | `9999` | 本地回调端口 |

### 3. 第一次使用

```python
from scripts import auth

# 任何 MCP 工具调用前必须先授权
try:
    auth.ensure_authorized()
except RuntimeError as exc:
    # 错误信息中给出 .oauth_next_step.json 路径
    print(exc)
    # 流程:agent 用 ask_user 展示 URL → 用户点 → poll → finalize
```

完整授权工作流见 `references/_shared/auth-flow.md`。

## 三场景分支等权

| 分支 | 场景 | 参考文件 |
|------|------|---------|
| **tasks** | 课题任务增删改查、日志、课题发现 | `references/tasks/README.md` |
| **reports** | 周报 CRUD、批量、**根据课题任务生成周报** | `references/reports/README.md` |
| **free-mode** | 工具发现、任意工具透传 | `references/free-mode/README.md` |

三分支等权，没有主流程与兜底之分。分支路由指向**参考文件（场景提示词）**，参考文件内部指导脚本使用；**skill 不维护硬编码工具清单**，工具事实来源是 MCP `tools/list`（见 `references/_shared/tool-discovery.md`）。

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
- `references/` - 三分支场景提示词 + 共享约定
- `docs/oauth-flow.md` - OAuth 三方交互详解
- `docs/architecture.md` - 架构决策记录（ADR-009/010 记录本次重构）

## 版本

- v1.0.0（2026-09-21）：从 CCBSkillsHub 独立为 standalone 项目；独立前的迭代记录见原仓库 git 历史。
