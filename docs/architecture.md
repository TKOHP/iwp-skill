# 架构决策记录（ADR）

> 本文档记录 iwp skill 实施过程中的关键架构决策及理由。

## ADR-001：路由分三分支等权

**决策**：iwp skill 采用 router-style 设计，tasks / reports / free-mode 三分支等权。

**理由**：
- 用户从自然语言分配，三类操作（任务 / 周报 / 自由）体量相当
- 未来扩展（新增 MCP 工具）时，对称扩展到三分支
- 不要让 free-mode 当作"主流程兜底"——它是与 tasks/reports 等权的子功能

**替代方案**：
- 单文件封装所有 20 个工具 → 太扁平，函数命名冲突
- 一个 "tasks + reports" 合并的"主工作流" + 一个"自由"兜底 → 隐含主次等级

**反例**（不推荐）：
- 把 `call_any` 暴露成"all-in-one"，让所有调用都走它 → 失去 workflows 的语义化优势

---

## ADR-002：本地回调 127.0.0.1 + 后台进程

**决策**：iwp skill 的 OAuth callback 监听 `127.0.0.1:9999`，通过 subprocess 后台启动 callback server。

**理由**：
- OAuth 2.1 公开客户端不能用 HTTPS（无证书），localhost 是 RFC 8252 推荐
- 单脚本 5min 阻塞等待会撞 shell 120/300s 超时（用户档案明确禁止）
- agent 分步驱动：调 `ensure_authorized` → 拿到 URL → ask_user → 等 poll → finalize

**替代方案**：
- 同步 `run_in_background` 但不拆 agent 步骤 → 单脚本超时
- 用 `tokio` / `asyncio` 异步等待 + 同一个 Python 进程 → 复杂度过高

**关键约束**：
- 端口被占用时显式报错（让用户杀掉占用进程）——不自动换端口（用户会困惑）

---

## ADR-003：token Fernet 加密缓存

**决策**：MCP token 缓存到 `.token_cache.enc`，Fernet（AES-128-CBC + HMAC-SHA256）加密。

**理由**：
- 与 MCP server 端 `iwp_credentials.py` 风格一致（对称加密 + Fernet）
- 密钥分离（`.token_key`，权限 600）；丢失密钥 = 重新授权（无业务损失）
- 比"只放 localStorage 等价位置"更稳——脚本可重启读取

**替代方案**：
- OS keyring（Windows Credential Manager / macOS Keychain）→ 跨平台一致性差
- 不加密直接放文件 → 泄露面太大

---

## ADR-004：HMAC 背信道 + 服务端 otc

**决策**：IWP 后端 → MCP server 的 `iwp_refresh_token` 传递走 HMAC-SHA256 签名 + otc 单次消费，**不**通过浏览器 URL。

**理由**：
- 浏览器 URL/history/log/Referer 全部不可见
- HMAC 时间戳 60s + otc 60s TTL 双重防重放
- 即使 MITM 截获 `tx_id`（5min 内），也换不出 token

**替代方案**：
- 浏览器跳转携带 token → access log / Referer 必然暴露（不可接受）
- 服务器直接共享 Redis / 数据库 → 跨服务耦合，过期清理复杂

---

## ADR-005：PKCE 强制 S256

**决策**：iwp skill 仅用 `code_challenge_method=S256`，不允许 plain。

**理由**：
- S256 是 RFC 7636 的唯一推荐方式
- iwp skill 作为 OAuth 公开客户端（无 client_secret），PKCE 是必需
- plain 仅用于 legacy 兼容，本方案是绿色新建

**替代方案**：
- plain → verifier 明文，风险等同于无 PKCE

---

## ADR-006：自动 refresh + 失败 → 重新授权

**决策**：`scripts/client.py`（现协议自适应实现，见 ADR-011）在收到 MCP 401 时自动尝试 refresh_token，失败后清缓存 + 抛 `McpAuthExpiredError`。

**理由**：
- 30min access_token 寿命下，每次手动 refresh 体验差
- 自动 retry + 透明恢复对 agent 透明
- 但 refresh 失败 → 必须重新授权（不允许"猜 token"或"绕过授权"）

**关键约束**：
- 重用检测触发时（同 mcp_session_id 已被吊销）→ refresh 一定失败 → 走重新授权流程
- 不允许把"refresh 失败"当作"网络抖动"而默默重试 N 次

---

## ADR-007：MCP 内部端点独立端口（8001）

**决策**：MCP server 同时监听两个端口：8000 公网（oauth/metadata/mcp）+ 8001 内部（仅 `/internal/oauth/iwp-confirm`）。

**理由**：
- nginx 不能反代 `/internal/*`（部署约束）
- 独立端口 + 防火墙规则 = 网络层强约束
- 单进程双端口实现简单（asyncio.gather）

**替代方案**：
- 单端口（8000）+ Host 头分流 → 无 TLS 终结时不可行
- 独立进程 → 资源浪费 + 一致性维护复杂

---

## ADR-008：iwp skill 客户端 client_id 静态白名单

**决策**：iwp skill 用静态 `client_id=test-client`，注册到 MCP `MCP_ALLOWED_CLIENTS`。

**理由**：
- 本地 skill 无法托管 `/.well-known/oauth-client.json`（CIMD）
- 静态白名单足够（当前只有 1 个客户端）
- 阶段 2 引入 CIMD 拉取（方案 §十 演进信号触发）

**未来扩展**：
- 第三方 MCP host（Claude Desktop 等）需走 CIMD——MCP 端加 CIMD 拉取逻辑
- iwp skill 仍是本地固定 client_id

