# 领域模型与口径权威表（跨路由共享）

> 本文是 IWP 平台领域事实在技能侧的**唯一安身之所**。
> 只收 server description 放不下的**跨实体语义**。单工具的参数清单、必填性、枚举取值定义、默认值、危险提示**一律不复述**——以 `python cli.py tools` 输出的 description/inputSchema 为准。

## 1. 实体层级

- **课题（subject）是任务与周报的顶级索引**：任务必挂课题（subject_id 必填）；周报可选挂课题（subject_id 可空）。
- 任务有**父子层级**（parent_task_id，拆子任务）；周报无层级。
- **工作周（work_week）是独立的时间维度**：周报按工作周归属；任务的"本周归属"不由人工判断，由素材命令的日期重叠判据代码决定（见 §3）。
- **周报三元组**（work_week_id / user_id / org_id）是创建周报的必填上下文，`get_current_work_week` 一次返回工作周与身份；周报素材命令（reports material）的输出同样携带，无需二次调用。

## 2. 状态枚举权威表

**任务（subject_task）**：

| 值 | 中文 | 英文枚举（update_task 等用） |
|----|------|------------------------------|
| 0 | 待开始 | pending |
| 1 | 进行中 | in_progress |
| 2 | 已完成 | completed |
| 3 | 已取消 | cancelled |

**课题（subject）**：

| 值 | 中文 |
|----|------|
| 0 | 储备中 |
| 1 | 进行中 |
| 2 | 已完成 |
| 3 | 暂停 |

⚠️ **两个枚举不可互套**：任务 3=已取消，课题 3=暂停，同名字段含义不同（历史上曾因互套把任务状态 3 错渲染为"暂停"）。

- 来源与校准：后端任务状态定义 + server description，2026-10-27 校准。与 server description 冲突时，以 server description 为准并回改本文。
- 任务状态的中文标签属**呈现层翻译**，由 `skill_config.TASK_STATUS_LABELS` 提供（CLI 输出的 `status_label` 字段），不构成第二份契约语义。

## 3. 「本周的任务」归属口径

唯一执行者是**周报素材命令**（代码化，对齐平台 `/tasks/weekly-draft` 权威逻辑）：

```bash
python cli.py reports material --out _w2_material.json   # 当前周；--week <ID> 指定历史周
```

命令内部**优先走服务端工具 `list_my_week_tasks`**（weekly-draft raw 模式，筛选口径的单一来源，且免逐任务详情调用）；旧 server 无此工具或调用失败时自动回退本地筛选（同款口径纯函数）。输出 `source` 字段标明实际路径（server/local），`warnings` 记录降级原因。

口径五条（代码实现，LLM 不重做筛选）：我是执行者且未移除；未删除；状态∈{进行中, 已完成}；生效日期（实际‖计划）与工作周**重叠**；起止日期全缺视为入选。

任何场景不得让 LLM 肉眼比对日期重做该筛选；需要不同时间窗时用 `--week` 指定工作周。

## 4. 周报三字段内容语义

server 对 `work_name` / `work_content` / `work_result` 只给类型，内容语义是领域契约：

| 字段 | 内容语义 |
|------|---------|
| work_name | 该课题本周**任务名称的组织**（可涵盖多条任务），**不是课题名**；分点，每点一个事项短名 |
| work_content | 所做工作的**详细描述**；分点，与 work_name 同号对应 |
| work_result | **产出与进度**；分点，与 work_name 同号对应 |

分点的结构与字数、可选写作形态、面向读者的写法，见 `../reports/README.md` §4「三点对齐 / 写作形态 / 读者契约」——本节只定字段语义，不复述格式。

智能生成路径（筛选 → 生成 → 溯源预览）的完整工作流见 `../reports/README.md` W2。

## 5. 本文边界（明确不收录）

- 单工具参数清单、必填性、枚举取值定义、默认值、危险提示 → `python cli.py tools` 为准；
- 工作流步骤、确认门槛、呈现规则 → 各业务场景文件与 `_shared` 过程文件（write-preview / output-format / user-interaction / failure-modes / auth-flow / tool-discovery）。
