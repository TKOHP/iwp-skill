# 工具发现与调用约定（跨分支共享）

> 本 skill **不维护硬编码的 MCP 工具清单**。工具的事实来源（single source of truth）是 MCP server 的 `tools/list`——它包含每个工具的名称、描述与 `inputSchema`。skill 与 MCP 版本不同步时，本文的约定保证调用路径不炸。

## 1. 统一调用入口

```python
from scripts.client import call_tool

result = call_tool("<工具名>", {"参数名": "值"})
# 返回 IWP 业务格式 dict: {code, message, data}
```

`call_tool` 内部已完成：协议版本协商（`server/discover` 探测 + initialize 回退，兼容 2025-03-26 ~ 2025-11-25 会话式与 2026-07-28 无状态两代）、Bearer 注入、401 自动续期重试、错误码翻译（见 `failure-modes.md`）。

端点连通性/协商诊断：

```bash
python -m scripts.protocol_selfcheck   # 输出协商结果、工具数、只读调用抽查
```

## 2. schema 优先原则（最重要）

参考文件（tasks / reports / free-mode 的 README）中的调用示例**仅示意场景与工作流顺序**，**不保证参数清单是最新的**。每次调用前：

1. 用发现入口查该工具的真实 `inputSchema`
2. 按 schema 组装参数（名称、类型、必填性都以 schema 为准）
3. schema 与参考文件示例冲突时，**以 schema 为准**

```python
from scripts import swagger_meta
from scripts.client import McpClient

with McpClient() as client:
    tools = swagger_meta.load_or_refresh(client)   # 优先读本地缓存(30 天 TTL)

tool = next(t for t in tools if t["name"] == "create_task")
print(tool["description"], tool["inputSchema"])
```

缓存可能陈旧（MCP 升级后），强制刷新：

```python
swagger_meta.invalidate()   # 下次 load_or_refresh 将实时拉取
```

## 3. 发现工作流（什么时候必须先发现）

| 情形 | 动作 |
|------|------|
| 参考文件工作流中明确列出的工具 | 可直接调用；报"工具不存在"时进入发现流程 |
| 参考文件未覆盖、或 MCP 可能新增/改名 | 先 `tools/list` 全量发现，再按 description 选择工具 |
| 参数报 `validation_error` | 重新读该工具 inputSchema，核对参数名与类型 |
| 怀疑版本不同步 | `swagger_meta.invalidate()` 后重新发现 |

发现后调用仍走 `call_tool`，无需绕过 client。

## 4. 返回值处理约定

- 成功：`{"code": 200, "message": "...", "data": {...}}` —— 业务数据在 `data`
- 分页：`data` 内通常含 `list/total/page/page_size`（个别端点为 `items`，以实际返回为准）；client 已自动跟随 `nextCursor` 合并工具清单
- 输出给用户前按 `output-format.md` 渲染，不要堆原始 JSON

> 结构化输出说明：server 若返回 `structuredContent`（2025-06-18+），client 以 `content` 文本为优先（保证与历史返回形状一致），仅在 content 缺失时采用 `structuredContent` 并解包 SDK 的 `{"result": "<json>"}` 包装壳。

## 5. 反模式（禁止）

1. **硬编码清零**：脚本与参考文件里不新增工具全量清单/参数表——迁入参考文件的只能是场景工作流与示例参数，参数全量以 `tools/list` 为准
2. **禁止**绕过 `scripts.client` 直接手写 JSON-RPC / HTTP 请求
3. **禁止**在未读取 inputSchema 的情况下"猜参数"重试 `validation_error`
4. **禁止**把 `free-mode` 的透传当作绕过场景工作流（确认规则等）的捷径
