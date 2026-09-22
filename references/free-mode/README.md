# 自由模式场景（free-mode 分支）

> 本文是场景提示词。工具参数以 MCP `tools/list` 的 inputSchema 为准（**schema 优先**，见 `../_shared/tool-discovery.md`）。

## 1. 场景识别（何时路由到这里）

| 用户说 | 进入本场景 |
|--------|-----------|
| "你能调哪些 MCP 工具？" / "list MCP tools" | ✅ 发现工作流 W1 |
| 指定了一个 tasks/reports 场景未覆盖的工具名 | ✅ 透传工作流 W2 |
| "某个工具的参数 schema 是什么？" | ✅ 发现工作流 W1 |
| MCP 刚加了新工具，参考文件还没写 | ✅ 透传工作流 W2 |

注意：tasks/reports 场景**已经覆盖**的请求（列任务、写周报等）应路由回对应分支——那里有场景级的确认规则与输出约定，本场景没有。

## 2. 前置授权检查

按 `../_shared/auth-flow.md` §1 执行授权态检查；未授权则走其 §2 三方流程。**不重复展开。**

## 3. 工作流 W1：工具发现

```bash
python cli.py tools --out _tools.json    # 加 --refresh 强制刷新 schema 缓存
```

用读文件工具在 `_tools.json` 中定位工具的 name/description/inputSchema。

渲染约定：分组列表（课题任务 / 周报）+ 一句话描述；schema 展示必填项与类型，不贴原始 JSON。

## 4. 工作流 W2：自由透传

```text
1. 确定工具名与参数:
   - 用户给了工具名 → 先经 W1 查其 inputSchema,按 schema 组装参数
   - 用户只描述意图 → 经 W1 浏览 description 匹配工具,列出候选让用户确认
2. 确认（见 §5）后:
   简单 ASCII 参数 → python cli.py call <工具名> --args '<JSON>'
   含中文/复杂结构 → 参数写 JSON 文件后 python cli.py call <工具名> --args-file _args.json
3. 回执: 按 `../_shared/output-format.md` 渲染
```

## 5. 写操作确认规则

透传**不豁免**确认规则——按工具语义判断：

| 工具性质（看 description / read_only 线索） | 确认要求 |
|------|---------|
| 只读（list / get / statistics 类） | 可直接调用 |
| 写操作（create / update / assign） | 复述参数，用户确认后调 |
| 高危（delete 类、批量） | 先查询目标确认存在 → 明确告知后果 → 二次确认 |

工具语义不明时，宁可先问用户，不要盲调。

## 6. 调用约定

调用走 `python cli.py call`（万能透传），发现与 **schema 优先**原则见 `../_shared/tool-discovery.md`。

新工具频繁使用、场景稳定后 → 候选迁入对应场景分支的参考文件——只能带场景工作流与示例参数，参数全量仍以 `cli.py tools` 输出为准（改文档，不改脚本）。

## 7. 错误处理

按 `../_shared/failure-modes.md` 统一翻译。本场景高频错误：

| 错误 | agent 动作 |
|------|-----------|
| 工具不存在（tools/list 无此名） | 用 W1 重新发现，给出名称最接近的候选 |
| `validation_error` | 重新读 inputSchema 核对参数后引导用户修正 |
| `permission_denied` | 提示该工具所需权限，联系管理员 |
| `rate_limited` | 稍后重试；自由模式不做自动重试风暴 |
