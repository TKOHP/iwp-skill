# DWS 源码导读：钉钉工作台 CLI 的实现解剖

> 对象：`DingTalk-Real-AI/dingtalk-workspace-cli`（Apache-2.0）
> 源码快照：Gitee 归档 `main`，2026-11-11 拉取，压缩包 58,370,887 B；解压在 `D:\workproject\MAS\_ref\dingtalk-workspace-cli-main`
> 本机安装版本：1.0.65.1（npm 最新 1.0.67），因此源码与已发布技能的内容会有少量差异，下文凡涉及版本差异处均标注。
> 用途：只供人阅读，不属于 iwp-skill 的运行文件。

---

## 0. 三分钟结论

DWS 不是一个"把 OpenAPI 包一层的命令行工具"，它在做四件相互耦合的事：

1. **把钉钉 1723 个工具压缩成一份可被机器读的声明**（`corecmd.Spec` + `ContractDecl`），一份声明同时喂给 `--help`、Agent Schema、dry-run、运行时校验。这是它和普通 CLI 最本质的区别。
2. **启动时与 Schema 查询时不调用 MCP `tools/list`**。工具面是编译期装配的静态表，不是运行时发现的。这是它敢把 1723 个工具交给 LLM 的前提（确定性 + 可审查 + 零发现延迟）。
3. **执行的是一条统一的 `tools/call` JSON-RPC**，CLI 只是这台内核的一个投影；`output.MCPResult` 的注释写明 CLI 与 MCP 适配器共用同一份不可变 `CommandResult`。
4. **把"确认"做成机制而不是提示词**：`Confirmation=user_required` 时非交互环境直接失败（`confirmation_required`），而不是静默跳过写操作。

体量：**2,696 个 Go 文件（排除 third_party）、约 83.1 万行、33.2 MB**；`go 1.26.7`。

---

## 1. 目录地图

| 路径 | 职责 | 体量 |
|---|---|---|
| `cmd/main.go` | 进程入口：Schema 缓存 worker、遥测 worker、执行主流程 | 149 行主体 |
| `internal/app` | root Cobra 装配、静态 utility（auth/audit/schema/completion）、versioned plugin descriptors | 336 文件 / 3,744 KB |
| `internal/helpers` | 各产品命令处理器（chat、doc、aitable……） | 701 文件 / 11,390 KB |
| `internal/corecmd` | 声明式 leaf 框架：`Spec`、`ConfirmSafety`、flag 解析、约束校验 | 53 文件 / 486 KB |
| `internal/cli` | Schema 装配与 catalog 契约（`ResolveSchemaBuild`） | 164 文件 / 2,441 KB |
| `internal/shortcut` | `+verb` 高层封装（adapter / runner） | 519 文件 / 7,084 KB |
| `internal/executor` | 调用意图与结果类型（`Invocation` / `Runner`） | 2 文件 / 6.6 KB |
| `internal/transport` | MCP JSON-RPC HTTP 客户端、协议版本协商、重试、快照 | 18 文件 / 206 KB |
| `internal/auth` | device flow、OAuth 码交换、token 存储、profile、keychain | 108 文件 / 1,450 KB |
| `internal/event` | 事件订阅（Stream）类型与端点 | 128 文件 / 1,078 KB |
| `internal/output` | json/table/raw、jq、fields、ndjson、csv、脱敏、`MCPResult` | 36 文件 / 392 KB |
| `internal/audit` | 哈希链审计、轮转、脱敏、文件锁 | 14 文件 / 49 KB |
| `internal/plugin` | 第三方插件清单（MCP server / skills / hooks / userConfig） | 8 文件 / 107 KB |
| `internal/schemacache` | Schema 本地缓存（Windows 40 KB / Unix 27 KB 平台实现） | 19 文件 / 274 KB |
| `internal/runtimepayload` | 平台原生库加载（`x7k2m9p4q1w864.dll` 等） | 15 文件 / 88 KB |
| `internal/security` | 端点白名单 / 域名信任 | 7 文件 / 34 KB |
| `internal/safety` | **只有提示注入扫描**，不是确认门 | 2 文件 / 7.7 KB |
| `skills/` | mono / multi 两套技能树，编译期嵌入二进制 | — |
| `docs/` | 60+ 篇设计文档，含 138 KB 与 50 KB 的两份 RFC | — |

