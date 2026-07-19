# Hermes Kanban Runtime Kernel Phase 4G11

# Runtime Coordination Epochs 与 Active Graph Control

## 1. 背景

Phase 4G10 已经证明 Runtime 能够执行：

```text
一个 primary early assessment
    -> 2 至 3 个 isolated durable children
    -> frozen contributions
    -> 原 primary session 恢复并集成
```

这解决了 Phase 4G8 中“primary 到 terminal plateau 后才创建 fresh strategy worker”的迟到扩图
问题，也证明 durable worktree、contribution provenance、session resume 和 integration owner 可以
在真实任务中工作。

但 Phase 4G10 Arm 2 的执行过程仍然主要是一次静态拆分：

```text
primary assessment
    -> 一次 graph expansion
    -> children 各自运行到 materialization terminal
    -> primary 集成
    -> primary 独自消费后续反馈
```

Child 运行期间发现的共享接口、scope overlap、新 gap 和集成风险不会及时改变其他 active
responsibility。DB 中虽然存在全局 graph 和 node 状态，但这些状态主要用于恢复、readiness、审计和
completion，并未形成持续影响执行结构的闭环。

Phase 4G11 建立一个受控的 Runtime Blackboard：worker 不直接互相聊天，而是将具有跨责任影响的
事实写入 Runtime；Decision Provider 读取压缩后的全局 execution snapshot；Runtime 再通过持久化、
版本化 directive 修改尚未 terminal 的 execution node。

## 2. 目标

Phase 4G11 的目标是把 Runtime 从一次性 durable decomposition 扩展为低频、可恢复的闭环
orchestration：

```text
Worker semantic checkpoint
    -> Runtime Blackboard
    -> Coordination Epoch
    -> Decision Provider control proposal
    -> local validator
    -> durable node directive
    -> same worker session resume
    -> directive acknowledgment
```

具体目标：

1. worker 可以在 execution node 非终态时提交通用 coordination checkpoint；
2. checkpoint 不更新 progress ledger，不声明 goal 完成；
3. Runtime 将多个 active node 的责任、阶段、发现、artifact 和 contract revision 投影为全局快照；
4. Decision Provider 可以继续、重定向或修订已存在的 active responsibility，而不只创建新 node；
5. directive 进入 DB-backed mailbox，不能只存在于 provider prompt 或进程内存；
6. worker 在 cooperative safe point 恢复同一 backend session，并明确 ACK 已消费 directive；
7. Runtime 可以证明一个 node 的发现实际改变了另一个 active node 的后续执行 context；
8. 保留 Phase 4G10 的 primary integration owner、isolated contribution 和 capability boundary。

## 3. 非目标

Phase 4G11 不实现：

- worker 间自由聊天；
- shared-workspace peer-to-peer agent team；
- 将每个 tool call、heartbeat 或普通进度更新发送给 Decision Provider；
- 向正在执行的 Codex turn 异步插入消息；
- 依靠强制 kill 打断正常 worker 以交付普通 directive；
- 让 Decision Provider 直接修改 DB、Kanban task、progress ledger 或 capability authorization；
- 将 implementation、test、debug 等传统阶段变成固定 runtime nodes；
- 默认启用 evaluator；
- 使用 Large/Hard benchmark 作为 MVP 基础设施测试。

## 4. 核心判断

### 4.1 无直接通信不等于无信息流

Runtime worker 不直接调用另一个 worker，但可以通过 Runtime Blackboard 间接协调：

```text
Worker A
    -> checkpoint + evidence
    -> authoritative Runtime state
    -> Decision Provider routing
    -> versioned directive
    -> Worker B
```

跨节点事实必须有来源、revision 和消费记录。不得由某个 worker 在隐藏会话中口头改变另一个
worker 的责任。

### 4.2 Runtime 只处理低频结构协调

高频代码搜索、局部调试、测试失败和同一 feedback loop 内的小修复仍属于单个 worker。只有以下
语义变化可以开启 coordination epoch：

- `milestone_completed`；
- `shared_contract_changed`；
- `scope_overlap_detected`；
- `gap_discovered`；
- `assumption_invalidated`；
- `blocking_dependency`；
- `partial_contribution_ready`；
- `integration_risk`。

