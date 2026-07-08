# Hermes Kanban Runtime Kernel Phase 4 实现计划

Phase 4 的目标是把已经具备本地闭环和真实 decision provider 的 runtime kernel
推进到生产化边界。Phase 4 不再证明“runtime 能不能持续推进”，而是补齐真实
长期运行需要的 compaction provider、可观测性、supervisor 恢复、并发安全和
权限审计。

Phase 4 必须继续遵守前面所有不变量：DB 是唯一事实源；decision session 是
非权威推理上下文；decision provider 和 compaction provider 都不能写 DB；
worker receipt 不是 runtime compaction；completion、readiness、liveness、
blocked 和 human gate 都由本地 reducer / validator 控制。

## Phase 4 拆分

Phase 4 分为四个子阶段：

```text
Phase 4A Real Compaction Provider Integration
Phase 4B Runtime Observability / Dashboard API
Phase 4C Production Supervisor / Recovery
Phase 4D Concurrency / Safety Hardening
```

这四个阶段可以局部并行，但实现顺序建议从 4A 开始。原因是长期运行的 context
生命周期必须先稳定，否则 dashboard 和 daemon 会放大错误上下文。

## Phase 4A: Real Compaction Provider Integration

### 目标

接入真实 LLM compaction provider，让 decision session compaction 从
deterministic DB-derived checkpoint 升级为可由模型进行结构化认知重写，同时
保留 deterministic fallback。

### 实现内容

新增 no-tools single-shot `RuntimeCompactionProvider`：

```python
compaction_provider(segment, db_state, profile, budget) -> checkpoint_candidate
```

它可以复用 Hermes provider substrate，但不能复用完整 `AIAgent` 工具循环，不能
执行工具，不能写 DB，不能读取 worker 内部日志原文，不能把旧 segment 原文在
compaction 后继续带回 active provider input。

Compaction provider 输入必须包含：

- stable runtime compaction contract；
- selected compaction profile；
- source segment entries；
- current DB-derived goal contract / ledger / frontier / gaps；
- validator rejection history；
- human decisions / waivers；
- token budget；
- checkpoint output schema。

输出必须是 checkpoint candidate，而不是 graph patch。

Checkpoint candidate 必须经过现有 checkpoint validator。validator 继续检查：

- provenance；
- node/goal/artifact/patch/human decision refs；
- revision 绑定；
- failed verifier 不得写成 passed；
- partial/self-reported evidence 不得写成 confirmed；
- hard blocker / human gate 不得遗漏。

### 非目标

不让 compaction provider 决定下一步怎么推进 execution graph。

不让 compaction provider 修改 goal contract、ledger、graph 或 job state。

不把 deterministic fallback 删除。真实 provider 失败时必须可降级。

### 验收标准

- `runtime compact --provider real ...` 能显式调用真实 compaction provider；
- 默认测试仍不触网，使用 fake/replay compaction provider；
- compaction 成功后旧 segment 原文不进入 provider input；
- checkpoint profile hash/version/request/response refs 可审计；
- validator 拒绝错误 checkpoint 时不会污染 active segment；
- deterministic fallback 仍可用。

## Phase 4B: Runtime Observability / Dashboard API

### 目标

让 operator 能判断长任务卡在哪里：graph、goal contract、progress ledger、
decision provider、compaction、worker evidence、human gate，还是 liveness。

### 实现内容

新增或补强 API/CLI JSON surfaces：

- `/runtime/jobs`
- `/runtime/jobs/{id}`
- `/runtime/jobs/{id}/goals`
- `/runtime/jobs/{id}/ledger`
- `/runtime/jobs/{id}/graph`
- `/runtime/jobs/{id}/events`
- `/runtime/jobs/{id}/patches`
- `/runtime/jobs/{id}/decisions`
- `/runtime/jobs/{id}/decision-session`
- `/runtime/jobs/{id}/checkpoints`
- `/runtime/jobs/{id}/compactions`
- `/runtime/jobs/{id}/human-gates`
- `/runtime/jobs/{id}/liveness`

Dashboard 应展示：