`go.mod` 有三处 `replace` 指向仓库内副本：`aem-go-sdk`、`safechat-go-sdk`、**`spf13/cobra`（"v1.10.2 with a Traverse flag-error fix"）**——连 cobra 都打了补丁，说明命令框架层被深度定制过。

---

## 2. 进程启动：`cmd/main.go`

`run()` 的顺序值得注意，因为它暴露了三条旁路：

```go
if handled, code := runSchemaCacheBuilder(os.Args[1:], os.Stdout); handled { return code }
if telemetry.RunWorker(os.Args[1:], os.Stdin) { return 0 }
agentInfo := resolveAgentInfo()
optedOut := telemetry.OptedOut()
if !optedOut { identity = snapshotTelemetryIdentity(os.Args[1:]) }
command := filepath.Base(os.Args[0])
code, path, message := appExecute()
// 组 telemetry.Event{Version, Command, Path, ExitCode, DurationMillis,
//   CompletedAtMillis, ErrorSummary, AgentInfo} 并 submitTelemetry
```

三个可观察的事实：

- **Schema 缓存有独立子命令**（`RunSchemaCacheBuilder`），可以在不跑主流程的前提下重建缓存。
- **遥测是内建的**：事件含版本、命令、命令路径、退出码、耗时、错误摘要、以及 `runtimecontext.ResolveAgent` 探测到的宿主 Agent 身份。有 opt-out（`telemetry.OptedOut()`），私有 worker 提前返回不采集。
- **按二进制名区分入口**（`filepath.Base(os.Args[0])`），说明同一份代码可能以 `dws`、`wecom-cli` 等不同名字安装。

---

## 3. 一次调用穿过什么

```
cobra leaf
  └─ corecmd.Spec（声明）
       ├─ Flags → 解析：主 flag(Changed) → 别名 → env → default → ArgDefault
       ├─ Constraints → 至少一个 / 恰好一个 / 互斥
       ├─ SafetySpec → [ConfirmFirst? 先确认] → 校验 → 建 toolArgs → [再确认]
       ├─ Invoke(c *Ctx, toolArgs map[string]any)
       │    └─ executor.Invocation{Kind, Tool, CanonicalPath, Params}
       │         └─ transport.Client.CallTool(endpoint, tool, arguments)
       │              └─ {"jsonrpc":"2.0","id":3,"method":"tools/call",
       │                  "params":{"name":tool,"arguments":params}}
       │                   → 钉钉网关 → 业务 RPC
       └─ output：CommandResult → json|table|raw / --jq / --fields / ndjson
```

关键点在中间那一层：**声明面不碰后端，执行面不发明 CLI 表面**（`docs/command-domain-model.md` 的六条不变量之二）。所以 `--dry-run` 可以在任何命令上打印出真实请求体而不需要后端存在。

---

## 4. 声明层：`corecmd.Spec`

一个 leaf 命令的全部事实写在一个 struct 里（`docs/command-framework-architecture.md`）：

| 字段 | 作用 |
|---|---|
| `Use / Short / Long / Example` | 帮助文本 |
| `Flags []FlagSpec` | 参数声明：Name/Usage/Kind/Default/Required/Aliases/EnvVar/**Bind**/Transform/Enum/Format |
| `Constraints []Constraint` | `at_least_one` / `exactly_one` / `mutually_exclusive` |
| `Safety contract.SafetySpec` | 风险契约（见 §5） |
| `ConfirmFirst bool` | 确认放在校验之前还是之后 |
| `ConstParams map[string]any` | 常量参数（不进 CLI 表面但进 toolArgs） |
| `Contract ContractDecl` | 给 Agent 的元数据（见下） |
| `Invoke` / `Orchestrate` / `RunE` | 执行面，**三者恰好一个**（RunE 是逃生舱） |
| `Validate` / `PostMount` | 钩子 |

`FlagSpec.Bind` 是"CLI 名 ≠ 接口名"时的映射点：`--table-id` 可以绑定到 `tableId`，参数到接口属性的映射由 leaf 的 `ParamDecl.Property` 拥有，而不是靠字符串推断。

`ContractDecl` 是给模型的第二层（`docs/command-domain-model.md`）：

