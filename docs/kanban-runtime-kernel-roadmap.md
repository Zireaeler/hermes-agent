# Hermes Kanban Runtime Kernel 实现路线

本文档是 Hermes Kanban Runtime Kernel 的实现总控计划。它不是架构论文，也不是具体代码设计；具体架构以 `docs/kanban-runtime-kernel-design.md` 为准，阶段细节以 `docs/kanban-runtime-kernel-phase1.md`、`docs/kanban-runtime-kernel-phase2.md`、`docs/kanban-runtime-kernel-phase2b.md`、`docs/kanban-runtime-kernel-phase2c.md` 和 `docs/kanban-runtime-kernel-phase2d.md` 为准。

本文件负责定义整体演进顺序、阶段依赖关系和不可违背的实现约束。

## 1. 文档目的

Runtime Kernel 的目标不是构建传统 multi-agent workflow，而是在 Hermes Kanban 执行系统之上构建一个长期任务执行 runtime，使系统能够：

- 接收用户复杂目标。
- 将目标转换为可追踪 runtime job。
- 动态生成和调整 execution graph。
- 调度 Codex、Claude Code、本地脚本、人工作业等 execution backend。
- 持续验证目标完成情况。
- 在目标未完成且无需用户决策时持续推进。
- 支持长任务 decision session 和上下文压缩。
- 保证任务执行可恢复、可审计、可回放。

本文档不定义具体代码实现，而定义实现顺序和阶段边界。

## 2. 核心架构约束

以下原则在所有阶段必须保持，不允许为了快速实现而破坏。

### 2.1 Runtime State 必须外置

数据库是系统唯一事实来源。事实状态包括 runtime job、goal contract、progress ledger、execution graph、execution event、artifact、graph patch、kernel decision、decision session segment 和 checkpoint。

LLM 上下文不是事实来源。LLM 可以拥有连续推理上下文，但不能直接修改系统状态。

### 2.2 Execution Graph 不是 Workflow

Execution graph 表示当前为了完成目标而生成的工作结构。

禁止：

- 固定 planner -> coder -> reviewer -> tester 流程。
- 根据 node type 自动推进阶段。
- 将 node type 当作 workflow phase。

Node type 只表示执行意图，例如 `analysis`、`implementation`、`verification`、`debug`、`research`、`human_gate`。

### 2.3 Worker 不是 Manager

Codex、Claude Code 或其他 worker 是 execution unit。

Worker 负责执行单个 node、产生 artifact、返回 evidence。Worker 不负责管理整个任务、创建其他 worker 或修改 execution graph。

### 2.4 LLM 只产生结构建议

LLM 可以提议创建节点、拆分节点、验证节点、human gate 或策略调整。

LLM 不可以直接修改数据库、直接标记任务完成、直接决定 node ready 或绕过 validator。

所有结构变化必须经过：

```text
LLM proposal
      |
      v
Patch Validator
      |
      v
Runtime State Update
```

### 2.5 Goal 决定完成，不是 Graph 决定完成

Execution graph 是实现路径。Goal contract 是完成标准。

禁止把“所有 node 完成”等同于“任务完成”。必须由 required goal items 是否有足够 evidence 来判定任务完成。

## 3. 总体阶段规划

整体分为：

```text
Phase 0  Architecture Contract
Phase 1  Runtime Kernel Foundation
Phase 2A Execution Graph Control Plane
Phase 2B Decision Provider / Decision Session Foundation
Phase 2C Goal Progress Hardening
Phase 2D Decision Session Compaction
Phase 3A Real Decision Provider Integration
Phase 3B Real Provider Patch Quality / Validator Feedback
Phase 3C Real Provider End-to-End Runtime Loop
Phase 3D Long Running Autonomous Task Runtime
Phase 4  Production Hardening
```

当前代码已经推进到 Phase 4G1 MVP：Phase 2D 本地 compaction 闭环、Phase 3 real
decision provider、Phase 4 production hardening、Phase 4E recovery、Phase 4F capability
policy、Phase 4G deterministic soak、Phase 4G0 memory lifecycle 和 Phase 4G1 real-smoke
入口均已实现。

隔离真实模型源已验证 decision execute、一次 rejected patch、一次 accepted apply，以及
real compaction 的 validator/fallback/segment rollover 安全路径。真实 compaction candidate
quality 尚未通过，Phase 4G2 bounded loop 和 Phase 4G3 real worker lane smoke 仍属于后续
阶段。详见 `docs/kanban-runtime-kernel-real-integration-validation.md`。

## 4. Phase 0: Architecture Contract

### 目标

