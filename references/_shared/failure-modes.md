# 错误码翻译表（跨分支）

> 本文档定义 MCP 错误码到 iwp skill 用户可见提示的翻译规则。
> 适用于 tasks / reports / free-mode 三个分支。

## 错误源分类

### 1. OAuth 协议层（本地）

| 场景 | 表现 | 处理 |
|------|------|------|
| 无 token 缓存 | `auth.ensure_authorized()` 抛 RuntimeError | agent 用 ask_user 展示 authorize_url |
| 本地 9999 端口被占用 | `ensure_authorized()` 报错含 `占用进程` 提示 | 用户检查并杀掉占用进程后重试 |
| 用户 5min 内未完成授权 | `poll_callback_result()` 超时 | agent 显示"超时"，提示重试 |
| state 不匹配 | callback 返回 `state_mismatch` | 显示"安全校验失败，可能是 CSRF 攻击" |
| 时间戳偏差 > 60s | iwp_confirm 返回 401 | IWP 端时钟问题；联系管理员 |

### 2. MCP server 端（OAS / 协议层）

| MCP 错误码 | 含义 | 用户提示 |
|-----------|------|---------|
| `invalid_target` | resource 不匹配或缺失 | "OAuth resource 参数错误，请检查 MCP_PUBLIC_BASE_URL 配置" |
| `invalid_grant` | code / refresh_token 无效或已使用 | "授权凭证无效或已被使用，请重新授权" |
| `unsupported_grant_type` | grant_type 不在白名单 | "OAuth grant 类型不支持（一般不会出现）" |

### 3. MCP server 端（鉴权层）

| 错误码 | 含义 | 用户提示 |
|-------|------|---------|
| `mcp_token_invalid` | MCP access_token 失效 / 签名错 / aud 不匹配 | "MCP 凭证已失效，请重新授权" |
| `iwp_credential_expired` | IWP refresh_token 失效（用户 Web 登出 / 下线所有设备） | "IWP 登录已过期，请重新授权" |

### 4. MCP server 端（业务层）

| 错误码 | 含义 | 用户提示 |
|-------|------|---------|
| `permission_denied` | 权限不足（IWP 角色权限码缺失） | "权限不足，请联系管理员检查您在 IWP 的角色权限" |
| `validation_error` | 入参错误（主服务 400） | "参数错误：XXX（请检查入参）" |
| `not_found` | 资源不存在 | "资源不存在（ID 是否正确？）" |
| `upstream_error` | 上游 Flask 5xx | "上游 IWP 服务异常，请稍后重试" |
| `upstream_timeout` | 上游 Flask 超时 | "上游 IWP 服务响应超时，请稍后重试" |
| `version_mismatch` | server 协议版本与 client 支持集无交集 | "MCP 协议版本不兼容（server: X，client 支持 2025-03-26+），请升级 server 或 skill" |
| `protocol` | 响应结构/会话异常（缺 session 头、非 JSON、input_required 等） | "MCP 协议交互异常，运行 python -m scripts.protocol_selfcheck 诊断" |

### 5. MCP server 端（限流）

| 错误码 | 含义 | 用户提示 |
|-------|------|---------|
| `rate_limited` | 触发限流（每分钟/每日/工具维度） | "调用过于频繁，请稍后 30 秒再试" |

### 6. 网络层（本地）

| 场景 | 表现 | 处理 |
|------|------|------|
| MCP 不可达 | `httpx.ConnectError` | "MCP server 不可达，请检查服务是否启动（127.0.0.1:8000）" |
| MCP 响应超时 | `httpx.ReadTimeout` | "MCP server 响应超时，请稍后重试" |
| MCP 响应非 JSON | JSONDecodeError | "MCP 返回异常，请联系管理员" |

## 重试策略（自动）

| 错误类别 | 重试 | 备注 |
|---------|------|------|
| 网络层（ConnectError / ReadTimeout） | 自动重试 1 次（200ms 退避） | `scripts/client.py:_post_raw` 实现 |
| MCP 5xx | 自动重试 1 次 | 同上 |
| MCP 401 | refresh_token 重试 1 次；失败 → McpAuthExpiredError | 需用户重新授权 |
| 4xx（非 401）| 不重试 | 直接报错 |

## 401 + 失效检测

iwp skill 的 401 处理流程：

1. 调 MCP 工具返回 401 → `scripts/client.py:_post_raw` 捕获
2. 调 `auth.auto_refresh_if_needed(force=True)`
3. 若 refresh 成功：用新 token 重试 1 次
4. 若 refresh 失败：`token_store.invalidate()` + 抛 `McpAuthExpiredError`
5. agent 捕获 `McpAuthExpiredError` → 调 `auth.ensure_authorized()` 重新走 OAuth

## 何时停止重试 + 引导用户

任何 `McpAuthExpiredError` / `RuntimeError("凭证...")` → 必须：

1. 不要默默重试
2. 调用 `auth.ensure_authorized()` 让用户重新授权
3. 用 ask_user 展示 URL

不要尝试"猜 token"或"绕过授权"——绝对禁止。