- `SelectionSpec{AgentSummary, UseWhen[], AvoidWhen[], Prerequisites[], Tips[], Examples[]}`——每个公开工具都有人写的"什么时候用 / 什么时候别用 / 例子"。
- `InterfaceSpec{Mode: local|mcp|composite, Availability, Ref{ProductID, RPCName}}`——这个命令背后到底是本地实现、一次 MCP 调用，还是多次调用编排。
- `DryRunSpec{PreviewKind: invocation|request|plan, RemoteReads}`——dry-run 预览到什么粒度，以及预览本身会不会读远端。
- `Positionals`、`ToolIdentitySpec`、`Title/Description`。

**构建期校验，失败直接 panic**：`validateDispatchDecl → validateSafetySpec → validateContractDecl → RegisterFlags → ValidateConstraintDecls → embedContractIntoSchema → AnnotateConstraints → PostMount`。这是"构建时拦截 > 运行时报错"原则的落地：声明写错，二进制编不出来。

三档迁移（老代码怎么进来）：

| 档 | 入口 | 含义 |
|---|---|---|
| Tier 1 | `corecmd.New` / `NewLeafCommand` | 完全托管 |
| Tier 2 | `DeclareLeafMetadata` | 只补 Safety+Contract，执行体冻结；无 `Validate` 时确认推迟到 gated `CallTool`，调用成功却未确认则 fail-closed |
| Tier 3 | 裸 Cobra | 待收敛 |

---

## 5. 契约层：四个风险字段为什么必须独立

```go
type SafetySpec struct {
    Effect       string // read | write | destructive
    Risk         string // low | medium | high
    Confirmation string // not_required | user_required
    Idempotency  string // idempotent | retryable | non_idempotent | unknown
}
```

三条规则（源码与文档一致）：

1. **字段互不推导**。框架只读 `Confirmation` 决定要不要在运行时弹确认；`Effect/Risk/Idempotency` 原样发布到 Schema，供调用方判断。写操作不一定需要确认，危险操作也可以声明不需要确认——判断权交给人，而不是由 `Effect=write` 自动推成"必须确认"。
2. **非空必须四个一起声明**，部分声明在构建期被拒；全空表示"历史只读默认" `read/low/not_required/idempotent`。
3. **声明即 review**：安全字段是评审对象，不是运行时猜出来的。

这套设计直接回应了一个真实问题：钉钉侧 1723 个工具里，`confirmation=user_required` 只占 26.5%，`chat message send`、`mail message send`、`oa approval approve` 这类高影响写操作是**免确认**的。DWS 的答案是"让作者显式声明 + 让宿主看见声明"，而不是让框架替作者猜。

---

## 6. Schema 层：整套设计的重心

### 6.1 四种投影，一份事实

| 投影 | 消费方 | 实现 |
|---|---|---|
| Agent Runtime Schema | 模型 | `dws.schema.*` cobra annotations，`runtimeannotate` / `ContractFinal` |
| SchemaRegistry / Catalog | `dws schema`、`dws schema --all` | `RegisterSchemaSourceRoot` → `ResolveSchemaBuild` |
| CommandMeta | 运行时 O(1) 查询 | `map[cli_path]CommandMeta`，`ResolveMeta` / `SafetyForCLIPath` |
| Dry-run Capabilities | `--dry-run` | `Invocation` + `EchoRunner` |

**生产权威是 leaf 的 `ContractFinal` / `ProductDecl`**。`InstallBuildTimeAgentMetadataJSON` 只给 CI dump 用；`schema_agent_metadata/`、`schema_hints/` 已退役；Catalog 与 meta-index 路径禁止提交。

### 6.2 `ResolveSchemaBuild` 三段

```go
type ResolvedSchemaBuild struct {
    effective EffectiveCommandRegistry
    bound     BoundCommandRegistry
    registry  SchemaRegistry
    root      *cobra.Command // 私有，防止调用方自拼"看似已解析"的结果
}
```

`ResolveSchemaBuild(root)` = `resolveEffectiveCommandRegistry` → `resolveBoundCommandRegistry` → `resolveAssembleSchemaRegistry`，**一次解析产出三个视图**。源码注释：

> Command discovery is intentionally impossible below this boundary: callers must first provide a fail-closed bound registry.

