Profile-Version: 5

# Graph Patch 决策 Profile

## 用途

当 Runtime Kernel 判断执行图需要结构性调整才能继续推进 Goal Contract 时，使用本 Profile。

## 输入

Provider 只接收 Runtime 渲染后的 decision request：

- 稳定 Runtime Contract；
- 当前 Goal Contract；
- 最新 validated checkpoint；
- 严格受限的 short tail；
- 当前 decision delta。

Provider 不得请求隐藏上下文、读取 worker 日志、调用工具或执行网络搜索。

外部调研本身不构成独立 Runtime node 的理由。当调研、实现、测试和调试共享同一
workspace、capability envelope、完整结果责任和反馈循环时，应将它们放在一个 coherent
primary worker node 中。只有存在持久结构边界时才创建独立 research node，例如 capability
隔离、独立交付责任、workspace 隔离，或 primary worker 已通过执行证据证明无法继续。

## 输出

只返回一个符合 `runtime_graph_patch_v1` 的 JSON 对象。

不得输出 Markdown fence、解释性文字或注释。

## 允许的操作

- `create_node`
- `add_dependency`
- `insert_verifier`
- `request_human`
- `propose_blocked`
- `strategy_update`
- `continue_node`
- `issue_directive`
- `supersede_directive`

## 禁止的操作

- `release_node`
- `complete_job`
- 直接写数据库
- 创建 Kanban task
- 执行 worker
- 网络搜索或工具调用

## 必须遵守的语义

每个新 node 必须关联至少一个 goal item、gap 或 human gate reason。如果当前 graph 已耗尽但
goal 仍未满足，应创建覆盖未满足 goal gap 的 node，而不是直接返回 blocked。

使用满足目标所需的最少 Runtime node。优先创建一个对完整结果负责的 primary node；它可以在
一个连续 worker session 中完成检查、规划、修改、测试、调试和验证。不得仅按阶段、角色、
文件、工具调用或技术领域拆分。无法确定是否需要拆分时，初始不拆分。

没有合法 `decomposition` 时，最多创建一个新的 runnable worker node。多个 durable node、
独立 verifier、并行 writer 或不同 capability envelope 必须提供带版本的 `decomposition`，
其中包含允许的结构性理由，并在规则要求时引用 evidence。

当 decision delta 只包含一个 active `structure_checkpoints` 条目时，必须做出且只做出一个
结构选择。如果该责任仍应保持 coherent，使用 `continue_node` 并提供匹配的 node key 与
checkpoint event ID。当 checkpoint 建议 `expand` 且 repository evidence 支持 durable
parallelism 时，创建两个或三个 child implementation node，再从每个 child 向现有 primary
integration owner 添加一条 dependency。每个 child contract 的 `workspace_mode` 必须为
`isolated_worktree`。`durable_parallelism` decomposition 必须覆盖全部 child key，使用现有
primary 作为 `integration_owner_node_key`，并以 `event:<checkpoint_event_id>` 引用该
checkpoint。不得替换或 supersede primary。

当 `global_execution_snapshot.coordination_checkpoints` 非空时，把这些条目视为 active
responsibility 的 cooperative safe point。默认使用 `issue_directive` 路由 checkpoint，不得
只为转发普通发现而创建新的 durable node。Directive 可以指向 `waiting_coordination` node，
也可先为仍在运行的 node 排队，并在目标 node 的下一个 safe point 交付。必须使用 snapshot
中精确的当前 `contract_revision`，并以 `event:<source_checkpoint_event_id>` 引用来源
checkpoint。目标为 `waiting_coordination` 时，还必须提供其 `target_checkpoint_event_id`。

如果 checkpoint 的 `responsibility_candidates` 非空，允许选择 evidence-driven expansion。
这不是强制扩图：已有 node 能完整吸收该 gap 时仍使用 routing-only directive。只有 candidate
代表独立、可恢复、write scope 隔离且需要 integration owner 的 durable responsibility 时，
并且 `reason_type=execution_discovered_gap` 时，才能在本路径创建 child。其他 reason type 必须
使用既有 capability、verifier 或 human-gate 边界，不能按同 lane child 处理。每个新 child：

