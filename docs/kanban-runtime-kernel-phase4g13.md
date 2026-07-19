# Hermes Kanban Runtime Kernel Phase 4G13

# Natural Discovery and Coordination Cost Control

## 1. 背景

Phase 4G11 已经证明：worker 可以在非终态语义安全点提交 checkpoint，Runtime 可以根据
DB 中的全局执行状态生成 directive，并让另一个 active node 在原 Codex session 中继续和
ACK。

Phase 4G12 进一步证明：worker checkpoint 中的非权威
`responsibility_candidates` 可以驱动受 validator 约束的动态 graph mutation：

```text
worker checkpoint
    -> responsibility candidate
    -> Decision Provider proposal
    -> validated child node
    -> isolated contribution
    -> primary integration
```

但 Phase 4G12 Small 是受控实验。Fixture 明确要求初始 child 在第一轮产生 checkpoint，且
parser prompt 提示了 candidate 的 key、scope 和 integration owner。这证明了机制可行，尚未
证明普通任务中的自然发现，也带来了额外 resume 和 Decision Provider 调用。

Phase 4G13 的重点不是继续增加 graph 操作，而是建立事件驱动、低开销的协调路径，并用一个
不泄露拓扑答案的开源自然 Medium 检查 Runtime 是否会在正确时机选择扩图或保持单责任执行。

---

## 2. 目标

Phase 4G13 实现：

1. 普通 child 不再因为“它是 child”而强制首轮 checkpoint；
2. 非终态 checkpoint 只用于真实、会影响其他责任或 graph 的结构事件；
3. terminal receipt 可以正交携带 `responsibility_candidates`；
4. terminal candidate 成为可引用、可消费的 DB 事实，但不直接创建 node；
5. 动态 child 没有新跨节点影响时可以直接完成并冻结 contribution；
6. dependency、readiness、terminal ingest、directive ACK 等确定性状态由本地 reducer 推进；
7. 只有仍需结构判断时才调用 Decision Provider；
8. 记录无效 resume、有效 decision 比例和 coordination token overhead；
9. 用同一自然 Medium 对比 coherent single worker 与 Runtime orchestra。

核心路径：

```text
ordinary execution
    -> local reducer
    -> continue / terminal

real structural event
    -> checkpoint or terminal candidate
    -> global snapshot
    -> Decision Provider
    -> validated expansion or explicit no-expansion resolution
```

---

## 3. 非目标

Phase 4G13 不实现：

- mid-turn asynchronous prompt injection；
- worker peer-to-peer communication；
- worker 直接创建 durable node；
- 每个 milestone 都调用 Decision Provider；
- 以 node 数量作为成功指标；
- 普通开发任务默认创建独立 evaluator；
- evaluator 失败后的多轮 worker 修复；
- 用 Hard benchmark 验证本阶段基础机制；
- 证明 Runtime orchestra 必然优于 native communicating subagents。

本阶段允许自然 Medium 最终不扩图。若 evidence 表明一个 coherent worker 更合适，Provider
选择不扩图是正确结果。

---

## 4. 事件分类

### 4.1 普通局部进展

以下事实默认留在 worker session、task event 或 terminal receipt，不触发结构决策：

- repository inspection；
- 普通代码修改；
- 局部测试通过或失败；
- 同一责任内的 debug；
- 不影响其他 node 的 milestone；
- 已知 dependency 完成；
- child 完成其既定 contribution。

### 4.2 真实结构事件

只有满足下列至少一个条件时，非终态 worker 才应提交 coordination checkpoint：

- 新事实使另一个 active node 的 contract、scope 或 next action 失效；
- 发现 active write scope 冲突；
- 发现当前 graph 未覆盖的独立、可恢复责任；
- 发现新的 capability、workspace、human authority 或 independent verification 边界；
- 已完成 partial contribution，且其他 active node 必须在自己继续前消费它；
- integration risk 需要 Runtime 在多个 active responsibility 之间做选择。

单纯“完成了第一轮工作”“任务很复杂”“可能可以并行”不是结构事件。

### 4.3 终态发现