普通日志、文件读取、测试开始、测试通过、heartbeat 和 token 使用变化不得触发 Decision Provider。

### 4.3 Active node 通过 cooperative safe point 协调

Phase 4G11 中的 active node 指 goal responsibility 尚未 terminal，而不是要求底层模型 turn 必须在
生成 token 的同时接收新消息。

Worker 在安全边界结束当前 materialization，execution node 进入 `waiting_coordination`，backend
session 标记为可恢复。Runtime 完成协调后创建新的 materialization，继续同一个 node、workspace 和
Codex thread。

对尚在当前 turn 中运行的 target，directive 可以先进入 `queued` mailbox，但不得谎称已经交付或
修改该 turn 的行为。它只在 target 的下一个 cooperative boundary 进入 prompt。MVP 不实现异步
mid-turn injection。

### 4.4 Graph 是责任结构，不是消息队列

Directive 不应为每条消息创建新 execution node。只有新增独立、可恢复责任时才扩图。已有 node 的
scope、acceptance、next intent 或共享 contract 变化，应优先更新该 node 的 contract revision 并恢复
原 session。

## 5. Runtime Blackboard

Blackboard 是 DB facts 的投影，不是新的事实源。它至少包含：

```text
Goal coverage
Active responsibility map
Node phase and contract revision
Latest semantic checkpoints
Shared contract discoveries
Open scope conflicts and blockers
Frozen artifacts and workspace revisions
Pending/delivered/acknowledged directives
Available worker capacity
Recent semantic delta
```

Decision Provider 输入使用完整但有预算的 `global_execution_snapshot`。它不接收所有 worker 日志、
隐藏推理或完整 patch content。技术细节通过 evidence/artifact reference 保留，必要的短摘要进入快照。

## 6. Coordination Checkpoint

Schema：

```json
{
  "schema": "runtime_worker_coordination_checkpoint_v1",
  "kind": "shared_contract_changed",
  "summary": "output persist 已移动到嵌套 output 定义",
  "phase": "implementation",
  "completed_scope": ["run-cache restore"],
  "remaining_scope": ["stage dry-run"],
  "findings": [
    {
      "finding_key": "output-schema-v2",
      "type": "shared_contract_changed",
      "summary": "plots 与 serialization 必须消费新的 output schema",
      "affected_node_keys": ["plots-output", "primary-integration"],
      "evidence_refs": ["workspace:path:dvc/stage/cache.py"]
    }
  ],
  "next_intent": "收到协调结果后继续 stage dry-run",
  "changed_files": ["dvc/stage/cache.py"],
  "consumed_directive_ids": [],
  "worker_session_should_resume": true
}
```

约束：

- `kind` 必须属于允许枚举；
- `summary`、`phase`、`next_intent` 非空；
- `completed_scope` 和 `remaining_scope` 是字符串列表；
- `findings` 至少一项，每项有稳定 `finding_key`、type、summary 和 evidence refs；
- `affected_node_keys` 只能引用当前 job 已存在 node；
- `changed_files` 允许非空，但必须满足当前 declared write scope；
- `consumed_directive_ids` 只能 ACK 已向该 node 交付的 directive；
- checkpoint 不能 claim goal item、写 completion evidence 或请求 capability authorization；
- `worker_session_should_resume` 必须为 true。

Checkpoint ingest 产生：

```text
materialization.status = coordination_checkpoint
execution_node.state = waiting_coordination
backend_worker_session.status = interrupted
event_type = worker_coordination_checkpointed
job.state = waiting_decision
```

它不写 progress ledger。

## 7. Coordination Epoch

一个 epoch 由一个或多个尚未消费的 coordination checkpoint 构成。

```text
epoch revision N
    latest semantic delta
    all active node summaries
    pending directives
    goal/gap coverage
    graph revision
```

MVP 使用以下触发规则：

1. 任一 node 提交有效 coordination checkpoint；
2. 不存在同一 epoch 的 started Decision；
3. 未超过 job 配置的 coordination decision budget；
4. checkpoint 尚未被 control patch 消费。

