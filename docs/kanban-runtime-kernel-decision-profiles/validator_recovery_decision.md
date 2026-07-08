Profile-Version: 1

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

## Safe Fallback

When the best next structural action is unclear, return a small `create_node`
patch for a concrete implementation, analysis, research, debug, or verification
node linked to an unmet goal item or open gap.

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
      "goal_item_keys": ["initial-runtime-result"]
    }
  ]
}
```