worker 已完成自身责任时，不应为了上报新 gap 再进入一次非终态 checkpoint 和 resume。它可以在
`runtime_worker_receipt_v1` 中同时返回：

```json
{
  "verdict": "succeeded",
  "responsibility_candidates": [
    {
      "candidate_key": "legacy-event-adapter",
      "outcome": "补齐旧事件格式适配",
      "reason_type": "execution_discovered_gap",
      "acceptance_criteria": ["旧事件可以进入新版 consumer"],
      "declared_write_scope": ["src/compat/**", "tests/compat/**"],
      "goal_item_keys": ["versioned-events"],
      "integration_owner_node_key": "event-integration",
      "evidence_refs": ["workspace:src/consumer.py"]
    }
  ]
}
```

`verdict` 描述当前 node 的完成状态；candidate 描述 graph 可能遗漏的其他责任。两者正交。

---

## 5. Worker 输出协议

### 5.1 普通 terminal receipt

`runtime_worker_receipt_v1` 增加必填但通常为空的：

```json
"responsibility_candidates": []
```

Closed-loop Codex lane 的 structured-output transport 使用
`runtime_worker_event_v1` envelope，其 `event` 在 terminal receipt 与 coordination checkpoint
之间二选一。Wrapper 只解决单次 materialization 的输出 schema 选择；Runtime ingest 后仍保存
canonical `runtime_worker_receipt_v1` 或 `runtime_worker_coordination_checkpoint_v1`，wrapper
本身不是新的 DB 事实类型。

普通 child 完成时直接返回 terminal receipt。Runtime 不再通过
`non_authoritative_contribution` 或 child 身份自动要求其第一轮输出 coordination checkpoint。

### 5.2 Nonterminal coordination checkpoint

`runtime_worker_coordination_checkpoint_v1` 保留，但必须满足 structural relevance：

- 至少一个 finding 影响其他现有 active node；或
- 至少一个有效 `responsibility_candidate`；或
- checkpoint 消费了已交付 directive，并产生新的跨节点事实。

只包含本 node 局部进展的 checkpoint 无效。Worker 应继续执行，最终返回 receipt。

### 5.3 Terminal candidate validation

terminal candidate 复用 Phase 4G12 的 candidate schema 和边界：

- candidate key 唯一；
- goal linkage 存在；
- acceptance criteria 非空；
- declared write scope 规范化且非空；
- integration owner 存在且仍为非终态；
- evidence refs 可由 Runtime 验证；
- candidate 不能扩大 capability；
- worker 不能声称 candidate 已创建 node。

终态 source node 不能把自己指定为 integration owner。Primary worker 若在自身责任尚未完成时
发现独立 gap，应使用非终态 checkpoint；不能先声称整个责任完成，再要求自己作为 terminal
owner 接收新 dependency。

---

## 6. Candidate 事实生命周期

terminal receipt 中的有效 candidate ingest 后，Runtime 生成独立事件：

```text
worker_responsibility_candidates_recorded
```

每个 candidate 使用稳定引用：

```text
event:<event_id>#responsibility:<candidate_key>
```

Candidate 状态由 DB 事实推导：

```text
pending
    -> expanded
    -> absorbed_by_existing
    -> rejected_not_durable
    -> deferred
```

其中：

- `expanded`：accepted patch 创建了引用该 candidate 的 child；
- 其他状态：accepted patch 使用 `resolve_responsibility_candidate` 明确记录不扩图结论；
- `deferred` 只允许在当前仍有 active coverage 且不会造成 goal gap 无 owner 时使用。

同一个 pending candidate 只能消费一次。Decision Provider patch 必须对 decision delta 中每个
pending candidate 做出 `create_node` 或 `resolve_responsibility_candidate` 之一，不能静默忽略。

---

## 7. Integration Owner Hold

若 terminal candidate 指向一个 `waiting_dependency` integration owner，candidate 未决期间本地
reducer 不得把 owner 提升为 `ready`。原因是 Provider 可能需要先增加新的 child dependency；若
owner 已 materialize，动态扩图会产生移动中的集成边界。

Hold 只由 pending terminal candidate 导致，并且必须可观测：

