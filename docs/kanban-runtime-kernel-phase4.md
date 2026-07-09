# Hermes Kanban Runtime Kernel Phase 4 实现计划与落地状态

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

## 当前落地状态

Phase 4 的第一版生产化硬化实现已经落地在：

```text
6028c53 feat(kanban): harden runtime production phase4
```

这个提交完成的是 Phase 4 MVP，也就是生产 hardening 的核心 runtime 闭环：

- real/fake/deterministic compaction provider 边界；
- compaction checkpoint validator 和 deterministic fallback；
- compaction provider audit；
- 失败 compaction 不污染 active segment；
- runtime observability snapshot；
- CLI `runtime inspect`；
- dashboard 只读 runtime API；
- DB-backed supervisor lease；
- CLI `runtime supervise`；
- stale checkpoint、materialization idempotency、supervisor lease 互斥等测试。

它不是完整生产最终态。以下能力仍属于后续补强：

- 常驻 daemon / service packaging；
- worker crash / timeout / stale run 的完整 recovery policy；
- runtime capability policy，以及 destructive action / external cost / credential / workspace boundary 的完整安全策略；
- dashboard 前端 UI 页面；
- event replay consistency checker；
- 真实模型 compaction smoke、provider-specific prompt behavior 验证和长任务 soak 测试。

因此当前状态应表述为：Phase 4 文档主干已经实现，Phase 4 MVP 已完成；严格
production complete 仍需要后续运行化、UI 和安全策略补强。

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

### 当前实现

已实现：

- `CompactionProviderRequest`
- `CompactionProviderResult`
- `RuntimeCompactionProvider`
- `DeterministicCompactionProvider`
- `build_compaction_provider_request()`
- `render_compaction_prompt()`
- `render_compaction_messages()`
- `parse_compaction_checkpoint()`
- provider-shaped `compact_decision_session()`

CLI：

```bash
hermes kanban runtime compact <job_id> --provider deterministic --json
hermes kanban runtime compact <job_id> --provider fake --json
hermes kanban runtime compact <job_id> --provider real --model-provider <provider> --model <model> --json
hermes kanban runtime compact <job_id> --provider real --codex-config --json
hermes kanban runtime compact <job_id> --provider real --no-fallback --json
```

实现边界：

- provider 调用是 no-tools single-shot；
- provider 只能输出 checkpoint candidate；
- graph patch JSON 会被 compaction parser 拒绝；
- checkpoint 必须通过 `validate_decision_checkpoint()`；
- provider rejected / parse_failed / provider_error 默认可 fallback 到 deterministic checkpoint；
- `--no-fallback` 时 rejected checkpoint 只写审计 entry，不关闭 source segment；
- 成功后旧 segment 标记 `compacted`，新 active segment 使用 checkpoint，不带旧 transcript 原文。

注意：当前实现证明的是 real compaction provider 的调用路径、解析、validator、
fallback 和审计闭环已经存在；它不等于真实模型 compaction 质量已经被证明。
真实模型的 checkpoint 质量、长任务上下文稳定性、provider-specific prompt
behavior 和多轮 compaction 后的策略连续性，仍需要后续 smoke / soak 测试验证。

自动 supervisor 使用真实 compaction provider 时还需要额外质量保护：如果同一
job 连续多次 provider_error、parse_failed 或 validator rejected 后被
deterministic fallback 接管，系统不应静默认为质量正常。后续应记录
`fallback_count` / `fallback_streak`，超过阈值时产生
`compaction_quality_degraded` 或 `operator_attention_required` synthetic event。

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

### 当前实现

已实现只读 observability surface：

- `runtime_observability_snapshot(conn, job_id, limit=50)`
- CLI `runtime inspect <job_id> --json`
- API `GET /api/runtime/jobs`
- API `GET /api/runtime/jobs/{job_id}`
- API `GET /api/runtime/jobs/{job_id}/{section}`

支持的 section：

```text
goals
ledger
graph
events
patches
decisions
decision-session
checkpoints
compactions
human-gates
liveness
```

这些 API 只读，不直接修改 DB。写操作仍必须走 runtime command/API，例如
advance、compact、waive-goal、human-decision、cancel/pause/resume。

当前未实现 dashboard 前端 UI 页面；本阶段只提供 dashboard 可消费的 API/CLI
结构。

后续 observability 需要把 legal waiting reason 做成一等字段，而不是只依赖
events 推断。Operator 应能直接区分：

```text
waiting_worker
waiting_human
waiting_decision
provider_unavailable
budget_exhausted
blocked_by_policy
liveness_violation
operator_attention_required
```

这类字段应和 job state、liveness summary 一起出现在 runtime snapshot/API 中，
让 dashboard 能判断当前状态是合法等待、可恢复等待、策略降级，还是非法 idle。

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

### 当前实现