多个在同一 supervisor tick 内到达的 checkpoint 应合并到同一 epoch。已有其他 worker 正在运行时仍可
生成全局 Decision，但对正在运行 target 的 directive 先保持 `queued`，不能修改其已开始的
materialization contract。

## 8. Durable Directive Mailbox

MVP 增加 `runtime_node_directives`：

```text
id
job_id
target_node_id
source_checkpoint_event_id
decision_id
action
status
expected_contract_revision
applied_contract_revision
directive_json
created_at
delivered_at
acknowledged_at
delivered_materialization_id
acknowledged_materialization_id
```

状态：

```text
queued
    -> delivered
    -> acknowledged

queued/delivered
    -> superseded
```

语义：

- `queued`：已被 validator 接受，但 target 当前 materialization 尚未消费；
- `delivered`：directive 已进入某次 materialization 的 worker context；
- `acknowledged`：该 materialization 的 canonical checkpoint/receipt 明确列出 directive ID；
- `superseded`：后续 control decision 明确替代，未作为执行事实消费。

只有 `acknowledged` 才能证明控制闭环完成。创建 DB row、生成 prompt 或恢复 session 本身都不算
worker 已消费。

## 9. Active Node Control Patch

`runtime_graph_patch_v1` 增加：

```text
issue_directive
supersede_directive
```

`issue_directive` 示例：

```json
{
  "op": "issue_directive",
  "target_node_key": "plots-output",
  "source_checkpoint_event_id": 812,
  "target_checkpoint_event_id": 815,
  "action": "revise_contract",
  "expected_contract_revision": 1,
  "summary": "消费 output schema v2，并把 CLI registration 留给 primary",
  "instructions": [
    "使用 output-schema-v2 artifact",
    "不要修改 dvc/cli.py"
  ],
  "evidence_refs": ["event:812"],
  "contract": {
    "outcome": "完成 plots/output module API 与 focused tests",
    "acceptance_criteria": ["plots focused tests pass"],
    "success_evidence": ["changed_files", "verification", "worker_summary"],
    "declared_write_scope": ["dvc/repo/plots/**", "tests/**/plots/**"],
    "prohibited_actions": ["修改 shared CLI registration"]
  }
}
```

本地 validator 必须检查：

- target 属于同一 job 且未 terminal；
- source checkpoint 存在且未被无关 revision 消费；
- `expected_contract_revision` 与 DB 一致；
- replacement contract 满足 typed node contract；
- goal/gap linkage 和 capability envelope 不被 directive 修改；
- write scope 不与其他 active writer 产生未声明重叠；
- `target_checkpoint_event_id` 若存在，必须属于 target 的未消费 safe point；
- provider 不能直接标记 directive acknowledged；
- 一个 patch 不能对同一 target 产生相互冲突的 revision。

Action MVP：

- `continue`：责任不变，注入新的 context/evidence；
- `revise_contract`：替换 typed contract 并增加 revision；
- `narrow_scope`：作为 `revise_contract` 的受限语义别名；
- `request_partial_contribution`：要求 target 在下一安全边界冻结中间 artifact。

MVP 不实现自动扩大 capability，也不允许 directive 改 goal contract。

## 10. Worker Resume 与 ACK

Materialization context 必须包含：

```text
Current node contract revision
Pending directive IDs
Directive action and instructions
Source checkpoint/evidence refs
Required acknowledgment rule
```

若 node 处于 `waiting_coordination`，应用 control patch 后进入 `ready`。Runtime 保持原 node ID、
workspace 和 backend session，下一次 materialization 以 `resume_reason=coordination_directive` 恢复。

Worker 下一次 canonical checkpoint 或 terminal receipt 必须包含：

```json
{
  "consumed_directive_ids": ["rdir_xxx"]
}
```

缺少 ACK 时：

- 不将 directive 标记为 acknowledged；
- 不允许使用该 materialization 证明“directive 已影响结果”；
- 若 directive 是 required，terminal receipt 不进入 goal completion，node 回到可恢复协调状态；
- 原始 worker output 仍保留为 artifact/evidence，不能静默丢弃。

