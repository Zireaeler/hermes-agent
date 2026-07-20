# Hermes Kanban Runtime Kernel Phase 4G16

# Natural Orchestra Calibration

## 1. 背景

Phase 4G11-4G15 已经依次建立：

- 非终态 coordination checkpoint；
- 全局 execution snapshot；
- DB-backed directive、same-session resume 和 ACK；
- evidence-driven graph mutation；
- event-driven checkpoint 与终态 responsibility candidate；
- durable contribution handoff；
- active Codex turn live steer；
- 强制 orchestration learning bundle、registry 和 archive gate。

这些能力分别通过 deterministic test、受控 Small、Natural Medium 和 live transport 对照得到验证。
但是，当前生产路径仍有一个重要缺口：worker prompt 虽然允许自然提交 checkpoint，checkpoint 一旦入库，
本地 reducer 就把 job 统一置为 `waiting_decision`。Runtime 尚未把以下情况确定性区分：

```text
局部事实，不影响其他责任
    -> 不需要 coordination

影响一个已存在责任，但不改变 topology / scope / capability
    -> 本地 context directive

发现新的 durable responsibility，或需要改变 owner / scope / capability
    -> Decision Provider
```

Phase 4G15 controlled live harness 又直接构造了 source checkpoint、affected target 和 directive，证明了
live transport 与 learning lifecycle，却没有证明普通生产路径能够从真实 worker checkpoint 自然选择
`no-op`、`local directive` 或 `provider-required`。因此它不能单独回答：Runtime 是否在自然任务中减少了
错误工作和重做，以及这种收益是否超过 coordination 成本。

Phase 4G16 建立 Natural Orchestra Calibration。它不增加新的 agent 角色，而是把已有能力接入一条
可审计的生产路由，并用三类成对任务持续校准系统：

```text
真实 worker evidence
    -> canonical checkpoint ingest
    -> deterministic action classification
    -> no-op / local context route / Decision Provider
    -> live or durable delivery
    -> terminal quality
    -> learning finding
    -> absorbed regression
```

---

## 2. 目标

Phase 4G16 必须实现：

1. worker 在普通 production materialization 中自主决定是否提交 structural checkpoint；
2. validation harness 不得直接插入 checkpoint、directive、candidate key 或拆分答案；
3. checkpoint 入库后先产生一个不可变、幂等的 coordination action audit；
4. 本地 reducer 能区分 `resume_source_only`、`local_context_route` 和 `provider_required`；
5. topology、owner、scope、capability 和新 durable responsibility 不明确时才调用 Decision Provider；
6. topology 不变的显式 cross-node finding 由本地 reducer 生成最小 `continue` directive；
7. target 为 active app-server turn 时尝试 live delivery，否则保留 durable queue；
8. 普通 terminal receipt、dependency readiness 和 source-only progress 不调用 Decision Provider；
9. learning analyzer 能识别误协调、错过协调、无效协调和 coordination overhead；
10. 每个校准 run 都经过 marker、bundle、registry absorption、archive 和 cleanup gate；
11. 使用 coherent negative control、shared-contract Medium、durable-boundary Medium 三类冻结任务；
12. 每类任务比较 coherent single worker baseline 与 Runtime treatment；
13. Runtime treatment 的最终仓库测试质量不得低于对应 baseline。

---

## 3. 非目标

Phase 4G16 不实现：

- worker peer-to-peer 自由通信；
- worker 直接创建、删除或修改 durable node；
- 根据普通 progress、heartbeat、tool call 或局部测试自动协调；
- 本地 reducer 根据自然语言猜测新的 contract、scope 或 ownership；
- 本地 reducer 自动选择 `stop_obsolete_work`、`narrow_scope` 或 `revise_contract`；
- 自动 promotion provider profile、validator、memory 或 capability policy；
- Dashboard；
- Hard/Large benchmark；
- evaluator node 或多轮 evaluator -> worker 刷分；
- 为了覆盖分支而伪造 worker 失败、candidate 或 checkpoint。