已实现 DB-backed supervisor lease 和可测试 tick：

- `acquire_runtime_advance_lock()`
- `release_runtime_advance_lock()`
- `supervisor_runtime_tick()`
- `supervise_runtime_jobs_once()`
- CLI `runtime supervise`

CLI：

```bash
hermes kanban runtime supervise --job-id <job_id> --owner <owner> --json
hermes kanban runtime supervise --limit 10 --owner <owner> --json
```

当前 supervisor 不持有 correctness 所需的隐藏内存。每次 tick 通过 DB lease
保护同一 job 的 advance，并在 finally 中释放 lease。崩溃后依赖
`claim_expires_at` TTL 被新 owner 接管。

当前未实现完整常驻 daemon packaging，也未实现完整 stale worker run recovery；
worker recovery 仍依赖已有 Kanban task/run/evidence 机制和后续策略补强。

Worker recovery 应拆成后续明确交付项，而不是藏在 Phase 4C 的泛化描述里。
建议后续新增 `Phase 4E Worker Recovery Policy`，专门处理：

- stale materialization；
- run timeout；
- worker crash；
- task done 但 receipt missing；
- task failed 但 node 仍 running；
- node running 但 task/run 消失；
- retry/rerun policy；
- terminal node fact 不可静默改写；
- recovery event 和 operator audit。

这部分比 dashboard UI 更接近长期任务生产运行的必需条件。

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

- runtime capability policy；
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

### 当前实现

已实现或已有：

- graph patch expected revision stale rejection；
- checkpoint revision stale rejection；
- compaction provider rejection 不替换 active segment；
- deterministic fallback；
- materialization idempotency；
- supervisor lease 互斥和 TTL 接管；
- compaction provider input/output/checkpoint audit；
- runtime operator/API observability。

测试覆盖：

- fake compaction provider accepted checkpoint；
- compaction provider graph patch output rejection；
- provider checkpoint validator rejection；
- fallback disabled 时 active segment preserved；
- fallback enabled 时 deterministic checkpoint 接管；
- stale checkpoint rejection；
- supervisor lock exclusivity and expiry；
- supervisor tick 不重复 materialization；
- runtime inspect JSON shape；
- runtime dashboard API shape。

仍需后续补强：

- runtime capability policy；
- destructive action policy；
- external cost policy；
- credentials/secrets policy；
- workspace boundary checks；
- event replay consistency checks；
- failed worker retry/rerun policy；
- checkpoint restore validation command；
- provider backoff policy。

Runtime capability policy 是后续安全 hardening 的核心对象。Provider 和 worker
都可能提出动作，但最终必须由本地策略判断某类 node、lane 或 worker 是否允许：

- 读写文件系统；
- 访问 workspace 外路径；
- 访问网络；
- 使用凭证或 secret；
- 调用外部付费 API；
- 执行 git 操作；
- 执行数据库迁移；
- 执行破坏性命令。

模型可以 propose `request_human` 或 `strategy_update`，但不能自行判断这些
安全边界。Capability policy 应尽量成为 validator、worker lane、dashboard API
和 CLI 共享的策略对象，避免安全规则散落在多处。

## Phase 4 完成定义

Phase 4 完成时，Hermes Runtime Kernel 应具备：

- 真实 decision provider 已由 Phase 3 集成，并在 Phase 4 中继续受
  audit、observability、retry/backoff 和 validator recovery 边界约束；
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

Production complete 之前还必须补一类 synthetic long-run soak test。这个测试不
一定要真实运行数小时，但至少要模拟几十到上百次
decision / patch / validator / compaction cycle，覆盖：

- 多次 segment compaction；
- 旧 transcript 不进入新 provider input；
- stale checkpoint 被拒绝；
- deterministic fallback 生效但可观测；
- fallback streak 触发 quality degraded / operator attention；
- supervisor lease 释放和过期抢占；
- materialization 不重复；
- goal 未完成时不静默停止；
- liveness violation 能触发 gap decision / strategy update。

## 当前验证命令

Phase 4 MVP 的默认验证必须保持离线，不依赖真实网络或 API key。

已使用的验证命令：

```bash
scripts/run_tests.sh \
  tests/hermes_cli/test_kanban_runtime_kernel.py \
  tests/hermes_cli/test_kanban_runtime_decision.py \
  tests/hermes_cli/test_kanban_cli.py \
  -- --tb=short
```

结果：

```text
167 passed
```

Runtime dashboard API 的 focused 验证：

```bash
scripts/run_tests.sh tests/hermes_cli/test_web_server.py \
  -- -k test_runtime_observability_api --tb=short
```

结果：

```text
1 passed
```

完整 `test_web_server.py` 在当前环境存在无关失败，主要是缺少 `requests`、
`agent.model_metadata` patch 目标问题和 PTY websocket 测试问题；这些不是
Phase 4 runtime API 引入的。
