Profile-Version: 2

# Validator Recovery Decision Profile

## Purpose

Use this profile after a previous runtime graph patch was parsed but rejected by
the local validator.

## Input

The provider receives the normal runtime decision request plus
`validator_feedback` containing:

- `rejected_patch`;
- validator `status`;
- validator `reason`;
- current and expected graph revision when relevant.

## Output

Return exactly one corrected JSON object matching `runtime_graph_patch_v1`.

No Markdown fences, no explanatory prose, no comments.

## Recovery Rules

- Do not repeat the same rejected op shape.
- If the rejected reason says a node key is unknown, either reference an
  existing node from `delta.frontier` / checkpoint `graph_frontier`, or avoid
  that dependency.
- If the rejected reason concerns `add_dependency`, use only
  `from_node_key` for the prerequisite node and `to_node_key` for the dependent
  node. Do not use alias fields such as `node_key` or `depends_on_node_key`.
- If the rejected reason says `insert_verifier` needs
  `target_node_key` or `target_goal_item_key`, include one of those fields.
- If the rejected reason says `insert_verifier` needs `goal_item_keys` or
  `gap_keys`, add concrete existing goal/gap keys for the verifier node itself.
- If no valid verifier target is obvious, prefer `create_node` linked to a
  required `goal_item_key` or open `gap_key`.
- If the rejected reason says a new node lacks goal/gap/human linkage, add
  `goal_item_keys`, `gap_keys`, or `human_gate_reason`.
- If the rejected reason is stale revision, use the current `db_revision` /
  `graph_revision` from the request.
- If the rejected op is `strategy_update`, include `strategy_summary` and a
  non-empty `changes_from_previous_attempts` list. Include a typed `contract`
  with `outcome`, `acceptance_criteria`, `success_evidence`,
  `declared_write_scope`, and `prohibited_actions`. Provider-first jobs reject
  `strategy_update` without this contract. Do not use it to mark the job done
  or blocked.

## Safe Fallback

When the best structural action is unclear, prefer one coherent primary
execution node covering the current goal gap. The node may include inspection,
research, implementation, testing, debugging, and local verification when they
share one workspace, capability envelope, accountable outcome, and feedback
loop.

Do not recover from validator rejection by splitting work into analysis,
research, implementation, testing, or debugging phases. Without a valid
`decomposition`, return at most one new runnable worker node.

Write scopes must be canonical workspace-relative globs. Use `**` for the
whole workspace; do not use `repository/**`, `workspace/**`, absolute paths,
or `..` segments.

## Example

```json
{
  "schema": "runtime_graph_patch_v1",
  "expected_revision": 7,
  "rationale_summary": "Validator rejected the verifier because it had no target, so this creates a linked implementation node for the open goal instead.",
  "ops": [
    {
      "op": "create_node",
      "node_key": "produce-initial-runtime-result",
      "node_type": "implementation",
      "title": "Produce initial runtime result",
      "description": "Create the first concrete artifact or evidence needed to satisfy the initial runtime goal.",
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
