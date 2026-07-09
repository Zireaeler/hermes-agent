# Hermes Kanban Runtime Kernel Phase 4E：Worker Recovery 和 Runtime Consistency

Phase 4E 的目标是把 Phase 4 MVP 收成可长期运行的 production baseline。

当前 runtime kernel 已经具备 goal contract、progress ledger、execution graph、
decision session、decision session compaction、真实 decision provider、真实
compaction provider 调用边界、observability API 和 DB-backed supervisor lease。
下一步不应继续扩展智能能力，也不应优先做 dashboard UI，而应先处理长期任务最
容易破坏事实源一致性的部分：worker materialization、Kanban task/run、node
state、receipt、ledger 和 event log 之间的不一致。

Phase 4E 不是新一轮 planner 或 multi-agent phase。它仍然遵守 runtime kernel
边界：DB 是事实源，worker 是局部执行单元，LLM 只能提出 graph patch proposal，
readiness/completion/liveness/recovery 都由本地 reducer 和 policy 控制。

## 1. 为什么 Phase 4E 是下一步

Phase 4 MVP 已经能决策、压缩、supervise 和暴露观测面，但真实长任务首先会撞到
worker 状态不一致，而不是 LLM 不够聪明。

典型问题：

- execution node 仍是 `running`，但 Kanban task 已经 timeout；
- task 已经 `done`，但 receipt 缺失或不符合 schema；
- worker run crash 了，但 node 没有失败；
- node 是 `running`，但 task/run 已经消失；
- 同一个 ready node 被重复 materialize；
- worker 返回 `uncertain`，但没有后续 recovery 路径；
- supervisor 重启后不知道该等待、retry、rerun、还是创建 debug node；
- retry 覆盖了 terminal node fact；
- task failed 和业务验证 failed 被混成一类。

这些问题如果不先收口，后续真实 compaction smoke、long-run soak 和 dashboard UI
都会变成诊断混乱：无法判断卡住原因是模型、compaction、worker、supervisor 还是
DB 状态脏。

## 2. 目标

Phase 4E 要实现一套本地、可审计、可重放的 worker recovery 和 runtime consistency
机制。

目标包括：

- 识别 execution node、node_materializations、Kanban task/run、receipt 和 ledger
  之间的不一致；
- 把不一致规范化成结构性 execution_events；
- 在 supervisor tick 前先 reconcile worker/materialization 状态；
- 保留 materialization attempt history，不覆盖旧 run 事实；
- 区分 infra failure、receipt failure、business failure 和 verifier failure；
- 定义 retry/rerun policy；
- 防止 terminal node fact 被静默改写；
- 提供 event replay / consistency checker；
- 为后续 long-run soak 提供可信的 runtime 状态基础。

## 3. 非目标

Phase 4E 不接入新的 LLM 决策能力。

Phase 4E 不把 worker recovery 交给 decision provider。模型可以在 recovery 之后基于
gap 提议 debug/fix/supersede/strategy_update node，但不能自行判断 Kanban run 是否
stale、task 是否 missing、terminal fact 是否可覆盖。

Phase 4E 不实现 dashboard 前端 UI。它只保证 observability API 能看到 recovery
状态、legal waiting reason 和 consistency warnings。

Phase 4E 不实现完整 security/capability policy。Capability policy 是 Phase 4F 的
候选后续项，但 Phase 4E 需要为它保留 hook。

## 4. 核心设计原则

### 4.1 Recovery 是本地 reducer

Worker recovery 必须是本地 deterministic reducer，不依赖 LLM。

输入：

- execution_nodes；
- node_materializations；
- Kanban tasks；
- task_runs；
- task_events；
- task_progress_snapshot；
- worker receipt / evidence；
- runtime job state；
- current time / timeout policy。

输出：

- execution_events；
- node_materializations 状态更新；
- execution_nodes 状态更新；
- optional progress ledger updates；
- optional goal gap updates；
- legal waiting reason / liveness state。

