# Hermes Kanban Runtime Kernel Phase 3D 实现计划

Phase 3D 的目标是把 runtime 从“能跑多轮 evidence/provider loop”推进到
“长任务可以持续恢复和调整策略”。本阶段仍然不是 daemon，不是旧 Orchestra
manager loop，也不是让 LLM 成为负责人 agent。它要补齐长任务推进中最容易
缺失的结构能力：strategy update、显式 goal waiver / goal mutation、合法等待
边界和恢复后继续推进。

## 目标

第一，strategy update 成为一等 graph patch op。它不是 provider 的自由文本
解释，也不是直接改 DB 状态；它必须落成 `strategy_update` execution node，
服务某个 goal item 或 open gap，之后仍通过 Kanban task/evidence 回写。

第二，用户目标变更要进入 DB 事实层。第一版先支持显式 goal waiver：用户或
operator 可以放弃某个 goal item，runtime 写入 waived ledger evidence 和
goal mutation event。Completion 仍由 reducer 判断，不由 CLI 直接 mark done。

第三，resume 语义要可测试。runtime job 停在 `waiting_worker`、
`waiting_decision` 或 `waiting_human` 后，后续调用 `runtime advance` 必须能从
DB facts 恢复，不依赖进程内记忆。

第四，anti-stuck signal 不能只是 dashboard 提示。连续失败、stale gap 或重复
patch rejection 后，provider 应能通过 `strategy_update` 创建改变策略的节点，
而不是继续重复同类 implementation/verifier。

第五，默认测试继续离线。真实 provider smoke 可选，但 Phase 3D 完成不能依赖
网络或 `.codex`。

## 非目标

不实现常驻 runtime daemon。

不接真实 compaction provider。

不允许 provider 直接修改 goal contract、waive goal、complete job、mark blocked
或 release node。

不把 strategy update 当成固定 phase。它只是一个执行意图，只有在 gap 或
anti-stuck 信号需要时才出现。

不把 human gate 作为不确定时的默认出口。只有符合 human gate policy 的外部
权限、凭证、成本、破坏性变更、产品偏好或合规边界才可以等待用户。

## Graph Patch

新增 op：

```json
{
  "op": "strategy_update",
  "node_key": "revise-provider-contract-strategy",
  "title": "Revise provider contract strategy",
  "description": "Review failed attempts and propose a smaller verifiable path.",
  "goal_item_keys": ["runtime-provider-contract"],
  "gap_keys": ["runtime-provider-contract:stale_or_no_progress"],
  "strategy_summary": "Stop repeating implementation attempts; isolate the failing contract first.",
  "changes_from_previous_attempts": [
    "insert a research/debug node before new implementation",
    "verify the contract with a minimal fixture"
  ]
}
```

Validator 必须要求：

- `node_key`、`title`、`description`；
- `goal_item_keys` 或 `gap_keys` 或 `human_gate_reason`；
- `strategy_summary`；
- 至少一个 `changes_from_previous_attempts`。

Apply 后创建 `node_type='strategy_update'` 的 execution node。它可被 materialize
为普通 Kanban task，worker receipt 可写入 `rejected_approaches`、
`known_failure_boundaries`、`new_constraints` 和后续建议，但仍不直接改 graph。

## Goal Waiver

新增显式 CLI：

```bash
hermes kanban runtime waive-goal <job_id> <goal_item_key> \
  --reason "user accepted mock provider for phase one" \
  --source user \
  --json
```

语义：

- 查找当前 active goal contract 的 goal item；
- 写入 `progress_ledger`，`satisfaction='waived'`，
  `verification_state='waived'`；
- 将 goal item state 推导为 `waived`；
- 写入 `goal_item_waived` 和 `human_decision_received`/`goal_contract_changed`
  类事件；
- 调用 reducer 重新判断 job state；
- 不直接设置 job done。

Completion rule 可以把 `satisfied` 和 `waived` 视为 required goal item 已解决，
但 contradicted evidence、running node、waiting human 和 failed required verifier
仍然阻止 done。

## Resume / Continuation

Phase 3D 的恢复语义不是恢复进程内对象，而是重复调用：

```bash
hermes kanban runtime status <job_id> --json
hermes kanban runtime advance <job_id> --loop --provider fake|real --json
```

只要 DB state 中仍有 open gaps、ready/running nodes、human gates、decision
session 和 ledger facts，runtime 就必须能继续。测试应模拟进程间恢复：一次
CLI 调用停在 waiting_worker，下一次 CLI 调用写 evidence，再下一次 advance
继续推进。

## 验收标准

第一，`strategy_update` patch 被 validator 支持，缺少 goal/gap linkage、
strategy summary 或 changed strategy 时被拒绝。

第二，`strategy_update` apply 后生成 materializable strategy node，并且不会
直接修改 completion、ledger 或 graph 外事实。

第三，`runtime waive-goal` 能显式 waive goal item，写 ledger 和 events，并由
reducer 本地判断 job 是否 done。

第四，离线测试证明 job 可以跨多个 CLI 调用恢复推进：waiting_worker -> evidence
ingest -> waiting_decision -> strategy_update patch -> materialize strategy node ->
waive 或后续 evidence ->合法 done / waiting boundary。

第五，真实 provider 默认不参与单测；真实 smoke 仍必须显式启用。
