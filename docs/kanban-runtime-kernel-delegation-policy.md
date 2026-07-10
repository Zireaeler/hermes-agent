# Hermes Kanban Runtime Kernel Worker Delegation Policy

## 1. 目的

本文定义 Hermes Runtime Kernel 的 worker 委派与 execution graph 扩展原则。

它解决的不是“如何把计划拆得更细”，而是“何时值得创建 durable runtime node”。默认目标是
在不损害安全、可恢复性、独立验证和证据充分性的前提下，使用最少必要的 runtime node
完成当前 goal gap。

本文是 `docs/kanban-runtime-kernel-design.md` 的补充约束。若本文与早期
`analysis -> implementation -> verification` fixture 表述冲突，以本文为准；该 fixture
只能用于 deterministic 测试，不能成为生产默认 workflow。

## 2. 核心规则

任何能够由一个具备适当能力的 Codex、Claude Code 或其他 worker 的连续 session
可靠完成的工作，默认都应是一个 execution node。

一个 node 表示一项完整、可验收、可恢复的工作责任，而不是：

- 一个开发阶段；
- 一个角色；
- 一个文件；
- 一次工具调用；
- 一次模型调用；
- 一个传统项目计划步骤。

一个 primary worker node 可以包含共享同一 workspace、同一责任和同一验收边界的：

- inspection；
- local research；
- local planning；
- implementation；
- testing；
- debugging；
- local verification。

例如，实现并验证 OAuth 登录通常应是一个 node，而不是按 `inspect`、`research`、
`design`、`backend`、`frontend`、`test`、`fix` 切分为多个 runtime worker。

### 2.1 Coherent Responsibility Test

一组活动同时满足以下条件时，默认属于同一个 node：

1. 共享同一主要 workspace；
2. 使用同一 capability envelope；
3. 对同一完整 outcome 负责；
4. 共享同一验收边界；
5. inspection、修改、测试和 debug 之间存在连续反馈循环；
6. 不要求独立、无偏的外部 evidence；
7. 不跨越 human authority、credential 或其他安全边界。

活动数量、涉及文件数量、技术领域数量和传统开发阶段数量，都不是拆分 runtime node 的
充分理由。

## 3. Decision Provider 的职责

Decision Provider 是结构升级控制器，不是传统任务拆分器。

它的优化目标是最小化：

- runtime worker 数量；
- workspace/context 的重复获取；
- worker handoff 数量；
- 重叠的 workspace ownership；
- graph coordination 和 decision round 成本；
- 不必要的 materialization、receipt 和恢复边界。

同时必须满足：

- capability 和 human authorization 边界；
- goal evidence 的充分性；
- terminal fact 的可恢复性；
- 必要的独立验证；
- 有真实收益的 durable 并行。

当不确定是否拆分时，默认不拆分。先创建一个足够完整的 primary node，让 worker 的
执行证据暴露实际边界；不能根据抽象阶段名称预先制造 graph。

Decision Provider 只能输出 graph patch proposal。它不能直接创建 task、启动 worker、
改变 readiness、完成 job、授予 capability 或覆盖本地 validator。

## 4. 默认执行图

初始执行图应倾向一个 primary execution node：

```text
Goal Contract
      |
      v
Primary Worker Node
      |
      v
terminal evidence / structural receipt
```

初始 node 的目标应描述完整交付责任，例如：

```text
在当前仓库实现并验证 OAuth 登录，包括必要接口、前端接入、错误处理、
测试和本地验证；不部署生产环境。
```

node contract 应能够表达：目标关联、workspace scope、允许能力、成功 evidence、验证要求、
禁止边界和最终 receipt，而不是列出强制执行步骤。

一个 node 不等于黑盒。worker 应持续写入 heartbeat、progress、artifact、test result、
checkpoint 或 blocker event，供 Kanban lifecycle、recovery 和 observability 使用。这些局部
事件不应自动触发 Decision Provider 重规划。

