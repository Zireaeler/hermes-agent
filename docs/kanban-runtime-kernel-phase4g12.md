# Hermes Kanban Runtime Kernel Phase 4G12

# Evidence-Driven Dynamic Graph Mutation

## 1. 背景

Phase 4G11 已经证明以下闭环可以真实运行：

```text
一个 worker 提交阶段性语义 checkpoint
    -> Runtime 投影全局 execution snapshot
    -> Decision Provider 生成 control patch
    -> DB-backed directive 改变另一个 active node 的下一段执行
    -> 原 worker session 恢复并 ACK
```

但 Phase 4G11 的真实 Small 验证仍有一个明确限制：三节点拓扑由 fixture 预先创建；当
`global_execution_snapshot.coordination_checkpoints` 非空时，Decision Profile 又要求 patch
只能包含 `issue_directive`。因此该 run 证明了 active-node context control，没有证明运行中新证据
可以改变 graph topology。

Phase 4G12 补齐这个缺口：worker 在 cooperative safe point 发现新的 durable responsibility 后，
可以将结构化的责任候选写入 checkpoint；Decision Provider 基于整个 active graph 决定是否创建
新 node；本地 validator 负责确认该扩图确实由 checkpoint evidence 支撑，并且没有越过 goal、scope、
capability 和 integration owner 边界。

## 2. 目标

Phase 4G12 建立以下最小闭环：

```text
Worker A discovers a durable gap
    -> coordination checkpoint + responsibility candidate
    -> Runtime global snapshot
    -> Decision Provider chooses routing-only or evidence-driven expansion
    -> local validator checks evidence and structural boundaries
    -> create isolated child + dependency + active-node directives
    -> existing sessions resume, new child starts
    -> primary integration owner consumes all frozen contributions
```

具体目标：

1. worker checkpoint 可以携带非权威 `responsibility_candidates`；
2. candidate 只表达 worker 发现的结构事实，不直接创建 node；
3. coordination epoch 可以选择普通 directive 路由，或一次受控的 evidence-driven graph expansion；
4. 新 node 必须关联现有 goal item，并引用精确 checkpoint candidate；
5. 新 node 使用 isolated worktree，不能抢占 existing active writer 的声明范围；
6. 新 node 必须有明确 integration owner，最终结果仍由 owner 集成；
7. 同一 control patch 同时处理既有 waiting node，不能扩图后把原责任悬空；
8. 报告必须区分 context control、contract revision 和 topology mutation。

## 3. 非目标

Phase 4G12 不实现：

- worker 直接创建 durable runtime node；
- worker 间 peer-to-peer 消息或共享隐藏推理；
- 每个 checkpoint 都自动扩图；
- 根据普通日志、测试失败或 heartbeat 扩图；
- 向正在生成 token 的模型 turn 异步注入消息；
- 任意删除、迁移或抢占 active responsibility；
- 无 evidence 的 speculative decomposition；
- 使用 Large/Hard benchmark 验证本阶段基础设施；
- 默认启用 evaluator。

## 4. 能力语义

### 4.1 “即时”的准确含义

本阶段的即时控制是 cooperative，而不是异步中断：

```text
worker 到达语义 safe point
    -> checkpoint canonical ingest
    -> 下一次 coordination epoch
    -> graph patch 生效
```

正在执行的模型 turn 不会被强行打断。Runtime 保证的是：在下一段 materialization 开始前，已接受的
拓扑和 directive 变化已经进入 DB 事实与 worker context。

### 4.2 两类 coordination epoch

`routing-only`：

- 没有新的 durable responsibility；
- patch 只包含 `issue_directive` / 必要的 `supersede_directive`；
- 每个 `waiting_coordination` node 恰好收到一条 directive。

`evidence-driven-expansion`：

- 至少一个 checkpoint 含有效 `responsibility_candidates`；
- patch 可以包含少量 `create_node`、对应 `add_dependency` 和既有 waiting node 的 directive；
- 每个新 node 必须引用一个 candidate；
- 扩图与 directive 在同一个原子 graph patch 中应用。

若 candidate 存在，Decision Provider 仍可选择 `routing-only`，但必须在 directive 中明确由哪个既有
责任吸收该 gap。Runtime 不以“节点更多”为成功标准。

## 5. Responsibility Candidate

`runtime_worker_coordination_checkpoint_v1` 增加可选字段：

```json
{
  "responsibility_candidates": [
    {
      "candidate_key": "legacy-token-adapter",
      "outcome": "为历史 token record 提供独立兼容转换层",
      "reason_type": "execution_discovered_gap",
      "acceptance_criteria": [
        "legacy record 转换为当前 token contract",
        "兼容测试通过"
      ],
      "declared_write_scope": [
        "src/token_compat.py",
        "tests/test_token_compat.py"
      ],
      "goal_item_keys": [
        "versioned-token-pipeline"
      ],
      "integration_owner_node_key": "pipeline-integration",
      "evidence_refs": [
        "workspace:path:fixtures/legacy_tokens.json"
      ]
    }
  ]
}
```