---

## ADR-009：去 workflows 封装层，参考文件 + 动态发现驱动（2026-09-20，取代旧 ADR-009）

**决策**：删除 `scripts/workflows/{tasks,reports,freeform}.py` 语义化封装层。scripts 只保留纯基础设施（auth / client.call_tool / token_store / swagger_meta）；场景知识全部收进 `references/{分支}/README.md` 场景提示词；工具事实来源 = MCP `tools/list`（swagger_meta 缓存）。

**理由**：
- 旧设计中 API 知识存在三份冗余副本（MCP 工具 description / workflows 函数签名 / references 文档），任何 MCP 变更都会造成漂移（实际案例：`list_my_reports` 曾被写进速查文档但 MCP 从未有过该工具）
- 分支路由原本直接指向脚本函数，缺少场景级指导（何时确认、怎么组织输出、错误怎么办）
- 动态发现让 skill 与 MCP 版本不同步时**代码路径不炸**，只剩文档性漂移，且 tool-discovery 约定"schema 优先"消解参数漂移

**替代方案**：
- 保留封装 + 运行时自检（调用前查 tools/list 验证存在）→ 失败友好但硬编码仍在，双份知识照漂
- 维持 1:1 封装 + 人工同步 → 每次变更靠人记得同步，正是本次要解决的问题

**代价**：
- LLM 需按参考文件示例 + inputSchema 组装参数；由 tool-discovery 的"schema 优先"约定约束

---

## ADR-010：MCP 工具集最小化（17 个）

**决策**：MCP 工具从 20 个裁剪到 17 个——删除附件（upload/get_task_attachment）、任务统计（get_task_statistics）、周报统计与趋势（get_weekly_report_statistics/trend）；新增 list_subjects（课题发现）与 get_current_work_week（工作周+调用者身份复合发现）。

**理由**：
- 围绕三大用户场景（任务增删改查 / 周报生成 / 根据任务生成周报）最小化：不在场景内的工具一律删除
- 创建周报必填 work_week_id / user_id / org_id 三元组，但 WorkWeek 模型无 org_id、MCP access_token 的 claim 只有 sub——get_current_work_week 复合 /api/auth/profile 一次拿齐
- "在课题 X 下建任务"需要 subject_id，此前只能用 my_tasks 反查

**约束**：
- 只删 mcp_server/tools 层与 skill 封装，IWP 后端 REST 一律不动（Web 前端继续用）
- 已知限制：分配任务/周报仍需用户提供他人的 user_id（无用户搜索工具）

详见 `../InnovationWorkPlatform-mcp/docs/mcp/MCP工具集最小化与iwp-skill场景化重构-方案设计.md`。

---

## 历史 ADR-009（已废弃，2026-09-20）

<details>
<summary>原文：scripts/workflows 不暴露 tool 列表自动发现</summary>

**决策**：`scripts/workflows/{tasks,reports}.py` 的 `ALL` 列表与 MCP tools 一一对应，但**不**自动从 MCP 拉取清单同步。

**理由**：
- 20 个工具体量固定，无自动同步收益
- 显式列表让 SKILL.md 和 references 可读
- free-mode 走 `list_available_tools()` 自动发现——扩展点明确

**废弃原因**：三份冗余知识漂移（见 ADR-009 新版）；封装层整体移除后本决策随之失效。
</details>

---

## ADR-011：技能内置协议自适应 MCP client（2026-09-21，取代 client 旧实现）

**决策**：`scripts/client.py` 重写为协议自适应 client——运行时通过 `server/discover` 探测（2026-07-28）+ initialize 回退协商版本，自适应两代行为：会话式（2025-03-26 / 2025-06-18 / 2025-11-25：握手 + notifications/initialized + Mcp-Session-Id / MCP-Protocol-Version 头 + ping 应答 + DELETE 终止）与无状态（2026-07-28：`_meta` 元数据 + `Mcp-Method`/`Mcp-Name` 头 + `resultType` 容错）。协议常量/协商器/SSE 解析器独立成 `scripts/mcp_protocol.py`（纯函数），配套 `scripts/protocol_selfcheck.py` 诊断 CLI。

**理由**：
- 宿主 MCP client（MiniMax Code 等）无 OAuth 入口（mcp.json 不支持、headless 无浏览器），静态 Bearer 与 30min 滚动 token 模型不兼容——内置 client 是唯一可行路径且保证开箱即用
- MCP 规范已分两代且行为互斥（2026-07-28 移除握手/会话），硬编码单版本会在 server 升级/连接其他 server 时翻车
- capabilities 只声明 tools（未声明即契约），server 不会发起 sampling/elicitation 等未实现交互——用最小的声明面换取协议合规

**替代方案**：
- 依赖宿主 client + server 加免鉴权 bypass → 削弱安全模型，否决
- 仅实现会话式三代 → 实现量最小，但 2026-07-28 server 连不上，违背开箱即用
- 直接依赖官方 python-sdk ClientSession → 引入重依赖且其生命周期模型（长驻 task）与 skill 一次性调用模式不匹配

**代价与取舍**：
- 协议栈自维护（T3-2 的 notifications/initialized 缺失在本重写中修复）；`resultType=input_required`（MRTR）明确不支持，遇到报错
- 结果解析以 `content` 文本优先、`structuredContent` 兜底（解包 SDK 2.x 的 `{"result": "<json>"}` 包装壳），保证工作流消费的历史返回形状零回归