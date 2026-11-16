# 连接器形态调研：钉钉 DWS 解剖与 iwp-skill 定位

> **性质**：调研记录 + 对照结论，仅面向人类读者。技能运行文件（`SKILL.md`、`references/`、`examples/`、README）不得引用本文。
> **撰写日期**：2026-11-11
> **证据等级**：🟢 一手（官方文档 / 开源仓库 / 本机实测）　🟡 三方分析（用户提供的千问办公实测文档）　⚪ 未验证
> **一句话结论**：钉钉的"插件"不是一种文件格式，而是**平台代持凭证 + 预知能力清单 + 机制化门禁 + 可审计执行**；DWS 是一个可以逐文件对读的参照实现，iwp-skill 与它同构但少了后三样。

---

## 1. 先把三个词钉死

调研中最容易糊的是"插件""连接器""技能"，三者不是同一层的概念。

| 术语 | 定义 | 判据 |
|---|---|---|
| **连接器**（connector） | 把某个平台的能力暴露成 Agent 可调用的工具集合；负责描述、鉴权、执行、治理 | 去掉它，Agent 还能不能碰到那个平台的数据 |
| **插件**（plugin） | 宿主（Claude Code / DSH / 钉钉 / 千问）里的**安装单元**：一个包，内含连接器声明、技能、命令、钩子、配置项 | 它是不是被宿主按包管理、可启用/禁用/升级 |
| **技能**（skill） | 一段**流程知识**：什么时候用哪个工具、先做什么后做什么、边界在哪 | 换宿主后内容是否还需要改 |

三者的关系可以这样记：**插件是容器，连接器是手，技能是经验**。同一个连接器可以装进不同容器的壳里，技能则是贴在容器内壁的说明书。

另一个必须区分的对：

| | 通道（channel） | 连接器（connector） |
|---|---|---|
| 职责 | 嘴和耳朵：进出消息、会话、推送 | 手：读写业务对象（文档、表格、审批、待办、通讯录） |
| 身份 | 机器人 / 应用身份 | 用户身份（OAuth） |
| 典型能力 | 在已建会话里回复、发文件、委派给 IM | 42 个服务、1723 个工具 |
| 缺了它 | Agent 不能主动触达人 | Agent 不能替人干活 |

钉钉侧这两者由不同产品承担：通道是机器人 + Stream 模式；连接器是 DWS（DingTalk Workspace）。**混着谈会导致"插件化"目标失焦**——本轮要评估的是连接器形态。

---

## 2. 钉钉侧的五种注册形态，与它们共同收敛到的四件套

| # | 形态 | 描述载体 | 鉴权 | 谁托管凭证 |
|---|---|---|---|---|
| 1 | 自定义能力（Actions / OpenAPI 插件） | OpenAPI 3.0 YAML | noAuth / API Key（Basic、Bearer、自定义 Header）/ OAuth2.0 | 钉钉平台 |
| 2 | 拟人操作（RPA） | `ai-plugin.json`（`adaptive_schema_version: 1.0.0`） | 不登录，复用用户浏览器会话；平台注入 `x-dingtalk-context`、`x-dingtalk-entity` | 浏览器 |
| 3 | MCP 技能（助理 / 组织技能库） | MCP server（SSE / streamable-http） | 组织级统一配 Key | 平台（管理员代持） |
| 4 | dingtalk-mcp（反向，给外部 Agent） | 13 份自造 YAML DSL | App Token（client_id + client_secret） | 用户环境变量 / 本地缓存 |
| 5 | 连接平台 iPaaS | 连接器 = 触发事件 + 执行动作；连接流 = 可视化编排 | 各连接器自定义 | 平台 |

形态各异，但结构是同一套**四件套**：

```
能力描述（OpenAPI / ai-plugin.json / MCP tools / YAML DSL）
      ↓
身份（谁在调用：应用身份 or 注入的 currentUser）
      ↓
执行通道（HTTP / WebSocket 反连 / MCP JSON-RPC / 浏览器）
      ↓
分发治理（应用市场 / 技能库 / 审计 / 计费 / 审核沙箱）
```