```text
integration_owner_held_for_responsibility_candidate
```

Candidate 被 expanded 或明确 resolution 后，reducer 重新计算 dependencies：

- expanded：等待新 child；
- absorbed/rejected/deferred：现有 dependencies 满足时直接变为 ready；
- 不再额外调用 Decision Provider。

---

## 8. Decision Provider 调用边界

以下状态由本地 reducer 直接处理，不调用 Provider：

- terminal receipt 没有 candidate；
- child contribution 冻结；
- dependencies 已满足；
- existing dependent node 变为 ready；
- goal 已满足；
- directive 已 ACK 且没有新结构事实；
- evaluator policy、capability policy 或 human gate 已有确定性处理路径；
- malformed candidate 被 receipt validator 拒绝。

只有以下情况调用 Provider：

- 存在未消费 terminal responsibility candidate；
- 存在有效 nonterminal coordination checkpoint；
- 普通 local reduction 后仍存在没有 active coverage 的 goal gap；
- 既有 anti-stuck / strategy update 规则要求结构判断。

`worker terminal receipt` 本身不是 Provider trigger。

---

## 9. Patch Contract

Phase 4G13 增加：

```json
{
  "op": "resolve_responsibility_candidate",
  "source_responsibility_ref": "event:123#responsibility:legacy-event-adapter",
  "resolution": "absorbed_by_existing",
  "existing_node_key": "event-integration",
  "rationale": "现有 integration owner 的 contract 和 write scope 已覆盖该 gap",
  "evidence_refs": ["event:123"]
}
```

允许的 `resolution`：

- `absorbed_by_existing`；
- `rejected_not_durable`；
- `deferred`。

Validator 必须检查：

- source ref 指向当前 job 的 pending candidate；
- resolution 合法；
- `absorbed_by_existing` 引用真实非终态 node，且其 contract/goal linkage 能覆盖 candidate；
- `deferred` 不会留下无 owner 的 required goal gap；
- evidence refs 包含 candidate event；
- create 与 resolve 不得重复消费同一个 candidate。

Provider 不得直接写 candidate 状态；只有 accepted patch 的本地 apply 路径写 resolution event。

---

## 10. Coordination Cost Metrics

Observability 至少增加：

```text
coordination_checkpoint_count
coordination_resume_count
coordination_resume_without_new_evidence_count
structure_assessment_count
receipt_invalid_count
receipt_recovery_retry_count
context_reacquisition_count
invalid_resume_count
structural_decision_count
effective_structural_decision_count
effective_structural_decision_ratio
terminal_candidate_count
candidate_expanded_count
candidate_resolved_without_expansion_count
coordination_provider_input_tokens
coordination_provider_output_tokens
coordination_worker_input_tokens
coordination_worker_cached_input_tokens
coordination_worker_output_tokens
coordination_token_overhead
```

定义：

- `coordination_resume_without_new_evidence`：因 coordination directive 恢复的 worker turn 只做
  ACK，没有新增 changed files、finding、candidate、verification 或 contribution evidence；
- `invalid_resume_count`：receipt recovery 尝试复用已终止 session，最终只能
  `fallback_fresh` 的次数；它与正常的 same-session resume 分开记录；
- `receipt_invalid_count`、`receipt_recovery_retry_count` 和
  `context_reacquisition_count`：分别记录协议无效终态、因此产生的恢复轮次和丢失原 session
  后重新获取上下文的次数；
- `effective_structural_decision`：accepted decision 至少产生 graph mutation、contract/scope
  change、被后续 ACK 的 directive，或消费 terminal candidate；
- `coordination_token_overhead`：可归因于 checkpoint、结构 decision 和 directive resume 的
  provider/worker token 总和；reported usage 与 estimated usage 分开记录，不能混成精确值。

这些指标用于比较成本，不参与 runtime correctness 或 goal completion。

---

## 11. 开源自然 Medium 选择

优先使用公开的 `FeatureBench`。该 benchmark 面向真实开源仓库中的 feature-level 开发，提供：

