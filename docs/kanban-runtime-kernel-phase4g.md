# Hermes Kanban Runtime Kernel Phase 4G：Synthetic Long-Run Soak 和 Runtime Consistency Baseline

Phase 4G 的目标是把 Phase 4E、Phase 4F 和 Phase 4G0 已经形成的 production
hardening 能力放进同一个 deterministic 长任务场景中验证，建立 runtime kernel 的
长期运行基线。

Phase 4G 不新增新的智能能力，不接入新的模型行为，不优先做 dashboard UI。它要回答
一个更基础的问题：当 runtime job 经历几十轮 decision、patch、validator、worker
evidence、recovery、compaction、memory hint、capability block、human decision 和
supervisor resume 时，系统是否仍然保持事实一致、可恢复、可审计，并且不会在 goal
未完成时静默停住。

Phase 4G 是 Phase 4 production baseline 的集成验证阶段。它应在以下能力具备后进行：

- Phase 4E worker recovery 和 runtime consistency；
- Phase 4F runtime capability / security policy；
- Phase 4G0 runtime memory lifecycle。

## 1. 背景

Phase 4E 解决 worker materialization、Kanban task/run、node state、receipt 和
ledger 之间的不一致。

Phase 4F 解决 execution node、worker lane 和 runtime job 的 capability / security
边界。

Phase 4G0 解决跨 job 的 runtime memory lifecycle，让历史经验以 non-authoritative
hint 形式进入未来 decision provider request。

这些能力分别可测，但长期任务的风险主要出现在组合处：

- stale worker 被 recovery 后，decision provider 是否看到一致的 graph frontier；
- capability block 是否被 projection 成合法等待，而不是 liveness violation；
- memory hint 是否只进入 provider request，不进入 checkpoint 或 policy；
- compaction 多次发生后，旧 segment 原文是否确实退出 active provider input；
- patch rejected、stale revision 和 retry 是否都留下可审计事件；
- supervisor restart / lease takeover 是否会重复 materialize ready node；
- goal gap reopen 后，completion 是否仍由 progress ledger 决定。

Phase 4G 用 deterministic synthetic long-run soak 先验证 runtime 自身。真实模型
compaction smoke、真实复杂项目长跑和 dashboard UI 应在这个基线之后推进，否则问题
定位会被模型质量、worker 状态、权限策略和上下文生命周期混在一起。

## 2. 目标

Phase 4G 的目标是实现一套可重复、离线、确定性的 long-run soak harness，并补强
runtime consistency checker，使它能够覆盖跨模块不变量。

目标包括：

- 构造 synthetic runtime job；
- 驱动多轮 supervisor / advance / materialization / evidence ingest；
- 注入 worker stale、receipt missing、validator rejected、stale patch、capability
  require_human、compaction fallback、memory hint usage 等事件；
- 多次触发 decision session compaction；
- 验证旧 segment 不进入 active provider input；
- 验证 runtime memory hint 不进入 checkpoint，不影响 validator、completion 或
  capability policy；
- 验证 materialization 不重复，retry 不覆盖 terminal fact；
- 验证 legal waiting reason、liveness 和 policy block 的投影一致；
- 验证 final completion 由 required goal items 的 sufficient evidence 判定；
- 生成 bounded soak report，供 CLI、测试和后续 dashboard 使用。

## 3. 非目标

Phase 4G 不接真实 LLM。

Phase 4G 不接真实 Codex / Claude Code worker。

Phase 4G 不实现 dashboard 前端 UI。

Phase 4G 不引入 embedding、vector database 或自动 learning。

Phase 4G 不让 deterministic soak provider 成为新的默认 workflow。Soak scenario 是测试
夹具，不是 runtime 的生产推进策略。

Phase 4G 不把所有 consistency 问题都自动修复。它至少要能稳定检测、分类、审计和阻止
错误状态继续污染 runtime。

## 4. 核心原则

### 4.1 Soak 必须 deterministic

第一版必须完全离线、无网络、无真实模型、无真实外部 worker。所有 provider output、
worker receipt、human decision 和 compaction result 都应由 fixture 或 scripted
driver 产生。

这样失败时可以明确定位为 runtime 状态机、reducer、validator、policy、compaction 或
memory integration 的问题，而不是模型输出波动。

### 4.2 Soak 不是 workflow

Synthetic scenario 可以有步骤，但不能把步骤实现成生产 workflow。它只能驱动 runtime
已有 API：

- `advance_runtime_job()`；
- `supervise_runtime_job()`；
- `apply_graph_patch()`；
- `materialize ready node`；
- `ingest_runtime_node_evidence()`；
- `compact_decision_session()`；
- `reconcile_runtime_materializations()`；
- `create_human_decision()`；
- runtime memory candidate / promote / usage audit；
- capability authorization / policy evaluation。