也就是：下游拿不到"自己去扫 cobra 树"的能力，必须先交出一个已经绑定、缺声明就失败的注册表。`assembleSchemaRegistryFromBound` 逐条走 `ContractFinal`/`ProductDecl`，缺声明 fail-closed。

`pinnedRuntimeSchemaMetadataSources()` 现在返回空，注释写明：

> Production pin and Agent inject are both retired; assembly is Contract / ParamDecl / Cobra only.

### 6.3 为什么"不调 `tools/list`"是核心决策

`docs/architecture.md` 原话：

> `dws` is a Go CLI with a versioned, static command surface for DingTalk MCP capabilities. Cobra help serves humans; runtime-assembled Schema (`ResolveSchemaBuild`) serves AI agents.

推论（我认为这是整个项目最值得抄的一条）：

- **确定性**：同一二进制、同一版本 → 同一工具面。模型看到的世界不会因为后端灰度而抖动。
- **可审查**：Schema 可以被 PR review、被 CI 快照、被 `interface-snapshot` 工具比对。
- **零发现成本**：`dws schema --compact` 是本地计算，不花一次网络往返。
- **可裁剪**：`--compact` 用**正向字段白名单**，后端新增字段不会悄悄撑大上下文。
- 代价：每次后端新增接口，CLI 必须发版。所以它有 `internal/syncdata`（从 Wukong baseline 生成的静态端点数据）与 `fetch_mcp_metadata` 工具链来降低这个成本。

对比 iwp-skill：iwp 走的是 `tools/list` 动态发现（ADR-009 明确"工具事实来源=MCP `tools/list`"）。两者都能工作，但代价结构相反——DWS 用发版换确定性，iwp 用确定性换零维护。

---

## 7. 执行层

### 7.1 `executor.Invocation`

```go
type Invocation struct {
    Kind, Stage string
    Implemented bool
    DryRun      bool
    CanonicalProduct, Tool, CanonicalPath, LegacyPath string
    Params map[string]any
}
type Runner interface { Run(context.Context, Invocation) (Result, error) }
```

`EchoRunner` 在 `DryRun` 时返回 `{dry_run:true, request: ToolCallRequest(...), note:"execution skipped by --dry-run"}`——dry-run 不是"假装成功"，而是**把真实请求体打印出来并标记未执行**。

`NewCompatibilityInvocation` / `NewHelperInvocation` 的 `Implemented:false` 是显式的"这条路还没接"信号，避免调用方误以为已支持。

### 7.2 `transport.Client`

| 能力 | 位置 / 行为 |
|---|---|
| `Initialize` | 逐个尝试所有支持的 MCP 协议版本；单端点 3 次 3 s 拨号≈9 s；全失败报 `initialize failed for all supported protocol versions at %s` |
| `ListTools` / `ListToolsPage` | 存在，但主路径**不用**它建 Schema |
| `CallTool` | `tools/call` |
| 重试 | `WithMaxRetries` + `doWithRetry` |
| 超时 | `WithHTTPTimeout` |
| 重定向 | `WithRedirectsDisabled`（默认禁，防止凭证被带去别的主机） |
| 快照 | `SnapshotRecorder.RecordJSONRPC(method, endpoint, req, resp)`——可回放 |
| 日志 | `logging.LogRequest/LogResponse` 带 `ExecutionId`，参数脱敏 |
| 回环 HTTP | 仅 `DWS_ALLOW_HTTP_ENDPOINTS` 允许 |

注意 `Initialize` 的多版本协商：它假设后端可能是 2025-03-26 / 06-18 / 11-25 任一版本，逐个试而不是猜一个。这对"同一客户端面对多代服务端"是必要的，iwp 的 ADR-011 解决的是同一个问题。

### 7.3 CLI 与 MCP 是同一内核的两个投影

`internal/output/mcp.go`：

> `MCPResult` is the protocol-neutral projection consumed by MCP transports. Both CLI and MCP adapters start from the same immutable `CommandResult`; MCP handlers must not rebuild a second business envelope.

这句话直接回答了"连接器是不是 CLI"：**CLI 只是投影之一**。同一份 `CommandResult` 既能出 `--format json`，也能包成 MCP `structuredContent`。DWS 选择先做 CLI，是因为 CLI 的宿主面最广、调试成本最低，不是因为连接器必须是 CLI。

---

## 8. 安全层