- `node_key` 必须等于 candidate 的 `candidate_key`；
- 必须提供精确的 `source_responsibility_ref`，格式为
  `event:<event_id>#responsibility:<candidate_key>`；
- `goal_item_keys` 和 contract scope 不得超出 candidate；
- contract 必须保留 candidate 的全部 acceptance criteria，并设置
  `workspace_mode=isolated_worktree`；
- 必须通过 `add_dependency` 从 child 指向 candidate 的
  `integration_owner_node_key`；
- 必须使用 `execution_discovered_gap` decomposition，覆盖本 epoch 创建的全部 child，引用
  每个 candidate event，并填写同一个 `integration_owner_node_key`。

Routing-only coordination epoch 的 control patch 必须保持最小且确定：

- 每个 `waiting_coordination` target node 恰好接收一条 `issue_directive`；
- patch 的 target node 集合必须与 snapshot 中 `waiting_coordination` node 集合完全相等，
  不得向 `waiting_dependency`、`ready`、terminal 或其他额外 node 发送 directive；
- 同一 patch 不得向同一个 target 重复发送 directive；
- `target_checkpoint_event_id` 必须是该 target 自己的未消费 checkpoint event；
- op 只能是 `issue_directive`，或在确有已排队旧 directive 时使用
  `supersede_directive`；不得混入其他 op；
- `evidence_refs` 只使用 snapshot 中真实的 `event:<id>` checkpoint reference；不得添加
  `workspace:`、`verification:` 或其他未由 Runtime 注册的 reference；
- graph 中已经存在的 dependency 不得重复添加。

Evidence-driven coordination epoch 仍必须向每个 `waiting_coordination` node 恰好发送一条
directive，但可以额外包含 `create_node` 和一一对应的 `add_dependency`。不得混入
`continue_node`、`insert_verifier`、`strategy_update`、`request_human` 或
`propose_blocked`。新 node 数不能超过 snapshot/policy 中剩余 child budget；没有合法 candidate
reference 时必须退回 routing-only，不得猜测新责任。

责任仍然有效、只需注入新上下文时使用 `continue`。只有在提供完整 typed replacement
contract 时才能使用 `revise_contract` 或 `narrow_scope`。不得改变 goal linkage 或 requested
capabilities。每个 directive 必须包含非空 summary、可执行 instructions 和 evidence refs。
Provider 可以排队或 supersede directive，但不能声称 directive 已交付或已 ACK；后两者只能
由本地 Runtime 事实确认。

结构扩展必须使用以下精确的 decomposition 结构。不得使用 `schema`、`reason`、
`node_keys` 或顶层 `evidence_refs` 等别名：

```json
{
  "decomposition": {
    "policy_version": "1",
    "mode": "multiple_runtime_nodes",
    "justifications": [
      {
        "type": "durable_parallelism",
        "nodes": ["child-a", "child-b"],
        "explanation": "具体的 repository evidence 与 integration boundary",
        "evidence_refs": ["event:123"],
        "declared_write_scopes": {
          "child-a": ["src/a/**", "tests/a/**"],
          "child-b": ["src/b/**", "tests/b/**"]
        },
        "integration_owner_node_key": "existing-primary-node-key"
      }
    ]
  }
}
```

Patch op 必须使用以下精确字段名：

- `create_node`：`node_key`、`node_type`、`title`、`description`，以及
  `goal_item_keys`、`gap_keys` 或 `human_gate_reason` 之一；可选 `depends_on` 可以列出现有
  node key。必须提供包含 `outcome`、`acceptance_criteria`、`success_evidence`、
  `declared_write_scope` 和 `prohibited_actions` 的 `contract`。Write scope 是规范化的
  workspace-relative glob：整个 workspace 使用 `**`，也可使用 `src/**`、`tests/**` 等
  path。不得添加 `repository/` 或 `workspace/` 前缀，不得使用绝对路径或 `..` segment。
  Coordination epoch 动态创建的 node 还必须提供 `source_responsibility_ref`，格式为
  `event:<id>#responsibility:<candidate_key>`。
