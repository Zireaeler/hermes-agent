# Hermes Kanban Runtime Kernel Phase 4G15

# Evidence-Driven Live Orchestra Improvement

## 1. 背景

Phase 4G11 已建立 DB-backed coordination checkpoint、global execution snapshot、durable
directive mailbox 和 same-session resume。Phase 4G12/4G13 允许 evidence-driven graph
mutation，并移除没有真实结构事件的强制 checkpoint。Phase 4G14 又把 isolated attempt patch
捕获与 receipt 语义验收解耦，使 child 已完成的工程成果不会因 handoff metadata 错误而丢失。

这些阶段解决了：

- worker 如何在安全边界提交跨节点发现；
- Decision Provider 如何基于全局 DB facts 提议 control patch；
- directive 如何持久化、交付和 ACK；
- isolated contribution 如何被可靠保存和集成。

但 Phase 4G14 Natural Medium 仍然主要执行：

```text
Primary early assessment
    -> 一次 graph expansion
    -> isolated children 各自执行到 terminal
    -> Primary 最终集成
```

这证明了 durable handoff，却没有证明一个 node 的新事实能在另一个 node terminal 前改变其执行。
Phase 4G11 对正在运行的 target 只将 directive 保持为 `queued`，因为已经开始的 materialization
不能被静默改写。这个约束保证审计正确性，但意味着 stale work 仍可能继续到 terminal。

真实 Medium 还说明：每次测试虽然保存大量 DB、session、worker event 和 evaluator evidence，
但“过程分析 -> 改进候选 -> replay 验证 -> 稳定策略”的闭环尚未成为强制协议。人工报告可以得出
结论，却不能保证下一次 run 会检索、验证或处理这些结论。

Phase 4G15 同时补齐两个缺口：

1. **Live Structural Control**：validated directive 可以在安全条件下通过 Codex app-server
   `turn/steer` 注入正在运行的同一 worker turn；
2. **Orchestration Learning Loop**：每次受管 real run 必须生成、登记并追踪一个可复现 learning
   bundle，候选只有通过 replay/对照门禁后才能 promotion。

Phase 4G15 不追求“每次 run 都自动修改系统”。健康 run 可能没有值得修改的内容。它保证的是：

```text
每次受管 run
    -> 必须完成过程分析
    -> 每个发现必须有吸收状态
    -> 值得处理的问题不能只停留在聊天或手写总结
    -> 稳定策略变更必须有可复现对照证据
```

---

## 2. 目标

Phase 4G15 必须实现：

1. worker 在 cooperative safe point 提交带 evidence 的 structural checkpoint；
2. Runtime 将 checkpoint 持久化为全局事实并计算 affected nodes；
3. 本地 reducer 能确定的 context routing 不调用 Decision Provider；
4. topology、ownership、scope 或 responsibility 变化不明确时才请求 Decision Provider；
5. live-safe directive 可以在 target terminal 前进入其 active Codex turn；
6. worker 在 canonical checkpoint 或 receipt 中 ACK directive；
7. steer 不可用、不安全或失败时保持 durable queued fallback；
8. 每次受管 real run 自动生成机器可读 learning bundle 和中文过程报告；
9. bundle 必须被 stable learning registry 幂等吸收，archive/cleanup 才能完成；
10. 改进候选只有经过 baseline/treatment replay、质量非回归和明确 promotion 后才能影响稳定
    delegation/coordination policy；
11. 使用轻量受控任务证明 sibling terminal 前发生责任变化，并测量避免的 stale work；
12. 对照 coherent single worker，最终质量不得更差。

---

## 3. 非目标

Phase 4G15 不实现：

- worker peer-to-peer 自由聊天；
- worker 直接创建、删除或修改 durable graph node；
- 将普通 progress、tool call 或 heartbeat 送入 Decision Provider；
- 对所有 running turn 强制定时中断；
- 自动扩大 capability、write scope 或 credential envelope；
- 从一次 benchmark 自动修改 provider profile；
- 将 evaluator hidden contract 设为普通开发任务的默认门禁；
- embedding、vector database 或 autonomous reinforcement learning；
- Dashboard。