生产 runtime 仍然必须 goal-driven，而不是 phase-driven。

### 4.3 Consistency checker 是本地事实审计

Consistency checker 不能依赖 LLM。它读取 DB state、event log、graph、ledger、
materialization、decision session、checkpoint、memory usage 和 capability projection，
输出 deterministic violations / warnings / pass summary。

### 4.4 Report 不能替代事实源

Soak report 是测试和 operator 辅助输出，不是事实源。它可以记录 summary、counts、
first_failure、final_status 和 invariant results，但不能作为 completion、readiness 或
policy 的依据。

## 5. Synthetic Long-Run Soak Harness

建议新增：

```python
run_runtime_soak(conn, scenario, *, max_ticks=None, seed=None) -> dict
```

或者在测试层先实现同等 fixture。第一版可以不暴露公共 API，但结构应稳定。

### 5.1 Scenario 输入

Scenario 应包含：

- root objective；
- workspace_path；
- goal items；
- initial graph patch；
- scripted decision provider outputs；
- scripted worker receipts；
- scripted validator rejection / stale patch；
- scripted compaction triggers；
- scripted capability requests；
- scripted human decisions；
- expected invariant results。

建议采用 Python fixture 或 JSON-like dict。第一版不需要设计新的 DSL 文件格式。

### 5.2 Tick 顺序

每个 soak tick 应尽量复用生产 supervisor 顺序：

```text
acquire supervisor lease
      |
      v
reconcile materializations
      |
      v
ingest worker evidence
      |
      v
update progress ledger / goal gaps
      |
      v
evaluate capability policy
      |
      v
update readiness / materialize ready nodes
      |
      v
decision provider if needed
      |
      v
apply / reject graph patch
      |
      v
compact decision session if needed
      |
      v
record memory usage outcome
      |
      v
check completion / liveness / legal waiting reason
      |
      v
run consistency checker
      |
      v
release lease
```

如果现有生产 supervisor 顺序不同，Phase 4G 应明确差异并优先推动生产路径和 soak 路径
收敛。

### 5.3 Soak Report

Soak report 至少包含：

```json
{
  "scenario": "runtime_phase4g_baseline",
  "ticks": 80,
  "job_id": "rjob_xxx",
  "final_state": "done",
  "goal_completion": true,
  "decision_count": 24,
  "patch_applied": 18,
  "patch_rejected": 3,
  "stale_patch_rejected": 1,
  "worker_recoveries": 4,
  "materialization_attempts": 12,
  "compactions": 3,
  "memory_hints_used": 2,
  "capability_blocks": 1,
  "human_decisions": 1,
  "liveness_violations": 0,
  "consistency": {
    "status": "passed",
    "violation_count": 0
  }
}
```

Report 必须 bounded，不能包含完整 transcript、完整 worker log 或完整 topic memory。

## 6. Runtime Consistency Checker 补强

Phase 4G 应把 consistency checker 从单点检查扩展为跨模块不变量审计。

建议新增或补强：

```python
check_runtime_consistency(conn, job_id, *, strict=False) -> dict
```

### 6.1 Graph / Node 不变量

必须检查：

- terminal node state 与 terminal execution event 一致；
- succeeded / failed / blocked / superseded node 不被静默改写；
- ready node 没有 running materialization；
- running node 有 active materialization 或合法 recovery event；
- dependency 不成环；
- node 关联 goal item、gap 或 human reason；
- verifier failure 不改写被验证 node 的 terminal fact。

### 6.2 Materialization / Worker 不变量

必须检查：

- 每个 materialization attempt 有 node_id；
- terminal materialization 有 terminal_event_id 或明确 missing reason；
- latest_task_id/latest_run_id 与 attempt history 不冲突；
- 同一 ready node 不重复创建 active task；
- stale / crashed / missing run 被 reconcile 成结构事件；
- retry/rerun 创建新 attempt，不覆盖旧 attempt；
- task done 但 receipt missing 时不会直接标记 node succeeded。

### 6.3 Ledger / Goal 不变量

必须检查：

- progress ledger 引用的 goal item、node、artifact、event 存在；
- required goal item 没有 sufficient evidence 时 job 不得 done；
- job done 时 required goal items 都 satisfied 或 waived；
- goal gap reopen 后 completion projection 被撤回或等待重新验证；
- waiver 必须有合法 human decision 或 policy reason。

### 6.4 Decision Session / Compaction 不变量

必须检查：

- active segment 唯一；
- compacted old segment 不进入 active provider input；
- latest checkpoint 绑定 graph / ledger / goal revision；
- checkpoint 引用的 node、event、decision、patch、artifact 存在；
- rejected checkpoint 不关闭 source segment；
- deterministic fallback 被审计；
- fallback 连续超过阈值时有 `compaction_quality_degraded` 或等价事件。

### 6.5 Runtime Memory 不变量