### 4.2 Materialization attempt 是事实，不是临时映射

`execution_nodes.latest_task_id/latest_run_id` 只是快照，不能作为唯一事实。

真实 worker lifecycle 必须以 `node_materializations` attempt history 为准：

- 每次 materialize 都有独立 attempt；
- attempt 绑定 task_id、run_id、lane、status、created_at、completed_at；
- retry/rerun 创建新 attempt；
- 旧 attempt 不被覆盖；
- terminal attempt 保留 terminal_event_id。

### 4.3 Terminal node fact 不可静默改写

如果 node 已经 terminal：

- `succeeded` 不得被 retry 覆盖成 failed；
- `failed` 不得被新 receipt 静默改成 succeeded；
- `blocked` 不得被 worker receipt 隐式解除；
- verifier 失败不能改写 implementation node 的 succeeded fact；
- 后续变化必须通过 verifier node、debug node、supersede relation、new attempt 或
  explicit recovery event 表达。

### 4.4 Recovery event 是结构事件

Recovery 不是日志字符串。每个 recovery case 都应产生 kernel 关心的结构事件，供
dashboard、soak test、decision delta 和 replay checker 使用。

## 5. Recovery Event 分类

建议新增或正式使用以下 event_type：

```text
materialization_lost
worker_run_stale
worker_run_timeout
worker_run_crashed
receipt_missing
receipt_invalid
receipt_recovery_requested
node_recovery_retry_scheduled
node_recovery_rerun_scheduled
node_recovery_not_retryable
materialization_reconciled
terminal_fact_preserved
task_node_state_mismatch
ledger_reference_missing
checkpoint_reference_missing
consistency_violation
consistency_check_passed
legal_waiting_reason_updated
```

事件要求：

- 必须包含 job_id；
- 若能定位 node，必须包含 node_id/node_key；
- 若能定位 materialization，必须包含 materialization_id/attempt；
- 若能定位 Kanban task/run，必须包含 task_id/run_id；
- 必须包含 recovery_reason；
- 必须包含是否 retryable；
- 必须包含 policy decision。

## 6. Reconcile Reducer

建议新增：

```python
reconcile_runtime_materializations(conn, job_id, *, board=None, now=None, policy=None) -> dict
```

职责：

1. 读取 job 的 running / ready / waiting worker nodes；
2. 读取每个 node 的 latest materialization 和 attempt history；
3. 对比 Kanban task/run/task_progress_snapshot；
4. 识别 mismatch；
5. 写入 execution_events；
6. 更新 materialization status；
7. 必要时更新 node state；
8. 返回 structured summary。

Supervisor tick 顺序应调整为：

```text
acquire advance lease
  |
  v
reconcile materializations
  |
  v
ingest terminal evidence
  |
  v
update progress ledger / goal gaps
  |
  v
update readiness / materialize ready nodes
  |
  v
decision / compaction if needed
  |
  v
liveness / legal waiting reason
  |
  v
release lease
```

Decision provider 不应该在 reconcile 之前看到脏的 worker 状态。

## 7. 不一致状态分类

### 7.1 Node running 但 task missing

条件：

```text
execution_nodes.state = running
latest_task_id is null OR task row missing
```

处理：

- 记录 `materialization_lost`；
- 如果存在 latest materialization，将其标记为 `lost`；
- 根据 schema 支持情况，将 node 标记为 `failed` 或 `waiting_recovery`；
- 如果 infra retry policy 允许，调度一次 retry materialization；
- 不先调用 LLM。

### 7.2 Node running 但 run stale / heartbeat expired

条件：

```text
node running
task/run exists
run heartbeat or claim expired beyond policy
```

处理：

- 记录 `worker_run_stale` 或 `worker_run_timeout`；
- 将 materialization status 更新为 `stale` / `timed_out`；
- 如果 infra retry 次数低于阈值，自动 retry 一次；
- 否则用 infra failure reason 将 node 标记为 failed。

