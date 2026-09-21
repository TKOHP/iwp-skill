---
name: iwp
description: IWP（创新工作平台）MCP 客户端：调用 IWP 后端课题任务与周报工具，OAuth 2.1 授权。触发词：列出我的任务、创建任务、课题任务、周报、提交周报、根据任务生成周报、MCP 工具。
---

# iwp skill - 路由器入口

## 这是什么

iwp skill 是 IWP（创新工作平台）MCP server 的标准 OAuth 2.1 PKCE 客户端：浏览器授权只见事务 ID，凭证只经 OAuth 授权流程获取，任何位置都不粘贴 refresh_token。

- **工具事实来源是 MCP `tools/list`**：本 skill 只描述场景与工作流，不维护硬编码工具清单——数量与参数以实时发现为准

## 路由规则（先读这里，再进分支）

| 用户想做什么 | 场景分支 | 必读参考文件 |
|--------------|---------|-------------|
| 列任务 / 建任务 / 改任务 / 删任务 / 分配 / 任务日志 / 找课题 | **tasks** | `references/tasks/README.md` |
| 写周报 / 提交周报 / 查周报 / **根据本周课题任务自动生成周报** | **reports** | `references/reports/README.md` |
| 列出所有 MCP 工具 / 调一个分支未覆盖的工具 / 查某工具参数 schema | **free-mode** | `references/free-mode/README.md` |

三分支**平级等权**：tasks / reports / free-mode 无主流程与兜底之分（设计记录见 `docs/architecture.md` ADR-001）。

路由命中后：**读对应参考文件 → 按其工作流执行**。

## 授权铁律（每次调工具前）

任何分支、任何工具调用之前，必须先确认授权态有效（access_token 存在且未过期）。无效或缺失时，按 `references/_shared/auth-flow.md` 执行三方 OAuth 流程（**授权前必读**：授权流程、端口占用、超时与 state 校验的唯一事实来源，本文不重复其步骤）。

## 调用约定

所有工具调用走统一入口 `scripts.client.call_tool("<工具名>", {参数})`，返回 IWP 业务格式 `{code, message, data}`。client 内置完整 MCP 协议栈（版本协商兼容 2025-03-26~2026-07-28 两代），无需宿主 MCP 配置。

- **schema 优先**：调用示例仅示意，参数以 MCP `tools/list` 的 inputSchema 为准 → `references/_shared/tool-discovery.md`
- 错误翻译 → `references/_shared/failure-modes.md`；输出渲染 → `references/_shared/output-format.md`
- 连接/协商诊断：`python -m scripts.protocol_selfcheck`
- **写操作先确认**：create / update / delete / assign 前向用户复述将要写入的内容；delete 与批量提交属高危，必须显式确认

## 相关文档

- `references/tasks/README.md` / `references/reports/README.md` / `references/free-mode/README.md` - 三分支场景提示词
- `references/_shared/` - 授权工作流 / 工具发现（schema 优先）/ 错误码翻译 / 输出格式
- `skill_config.py` + `scripts/`（auth / client / token_store / swagger_meta）- 纯基础设施
- `docs/architecture.md` / `docs/oauth-flow.md` - 架构决策记录 / OAuth 三方交互详解