仓库内冻结测试可以作为最终质量 oracle，但不创建独立 evaluator worker。Phase 4G16 校准的是 Runtime
编排选择与成本，不重新验证 Phase 4G8 的独立 evaluator completion policy。

---

## 4. Canonical Production Path

### 4.1 Worker 责任

closed-loop worker 仍然拥有完整 node outcome。默认路径是持续执行到 terminal receipt。只有出现带
repository evidence 的跨节点结构事实时，才提交
`runtime_worker_coordination_checkpoint_v1`：

```text
inspect -> implement -> test -> debug -> terminal receipt
                         |
                         +-> concrete cross-node structural fact
                                -> coordination checkpoint
```

Prompt 不要求 child 首轮 checkpoint，不给 expected finding，不列 candidate key，也不告诉 worker 应该
拆哪个文件。普通局部 milestone、一次测试失败和本 node 内部设计变化都不属于 structural event。

Worker 只负责报告：

- `kind`；
- `summary`；
- `findings[].affected_node_keys`；
- `findings[].evidence_refs`；
- 可选的 non-authoritative `responsibility_candidates`。

Worker 不选择 `live`、`durable`、`provider_required` 或 graph op。

### 4.2 Runtime 路由

Checkpoint canonical ingest 后，Runtime 在同一事务中：

1. 保存 `worker_coordination_checkpointed`；
2. 解析 source、affected nodes、finding kinds、candidate 和当前 graph facts；
3. 创建 `runtime_coordination_actions` 行；
4. 记录 `runtime_coordination_action_classified`；
5. 根据 classification 执行本地动作，或将 job 置为 `waiting_decision`；
6. 动作完成后记录 resolution，不允许同一 checkpoint 重复路由。

Decision Provider 只能消费 action audit 指向的 unresolved structural evidence，不能绕过 action classifier
重新解释普通 worker progress。

---

## 5. Coordination Action Audit

新增表：

```text
runtime_coordination_actions
    id
    job_id
    source_node_id
    source_checkpoint_event_id UNIQUE
    classification
    route
    status
    affected_node_keys_json
    finding_keys_json
    candidate_refs_json
    rationale_json
    directive_ids_json
    decision_id
    graph_revision_before
    graph_revision_after
    created_at
    resolved_at
```

`classification`：

```text
source_only
existing_responsibility_context
durable_structure_unknown
stale_or_unrouteable
```

`route`：

```text
resume_source_only
local_context_route
provider_required
no_action
```

`status`：

```text
classified -> applied
classified -> waiting_provider -> applied
classified -> waiting_provider -> rejected
classified -> no_action
```

该表是 action lifecycle 的 authoritative audit，不是 goal correctness 的事实源。Graph、directive、receipt、
ledger 和 completion 仍由既有表负责。

每个 checkpoint 最多对应一行。Supervisor restart、重复 receipt ingest、Provider retry 和 archive replay
不得重复创建 action 或 directive。

---

## 6. Deterministic Classification

### 6.1 `resume_source_only`

以下情况不需要全局决策：

- finding 最终只影响 source node；
- 显式 affected sibling 在 action 分类前已经 terminal，且没有 candidate 或未满足 goal gap；
- checkpoint 来自旧 fixture 强制 safe point，但没有真实跨节点变化。

自然 worker 提交纯 source-only checkpoint 仍应被 receipt validator 拒绝；本 route 主要保证旧数据、竞态和
恢复路径不会无意义调用 Provider。Runtime 只恢复 source session，并记录 `no_action_reason`。

### 6.2 `local_context_route`

同时满足以下条件时，本地 reducer 可以路由：

1. 没有 responsibility candidate；
2. affected node 全部已经存在于当前 job；
3. 不创建、删除、替换 node 或 dependency；
4. 不改变 goal linkage、capability、workspace owner 或 write scope；
5. 不需要 human authority 或 independent verification；
6. source evidence 可由 Runtime 验证；
7. target 为 `planned`、`waiting_dependency`、`ready`、`running` 或
   `waiting_coordination`。

MVP 本地动作固定为 `continue` context directive。Instruction 只包含 source checkpoint 的结构化 summary、
finding summary 和 evidence refs，要求 target 在自身 contract 内重新检查当前假设；不能擅自告诉 target
停止工作、扩大范围或采用某个实现方案。

