Profile-Version: 2

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
perform web search.

External research does not by itself justify a separate runtime node. When
research, implementation, testing, and debugging share the same workspace,
capability envelope, accountable outcome, and feedback loop, include them in
one coherent primary worker node. Create a separate research node only when a
durable structural boundary exists, such as capability isolation, independent
deliverable ownership, workspace isolation, or execution-discovered inability
of the primary worker to continue.

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

Use the minimum number of runtime nodes necessary. Prefer one primary node that
owns a complete outcome and may inspect, plan, modify, test, debug, and verify
within one continuous worker session. Do not split by phase, role, file, tool
call, or technical discipline. When uncertain, do not split initially.

Without a valid `decomposition`, create at most one new runnable worker node.
Multiple durable nodes, an independent verifier, parallel writers, or different
capability envelopes require a versioned `decomposition` with an allowed
structural reason and evidence where required.

Use exactly these field names for patch ops:

- `create_node`: `node_key`, `node_type`, `title`, `description`, and
  `goal_item_keys` or `gap_keys` or `human_gate_reason`; optional
  `depends_on` may list existing node keys. Include `contract` with `outcome`,
  `acceptance_criteria`, `success_evidence`, `declared_write_scope`, and
  `prohibited_actions`. Write scopes are canonical workspace-relative globs:
  use `**` for the whole workspace or paths such as `src/**` and `tests/**`.
  Never prefix them with `repository/` or `workspace/`, and never use absolute
  paths or `..` segments.
- `add_dependency`: `from_node_key` is the prerequisite node and `to_node_key`
  is the dependent node; optional `dependency_type` defaults to `depends_on`.
- `insert_verifier`: `verifier_node_key`, `title`, either
  `target_node_key` or `target_goal_item_key`, and `goal_item_keys` or
  `gap_keys` for the verifier node's own goal/gap linkage. Also fix at least
  one immutable target reference: `target_evidence_ref`,
  `target_materialization_attempt`, `target_artifact_ref`, or
  `target_workspace_revision`. A `target_evidence_ref` must use a validator
  supported immutable format: `receipt:<node_key>:attempt-<n>`,
  `event:<event_id>`, or `artifact:<artifact_id_or_ref>`. Do not copy a
  mutable `node:<id>` ledger reference into this field.
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
      "goal_item_keys": ["usage_doc"],
      "contract": {
        "outcome": "Deliver verified usage documentation for the implemented feature.",
        "acceptance_criteria": ["Run instructions are complete", "Verification steps are executable"],
        "success_evidence": ["changed_files", "verification", "worker_summary"],
        "declared_write_scope": ["docs/**"],
        "prohibited_actions": ["production_deployment"]
      }
    }
  ]
}
```