- job state 和 legal waiting reason；
- required goal items 与 satisfied/partial/waived/open 状态；
- progress ledger evidence refs；
- active frontier；
- graph patches accepted/rejected/stale/noop；
- latest decision provider/model/profile/request_ref/response_ref；
- active decision segment、latest checkpoint、short tail composition；
- compaction status/profile/hash/validator result；
- liveness violation / anti-stuck signals；
- human gates and waiver history。

### 非目标

不在 dashboard 中直接修改 DB。所有 action 必须调用 runtime command/API：
advance、compact、waive-goal、human-decision、cancel/pause/resume。

不展示完整 worker logs 或完整 decision transcript 原文作为默认视图。默认展示
bounded summary 和 refs。

### 验收标准

- CLI/API 能返回 dashboard 所需结构；
- dashboard 不需要拼 SQL 或读取私有表细节；
- 长任务卡住时能从 status 判断是 worker、decision、compaction、human gate、
  liveness 还是 validator 问题；
- 默认测试覆盖 API JSON shape。

## Phase 4C: Production Supervisor / Recovery

### 目标

把手动 `runtime advance --loop` 升级为可部署的 bounded supervisor 服务，同时
保持每个 tick 可恢复、可审计、可停止。

### 实现内容

新增 supervisor runner：

- poll active jobs；
- acquire `advance_lock` / job lease；
- run bounded advance ticks；
- honor pause/cancel;
- backoff on provider unavailable；
- stop at legal boundaries：waiting_worker、waiting_human、waiting_decision、
  done、blocked、cancelled、budget_exhausted；
- record illegal idle / liveness violation；
- trigger compaction policy；
- avoid duplicate materialization；
- resume after process crash using DB state。

Worker recovery：

- detect stale node materialization；
- map Kanban task/run crash/timeout into node evidence;
- allow retry/rerun with `node_materializations` attempt history；
- never rewrite terminal node facts silently。

### 非目标

不引入 fixed planner/coder/reviewer/tester phase manager。

不让 supervisor hold hidden state required for correctness。

不 make daemon required for unit tests; tests can drive supervisor ticks directly。

### 验收标准

- two supervisor processes cannot advance the same job concurrently；
- crash/restart can resume from DB without duplicate running task；
- stale worker run produces auditable event and recoverable state；
- pause/cancel respected；
- budget exhausted is resumable, not failed。

## Phase 4D: Concurrency / Safety Hardening

### 目标

补齐生产运行中的并发、权限、审计和破坏性操作边界。

### 实现内容

并发：

- graph revision / db revision 全面用于 patch apply；
- expected revision stale rejection；
- optimistic concurrency around decisions and compactions；
- stale checkpoint rejection；
- idempotency keys for materialization and event ingest。

安全：

- permission policy；
- destructive action policy；
- credentials/secrets human gate；
- external cost human gate；
- workspace boundary checks；
- validator hardening；
- audit trail for every operator/runtime/provider action。

恢复：

- event replay checks；
- checkpoint restore validation；
- failed compaction retry/fallback；
- failed provider retry/backoff；
- failed worker retry/rerun policy。

### 非目标

不把安全边界交给模型判断。模型可以 propose request_human 或 strategy_update，
但 validator/policy 决定是否允许。

### 验收标准

- stale patch/checkpoint 被拒绝；
- destructive/cost/credential actions require human gate；
- audit trail 能解释每个 state change；
- repeated ingest/materialization is idempotent；
- production failure modes不直接 corrupt runtime state。

## Phase 4 完成定义

Phase 4 完成时，Hermes Runtime Kernel 应具备：

- 真实 decision provider；
- 真实 compaction provider + deterministic fallback；
- job-level decision session segmentation/checkpoint lifecycle；
- goal contract / progress ledger / gap detector；
- liveness and anti-stuck；
- DB-based long-running resume；
- supervisor service；
- dashboard/API observability；
- concurrency and safety hardening；
- explicit human gate and waiver workflow；
- complete audit trail。

此时系统才可以从“runtime kernel prototype”进入“可长期运行的生产 runtime”。