- 固定 repository 与 base commit；
- 正常业务 problem statement；
- 与 worker 隔离的 test patch / acceptance suite；
- Docker image 与可复现 evaluator；
- `lite`、`fast`、`full` split。

Phase 4G13 不直接使用 gold patch，不把 test patch 注入 worker context，也不根据 gold patch 生成
candidate key、write scope 或 node contract。

实例筛选条件：

1. CPU 环境可运行，不需要 GPU 或外部付费服务；
2. gold/reference change 涉及约 5--15 个文件；
3. 至少存在一个共享 integration surface 和两个可区分的实现责任；
4. repository 安装与 acceptance suite 在本机资源预算内；
5. base 失败、reference/gold 成功的 qualification 可复现；
6. problem statement 不直接给出 runtime node 划分答案；
7. 未被本分支此前真实运行使用。

FeatureBench fast split 的
`fastapi__fastapi.02e108d1.test_compat.71e8518f.lv1` 曾作为初选候选，但 qualification 发现
其 problem statement 直接给出目标文件、函数名和接口描述。它可以用于编码能力测试，却会把
潜在责任边界提前暴露给 worker，因此不符合本阶段的 natural discovery 要求，不作为主实验。

主实验从 SWE-EVO 中筛选一个此前未运行过的 Medium 实例。SWE-EVO 提供高层 SRS、固定
base/reference revision 和隔离 acceptance oracle，但 SRS 不直接给出文件级实现拓扑。筛选时只用
reference change 和 tests 做离线 qualification 与规模判断；选定后将这些材料隔离，不进入任一 arm
的 worker、Decision Provider、memory、checkpoint 或 runtime context。

当前已经 qualification 并冻结的实例是：

```text
instance: dask__dask_2023.6.1_2023.7.0
base: 85c99bc20abc382774cfb6e5bf5f2db76ac09378
reference changed files: 9
FAIL_TO_PASS: 5
PASS_TO_PASS: 707
image digest: sha256:e0ee1e98546c7599146b341c40503c109b69bddc740802ef8d287b388f8cd29f
```

Qualification 已确认 base 的 FAIL_TO_PASS 失败且 PASS_TO_PASS 全过，gold 的两组测试均全过，
base/gold 环境指纹一致。公开冻结值见
`docs/validation/phase4g13/qualification-manifest.json`；gold patch、test patch 和 official test ids
仍只保留在受保护运行目录，不提交到 worker 可见输入。

若没有 SWE-EVO 实例同时满足上述条件，则本阶段应明确记录 qualification blocked，并更换公开
benchmark；不得删改 SRS、根据 reference patch 编造 candidate，或为了观察扩图而人为增加责任边界。

---

## 12. 双臂验证协议

### 12.1 Arm A：Coherent Single Worker

- 一个 fresh Codex worker session；
- 一个对完整结果负责的 node；
- 不启用 Runtime dynamic expansion；
- worker 自己检查、实现、测试和 debug；
- 不运行 evaluator feedback loop。

### 12.2 Arm B：Runtime Orchestra

- 相同 base commit、problem statement、模型、reasoning effort 和 tool capability；
- 初始只创建一个 coherent primary node；
- 不提供 candidate key、write scope、文件划分或 latent gap；
- 开启 Phase 4G13 natural coordination；
- worker 可以自然 checkpoint 或 terminal candidate；
- Provider 可以扩图，也可以明确选择不扩图；
- durable child 使用隔离 worktree，primary 负责最终 integration。

两个 arm 默认不要求 worker 使用 internal subagents，以便比较 coherent session 与
Runtime-level durable orchestration，而不是混合两种 orchestra。

### 12.3 Acceptance

两个 arm 结束后各对固定 candidate revision 运行一次隔离 acceptance suite：

- evaluator 不向 worker 回流失败；
- Arm A 直接在 worker session 结束后运行隔离 evaluator process；
- Arm B 可以复用正常 evaluator task/run/receipt lane 固定 revision 和 provenance，但只运行一次，
  不把 evaluator node 计入 orchestra worker 数，也不允许启动 remediation worker；
- 只用于最终任务质量评分；
- 结果可以成为 terminal acceptance fact，但不能成为新的结构扩展输入。

