Profile-Version: 5

# Validator Recovery 决策 Profile

## 用途

上一份 Runtime graph patch 已经被解析，但被本地 validator 拒绝时，使用本 Profile 生成一次受约束修正。

## 输入

Provider 接收普通 Runtime decision request，以及 `validator_feedback`：

- `rejected_patch`；
- validator `status`；
- validator `reason`；
- 相关的当前和预期 graph revision；
- 尚未解决的 `pending_coordination_actions`。

## 输出

只返回一个符合 `runtime_graph_patch_v1` 的 JSON 对象。不得输出 Markdown fence、解释性文字或注释。

## Recovery Rules

- 不得重复同一个已拒绝 op shape。
- 若 reason 指出 node key 未知，只能引用 `delta.frontier`、checkpoint graph frontier 或
  `pending_coordination_actions` 中已存在的 node；否则放弃该 dependency。
- `add_dependency` 只能使用 `from_node_key` 表示 prerequisite，使用 `to_node_key` 表示 dependent；
  不得使用 `node_key`、`depends_on_node_key` 等 alias。
- `insert_verifier` 必须提供 `target_node_key` 或 `target_goal_item_key`，并为 verifier 自身提供真实
  `goal_item_keys` 或 `gap_keys`。
- 若关联 goal 没有 `verifier_required=true`，不得继续尝试 verifier。责任已覆盖时不改 graph；尚未覆盖时
  使用一个 coherent implementation node 承担实现、测试、调试和本地验证。
- 新 node 缺少 goal/gap/human linkage 时，补充当前 request 中真实存在的 `goal_item_keys`、
  `gap_keys` 或 `human_gate_reason`。
- stale revision 必须使用 request 中当前 `db_revision` / `graph_revision`。
- `strategy_update` 必须包含 `strategy_summary`、非空 `changes_from_previous_attempts` 和 typed `contract`。
  不得用它直接完成或阻塞 job。
- graph expansion 被要求提供 `decomposition` 时，保留合法 execution op，并增加 graph-patch profile
  规定的精确 versioned decomposition。存在另一个 nonterminal execution node 时，即使只新增一个 node，
  也属于 expansion。
- receipt-invalid、timeout 或 branch exhausted 后的 recovery 使用 evidence-backed
  `context_or_runtime_limit`，并引用 decision delta 中真实的 `event:<id>` 或
  `receipt:<node-key>:attempt-<n>`。Justification 的 `nodes` 必须覆盖 patch 创建的全部 execution node。
- 不得在同一个 recovery patch 中为新 `strategy_update` 再添加 dependency。若它替换已有 promoted
  contribution 的 integration owner，设置 `replaces_node_key` 和
  `inherit_promoted_contributions=true`。Runtime 只继承 validated promoted artifact，不继承
  quarantined attempt patch。

## Coordination Action Recovery

当 `pending_coordination_actions` 含 `status=rejected` 的 provider-required action：

1. 只修正 validator 指出的字段或结构原因；
2. 继续引用原 `source_checkpoint_event_id` 和 candidate ref；
3. 不得把 action 降级为本地已解决，也不得伪造 ACK、delivery 或 terminal evidence；
4. 若现有责任足以吸收 candidate，使用 `resolve_responsibility_candidate`；
5. 若需要 durable expansion，保持 source candidate、goal、scope、dependency 和 integration owner
   lineage；
6. 不得重新处理已经 `applied`、`no_action` 或本地 `local_context_route` 的 action；
7. 若 reason 表明 candidate 不足以支持 expansion，选择显式 no-expansion resolution，而不是猜测新
   candidate。

一次 rejection 不允许删除 action。修正 patch 再次失败时，Runtime 保留 action 与全部 validator feedback，
后续 recovery 仍基于同一事实链。

## Safe Fallback

最佳结构动作不明确时，优先保持一个覆盖当前 goal gap 的 coherent primary execution node。同一 workspace、
capability envelope、完整 outcome 和 feedback loop 下的 inspection、research、implementation、testing、
debugging 和 local verification 应保持在一个 node 中。

不得为了从 validator rejection 恢复，而拆成 analysis、research、implementation、testing 或 debugging
阶段。没有合法 `decomposition` 时，最多返回一个新的 runnable worker node，并且当前 delta 中不能存在
使 decomposition 成为必需的其他 nonterminal execution node。

Write scope 必须是规范化 workspace-relative glob。整个 workspace 使用 `**`；不得使用
`repository/**`、`workspace/**`、绝对路径或 `..` segment。

## 示例

```json
{
  "schema": "runtime_graph_patch_v1",
  "expected_revision": 7,
  "rationale_summary": "上一份 verifier patch 缺少合法 target，因此改为由一个 coherent implementation node 覆盖仍未满足的 goal。",
  "ops": [
    {
      "op": "create_node",
      "node_key": "produce-initial-runtime-result",
      "node_type": "implementation",
      "title": "Produce initial runtime result",
      "description": "Create and locally verify the complete result for the open goal.",
      "goal_item_keys": ["initial-runtime-result"],
      "contract": {
        "outcome": "Produce and locally verify the complete initial runtime result.",
        "acceptance_criteria": ["Requested result exists", "Local verification passes"],
        "success_evidence": ["changed_files", "verification", "worker_summary"],
        "declared_write_scope": ["**"],
        "prohibited_actions": ["production_deployment"]
      }
    }
  ]
}
```
