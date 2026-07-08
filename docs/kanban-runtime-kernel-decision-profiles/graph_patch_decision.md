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
