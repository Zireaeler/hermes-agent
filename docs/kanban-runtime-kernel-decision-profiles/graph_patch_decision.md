Profile-Version: 1

# Graph Patch Decision Profile

## Purpose

Use this profile when the runtime kernel has detected that the execution graph
needs structural work to keep moving toward the goal contract.

## Input

The provider receives only the runtime-rendered decision request:

- stable runtime contract;
- current goal contract;
- latest validated checkpoint;
- strict short tail;
- current decision delta.

The provider must not request hidden context, read worker logs, call tools, or
perform web search. If external research is required, return a graph patch that
creates a `research` node linked to the relevant goal item or gap.

## Output

Return exactly one JSON object matching `runtime_graph_patch_v1`.

No Markdown fences, no explanatory prose, no comments.

## Allowed Ops

- `create_node`
- `add_dependency`
- `insert_verifier`
- `request_human`
- `propose_blocked`
- `strategy_update`

## Forbidden Ops

- `release_node`
- `complete_job`
- direct database writes
- Kanban task creation
- worker execution
- web search or tool calls

## Required Semantics

Every new node must link to at least one goal item, gap, or human gate reason.
If the current graph is exhausted while goals remain unmet, create nodes that
address the unmet goal gaps instead of returning blocked.

Use exactly these field names for patch ops:

- `create_node`: `node_key`, `node_type`, `title`, `description`, and
  `goal_item_keys` or `gap_keys` or `human_gate_reason`; optional
  `depends_on` may list existing node keys.
- `add_dependency`: `from_node_key` is the prerequisite node and `to_node_key`
  is the dependent node; optional `dependency_type` defaults to `depends_on`.
- `insert_verifier`: `verifier_node_key`, `title`, either
  `target_node_key` or `target_goal_item_key`, and `goal_item_keys` or
  `gap_keys` for the verifier node's own goal/gap linkage.
- `request_human`: include `decision_type`, `question`,
  `default_recommendation`, `why_user_required`, and affected goal/gap keys.
- `strategy_update`: include `node_key`, `title`, `description`,
  `goal_item_keys` or `gap_keys`, `strategy_summary`, and
  `changes_from_previous_attempts`.

Use `insert_verifier` only when you can name an existing `target_node_key` from
the graph frontier or a real `target_goal_item_key` from the goal contract, and
you can also provide `goal_item_keys` or `gap_keys` for the verifier node. If
you are unsure, prefer `create_node` with `goal_item_keys` or `gap_keys`.

Never invent empty node keys, empty target fields, unknown goal keys, or
dependencies on nodes that are not present in the request.

Never use alias fields such as `node_key` / `depends_on_node_key` for
`add_dependency`; they are invalid. Use `from_node_key` / `to_node_key`.

Use `strategy_update` when anti-stuck signals, repeated failed attempts, or
stale gaps require a changed approach. It creates a materialized
`strategy_update` node; it does not complete or block the job.

## Example

```json
{
  "schema": "runtime_graph_patch_v1",
  "expected_revision": 7,
  "rationale_summary": "The runtime has no ready nodes and the usage documentation goal is still unmet.",
  "ops": [
    {
      "op": "create_node",
      "node_key": "write-usage-doc",
      "node_type": "implementation",
      "title": "Write usage documentation",
      "description": "Document how to run and verify the implemented feature.",
      "goal_item_keys": ["usage_doc"]
    }
  ]
}
```