必须检查：

- candidate memory 不进入 provider request；
- deprecated memory 不进入 provider request；
- accepted memory 以 non-authoritative hint 注入；
- memory hint 正文不进入 checkpoint payload；
- memory usage 关联 decision_id 和 provider request ref；
- memory hint 不影响 validator result、goal completion、readiness 或 capability
  authorization；
- memory candidate 不包含明显 secret / token / credential 原文。

### 6.6 Capability / Liveness 不变量

必须检查：

- denied capability 的 node 不被 materialize；
- requires_human capability 进入 waiting_human 或 waiting_capability_authorization；
- valid human authorization 可以解除 require_human，但不能覆盖 hard deny；
- capability block 被 observability 暴露为合法 waiting reason 或明确 policy error；
- job 未完成且无 ready / running / human / pending decision 时必须有 liveness event；
- blocked_by_policy 不能伪装成普通 idle。

### 6.7 Supervisor Lease 不变量

必须检查：

- 同一 job 同时只有一个 active supervisor lease；
- expired lease 可以被 takeover；
- lease takeover 不重复 materialize 已有 active attempt；
- budget exhausted 是合法 resumable exit，不是 completion；
- provider unavailable 是合法 waiting_decision / provider unavailable，不是 silent idle。

## 7. MVP Soak Scenarios

Phase 4G MVP 至少应覆盖以下 deterministic scenarios。

### 7.1 Baseline Long-Run Completion

目标：

验证多轮 graph expansion、worker evidence、ledger update 和 final completion。

要求：

- 至少 20 个 tick；
- 至少 5 个 execution node；
- 至少 2 个 goal item；
- completion 由 progress ledger 证明；
- 所有 required goal items satisfied 或 waived。

### 7.2 Worker Stale Recovery

目标：

验证 Phase 4E recovery 与 long-run supervisor 集成。

要求：

- node running 但 task/run stale；
- reconcile 产生 `worker_run_stale` 或等价事件；
- retry/rerun 创建新 materialization attempt；
- 旧 attempt 不被覆盖；
- final ledger 只引用有效 terminal evidence。

### 7.3 Validator Rejection and Strategy Update

目标：

验证 patch rejected 不会污染 graph，且 decision session 能收到反馈继续推进。

要求：

- 至少一次 unknown node / missing goal linkage / stale revision rejection；
- rejected patch 写入 event 和 decision segment；
- 后续 patch 改变策略并被接受；
- liveness 不静默停止。

### 7.4 Compaction Rollover

目标：

验证多次 decision session compaction 后 provider input 正确。

要求：

- 至少 2 次 compaction；
- old segment 标记 compacted；
- active provider input 不包含 old transcript 原文；
- checkpoint refs 通过 validator；
- fallback / rejected checkpoint 有审计。

### 7.5 Runtime Memory Hint Isolation

目标：

验证 Phase 4G0 memory lifecycle 与 long-run decision provider 集成。

要求：

- 先生成 candidate；
- promote 到 accepted topic；
- 后续类似 gap 检索到 memory hint；
- memory hint 被标记 non-authoritative；
- memory hint 不进入 checkpoint；
- memory hint 不改变 validator / capability / completion 结果。

### 7.6 Capability Gate

目标：

验证 Phase 4F capability policy 与 liveness 关系。

要求：

- node 请求 requires_human capability；
- runtime 不 materialize worker；
- job projection 是合法 waiting_human / waiting_capability_authorization；
- human authorization 后 node 才可 materialize；
- hard deny 不能被 human authorization 覆盖。

### 7.7 Supervisor Resume and Lease Takeover

目标：

验证 supervisor crash / restart 后不重复执行事实。

要求：

- active lease 过期；
- 新 supervisor takeover；
- 已有 active materialization 不重复创建；
- pending decision 不重复写入不一致 patch；
- final report 记录 takeover。

### 7.8 Goal Gap Reopened

目标：

验证已满足目标被 verifier 或 later evidence 重新打开后，job 不会错误 done。

要求：

- goal item 先 partial / satisfied；
- verifier 失败或 evidence invalid 产生 gap reopen；
- job completion 撤回或保持 active；
- debug / verification node 推进后重新 satisfied；
- final done 仍由 ledger 证明。

## 8. Observability 和 CLI

Phase 4G MVP 可以先提供测试内部 report。若暴露 CLI，建议为：

```bash
hermes kanban runtime soak --scenario phase4g-baseline --json
```

CLI 输出应是 bounded report，不应输出完整 decision session transcript、worker log、
memory topic 正文或 secret。

Dashboard API 后续可以复用 report shape，但 Phase 4G 不要求实现 dashboard UI。

## 9. 测试策略

第一批测试必须 deterministic，不依赖网络、真实模型或真实 worker。

建议测试：