固定 Runtime Kernel 的核心设计边界，防止实现过程中重新退化成旧 Orchestra。

### 实现内容

新增架构约束文档、terminology 定义、禁止设计列表。

明确：Runtime Kernel 不是 multi-agent workflow，而是 goal-driven execution runtime。

### 必须禁止

禁止重新引入 planner agent、coder agent、reviewer agent、manager agent、固定 phase state machine、mailbox 作为主要控制机制。

### 验收标准

任何新增模块都必须回答：

1. 它服务哪个 goal？
2. 它修改哪个 runtime state？
3. 它是否可以恢复？
4. 它是否可以通过 event 重放或审计？

## 5. Phase 1: Runtime Kernel Foundation

### 目标

建立 runtime kernel 基础能力。本阶段不追求智能，只证明任务可以被 runtime 管理。

### 实现内容

新增 runtime job、goal contract、goal items、progress ledger、execution node、execution dependency、execution event、graph patch、kernel decision、artifact reference。

实现：

```python
create_runtime_job()
advance_runtime_job()
status_runtime_job()
apply_graph_patch()
```

### Decision Provider

本阶段使用 deterministic provider，不接真实 LLM。

例如输入 `analysis node completed`，输出 `create implementation node` 和对应 verifier node。

### 验收标准

必须证明 job 可以创建、graph 可以保存、patch 可以应用、node 状态可以推进、event 可以记录、completion 由 goal contract 和 progress ledger 判定。

## 6. Phase 2A: Execution Graph Control Plane

### 目标

让 execution graph 真正驱动 Kanban worker。

### 实现内容

增加 node materialization。

流程：

```text
Execution Node
      |
      v
Kanban Task
      |
      v
Worker Run
      |
      v
Evidence
      |
      v
Node / Ledger Update
```

新增 `node_materializations`，用于记录 node、task、run、attempt、worker lane 和 terminal event。

### Worker Context

worker 初始化上下文必须包含 root objective、node goal/gap、node description、dependency output、constraints、artifact refs 和 receipt schema。

### 验收标准

必须证明一个 execution node 可以创建 Kanban task、启动 worker fixture、获取 evidence、更新 runtime state。

## 7. Phase 2B: Decision Provider / Decision Session Foundation

### 目标

让结构决策从冷启动 snapshot oracle 升级为受 DB 约束的 decision session。

### 实现内容

新增或补强 decision session、decision delta、provider request composition、kernel decision record、provider output parsing、record/replay provider、patch rejection feedback。

Provider input 形态应逐步变为：

```text
stable runtime contract
current goal contract
latest checkpoint
short tail
current delta
```

### 验收标准

系统可以记录每次 decision 的 delta、provider output、parsed patch、validator result 和 patch outcome；patch 被拒绝后，拒绝原因能够反馈到后续 decision context。

## 8. Phase 2C: Goal Progress Hardening

### 目标

实现真正长期推进能力的本地规则基础。

### 实现内容

补硬 goal gap detector、liveness check、anti-stuck policy、human gate policy、goal-driven completion、ledger-aware worker receipt ingest。

Goal gap detector 负责发现未满足目标、缺少 evidence、验证失败、当前 graph 无法继续推进。

Liveness check 的核心规则：

```text
goal incomplete
AND no running node
AND no ready node
AND no human gate
AND no pending decision
= liveness_violation + goal gap decision
```

Anti-stuck 检测同一个 gap 多轮无进展、同类 node 连续失败、patch 连续 rejected、decision 连续 noop、worker 多次 uncertain。

Human gate policy 明确什么时候必须问用户，例如凭证、付费资源、删除数据、高影响架构选择；普通文件组织、实现选择、mock-first 实现不应阻塞用户。

### 验收标准

复杂任务不能因为当前 graph 空、某个 worker 失败、当前方案不可行而直接停止。系统必须能生成 gap decision、strategy update 或合法 human gate。

## 9. Phase 2D: Decision Session Compaction

### 目标

实现类似 Codex/Claude Code loop 的长期上下文压缩能力。

### 核心原则

压缩对象不是 worker log、execution log 或 receipt。

压缩对象是 decision session transcript，即：

```text
DB delta
   |
   v
LLM patch
   |
   v
validator result
   |
   v
graph change
   |
   v
goal / ledger / gap change
```

形成的系统级推理上下文。

### 实现内容

新增 decision session segment 生命周期：

```text
Decision Session
  |
  +-- Segment A
  |
  +-- Checkpoint
  |
  +-- Segment B
```

新增 `decision_session_segments`，保存 segment 生命周期、covered decision、covered event 和 revision。