### 8.1 确认门 `ConfirmSafety`（`internal/corecmd/corecmd.go:1744`）

```go
if strings.TrimSpace(safety.Confirmation) != "user_required" || confirmationBypass(cmd) { return nil }
```

- 交互判定用 `stdinIsTerminal`（ioctl）；`SetIn`/重定向一律算非交互。
- 提示只在真 TTY 打印：`即将执行 %s（effect=%s, risk=%s），确认继续？(yes/no): `
- **EOF 且空答案 → `confirmationRequiredError`**，注释写明：

> EOF must not be treated as decline: that silently drops writes in agent/CI

  这是整份代码里最值得抄的一行判断。把 EOF 当"用户拒绝"会导致 Agent 场景下写操作被静默吞掉；把它当"需要确认但无法确认"则会把问题暴露给调用方。
- 错误文案：`"%s 需要用户确认，当前环境无法交互；加 --dry-run 预览，或确认后加 --yes 执行"`，`reason=confirmation_required`，`hint="非交互环境（agent/CI）必须显式传入 --yes，不能依赖 stdin 提示"`。
- `--yes` / `--dry-run` 的探测在 cmd / inherited / persistent 三个 flagset 上 **OR**。源码注释记录了一个真实 bug：leaf 局部 `--dry-run=false` 遮蔽了 root persistent `--dry-run=true`，导致"以为在预演，实际写了"。
- 破坏性 sheet 命令另有一道 `--yes`-only 的外层门 `helpers.protectSheetMutationCommand`。

### 8.2 其余四道

| 机制 | 位置 | 要点 |
|---|---|---|
| 提示注入扫描 | `internal/safety/content_scan.go` | 5 条正则（中英各半），`maxFindings: 20`，snippet 截 160 字符；扫的是**返回内容**，不是用户输入 |
| 域名信任 | `internal/security` | 默认 `*.dingtalk.com`，强制 HTTPS，仅回环可 HTTP |
| 凭证存储 | `internal/keychain` + `auth/token.go` | PBKDF2 + AES-256-GCM，密钥绑设备 MAC；Keychain/DPAPI；零凭证落盘（Client Secret 仅内存） |
| 参数脱敏 | `internal/logging` + `output/redaction.go` | 日志与输出两处各自脱敏 |

注意 `internal/safety` 这个名字容易误导：它**只有内容扫描**，确认门在 `corecmd`。读源码时按目录名猜职责会猜错。

---

## 9. 认证层

四类凭证并存（`internal/auth`，108 文件 / 1.45 MB，按体量是全仓第五大包）：

| 类型 | 入口 | 用途 |
|---|---|---|
| 用户 OAuth（浏览器） | `dws auth login` | 常规；本地起回调 |
| 用户 OAuth（设备码） | `dws auth login --device` | 无头环境；`device_flow.go` 27 KB，含 `requestDeviceCode` / `pollDeviceToken` / `waitForAuthorizationByFlowID` |
| 授权码交换 | `dws auth exchange --code-stdin` | 宿主（如 DSH）已经拿到 code 时交给 CLI |
| 应用 token | `dws api` 路径 | 只读 App Token，**不提供 `--as user`** |

多账号模型：profile 的唯一标识是 **`corpId:userId`**（不是邮箱、不是手机号），`--profile <corpId:userId>` 精确路由；跨组织读由 Agent 编排，写默认当前账号。

`auth exchange` 的几个细节体现了工程品味：

- stdin 上限 64 KiB；`--code` 与 stdin 互斥（避免授权码进 argv 被子进程列表看到）。
- **不重试、不换传输**——授权码是一次性的，重试会把"码已用掉"变成"看起来像网络问题"。
- 持久化前先用新 token 查身份，要求恰好一个完整 `corpId:userId`，否则不写。
- `--expected-corp-id` / `--expected-user-id` 是**断言**，不是覆盖。
- `--dry-run` 不读 stdin、不发现应用、不换码、不写凭证。
- 成功返回里**永远不含 token/code**，只含 `{status, dwsProfile, corpId, userId, clientId, expiresAt, currentProfile, isCurrentProfile, useOnce}`。
- 该命令**显式排除在稳定 Agent Schema catalog 之外**（`internal/cli/schema_command_exclusions.go`）——因为让模型自己调它没有意义，且会诱使模型把码写进日志。