钉钉近两年的演进方向很清楚：**描述层向 MCP tools 收敛，鉴权和治理层留在平台**。MCP 广场宣称 6000+ 企业级 MCP 服务，双路径接入（钉钉内 DEAP 技能中心/工作流；外部 RESTful / streamable-http）也是同一判断的旁证。

推论：**"像钉钉插件那样做"的实质不是写一份 YAML，而是把凭证、身份、审计、计费交给平台托管**。这一点决定了后面所有取舍。

---

## 3. 薄连接器解剖：`dingtalk-mcp`（反面样本）

### 3.1 可见性 🟢

- GitHub `open-dingtalk/dingtalk-mcp` 的 HEAD 树**只有一个 README blob**（`truncated: false`）→ 仓库是空壳。
- 真实产物在 npm：`dingtalk-mcp@1.1.21`（2026-01-20，MIT，36 个文件，157,846 字节）。`dist/*.js` 已编译，只有 `.d.ts` / `.d.ts.map`，**无 TS 源码**；根目录 13 份 `*_mcp_server.yaml`。

### 3.2 它到底是 IM 端还是连接器？是连接器 🟢

整包 grep `StreamServer|listen|receive|callback|webhook|ws://|wss://` **零命中**；唯一网络入口是 `oapi.dingtalk.com/gettoken`。**只发不收**，没有任何入站消息处理。所以它不是 IM 端。

### 3.3 规模与机制

**14 个 profile / 98 个工具**：

| profile | 工具数 | profile | 工具数 |
|---|---|---|---|
| appmanage | 11 | servicewindow | 6 |
| notable（待办/日志） | 18 | honor | 5 |
| tb（Teambition） | 15 | report | 5 |
| calendar | 8 | robot_im | 5 |
| contacts | 6 | tasks | 5 |
| department | 6 | notice | 4 |
| | | checkin | 2 |
| | | ding_im | 2 |

`dist/DingTalkMCPServer.js` 的关键路径（已通读）：

- `loadConfig(dir)` 读目录下全部 `*mcp_server.yaml`，按 `ACTIVE_PROFILES`（逗号分隔，`ALL` 全开）或 `default_active` 选 profile；**工具名重复直接抛错**。
- `getValidAccessToken()`：`DINGTALK_ACCESS_TOKEN` 环境变量 → `.dingtalk_token_cache.json`（提前 5 分钟过期）→ `GET oapi.dingtalk.com/gettoken?appkey&appsecret`。缓存文件明文。
- `buildHeaders`：只在 url 含 `https://api.dingtalk.com` 时注入 `x-acs-dingtalk-access-token`；`oapi.dingtalk.com` 则把 `access_token` 拼到 query。→ **按域名切换鉴权形态，是兼容层而非设计**。
- `generateSchema(args)` 跳过带 `system` 和 `not_need_model_transform` 的参数（不给模型看）；`buildBody` 按 `position: body` 映射，`system` 从环境变量取值。
- 传输 = `StdioServerTransport`（本地 stdio，非 HTTP）。

### 3.4 从这里能学到的三件事

1. **安全声明字段是装饰性的**：YAML 里写了 `securitySchemes: [{id: DingTalkAuth, type: apiKey, in: header}]`，但代码根本不读它，鉴权是按域名硬编码的。→ 描述文件里的字段，如果没有代码消费，就不能当成契约。
2. **`responseTemplate: {}` 全空 = 响应原样透传**：工具的返回结构没有治理，token 膨胀不可控，模型看到的是服务端原始 JSON。
3. **"人当参数"的身份模型**：整份配置没有任何用户身份字段，要看谁的数据就把 `userid` 当参数传（如 `getUserDetailByUserId`）。这是**应用视角**，与 iwp 的"我的视角（sub 来自 token）"根本不同。

---

## 4. 厚连接器解剖：DWS（DingTalk Workspace CLI）

### 4.1 开源事实 🟢

| 项 | 值 |
|---|---|
| 仓库 | `DingTalk-Real-AI/dingtalk-workspace-cli` |
| 许可证 | Apache-2.0 |
| 语言 | Go（1.26+），另含 Python 辅助脚本 |
| 体量 | 243,382 KB；3,977 stars / 279 forks |
| 时间 | created 2026-03-21，pushed 2026-11-10 |
| 活跃度 | 242 open issues（含 PR），有 Discussions |
| npm | `dingtalk-workspace-cli`，最新 1.0.67（2026-11-04）；本机装的是 1.0.65.1 |
| 安装 | `install.sh` / `install.ps1` / npm / Homebrew / Releases / `go build -o dws ./cmd` |