Phase 4G15 不承诺每次 run 都产生 policy 变更。它要求每次 run 都产生完整分析和明确的吸收结果：

```text
candidate_created
no_action_required
duplicate_of_existing_candidate
covered_by_existing_regression
infrastructure_invalid
```

不得使用空报告或泛化模型总结冒充吸收。

---

## 4. 核心原则

### 4.1 结构事件，而不是进度事件

只有会改变其他 durable responsibility 的事实才进入 live orchestra：

```text
shared_contract_changed
assumption_invalidated
scope_conflict
partial_artifact_ready
capability_boundary
integration_rejected
responsibility_gap_discovered
```

普通文件读取、代码修改、局部测试结果、heartbeat、debug 尝试或没有 cross-node effect 的阶段切换
不触发全局 Decision。

### 4.2 Reducer-first，Provider 只处理结构未知

Runtime 收到 checkpoint 后先执行本地路由：

```text
validated checkpoint
    -> persist DB fact
    -> resolve explicit affected_node_keys
    -> compare dependency / scope / contract revision
    -> deterministic context directive when topology is unchanged
    -> Decision Provider only for unresolved topology or ownership
```

例如 A 发布一个已存在 artifact 的新 hash，而 B 的 contract 已明确要求消费该 artifact，本地 reducer
可以直接为 B 排队 `continue` directive。若新事实要求拆分 B、替换 owner、扩大 write scope 或创建
credentialed node，才进入 Decision Provider。

### 4.3 Live steer 不改变权威边界

`turn/steer` 只是 directive transport，不是事实源。有效控制链仍然是：

```text
checkpoint event
    -> validated directive row
    -> live delivery attempt
    -> Codex app-server accepts expected active turn
    -> canonical worker ACK
```

只有 canonical ACK 才证明 worker 消费了 directive。成功发送 JSON-RPC、收到 app-server response、
写入 prompt 或记录 worker log 都不能替代 ACK。

### 4.4 不用强杀模拟协作

live steer 必须优先使用 Codex 原生 same-turn `turn/steer`。以下情况退回 durable queue：

- worker lane 仍使用 `codex exec`；
- app-server 不支持当前 turn kind；
- expected turn ID 已变化；
- transport 不可达；
- directive 需要 capability 或 write scope 扩大；
- target 已进入 terminal transition；
- steer 重试预算耗尽。

默认不得通过 `SIGKILL` 或 lease takeover 强迫 worker 接收普通协调信息。Hard interruption 只属于
已有 crash/recovery policy，不是 live coordination 的正常路径。

### 4.5 经验吸收不等于自动学习

单个 run 的经验可能是偶然环境差异、模型波动或 benchmark 特例。经验必须经过：

```text
run fact
    -> deterministic finding
    -> improvement candidate
    -> replay evaluation
    -> promoted policy lesson
```

run fact 和 finding 可以自动生成；candidate 可以自动登记；promotion 不能自动发生。

---

## 5. Live Worker Transport

### 5.1 Transport capability

worker lane 增加显式 transport：

```text
codex_exec
codex_app_server
```

`codex_exec` 保持当前兼容行为，不支持 live steer。`codex_app_server` 使用独立、Hermes-owned
app-server process 和 private Unix socket，运行 thread/turn 并保留原生 session。

Runtime 必须记录：

```text
transport_kind
app_server_endpoint_ref
thread_id
active_turn_id
transport_process_identity
transport_started_at
last_transport_heartbeat_at
steer_capability
```

endpoint 只能位于该 worker 的 private runtime directory，不进入 worker prompt，不允许其他 job
使用。API key/base URL 继续按 validation retention policy 脱敏。

### 5.2 App-server lifecycle

由外部 worker wrapper 管理 app-server：

```text
Kanban worker task
    -> launch codex app-server on private Unix socket
    -> initialize
    -> thread/start or thread/resume
    -> turn/start
    -> stream notifications into normal worker events
    -> poll validated live directives for current materialization
    -> turn/steer when eligible
    -> wait turn/completed
    -> normal receipt ingest
```