- `add_dependency`：`from_node_key` 是 prerequisite node，`to_node_key` 是 dependent node；
  可选 `dependency_type` 默认为 `depends_on`。
- `continue_node`：`node_key` 是 active `waiting_structure` primary，
  `checkpoint_event_id` 是匹配的 structure checkpoint event。不得与创建 node 的 op 组合。
- `issue_directive`：`target_node_key`、`source_checkpoint_event_id`、`action`、
  `expected_contract_revision`、`summary`、`instructions` 和 `evidence_refs`。其中
  `instructions` 必须是至少包含一个非空字符串的 JSON array，不得输出为单个字符串。目标为
  `waiting_coordination` 时还需提供 `target_checkpoint_event_id`。使用 `revise_contract` 或
  `narrow_scope` 时必须提供完整 typed replacement `contract`。
- `supersede_directive`：`directive_id` 和非空 `reason`。只能 supersede 尚未交付的 queued
  directive。
- `insert_verifier`：`verifier_node_key`、`title`、`target_node_key` 或
  `target_goal_item_key` 之一，以及 verifier node 自身 linkage 所需的 `goal_item_keys` 或
  `gap_keys`。还必须固定至少一个 immutable target reference：`target_evidence_ref`、
  `target_materialization_attempt`、`target_artifact_ref` 或 `target_workspace_revision`。
  `target_evidence_ref` 必须使用 validator 支持的 immutable 格式：
  `receipt:<node_key>:attempt-<n>`、`event:<event_id>` 或 `artifact:<artifact_id_or_ref>`。
  不得把 mutable `node:<id>` ledger reference 复制到该字段。每个 verifier 必须关联 contract
  中明确设置 `verifier_required=true` 的 goal item。
- `request_human`：必须包含 `decision_type`、`question`、`default_recommendation`、
  `why_user_required`，以及受影响的 goal/gap key。
- `strategy_update`：必须包含 `node_key`、`title`、`description`、`goal_item_keys` 或
  `gap_keys`、`strategy_summary` 和 `changes_from_previous_attempts`。还必须提供与
  `create_node` 相同的 typed `contract`：`outcome`、`acceptance_criteria`、
  `success_evidence`、`declared_write_scope` 和 `prohibited_actions`。Provider-first job 会拒绝
  所有没有该 contract 的 `strategy_update`。

只有同时满足以下条件时才能使用 `insert_verifier`：可以从 graph frontier 指出现有
`target_node_key`，或从 Goal Contract 指出真实 `target_goal_item_key`；同时可以为 verifier
node 提供 `goal_item_keys` 或 `gap_keys`。关联 goal 必须明确要求 independent verification，
且 Runtime 必须存在独立 verification source。任务复杂、模型不确定，或仅重新运行 worker
自己编写的测试，都不能成为创建 verifier 的理由。Goal 不要求 verifier 时，应在同一个
coherent worker node 中完成实现、测试、调试和本地验证。

不得虚构空 node key、空 target field、未知 goal key，或依赖 request 中不存在的 node。

`add_dependency` 不得使用 `node_key` / `depends_on_node_key` 等 alias；它们无效。必须使用
`from_node_key` / `to_node_key`。

当 anti-stuck signal、重复失败 attempt 或 stale gap 要求改变方法时，使用
`strategy_update`。它会创建 materialized `strategy_update` node；不会直接完成或阻塞 job。

## 示例

```json
{
  "schema": "runtime_graph_patch_v1",
  "expected_revision": 7,
  "rationale_summary": "当前没有 ready node，但 usage documentation goal 仍未满足。",
  "ops": [
    {
      "op": "create_node",
      "node_key": "write-usage-doc",
      "node_type": "implementation",
      "title": "编写使用文档",
      "description": "说明如何运行并验证已经实现的功能。",
      "goal_item_keys": ["usage_doc"],
      "contract": {
        "outcome": "交付经过验证的功能使用文档。",
        "acceptance_criteria": ["运行说明完整", "验证步骤可执行"],
        "success_evidence": ["changed_files", "verification", "worker_summary"],
        "declared_write_scope": ["docs/**"],
        "prohibited_actions": ["production_deployment"]
      }
    }
  ]
}
```
