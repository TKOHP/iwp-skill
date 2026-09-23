# 授权工作流（跨路由共享）

> 任何路由、任何工具调用之前必须完成授权检查。本文是 `SKILL.md` 授权铁律的完整版。
> 授权的前提是事前检查（依赖 + `.env`）已通过——见 `setup-flow.md`。

## 1. 授权态检查

```bash
python cli.py auth status
```

- 返回 `authorized: true` → 直接进入路由工作流
- 返回 `authorized: false` → 走第 2 节三方流程
- status 内部会先用 refresh_token 静默续期再判断,避免把"30 分钟 access_token
  过期"误判为需要重授权

## 2. 三方授权流程（agent 逐步执行）

CLI 把原多步 python 流程收敛为两条命令。`auth start` **不会自动完成授权**:

1. **运行 `python cli.py auth start`**,拿到 `authorize_url` 与 `state`
2. **用 `ask_user` 工具把 `authorize_url` 展示给用户**——不要用 print 让用户手动复制
3. 用户在浏览器完成三方流程：
   `MCP server /authorize` → IWP SPA `/oauth-authorize?tx_id=...` →（已登录一键确认；未登录页内登录）→ 确认授权 → 302 回 `http://localhost:9999/callback?code=...&state=...`
   本地 callback server 自动接收 code
4. **运行 `python cli.py auth finish`**——内部完成:轮询回调(默认 300s)→
   校验 state → code + PKCE verifier 换 token → 写加密缓存 → 清理中间产物
   （`.oauth_next_step.json` / `.callback_result.json`）

浏览器最后一跳死亡但授权事务已确认的恢复路径:从 `/oauth/callback` 的 302
里拿到 code 后,用 `python cli.py auth finish --code <code>` 手动注入,无需重走。

## 3. 时序与异常

| 环节 | 约束 |
|------|------|
| 总超时 | 5 分钟内必须完成；`auth finish` 轮询超时返回 `callback_timeout` 错误，提示用户重试 |
| 本地回调端口 | `127.0.0.1:9999` 被占用时 `auth start` 显式报错并给出占用进程信息；让用户处理占用后重试，**不要换端口硬试** |
| state 校验 | `auth finish` 内部校验回调 `state` 与授权事务一致；不一致返回 `state_mismatch`，按 CSRF 处理，终止流程 |
| 用户拒绝授权 | `auth finish` 返回 `auth_denied` → 报告用户，不重试不猜测 |

## 4. token 失效与自动续期

- 调工具返回 401 → CLI/client 自动用 refresh_token 续期并重试 1 次
- refresh 也失败（错误 kind 为 `auth_expired`）→ **必须**引导用户重新授权：
  1. `python cli.py auth invalidate`
  2. `python cli.py auth start` 重新走第 2 节流程
- 不要"猜 token"、"绕过授权"或默默重试——绝对禁止

## 5. 撤销授权（用户要求下线时）

```bash
python cli.py auth invalidate   # 仅清本地缓存凭证
```

如需同时撤销服务端凭证（MCP /revoke），调用 `scripts.client.revoke_current_token()`。

注意：IWP 的"退出登录"（Web 会话）不影响 MCP 会话；MCP 凭证只能通过本入口或"下线所有设备"清除。