---

## 10. 输出层

`internal/output` 12 个文件：

- `formatter.go` / `result.go` / `envelope.go`：统一信封（成功、失败、部分失败、pending 四态）。
- `filter.go`：`--fields` 投影与 `--jq`。
- `ndjson.go`：事件流一行一事件。
- `csv.go`、`pretty.go`：人读格式。
- `redaction.go` / `sanitize.go`：脱敏与净化。
- `mcp.go`：`MCPResult{StructuredContent, IsError}`。
- `rollout.go`：灰度开关。

一个细节：MCP 适配器**不允许重建第二套业务信封**，必须复用 `CommandResult`。这是"单一数据源"原则在输出层的延伸。

---

## 11. 事件层

`internal/event`（128 文件 / 1.08 MB）+ `docs/event-subprocess-contract.md`：

- `SourceKind`：`app_stream`（应用身份）/ `personal_stream`（用户身份）两族。
- `RawEvent{EventType, RuleType, EventBornTime, Data, ...}`；去重键的兜底策略是 `type + born_time + sha256(data)[:16]`。
- `dws event consume`：以当前登录用户身份走托管 Stream WebSocket，每事件一行 NDJSON；33 个公开事件；`+listen-im --kind at-me|sender|group|all-direct|all-group`。
- 同一用户的多个消费者共享一个本地 bus（Unix Socket / Windows Named Pipe），避免重复建连。
- `event subprocess contract` 规定了子进程的生命周期与背压——因为"实时监听"是长驻进程，和一次性 CLI 调用的资源模型不同。

这块是 DWS 从"连接器"跨到"通道"的地方，也是它唯一需要常驻进程的部分。

---

## 12. 技能层

### 12.1 编译期嵌入

`skills_embed.go`：

```go
//go:embed all:skills/mono all:skills/multi
var EmbeddedSkills embed.FS
```

注释说明三件事：

1. `dws skill setup` 安装的是**本二进制自带的那份技能**，与本地 checkout 无关——解决"CLI 升了、技能还是旧的"错配。
2. `all:` 前缀必须有，否则 `_common` 这类下划线目录会被跳过。
3. **`npx skills add` 在 `skills/` 下最多走三层**，所以产品技能必须放在 `skills/multi/<name>/SKILL.md`；mono 巨型技能用 `metadata.internal: true` 对公共安装隐藏，但 `dws skill setup --mode mono` 仍会装它。

### 12.2 两种布局

| | mono | multi（默认） |
|---|---|---|
| 形态 | 一个 `dws` 技能 + `references/products/*`（35 篇，最大 174 KB） | 16 个 `dingtalk-*`，必选 `dingtalk-shared` |
| 上下文 | 一次加载大 | 按产品最小加载 |
| 适合 | 跨产品工作流 | 单产品任务 |
| 切换 | `DWS_SKILL_MODE=mono` 或 `dws skill setup --mode mono|multi` | 默认 |

分发通道与 iwp-skill 完全相同：`npx skills add DingTalk-Real-AI/dingtalk-workspace-cli -g`。

### 12.3 技能合同（可机检）

`docs/skill-content-framework.md` 规定 multi 目录必须满足：

- 目录名 `dingtalk-<product>`，`SKILL.md` 的 `name` 与目录一致；
- `description` 必须含触发意图与**边界**（"不做 X，走 Y"）；
- `metadata.category: product|shared`；`metadata.requires.bins` 必须含 `dws`；
- 契约块 `<!-- DWS_RUNTIME_CONTRACT_START -->…END -->`，或显式 `PREREQUISITE` 指向 `dingtalk-shared`；
- mono 的每个 `references/products/<stem>` 必须在 `skills/content-qa/mono-multi-coverage.yaml` 里有 coverage 或 omit_coverage 行。

门禁 `make skill-mono-multi-content` 检查这些；它不在默认 `make policy` 里，属于独立门。

### 12.4 一个技能长什么样（本机 1.0.65.1 的 `dingtalk-todo`）

frontmatter：`name` / `description`（含触发词 + 排除项）/ `metadata{cli_version, category, requires.bins}` / `install_source: connector` / `connector_id: dws` / `version` / `enabled_at`。

