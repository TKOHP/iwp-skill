# 课题任务场景（tasks 路由）

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

```bash
# 我的任务(推荐 --all 自动翻页;结果含 status_label 渲染)
python cli.py tasks list --mine --all --out _my_tasks.json

# 课题 X 的任务:先经 W4 拿到 subject_id
python cli.py tasks list --subject 26 --all --out _subject_tasks.json

# 单个任务详情 / 任务日志(透传)
python cli.py call get_task_detail --args '{"task_id": 132}'
python cli.py call get_task_logs --args '{"task_id": 132, "page": 1, "page_size": 20}'
```

用户没说状态时不臆造过滤条件；列表为空时如实说明。

### W2 创建任务

```text
1. 解析要素：课题（名称→ID）、标题、紧急度、起止日期、执行者
2. 缺课题 → W4 课题发现；缺日期/紧急度用合理默认或询问用户
3. 生成完整预览（见 §5），确认后把参数写入 JSON 文件（含中文必须走文件）:
   python cli.py call create_task --args-file _args.json
   # _args.json: {"subject_id": 26, "title": "...", "description": "...",
   #              "urgency": "normal", "planned_start_date": "YYYY-MM-DD",
   #              "planned_end_date": "YYYY-MM-DD", "assignee_ids": [39]}
   # 参数以 cli.py tools 中 create_task 的 inputSchema 为准
4. 回执：新建任务 ID + 关键字段
```

### W3 更新 / 删除 / 分配 / 写日志

```bash
# 更新:  生成完整预览（见 §5）→ 确认后调 → 回读核验
python cli.py call update_task --args-file _args.json
# _args.json: {"task_id": 132, "status": "completed"}   (其余可选字段以 inputSchema 为准)

# 删除:  高危，见 §5
python cli.py call delete_task --args '{"task_id": 132}'

# 分配:  覆盖式！完整预览中被移除的执行者显式标注（见 §5）
python cli.py call assign_task --args-file _args.json
# _args.json: {"task_id": 132, "assignee_ids": [39]}

# 写日志
python cli.py call add_task_log --args-file _args.json
# _args.json: {"task_id": 132, "content": "..."}
```

### W4 课题发现（拿 subject_id）

```bash
# 模糊搜索课题名称
python cli.py call list_subjects --args '{"subject_title": "科创", "page": 1, "page_size": 20}'
# 或全量拉取后本地筛选
python cli.py call list_subjects --args '{"page": 1, "page_size": 100}' --out _subjects.json
```

```text
1. 多个候选时列出让用户选择（按 `../_shared/user-interaction.md`）；不要猜测"相近名称"
2. status 语义: 1-进行中, 2-已完成, 3-暂停, 0-储备中（以 inputSchema/工具描述为准）
```

## 4. 调用约定

调用走 `../_shared/tool-discovery.md` 的约定：统一入口 `python cli.py`，**schema 优先**。

## 5. 写操作完整预览

任何写操作执行前按 `../_shared/write-preview.md` 生成全字段完整预览（模板 U：变更类「现状→执行后」对照；模板 C：创建类全字段含默认值）并经用户确认，执行后回读核验。规则、模板与各工具要点以该文件为唯一事实来源，此处不重复。

批量修改（"把 X、Y、Z 都标完成"）：逐条完整预览 → 用户确认 → 逐条 update → 每条独立回执与核验。

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