app-server process、socket、thread 和 turn identity 都必须进入 worker event stream。Wrapper crash 后
不得根据内存猜测 turn identity；恢复路径从 DB/task events 和 app-server/thread evidence 重建，无法
重建时退回现有 same-session resume。

### 5.3 Live-safe action

第一版允许 live steer 的 action：

```text
continue
narrow_scope
request_partial_contribution
stop_obsolete_work
```

约束：

- capability envelope 不变；
- write scope 只能保持或缩小；
- goal/gap linkage 不变；
- verifier fixed target 不变；
- 不改变 workspace ownership；
- 指令必须引用 source checkpoint event；
- `expected_contract_revision` 和 `expected_turn_id` 都必须匹配。

需要扩大 scope、改变 owner、创建 node 或调整依赖的 action 仍使用 durable graph patch，并在下一安全
边界生效。

### 5.4 Delivery state

保留 `runtime_node_directives` 作为 canonical mailbox，并增加 delivery attempt：

```text
runtime_live_directive_deliveries
    id
    directive_id
    materialization_id
    task_run_id
    transport_kind
    expected_thread_id
    expected_turn_id
    status
    request_ref
    response_ref
    error_code
    created_at
    accepted_at
```

状态：

```text
pending
    -> accepted
    -> acknowledged

pending
    -> stale_turn
    -> not_steerable
    -> transport_failed
    -> queued_fallback
```

一个 directive 只能有一个 accepted live delivery。重复 poll、supervisor restart 或 wrapper reconnect
不得产生重复 steer。

---

## 6. Live Orchestra 执行例

目标涉及 parser、renderer 和 CLI 三个责任域：

```text
Primary
    -> parser child
    -> renderer child
    -> Primary owns CLI integration
```

Parser 在实现中发现 token model 必须从字符串变为 `{kind, value}`：

```text
parser safe-point checkpoint
    finding: shared_contract_changed(token-model-v2)
    affected: renderer, primary
```

此时 renderer 的 active turn 仍在使用 v1：

```text
Runtime persists checkpoint
    -> reducer resolves renderer as affected
    -> issue narrow_scope/continue directive
    -> app-server turn/steer(renderer active turn)
    -> renderer adapts before terminal
    -> renderer receipt ACKs directive
```

若 renderer 无法接受 steer，directive 保持 queued，Primary 最终必须能看见“stale work 未避免”，
learning bundle 将其记录为实际成本，不能把 queued 当作成功协作。

---

## 7. Orchestration Learning Bundle

### 7.1 强制输出

每个启用 Phase 4G15 validation policy 的 real run 在 archive 前必须生成：

```text
reports/orchestration-learning.json
reports/orchestration-learning.md
```

JSON schema：`hermes_runtime_orchestration_learning_bundle_v1`。顶层至少包含：

```json
{
  "schema": "hermes_runtime_orchestration_learning_bundle_v1",
  "run_identity": {},
  "source_evidence": {},
  "timeline": [],
  "graph_evolution": {},
  "coordination": {},
  "handoff": {},
  "cost": {},
  "quality": {},
  "findings": [],
  "improvement_candidates": [],
  "regression_scenarios": [],
  "absorption": {}
}
```

### 7.2 Authoritative inputs

Analyzer 只从以下输入建立事实：

- Runtime/Kanban DB snapshot；
- execution events、graph patches、directives、receipts 和 ledger；
- worker/provider structured events；
- contribution、attempt patch 和 candidate lineage；
- run report、quality/evaluator result；
- manifest、protocol commit 和 environment fingerprint；
- 明确指定的 baseline bundle。

不得从聊天记忆、手工结论或没有 evidence ref 的 LLM 总结建立 run fact。

### 7.3 必须分析的指标

每个 bundle 至少计算：

```text
node_count_by_role
graph_patch_count
useful_decision_count / rejected_or_noop_decision_count
structural_checkpoint_count
live_directive_attempted / accepted / acknowledged / fallback
checkpoint_to_directive_latency
directive_to_ack_latency
target_terminal_after_directive
stale_work_avoided_count
stale_work_not_avoided_count
contribution_capture / promotion / integration ratio
worker_reexecution_due_to_handoff
context_reacquisition_count
worker wall time / critical path / orchestration wait
input / cached input / output / uncached input
quality outcome and baseline delta
```