Target 状态决定 transport：

```text
ready
    -> queued，在首次 materialization 时进入 context

planned / waiting_dependency
    -> queued，dependency 满足后的首次 materialization 进入 context

running + active app-server turn
    -> queued + live delivery attempt

running + non-steerable transport
    -> queued，等待 target 的下一个 safe point / recovery boundary

waiting_coordination
    -> directive 激活并恢复原 session
```

Source 自身若处于 `waiting_coordination`，Runtime 同时生成一个 source resume directive。该 directive 只说明
cross-node fact 已被路由，source 继续原 contract；它不需要 Provider 生成自然语言策略。

### 6.3 `provider_required`

以下任一条件成立时必须进入 Decision Provider：

- 存在有效 `responsibility_candidates`；
- 需要创建、删除、替换、拆分或合并 durable responsibility；
- 需要新增或修改 dependency；
- 需要改变 write scope、workspace ownership 或 integration owner；
- 需要 capability、credential、human authority 或 independent verification 边界；
- affected node 无法从当前 graph facts 唯一解析；
- 多个 finding 对同一责任提出互相冲突的结构影响；
- 本地 route 无法在保持 contract 不变的条件下继续。

Provider 输出仍受 Phase 4G12/4G13 graph patch validator 约束。Action audit 必须记录 decision ID、patch
结果和 graph revision；Provider 无输出、parse failure 或 validator rejection 不得被记为 applied。

### 6.4 `no_action`

若 checkpoint 在 ingest 后因竞态已经失去 target，且 source 也已 terminal，不创建 directive，不调用
Provider。Runtime 记录 stale evidence 和原因；不能删除原 checkpoint。

---

## 7. Decision Trigger 收缩

`worker_coordination_checkpointed` 本身不再等于 `decision_requested`。

正确顺序：

```text
checkpoint ingest
    -> local action classification
    -> local route 可完成？
       -> 是：应用 directive / resume，继续 reducer
       -> 否：action=waiting_provider，job=waiting_decision
```

以下状态变化不调用 Provider：

- terminal receipt ingest 后已有 dependency 变为 ready；
- integration owner 可由本地 reducer 直接 materialize；
- source-only checkpoint 恢复；
- existing-responsibility context route 已成功排队；
- directive delivery / ACK；
- goal 已满足；
- 普通 child contribution capture / promotion；
- archive、learning absorption 和 cleanup。

Decision delta 增加 `pending_coordination_actions`，只投影 `route=provider_required` 且
`status=waiting_provider` 的 action。历史 checkpoint 可用于 provenance，但不能使已解决 action 再触发决策。

---

## 8. Learning Findings

Phase 4G15 analyzer 增加四类 deterministic finding。它们必须引用 DB event/action/directive、worker
receipt、quality result 和 baseline bundle，不能只根据报告作者判断。

### 8.1 `false_coordination`

满足以下事实之一：

- source-only/local progress 被路由给 sibling；
- route 违反 classifier 规则；
- treatment 增加 checkpoint/resume/decision，但没有产生 contract consumption、stale work reduction、
  handoff preservation 或质量改善；
- coherent negative control 出现任何 provider-required action 或新增 durable node。

单次 directive 没有提高测试分数不自动等于 false coordination；必须同时证明它没有改变 target 的有效
执行事实且只增加成本。

### 8.2 `missed_coordination`

满足以下事实：

- 一个 worker terminal receipt 或 contribution 已证明 shared contract/assumption 改变；
- sibling 在该事实之后仍基于旧 revision 产生需要废弃或重做的 artifact；
- 该事实之前没有 checkpoint/action/directive；
- baseline/treatment manifest 可以对齐同一责任和 quality oracle。

若没有足够 lineage 证明 sibling 使用旧事实，字段必须为 `unknown`，不能推断为 0。

