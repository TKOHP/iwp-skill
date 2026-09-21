# 授权工作流（跨分支共享）

> 任何分支、任何工具调用之前必须完成授权检查。本文是 `SKILL.md` 授权铁律的完整版。

## 1. 授权态检查

```python
from scripts import auth, token_store

tokens = token_store.load()
if not tokens or not token_store.is_access_token_valid(tokens):
    auth.ensure_authorized()
```

- `token_store.load()` 读 Fernet 加密缓存（`.token_cache.enc`，密钥 `.token_key`，权限 600）
- 缓存有效 → 直接进入分支工作流
- 缓存缺失/过期 → `ensure_authorized()` 抛 `RuntimeError`（含 `authorize_url` 与 `state`），并把中间产物写到 `.oauth_next_step.json`

## 2. 三方授权流程（agent 逐步执行）

`ensure_authorized()` **不会自动完成授权**。它抛错后，agent 必须：

1. **读取 `.oauth_next_step.json`**，取 `authorize_url` 与 `state`
2. **用 `ask_user` 工具把 `authorize_url` 展示给用户**——不要用 print 让用户手动复制
3. 用户在浏览器完成三方流程：
   `MCP server /authorize` → IWP SPA `/oauth-authorize?tx_id=...` →（已登录一键确认；未登录页内登录）→ 确认授权 → 302 回 `http://localhost:9999/callback?code=...&state=...`
   本地 callback server（`auth.run_local_callback_server`）接收 code 并落盘 `.callback_result.json`
4. **agent 调 `auth.poll_callback_result(timeout_s=300)`** 等待并读取 code（同进程阻塞轮询；不要在别的进程里等）
5. **agent 调 `auth.finalize_authorization(code=..., verifier=...)`** 用 code + PKCE verifier 换 token 并写入加密缓存

`finalize_authorization` 成功后会自动清理授权中间产物（`.oauth_next_step.json` / `.callback_result.json` / `.bak`）。

## 3. 时序与异常

| 环节 | 约束 |
|------|------|
| 总超时 | 5 分钟内必须完成；`poll_callback_result()` 超时抛 `RuntimeError`，提示用户重试 |
| 本地回调端口 | `127.0.0.1:9999` 被占用时 `ensure_authorized()` 显式报错并给出占用进程信息；让用户处理占用后重试，**不要换端口硬试** |
| state 校验 | callback 的 `state` 必须与 `.oauth_next_step.json` 中一致；不一致按 CSRF 处理，终止流程 |
| 用户拒绝授权 | callback 收到 error → 报告用户，不重试不猜测 |

## 4. token 失效与自动续期

- 调工具返回 401 → `scripts/client.py` 自动用 refresh_token 续期并重试 1 次
- refresh 也失败（`McpAuthExpiredError` / `iwp_credential_expired`）→ **必须**引导用户重新授权：
  1. `token_store.invalidate()`
  2. `auth.ensure_authorized()` 重新走第 2 节流程
- 不要"猜 token"、"绕过授权"或默默重试——绝对禁止

## 5. 撤销授权（用户要求下线时）

```python
from scripts.client import revoke_current_token
revoke_current_token()  # 调 MCP /revoke + 清本地缓存
```

注意：IWP 的"退出登录"（Web 会话）不影响 MCP 会话；MCP 凭证只能通过本入口或"下线所有设备"清除。