新增 `decision_segment_entries`，保存 delta、provider output、patch、validator result、patch outcome 和 compaction event。它是一等 append-only transcript，不应从多张事实表事后反推。

新增 `decision_checkpoints`，保存压缩后的长期上下文，必须包含 goal contract、active milestone、satisfied goals、open gaps、key decisions、rejected approaches、failure boundaries、human decisions、artifact index 和 provenance。

### Compaction 流程

```text
Active Segment
      |
      v
Compaction Trigger
      |
      v
Compaction Provider / Deterministic Builder
      |
      v
Checkpoint Validator
      |
      v
Archive Old Segment
      |
      v
Create New Segment
```

### Compaction Trigger

第一阶段支持 token threshold 和 manual trigger。后续支持 milestone change、validator rejection、anti-stuck、human decision、graph large change。

### Compaction Profile

支持 markdown 热插拔，例如：

```text
profiles/
  token-budget.md
  validator-boundary.md
  human-change.md
  anti-stuck.md
  milestone-transition.md
```

每个 profile 定义使用场景、输入选择、压缩目标、输出 schema 和 validator 要求。

### 验收标准

必须证明长任务可以跨多个 context segment；压缩后旧 transcript 不进入 active context；checkpoint 可以恢复 decision session；checkpoint 错误不会污染 runtime state。

## 10. Phase 3A: Real Decision Provider Integration

### 目标

在不破坏 runtime kernel 边界的前提下接入真实 decision provider。

真实 provider 只替代 patch proposal 生成能力。DB authoritative state、goal gap detector、readiness reducer、progress ledger、completion、validator 和 compaction lifecycle 仍由本地 runtime 控制。

### 实现内容

新增 no-tools single-shot `RuntimeDecisionProvider`，复用 Hermes provider substrate，但不复用完整 `AIAgent` conversation/tool loop。

Provider input 必须来自 Phase 2D request composition：

```text
stable runtime contract
current goal contract
latest checkpoint
strict short tail
current delta
```

CLI 必须显式启用真实 provider，例如 `--provider real --model-provider ... --model ...` 或 `--provider real --codex-config`。

### 验收标准

真实 provider 调用可审计 provider/model/profile/request_ref/response_ref/parse_status/retry/validator result；默认单测不触网。

## 11. Phase 3B: Real Provider Patch Quality / Validator Feedback

### 目标

让真实 provider 不只是能调用，而是能更稳定地产生 validator 可接受的 graph patch。

### 实现内容

强化 decision profiles，新增 validator recovery profile，并在 `provider-smoke --execute` 中支持 validate-but-no-apply recovery retry。

Recovery 只能反馈 rejected patch 和 validator reason，不能放宽 validator，不能自动 apply rejected patch，也不能成为默认 `advance` 行为。

### 验收标准

真实 `.codex` 隔离 smoke 能 parsed + accepted；真实 isolated `runtime advance --provider real --codex-config` 能完成一次 patch applied；默认测试继续离线。

## 12. Phase 3C: Real Provider End-to-End Runtime Loop

### 目标

证明真实 provider patch 可以进入多轮 runtime 闭环，而不是停在单次 patch apply。

### 实现内容

补齐手动/测试 evidence bridge：按 `job_id + node_key` 完成最新 materialized Kanban task，写入结构化 receipt metadata。该桥只完成 Kanban task，不直接改 execution graph、progress ledger、goal item、graph revision 或 decision records。

多轮闭环必须经过：

```text
provider patch applied
  |
  v
node materialized to Kanban task
  |
  v
worker/manual evidence completed
  |
  v
runtime advance ingests evidence
  |
  v
ledger/gaps update
  |
  v
next provider decision or local completion
```

### 验收标准

离线测试能通过 fake provider 跑完 implementation + verification 的多轮 goal loop；真实 `.codex` smoke 能在隔离 job 中完成 evidence ingest 后的 real provider patch apply 和下一 node materialization。

## 13. Phase 3D: Long Running Autonomous Task Runtime

### 目标

实现真正持续推进复杂任务。

### 实现内容

增强长任务 continuation 语义，增加 strategy update、explicit goal waiver、DB-based resume 和 dynamic replanning 的最小闭环。

Strategy update 不是 provider 的隐藏记忆，也不是直接 DB mutation；它必须落成 `strategy_update` execution node，服务 goal item 或 gap，并通过 Kanban evidence 返回。

Goal evolution 第一版先支持 explicit waiver。用户或 operator 可以 waive 某个 goal item，runtime 写入 ledger 和 events，completion 仍由 reducer 判断。