没有相应 evidence 时字段必须为 `unknown` 并记录 observability gap，不能填 `0`。

### 7.4 中文时间线

Markdown 报告必须按实际时间顺序解释：

1. 为什么创建或不创建 node；
2. 每个 worker 实际负责什么；
3. 哪个事实触发 graph/directive 变化；
4. target 是否在 terminal 前消费；
5. 哪些工作被保留、废弃或重做；
6. 最终质量和 coherent single baseline；
7. 本 run 产生了什么候选，或为什么无需改动。

报告不能只罗列 event type 和 token 数。

---

## 8. Stable Learning Registry

### 8.1 位置与边界

默认位置：

```text
<validation-artifact-root>/orchestration-learning/registry.sqlite3
```

Registry 是验证经验的 authoritative index，但不是 Runtime job correctness 的事实源。它不得影响
readiness、goal completion、capability authorization、worker recovery、graph validator 或 evaluator
verdict。它只追踪 run 分析、候选、replay 和 promotion。

### 8.2 Run absorption

每个 bundle 以 `phase + instance_id + run_id + bundle_sha256` 幂等登记。状态：

```text
generated -> validated -> absorbed
generated/validated -> invalid
```

`absorbed` 必须满足：

- bundle schema 有效；
- source DB/report/manifest refs 存在；
- timeline 至少覆盖 graph、worker 和 terminal/stop；
- 每个 finding 有 evidence refs；
- 每个 finding 有 absorption disposition；
- candidate 已登记，或给出 `no_action_required` 等明确 disposition；
- bundle hash 写入 registry。

### 8.3 Improvement candidate

Candidate 至少包含 category、scope、symptom、root cause、evidence refs、proposed change、expected
effect、regression scenario key 和 status。Candidate 不进入 Runtime Memory retrieval，也不改变
provider profile。

允许状态：

```text
candidate
replay_ready
validated
promoted
rejected
superseded
```

### 8.4 Replay 与 promotion

Promotion 至少需要同一冻结场景的：

```text
baseline arm
treatment arm
same goal / repository / model class / capability envelope
quality non-regression
targeted orchestration metric improvement
manifest-verified evidence
operator or explicit release approval
```

只有 `promoted` lesson 才可以被人工写入 stable delegation/coordination guidance，或经 Phase 4G0
memory promotion 流程成为 non-authoritative accepted memory。Registry 不直接改 Markdown、prompt、
validator 或代码。

---

## 9. Archive 与 Cleanup Gate

启用 `orchestration_learning_required=true` 的 run，archive 前必须：

1. 生成 JSON/Markdown bundle；
2. deterministic schema validation 通过；
3. bundle 被 stable registry 幂等吸收；
4. registry receipt 写回 bundle 的 `absorption`；
5. bundle hash 和 registry receipt 进入 archive manifest/catalog；
6. archive manifest 验证通过；
7. 才允许 cleanup 可重建 workspace/toolchain/cache。

若 analyzer 或 registry 不可用，run evidence 保留，archive/cleanup 停止，并报告
`learning_gate_blocked`。不得因为 learning 分析失败而删除原始 session、DB、events 或 artifacts。

旧 Phase 4G8-4G14 run 不追溯强制该 gate。它们可以离线 import 为 baseline，但必须标记
`legacy_import`。

---

## 10. Deterministic Findings

MVP analyzer 至少识别：

### 10.1 Handoff loss

isolated attempt patch 已捕获、contribution 未 promotion/integration，随后另一 worker 重做重叠文件时，
生成 `handoff_reexecution` finding。

### 10.2 Queued-too-late directive

directive 在 target running 时创建，而 target 在 ACK 前 terminal，生成
`live_coordination_missed` finding。

### 10.3 Effective live directive

directive 在 target running 时创建、live delivery accepted、target terminal 前 ACK 且不存在 stale-path
marker，生成 positive `live_coordination_effective` finding。