**结论：源码完全开放，可以逐文件对读**（这是本轮最有价值的发现——不必再猜钉钉怎么做）。而且它把"分发技能"也交了底：

```
npx skills add DingTalk-Real-AI/dingtalk-workspace-cli -g
```

与 iwp-skill 走的是**同一条分发通道**。

### 4.2 本机落盘形态 🟢

| 路径 | 内容 |
|---|---|
| `C:\Users\11349\.qwenworkcn\bin\ext\dws-core-windows-amd64.exe` | 51,150,320 B（主二进制） |
| `…\bin\ext\x7k2m9p4q1w864.dll` | 3,236,912 B（native payload 库） |
| `…\bin\ext\cli-common-shim-*.exe` | 5–6 MB，`dws` / `wecom-cli` 共用 shim |
| `…\.dws-version` | `1.0.65.1` |
| `…\.dws-runtime-manifest.json` | `payload_version 20260909`，payload/library 双 sha256 |
| `C:\Users\11349\.dws\` | `identity.json`、`profiles.json`、`token.json`、`audit\audit-*.jsonl`、`logs\dws.log` |
| `C:\Users\11349\.qwenworkcn\skills\dingtalk-*` | 16 个技能目录 |
| `…\skills\.dws-skill-state.json` | `layout: multi`、`version: 1.0.65.1`、`routingFallbackSkill: dingtalk-shared` |

注意 `bin\dws` 本身是一个 POSIX shim（`export QWORK_SHIM_ROUTE="dws"`，按 `PROCESSOR_ARCHITECTURE` 分发），说明它被设计成"一套代码多宿主路由"，而不是单一二进制的直连。

### 4.3 架构：CLI 做壳，MCP 做核 🟢

`docs/architecture.md` 的原话：

> `dws` is a Go CLI with a versioned, static command surface for DingTalk MCP capabilities. Cobra help serves humans; runtime-assembled Schema (`ResolveSchemaBuild`) serves AI agents.

调用链：

```
cmd → internal/app（Cobra root、static utility、versioned plugin descriptors）
    → internal/helpers（产品命令处理器）
    → internal/executor + internal/transport（执行 MCP JSON-RPC）
    → internal/output（格式化）