支持：

```text
任务开始
  |
  v
执行
  |
  v
发现问题
  |
  v
调整策略
  |
  v
继续执行
  |
  v
完成目标
```

### 验收标准

支持数小时级任务、多轮 worker 执行、多次策略调整、用户中途修改目标、任务恢复，以及合法 done / waiting_worker / waiting_decision / waiting_human 边界。

## 14. Phase 4: Production Hardening

### 目标

生产稳定性。

### 实现内容

Phase 4 拆分为：

```text
Phase 4A Real Compaction Provider Integration
Phase 4B Runtime Observability / Dashboard API
Phase 4C Production Supervisor / Recovery
Phase 4D Concurrency / Safety Hardening
```

Phase 4A 接入真实 compaction provider，但保留 deterministic fallback、
checkpoint validator、profile hash/version、旧 segment exclusion 和 provenance。

Phase 4B 补齐 dashboard/API 可观测性，展示 job、goal contract、progress
ledger、graph frontier、events、patch history、decision session、checkpoint、
compaction status、human gate、liveness 和 anti-stuck signals。

Phase 4C 实现 production supervisor/recovery：bounded daemon、advance lock、
pause/cancel、worker crash/stale recovery、retry/rerun policy 和 DB-based resume。

Phase 4D 做并发与安全 hardening。

并发：graph revision、db revision、optimistic concurrency、stale patch/checkpoint detection。

恢复：checkpoint restore、event replay、failed worker recovery。

观测：runtime metrics、decision latency、token usage、compaction frequency、patch acceptance rate。

安全：validator hardening、permission policy、audit trail。

### 当前落地状态

Phase 4 MVP 已经落地，提交为：

```text
6028c53 feat(kanban): harden runtime production phase4
```

当前已经具备：

```text
real/fake/deterministic compaction provider boundary
      |
      v
runtime inspect + dashboard read-only API observability
      |
      v
DB-backed supervisor lease + runtime supervise
      |
      v
stale checkpoint / materialization idempotency / provider fallback tests
```

Phase 4 MVP 的含义是：生产 hardening 的核心 runtime 闭环已经在代码中成立。
它不等于完整 production final。常驻 daemon、dashboard 前端 UI、完整 worker
crash/stale recovery、destructive/cost/credential safety policy、event replay
checker、runtime capability policy 和真实 compaction provider smoke/soak 仍属于后续补强。

Phase 4 的完成定义需要区分 Phase 3 前提和 Phase 4 新增能力：真实 decision
provider 已在 Phase 3 集成，Phase 4 继续要求它受 audit、observability、
retry/backoff 和 validator recovery 约束；Phase 4 自己新增的是真实
compaction provider 边界、observability、supervisor/recovery、concurrency 和
safety hardening。

建议后续新增：

```text
Phase 4E Worker Recovery Policy
Phase 4F Runtime Capability / Security Policy
Phase 4G Synthetic Long-Run Soak and Runtime Consistency Baseline
Phase 4G1 Real Model Provider Smoke
Phase 4G2 Real Provider Bounded Loop with Synthetic Worker Evidence
Phase 4G3 Real Worker Lane Smoke
Phase 4H Dashboard Runtime UI
```

Phase 4E 专门处理 stale materialization、run timeout、worker crash、task done
但 receipt missing、task failed 但 node running、node running 但 task/run
消失、retry/rerun policy，以及 terminal node fact 不可静默改写。

当前 Phase 4E MVP 已完成 worker/materialization reconcile 主路径、missing task、
missing receipt、stale、timeout、crash recovery、retry attempt history、
retry limit、business failure 不原地 retry、verifier failure 不改写实现节点、
supervisor/advance 前置接入、基础 consistency checker、checkpoint/ledger
引用检查、CLI 入口和 observability 字段。它仍不是 production complete；
synthetic long-run soak、更复杂的真实 worker crash/timeout 组合场景、更深
event replay consistency 和 capability/security policy 仍属于后续收尾。

Phase 4F 专门把 destructive action、external cost、credential/secret、
workspace boundary、network、git、database migration 等权限判断收敛成
runtime capability policy，避免安全规则散落在 validator、worker lane、
dashboard API 和 CLI。

Phase 4F MVP 的实现边界是：定义 capability taxonomy 和默认 policy；让
`create_node` / `strategy_update` 可以声明 `requested_capabilities`；让 graph
patch validator 检查未知 capability、denied capability 和 require-human
capability；materialization 前评估 node capability，未授权危险 capability 不创建
Kanban task；worker context 明确下发 allowed / denied / requires-human；runtime
inspect 和 dashboard read-only API 能解释 `blocked_by_policy`。LLM 只能提出
capability request 或 human gate，不能自行授权。