正文六段：**执行契约**（先读 `dingtalk-shared`、先拆步骤、`--format json`、写操作按 Runtime gate 才加 `--yes`、写后读回核验、非幂等写禁止盲目重放）→ **Shortcuts 表**（17 行，每行标 read/write）→ **路由优先级** → **Golden Routes**（意图 → 首选入口 → 边界）→ **低频原子能力** → **关键约束**（ID 纪律、优先级 10/20/30/40、`--due` 是 deadline 而非提醒时间）。

配套 `references/`（5 个文件、37 KB）+ `scripts/`（3 个 Python，批量创建/日报/逾期扫描）。

这套结构值得直接对照 iwp-skill 的 SKILL.md：DWS 的"Golden Routes"和"关键约束"两节，正是 iwp 目前缺的"易错点固化"。

---

## 13. 插件层：DWS 自己也有插件系统

`internal/plugin/manifest.go`：

```go
type Manifest struct {
    Name, Version, Description string
    Type          string   // "managed" | "user"
    MinCLIVersion string
    MCPServers    map[string]*MCPServer
    Skills        string
    Hooks         string
    Permissions   []string
    UserConfig    map[string]ConfigItem
    Build         *BuildConfig
}
type MCPServer struct {
    Type     string // "streamable-http" | "stdio"
    Endpoint string
    Command  string
    Args     []string
    Env, Headers map[string]string
    CLI      json.RawMessage // CLIOverlay，透传
}
```

与 Claude Code 的 `plugin.json` 同构（mcpServers / skills / hooks / userConfig / permissions），差别是多了 `Build{Command, Output}`（插件可以自带构建步骤）和 `CLI` 覆盖层。

这条事实很重要：**DWS 既做连接器，也做插件宿主**。它的插件不是"钉钉侧的注册物"，而是"给 dws 增加 MCP server / 技能 / 钩子的本地扩展"。换句话说，插件宿主是 CLI，不是钉钉云。

---

## 14. 治理层

| 机制 | 事实 |
|---|---|
| 审计 | `internal/audit`：L1 sha256 哈希链，按天轮转，每文件首条从 `""` 起链；`SealFromFile` 只读尾部 64 KiB 取上一条 hash（异常行回退全扫）；`VerifyFile` 逐行验 `prev_hash` 与 `sha256(prevHash‖body)`；跨进程用独占文件锁防叉链 |
| 遥测 | `main.go` 内建事件（版本/命令/路径/退出码/耗时/错误摘要/Agent 来源），可 opt-out |
| 升级 | `dws upgrade --check/--list/--beta/--version/--rollback/-y`；版本查询走 npm Registry；六通道发版后 `verify/verify-all-channels.sh`（SKIP 不算 PASS） |
| 版本策略 | `pkg/edition`：oss 与 enterprise；`MinCLIVersion` 在插件清单里做兼容断言 |
| CI | 九道门禁：Lint / Test / Coverage / Policy / Edition / Interface Integrity / AI Behavior / CLI Smoke / Mock MCP；main 另跑 Multi-profile E2E |
| 风险分级 | 按改动面 fail-closed：docs-only / standard / high-risk+main |
| 评审 | review orchestration 由 base 侧持有：请求 peer 不执行 PR 代码，auto-merge 需最新 push 的 peer 批准 + 九门禁 |

`interface-snapshot` 与 `cmd/fetch_mcp_metadata` 两个独立 main 说明：Schema 面本身是被当作**接口契约**做快照比对的，改动会触发可见的 diff。

---

## 15. 功能面

`docs/command-index.md`（自动生成，来源是 `internal/plugin` 的命令描述符）：

> Products: 13 · Total commands: 180 · Generated from `internal/plugin` command descriptors — the same code path the CLI uses at runtime.

| 产品 | 命令数 | 产品 | 命令数 |
|---|---:|---|---:|
| `aitable` AI 表格 | 41 | `minutes` 听记 | 19 |
| `chat` 群聊/IM | 39 | `calendar` 日程 | 14 |
| `doc` 文档 | 21 | `oa` 审批 | 12 |
| `drive` 云盘 | 11 | `report` 日志 | 7 |
| `contact` 通讯录 | 6 | `todo` 待办 | 6 |
| `attendance` 考勤 | 4 | `devdoc` 开放平台文档 | 2 |
| `ding` DING | 2 | | |