### 7.3 Task terminal 但 receipt missing

条件：

```text
task.status in done/blocked/failed
receipt/evidence missing or not JSON object
```

处理：

- 记录 `receipt_missing` 或 `receipt_invalid`；
- 不标记任何 goal item satisfied；
- 如果 task result text 可以包装成最小 failed/uncertain receipt，则按 `node_uncertain` ingest；
- 否则调度一次 receipt recovery，或用 receipt failure 将 node 标记为 failed。

### 7.4 Task failed 但 node 仍 running

条件：

```text
task terminal failure
node.state = running
```

处理：

- 通过 ingest/reconcile 规范化为 `node_failed`；
- 更新 materialization terminal status；
- 记录 `task_node_state_mismatch` 和 `materialization_reconciled`。

### 7.5 Implementation succeeded 后 verifier failed

条件：

```text
implementation node succeeded
verifier node failed
```

处理：

- 保留 implementation node succeeded fact；
- 将 verifier 标记为 failed；
- 将 ledger/gap 更新为 failed verification；
- 创建或暴露 gap，供后续 debug/fix/supersede decision 使用；
- 不修改 implementation terminal fact。

### 7.6 Infra failure 后需要 retry

条件：

```text
infra failure and retry_count < retry_limit
```

处理：

- 创建新的 node_materializations attempt；
- 只有在新 materialization 创建后才更新 latest_task_id/latest_run_id 快照；
- 保留旧 attempt terminal status；
- 记录 `node_recovery_retry_scheduled`。

### 7.7 Business failure

条件：

```text
worker receipt says failed because task approach failed
verifier failed because artifact behavior is wrong
```

处理：

- 默认不自动 retry 同一个 node；
- 将 node 标记为 failed；
- 打开 goal gap；
- decision provider 后续可以基于 gap 创建 debug/fix/supersede/strategy_update node。

## 8. Retry / Rerun Policy

默认本地 policy：

```json
{
  "infra_retry_limit": 1,
  "receipt_recovery_limit": 1,
  "business_failure_auto_retry": false,
  "uncertain_auto_retry": false,
  "retryable_failure_types": [
    "worker_run_timeout",
    "worker_run_crashed",
    "materialization_lost",
    "receipt_missing"
  ],
  "non_retryable_failure_types": [
    "business_failed",
    "verification_failed",
    "policy_blocked",
    "missing_secret",
    "external_permission"
  ]
}
```

Policy 必须支持按 job 配置，但 Phase 4E MVP 应先提供 deterministic defaults。

## 9. Event Replay / Consistency Checker

建议新增：

```python
check_runtime_consistency(conn, job_id) -> dict
```

第一版不需要完整 replay 所有 projection，但必须检查关键不变量：

- node terminal event 与 node.state 一致；
- materialization terminal state 与 latest task/run terminal event 一致；
- progress_ledger 引用的 node/artifact/event 存在；
- checkpoint 引用的 event/decision/patch/goal/artifact 存在；
- active segment 和 latest checkpoint 不冲突；
- compacted segment 不再进入 active provider input；
- job done 时 required goal items 都有 sufficient evidence；
- job active 且无 ready/running/human/pending decision 时，必须有 liveness event；
- retry/rerun 没有覆盖 terminal node fact；
- latest_task_id/latest_run_id 指向 latest non-terminal or latest attempt snapshot。

CLI 建议：

```bash
hermes kanban runtime consistency <job_id> --json
hermes kanban runtime reconcile <job_id> --json
```

API 后续：

```text
GET /api/runtime/jobs/{id}/consistency
POST /api/runtime/jobs/{id}/reconcile
```

第一版 API 可以只做 GET，POST reconcile 先通过 CLI/supervisor tick 暴露。

## 10. Observability 变更

Runtime snapshot 应增加一等字段：