### 10.4 Unnecessary orchestration

multi-worker treatment 质量不高于 single baseline，成本更高且没有 handoff/recovery/safety 收益时，
生成 `orchestration_no_net_value` candidate，供拆分策略 replay，不自动禁用 multi-worker。

### 10.5 Local-reducer opportunity

Decision Provider patch 只执行 deterministic readiness/routing transition 时，生成
`provider_called_for_local_transition` candidate。

---

## 11. 受控验证

第一版不使用 hard benchmark。构造一个真实 git repository 和三个责任：

```text
producer child
consumer child
Primary integration owner
```

场景：

1. 两个 child 在独立 worktree 并行执行；
2. consumer 先按 contract v1 开始任务，并写入可观测 stale marker；
3. producer 从 repository evidence 发现 contract v2，在 safe point 提交 checkpoint；
4. Runtime 在 consumer terminal 前发布 live-safe directive；
5. consumer 的同一 app-server thread/turn 接收 steer，删除或避免 stale marker，按 v2 完成；
6. receipt ACK directive；
7. Primary 集成两个 frozen contribution；
8. 完整测试通过；
9. analyzer 生成 bundle；
10. registry 吸收 run；
11. archive gate 通过后清理临时 workspace。

同一场景运行两个 arm：

```text
baseline: durable queue only
treatment: live steer enabled
```

Baseline 不需要故意让最终质量失败。它可以由 Primary 最终修正 stale work，但必须记录额外重做或
更长 critical path。Treatment 必须在 consumer terminal 前消费 directive，并减少该重做。

---

## 12. Acceptance Criteria

Phase 4G15 完成必须满足：

- [ ] `codex_app_server` worker transport 有明确配置和审计身份；
- [ ] active thread/turn identity 可从持久事件恢复；
- [ ] live-safe directive 使用 expected thread/turn precondition；
- [ ] stale turn、not steerable 和 transport failure 都退回 durable queue；
- [ ] source checkpoint 到 target ACK 有完整 DB lineage；
- [ ] controlled treatment 在 target terminal 前 ACK directive；
- [ ] treatment 比 baseline 少至少一次可证明的 stale work/reimplementation；
- [ ] treatment 最终质量不低于 coherent baseline；
- [ ] 每个受管 run 都生成 JSON bundle 和中文过程报告；
- [ ] 每个 finding 都有 evidence refs 和 absorption disposition；
- [ ] registry import 幂等；
- [ ] 未经 replay/approval 的 candidate 不能 promotion；
- [ ] learning bundle 缺失或未 absorbed 时 archive/cleanup 被拒绝；
- [ ] 原始 DB、session、worker events 和 artifact lineage 保留；
- [ ] 不调用 hard benchmark；
- [ ] 受影响离线测试通过。

---

## 13. 实施顺序

1. 固化 live-safe action、delivery state、learning bundle 和 binding constraints；
2. 实现最小 app-server JSON-RPC transport 和 fake protocol tests；
3. 增加 running target live routing、delivery persistence、ACK、fallback 和 consistency；
4. 实现 analyzer、中文 renderer、registry、replay/promotion 和 archive gate；
5. 运行 controlled baseline/treatment real process case；
6. 保存中文报告和 stable archive，运行完整受影响回归。

---

## 14. 最终边界

Phase 4G15 的核心不是让 Runtime 对每个 worker 动作重新规划，也不是让多个 worker 模拟聊天群。
正确模型是：

```text
Worker
    负责 node 内连续执行
    在低频、高影响 safe point 发布事实

Runtime
    持久化全局状态
    本地路由确定性影响
    对结构未知请求 Decision Provider
    通过 live steer 或 durable queue 交付控制

Learning Loop
    分析每次真实 run
    保留可复现证据
    登记候选
    只在对照验证后 promotion
```

目标是：

> Runtime 在真实新证据出现后，及时改变仍在执行的责任，减少错误工作或避免重做；每次验证中的
> orchestra 行为都被证据化分析并进入可追踪的改进生命周期，且最终质量不低于 coherent single
> worker。