Phase 4G0 专门做 Runtime Memory Hints。它不是复杂学习系统，也不是新的事实源；
它只把跨 job 经验以 Markdown guidance / memory index / scoped topic /
candidate 的形式回流到 decision provider 输入。Runtime guidance 是强规则、短小、
常驻；memory topic 是按 scope 和 goal/gap 检索的 non-authoritative hint；
candidate 默认不注入，人工 promote 后才进入 accepted memory。第一版必须显式支持
runtime-global、workspace/repo、domain/job-family、job-local 四类 scope，防止经验
跨项目污染。

Phase 4G 专门做 deterministic synthetic long-run soak 和 runtime consistency
baseline。目标不是证明某个业务项目能完成，也不是证明真实模型质量，而是先证明
runtime 本身能承受几十到上百次 decision / patch / validator / compaction /
reconcile / memory / capability cycle，并且在 goal 未完成时不会静默 idle。

Phase 4G1 专门做真实模型源 smoke。它验证 real decision provider 和 real compaction
provider 的 no-tools single-shot 调用、解析、validator、fallback、审计和隔离行为。
它不接真实 worker，也不跑真实长任务。

当前已有一次隔离真实 smoke：decision execute 和 one-step apply 已验证；real compaction
fallback safety 已验证，但真实 checkpoint candidate 因 provenance 缺失未通过质量门槛。
真实运行事实和后续门槛统一记录在
`docs/kanban-runtime-kernel-real-integration-validation.md`。

Phase 4G2 专门做真实 decision provider 的 bounded loop，但 worker evidence 仍使用
synthetic receipt。目标是验证真实 provider 能在多轮 validator / ledger / gap feedback
中保持 runtime 边界。具体实现和验收见
`docs/kanban-runtime-kernel-phase4g2.md`。

Phase 4G3 再接真实 worker lane smoke，验证真实 provider proposal、Kanban
materialization、真实 worker evidence 和 runtime ingest 的端到端边界。

真实 compaction/provider/worker smoke 和 soak 都应复用 Phase 4G 的 report 和
consistency checker。

Phase 4H 再做 dashboard runtime UI。UI 应消费前面阶段形成的稳定
observability API，而不是提前展示一个 recovery 和 consistency 尚未稳定的系统。

## 15. 当前实现优先级

如果现在继续开发，推荐顺序：

```text
Phase 4 productionization follow-through
      |
      v
Phase 4E worker recovery MVP 收尾和 stale/crash 补强
      |
      v
Phase 4F runtime capability/security policy
      |
      v
Phase 4G0 runtime memory hints
      |
      v
Phase 4G synthetic long-run soak and runtime consistency baseline
      |
      v
Phase 4G1 real model provider smoke
      |
      v
Phase 4G2 real provider bounded loop with synthetic worker evidence
      |
      v
Phase 4G3 real worker lane smoke
      |
      v
Phase 4H dashboard runtime UI
```

原因：

Phase 4 MVP 已经补齐 runtime 的核心生产化边界，但还没有完整产品化运行形态。

dashboard/API 已有读面，但还没有前端 UI。

supervisor tick 和 lease 已有，但还不是 packaged daemon。

compaction provider 边界已接入，真实 fallback smoke 已验证；真实模型 candidate quality
和 long-run soak 仍需要继续验证。

并发和 idempotency 已有关键测试，但 destructive action、external cost、
credential、workspace boundary、network、git write、database migration 和
event replay 仍需要专门 hardening。

runtime 还缺少跨 job 的经验回流。Phase 4G0 应先用 Markdown + scope + candidate /
accepted / deprecated 的轻量模型验证经验提示是否能减少重复错误，避免一开始实现
复杂 `runtime_experience_items` / confidence / promotion 系统。

最优先的是 Phase 4E，因为真实长任务最先出问题的地方通常是 worker
materialization、Kanban task/run、receipt、node state 和 progress ledger 之间
的不一致。如果先做 dashboard UI 或继续扩展智能层，卡住时仍然无法判断是 worker
状态脏、supervisor recovery 有 bug、compaction 降级，还是模型决策差。

Production complete 前还需要一类 synthetic long-run soak：模拟几十到上百次
decision / patch / validator / compaction cycle，多次 segment compaction，
确认旧 transcript 不进入 provider input、stale checkpoint 被拒绝、fallback
可观测、supervisor lease 可释放/抢占、materialization 不重复，且 goal 未完成
时不会静默停止。