## 11. 形象执行示例

目标：修改一个存在 `parser`、`renderer` 和 `CLI` 三个责任域的小型仓库。

```text
Primary assessment
    -> parser worker
    -> renderer worker
    -> primary owns CLI integration
```

Parser 完成第一部分后发现 token model 必须从字符串改为带 `kind` 的对象：

```text
parser checkpoint
    finding: shared_contract_changed(token-model-v2)
    affected: renderer, primary
```

Renderer 在自己的第一个 implementation slice 后也进入 safe point。Decision Provider 看到：

- parser 已实现 token-model-v2；
- renderer 仍按 v1 contract 工作；
- CLI integration 尚未开始；
- 两者 write scope 仍不重叠。

Control patch：

```text
parser      -> continue，完成 parser tests
renderer    -> revise_contract，消费 token-model-v2
primary     -> queued context directive，保留 CLI compatibility adapter
```

三个 node 恢复原 session，并在下一 checkpoint/receipt ACK directive。最终报告必须能追溯：

```text
parser event
    -> coordination decision
    -> renderer contract revision 2
    -> renderer materialization attempt 2
    -> renderer ACK
    -> renderer contribution
```

若 renderer 预计需要与 parser 高频来回确认，Decision Provider 应停止 durable split：冻结 renderer
当前 artifact，将剩余 renderer integration 收回 primary，而不是模拟自由聊天。

## 12. 与 Native Internal Orchestra 的边界

Native parent/subagent 适合：

- 同一 workspace；
- 高频共享局部发现；
- 快速搜索、审计和小修复；
- parent 可以承担即时冲突解决。

Runtime durable coordination 适合：

- 长时间责任；
- workspace/capability 隔离；
- 低频但高影响的共享 contract；
- crash/restart 后仍需继续；
- 需要明确 ownership、artifact 和审计链。

正确组合：

```text
Runtime 选择少量 durable responsibility
    -> 每个 worker 在责任内部可按 policy 使用 ephemeral subagents
    -> 跨 durable responsibility 的影响经 Blackboard 和 directive
```

## 13. 安全与权威边界

Directive 不得：

- 修改 goal contract；
- 写 progress ledger；
- 标记 goal complete；
- 授予 capability；
- 绕过 declared write scope；
- 修改 verifier fixed target；
- 将 memory hint 升格为事实；
- 让 worker 创建 durable node。

Database facts、goal contract、capability policy、validator 和 reducer 的权威顺序不变。

## 14. MVP 实现范围

必须实现：

1. `runtime_worker_coordination_checkpoint_v1` parser/validator；
2. `waiting_coordination` node state 与 `coordination_checkpoint` materialization status；
3. checkpoint ingest 不写 ledger；
4. `runtime_node_directives` durable mailbox；
5. node `contract_revision`；
6. `issue_directive` 和 `supersede_directive` patch validator/apply；
7. `global_execution_snapshot` decision delta；
8. same-session `coordination_directive` resume；
9. directive delivery 和 ACK ingest；
10. status/inspect orchestration observability；
11. deterministic controlled integration test；
12. 一个 small 真实 worker/provider smoke，或在真实 provider 不稳定时使用完整 worker lane 加 replay
    Decision Provider；
13. 中文验证报告。

暂不实现：

- async mid-turn injection；
- peer-to-peer worker messaging；
- arbitrary contract merge language；
- 多层嵌套 durable coordinator；
- 自动 semantic embedding；
- Large/Hard capability benchmark。

## 15. 测试设计

### 15.1 Deterministic control case

至少验证：

1. A、B 是两个非终态 execution nodes；
2. A checkpoint 报告影响 B 的 shared contract；
3. B 在 safe point 等待；
4. Decision patch 基于 A event 对 B `issue_directive`；
5. B contract revision 从 1 变为 2；
6. B 使用相同 backend session 恢复；
7. resume prompt 包含 directive 和 source evidence；
8. B 的下一 receipt ACK directive；
9. directive 状态变为 acknowledged；
10. checkpoint 本身没有写 progress ledger；
11. stale revision、未知 target、越权 capability、scope overlap 和伪 ACK 被拒绝；
12. 重复 ingest 不产生重复 checkpoint/directive/ACK fact。

