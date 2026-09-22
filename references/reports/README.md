# 周报场景（reports 分支）

> 本文是场景提示词。工具参数以 MCP `tools/list` 的 inputSchema 为准（**schema 优先**，见 `../_shared/tool-discovery.md`）。

## 1. 场景识别（何时路由到这里）

| 用户说 | 进入本场景 |
|--------|-----------|
| "帮我写本周周报" / "提交周报" / "建一条周报" | ✅ 子工作流 W1 |
| "批量提交本周所有周报" | ✅ 子工作流 W1（批量分支） |
| **"根据我本周的任务生成周报"** / "按课题任务写周报" | ✅ 子工作流 W2 |
| "查我的周报" / "这周谁还没交" / "看一下周报 X" | ✅ 子工作流 W3 |
| "改一下周报 X" / "删除周报 X" | ✅ 子工作流 W3 |

**"根据课题任务生成周报"是本分支内的子工作流，与手动创建等权**，不是独立分支，也不是 W1 的附属。判断依据：交付物是周报，任务查询只是数据来源（复用 tasks 场景的查询工具，见 `../tasks/README.md` W1）。

## 2. 前置授权检查

按 `../_shared/auth-flow.md` §1 执行授权态检查；未授权则走其 §2 三方流程。**不重复展开。**

## 3. 子工作流 W1：手动创建周报

```text
1. 拿创建三元组:
   python cli.py call get_current_work_week --args '{}' --out _week.json
   → result.work_week.id = work_week_id
   → result.identity = {user_id, real_name, org_id, org_name}
   （指定历史周: --args '{"work_week_id": N}'）
2. 起草周报内容: work_name / work_content / work_result
   （用户给了素材就整理；没给就先问，不要编造工作内容）
3. 生成完整预览（见 §5），确认后创建（含中文参数必须走 --args-file，以 inputSchema 为准）:
   单条 → python cli.py call create_weekly_report --args-file _args.json
          # {"work_week_id": N, "user_id": N, "org_id": N, "work_name": "...",
          #  "work_content": "...", "work_result": "...", "is_submitted": 0}
   多条 → python cli.py call batch_create_weekly_reports --args-file _args.json
          # {"work_week_id": N, "user_id": N, "org_id": N, "reports": [...]}
4. 回执: 周报 ID + 提交状态
```

默认 `is_submitted=0`（草稿）；**用户明确说"提交"才置 1**。

## 4. 子工作流 W2：根据课题任务生成周报

```text
1. 拿三元组: 同 W1 第 1 步（work_week_id + identity）
2. 收集任务素材:
   a. python cli.py tasks list --mine --all --out _my_tasks.json
      → 按工作周起止日期过滤（start_date ~ end_date 与任务计划/更新日期比对）
   b. 关键任务补细节: python cli.py call get_task_logs --args '{"task_id": N}'
   c. 需要按课题组织时: python cli.py call list_subjects --args '{}' 定位课题
      （任务查询工具的完整用法见 ../tasks/README.md W1/W4）
3. 按课题聚合起草:
   每课题一条 → work_name=课题或事项名, work_content=做了什么, work_result=产出/进度
4. **逐条完整预览给用户确认**（模板 C 全字段区块，任务 → 周报的映射是 LLM 生成的，
   必须让用户过目，防止把未完成事项写成成果；见 ../_shared/write-preview.md）
5. 确认后: 单条 → create_weekly_report；多条 → batch_create_weekly_reports
   （每条可带各自 subject_id；默认 is_submitted=0）
```

边界：本周无任务 → 如实说明，回退 W1 询问用户素材；任务与周时间段不匹配 → 列出可疑项让用户取舍，不要静默丢弃。

## 5. 写操作完整预览

任何写操作执行前按 `../_shared/write-preview.md` 生成全字段完整预览（模板 U：变更类「现状→执行后」对照；模板 C：创建类全字段含默认值）并经用户确认，执行后回读核验。规则、模板与各工具要点以该文件为唯一事实来源，此处不重复。

batch_create：逐条完整预览（每条一个区块）→ 确认后调 → **单条失败不阻塞其余**，事后逐条报告。

## 6. 查询子工作流 W3

```bash
# 我的周报 / 按维度查(参数以 inputSchema 为准)
python cli.py call list_weekly_reports --args-file _args.json --out _reports.json
# _args.json 示例: {"user_id": 39} 或 {"work_week_id": N} / {"subject_id": N} / {"is_submitted": 1}

# 周报详情 / 更新
python cli.py call get_weekly_report_detail --args '{"report_id": N}'
python cli.py call update_weekly_report --args-file _args.json
# _args.json: {"id": N, ...要改的字段}
```

`user_id`/`org_id` 从 `get_current_work_week` 的 identity 取；查他人周报需要用户提供其 ID。

## 7. 调用约定

调用走 `../_shared/tool-discovery.md` 的约定：统一入口 `python cli.py`，**schema 优先**。

## 8. 输出渲染

按 `../_shared/output-format.md`。周报列表用表格（ID/周/事项/提交状态）；W2 的逐条完整预览用表格区块展示；创建回执给 ID + is_submitted 状态。

## 9. 错误处理

按 `../_shared/failure-modes.md` 统一翻译。本场景高频错误：

| 错误 | agent 动作 |
|------|-----------|
| `not_found`（无进行中工作周，`get_current_work_week` 返回 work_week=null） | 按 payload.hint 提示用户到 Web 端创建工作周 |
| `identity_unavailable` | 提示身份信息获取失败；让用户直接提供 user_id/org_id 或稍后重试 |
| `permission_denied`（weekly_report_* / work_week_view） | 提示联系管理员检查角色权限 |
| `validation_error` | 重新读 inputSchema 核对参数后引导用户修正 |
| batch 部分失败 | 逐条报告成功/失败清单，失败条目给出原因，不自动重试 |