仅仅因为 candidate-required 校准任务没有产生 candidate，不得标记为 `missed_coordination`。如果
structure assessment 的 repository evidence 明确支持 coherent single worker，且没有 sibling、旧 revision
artifact 或重做 lineage，则应记录 `calibration_fixture_gap`。该 finding 的改进范围是
`validation_campaign`，不能生成修改 reducer、transport 或 worker policy 的候选。

### 8.3 `ineffective_coordination`

满足以下事实之一：

- directive 在 target terminal 前未 accepted/ACK，且 target 产生了 stale work；
- target ACK 了 directive，但 receipt/contribution 没有引用或消费对应 revision；
- provider-required action 被拒绝后没有恢复路径，导致 responsibility 无 owner；
- local route 重复触发同一 checkpoint 或产生重复 directive。

### 8.4 `coordination_overhead`

每个 paired run 至少记录：

```text
checkpoint_count
action_count_by_route
provider_decision_count_caused_by_coordination
directive_count / live_attempt / accepted / acknowledged
source_resume_count
target_context_reacquisition_count
coordination_wait_wall_time
worker_input / cached_input / output tokens
orchestration_token_overhead
stale_work_avoided / stale_work_not_avoided
final_quality_delta_vs_baseline
wall_time_delta_vs_baseline
```

无法从 provider/worker evidence 分离 token 时必须报告 `unknown`，不能使用混合总量冒充 coordination
token。

---

## 9. Natural Calibration Campaign

### 9.1 共同约束

每类任务运行两个 arm：

```text
baseline:
    one coherent worker
    same repository / goal / model class / capability envelope
    no Runtime graph expansion or cross-node directive

treatment:
    normal production initialization
    event-driven checkpoint policy
    local action classifier + Decision Provider
    live steer only when naturally eligible
```

共同要求：

- prompt 只包含真实目标、repository facts 和 node contract；
- 不预告 candidate key、目标 node key、应拆文件、预期 checkpoint 或 expected route；
- 不由 harness 插入 checkpoint、directive 或 graph patch；
- baseline 与 treatment 使用相同冻结仓库 revision；
- 每个 arm 只运行 worker 自己的实现/调试 loop 和一次最终 repository oracle；
- 不创建 evaluator worker，不通过 evaluator feedback 反复刷分；
- worker task quality failure 与 Runtime correctness failure 分开报告；
- treatment 最终 oracle 结果不得低于 baseline；
- 原始 session、DB、events、patch、workspace revision、tests 和 token facts 进入稳定 archive。

### 9.2 Case A: Coherent Negative Control

任务应是一个明确单 responsibility 的小型 brownfield 修改，所有修改共享同一模块、同一 feedback loop 和
同一验收边界。

预期：

- baseline 和 treatment 都由一个 coherent worker 完成；
- treatment 不产生 cross-node checkpoint、Provider coordination decision 或额外 child；
- 若 worker 产生 local-only checkpoint，validator 拒绝并要求继续，不扩图；
- 最终测试一致；
- treatment 的 orchestration overhead 接近 0。

该 case 用于证明系统不会为了展示 orchestra 而制造 orchestra。

### 9.3 Case B: Shared-Contract Medium

任务包含两个可能并行但共享明确 contract 的责任域。Repository 本身提供 contract consumer、fixture 和
回归测试，但 prompt 不告诉 worker 哪个模块先改变、何时 checkpoint 或应该通知谁。

预期观察：

- early assessment 可以选择 coherent single 或 evidence-backed split；
- 若形成多个 active responsibilities，任一 worker 发现 shared contract 变化时应自然提交 checkpoint；
- action classifier 应选择 `local_context_route`，而不是无条件调用 Provider；
- target 在 terminal 前消费新事实时记录 stale work avoided；
- 若模型未提交 checkpoint 且产生重做，记录 `missed_coordination`；
- 若 Provider 正确选择不扩图，同样是合法结果；节点更多不是成功条件。

### 9.4 Case C: Durable-Boundary Medium

任务包含一个只有执行中读取 repository evidence 后才能确认的独立责任，例如隔离兼容层、不同 capability
边界或明确不重叠的可恢复 artifact。Prompt 不暴露 candidate key 或拆分答案。

预期观察：