```

也就是说：**DWS 自己就是一个 MCP 客户端**，和 iwp-skill 的内置 MCP client 是同一个位置的东西。区别在下一步。

### 4.4 最关键的一处设计：schema 不来自 `tools/list`

Architecture 明确写：

> Startup and Schema queries do not call MCP `tools/list`.

`ResolveSchemaBuild` 的输入是**审过的 `CommandRegistry`**：把每个 identity 绑定到当前 Cobra leaf，再解析 typed constraints、sanitized MCP snapshots、leaf 的 `ContractFinal` / `ProductDecl`，合成一个 `SchemaRegistry`；没有"生成物落盘"的交付步骤，靠 `sync.Once` 懒加载，口号是 **"声明即 Catalog"**。

`internal/corecmd` 则是每个叶子命令的声明式契约 `corecmd.Spec`，一份声明同时产出：

- flag 注册（含 alias / env / default 解析）
- `required` 与跨 flag 约束校验
- **Risk write 确认**（危险操作必须先过确认门）
- 组装 MCP `toolArgs`
- 投影成 Runtime Schema（给 Agent 看的那一份）

**这就是"能力清单被宿主预知"的实现方式**。对比 iwp：工具事实来源是运行时 `tools/list`（ADR-009），客户端在连接前并不知道有哪些工具、更拿不到 read/write/destructive 标签。

### 4.5 五面视图

| 面 | DWS 的做法 | 证据 |
|---|---|---|
| **描述面** | 静态 Schema（`dws schema --compact`），正向字段白名单防上下文膨胀；Help 与 Schema 冲突时取更保守语义 | `internal/cli`、`corecmd.Spec` |
| **身份面** | 当前钉钉用户 OAuth；`profile` 以 `corpId:userId` 唯一标识，支持多组织多账号切换 | `dws auth login`、`profiles.json` |
| **授权面** | 每个 leaf 自带 `effect`（read/write/destructive）、`risk`（low/medium/high）、`confirmation`（user_required）标签；写操作走 Runtime gate，确认后才允许 `--yes` | `internal/safety`、`corecmd.Spec` |
| **执行面** | MCP JSON-RPC over HTTP；`internal/transport` 负责请求签名；`internal/security` 域名白名单（默认 `*.dingtalk.com`）；强制 HTTPS | `internal/executor`、`internal/transport` |
| **治理面** | 本机 `audit/*.jsonl` 哈希链；`internal/keychain` 加密 token（PBKDF2 + AES-256-GCM，密钥绑设备；Keychain / DPAPI）；`internal/logging` 参数脱敏；`pkg/edition` 分 oss / enterprise | README、`internal/audit` |

另外两个能力不属于连接器本体，但属于同一产品的野心：

- **`dws event consume`**：以当前用户身份走托管 Stream WebSocket，33 个事件（IM 各类、7 个 OA 审批、3 个 Todo 生命周期、卡片回调等），每事件一行 NDJSON。→ 补上了"耳朵"。
- **`dws dev connect`**：把钉钉机器人接到本地 AI CLI（Claude Code / Codex / opencode / Qoder / Gemini / Pi / 任意 `--agent-cmd`），@机器人即用本地 Agent 回答。→ **这是"通道"与"连接器"的缝合线**，也是钉钉自己对"Agent 在本地、平台在云上"这个现实给出的答案。

### 4.6 规模与治理的现实（🟡 第三方实测）

用户提供的千问办公实测文档（`钉钉能力全景-通道与连接器.md`，393 行）给出的基线：

| 维度 | 数字 |
|---|---|
| 服务 / 工具 | 42 服务 / 1723 工具 |
| 可用性 | 1692 available / 31 unavailable（fail-closed） |
| 影响面 | read 823 / write 804 / destructive 96 |
| 风险 | low 853 / medium 701 / high 169 |
| 需用户确认 | 457（26.5%） |
| 接口形态 | composite 907（`+` 前缀）/ mcp 802 / local 14 |
| 密度分级 | L1 823 / L2 362 / L3 442 / L4 95 / L5 1（唯一 L5：`dev connect restart`） |

15 个能力轴：消息与群组 256、智能表格 384、文档 163、通讯录 162、开放平台 131、日历会议 116、审批考勤差旅 110、钉盘 86、教育 81、待办日志 60、人事 57、邮件 56、合同 52、aisearch 6、审计 3。

该文档指出的六个问题，值得逐条记下来当反面清单：

1. 免确认的高影响写：`chat message send`、`mail message send`、`oa approval approve`、`doc permission add`、`drive upload`、`contact org create`。
2. 双轨门禁不一致：`oa +approve-by` 需要确认，而等价的 `oa approval approve` 免确认。
3. 敏感读无门禁：`hrm salary`、`hrm performance`、`dev app credentials get`。
4. 递归扩展面：自建 MCP、`mcp published invoke` 不传播标签、数字员工 agent-in-agent。
5. `dev connect restart` 免确认即可破坏运行环境。
6. **无服务级开关**：单个 Profile 就是全量可调用面，只能靠 Skill 白名单 + 门禁 + 审计兜。

对照 iwp 的 ADR-010（MCP 工具集最小化，17 个工具）：**这个决策是对的**。1723 个工具的治理成本不是线性的。

---

## 5. 三方对照：`dingtalk-mcp` / DWS / iwp-skill

| 维度 | dingtalk-mcp | DWS | iwp-skill（现状） |
|---|---|---|---|
| 形态 | MCP server（stdio） | CLI + 内置 MCP client | CLI + 内置 MCP client + skill |
| 语言 | TypeScript（编译产物） | Go | Python |
| 工具来源 | YAML DSL → 运行时装配 | `CommandRegistry` → 静态 Schema，**不调 tools/list** | 运行时 `tools/list`（ADR-009） |
| 工具规模 | 98 / 14 profile | 1723 / 42 服务 | 17 |
| 身份 | App Token（应用身份，userid 当参数） | 用户 OAuth（sub 来自 token） | 用户 OAuth + PKCE（sub 来自 token） |
| 授权流程 | 应用密钥换 token，无 PKCE | 设备码 / 浏览器 OAuth；token 加密落盘 | 授权码 + PKCE + loopback 回调，token Fernet 加密 |
| 元数据发现 | 无 | 无（预注册） | 部分：有 8414 探测，无 PRM / 401 头 / DCR |
| 风险标签 | 无（`responseTemplate: {}`） | 每 leaf 有 effect / risk / confirmation | **无**，靠 SKILL.md 的提示词约定 |
| 确认门禁 | 无 | 代码层 Runtime gate + `--yes` | SKILL.md 要求"先出全字段预览表" |
| 审计 | 无 | JSONL 哈希链 | 无 |
| 入站事件 | 无（只发不收） | `event consume`（33 事件，NDJSON） | 无 |
| 多身份 | 单 token | profile = `corpId:userId`，可切换 | 单 token |
| 技能 | 无 | 16 个产品技能 + shared；mono / multi 两种布局 | 1 个 SKILL.md，5 条路由 |
| 分发 | npm + npx | `npx skills add` + 内置 `dws skill setup` | `npx skills add` |
| 更新 | npm 版本 | `.dws-version` + runtime manifest 双 sha256 | `update_check.py` PSF 指纹（ADR-015） |

读这张表的正确方式：**同样的架构，治理成熟度差一个数量级**。iwp 的 17 个工具、单身份、单技能，正好是 DWS 的一个"最小可行子集"；DWS 用 51MB 二进制、九道门禁流水线、1723 个工具换来的，是那四样 iwp 没有的东西：静态清单、风险标签、机制化门禁、可审计执行。

---

## 6. 回到本会话的问题：iwp 该怎么定位

### 6.1 现状坐标

iwp-skill = **第一方 CLI 客户端 + OAuth 2.1 授权码 + PKCE + 预注册**，不是 MCP 自动发现（判据：自动发现要求客户端启动前不知道 AS 地址与 client_id；iwp 的端点和 `client_id=iwp-skill` 都是硬编码 + 服务端静态白名单）。

它在"连接器五面"里：描述面（tools/list 动态）、身份面（OAuth sub）、授权面（缺标签）、执行面（HTTP JSON-RPC）、治理面（无审计）——**身份面做得比 dingtalk-mcp 好，授权面与治理面基本是空的**。

### 6.2 六条可借鉴项（按性价比排序，全部不依赖插件系统）

| # | 借鉴项 | DWS 的做法 | iwp 的落点 | 成本 |
|---|---|---|---|---|
| 1 | 风险标签进契约 | `corecmd.Spec` 声明 effect / risk / confirmation，同时投影成 Schema 与门禁 | 在 `swagger_meta.py` 或工具清单里为本方 17 个工具补 `effect/risk/confirmation`，并让 `cli.py` 在执行前读取 | 0.5–1 天 |
| 2 | 确认从提示词变机制 | `internal/safety` 的 Runtime gate + `--yes` | 写工具强制 `--dry-run` 出预览，`--yes` 才落地；`SKILL.md` 只描述调用方式，不承担安全 | 1–2 天 |
| 3 | 本地审计 | `audit/*.jsonl` 哈希链 | 每次 `call` 追加一行 JSONL（工具、参数摘要、结果码、耗时），做链式哈希 | 1 天 |
| 4 | 拆包 | 16 个产品技能 + 1 个 shared，`routingFallbackSkill` 兜底 | 1 个 SKILL.md 的 5 条路由 → 1 个连接器技能 + 3 个场景技能（任务 / 周报 / 自由） | 0.5 天 |
| 5 | 多身份 | profile = `corpId:userId` | 多账号切换（不同课题组的身份不同时成立） | 视需求 |
| 6 | 入站事件 | `event consume` | 若要做"任务变更通知 / 周报提醒"，需要服务端提供事件通道；当前无 | 大 |

前三条的共同点：**把"安全"从文本移进代码**。这也是 DWS 与 iwp 当前最本质的差距——iwp 的授权铁律（先 preflight、先查 auth status、写操作先出预览表）写在 `SKILL.md` 里，靠模型自觉；DWS 写在 Go 里，模型绕不过去。

### 6.3 关于"像钉钉插件那样做"

回到最初的问题框架：**以"插件包含连接器 + 技能"的形态接入 IWP 是否更好？**

- **结构上是对的**：DWS 就是"一个 CLI（连接器）+ 16 个技能（流程知识）+ 一份清单（注册物）"，与"插件包含连接器 + 技能"完全同构。这条路是通的。
- **但插件不是必需项**：DWS 的技能通过 `npx skills add` 分发，与 iwp 同一条通道；它的"插件感"来自 `dws skill setup` 把技能写进宿主目录 + state 文件，而不是来自某个平台的插件注册表。
- **真正拉开差距的是治理层**：静态清单、风险标签、门禁、审计。这四样与"是不是插件"无关，与"谁是可信执行者"有关。
- **因此下一步不该是"做成插件"，而是"把治理层从提示词搬进代码"**，然后用同一个连接器去适配不同壳（声明式插件、skill 目录、MCP 端点）。

### 6.4 未解决问题

1. `dws` 是否提供纯 MCP 端点（不经过 CLI），让不支持 shell 的宿主直连？——开源仓库可查 `internal/transport`，未核。
2. DWS 的静态 Schema 如何与 MCP server 端工具漂移对齐（若服务端新增工具，客户端何时知道）？——`syncdata` 是"从 Wukong baseline 生成"，生成频率未知。
3. 千问办公 / WorkBuddy 是否支持第三方 MCP 直连——只查到前端渲染页面，未验证。
4. iwp 服务端是否能提供 `/.well-known/oauth-protected-resource` 与 401 元数据头（MCP 规范的 MUST），需服务端配合。

---

## 7. 证据索引

### 一手（本机可复核）

| 内容 | 路径 |
|---|---|
| DWS 架构文档 | `DingTalk-Real-AI/dingtalk-workspace-cli` → `docs/architecture.md` |
| 技能布局与 state | `C:\Users\11349\.qwenworkcn\skills\.dws-skill-state.json` |
| 单个产品技能样例 | `C:\Users\11349\.qwenworkcn\skills\dingtalk-todo\SKILL.md`（11,749 B）+ `references/todo.md`（22,992 B） |
| 共享技能 | `C:\Users\11349\.qwenworkcn\skills\dingtalk-shared\`（`intent-guide.md` 54,913 B、`lite-catalog.md` 18,055 B 等 14 份） |
| 运行时落盘 | `C:\Users\11349\.dws\{identity,profiles,token}.json`、`audit\audit-20261111.jsonl`（12,841 B） |
| 版本与载荷 | `C:\Users\11349\.qwenworkcn\bin\ext\.dws-version`、`.dws-runtime-manifest.json` |
| iwp 授权实现 | 本仓库 `scripts/auth.py:1-17`、`105-107`、`408-419`；`skill_config.py:63-104`；`scripts/preflight.py:36` |

### 三方分析（用户提供）

| 内容 | 路径 |
|---|---|
| 钉钉能力全景（通道 / 连接器 / 1723 工具基线） | `C:\Users\11349\.qwenworkcn\workspace\2026-11-11\mwbjjf79582ev5gs\outputs\钉钉能力全景-通道与连接器.md` |
| DWS 白盒审查清单 | `C:\Users\11349\.qwenworkcn\workspace\2026-11-11\mwbgb7d1w9uqgud3\outputs\钉钉DWS连接器白盒审查清单.md` |
| 原始 schema 导出 | 同目录 `dws_schema_all.json`（4,524,137 B）、`analyze_dws.py`（7,317 B）、`analysis_report.md`（64,928 B） |

### 官方文档（链接）

- 自定义能力鉴权：https://open.dingtalk.com/document/ai-dev/actions-permission-description-1.md
- 拟人操作协议：https://open.dingtalk.com/document/ai-dev/adaptive-plug-in-description-protocol-2.md
- MCP 插件技能：https://open.dingtalk.com/document/ai-dev/create-mcp-plug-in-skills-in-ai-assistant.md
- Stream 模式：https://help.dingtalk.io/zh/open/development/introduction-to-stream-mode
- MCP 授权规范（2025-06-18）：https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization
- DWS 仓库：https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli
