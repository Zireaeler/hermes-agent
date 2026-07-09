# Hermes Kanban Runtime Kernel Phase 4F：Runtime Capability / Security Policy

Phase 4F 的目标是把 runtime kernel 的安全和权限边界从分散判断收敛成一套
本地、可审计、可测试的 runtime capability policy。

Phase 4E 已经补强 worker recovery、materialization reconcile 和 runtime
consistency。下一步不能直接进入 synthetic long-run soak 或 dashboard UI，因为真实
长任务会持续创建 execution node、调用 worker lane、读取 artifact、写 workspace、
运行命令、访问网络、触碰 git 和请求外部资源。如果这些动作的权限判断散落在
patch validator、worker context、CLI 参数、dashboard API 或 worker lane 内部，
runtime 很快会失去统一安全边界。

Phase 4F 不是让 LLM 变得“更谨慎”。它是本地 policy 层：provider 可以提出需要
哪些 capability，worker 可以按 node contract 执行局部任务，但 capability 是否
允许、是否需要 human gate、是否被拒绝，必须由 runtime kernel 和本地 validator
决定。

## 1. 目标

Phase 4F 要实现一套最小但可运行的 capability / security policy MVP。

目标包括：

- 定义 runtime capability taxonomy；
- 定义 job / node / lane 的 capability policy 输入；
- 让 graph patch validator 校验 node capability request；
- 让 materialization 时的 worker context 明确可用和禁止的 capability；
- 让需要用户授权的 capability 进入 human gate，而不是由模型自行默认；
- 让 policy block 成为结构事件和 observability 一等字段；
- 让普通内部实现路径不被过度 human gate；
- 为后续 long-run soak 提供统一安全边界。

## 2. 非目标

Phase 4F 不实现操作系统级 sandbox。

Phase 4F 不替代 worker backend 自身的安全机制。Codex、Claude Code、本地脚本、
容器、远程 runner 可以继续有自己的 sandbox 和 allowlist，但 runtime kernel 必须
在调度前表达“这个 node 被允许请求什么”。

Phase 4F 不允许 LLM 直接授权自己。LLM 可以 propose `request_human`，也可以在
`create_node` / `strategy_update` 中声明 capability request，但是否允许由本地
policy 决定。

Phase 4F 不解决所有企业级 IAM、租户隔离和 secret vault 问题。第一版只做 runtime
job 级安全边界和可审计授权事实。

## 3. 核心原则

### 3.1 Capability 是显式 contract，不是隐式行为

每个 execution node 应明确声明自己需要的 capability。未声明 capability 的 node
默认只能执行最低风险的本地、非破坏性、workspace 内操作。

### 3.2 Policy 是本地事实

Capability policy 来自 DB state、job metadata、workspace policy、lane policy、
human decision 和默认 runtime policy。它不能来自 LLM 隐性判断。

### 3.3 Human gate 只用于真实授权边界

需要 credentials、外部费用、破坏性操作、workspace 外访问、高影响 git 操作或 DB
migration 时，可以触发 human gate。普通文件组织、mock-first 实现、测试创建和
非破坏性 workspace 内改动不应要求用户确认。

### 3.4 Provider 只能 propose

Decision provider 只能提出 capability request 或 human gate proposal。它不能直接
写入 allow decision，不能绕过 validator，也不能在 provider prompt 中获得隐藏权限。

### 3.5 Worker context 必须携带 policy

Worker 启动时应看到 node 被允许和禁止的 capability，以及哪些动作需要停止并返回
`human_required` / `blocked` receipt。Worker receipt 仍是节点交付契约，不是
runtime compaction。

## 4. Capability Taxonomy

第一版建议定义以下 capability key：

```text
filesystem_read
filesystem_write
workspace_write
workspace_escape
network_access
secret_access
external_cost
destructive_action
git_read
git_write
db_read
db_migration
process_spawn
long_running_process
```

含义：

- `filesystem_read`：读取本地文件；
- `filesystem_write`：写入本地文件；
- `workspace_write`：在 job workspace 内写入；
- `workspace_escape`：访问 workspace 外路径；
- `network_access`：访问网络；
- `secret_access`：读取 credential、token、API key 或 secret vault；
- `external_cost`：调用可能产生费用的外部 API 或服务；
- `destructive_action`：删除数据、覆盖不可恢复文件、执行破坏性迁移；
- `git_read`：读取 git 状态、diff、log；
- `git_write`：commit、branch、merge、rebase、push 或修改 git refs；
- `db_read`：读取项目数据库或 runtime 外业务数据库；
- `db_migration`：执行 schema migration 或 destructive SQL；
- `process_spawn`：启动本地进程、脚本、测试命令；
- `long_running_process`：启动长期驻留服务或 daemon。

默认策略应保守但不阻塞常规实现：

```json
{
  "allow_by_default": [
    "filesystem_read",
    "workspace_write",
    "git_read",
    "process_spawn"
  ],
  "require_human": [
    "workspace_escape",
    "secret_access",
    "external_cost",
    "destructive_action",
    "git_write",
    "db_migration",
    "long_running_process"
  ],
  "deny_by_default": [
    "network_access",
    "db_read"
  ]
}
```

`network_access` 是否默认 deny 可以后续按任务类型调整。第一版建议 deny 或 require
human，避免真实 provider/worker 在 long-run soak 中隐式访问外部资源。

## 5. 数据模型建议

### 5.1 runtime_capability_policies

建议新增表：

```text
runtime_capability_policies
```

字段：

```text
id
job_id
scope_type
scope_ref
policy_revision
allow_json
deny_json
require_human_json
defaults_json
source
created_at
updated_at
metadata_json
```

`scope_type` 可为：

```text
job
node_type
lane
workspace
global_default
```

第一版可以只实现 `job` 和 `global_default`，但 schema 应留出扩展空间。

### 5.2 node capability metadata

第一版可以先复用 `execution_nodes.metadata_json`：

```json
{
  "requested_capabilities": [
    "workspace_write",
    "process_spawn"
  ],
  "capability_policy": {
    "allowed": ["workspace_write", "process_spawn"],
    "denied": [],
    "requires_human": []
  }
}
```

后续如果查询压力变大，再拆出 `runtime_node_capabilities` 表。

### 5.3 human decision 绑定

Human gate decision 应写入 event / ledger / policy metadata，表达：

```json
{
  "decision_type": "capability_authorization",
  "capabilities": ["network_access"],
  "scope": "job",
  "expires_at": null,
  "reason": "User allowed network access for package metadata lookup."
}
```

授权不能只存在 provider context 中。

## 6. Graph Patch Validator 接入

### 6.1 create_node / strategy_update

`create_node` 和 `strategy_update` 可以接受：

```json
{
  "requested_capabilities": [
    "workspace_write",
    "process_spawn"
  ]
}
```

Validator 必须检查：

- capability key 是否已知；
- requested capability 是否与 node_type、lane、workspace policy 相容；
- denied capability 是否被请求；
- require_human capability 是否已有有效 human decision；
- 若没有授权，patch 是否同时创建合法 `request_human` node；
- LLM 不得通过 metadata 自行写入 allowed capability。

### 6.2 request_human

`request_human` 支持 capability authorization：

```json
{
  "op": "request_human",
  "decision_type": "permission",
  "capability_request": {
    "capabilities": ["network_access"],
    "scope": "job",
    "reason": "Need to inspect current package registry metadata."
  }
}
```

Validator 必须要求：

- `why_user_required` 清楚说明风险；
- `risk_if_defaulted` 或等价字段说明默认拒绝的影响；
- `affected_goal_items` 或 `goal_item_keys` 存在；
- capability request 不包含未知 key。

## 7. Materialization 接入

Worker context 应加入一段机器可读 policy footer：

```json
{
  "runtime_capability_policy": {
    "allowed": ["filesystem_read", "workspace_write", "git_read", "process_spawn"],
    "denied": ["workspace_escape", "secret_access", "external_cost"],
    "requires_human": ["network_access", "git_write", "db_migration"],
    "on_denied": "return receipt with verdict=blocked and blocked_reason=policy_blocked",
    "on_requires_human": "return receipt with human_required=true"
  }
}
```

Worker 不应被要求自己推导安全边界。它只需要遵守 context 中的 allowed/denied。

Materialization 前如果 node 请求了 denied capability，runtime 不应创建 Kanban task。
它应记录 `policy_blocked` 或 `capability_denied` event，并让 job 进入合法 waiting /
blocked 状态。

## 8. Runtime Events

建议新增 event_type：

```text
capability_policy_created
capability_policy_updated
capability_request_evaluated
capability_denied
capability_requires_human
capability_authorized
capability_policy_blocked
```

事件 payload 必须包含：

```text
job_id
node_id/node_key
requested_capabilities
allowed
denied
requires_human
policy_revision
decision_id 或 human_decision_ref
reason
```

## 9. Observability

`runtime_observability_snapshot()` 应新增：

```json
{
  "capabilities": {
    "policy_revision": 1,
    "allowed_by_default": [],
    "require_human": [],
    "denied_by_default": [],
    "blocked_nodes": [],
    "pending_authorizations": [],
    "recent_policy_events": []
  },
  "legal_waiting_reason": "blocked_by_policy"
}
```

Dashboard 第一版不需要复杂 UI，但 API 必须能回答：

- 当前 job 被哪个 capability 卡住；
- 哪个 node 请求了危险能力；
- 是否已有 human decision；
- policy block 是合法等待还是 runtime bug；
- worker context 中实际下发了哪些 allowed/denied capability。

## 10. CLI / API

建议新增 CLI：

```bash
hermes kanban runtime capability <job_id> --json
hermes kanban runtime authorize-capability <job_id> --capability network_access --reason "..." --json
```

第一版也可以只实现 read-only `capability`，authorization 先通过既有 human decision
API 写入，避免提前扩展过多写面。

Dashboard API 建议新增只读 section：

```text
/runtime/jobs/{id}/capabilities
```

写接口必须走 runtime command / human decision API，不允许 dashboard 直接改 policy
表。

## 11. 实现顺序

### Step 1：文档和 taxonomy

- 新增 Phase 4F 文档；
- 更新 roadmap；
- 更新 AGENTS 约束；
- 固定 capability key 和默认 policy。

### Step 2：policy builder

- 实现 `build_runtime_capability_policy(conn, job_id)`；
- 第一版从 global default + job metadata/human decisions 生成；
- 输出 allowed / denied / requires_human。

### Step 3：patch validator 接入

- `create_node` / `strategy_update` 支持 `requested_capabilities`；
- validator 检查未知 capability；
- denied capability 拒绝；
- require_human capability 需要 human gate 或 existing authorization。

### Step 4：materialization 接入

- materialize 前评估 node capability；
- denied / requires_human 时不创建 task；
- worker context 写入 capability policy footer。

### Step 5：observability / CLI

- runtime inspect 暴露 capability summary；
- 新增 read-only CLI；
- dashboard API 增加 read-only section。

### Step 6：tests

- focused deterministic tests 覆盖 validator、materialization、observability。

## 12. 测试要求

必须覆盖：

- unknown capability 被 validator 拒绝；
- `workspace_write` / `process_spawn` 这类默认允许 capability 不触发 human gate；
- `secret_access` / `external_cost` / `destructive_action` 请求必须 human gate；
- denied capability 不会 materialize worker task；
- require_human capability 没有授权时创建或要求 human gate；
- human decision 授权后同 scope capability 可以通过；
- worker context 包含 allowed / denied / requires_human；
- LLM 不能通过 metadata 写入 allowed capability；
- dashboard/read-only API 不直接修改 policy；
- policy blocked job 的 `legal_waiting_reason` 是 `blocked_by_policy`，不是
  liveness violation；
- 默认测试离线，不依赖真实 provider、网络或 secret。

## 13. 完成定义

Phase 4F MVP 完成时必须满足：

- runtime 有统一 capability taxonomy 和 default policy；
- graph patch validator 能处理 node capability request；
- materialization 会阻止未授权危险 capability；
- worker context 明确下发 capability policy；
- human gate 是 capability authorization 的唯一升级路径；
- observability 能解释 policy block；
- focused tests 证明普通内部实现不被过度 gate，危险动作不会静默执行。

Phase 4F 完成后，再进入：

```text
Phase 4G Synthetic Long-Run Soak and Real Compaction Smoke
Phase 4H Dashboard Runtime UI
```