- worker 可以在 nonterminal checkpoint 或 terminal receipt 中报告 candidate；
- classifier 选择 `provider_required`；
- Provider 可以 expansion，也可以基于现有 owner 明确 `absorbed_by_existing` /
  `rejected_not_durable`；
- 若 expansion，必须有 source candidate ref、write scope、goal linkage、dependency 和 integration owner；
- 若不 expansion，必须有显式 resolution；
- 最终质量不得低于 coherent baseline。

该 case 验证动态 graph 选择，而不是要求系统一定增加 node。

---

## 10. Campaign Validity

每个 arm 的结果分类：

```text
runtime_valid_quality_passed
runtime_valid_task_failed
runtime_invalid
infrastructure_invalid
```

Phase 4G16 完成要求：

1. 三类 baseline/treatment 都有 manifest-verified archive；
2. 三个 treatment 的 Runtime consistency 全部通过；
3. Case A 不发生无依据 orchestra；
4. Case B 至少产生一个自然 checkpoint，或以 evidence 证明 coherent route 确实更合理；
5. Case C 至少产生一个自然 candidate，并由 Provider 明确消费；
6. 所有 local route 都有 action audit 和幂等 directive lineage；
7. 所有 provider route 都有 decision/patch resolution；
8. treatment 最终 repository oracle 不低于对应 baseline；
9. 每个 run 都有中文过程报告和 learning absorption receipt；
10. cleanup 只在 archive manifest 与 learning gate 都通过后执行。

若 Case B/C 的真实 worker 没有自然产生所需 evidence，不得由 harness 补造。该 arm 标记
`runtime_valid_task_failed` 或 `infrastructure_invalid`，分析 worker contract、任务可观测性和模型行为后再
修正系统或冻结任务。不得只为通过验收直接写 DB event。

首次 Durable-Boundary Medium 使用单一 v1 adapter 小仓库时，真实 worker 给出了
`continue_single_node`：核心 schema 与唯一 adapter 共享同一小型语义契约，拆分成本高于收益。该 run 的
任务质量、Runtime consistency 和归档均通过，但没有产生 candidate。此结果证明原 fixture 不足，不能反推
Runtime 漏协调。修订后的 fixture 必须同时包含 core schema、legacy transform 与 audit batch consumer 三个
可独立验收面；目标文本仍不得给出 node key、文件路径或预期 graph patch。

第二次修订 run 同样没有产生 candidate。真实 worker 观察到三个行为面，但 repository 只有约 150 行，
legacy 与 audit 又共同依赖尚未冻结的 v2 核心合同，因此仍选择 `continue_single_node`。Baseline 17/17、
treatment 15/15、质量非回退和 Runtime consistency 全部通过；失败仅限 Case C 的自然 candidate 验收。
该结果继续归类为 `calibration_fixture_gap`，同时暴露出后续设计问题：二元的“立即扩图/继续单节点”无法表达
“先由 Primary 冻结共享合同，再根据 milestone evidence 激活低耦合 child”。Phase 4G16 不得将本 run
写成正向 orchestra 证明。

扩大后的第三次 run 已自然返回 `defer_until_milestone` 和两个候选责任，但 checkpoint 的
`milestone_contract.artifact_scope` 与 `shared_integration_scope` 不一致。该 metadata 错误后的通用 receipt
recovery 同时要求 terminal receipt 与 nonterminal structure checkpoint，且没有回传精确错误，最终丢失原
candidate 并触发 full-workspace strategy worker 重做。该 run 必须归类为
`coordination_protocol_failure`，而不是 `calibration_fixture_gap`。正式重跑前必须支持：原 checkpoint 与字段
错误回流同一 session、metadata-only correction、workspace identity 不变校验，以及 repair attempt 进入原
checkpoint reducer。

---

## 11. Deferred Decomposition

### 11.1 为什么需要第三种结构结论

Early structure assessment 不能继续只返回：

```text
continue_single_node
expand
```

当潜在 child 的写范围已经相对独立，但它们都依赖 Primary 尚未稳定的共享合同，立即扩图会让 child 基于
猜测并行，继续单节点则会丢失后续安全并行的机会。Phase 4G16 增加第三种结论：