---

## 13. 判定方式

Phase 4G13 分开报告：

### Runtime Correctness

- candidate provenance 可追踪；
- pending candidate 恰好消费一次；
- expansion/no-expansion 都由 accepted patch 留痕；
- owner hold 与 dependency readiness 正确；
- 无重复 ledger、materialization 或 terminal fact；
- consistency 为 0 violation。

### Natural Discovery

- prompt 未泄露 node 拆分答案；
- worker 是否自然发现 durable gap；
- Provider 是否依据 evidence 做出合法结构选择；
- 未发生扩图时是否有明确、合理的不扩图结论。

### Task Capability

- acceptance passed / total；
- regressions；
- changed files；
- unresolved requirements。

### Orchestra Value

- wall time；
- total/cached input tokens；
- output tokens；
- handoff 与 resume 数；
- coordination token overhead；
- 与 Arm A 相比，质量、成本和恢复能力是否有实际变化。

不允许只凭“创建了更多 node”或“流程更可审计”声称 orchestra 有正价值。

---

## 14. 实施顺序

1. 扩展 receipt schema 和 candidate validator；
2. 持久化 terminal candidate event 与 pending/consumed lifecycle；
3. 增加 integration owner hold；
4. 移除 child-first-checkpoint 隐式规则；
5. 收紧 nonterminal checkpoint structural relevance；
6. 增加 `resolve_responsibility_candidate` patch op；
7. 收缩 Decision Provider trigger；
8. 增加 coordination cost observability；
9. 完成 deterministic tests；
10. qualification 并冻结 FeatureBench Medium；
11. 运行 Arm A / Arm B；
12. 归档原始 session、DB、events、workspace revision 和中文报告。

---

## 15. 验收标准

Phase 4G13 完成必须满足：

- 普通 dynamic child 首轮可以直接 terminal；
- terminal receipt 可以携带并持久化有效 candidate；
- invalid candidate 不能触发 graph mutation；
- terminal candidate 未决时 integration owner 不会提前 materialize；
- 每个 pending candidate 被 expanded 或明确 no-expansion resolution 恰好一次；
- 无结构事件时 local reducer 不调用 Decision Provider；
- coordination checkpoint 不能只包含普通局部进展；
- observability 包含本阶段成本指标；
- focused Runtime tests 通过；
- 自然 Medium 双臂均从相同冻结输入运行；
- acceptance 只运行一次且不回流；
- 中文报告同时给出 correctness、capability 和 orchestra value 结论；
- raw artifact manifest 校验通过。

Phase 4G13 最终要回答的不是“Runtime 能否制造更多 agent”，而是：

```text
当执行证据真的暴露新的责任边界时，
Runtime 能否及时改变全局结构；
当没有结构边界时，
Runtime 能否保持安静并避免额外成本。
```

---

## 16. 实际验证状态

2026-07-19 已完成冻结 Dask Medium 的双臂真实运行：

- [自然 Medium 执行报告](validation/phase4g13/natural-medium-execution-report.md)；
- [双臂对照与架构结论](validation/phase4g13/natural-medium-comparison.md)；
- [机器可读对照](validation/phase4g13/comparison-report.json)。

实测确认 early structure assessment、evidence-backed graph expansion、三个 isolated child
并行执行和全局 recovery 均真实发生；但 event-driven contribution receipt 协议缺陷导致 child
成果未进入最终 candidate，Runtime 最终退化为 full-workspace recovery worker 重做。两臂
official 结果均为 F2P `3/5`、P2P `707/707`，Runtime wall time 为 single worker 的
`2.96x`。

因此本阶段状态为：

- 事件驱动动态 orchestra：已实现并实测；
- 低开销 contribution handoff：首次实测失败，prompt/schema 修正已完成；
- 相比 coherent single worker 的净正价值：未证明；
- 下一门槛：先以轻量真实 contribution case 验证零 invalid resume 和真实 primary
  integration；该修复由 [Phase 4G14 Durable Contribution Handoff](kanban-runtime-kernel-phase4g14.md)
  规范。完成轻量 handoff 验证前不运行自然 Medium clean replay。
