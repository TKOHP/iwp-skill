# 工具发现与调用约定（跨分支共享）

> 本 skill **不维护硬编码的 MCP 工具清单**。工具的事实来源（single source of truth）是 MCP server 的 `tools/list`——它包含每个工具的名称、描述与 `inputSchema`。skill 与 MCP 版本不同步时，本文的约定保证调用路径不炸。

## 1. 统一调用入口（CLI）

```bash
# 万能透传:简单 ASCII 参数可内联
python cli.py call list_subjects --args '{"page": 1, "page_size": 20}'

# 参数含中文/复杂结构:必须走参数文件(禁止 bash 内联中文 JSON)
python cli.py call create_task --args-file _args.json

# 结果中文可读:写 UTF-8 文件后用读文件工具查看
python cli.py call get_task_detail --args '{"task_id": 132}' --out _result.json
```

- 返回:`{"ok": true, "tool": "<工具名>", "result": <业务数据>}`;失败为
  `{"ok": false, "error": {kind, message, hint}}` + 退出码 1
- `call` 内部委托 `scripts.client.call_tool`,已完成:协议版本协商(兼容
  2025-03-26 ~ 2026-07-28 两代)、Bearer 注入、401 自动续期重试、错误码翻译
  (见 `failure-modes.md`)
- stdout 永远 ASCII-safe JSON(管道安全);需要中文可读时加 `--out`

连通性/协商诊断:

```bash
python cli.py selfcheck   # 输出协商结果、工具数、只读调用抽查(JSON report 字段)
```

## 2. schema 优先原则（最重要）

参考文件（tasks / reports / free-mode 的 README）中的调用示例**仅示意场景与工作流顺序**，**不保证参数清单是最新的**。每次调用前：

1. 用发现入口查该工具的真实 `inputSchema`
2. 按 schema 组装参数（名称、类型、必填性都以 schema 为准）
3. schema 与参考文件示例冲突时，**以 schema 为准**

```bash
# 发现入口:schema 缓存 30 天 TTL;怀疑陈旧时加 --refresh
python cli.py tools --out _tools.json
# 然后用读文件工具在 _tools.json 里定位目标工具的 name/description/inputSchema
```

## 3. 发现工作流（什么时候必须先发现）

| 情形 | 动作 |
|------|------|
| 参考文件工作流中明确列出的工具 | 可直接调用；报"工具不存在"时进入发现流程 |
| 参考文件未覆盖、或 MCP 可能新增/改名 | 先 `python cli.py tools` 全量发现，再按 description 选择工具 |
| 参数报 `validation_error` | 重新读该工具 inputSchema，核对参数名与类型 |
| 怀疑版本不同步 | `python cli.py tools --refresh` 重新发现 |

发现后调用仍走 `python cli.py call <工具名>`，无需绕过 CLI。

## 4. 返回值处理约定

- 成功：`{"ok": true, "tool": "...", "result": <业务数据>}` —— 业务数据在 `result`
- 分页：`result` 内通常含 `list/total/page/page_size`（个别端点为 `tasks/items`，以实际返回为准）；`tasks list --all` 已在 CLI 内自动翻页合并
- 输出给用户前按 `output-format.md` 渲染，不要堆原始 JSON

> 结构化输出说明：server 若返回 `structuredContent`（2025-06-18+），client 以 `content` 文本为优先（保证与历史返回形状一致），仅在 content 缺失时采用 `structuredContent` 并解包 SDK 的 `{"result": "<json>"}` 包装壳。

## 5. 反模式（禁止）

1. **硬编码清零**：脚本与参考文件里不新增工具全量清单/参数表——迁入参考文件的只能是场景工作流与示例参数，参数全量以 `cli.py tools` 为准
2. **禁止**绕过 CLI（透传 `call`）直接手写 JSON-RPC / HTTP 请求
3. **禁止**在未读取 inputSchema 的情况下"猜参数"重试 `validation_error`
4. **禁止**把 `free-mode` 的透传当作绕过场景工作流（确认规则等）的捷径
