# 课题任务场景（tasks 分支）

> 本文是场景提示词。工具参数以 MCP `tools/list` 的 inputSchema 为准（**schema 优先**，见 `../_shared/tool-discovery.md`）。

## 1. 场景识别（何时路由到这里）

| 用户说 | 进入本场景 |
|--------|-----------|
| "列出我的任务" / "我的待办" / "本周任务" | ✅ |
| "课题 X 下有哪些任务" | ✅ |
| "在课题 X 下建个任务 Y" / "给课题 X 加任务" | ✅ |
| "改一下任务 X 的标题 / 状态 / 截止日期" | ✅ |
| "删除任务 X" | ✅（高危，见 §5） |
| "把任务 X 分配给张三" | ✅ |
| "任务 X 加一条进度" / "看任务 X 的日志" | ✅ |
| "有哪些课题" | ✅（课题发现） |

涉及"写周报 / 根据任务生成周报" → 去 `../reports/README.md`（周报场景会反向引用本场景的查询工作流）。

## 2. 前置授权检查

按 `../_shared/auth-flow.md` §1 执行授权态检查；未授权则走其 §2 三方流程。**不重复展开。**

## 3. 工作流步骤

### W1 查询任务

```text
1. "我的任务" → call_tool("list_my_tasks", {status?, page, page_size})
2. "课题 X 的任务" → 先经 W4 拿到 subject_id → call_tool("list_tasks", {subject_id, status?, page, page_size})
3. 单个任务详情 → call_tool("get_task_detail", {task_id})
4. 任务日志 → call_tool("get_task_logs", {task_id, page, page_size})
```

用户没说状态时不臆造过滤条件；列表为空时如实说明。

### W2 创建任务

```text
1. 解析要素：课题（名称→ID）、标题、紧急度、起止日期、执行者
2. 缺课题 → W4 课题发现；缺日期/紧急度用合理默认或询问用户
3. 向用户复述要素（见 §5），确认后：
   call_tool("create_task", {subject_id, title, description?, urgency?, planned_start_date?, planned_end_date?, assignee_ids?, parent_task_id?})
4. 回执：新建任务 ID + 关键字段
```

### W3 更新 / 删除 / 分配 / 写日志

```text
- 更新:   call_tool("update_task", {task_id, ...要改的字段})    ← 先 get_task_detail 展示现状
- 删除:   call_tool("delete_task", {task_id})                   ← 高危，见 §5
- 分配:   call_tool("assign_task", {task_id, assignee_ids})     ← 覆盖式！先展示现执行者
- 写日志: call_tool("add_task_log", {task_id, content})
```

### W4 课题发现（拿 subject_id）

```text
1. call_tool("list_subjects", {subject_title?, status?, page, page_size})
2. 多个候选时用 ask_user 让用户选择；不要猜测"相近名称"
3. status 语义: 1-进行中, 2-已完成, 3-暂停, 0-储备中（以 inputSchema/工具描述为准）
```

## 4. 调用约定

调用走 `../_shared/tool-discovery.md` 的约定：统一入口 `scripts.client.call_tool`，**schema 优先**。

## 5. 写操作确认规则

| 操作 | 确认要求 |
|------|---------|
| create_task | 复述：课题 / 标题 / 紧急度 / 日期 / 执行者，用户确认后再调 |
| update_task | 先 detail 展示现状 → 说明将改成什么 → 确认后调 |
| delete_task | **高危**：先 detail 确认目标，明确告知软删除但流程上有校验，二次确认 |
| assign_task | **覆盖式**：展示现有执行者将被替换，确认后调 |

批量修改（"把 X、Y、Z 都标完成"）：先列出目标清单 → 用户确认 → 逐条 update → 每条独立报告成败。

## 6. 输出渲染

按 `../_shared/output-format.md`：列表用表格（ID/标题/状态/紧急度/截止），分页标注"第 X 页/共 Y 条"，写操作回执给 ID + 关键字段。

## 7. 错误处理

按 `../_shared/failure-modes.md` 统一翻译。本场景高频错误：

| 错误 | agent 动作 |
|------|-----------|
| `permission_denied`（task_* / subject_view） | 提示联系管理员检查角色权限 |
| `validation_error` | 重新读 inputSchema 核对参数后引导用户修正 |
| `not_found`（task_id / subject_id） | 让用户确认 ID；ID 来自本会话列表的优先复核 |
| 无进行中课题（list_subjects 为空） | 如实告知，建议用户到 Web 端查看 |