```text
defer_until_milestone
```

它表达的不是“任务很复杂，稍后再拆”，而是以下可验证状态：

1. worker 已从 repository evidence 识别出两个或三个低耦合候选责任；
2. 候选责任当前被一个明确、尚未完成的共享合同阻塞；
3. Primary 仍负责建立、测试和集成该共享合同；
4. 合同形成不可变 milestone artifact 后，Runtime 才允许 Provider 决定是否创建 child。

### 11.2 Structure checkpoint contract

`runtime_worker_structure_checkpoint_v1` 的 `recommendation` 增加
`defer_until_milestone`。该结论必须同时给出：

- 两个或三个 `proposed_nodes`；
- 唯一 `milestone_contract`；
- 现有 Primary 作为 `integration_owner_node_key`；
- 候选之间不重叠的 `declared_write_scope`；
- Primary 独占的 `shared_integration_scope`。

`milestone_contract` 至少包含：

```json
{
  "milestone_key": "stable-shared-contract",
  "summary": "冻结后可供独立责任消费的共享合同",
  "artifact_scope": ["src/shared/**", "tests/shared/**"],
  "verification_criteria": ["共享合同测试通过"]
}
```

Runtime 验证该 checkpoint 后必须本地完成以下动作，不调用 Decision Provider：

1. 原样保存 worker checkpoint；
2. 把 deferred candidates 和 milestone contract 写入 Primary 的 DB metadata；
3. 记录 `structure_checkpoint_deferred_applied`；
4. 将同一个 Primary node 置回 `ready`；
5. 通过原 backend session 继续执行。

此时 graph topology、owner、scope 和 capability 都没有发生变化，因此 Provider 没有结构决策可做。

### 11.3 Milestone activation

Primary 后续只有在自己实际建立并验证共享合同时，才能提交
`runtime_worker_coordination_checkpoint_v1`，其中：

- `kind=milestone_completed`；
- `milestone_key` 必须精确匹配 deferred contract；
- `changed_files` 必须包含可验证的 milestone artifact scope；
- finding 必须引用真实 workspace evidence；
- `worker_session_should_resume=true`。

收到 checkpoint 后，Runtime 不直接相信“合同已冻结”的自然语言，而是：

1. 相对 job 的原始 base revision 捕获当前 Primary workspace binary patch；
2. 保存 hash-verified `runtime_milestone_seed_v1` artifact；
3. 在 Runtime 自有临时 worktree 中应用 patch 并生成 detached seed commit；
4. 将 commit 固定到 job 专用 `refs/hermes-runtime/...` ref；
5. 把 milestone artifact、seed revision、原 structure checkpoint 和当前 checkpoint 组成 activation evidence；
6. 将 deferred candidates 投影为 active responsibility candidates；
7. 生成一个幂等 `provider_required` coordination action。

Runtime-owned seed commit 不改变用户 branch，不要求 worker 获得 `git_write`，也不成为 progress ledger 或
goal completion 事实。它只定义 child 的不可变输入基线。

### 11.4 Child workspace 与 contribution

Provider 仍只能根据 active candidate 提议受约束 graph patch。若 Provider 选择 expansion：

- child node key、goal linkage、acceptance criteria 和 write scope 不得超出 candidate；
- child 必须指向现有 Primary integration owner；
- child 的 `isolated_worktree` 必须从 milestone seed revision 创建；
- child receipt 和 contribution patch 只包含 seed revision 之后的自身增量；
- Primary workspace 已含 milestone 改动，因此只需集成 child 增量；
- Primary 在 child 完成前转为 `waiting_dependency`，child contribution 就绪后再恢复原 session。

若 Provider 选择不 expansion，必须显式记录 `absorbed_by_existing`、`rejected_not_durable` 或 `deferred`。
不得因为 milestone 已完成就自动创建 node。

### 11.5 Restart、幂等与清理

同一 structure checkpoint、milestone checkpoint 或 materialization 重放时：