- `test_runtime_soak_baseline_completion`
- `test_runtime_soak_worker_stale_recovery`
- `test_runtime_soak_validator_rejection_strategy_update`
- `test_runtime_soak_compaction_rollover_excludes_old_segment`
- `test_runtime_soak_memory_hint_isolation`
- `test_runtime_soak_capability_gate_waiting_reason`
- `test_runtime_soak_supervisor_lease_takeover_no_duplicate_materialization`
- `test_runtime_soak_goal_gap_reopened_blocks_done`
- `test_runtime_consistency_checker_reports_cross_module_violations`

测试不应只断言最终 `done`。每个 scenario 都必须断言关键中间事件、attempt history、
checkpoint refs、memory usage、capability projection 和 liveness state。

## 10. 验收标准

Phase 4G MVP 完成时必须满足：

- 有一个 deterministic long-run soak harness；
- baseline scenario 至少覆盖几十轮 runtime tick；
- worker recovery、capability policy、memory hint、compaction 和 decision patch 在同一
  job 中被组合验证；
- consistency checker 覆盖 graph、materialization、ledger、checkpoint、memory、
  capability、liveness 和 supervisor lease；
- goal 未完成且无合法等待原因时必然产生 liveness violation；
- goal 完成只能由 progress ledger 和 goal contract 证明；
- memory hint 不进入 checkpoint，不影响 validator、completion、readiness 或
  capability policy；
- denied / requires_human capability 不会被 materialize；
- stale worker / missing receipt / crashed run 不会静默保持 running；
- repeated materialization 不覆盖 terminal fact；
- supervisor resume / lease takeover 不重复创建 active task；
- report bounded、可审计、无 secret；
- focused tests 全部离线通过。

## 11. 当前 MVP 实现入口

当前 Phase 4G MVP 的实现入口是：

```text
hermes_cli/kanban_runtime_soak.py
```

它提供 deterministic `phase4g-baseline` scenario：

- 创建 synthetic runtime job；
- 生成并 promote runtime memory candidate；
- 写入旧 decision segment sentinel 并执行 compaction rollover；
- 通过 DB lease takeover 跑 supervisor tick；
- 注入 validator rejected patch 和 stale revision patch；
- 创建 requires-human capability node；
- 通过 human authorization 解除 capability gate；
- 注入 worker run crash 并通过 reconcile retry；
- 完成 retry attempt 并由 progress ledger 证明 goal completion；
- 输出 bounded soak report。

CLI 入口：

```bash
hermes kanban runtime soak --scenario phase4g-baseline --json
```

当前 consistency checker 也已补充跨模块检查：

- node goal/gap/human linkage；
- ready/running node 与 active materialization；
- duplicate active materialization；
- capability blocked node 不得 materialize；
- memory hint 不得进入 checkpoint；
- memory usage 必须保持 accepted / non-authoritative；
- expired supervisor lease warning。

这些入口仍然是 deterministic baseline，不代表真实模型 compaction smoke 或真实 worker
长跑已经完成。

该 baseline 的 tick 总数包含 terminal job 后的 no-op padding，且仍使用 fixture
initialization。Phase 4G6 已新增 `phase4g6-active-long-run`，使用 production provider-first
初始化并以 50+ 个真实状态推进 tick、6 个 accepted checkpoint 和 goal gap reopen/resolution
替代 padding。Phase 4G baseline 保留用于早期跨模块回归，不再作为长期活跃运行的最高门槛。

## 12. 与后续阶段的关系

Phase 4G 完成后，再推进：

```text
Phase 4G1 Real Model Provider Smoke
      |
      v
Phase 4G2 Real Provider Bounded Loop with Synthetic Worker Evidence
      |
      v
Phase 4G3 Real Worker Lane Smoke
      |
      v
Phase 4G4 Worker Execution Continuity
      |
      v
Phase 4G5 Real Compaction Candidate Quality
      |
      v
Phase 4G6 Runtime Long-Run Reliability Soak
      |
      v
dashboard runtime UI
      |
      v
daemon / service packaging
      |
      v
production complete audit
```

真实模型 smoke 和真实 worker 长跑可以复用 Phase 4G report 和 consistency checker。
如果真实模型长跑失败，应优先用 Phase 4G deterministic baseline 判断问题是否来自
runtime 本身。

## 13. 总结

Phase 4G 不是让 runtime 更聪明，而是证明 runtime 在长期、复杂、可恢复的执行过程中
不会自己破坏事实状态。

它把 Phase 4E 的 worker recovery、Phase 4F 的 capability policy、Phase 4G0 的
runtime memory、Phase 2D/4A 的 decision session compaction 和 Phase 4B/4C 的
observability / supervisor lease 放进同一条 deterministic long-run 路径中验证。

只有这个 baseline 稳定后，继续接真实模型、真实 worker、dashboard UI 和 daemon 才有
可靠的诊断基础。