## 5. 允许创建额外 Runtime Node 的条件

创建多个 durable runtime node 时，至少必须存在下列一种结构性理由。

### 5.1 `independent_verification`

需要不继承实现者假设的独立验证，例如安全审计、兼容性验证或高风险验证。此类 verifier
应具有独立责任与 evidence，不应仅作为 implementation worker 的内部 subagent。

### 5.2 `capability_boundary`

工作需要不同权限、凭证或安全 profile，例如普通代码修改与带凭证的部署验证。上下文复用
不能覆盖 capability policy。

### 5.3 `human_authority_boundary`

工作跨越产品选择、费用、凭证、破坏性操作或其他合法 human gate。

### 5.4 `workspace_isolation`

需要独立 worktree、互斥写范围或不应共享未验证 workspace 状态。

### 5.5 `durable_parallelism`

多个工作输出低耦合、写范围不重叠、集成边界明确，且并行收益超过 context acquisition 和
协调成本。仅仅“理论上可以并行”不是理由。

### 5.6 `context_or_runtime_limit`

单个 worker 的可靠 session、时间、恢复或失败重做边界已被证据证明不足。

### 5.7 `execution_discovered_gap`

worker execution receipt 发现其无法继续覆盖的 gap，例如缺少受限凭证、外部契约不明或
需要独立 verifier。此时由 Decision Provider 根据已入库 evidence 扩展 graph。

以下理由无效：`different_phase`、`different_role`、`cleaner_plan`、`task_is_complex`、
`could_be_parallelized`。

## 6. Worker 内部 Subagent

Worker 不得创建 durable runtime node，也不得直接修改 execution graph。

若 backend 自身支持，worker 可以在 node 内使用 ephemeral internal subagent，例如并行
阅读局部代码、搜索文档或分析测试失败。Kernel 只看到一个 node、一个 capability envelope
和一个最终 accountable parent worker。

internal subagent 必须：

- 不进入 execution graph；
- 不拥有独立 lease、goal completion 或 durable recovery 权限；
- 不得扩大父 worker capability、workspace scope 或 credential access；
- 不得申请 capability 或 human authorization；
- 由父 worker 整合结果并对最终 receipt 负责；
- 不得替代需要独立验证、权限隔离、长期并行或独立审计的 runtime node。

是否支持 internal subagent、并发上限和内部 context compaction 由 backend 决定。当前
Runtime Kernel 不管理 worker 内部 subagent 生命周期，也不能把该能力假定为所有 Codex/CC
lane 的既有事实。

## 7. 结构升级 Receipt

worker receipt 后续应支持与现有 terminal verdict 正交的可选 `structure_request`：

```json
{
  "verdict": "blocked",
  "summary": "本地实现已完成，但当前权限不能验证 staging 契约",
  "structure_request": {
    "required": true,
    "blocking": true,
    "reason_type": "capability_boundary",
    "completed_scope": ["local adapter implemented", "unit tests passed"],
    "discovered_gaps": [
      {
        "gap_key": "verify-staging-contract",
        "description": "需要 staging credential 验证外部契约",
        "evidence_refs": ["artifact:adapter-test-report"]
      }
    ],
    "suggested_nodes": [
      {
        "objective": "使用 staging credential 验证身份提供方契约",
        "requested_capabilities": ["staging_credentials", "network_access"]
      }
    ]
  }
}
```

`structure_request` 不是新的 verdict 或 node state。它可以与 `succeeded` 配合表达当前责任
已完成但整个 goal 仍需要独立 verifier，也可以与 `blocked` 配合表达当前责任无法在现有
权限下继续。

这些字段是 evidence，不是 graph mutation 指令。MVP 只接受 terminal receipt 中的
`structure_request`，不增加 paused worker 或同一 backend session 的中途恢复协议。Runtime
ingest 将其持久化；Decision Provider 只在 reducer 确认仍需结构变更后读取并提出 patch；
validator 仍决定 patch 是否可落库。