- deferred metadata 只写一次；
- milestone artifact、seed ref 和 activation candidate 只产生一份；
- coordination action 只产生一份；
- graph patch 只能消费每个 candidate 一次；
- child materialization 必须恢复同一 seed revision；
- cleanup 在 contribution、learning 和 archive gate 通过前不得删除 seed artifact；
- terminal cleanup 必须删除 Runtime-owned seed ref 和临时 worktree，但保留已归档 artifact manifest。

### 11.6 失败分类

以下属于 Runtime correctness failure：

- milestone patch 未固定就创建 child；
- child 从错误 revision 创建；
- child contribution 重复包含 milestone patch；
- restart 重复激活 candidate 或重复创建 action；
- Primary 未等待 child 就错误完成；
- Provider 未消费 active candidate，而 Runtime 静默继续；
- cleanup 提前删除 seed evidence。

以下不自动属于 Runtime failure：

- worker 合理选择 `continue_single_node`；
- worker 声明 defer 后始终未达到 milestone；
- Provider 基于证据拒绝 expansion；
- child 并行收益不足但最终质量未下降。

后两类结果必须进入 learning finding，用于继续校准任务选择和 orchestration 成本。

---

## 12. 测试层次

### 12.1 Deterministic tests

至少覆盖：

- checkpoint 幂等创建 action；
- source-only 不调用 Provider；
- existing active target 产生本地 `continue` directive；
- ready target 在首次 materialization 消费 queued directive；
- active app-server target 进入 live delivery；
- candidate/scope/capability 变化进入 `provider_required`；
- 已解决 action 不再出现在 decision delta；
- Provider rejection 保留 unresolved evidence；
- restart 不重复 action/directive；
- local dependency/readiness transition 不调用 Provider；
- analyzer 四类 finding 和 `unknown` 语义；
- archive/cleanup 拒绝未吸收 campaign bundle。
- `defer_until_milestone` 不调用 Provider，并恢复同一 Primary session；
- milestone patch 生成 immutable seed artifact 和 Runtime-owned git ref；
- child 从 seed revision 创建，contribution 不重复包含 milestone patch；
- restart 不重复 deferred activation、artifact、action 或 graph consumption；
- 未匹配 milestone key、空 patch、错误 scope 和篡改 artifact 被拒绝。

### 12.2 Controlled production-path smoke

允许使用 deterministic worker adapter 生成 canonical receipt，但必须通过正常 Kanban task/run/evidence
ingest，禁止测试代码直接插 action 或 directive。该 smoke 只验证 route lifecycle，不作为自然模型行为证据。

### 12.3 Real paired campaign

三个 case 使用真实模型 worker。Decision Provider 只在 classifier 选择 `provider_required` 时真实调用；
baseline 不调用 Runtime Provider。报告必须把模型行为、Runtime correctness、任务质量和 orchestra value
分开。

---

## 13. Acceptance Criteria

Phase 4G16 完成时，系统必须能够证明：

- 自然 worker checkpoint 可以通过正常生产路径改变另一个未终态责任；
- 该变化不是 harness 直接插入 directive 的结果；
- 本地可确定的 context routing 不调用 Decision Provider；
- topology/scope/owner/capability 变化仍由 Provider + validator 控制；
- deferred candidate 只有在 hash-verified milestone seed 形成后才可进入 Provider；
- child 以固定 seed revision 为输入，Primary 以原 session 集成 child 增量；
- 同一 checkpoint 只产生一个 action 和一组幂等 directive；
- live delivery、durable fallback、ACK 和最终 consumption 都可追溯；
- 无需 orchestra 的任务不会制造额外 worker 或 decision；
- missed/false/ineffective coordination 和成本都进入 learning bundle；
- 三类 paired task 最终质量不低于 coherent single baseline；
- 每个 run 的过程结论已经被 registry 吸收，而不是只留在聊天或手写总结；
- 所有实现、测试、中文报告和 archive evidence 已提交并推送。

Phase 4G16 不以“创建更多 node”作为成功。它验证的是：

```text
新证据出现后，Runtime 在正确的时间选择正确的结构动作，
减少错误工作或重做，同时不牺牲最终质量。
```
