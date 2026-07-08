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
Phase 3  Long Running Autonomous Task Runtime
Phase 4  Production Hardening
```

当前代码实现已经推进到 Phase 2D 本地闭环：active segment、append-only entries、deterministic checkpoint、checkpoint validator、manual compaction、token-threshold policy、rejection/noop policy、markdown profile loader/hash、strict short tail provider input 和 CLI 可观测性已经具备。Phase 2D 剩余工作是更丰富语义触发和真实 LLM compaction provider。

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

## 10. Phase 3: Long Running Autonomous Task Runtime

### 目标

实现真正持续推进复杂任务。

### 实现内容

增强 decision session，增加 milestone planning、strategy update、dynamic replanning 和 goal evolution。

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

支持数小时级任务、多轮 worker 执行、多次策略调整、用户中途修改目标和任务恢复。

## 11. Phase 4: Production Hardening

### 目标

生产稳定性。

### 实现内容

并发：graph revision、optimistic concurrency、stale patch detection。

恢复：checkpoint restore、event replay、failed worker recovery。

观测：runtime metrics、decision latency、token usage、compaction frequency、patch acceptance rate。

安全：validator hardening、permission policy、audit trail。

## 12. 当前实现优先级

如果现在继续开发，推荐顺序：

```text
Real decision provider integration
      |
      v
real compaction provider integration
      |
      v
Phase 3 long-running task runtime
      |
      v
Phase 4 production hardening
```

原因：

没有 goal-driven runtime，系统不知道为什么继续。

没有 decision session，LLM 无法长期理解任务。

没有 compaction，长期任务无法稳定运行。

这三者是 Hermes Runtime Kernel 从“任务调度器”成为“长期任务执行系统”的关键路径。当前三者已经有本地基础实现，下一步应进入真实 decision provider，并保留真实 compaction provider 作为随后阶段。
