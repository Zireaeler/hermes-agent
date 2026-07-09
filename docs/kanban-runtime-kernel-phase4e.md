# Hermes Kanban Runtime Kernel Phase 4E: Worker Recovery and Runtime Consistency

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

## 5. Recovery Event Taxonomy

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

## 7. Mismatch Cases

### 7.1 Node running but task missing

Condition:

```text
execution_nodes.state = running
latest_task_id is null OR task row missing
```

Action:

- record `materialization_lost`;
- mark latest materialization `lost` if present;
- mark node `failed` or `waiting_recovery` depending on schema support;
- if infra retry policy allows, schedule retry materialization;
- do not call LLM first.

### 7.2 Node running but run stale / heartbeat expired

Condition:

```text
node running
task/run exists
run heartbeat or claim expired beyond policy
```

Action:

- record `worker_run_stale` or `worker_run_timeout`;
- update materialization status to `stale` / `timed_out`;
- if infra retry count below threshold, retry once;
- otherwise mark node failed with infra failure reason.

### 7.3 Task terminal but receipt missing

Condition:

```text
task.status in done/blocked/failed
receipt/evidence missing or not JSON object
```

Action:

- record `receipt_missing` or `receipt_invalid`;
- do not mark goal item satisfied;
- if task result text can be wrapped into minimal failed/uncertain receipt, ingest as
  `node_uncertain`;
- otherwise schedule receipt recovery once or mark node failed with receipt failure.

### 7.4 Task failed but node still running

Condition:

```text
task terminal failure
node.state = running
```

Action:

- normalize through ingest/reconcile into `node_failed`;
- update materialization terminal status;
- record `task_node_state_mismatch` and `materialization_reconciled`.

### 7.5 Verifier failed after implementation succeeded

Condition:

```text
implementation node succeeded
verifier node failed
```

Action:

- preserve implementation node succeeded fact;
- mark verifier failed;
- update ledger/gap as failed verification;
- create or expose gap for debug/fix/supersede decision;
- do not mutate implementation terminal fact.

### 7.6 Retry needed after infra failure

Condition:

```text
infra failure and retry_count < retry_limit
```

Action:

- create new node_materializations attempt;
- update latest_task_id/latest_run_id snapshot only after new materialization;
- keep old attempt terminal status;
- record `node_recovery_retry_scheduled`.

### 7.7 Business failure

Condition:

```text
worker receipt says failed because task approach failed
verifier failed because artifact behavior is wrong
```

Action:

- do not auto retry same node by default;
- mark node failed;
- open goal gap;
- decision provider may later create debug/fix/supersede/strategy_update node.

## 8. Retry / Rerun Policy

Default local policy:

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

Policy must be configurable per job, but deterministic defaults should be enough for Phase 4E.

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

## 10. Observability Changes

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

## 11. Implementation Plan

### Step 1: Document and schema compatibility

- Document recovery events and policies.
- Reuse existing `node_materializations` table where possible.
- Add schema columns only if required, preferring metadata_json for Phase 4E MVP.
- Ensure all new state is recoverable from DB.

### Step 2: Reconcile reducer

- Implement `reconcile_runtime_materializations()`.
- Add deterministic tests for each mismatch case.
- Ensure repeated reconcile is idempotent.

### Step 3: Retry/rerun policy

- Implement default retry policy.
- Add new materialization attempt on retry.
- Preserve old attempt terminal facts.
- Prevent business failure auto retry.

### Step 4: Supervisor integration

- Call reconcile before ingest in `advance_runtime_job()` or supervisor tick.
- Record reconcile summary in tick result.
- Ensure lease release still happens on reconcile errors.

### Step 5: Consistency checker

- Implement `check_runtime_consistency()`.
- Add CLI `runtime consistency`.
- Add focused tests for checkpoint/ledger/node/materialization references.

### Step 6: Observability API

- Add recovery/consistency/legal_waiting_reason to `runtime_observability_snapshot()`.
- Extend `runtime inspect`.
- Add focused API shape tests.

### Step 7: Synthetic long-run foundation

- Add a deterministic synthetic test that runs multiple reconcile / advance / compaction cycles.
- Include stale worker, retry, validator rejection, compaction fallback and liveness recovery.

## 12. Tests

Required offline tests:

- node running but task missing records `materialization_lost`;
- node running but run stale records `worker_run_stale`;
- task terminal but receipt missing records `receipt_missing`;
- task failed but node running becomes node_failed through reconcile/ingest;
- verifier failed does not rewrite implementation succeeded;
- infra timeout retries once and creates new materialization attempt;
- business failure does not auto retry same node;
- repeated reconcile is idempotent;
- retry does not overwrite old attempt terminal status;
- consistency checker rejects missing ledger node ref;
- consistency checker rejects checkpoint refs to missing node/event/patch;
- job done consistency requires required goal evidence;
- active incomplete job without runnable path requires liveness event;
- supervisor tick calls reconcile before decision;
- runtime inspect exposes legal_waiting_reason/recovery/consistency.

Default tests must remain offline and deterministic. Real worker/LLM smoke tests are not prerequisites
for Phase 4E unit coverage.

## 13. Completion Definition

Phase 4E is complete when:

- runtime can reconcile common worker/materialization mismatches without LLM;
- retry/rerun creates attempt history and preserves terminal facts;
- supervisor tick starts from reconciled worker state;
- consistency checker catches critical DB/reference violations;
- observability exposes legal waiting reason and recovery status;
- synthetic long-run test can include worker stale/retry/reconcile without silent idle;
- all default tests remain offline.

After Phase 4E, the recommended next stages are:

```text
Phase 4F Runtime Capability / Security Policy
Phase 4G Synthetic Long-Run Soak and Real Compaction Smoke
Phase 4H Dashboard Runtime UI
```
