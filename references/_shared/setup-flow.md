# 首次配置工作流（跨路由共享）

> 会话首次调用任何 iwp 工具前执行本流程。本文是 `SKILL.md` 授权铁律第 0 步的完整版；
> 检查全部通过后进入 `auth-flow.md` 授权流程。

## 0. 事前检查（一个脚本覆盖配置 + 依赖）

```bash
python scripts/preflight.py
```

输出 ASCII JSON（`{"ok", "checks", "hints"}`，全过退出码 0）。按 `checks` 分流：

| 检查结果 | 动作 |
|---------|------|
| `ok: true` | 直达第 3 步探测验证（已有 `.env` 的老用户第 2 步自动跳过） |
| `checks.deps` 有 `ok: false` | 走第 1 步 |
| `checks.env_present: false` | 走第 2 步 |

## 1. 依赖缺失 → 自动安装

```bash
pip install -r requirements.txt
```

安装目标固定为 `requirements.txt` 清单（`httpx` / `cryptography`），安装后重跑
`python scripts/preflight.py` 复核。

完成判据：`checks.deps` 两项均为 `ok: true`。

## 2. `.env` 缺失 → agent 引导创建

1. **用 `ask_user` 询问 `MCP_PUBLIC_BASE_URL`**——本地/远程示例值直接写进
   question 正文（如本地 `http://127.0.0.1:8000`、远程 `http://<host>:<port>`），
   其余 KEY（`IWP_CLIENT_ID` / `IWP_REDIRECT_URI` / `IWP_LOCAL_CALLBACK_PORT`）
   按 `.env.example` 默认值即可，无需逐项询问
2. **读 `.env.example` → 写 `.env`**（技能根目录）：仅替换
   `MCP_PUBLIC_BASE_URL` 一行为用户给的值，其余内容原样保留
3. 重跑 `python scripts/preflight.py`

完成判据：`checks.env_present: true`。

## 3. 可达性探测（写完 `.env` 或已有 `.env` 时）

```bash
python scripts/preflight.py --probe
```

探测 `{MCP_PUBLIC_BASE_URL}/.well-known/oauth-authorization-server`（未认证）。

完成判据：`checks.probe.ok: true`（HTTP 200 且响应含 `issuer` 字段）。

probe 失败 = URL 配错或服务未启动：回到第 2 步向用户确认 URL 后重写 `.env`
再探测，通过前不进入授权流程。

## 4. 转入授权

事前检查全过后，按 `references/_shared/auth-flow.md` 执行授权
（`python cli.py auth status` → 未授权则 `auth start` / `auth finish`）。

授权之后出现的连接类错误（不可达 / 421 / `invalid_target`）属于运行期问题，
按 `references/_shared/failure-modes.md` 处理，不走本流程。

## 边界

- 本流程会话内跑一次即可，preflight 全过后重复进入直接落到第 3 步
- 技能更新后 `.env` 丢失（skills CLI 直接更新会清空技能目录）→ 重新进入本流程
  （见 `references/_shared/update-flow.md`）
- probe 的 `issuer` 与 `MCP_PUBLIC_BASE_URL` 不一致时，提示用户核对服务端
  `MCP_PUBLIC_BASE_URL` 配置（历史坑：双协议头 `https://http://`）
