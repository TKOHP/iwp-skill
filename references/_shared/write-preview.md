# 写操作完整预览（跨路由共享）

> 本文是 SKILL.md「写操作完整预览」铁律的完整版，也是各路由写操作预览规则的唯一事实来源。任何写操作执行前按本文生成完整预览并取得用户确认。

## 1. 适用范围

任何改变平台数据的工具调用，执行前一律完整预览：

| 路由 | 覆盖工具 |
|----------|---------|
| tasks | create_task、update_task、delete_task、assign_task、add_task_log |
| reports | create_weekly_report、batch_create_weekly_reports、update_weekly_report（含提交 0→1）、delete_weekly_report |
| free-mode | 透传的任何写性质工具（create / update / assign / delete / 批量，按 description 判断） |

**无例外**：单字段低危修改、用户说「直接做」，同样先完整预览——预览是用户看到真实写入效果的唯一时机。执行以用户对预览的明确确认为准。

## 2. 取现状

变更类操作（update / delete / assign / 周报更新）预览前，先实时获取目标现状：

```bash
python cli.py call get_task_detail --args '{"task_id": N}' --out _detail.json
python cli.py call get_weekly_report_detail --args '{"report_id": N}' --out _detail.json
```

- 现状的唯一来源是本次调用返回的 detail；列表结果与会话记忆会过时，合成预览以 detail 为准
- 完成判据：预览「现状」列的每个值都能对应到本次 detail 返回

## 3. 模板 U：变更预览

适用 update_task、delete_task、assign_task、update_weekly_report、delete_weekly_report、提交。

呈现为全字段三列表格——**完整任务的变化**，而非仅变更字段：

| 字段 | 现状 | 执行后 |
|------|------|--------|
| 标题 | cli改造 | **X**（本次修改） |
| 状态 | 进行中 | 进行中 |
| 优先级 | 中 | 中 |
| …该对象全部字段 | 实时 detail | 现状平移 |

- 本次修改的字段在「执行后」列加粗并标注（本次修改）；其余字段照现状平移呈现
- MCP 英文枚举翻译为中文呈现（completed→已完成、high→高），写入参数保持英文枚举
- 大文本（description、work_content 等）全量呈现，不截断
- 完成判据：表格覆盖该对象全部用户可见字段，每个字段两列都有值

## 4. 模板 C：创建预览

适用 create_task、add_task_log、create_weekly_report、batch_create_weekly_reports。

呈现为全字段两列表格——用户尚未提供的内容也**显式呈现**：

| 字段 | 将写入的值 |
|------|-----------|
| 标题 | MCP工具契约改造 |
| 描述 | （空） |
| 紧急度 | normal（优先级=中） |
| 起止日期 | （默认值） |
| 执行人 | 无 |

- 用户提供的字段如实呈现；未提供的字段标注平台默认值或（空），每个字段都有一行
- 完成判据：表格覆盖该工具 inputSchema 的全部字段，无留白省略

## 5. 呈现与确认循环

1. 用 ask_user 呈现完整预览，**预览全文放 question 正文**（description 可能被 UI 隐藏）
2. 用户对预览中任何字段提出修改（含非本次目标字段）→ 更新写入参数 → 重新合成完整预览 → 再次呈现
3. 循环到用户对最终版预览明确回应确认/执行
4. 现状列的数据疑误：说明现状来自平台实时数据，预览只改「执行后」列；确属平台数据错误则引导用户先到平台修正
5. 用户看完预览放弃操作：不执行，如实报告未做任何写入

## 6. 执行与回读核验

```bash
python cli.py call update_task --args-file _args.json   # 中文参数一律走文件
python cli.py call get_task_detail --args '{"task_id": N}' --out _verify.json
```

- 执行后回读 detail，与预览逐字段对照
- 回执 = 新值 + 核验结论：「核验通过，与预览一致」或差异报告（预期 vs 实际）
- **批量写**：逐条完整预览（每条一个区块，模板不变）→ 确认后逐条执行 → 每条独立回执与核验，单条失败不阻塞其余

## 7. 工具要点表

| 工具 | 模板 | 特有要点 |
|------|------|---------|
| create_task | C | 默认值：状态=未开始、进度=0%、urgency 未给=normal（优先级=中）、执行人=无 |
| update_task | U | 只改用户指定字段，其余字段在「执行后」列照现状平移 |
| delete_task | U | 「执行后」整列标注「软删除，平台任务列表不可见」；平台侧恢复走 Web 端 |
| assign_task | U | 覆盖式：「执行后」呈现完整新名单，被移除者显式标注（移除） |
| add_task_log | C | 呈现新增日志全文；注明任务其他字段零改动 |
| create_weekly_report / batch | C | is_submitted 默认 0（草稿）；batch 每条一个区块逐条呈现 |
| update_weekly_report / 提交 | U | 提交（is_submitted 0→1）只在用户明确说「提交」时出现在预览中 |
| delete_weekly_report | U | 高危且不可恢复（物理删除）：「执行后」整列标注「删除后无法恢复」 |
