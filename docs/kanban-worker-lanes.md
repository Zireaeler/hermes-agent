# Kanban Worker Lanes

Hermes Kanban has two spawnable assignee classes:

- Hermes profile lanes, which preserve the existing behavior of spawning a
  Hermes profile for a claimed task.
- External worker lanes, which are trusted adapters registered in the worker
  lane registry. Codex CLI is the first built-in adapter.

The main Hermes agent stays the control plane. It creates and decomposes tasks,
queries status, plans review and test follow-ups, runs deterministic acceptance
checks, and decides whether a task can be marked done. External workers are the
execution plane. They implement, review, or verify code in a workspace and write
bounded evidence back to the Kanban database.

## Assignee Resolution

The dispatcher resolves an assignee in this order:

1. Registered worker lane.
2. Hermes profile.
3. `skipped_nonspawnable`.

This keeps existing Hermes profile worker behavior compatible while allowing
new external lanes to be added without hard-coding names in the dispatcher.
Unknown model-generated names are not treated as executable configuration.

Worker lanes can come from `kanban.worker_lanes` in config or from plugins that
call `PluginContext.register_worker_lane(...)`. Plugin registration failures are
logged and should not stop Hermes startup.

Inspect the trusted lane roster with:

```bash
hermes kanban worker-lanes --json
hermes kanban assignees --json
```

## Codex CLI Adapter

A Codex lane is configured under `kanban.worker_lanes`:

```yaml
kanban:
  worker_lanes:
    codex-fast:
      type: codex_cli
      model: gpt-5.4-mini
      sandbox: workspace-write
      approval: never
      max_concurrency: 2
      success_policy: block_for_review

    codex-deep:
      type: codex_cli
      model: gpt-5.5
      sandbox: workspace-write
      approval: never
      max_concurrency: 1
      success_policy: block_for_review

    codex-review:
      type: codex_cli
      model: gpt-5.5
      sandbox: read-only
      approval: never
      max_concurrency: 1
      success_policy: block_for_review
```

The adapter uses a fixed command shape controlled by the trusted config:

```bash
codex --cd <workspace> --sandbox <sandbox> --ask-for-approval <approval> exec -
```

If a model is configured, the adapter appends the supported model argument. It
does not execute arbitrary shell strings from a model, skill, or user-provided
lane request. The worker environment is allowlisted and does not forward every
secret or proxy variable by default.

Each run records lane-scoped identity such as:

```json
{
  "worker_lane": "codex-deep",
  "worker_kind": "codex_cli",
  "task_id": "t_1234",
  "run_id": 17,
  "worker_pid": 4567,
  "claim_lock": "claim...",
  "workspace": "/path/to/repo",
  "model": "gpt-5.5"
}
```

The dispatcher still owns task claiming, PID tracking, heartbeat staleness,
runtime timeout, crash detection, spawn failure counters, concurrency limits,
and worker lifecycle decisions. The lane only starts the external worker.

## Progress, Logs, And Metadata

Codex workers write evidence through the normal Kanban storage surfaces:

- worker log tail;
- lane/run-scoped heartbeat updates;
- `task_events`, including parsed `worker_progress` checklist events;
- bounded Codex JSON activity snapshots when available;
- structured metadata with output tail, git status, changed files, diff summary,
  verification commands, timeout state, and exit code.

Progress queries read these records without interrupting the running worker:

```bash
hermes kanban progress <task_id> --children --log-tail 4096
hermes kanban reviews --lane codex-deep
```

Tool callers can use `kanban_progress`, `kanban_reviews`, and
`kanban_acceptance`. The dashboard task drawer shows the same bounded evidence:
checklist progress, recent Codex activity, worker log tail, git/test metadata,
child state, acceptance gates, and review controls.

The progress parser recognizes both checklist styles:

```text
o (1) Analyze entry point
x (2) Modify dispatcher
o (3) Add tests

- [ ] Analyze entry point
- [x] Modify dispatcher
```

Parsed items are written as `worker_progress` task events so the dashboard and
main agent can query progress directly from the Kanban DB.

## Review And Acceptance

Codex success does not directly mark an implementation task done. With the
default `block_for_review` success policy, exit code 0 moves the task to
review-required evidence state, represented as a blocked task with
`review.required: true`.

Hermes should review Codex artifacts and bounded evidence, not the full Codex
session transcript. The intended flow is:

1. Implementation lane finishes and records evidence.
2. Hermes plans independent review and test follow-up tasks, often assigned to
   lanes such as `codex-review` and `codex-test`.
3. Review/test workers emit concise structured verdicts.
4. Hermes runs configured deterministic acceptance checks.
5. Hermes approves only after review/test/check gates pass.
6. Failed gates produce bounded request-changes feedback and can return the
   implementation task to `ready` until retry limits are exhausted.

If a planned review/test follow-up is assigned to a lane that is not a
registered worker lane and not a Hermes profile, the advance controller returns
a `blocked` step with `missing_lanes` instead of silently waiting forever. The
fix is to register the lane, persist an approved lane request, or reassign the
follow-up task to a spawnable assignee.

Large diffs can trigger review shard follow-ups. These shards are bounded
review tasks over subsets of changed files; every shard verdict must pass before
the source task can be approved. They are a control-plane review workflow, not a
claim that Hermes has semantically audited an arbitrary large diff by itself.

## Real Codex E2E Smoke

Unit tests use fake Codex processes. Before calling a worker-lane/control-plane
change ready on a machine with Codex auth, run the real smoke helper:

```bash
PYTHONPATH=/path/to/hermes-agent \
  /path/to/venv/bin/python scripts/smoke_kanban_codex_e2e.py
```

The helper creates a temporary `HERMES_HOME`, a temporary git workspace, and
three Codex lanes: `codex-impl`, `codex-review`, and `codex-test`. It preserves
the operator's normal `~/.codex`/`CODEX_HOME` auth state, does not touch the
deployment config, and waits only on tasks created by this smoke run.

A successful run proves the dispatcher can start a real implementation Codex
worker, progress can be queried while that worker is running, Codex completion
enters review-required state, real Codex review/test follow-up lanes can be
spawned, Hermes can run deterministic acceptance checks, and the controller can
approve the task from bounded evidence. Use `--model default` to omit an
explicit Codex model or `--keep` to retain the temporary DB, logs, and
workspace for inspection.

## Skill-Generated Lane Intent

Skills can choose an existing trusted lane by assigning a task:

```text
assignee=codex-deep
```

If a lane does not exist, a skill can submit a structured lane request:

```yaml
worker_lane_request:
  name: codex-long-context
  type: codex_cli
  model: gpt-5.5
  sandbox: workspace-write
  approval: never
  max_concurrency: 1
  success_policy: block_for_review
  reason: large refactor requiring stronger reasoning
```

The deterministic validator allowlists lane type, model, sandbox, approval
policy, success policy, and concurrency. It rejects arbitrary command, shell,
argv, and executable fields. Validated requests are only enabled or persisted by
a trusted operator/control path; decomposition output records lane request
intent on the task but does not automatically create executable lanes.

Trusted control paths can validate, enable, or persist a task-scoped intent and
write the decision back to `task_events`:

```bash
hermes kanban worker-lane-request lane.yaml \
  --enable \
  --task-id <root_task_id> \
  --source-event-id <worker_lane_request_intent_event_id> \
  --requested-by orchestrator
```

Tool callers use `kanban_worker_lane_request` with the same `task_id`,
`source_event_id`, and `requested_by` fields. Dashboard approvals use the same
audit event kinds: `worker_lane_request_validated` for validate-only and
`worker_lane_request_approved` when a lane is enabled or persisted.

## Goal Bridge

The initial goal bridge preserves existing `/goal` semantics while adding
Kanban handoff points:

1. `/goal create ...` can create or associate a top-level Kanban task.
2. Decomposition can create child tasks with concrete assignees.
3. Child implementation tasks can be routed to worker lanes.
4. `kanban_advance_goal` and the gateway controller can dispatch children,
   advance review/test/acceptance gates, and answer progress queries from
   Kanban state.

Running workers are not interrupted for user progress queries.

## Current Limits

- No full Codex event stream is exposed yet. Hermes stores bounded JSON activity
  snapshots and output tails, not the complete session.
- No Codex approval bridge is implemented. Lanes should use non-interactive
  approval settings such as `approval: never`.
- Deep review is workflow-based. Review shards and review/test lanes can be
  planned and gated, but Hermes still relies on worker verdicts and
  deterministic checks rather than automatically proving a large diff correct.
- Lane requests are intentionally conservative. They cannot define arbitrary
  executables or shell commands.
- Real deployment still needs configured Codex CLI auth and workspace access on
  the machine running the dispatcher.
