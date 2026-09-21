# OAuth 三方交互详解

> 对应方案文档 `InnovationWorkPlatform-mcp/docs/MCP-OAuth三方改造-方案设计.md` §1.2 流程图 + §2 核心模块设计。

## 三方角色

| 角色 | 在本方案中的实体 | 关键能力 |
|------|-----------------|---------|
| **客户端（Public Client）** | iwp skill（本机 Python 脚本） | PKCE 生成、URL 构造、本地回调 server、token 缓存 |
| **授权服务器（AS）** | IWP MCP server（:8000 公网 + :8001 内部） | 事务管理、code 签发、access_token/refresh_token 签发 |
| **资源所有者（RO）** | IWP 用户（浏览器） | 在 IWP SPA 授权页完成授权确认 |

加上 **IWP 后端** + **IWP 前端 SPA** = 实际是四方参与（IWP 后端作为 OAuth 信任中转）。

## 流程图

```
                   ┌──────────────┐
                   │  iwp skill   │  (1) PKCE 生成 (verifier + challenge)
                   │  (本机)      │  (2) state 生成
                   └──────┬───────┘  (3) build_authorize_url → URL
                          │ URL
                          ▼ (4) ask_user 展示 URL 给用户
                   ┌──────────────┐
                   │  用户浏览器  │
                   └──────┬───────┘
                          ▼ (5) GET /authorize?...&code_challenge&state&resource
                   ┌──────────────┐
                   │  MCP server  │  (6) 校验 client_id/redirect/S256/resource
                   │  (8000)      │  (7) 创建事务 T (tx_id, 5min TTL)
                   └──────┬───────┘
                          ▼ (9) 302 → {IWP_FRONTEND}/oauth-authorize?tx_id=T
                   ┌──────────────┐
                   │  IWP SPA     │  (10) 读 localStorage access_token
                   │  (5173)      │       - 有 → 直接 confirm
                   │              │       - 无 → 渲染 InPageLogin
                   └──────┬───────┘
                          ▼ (11) POST /api/auth/oauth/authorize {tx_id}
                   ┌──────────────┐
                   │  IWP 后端    │  (12) 验证 Bearer + 创建 mcp 独立会话
                   │  (5000)      │  (13) 签发专属 refresh_token
                   │              │  (14) 写 one-time code (otc, 60s TTL)
                   │              │  (15) HMAC 签名 + 背信道推送
                   └──────┬───────┘
                          ▼ (16) POST /internal/oauth/iwp-confirm
                              HMAC 验签 + otc 消费 + 事务置 confirmed
                              + 异步 profile 反查 user_id
                   ┌──────────────┐
                   │  MCP 内部   │
                   │  server(8001)│  (17) 异步失败 → 事务置 failed
                   └─────────────┘
                          ▼ SPA 看到授权成功，跳转
                   ┌──────────────┐
                   │  MCP /oauth  │  (18) 事务已 confirmed → 生成 code
                   │  /callback   │       302 → {redirect_uri}?code=...&state=...
                   └──────┬───────┘
                          ▼ (19) :9999 收到 code + state
                   ┌──────────────┐
                   │  iwp skill   │  (20) 验 state 匹配
                   │  (本机)      │  (21) POST /token code+verifier+resource
                   │              │       → access_token + refresh_token
                   └──────┬───────┘
                          ▼ (22) Fernet 加密缓存到 .token_cache.enc
                          ▼ (23) 后续调 MCP 工具带 Bearer
```

## 关键设计要点

### 1. 凭证全程走服务端背信道

- 浏览器 URL 只出现 `tx_id`（无敏感信息）
- `iwp_refresh_token` 由 IWP 后端通过 `POST /internal/oauth/iwp-confirm` 推送给 MCP
- HMAC-SHA256 签名（共享密钥文件 600）+ otc 单次 60s TTL = 防重放
- 即使浏览器历史 / access log / Referer 暴露，也无法还原 refresh_token

### 2. PKCE 强制

- `verifier` 仅 iwp skill 本地持有
- `code_challenge = base64url(sha256(verifier))`
- 即使 `code` 被截获，没有 `verifier` 也无法兑换 token
- 这是 RFC 7636 的核心安全保证

### 3. resource 参数（RFC 8707）

- iwp skill 构造 authorize URL 时带 `resource={MCP_PUBLIC_BASE_URL}`
- MCP /token 端点强制要求 resource 一致（缺省 400 `invalid_target`）
- 防止 token 被错误地发给其他 RS（confused-deputy）

### 4. aud 校验

- MCP access_token 签发时 `aud=MCP_AUDIENCE`
- `verify_access_token` 强校验 aud 不匹配 → 401
- 即使 token 泄露给其他服务，也无法冒充 MCP 调用方

### 5. 独立会话归属

- MCP 授权创建的 IWP 会话 `session_type="mcp"`，`no_kick=True`
- Web 登出仅删 web 会话；MCP 会话独立存活
- 用户调"下线所有设备"才会杀 MCP（MCP 下次 refresh → IWP 401 → 失效）

### 6. 重用检测

- MCP refresh_token 消费（GET + DEL 原子）
- 消费后写 `mcp_reuse:{hash}` 标记（60s 窗口）
- 60s 内第二次消费 → 触发重用检测 → 吊销同 mcp_session_id 全部 token + 清 IWP binding
- RFC 6749 §6：refresh token 重用即视为 compromise

## 安全性汇总

| 威胁 | 防护 |
|------|------|
| code 截获 | PKCE S256 |
| refresh_token 重用 | 重用检测 + 全吊销 |
| token 跨服务滥用 | resource 校验 + aud 校验 |
| CSRF 攻击 | state 一次性 + Bearer 头免疫 |
| 凭证经浏览器泄漏 | 服务端背信道（无 URL/history/log 痕迹） |
| 身份绑定串号 | user_id + refresh_token 同请求 + profile 反查二次核对 |
| 公开客户端无 secret | PKCE（OAuth 2.1 best practice） |
| 时钟偏差 | HMAC 时间戳 60s 窗口 |