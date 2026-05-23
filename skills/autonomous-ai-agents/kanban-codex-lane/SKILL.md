---
name: kanban-codex-lane
description: Use when a Hermes orchestrator should route coding work to registered Codex CLI worker lanes while Hermes keeps control of task lifecycle, progress queries, review/test gates, and deterministic acceptance.
version: 2.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [kanban, codex, worker-lanes, autonomous-agents]
    related_skills: [kanban-worker, kanban-orchestrator, codex]
---

# Kanban Codex Worker Lanes

## Overview

Codex is an external Kanban worker lane, not a Hermes model provider and not a nested helper that a Hermes worker starts manually. Hermes is the control plane: it creates tasks, assigns lanes, monitors progress, plans review/test follow-ups, runs deterministic acceptance checks, and decides whether a task is done or needs another implementation pass.

Codex is the execution plane. When a task is assigned to a registered lane such as `codex-deep`, the Kanban dispatcher starts the Codex CLI adapter. The adapter writes worker logs, heartbeats, progress events, bounded evidence, git/test summaries, and then blocks the task for review. A successful Codex run should end in `blocked` with `review.required: true`, not `done`.

## Available Lane Shapes

Common lane names are configuration, not hard-coded behavior:

- `codex-fast` — smaller/faster implementation lane.
- `codex-deep` — stronger implementation lane for larger code changes.
- `codex-review` — read-only or low-write review lane.
- `codex-test` — independent verification lane.

Discover current lanes with:

```bash
hermes kanban worker-lanes --json
```

Or, from an orchestrator with the `kanban` toolset, read board state with `kanban_progress`, `kanban_reviews`, and `kanban_acceptance`. Do not infer that a lane exists because the name sounds plausible.

## Routing Contract

Use Codex lanes for implementation, code review, and independent test verification when the task has clear acceptance criteria and a workspace path. Keep Hermes out of the coding hot path unless the task is too small or too ambiguous to delegate.

Implementation task body should include:

- the concrete goal and acceptance criteria;
- the workspace/repo constraints;
- files or subsystems in scope;
- prohibited actions such as secret access, unrelated rewrites, dependency churn, or production side effects;
- verification commands the worker should run when known;
- expected structured receipt shape.

For implementation:

```python
task = kanban_create(
    title="Implement bounded retry for worker lane failures",
    assignee="codex-deep",
    body="Work in the configured workspace only. Implement bounded retry for failed external worker lane runs. Preserve existing dispatcher lifecycle semantics. Run focused tests and report changed files, verification, and risks.",
    acceptance_check_request={
        "name": "focused-unit-test",
        "type": "command_template",
        "template": "pytest-target",
        "args": {"target": "tests/test_worker_lanes.py"},
    },
)["task_id"]
```

Use `acceptance_check_request` or `acceptance_check_requests` during
`kanban_create` whenever the acceptance condition is concrete. These requests
are validated by Hermes and run later by the controller; Codex workers should
not invent commands or mutate the board themselves.

For review/test, prefer the controller workflow rather than hand-authoring cards:

```python
kanban_advance_acceptance(
    task_id=task,
    review_assignee="codex-review",
    test_assignee="codex-test",
    loop=True,
)
```

For decomposed goals:

```python
kanban_advance_goal(
    task_id=root_task,
    review_assignee="codex-review",
    test_assignee="codex-test",
    loop=True,
)
```

These controller tools never wait for or interrupt running Codex workers. With `loop=True`, they repeat bounded deterministic passes until they are done, idle, blocked, waiting on a running worker, or at the iteration cap. They dispatch ready children/follow-ups, inspect bounded evidence, run configured deterministic acceptance checks, feed bounded request-changes comments back to implementation lanes, and approve only when gates pass.

For unattended operation, use `kanban_advance_controller(...)` or the gateway's
`kanban.advance_controller_in_gateway` tick. It scans decomposed roots and
standalone review-required implementation tasks, advances each to the next
idle boundary, and leaves running Codex workers alone.

## Lane Requests

Model output is not trusted execution config. If a suitable Codex lane does not exist, submit a lane request through the validator instead of inventing an assignee:

```python
kanban_worker_lane_request(
    worker_lane_request={
        "name": "codex-long-context",
        "type": "codex_cli",
        "model": "gpt-5.5",
        "sandbox": "workspace-write",
        "approval": "never",
        "max_concurrency": 1,
        "success_policy": "block_for_review",
        "reason": "large refactor requiring stronger reasoning",
    }
)
```

The default is validate-only. A trusted operator/orchestrator can pass `enable=True` for this process or `persist=True` to write sanitized config. The validator allowlists lane type, model, sandbox, approval policy, success policy, and concurrency; it rejects arbitrary command, shell, argv, and executable fields.

When decomposition output contains `worker_lane_request` or
`worker_lane_requests`, Hermes records a validated
`worker_lane_request_intent` event on the root task but does not register the
lane. Assign work only to already registered lanes until the request is
explicitly enabled or persisted through the trusted validator path.

## Progress And Review

Progress queries must read Kanban state without touching the worker process:

```python
kanban_progress(task_id=task, log_tail_bytes=4096)
kanban_acceptance(task_id=task)
kanban_reviews(lane="codex-deep")
```

The Codex adapter parses simple checklist output into `worker_progress` task events, records lane/run-scoped worker heartbeats, and writes logs under the normal Kanban worker log path. The main agent and dashboard should read these records instead of asking the running Codex process for status.

Review strategy:

1. Implementation Codex run exits 0 and blocks for review with bounded evidence.
2. Hermes plans independent `codex-review` and `codex-test` follow-ups.
3. Follow-up workers inspect the workspace/diff and must end with a structured verdict line:
   - review: `Verdict: approve`, `Verdict: request_changes`, or `Verdict: blocked`
   - test: `Verdict: pass`, `Verdict: fail`, or `Verdict: blocked`
4. Hermes runs configured `kanban.acceptance_checks` as fixed argv commands.
5. If every gate passes, Hermes approves and marks the implementation task done.
6. If a gate fails, Hermes writes bounded request-changes feedback and returns the implementation task to `ready` until the retry limit is reached.

Hermes reviews Codex artifacts and bounded metadata, not the full Codex session. Large diffs should be reviewed through follow-up workers and deterministic checks, not by replaying every token of the implementation run.

## Safety Rules

- Do not ask Codex to call Hermes Kanban tools or mutate board state.
- Do not let user or model output supply arbitrary shell commands for a lane.
- Do not forward all secrets or proxy variables into Codex lanes.
- Do not mark implementation tasks done directly after Codex exits.
- Do not interrupt running workers when the user asks for progress.
- Do not approve large diffs based only on Codex self-report.
- Prefer a new review/test follow-up or deterministic check over having Hermes semantically judge a large code change by itself.

## Verification Checklist

- [ ] The target lane exists in `hermes kanban worker-lanes --json`, or a validated lane request was submitted.
- [ ] The implementation task body has concrete scope, workspace, acceptance criteria, and verification expectations.
- [ ] Progress was read from Kanban state/events/logs, not by interrupting the worker.
- [ ] Successful implementation ended as `blocked` with `review.required: true`.
- [ ] Independent review/test follow-ups were planned or intentionally skipped for a narrow reason.
- [ ] Follow-up workers emitted purpose-specific `Verdict:` lines.
- [ ] Hermes acceptance checks ran when configured.
- [ ] Approval happened only after review/test/check gates passed; failures produced bounded request-changes feedback.