## 8. Graph Patch Decomposition Contract

`decomposition` 在 JSON schema 中允许缺省，但满足 graph expansion predicate 时由 validator
条件性强制要求。以下任一情况成立时必须存在：

1. 一个 patch 创建两个或更多非 human execution node；
2. 新 node 将与现有 nonterminal primary node 并行 runnable；
3. patch 插入独立 verifier；
4. patch 将同一 goal gap 分配给多个 durable worker；
5. patch 创建不同 capability 或 credential envelope 的执行责任；
6. patch 拆分已有 node 或建立新的并行写 lane。

没有 `decomposition` 时，一个 patch 最多创建一个新的 runnable worker execution node。
`strategy_update` 也会 materialize 为 durable worker node，因此计入预算。Verifier 可以提前
声明，但必须依赖固定 target，不能与仍在变化的 implementation 并行 runnable。

```json
{
  "policy_version": "1",
  "mode": "multiple_runtime_nodes",
  "justifications": [
    {
      "type": "independent_verification",
      "nodes": ["implementation", "security-verifier"],
      "explanation": "验证不得继承实现者的安全假设",
      "evidence_refs": ["receipt:implementation:attempt-1"]
    }
  ]
}
```

Validator 的可检查职责包括：

- 多 node patch 是否存在允许的理由枚举；
- 理由引用的 node 是否存在于同一 patch 或当前 graph；
- 并行写 node 是否声明重叠 scope；
- verifier 是否具有可追溯的 implementation target；
- 初始 patch 是否超过 primary execution node 预算；
- node 是否仍关联 goal/gap/human reason、outcome 与验证预期。

理由的附加约束：

- `context_or_runtime_limit` 必须引用已有 timeout、checkpoint、失败或 receipt evidence；
- `execution_discovered_gap` 必须引用 worker receipt/event；
- `durable_parallelism` 必须声明 write scope、依赖边界和 integration owner；
- `task_is_complex`、`different_phase`、`different_role`、`cleaner_plan` 和
  `could_be_parallelized` 必须拒绝。

Validator 不应伪装为语义工作量评估器。它不能证明单一 worker 一定能完成工作，只能拒绝
无结构理由、无目标关联或明显违反隔离约束的 graph expansion。

## 9. Decision Provider 调用时机

Decision Provider 应低频处理结构事件，而非 worker 的局部步骤。worker terminal receipt
本身不是 trigger。正确顺序是：

```text
ingest terminal receipt
      -> ledger / evidence
      -> readiness / capability
      -> completion / liveness
      -> structural decision request only when structure remains unknown
```

以下情况不应调用 Decision Provider：goal 已满足；已有 dependent node 自动变为 ready；
已有 graph 已覆盖新 gap；receipt 只更新已知 evidence。

真正的典型触发点：

- job 初始化；
- terminal receipt 经 reducer 后仍产生结构性未知；
- terminal `structure_request` 经 reducer 确认需要 graph expansion；
- capability/human authorization 变化；
- independent verifier 失败；
- anti-stuck 或重复失败；
- goal gap 没有 active coverage；
- 用户目标变更。

正常路径应是：

```text
Decision Provider
      |
      v
Primary Worker Session
      |
      v
local execution loop, events, tests, debugging
      |
      v
terminal or structural receipt
      |
      v
Decision Provider when structure is needed
```

不是每一个 local action 后都执行 `Decision -> Worker -> Decision`。

## 10. 当前实现与后续计划

截至 2026-07-10，Delegation Policy Enforcement MVP 已实现：

- `graph_patch_decision` 和 `validator_recovery_decision` 已升级为 Profile v2，不再鼓励按
  analysis、research、implementation、testing 或 debugging 阶段拆分；