注：逐项相加为 184，与页头 180 不一致，说明有隐藏/排除项（`auth exchange` 这类就被显式排除）。

**180 与 1723 的差别**：索引页写的是"pre 环境配置下的命令面"，而用户实测的生产配置是 42 服务 / 1723 工具。这不是矛盾，而是同一个结论的两个证据：**命令面由环境/edition 的 plugin descriptors 决定，不是硬编码**。

`shortcut` 是第二层命令面：`dws <service> +<verb>`。`FromShortcut` 在边界把 shortcut 的单一 `Risk` 展开成完整 `SafetySpec`，因此 shortcut 不需要重复声明四个安全字段。mono 技能的总览表按服务列了数量（aitable 140、chat 102、doc 49、drive 28、calendar 27……），规模远超原子命令。

---

## 16. 我认为最值得抄的六条

1. **Schema 不从 `tools/list` 现拉**，编译期装配 + CI 快照。确定性是可审查与可裁剪的前提。
2. **一份声明驱动四个投影**（help / schema / catalog / dry-run）。重复声明是安全漏洞的温床。
3. **安全字段互不推导**，且只让 `Confirmation` 驱动运行时行为。
4. **EOF 不是拒绝**。非交互环境必须显式 `--yes`，否则报 `confirmation_required`。
5. **声明错误在构建期 panic**，不在运行期返回 400。
6. **技能编译进二进制**，`skill setup` 装的永远是当前版本的那份。

对 iwp-skill 的直接可移植项（成本从低到高）：风险标签 → 确认门 → 审计链 → schema 缓存 → 技能嵌入。

---

## 17. 自己验证

```bash
# 源码（Gitee 归档，比 git clone 稳）
curl -sL 'https://gitee.com/DingTalk-Real-AI/dingtalk-workspace-cli/repository/archive/main.zip' -o dws.zip
tar -xf dws.zip

# 本机已安装版本
dws version
dws auth status
dws schema --all --compact > schema.json
dws schema "todo +create" --compact --jq '{cli_path,parameters,constraints,confirmation}' -f json
dws todo +create --title test --dry-run          # 看真实请求体，不执行
dws audit verify                                  # 哈希链校验
```

---

## 18. 证据索引（文件 → 事实）

| 文件 | 支撑的事实 |
|---|---|
| `cmd/main.go` | 启动顺序、Schema 缓存 worker、内建遥测 |
| `docs/architecture.md` | Go CLI + 静态命令面；Schema 不调 `tools/list`；九道门禁 |
| `docs/command-framework-architecture.md` | `corecmd.Spec` 全字段、三档迁移、构建期校验、四条投影 |
| `docs/command-domain-model.md` | `ContractDecl` 子结构、六条不变量 |
| `docs/auth-exchange.md` | 授权码交换的全部约束与"永不返回 token" |
| `docs/skill-content-framework.md` | mono/multi 合同、三层发现规则、coverage 门禁 |
| `skills_embed.go` | 技能 `go:embed`；`npx skills add` 三层限制；mono 隐藏策略 |
| `internal/executor/invocation.go` | `Invocation` / `Runner` / `EchoRunner` / `tools/call` 请求体 |
| `internal/transport/client.go` | 协议版本协商、重试、重定向禁用、快照、脱敏日志 |
| `internal/corecmd/corecmd.go:1744` | `ConfirmSafety`、EOF 语义、`confirmation_required` |
| `internal/safety/content_scan.go` | 提示注入扫描（不是确认门） |
| `internal/audit/chain.go` | sha256 哈希链、尾部 64 KiB 优化、验证算法 |
| `internal/cli/schema_runtime_registry.go:78` | `ResolveSchemaBuild`、fail-closed bound registry、pinned sources 退役 |
| `internal/plugin/manifest.go` | 插件清单结构与 Claude Code 同构 |
| `internal/output/mcp.go` | CLI 与 MCP 共用同一 `CommandResult` |
| `internal/event/types.go` | `SourceKind`、`RawEvent`、去重键 |
| `docs/command-index.md` | 13 产品 / 180 命令（pre 配置） |
| `internal/app`（336 文件） | root 装配、versioned plugin descriptors |
| `internal/auth`（108 文件） | device flow、OAuth 码、token、profile、keychain |