### 15.2 Small task

使用一个小型真实 Git repository，任务包含两个可隔离责任和一个在执行中才暴露的共享 contract。
测试重点不是 coding 难度，而是证明：

```text
node A 的阶段性事实
    -> global decision
    -> node B contract/context 改变
    -> same-session resume
    -> ACK
    -> 最终集成
```

不得用测试脚本直接改 node state 或伪造 acknowledged。Worker evidence 必须经过正常 Kanban
task/run/receipt ingest。

## 16. 可观测指标

必须记录：

- coordination epoch 数；
- checkpoint kind、source node 和 evidence refs；
- Decision Provider no-op/control/expansion 次数；
- directive queued/delivered/acknowledged/superseded 数；
- checkpoint 到 directive 的延迟；
- directive 到 ACK 的延迟；
- contract revision history；
- same-session resume identity；
- target 未 ACK、stale revision 和 scope conflict rejection；
- child contribution 被接受、修改、拒绝的比例；
- primary 重做 child scope 的比例；
- orchestration token 和 wall-time overhead。

## 17. 验收标准

Phase 4G11 MVP 完成需要：

- coordination checkpoint 可由真实 worker lane 产生并通过 canonical ingest；
- checkpoint 不改变 goal truth；
- global snapshot 同时包含 source 与 target active responsibility；
- Decision Provider 能提出并通过 validator 的 active-node control patch；
- directive 是 DB durable fact，支持 crash/restart 后继续交付；
- target 使用同一 backend session 恢复；
- ACK 与具体 materialization 绑定；
- 一个 node 的 checkpoint 可追溯地改变另一个 node 的 contract revision 或 context；
- 非法 control patch 和伪 ACK 被确定性拒绝；
- consistency checker 无新增 violation；
- small 验证报告清楚区分 Runtime correctness、closed-loop orchestration 和 task capability；
- 不运行 Large/Hard benchmark。

Phase 4G11 不以“节点更多”作为成功。若 small task 在结构评估后保持单 node，不能验证本阶段；受控
case 必须确实包含两个 active responsibility 和一次跨节点 directive/ACK 闭环。

## 18. 实现与验证状态

截至 2026-07-19，Phase 4G11 MVP 已完成实现：

- 增加 `closed_loop_coordination` orchestration mode；
- 增加 coordination checkpoint canonical parser/validator；
- 增加 `waiting_coordination` 与不写 progress ledger 的 checkpoint ingest；
- 增加 `runtime_node_directives` durable mailbox 和 node `contract_revision`；
- 增加 `issue_directive` / `supersede_directive` validator 与 apply path；
- 增加 global execution snapshot、same-session resume、directive delivery 与 ACK；
- 增加未 ACK delivered directive 在 protocol recovery materialization 中的再次注入；
- 增加 status/consistency/orchestration observability；
- 增加 deterministic control tests 和可恢复的真实 Small runner。

真实 Small 验证使用两个 isolated child responsibility 和一个 shared-workspace integration
owner。最终 run 证明：

```text
parser shared contract checkpoint
    + renderer blocking checkpoint
    -> real Decision Provider control patch
    -> two same-session resumes
    -> two directive ACKs
    -> two frozen contributions
    -> primary attribution and integration
    -> goal satisfied / consistency passed
```

最终成功 run 使用与生产配置相同的 base URL、API key 和 `gpt-5.6-sol` 模型，但由于该模型源
WebSocket transport 在验证窗口内反复断流，使用隔离 Codex home 将
`supports_websockets=false`，通过 HTTP transport 完成。WebSocket 配置、20 次重连和
same-session resume 路径已在此前 attempt 中实际触发；transport 可用性不作为 Runtime
correctness 的替代证据。

完整过程与证据见：

- `docs/validation/phase4g11/phase4g11-small-20260719-151215/execution-summary.md`；
- `docs/validation/phase4g11/phase4g11-small-20260719-151215/run-report.json`。