约束：

- `candidate_key` 在当前 checkpoint 中唯一，且不能与现有 node key 相同；
- `reason_type` 第一版只允许 `execution_discovered_gap`、`workspace_isolation`、
  `capability_boundary`、`independent_verification`；
- `outcome`、`acceptance_criteria`、`declared_write_scope`、`goal_item_keys` 和
  `evidence_refs` 非空；
- goal item 必须属于当前 job；
- `integration_owner_node_key` 必须引用当前 job 已存在的 nonterminal integration owner；
- write scope 必须是规范化 workspace-relative scope；
- candidate scope 不能与其他 active isolated writer 明显重叠；
- candidate 是 non-authoritative proposal，不改变 graph revision、ledger 或 completion。

## 6. Candidate Reference

由 coordination epoch 创建的新 node 必须包含：

```json
{
  "source_responsibility_ref": "event:123#responsibility:legacy-token-adapter"
}
```

本地 validator 解析该 reference，并检查：

1. event 属于当前 job；
2. event 类型为 `worker_coordination_checkpointed`；
3. candidate 存在于该 event payload；
4. `goal_item_keys` 不超出 candidate 声明；
5. node contract 的 write scope 不超出 candidate 声明；
6. node acceptance criteria 覆盖 candidate criteria；
7. patch 中存在 `new child -> integration owner` dependency；
8. decomposition 引用同一 checkpoint event。

Provider 可以收紧 scope 或补充 acceptance，但不能把 candidate 扩大成另一个未被 evidence 支撑的责任。

## 7. Validator Rules

当存在 active coordination checkpoint 时：

1. 没有 `create_node` 时，沿用 Phase 4G11 routing-only 规则；
2. 存在 `create_node` 时，op 只允许 `create_node`、`add_dependency`、`issue_directive` 和必要的
   `supersede_directive`；
3. 每个新 node 都必须有合法 `source_responsibility_ref`；
4. 每个 `waiting_coordination` node 仍必须恰好收到一条 directive；
5. 同一 epoch 创建的 node 数不能超过 policy 剩余 child budget；
6. 新 child 必须使用 configured worker lane、允许的 capability 和 `isolated_worktree`；
7. 每个新 child 必须依赖到 candidate 指定的 integration owner；
8. decomposition 必须使用 evidence-required reason，并引用 candidate event；
9. candidate scope 与 active isolated writer scope 明显重叠时拒绝；
10. patch 原子失败，任何非法 op 都不能留下部分 node、dependency 或 directive。

## 8. Runtime Facts 与可观测性

扩图成功后至少记录：

```text
worker_coordination_checkpointed
patch_applied
node_created
structure_checkpoint_expansion_applied
runtime_directive_queued
```

报告增加：

- `coordination_epoch_mode`；
- `responsibility_candidate_count`；
- `dynamic_nodes_created`；
- `source_responsibility_ref`；
- `graph_revision_before` / `graph_revision_after`；
- `existing_sessions_resumed`；
- `directive_ack_count`；
- `integration_owner_node_key`；
- `contribution_acceptance`；
- consistency 结果。

## 9. 受控真实验证

使用 Small fixture，不使用 Hard benchmark。初始 graph 只包含：

```text
parser-contract ---------\
                          +--> pipeline-integration
renderer-contract -------/
```

fixture 中存在运行前未建 node 的 legacy compatibility gap。真实 worker 必须先从 repository evidence
发现该 gap，并在 checkpoint 中产生 candidate。真实 Decision Provider 随后应创建：

```text
legacy-token-adapter ----> pipeline-integration
```

同时 parser/renderer 收到 directive，恢复原 session。最终 primary 集成三份 contribution 并通过测试。

强制断言：

- 初始 graph 不含 `legacy-token-adapter`；
- candidate 来自真实 worker checkpoint；
- graph revision 在 control patch 后增加；
- 新 node 的 source reference、goal、scope 和 dependency 全部可追溯；
- 至少一个既有 active node 在同一 epoch 收到并 ACK directive；
- 新 worker 使用独立 worktree；
- primary 接受三份 frozen contribution；
- final goal satisfied；
- consistency 0 violation。

## 10. 验收结论边界

本阶段通过后可以声明：

> Runtime Kernel 能够在 worker terminal completion 之前，根据阶段性语义 evidence 修改 active
> execution graph：既可以改变既有责任的下一段 context，也可以新增独立、可恢复的 durable
> responsibility，并将其纳入原 integration owner 的完成闭环。

仍不能声明：

- 任意时刻异步控制正在运行的模型；
- 任意复杂任务都会受益于扩图；
- Runtime orchestra 在质量上优于 native internal subagents；
- Provider 可以无条件自由修改 graph；
- Small 验证已经证明 Large/production 收益。