```json
{
  "legal_waiting_reason": "waiting_worker",
  "recovery": {
    "open_recovery_events": [],
    "retryable_count": 0,
    "non_retryable_count": 0,
    "latest_reconcile_at": 0
  },
  "consistency": {
    "status": "passed",
    "violation_count": 0,
    "warnings": []
  }
}
```

Legal waiting reason 不应只藏在 event log 里。

## 11. 实现计划

### Step 1：文档和 schema 兼容

- 明确 recovery events 和 policies；
- 尽量复用现有 `node_materializations` 表；
- 只有必要时才增加 schema column，Phase 4E MVP 优先使用 metadata_json；
- 确保所有新增状态都能从 DB 恢复。

### Step 2：实现 reconcile reducer

- 实现 `reconcile_runtime_materializations()`；
- 为每类 mismatch case 增加 deterministic tests；
- 确保重复 reconcile 幂等。

### Step 3：实现 retry/rerun policy

- 实现默认 retry policy；
- retry 时创建新的 materialization attempt；
- 保留旧 attempt terminal facts；
- 禁止 business failure 自动原地 retry。

### Step 4：接入 supervisor

- 在 `advance_runtime_job()` 或 supervisor tick 中先 reconcile，再 ingest；
- 在 tick result 中记录 reconcile summary；
- 确保 reconcile 出错时 lease 仍会释放。

### Step 5：实现 consistency checker

- 实现 `check_runtime_consistency()`；
- 增加 CLI `runtime consistency`；
- 为 checkpoint/ledger/node/materialization references 增加 focused tests。

### Step 6：扩展 observability API

- 将 recovery/consistency/legal_waiting_reason 加入 `runtime_observability_snapshot()`；
- 扩展 `runtime inspect`；
- 增加 focused API shape tests。

### Step 7：建立 synthetic long-run 基础测试

- 增加一个 deterministic synthetic test，运行多轮 reconcile / advance / compaction cycle；
- 覆盖 stale worker、retry、validator rejection、compaction fallback 和 liveness recovery。

## 12. Tests

必须新增的离线测试：

- node running 但 task missing 时记录 `materialization_lost`；
- node running 但 run stale 时记录 `worker_run_stale`；
- task terminal 但 receipt missing 时记录 `receipt_missing`；
- task failed 但 node running 时通过 reconcile/ingest 变成 node_failed；
- verifier failed 不改写 implementation succeeded；
- infra timeout 自动 retry 一次，并创建新的 materialization attempt；
- business failure 不自动 retry 同一个 node；
- repeated reconcile 幂等；
- retry 不覆盖旧 attempt terminal status；
- consistency checker 拒绝缺失 ledger node ref；
- consistency checker 拒绝 checkpoint refs 指向缺失 node/event/patch；
- job done consistency 要求 required goal evidence；
- active incomplete job 无 runnable path 时必须存在 liveness event；
- supervisor tick 在 decision 前调用 reconcile；
- runtime inspect 暴露 legal_waiting_reason/recovery/consistency。

默认测试必须保持离线和 deterministic。真实 worker/LLM smoke tests 不能成为
Phase 4E 单测覆盖的前提。

## 13. 完成定义

Phase 4E 完成时必须满足：

- runtime 能在不调用 LLM 的情况下 reconcile 常见 worker/materialization mismatches；
- retry/rerun 会创建 attempt history，并保留 terminal facts；
- supervisor tick 从已 reconcile 的 worker 状态开始；
- consistency checker 能发现关键 DB/reference violations；
- observability 暴露 legal waiting reason 和 recovery status；
- synthetic long-run test 能包含 worker stale/retry/reconcile，且不会 silent idle；
- 所有默认测试保持离线。

Phase 4E 之后建议进入：

```text
Phase 4F Runtime Capability / Security Policy
Phase 4G Synthetic Long-Run Soak and Real Compaction Smoke
Phase 4H Dashboard Runtime UI
```
