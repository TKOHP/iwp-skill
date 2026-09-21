# 输出格式约定（跨分支）

> 本文档定义 agent 在 tasks / reports / free-mode 三个分支向用户呈现 MCP 工具结果的统一格式。

## 通用原则

1. **不堆砌 JSON**：MCP 原始响应是结构化数据，但用户看的是叙事+关键字段摘要。
2. **分页结果**：明确标注"第 X 页 / 共 Y 条"，不要逐条列。
3. **失败信息最小化**：错误消息去掉敏感信息（token / session_id / URL 等已被脱敏 filter 处理）。
4. **状态码语义**：HTTP 200 + 业务 code 200 才算成功；其他情况按错误码翻译表处理。

## 成功响应（list 类）

```text
✅ 已获取 N 条任务 / 周报（第 1 页 / 共 N 条）

| ID | 标题 | 状态 | 紧急度 | 截止 |
|----|------|------|--------|------|
| 123 | 标题 A | in_progress | normal | 2026-09-30 |
| 124 | 标题 B | completed | high | 2026-09-25 |
...
（仅显示前 5 条，如需看全部请告诉我）
```

## 成功响应（detail 类）

```text
✅ 任务 #123 详情

标题：XXX
状态：in_progress（进行中）
紧急度：high
截止：2026-09-30
负责人：user_id(3) (张三)
描述：...
最近日志：
  - [2026-09-15] 完成了 A 模块
  - [2026-09-14] 启动 B 模块
```

## 成功响应（create / update / delete 类）

```text
✅ 任务创建成功
ID: 123
标题：XXX
链接：https://<host>/subject-detail/<id>#task123
```

## 失败响应

按 `./failure-modes.md` 的错误码翻译：

| MCP 错误 | 用户看到的提示 |
|---------|--------------|
| `iwp_credential_expired` | "授权已过期，请重新授权（即将弹窗）" |
| `permission_denied` | "权限不足，请联系管理员检查您在 IWP 的角色权限" |
| `rate_limited` | "调用过于频繁，请稍后 30 秒再试" |
| `validation_error` | "参数错误：XXX" |
| `not_found` | "资源不存在：XXX（ID 是否正确？）" |
| `upstream_error` / `upstream_timeout` | "上游 IWP 服务暂时不可用，请稍后重试" |

## 多步操作

若一次用户问题涉及多步（如"列出我的任务后，把状态为 X 的都标为已完成"），agent 应：

1. 先列任务（read-only）
2. 用户确认后再批量 update
3. 每步独立报告成功/失败

不要默默完成所有步骤后只报告最终结果——失败定位困难。