- stable prefix 已把 Decision Provider 定义为结构升级控制器，并声明 primary-node-first、
  合法拆分理由和单 runnable worker node 预算；
- `decomposition` 保持 schema 向后兼容，但在多 execution node、独立 verifier 或与已有
  running primary node 并行扩展时由 validator 条件性强制要求；
- typed node contract 已进入 patch validator，并持久化到 `constraints_json`；
- terminal `structure_request` 已校验、写入 `worker_structure_requested` event，并投影到
  后续 decision delta；
- verifier 必须固定 evidence、materialization、artifact 或 workspace revision；
- `declared_write_scope` 已支持 post-run `changed_files` verification，越界会产生
  `write_scope_violation` 并阻止 goal 被错误满足；
- deterministic eval 已覆盖单 node budget、多 node decomposition、并行 scope overlap、
  verifier target、structure request 和 scope violation；
- 隔离真实 Profile v2 smoke 已证明模型能返回一个带 typed contract 的 coherent primary
  node，并通过 validator dry-run。
- Delegation Initialization 已把 production create/promote 改为 provider-first，修正
  waiting-decision liveness，并将 expansion predicate 扩展到全部 nonterminal node；
- 隔离真实 worker smoke 已证明一个 primary node 可以在单一 materialization attempt 中完成
  inspection、implementation、testing、debugging 和 local verification。
- Phase 4G4 已持久化 Codex backend session，并通过隔离真实 smoke 证明 timeout 后可以在
  新 materialization attempt 中 resume 同一 session，且 ledger/job/consistency 正常完成。

当前仍未完成：

- backend internal subagent policy 下发、继承检查与观测；
- 任意长时间 primary worker node 的多次 checkpoint/resume soak；
- backend sandbox/worktree 对 `declared_write_scope` 的执行前强制隔离。

因此当前可以称为 policy enforcement MVP，不能称为长期 delegation production complete。

### 10.1 Typed Node Contract 与 Write Scope

MVP 的 `create_node.contract` 至少包含 `outcome`、`acceptance_criteria`、
`success_evidence`、`declared_write_scope` 和 `prohibited_actions`。它可以先持久化到
`constraints_json`，无需立即迁移独立表。

`declared_write_scope` 只是可验证声明，不是 sandbox。Validator 检查明显重叠和过宽声明；
worker 完成后必须基于结构化 `changed_files` 做 post-run 检查，并在越界时产生
`write_scope_violation`。在 backend 真正强制路径隔离前，不得声称该字段是安全边界。

### 10.2 Verifier 固定目标

独立 verifier 不能只引用可变的 `target_node_key`。至少还应固定
`target_evidence_ref`、`target_materialization_attempt`、`target_artifact_ref` 或
`target_workspace_revision` 之一。Verifier 必须使用新的 backend session/context，不能继承
implementation worker 的隐藏推理上下文。

### 10.3 真实 Profile v2 验证

2026-07-10 基于实现提交 `a95e128` 和 smoke hardening 提交 `a344b15`，在一次性
`HERMES_HOME` 中使用当前 `.codex` 模型源完成 L1 validate-without-apply：

- Profile 为 `graph_patch_decision` v2；
- 模型输出 parsed，最终验证运行 `retry_count=0`；
- patch 只有一个 `create_node` operation；
- `execution_node_count=1`，`immediate_execution_node_count=1`；
- typed contract 覆盖 `1/1`；
- 没有 `decomposition`，符合单 primary node 默认路径；
- validator 为 `accepted` / `would_apply=true`；
- smoke 未 apply，graph revision、graph patch 和 kernel decision 均保持不变；
- credential scan 通过，最终调用前后 `.codex` 配置与认证文件哈希保持一致。

该结果证明真实 provider 能遵守当前 delegation contract，不证明真实 worker 已完成 OAuth
实现，也不证明长期 primary worker session 的稳定性。完整脱敏事实见
`docs/kanban-runtime-kernel-real-integration-validation.md`。
